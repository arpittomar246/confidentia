"""Gold layer: star schema, KPI marts, disclosure control, and restatement.

Two storage targets with different jobs:

*Delta (encrypted)* holds the star schema — facts and dimensions at row grain, still
protected by per-column keys. Analysts query it through Spark.

*Postgres* holds only **disclosure-controlled aggregates**. It exists because
delta-rs cannot read encrypted Parquet (ADR 0007), so the JVM-free API cannot query
the lakehouse at all. Rather than weaken the lakehouse to make it readable, only
aggregates that have passed suppression cross the boundary. Nothing at row grain and
no identifier ever reaches Postgres.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from psycopg import sql

from confidentia.common.logging import get_logger
from confidentia.config.settings import Layer, Tenant, get_settings
from confidentia.pipelines.bronze.writer import verify_encrypted
from confidentia.pipelines.gold import star
from confidentia.pipelines.gold.disclosure import (
    assert_no_small_cells,
    enforce,
)
from confidentia.pipelines.gold.marts import MartSpec, marts_for
from confidentia.pipelines.silver.writer import silver_path
from confidentia.spark.session import key_ids_by_sensitivity, session_for_tenant, set_column_keys
from confidentia.store import db
from confidentia.store.repositories import RunType, track_run

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import DataFrame

logger = get_logger(__name__)


@dataclass
class GoldResult:
    tenant: str
    tables_built: dict[str, int] = field(default_factory=dict)
    marts_published: dict[str, int] = field(default_factory=dict)
    suppression: dict[str, str] = field(default_factory=dict)
    partitions_restated: int = 0
    duration_seconds: float = 0.0

    def summary(self) -> str:
        return (
            f"{self.tenant}: {len(self.tables_built)} star tables, "
            f"{len(self.marts_published)} marts published, "
            f"{self.partitions_restated} partitions restated, "
            f"{self.duration_seconds:.1f}s"
        )


def gold_path(tenant: str, table: str) -> Path:
    return get_settings().layer_path(Layer.GOLD, Tenant(tenant), table)


def load_reference(spark, tenant: str, dataset: str) -> DataFrame:
    """Read a small reference table straight from the raw drop.

    Facilities, providers and merchants are non-sensitive lookup data with no
    identifiers, no history and a few hundred rows. Putting them through the full
    medallion would add three Delta tables and two pipeline stages to protect
    nothing — the sensitivity classification for every column is `public`. They are
    read here directly and deliberately.

    The trade-off: they are not versioned, so a facility rename overwrites history.
    Acceptable for Type-1 attributes; a facility that needed history would have to
    move into Silver as an SCD2 dimension like `patient`.
    """
    root = get_settings().data_root / "raw" / tenant / dataset
    parts = sorted(root.glob("part-*.parquet"))
    if not parts:
        raise FileNotFoundError(f"no reference data at {root}; run generation first")
    return spark.read.parquet(*[str(p) for p in parts])


# Gold column classification.
#
# Gold has a different shape from Silver: identifiers are already tokenised, and
# what remains sensitive is the *measures* (amounts, counts, clinical codes) and the
# tokens themselves. The Silver column-key mapping cannot be reused — it names
# columns like `mrn` and `ssn` that a fact table does not have, and Parquet fails
# the write with "column not in schema" rather than ignoring them.
#
# Left in plaintext: partition and grouping columns, surrogate keys (SHA-256 of a
# natural key, so they disclose nothing on their own), calendar attributes, and
# low-sensitivity categorical labels. That is what keeps partition pruning and
# group-by pushdown working.
GOLD_PLAINTEXT_COLUMNS = frozenset(
    {
        "event_date",
        "date_sk",
        "date_value",
        "year",
        "quarter",
        "month",
        "month_name",
        "day_of_month",
        "day_of_week",
        "day_name",
        "week_of_year",
        "is_weekend",
        "year_month",
        "event_hour",
        "encounter_type",
        "disposition",
        "channel",
        "entry_mode",
        "auth_response",
        "currency",
        "mcc",
        "mcc_category",
        "risk_tier",
        "terminal_country",
        "account_type",
        "status",
        "home_state",
        "home_country",
        "state",
        "city",
        "facility_type",
        "specialty",
        "body_system",
        "is_current",
        "is_late_arrival",
        "is_disputed",
        "is_denied",
        "is_active",
        "is_accepting_patients",
        "version_number",
        # array<string> of *attribute names* that changed on an SCD2 version --
        # "state", "plan_id" -- never their values. Non-sensitive, and Parquet
        # cannot encrypt a nested column by top-level name anyway.
        "changed_attributes",
    }
)


class ComplexColumnEncryptionError(RuntimeError):
    """A sensitive column has a type Parquet cannot encrypt by name."""


def gold_column_keys(frame: DataFrame, tenant: str) -> str:
    """Column-key mapping derived from the columns actually present.

    Everything not explicitly plaintext is encrypted under the tenant's `internal`
    key. Deriving from the frame rather than from a static contract means a Gold
    table cannot be written with a mapping that names a column it does not have.

    **Complex types cannot be encrypted by top-level name.** Parquet Modular
    Encryption addresses leaf paths, so an `array<string>` column must be listed as
    `col.list.element`; naming `col` fails the write. Rather than silently drop
    such columns from the mapping — which would write them in *plaintext* while the
    code appeared to encrypt them — any complex column that is not explicitly
    allow-listed as plaintext raises. A complex column carrying sensitive data must
    be flattened before it reaches Gold.
    """
    from confidentia.config.settings import Sensitivity

    key_id = key_ids_by_sensitivity(tenant)[Sensitivity.INTERNAL]

    complex_columns = {
        field.name
        for field in frame.schema.fields
        if field.dataType.typeName() in {"array", "map", "struct"}
    }
    unhandled = complex_columns - GOLD_PLAINTEXT_COLUMNS
    if unhandled:
        raise ComplexColumnEncryptionError(
            f"columns {sorted(unhandled)} have a complex type that Parquet cannot "
            "encrypt by name. Flatten them before Gold, or add them to "
            "GOLD_PLAINTEXT_COLUMNS if they genuinely carry nothing sensitive. "
            "Leaving them out of the mapping would write them in plaintext."
        )

    encrypted = sorted(
        c
        for c in frame.columns
        if c not in GOLD_PLAINTEXT_COLUMNS
        and c not in complex_columns
        and not c.endswith("_sk")
    )
    if not encrypted:
        # Pure lookup table (dim_date). Nothing sensitive, so footer-only
        # encryption still protects the schema without pointless column crypto.
        return f"{key_id}:{frame.columns[0]}"
    return f"{key_id}:{','.join(encrypted)}"


def _write(
    frame: DataFrame,
    tenant: str,
    table: str,
    *,
    spark,
    partition: str | None = None,
) -> int:
    target = gold_path(tenant, table)
    set_column_keys(spark, gold_column_keys(frame, tenant))

    if partition and partition in frame.columns:
        # Repartition first: without it every task writes into every date partition
        # and the table explodes into tiny files (Stage 5).
        (
            frame.repartition(partition)
            .write.format("delta")
            .mode("overwrite")
            .option("overwriteSchema", "true")
            .partitionBy(partition)
            .save(str(target))
        )
    else:
        (
            frame.write.format("delta")
            .mode("overwrite")
            .option("overwriteSchema", "true")
            .save(str(target))
        )

    ok, detail = verify_encrypted(target)
    if not ok:
        raise RuntimeError(f"gold table {tenant}/{table} is not encrypted ({detail})")
    return frame.count()


# --------------------------------------------------------------- restatement


def mark_dirty_partitions(tenant: str, mart: str, dates: list[date], reason: str) -> int:
    if not dates:
        return 0
    with db.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO platform.mart_partition_state
                (tenant, mart, event_date, is_dirty, dirty_reason, dirtied_at)
            VALUES (%s, %s, %s, TRUE, %s, now())
            ON CONFLICT (tenant, mart, event_date) DO UPDATE
                SET is_dirty = TRUE, dirty_reason = EXCLUDED.dirty_reason, dirtied_at = now()
            """,
            [(tenant, mart, d, reason) for d in dates],
        )
    return len(dates)


def detect_late_arrival_restatements(
    fact_transaction: DataFrame, tenant: str = "financial"
) -> list[date]:
    """Find event dates whose aggregates a late dispute has invalidated.

    A chargeback filed in June for a March transaction makes the March aggregate
    wrong. Recomputing every partition every run would be correct and unaffordable;
    this returns only the dates that actually moved.
    """
    from pyspark.sql import functions as F

    affected = (
        fact_transaction.filter(F.col("is_disputed") & (F.col("days_to_dispute") > 1))
        .select(F.col("event_date"))
        .distinct()
        .collect()
    )
    return [row["event_date"] for row in affected if row["event_date"] is not None]


def record_restatement(*, run_id, tenant: str, mart: str, dates: list[date], reason: str) -> None:
    if not dates:
        return
    ordered = sorted(dates)
    db.execute(
        """
        INSERT INTO platform.restatement_log
            (run_id, tenant, mart, partitions_restated, trigger_reason,
             oldest_partition, newest_partition, lag_days)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run_id,
            tenant,
            mart,
            len(ordered),
            reason,
            ordered[0],
            ordered[-1],
            (ordered[-1] - ordered[0]).days,
        ),
    )
    with db.cursor() as cur:
        cur.executemany(
            """
            UPDATE platform.mart_partition_state
               SET is_dirty = FALSE, computed_at = now(),
                   restated_count = restated_count + 1
             WHERE tenant = %s AND mart = %s AND event_date = %s
            """,
            [(tenant, mart, d) for d in ordered],
        )


# ----------------------------------------------------------------- publishing


def publish_mart(frame: DataFrame, spec: MartSpec, *, run_id, report) -> int:
    """Materialise a disclosure-controlled aggregate into Postgres.

    Marts are small (hundreds to thousands of rows), so the frame is collected and
    written with COPY rather than streamed over JDBC — which would mean shipping a
    driver jar and a second connection pool for no benefit at this size.
    """
    import pandas as pd

    pdf: pd.DataFrame = frame.toPandas()
    table = sql.Identifier("gold", spec.name)
    columns = list(pdf.columns)

    def pg_type(series: pd.Series) -> str:
        kind = str(series.dtype)
        if "int" in kind:
            return "BIGINT"
        if "float" in kind:
            return "DOUBLE PRECISION"
        if "bool" in kind:
            return "BOOLEAN"
        if "datetime" in kind:
            return "TIMESTAMPTZ"
        if series.map(lambda v: isinstance(v, date)).any():
            return "DATE"
        return "TEXT"

    # Column types must be inferred from the original dtypes, before the NA
    # normalisation below flattens everything to object.
    definitions = sql.SQL(", ").join(
        sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(pg_type(pdf[c]))) for c in columns
    )

    # Suppression deliberately blanks cells, so a protected mart is full of missing
    # values -- and pandas represents them with three different sentinels (NaN,
    # NaT, pd.NA) depending on dtype. psycopg adapts none of them and raises
    # "cannot adapt type 'NAType'". Normalising every flavour to Python None is
    # what makes a suppressed cell arrive as a genuine SQL NULL.
    pdf = pdf.astype(object).where(pd.notna(pdf), None)

    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(table))
        cur.execute(sql.SQL("CREATE TABLE {} ({})").format(table, definitions))
        copy_stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
            table, sql.SQL(", ").join(sql.Identifier(c) for c in columns)
        )
        with cur.copy(copy_stmt) as copy:
            for row in pdf.itertuples(index=False, name=None):
                copy.write_row(row)

    db.execute(
        """
        INSERT INTO platform.disclosure_log
            (run_id, tenant, mart, min_cell_count, total_cells, primary_suppressed,
             complementary_suppressed, differential_privacy, epsilon, rows_published)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run_id,
            spec.tenant,
            spec.name,
            spec.policy.min_cell_count,
            report.total_cells,
            report.primary_suppressed,
            report.complementary_suppressed,
            spec.policy.differential_privacy,
            spec.policy.epsilon,
            len(pdf),
        ),
    )
    return len(pdf)


# ---------------------------------------------------------------------- build


def build_gold(tenant: str) -> GoldResult:
    """Build the star schema and publish every mart for one tenant."""
    started = time.perf_counter()
    result = GoldResult(tenant=tenant)
    pending_restatement: tuple[str, list[date]] | None = None

    with track_run(
        RunType.TRANSFORM,
        tenant=tenant,
        dataset="gold",
        source="gold",
        metadata={"layer": "gold"},
    ) as run:
        with session_for_tenant(tenant, run_id=run.run_id) as (spark, _keys):
            from confidentia.catalog.contracts import get_contract

            def load(dataset: str) -> DataFrame:
                contract = get_contract(tenant, dataset)
                set_column_keys(
                    spark, contract.column_keys_property(key_ids_by_sensitivity(tenant))
                )
                return spark.read.format("delta").load(str(silver_path(tenant, dataset)))

            dim_date = star.build_dim_date(spark)
            result.tables_built["dim_date"] = _write(dim_date, tenant, "dim_date", spark=spark)

            if tenant == "healthcare":
                encounters = load("encounter")
                claims = load("claim_line")

                dim_patient = spark.read.format("delta").load(
                    str(silver_path(tenant, "dim_patient"))
                )
                result.tables_built["dim_patient"] = _write(
                    star.build_dim_patient(dim_patient), tenant, "dim_patient", spark=spark
                )

                dim_facility = star.build_dim_from_reference(
                    load_reference(spark, tenant, "facility"),
                    key="facility_id",
                    sk_name="facility_sk",
                    columns=[
                        "facility_name",
                        "facility_type",
                        "city",
                        "state",
                        "postal_code",
                        "bed_count",
                        "is_active",
                    ],
                )
                result.tables_built["dim_facility"] = _write(
                    dim_facility, tenant, "dim_facility", spark=spark
                )

                dim_provider = star.build_dim_from_reference(
                    load_reference(spark, tenant, "provider"),
                    key="provider_id",
                    sk_name="provider_sk",
                    columns=["provider_name", "specialty", "facility_id", "is_accepting_patients"],
                )
                result.tables_built["dim_provider"] = _write(
                    dim_provider, tenant, "dim_provider", spark=spark
                )

                dim_diagnosis = star.build_dim_diagnosis(encounters)
                result.tables_built["dim_diagnosis"] = _write(
                    dim_diagnosis, tenant, "dim_diagnosis", spark=spark
                )

                fact_encounter = star.build_fact_encounter(encounters)
                result.tables_built["fact_encounter"] = _write(
                    fact_encounter, tenant, "fact_encounter", partition="event_date", spark=spark
                )

                fact_claim = star.build_fact_claim_line(claims)
                result.tables_built["fact_claim_line"] = _write(
                    fact_claim, tenant, "fact_claim_line", partition="event_date", spark=spark
                )

                sources = {
                    "mart_encounters_daily": (fact_encounter, dim_facility),
                    "mart_diagnosis_prevalence": (fact_encounter, dim_diagnosis),
                    "mart_claims_monthly": (fact_claim, dim_date),
                }
            else:
                transactions = load("transaction")
                chargebacks = load("chargeback")
                accounts = load("account")

                dim_merchant = star.build_dim_from_reference(
                    load_reference(spark, tenant, "merchant"),
                    key="merchant_id",
                    sk_name="merchant_sk",
                    columns=[
                        "merchant_name",
                        "mcc",
                        "mcc_category",
                        "country",
                        "city",
                        "state",
                        "risk_tier",
                    ],
                )
                result.tables_built["dim_merchant"] = _write(
                    dim_merchant, tenant, "dim_merchant", spark=spark
                )

                dim_account = star.build_dim_from_reference(
                    accounts,
                    key="account_id",
                    sk_name="account_sk",
                    columns=["account_type", "status", "home_state", "home_country"],
                )
                result.tables_built["dim_account"] = _write(
                    dim_account, tenant, "dim_account", spark=spark
                )

                fact_transaction = star.attach_chargebacks(
                    star.build_fact_transaction(transactions), chargebacks
                )
                result.tables_built["fact_transaction"] = _write(
                    fact_transaction,
                    tenant,
                    "fact_transaction",
                    partition="event_date",
                    spark=spark,
                )

                # Late disputes invalidate closed aggregates. Partitions are marked
                # dirty now, but the restatement is only *recorded* once the marts
                # have actually published -- see after the publish loop. Recording
                # it here marks partitions clean that a failed publish never
                # recomputed, which is the same ack-before-durable-write mistake
                # the CDC collector deliberately avoids (ADR 0009).
                affected = detect_late_arrival_restatements(fact_transaction, tenant)
                mark_dirty_partitions(
                    tenant, "mart_transactions_daily", affected, "late_chargeback"
                )
                pending_restatement = ("mart_transactions_daily", affected)
                result.partitions_restated = len(affected)

                sources = {
                    "mart_transactions_daily": (fact_transaction, dim_merchant),
                    "mart_merchant_risk": (fact_transaction, dim_merchant),
                    "mart_fraud_by_hour": (fact_transaction,),
                }

            for spec in marts_for(tenant):
                inputs = sources.get(spec.name)
                if inputs is None:
                    continue
                aggregate = spec.build(*inputs)
                protected, report = enforce(
                    aggregate,
                    spec.policy,
                    dataset=spec.name,
                    count_columns=spec.count_columns,
                )
                # Final gate immediately before anything leaves the boundary.
                assert_no_small_cells(protected, spec.policy, dataset=spec.name)
                published = publish_mart(protected, spec, run_id=run.run_id, report=report)

                result.marts_published[spec.name] = published
                result.suppression[spec.name] = report.summary()
                logger.info("mart_published", mart=spec.name, rows=published)

            # Now that the affected marts are durably published, the restatement
            # can be recorded and the partitions marked clean.
            if pending_restatement:
                mart_name, dates = pending_restatement
                if mart_name in result.marts_published:
                    record_restatement(
                        run_id=run.run_id,
                        tenant=tenant,
                        mart=mart_name,
                        dates=dates,
                        reason="late_chargeback",
                    )

        run.add(written=sum(result.tables_built.values()))
        run.note(
            tables=list(result.tables_built),
            marts=list(result.marts_published),
            partitions_restated=result.partitions_restated,
        )

    result.duration_seconds = time.perf_counter() - started
    return result
