"""E8 — what the pipeline does when a source schema drifts.

ADR 0008 gives the generators per-chunk deterministic seeding specifically so that
"E8 replays a dataset under a drifted schema", and the `generator-determinism` CI
job protects that property on every push. This is the replay.

**E8 measures behaviour, not time.** The question is not how fast a drifted batch
ingests — it is whether the platform notices. So the metric is what happens to the
rows: accepted intact, silently nulled, or refused. A drift that ingests quickly and
destroys data is the worst outcome, not the best.

Four drifts, each a real thing a source system does:

| Drift | What a source did |
|---|---|
| `column_added` | Added a field the contract does not know about |
| `column_dropped` | Stopped sending a field the contract requires |
| `type_violation` | Started sending an unparseable value where a timestamp was |
| `column_renamed` | Renamed a field, so the old name vanishes and a new one appears |

The baseline arm is the same data with no drift, so every number is a difference
from a known-good replay of identical rows.
"""

from __future__ import annotations

import shutil
from typing import Any

from confidentia.catalog.contracts import get_contract
from confidentia.common.logging import get_logger
from confidentia.config.settings import Settings, get_settings

logger = get_logger(__name__)

EXPERIMENT = "E8"
TENANT = "financial"
DATASET = "transaction"

# The column the structural drifts target. `amount` because it is a measure the
# Gold marts aggregate, so losing it is consequential rather than cosmetic.
TARGET = "amount"

# The column the *type* drift targets, and it has to be a different one.
#
# Every column in the Bronze contract for this dataset is declared `string` except
# two timestamps, a boolean and the partition date -- Bronze keeps raw values as
# text and Silver does the typing. So a "source started sending a string where a
# number was" drift on `amount` casts cleanly and produces zero nulls: the first
# version of this experiment ran exactly that and reported 0.0%, which was true and
# measured nothing.
#
# `event_ts` is a real timestamp in the contract, so an unparseable value there is
# the drift the arm was meant to exercise.
TYPE_TARGET = "event_ts"


def run(rows: int = 60_000, settings: Settings | None = None) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    from pyspark.sql import functions as F

    from confidentia.crypto.broker import request_keys
    from confidentia.pipelines.bronze.writer import _apply_contract
    from confidentia.spark.session import apply_encryption, build_session

    contract = get_contract(TENANT, DATASET)
    landing = settings.lake_root / "landing" / TENANT / DATASET
    work = settings.data_root / "bench" / "e8"
    shutil.rmtree(work, ignore_errors=True)

    spark = build_session("bench-e8", settings=settings)
    # The landing zone is encrypted since ADR 0022, so even a benchmark that only
    # reads it needs key material. This is the cost the fix imposes on everything
    # downstream, and it is the intended one: there is no longer a way to read raw
    # source records without presenting an attestation.
    apply_encryption(spark, request_keys(TENANT))
    findings: list[dict[str, Any]] = []

    try:
        base = (
            spark.read.parquet(str(landing))
            .limit(rows)
            .withColumn("_ingested_at", F.current_timestamp())
        ).cache()
        base.count()

        def drift(kind: str) -> Any:
            if kind == "baseline":
                return base
            if kind == "column_added":
                return base.withColumn("loyalty_tier", F.lit("gold"))
            if kind == "column_dropped":
                return base.drop(TARGET)
            if kind == "type_violation":
                # A source that started sending "13/08/2026 14:22" where an ISO
                # timestamp was. Targets event_ts, which the contract genuinely
                # types -- see the note on TYPE_TARGET.
                return base.withColumn(
                    TYPE_TARGET,
                    F.concat(F.lit("13/08/2026 "), F.col(TYPE_TARGET).cast("string")),
                )
            if kind == "column_renamed":
                return base.withColumnRenamed(TARGET, "txn_amount")
            raise ValueError(kind)

        for kind in ("baseline", "column_added", "column_dropped", "type_violation", "column_renamed"):
            frame = drift(kind)
            source_columns = set(frame.columns)
            # Each drift is judged on the column it actually damages.
            watched = TYPE_TARGET if kind == "type_violation" else TARGET

            try:
                applied = _apply_contract(frame, contract)
                total = applied.count()
                # The measure that matters: how many rows lost the watched column's
                # value. Bronze casts permissively, so a drift shows up as NULL
                # rather than as an exception -- which is the design, and is exactly
                # why this has to be counted rather than assumed.
                nulls = applied.filter(F.col(watched).isNull()).count()
                accepted = True
                error = ""
            except Exception as exc:  # noqa: BLE001 - the outcome under test
                total = nulls = 0
                accepted = False
                error = f"{type(exc).__name__}: {str(exc)[:90]}"

            declared = {c.name for c in contract.columns} - {"ingest_date"}
            findings.append(
                {
                    "drift": kind,
                    "accepted": accepted,
                    "rows": total,
                    "watched_column": watched,
                    "nulls": nulls,
                    "null_rate": (nulls / total) if total else 0.0,
                    # A column the source sent that the contract does not declare is
                    # silently dropped by the projection in `_apply_contract`.
                    "unknown_columns_dropped": sorted(
                        source_columns - declared - {"_ingested_at"}
                    ),
                    "error": error,
                }
            )
        return findings
    finally:
        spark.stop()


def main(rows: int = 60_000, persist: bool = True) -> list[dict[str, Any]]:
    findings = run(rows=rows)

    print(f"\n{'drift':18s} {'accepted':>9s} {'rows':>9s} {'NULL':>12s} "
          f"{'null rate':>10s}  watched / dropped")
    print("-" * 82)
    for f in findings:
        print(
            f"{f['drift']:18s} {f['accepted']!s:>9s} {f['rows']:>9,} "
            f"{f['nulls']:>12,} {f['null_rate']:>9.1%}  "
            f"{f['watched_column']}"
            + (f" / dropped {', '.join(f['unknown_columns_dropped'])}" if f['unknown_columns_dropped'] else "")
            + (f"  {f['error']}" if f["error"] else "")
        )

    baseline = findings[0]
    print(
        "\nMEASURED. Bronze casts permissively by design (ADR: 'Bronze must accept "
        "what arrived'),\nso drift surfaces as NULL rather than as a refused batch. "
        "The question E8 answers is\nwhether anything downstream would notice."
    )
    print(
        f"\nBaseline null rate: {baseline['null_rate']:.1%}. "
        "Any arm above that lost data silently at Bronze."
    )

    if persist:
        from confidentia.benchmarks.harness import ArmResult, Sample, record

        results = [
            ArmResult(
                experiment=EXPERIMENT,
                arm=f["drift"],
                samples=[Sample(seconds=0.0, rows=f["rows"])],
                notes=(f"null_rate={f['null_rate']:.4f} accepted={f['accepted']} "
                       f"dropped={','.join(f['unknown_columns_dropped']) or 'none'}"),
            )
            for f in findings
        ]
        # The metric is the null rate, not a duration -- E8 is about behaviour.
        for result, finding in zip(results, findings, strict=True):
            result.samples = [Sample(seconds=finding["null_rate"], rows=finding["rows"])]
        record(results, metric="null_rate", unit="ratio", label="MEASURED",
               notes=f"{rows} rows replayed under drift")
    return findings
