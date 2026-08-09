"""Execute quality rules against a layer and record what they found.

Detections are written **long-format** — one row per (record key, rule, field,
family) — to a Delta table in the lake, not to Postgres. Two reasons: a run over
60k records with 30 rules can produce hundreds of thousands of detection rows, and
Stage 7b needs to join them against the ground-truth answer key, which is a
Delta-to-Delta join rather than a database round trip.

Postgres receives only the aggregates the dashboard queries.

**Field-level detection is the point.** Recording "this record failed" is not
enough to score a rule: a null check that fires on the wrong column would look
correct. Every detection names the column it fired on, which is what makes Stage
7b's precision measurement meaningful rather than flattering.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from confidentia.catalog.contracts import get_contract
from confidentia.common.logging import get_logger
from confidentia.config.settings import get_settings
from confidentia.quality.rules import Dimension, Rule, Severity, build_rules
from confidentia.quality.scoring import QualityScore, RuleOutcome, score_dataset
from confidentia.spark.session import (
    column_keys_for_frame,
    key_ids_by_sensitivity,
    session_for_tenant,
    set_column_keys,
)
from confidentia.store import db
from confidentia.store.repositories import RunType, track_run

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import DataFrame, SparkSession

logger = get_logger(__name__)

# `layer` is part of the row, not just the path: the detections table is
# overwritten per run, so a Silver run replaces a Bronze one in place and nothing
# on disk would say which it was. Stage 7b scores against the ground-truth answer
# key, which describes what the *source* sent -- scoring Silver detections against
# it silently measures the platform's own cleaning instead. Recording the layer
# lets the evaluation refuse rather than quietly produce a flattering number.
DETECTION_COLUMNS = (
    "record_key", "rule_id", "family", "dimension", "field", "severity", "dataset", "tenant",
    "layer",
)


@dataclass
class QualityRunResult:
    tenant: str
    dataset: str
    layer: str
    score: QualityScore
    detections: int = 0
    outcomes: list[RuleOutcome] = field(default_factory=list)
    freshness_lag_minutes: float | None = None
    freshness_breached: bool = False
    duration_seconds: float = 0.0

    def summary(self) -> str:
        return f"{self.score.summary()}  {self.detections:,} detections"


def detections_path(tenant: str, dataset: str) -> Path:
    return get_settings().lake_root / "quality" / tenant / dataset / "detections"


def _layer_path(tenant: str, dataset: str, layer: str) -> Path:
    settings = get_settings()
    return settings.lake_root / layer / tenant / dataset


def register_rules(rules: list[Rule]) -> None:
    """Upsert the rule catalogue so results have something to reference."""
    if not rules:
        return
    with db.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO platform.dq_rule
                (rule_id, tenant, dataset, dimension, family, field, severity,
                 description, is_derived)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (rule_id) DO UPDATE
                SET description = EXCLUDED.description,
                    severity    = EXCLUDED.severity,
                    dimension   = EXCLUDED.dimension,
                    is_active   = TRUE
            """,
            [
                (
                    r.rule_id, r.tenant, r.dataset, r.dimension.value, r.family,
                    r.field, r.severity.value, r.description, r.is_derived,
                )
                for r in rules
            ],
        )


def _uniqueness_detections(frame: DataFrame, contract, tenant: str) -> tuple[DataFrame, Rule]:
    """Duplicate business keys. Needs a window, so it cannot be a row-level rule."""
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    key = contract.business_key[0]
    rule = Rule(
        rule_id=f"{tenant}.{contract.dataset}.duplicate_record.{key}",
        dataset=contract.dataset,
        tenant=tenant,
        dimension=Dimension.UNIQUENESS,
        family="duplicate_record",
        severity=Severity.MAJOR,
        description=f"{key} appears more than once",
        predicate=lambda: F.lit(False),
        field=key,
    )
    counted = frame.withColumn(
        "_key_count", F.count(F.lit(1)).over(Window.partitionBy(key))
    )
    detections = (
        counted.filter(F.col("_key_count") > 1)
        .select(F.col(key).cast("string").alias("record_key"))
        .withColumn("rule_id", F.lit(rule.rule_id))
        .withColumn("family", F.lit(rule.family))
        .withColumn("dimension", F.lit(rule.dimension.value))
        .withColumn("field", F.lit(key))
        .withColumn("severity", F.lit(rule.severity.value))
        .withColumn("dataset", F.lit(contract.dataset))
        .withColumn("tenant", F.lit(tenant))
    )
    return detections, rule


def _referential_detections(
    spark: SparkSession, frame: DataFrame, contract, tenant: str, layer: str
) -> tuple[DataFrame | None, list[Rule]]:
    """Foreign keys pointing at rows that do not exist.

    Needs the parent dataset, so it cannot be a row-level rule either. Parents are
    read from the same layer: checking Silver against Bronze would compare against
    records Silver has already quarantined.
    """
    from pyspark.sql import functions as F

    # (foreign key column, parent dataset, parent key column)
    relationships = {
        "encounter": [("patient_id", "patient", "patient_id")],
        "claim_line": [("encounter_id", "encounter", "encounter_id")],
        "transaction": [("account_id", "account", "account_id")],
        "chargeback": [("transaction_id", "transaction", "transaction_id")],
    }.get(contract.dataset, [])

    frames: list[DataFrame] = []
    rules: list[Rule] = []

    for fk, parent_dataset, parent_key in relationships:
        if fk not in frame.columns:
            continue
        parent_path = _layer_path(tenant, parent_dataset, layer)
        if not parent_path.exists():
            logger.warning(
                "referential_check_skipped",
                dataset=contract.dataset, parent=parent_dataset,
                reason="parent table not built",
            )
            continue

        rule = Rule(
            rule_id=f"{tenant}.{contract.dataset}.referential_orphan.{fk}",
            dataset=contract.dataset,
            tenant=tenant,
            dimension=Dimension.INTEGRITY,
            family="referential_orphan",
            severity=Severity.CRITICAL,
            description=f"{fk} references a {parent_dataset} that does not exist",
            predicate=lambda: F.lit(False),
            field=fk,
            quarantines=True,
        )
        rules.append(rule)

        parent = (
            spark.read.format("delta").load(str(parent_path))
            .select(F.col(parent_key).alias("_parent_key"))
            .distinct()
        )
        key = contract.business_key[0]
        orphans = (
            frame.select(F.col(key).cast("string").alias("record_key"), F.col(fk))
            .filter(F.col(fk).isNotNull())
            .join(parent, F.col(fk) == F.col("_parent_key"), "left_anti")
            .select("record_key")
            .withColumn("rule_id", F.lit(rule.rule_id))
            .withColumn("family", F.lit(rule.family))
            .withColumn("dimension", F.lit(rule.dimension.value))
            .withColumn("field", F.lit(fk))
            .withColumn("severity", F.lit(rule.severity.value))
            .withColumn("dataset", F.lit(contract.dataset))
            .withColumn("tenant", F.lit(tenant))
        )
        frames.append(orphans)

    if not frames:
        return None, rules
    combined = frames[0]
    for extra in frames[1:]:
        combined = combined.unionByName(extra)
    return combined, rules


def _record_freshness(frame: DataFrame, tenant: str, dataset: str, layer: str) -> tuple[float | None, bool]:
    """Freshness is a dataset property, not a row property."""
    from pyspark.sql import functions as F

    column = next((c for c in ("event_ts", "ingest_ts", "_ingested_at") if c in frame.columns), None)
    if column is None:
        return None, False

    row = frame.select(
        F.max(F.col(column)).alias("max_ts"),
        F.max(F.col("_ingested_at")).alias("max_ingest")
        if "_ingested_at" in frame.columns
        else F.max(F.col(column)).alias("max_ingest"),
    ).first()
    if row is None or row["max_ts"] is None:
        return None, False

    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    max_ts = row["max_ts"]
    if max_ts.tzinfo is None:
        max_ts = max_ts.replace(tzinfo=dt.UTC)
    lag_minutes = (now - max_ts).total_seconds() / 60

    sla = get_settings().quality.freshness_sla_minutes
    breached = lag_minutes > sla

    db.execute(
        """
        INSERT INTO platform.dq_freshness
            (tenant, dataset, layer, max_event_ts, max_ingest_ts, lag_minutes,
             sla_minutes, breached, observed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (tenant, dataset, layer) DO UPDATE
            SET max_event_ts = EXCLUDED.max_event_ts,
                max_ingest_ts = EXCLUDED.max_ingest_ts,
                lag_minutes = EXCLUDED.lag_minutes,
                breached = EXCLUDED.breached,
                observed_at = now()
        """,
        (tenant, dataset, layer, row["max_ts"], row["max_ingest"], lag_minutes, sla, breached),
    )
    return lag_minutes, breached


def run_quality(
    tenant: str,
    dataset: str,
    *,
    layer: str = "silver",
) -> QualityRunResult:
    """Evaluate every rule for a dataset and persist results, score and detections."""
    started = time.perf_counter()
    settings = get_settings()
    contract = get_contract(tenant, dataset)
    source = _layer_path(tenant, dataset, layer)

    if not source.exists():
        raise FileNotFoundError(f"no {layer} table at {source}")

    rules = build_rules(contract, layer)

    with track_run(
        RunType.QUALITY, tenant=tenant, dataset=dataset, source="quality",
        metadata={"layer": layer},
    ) as run:
        with session_for_tenant(tenant, run_id=run.run_id) as (spark, _keys):
            from pyspark.sql import functions as F

            set_column_keys(spark, contract.column_keys_property(key_ids_by_sensitivity(tenant)))
            frame = spark.read.format("delta").load(str(source))
            frame.cache()
            rows_total = frame.count()

            key = contract.business_key[0]
            outcomes: list[RuleOutcome] = []
            detection_frames: list[DataFrame] = []

            # Row-level rules: evaluate all of them in a single pass by projecting
            # one boolean column per rule, rather than filtering the table once per
            # rule. With 30 rules that is one scan instead of thirty.
            flags = frame.select(
                F.col(key).cast("string").alias("record_key"),
                *[
                    rule.predicate().alias(f"r{i}")
                    for i, rule in enumerate(rules)
                ],
            )
            flags.cache()

            counts = flags.select(
                *[
                    F.sum(F.col(f"r{i}").cast("long")).alias(f"r{i}")
                    for i in range(len(rules))
                ]
            ).first()

            for index, rule in enumerate(rules):
                failed = int(counts[f"r{index}"] or 0) if counts else 0
                outcomes.append(
                    RuleOutcome(rule=rule, rows_evaluated=rows_total, rows_failed=failed)
                )
                if failed:
                    detection_frames.append(
                        flags.filter(F.col(f"r{index}"))
                        .select("record_key")
                        .withColumn("rule_id", F.lit(rule.rule_id))
                        .withColumn("family", F.lit(rule.family))
                        .withColumn("dimension", F.lit(rule.dimension.value))
                        .withColumn("field", F.lit(rule.field))
                        .withColumn("severity", F.lit(rule.severity.value))
                        .withColumn("dataset", F.lit(dataset))
                        .withColumn("tenant", F.lit(tenant))
                    )

            # Uniqueness and integrity need more than one row.
            unique_detections, unique_rule = _uniqueness_detections(frame, contract, tenant)
            unique_failed = unique_detections.count()
            rules.append(unique_rule)
            outcomes.append(
                RuleOutcome(rule=unique_rule, rows_evaluated=rows_total, rows_failed=unique_failed)
            )
            if unique_failed:
                detection_frames.append(unique_detections)

            ref_detections, ref_rules = _referential_detections(
                spark, frame, contract, tenant, layer
            )
            rules.extend(ref_rules)
            if ref_detections is not None and ref_rules:
                ref_counts = (
                    ref_detections.groupBy("rule_id").count().collect()
                )
                found = {row["rule_id"]: row["count"] for row in ref_counts}
                for rule in ref_rules:
                    outcomes.append(
                        RuleOutcome(
                            rule=rule,
                            rows_evaluated=rows_total,
                            rows_failed=int(found.get(rule.rule_id, 0)),
                        )
                    )
                if found:
                    detection_frames.append(ref_detections)
            else:
                for rule in ref_rules:
                    outcomes.append(
                        RuleOutcome(rule=rule, rows_evaluated=0, rows_failed=0)
                    )

            lag_minutes, breached = _record_freshness(frame, tenant, dataset, layer)

            detections = 0
            if detection_frames:
                combined = detection_frames[0]
                for extra in detection_frames[1:]:
                    combined = combined.unionByName(extra)
                combined = combined.withColumn("layer", F.lit(layer)).select(*DETECTION_COLUMNS)

                # The detections table has its own schema, so it needs its own
                # column-key mapping. Leaving the contract's mapping in place fails
                # the write with "Encrypted column [event_ts] not in file schema".
                #
                # Only record_key is sensitive -- it identifies which record failed.
                # Rule metadata is deliberately plaintext so the Stage 7b join and
                # the dashboard's group-by can push down.
                set_column_keys(
                    spark,
                    column_keys_for_frame(
                        combined,
                        tenant,
                        plaintext={
                            "rule_id", "family", "dimension", "field",
                            "severity", "dataset", "tenant", "layer",
                        },
                    ),
                )
                target = detections_path(tenant, dataset)
                combined.write.format("delta").mode("overwrite").option(
                    "overwriteSchema", "true"
                ).save(str(target))
                detections = spark.read.format("delta").load(str(target)).count()

            rows_failing = (
                spark.read.format("delta")
                .load(str(detections_path(tenant, dataset)))
                .select("record_key")
                .distinct()
                .count()
                if detections
                else 0
            )

        register_rules(rules)
        score = score_dataset(
            outcomes,
            tenant=tenant,
            dataset=dataset,
            layer=layer,
            rows_total=rows_total,
            rows_failing=rows_failing,
            promotion_threshold=settings.quality.promotion_threshold,
        )
        _persist(run.run_id, tenant, dataset, layer, outcomes, score)

        run.add(read=rows_total, rejected=rows_failing)
        run.note(
            score=round(score.overall, 4),
            rules_failed=score.rules_failed,
            detections=detections,
            freshness_breached=breached,
        )

    result = QualityRunResult(
        tenant=tenant,
        dataset=dataset,
        layer=layer,
        score=score,
        detections=detections,
        outcomes=outcomes,
        freshness_lag_minutes=lag_minutes,
        freshness_breached=breached,
        duration_seconds=time.perf_counter() - started,
    )
    logger.info("quality_evaluated", **{
        "tenant": tenant, "dataset": dataset, "score": round(score.overall, 4),
        "detections": detections, "blocked": score.promotion_blocked,
    })
    return result


def _persist(
    run_id: Any,
    tenant: str,
    dataset: str,
    layer: str,
    outcomes: list[RuleOutcome],
    score: QualityScore,
) -> None:
    with db.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO platform.dq_result
                (run_id, rule_id, tenant, dataset, layer, rows_evaluated,
                 rows_failed, pass_rate)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    run_id, o.rule.rule_id, tenant, dataset, layer,
                    o.rows_evaluated, o.rows_failed, o.pass_rate,
                )
                for o in outcomes
            ],
        )
    db.execute(
        """
        INSERT INTO platform.dq_score
            (run_id, tenant, dataset, layer, overall_score, completeness, validity,
             consistency, uniqueness, integrity, timeliness, rows_total,
             rows_failing, rules_evaluated, rules_failed, promotion_blocked)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run_id, tenant, dataset, layer, score.overall,
            score.dimensions.get(Dimension.COMPLETENESS),
            score.dimensions.get(Dimension.VALIDITY),
            score.dimensions.get(Dimension.CONSISTENCY),
            score.dimensions.get(Dimension.UNIQUENESS),
            score.dimensions.get(Dimension.INTEGRITY),
            score.dimensions.get(Dimension.TIMELINESS),
            score.rows_total, score.rows_failing,
            score.rules_evaluated, score.rules_failed, score.promotion_blocked,
        ),
    )
