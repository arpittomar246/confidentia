"""E2 — partition sizing, and what it costs to get wrong.

Stage 5 hit this the hard way: Silver wrote 1,980 files totalling 73 MB — an
average of 37 KB per file — until a `repartition("event_date")` before the write
brought it to 180 files and 13 MB. That was a bug fix, not a measurement. This is
the measurement.

The sweep matters beyond file counts because of C5. The placement planner divides
the EPC budget by the measured row width to get a maximum partition size (867,787
rows at 116 B/row for a 128 MB EPC at 75% utilisation), and that number is only
meaningful if partition size is something the pipeline actually controls. This
measures the control.

**The enclave column is MODELLED and the rest is MEASURED**, and the two are kept
in separate columns rather than combined into one number, because there is no
enclave on this machine.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from confidentia.benchmarks.harness import ArmResult, record, run_arms
from confidentia.common.logging import get_logger
from confidentia.config.settings import Settings, get_settings

logger = get_logger(__name__)

EXPERIMENT = "E2"

# Output partition counts to sweep.
#
# The first version of this experiment swept
# `spark.databricks.delta.optimize.maxFileSize` instead, because `spark/session.py`
# sets it and its comment says "Experiment E2 sweeps this against the enclave
# memory budget". The sweep came back **completely flat** -- 90 files of 384 KB at
# every value from 8 MB to 512 MB.
#
# That setting governs OPTIMIZE, the compaction command, and this pipeline never
# runs it. Write-time file count is decided by the number of Spark output
# partitions and nothing else. So the setting the codebase claims E2 sweeps has no
# effect on the path it is applied to, and the honest experiment sweeps the thing
# that does.
#
# 4 reproduces the coarse regime; 720 the small-file regime Stage 5 fell into
# (1,980 files averaging 37 KB).
PARTITION_COUNTS = (4, 16, 90, 360, 720)


def _files(path: Path) -> list[int]:
    return [f.stat().st_size for f in path.rglob("*.parquet")]


def run(
    rows: int = 3_000_000,
    repeats: int = 3,
    warmup: int = 1,
    settings: Settings | None = None,
) -> tuple[list[ArmResult], dict[str, dict[str, Any]]]:
    settings = settings or get_settings()
    work = settings.data_root / "bench" / "e2"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    from pyspark.sql import functions as F

    from confidentia.spark.session import build_session

    spark = build_session("bench-e2", settings=settings)
    layout: dict[str, dict[str, Any]] = {}

    try:
        # Zipf-shaped key, because a uniform one would make every partition the
        # same size and the experiment would measure nothing real. ADR 0008 gives
        # encounters-per-patient a Zipf distribution for exactly this reason.
        frame = (
            spark.range(rows)
            .withColumn("patient_id", (F.pow(F.rand(seed=42), F.lit(3.0)) * 50_000).cast("int"))
            .withColumn("event_date", F.date_add(F.lit("2026-01-01").cast("date"), (F.col("id") % 90).cast("int")))
            .withColumn("diagnosis_code", F.concat(F.lit("D"), (F.col("id") % 500).cast("string")))
            .withColumn("billed_amount", (F.col("id") % 9973) * 1.37)
        ).cache()
        frame.count()

        def write(partitions: int) -> dict[str, Any]:
            path = work / f"p{partitions}"
            shutil.rmtree(path, ignore_errors=True)
            # Unpartitioned on disk: partitioning by event_date would pin the file
            # count to the 90 distinct dates and hide the effect being measured.
            (
                frame.repartition(partitions)
                .write.format("delta")
                .mode("overwrite")
                .save(str(path))
            )
            sizes = _files(path)
            layout[f"p{partitions}"] = {
                "files": len(sizes),
                "total_mb": sum(sizes) / 1e6,
                "mean_file_kb": (sum(sizes) / len(sizes) / 1e3) if sizes else 0,
                "largest_file_mb": (max(sizes) / 1e6) if sizes else 0,
                "rows_per_partition": rows // partitions,
            }
            return {"rows": rows, "bytes": sum(sizes)}

        arms = {f"p{n}": (lambda n=n: write(n)) for n in PARTITION_COUNTS}
        results = run_arms(EXPERIMENT, arms, warmup=warmup, repeats=repeats)
        return results, layout
    finally:
        spark.stop()


def main(rows: int = 3_000_000, repeats: int = 3, persist: bool = True) -> None:
    from confidentia.tee.model import EnclaveCostModel

    results, layout = run(rows=rows, repeats=repeats)
    model = EnclaveCostModel.from_settings()

    epc_rows = model.epc_budget_bytes // 116  # 116 B/row, measured by C5
    print(
        f"\n{'parts':>7} {'p50':>8} {'files':>7} {'mean file':>11} {'rows/part':>11} "
        f"{'total':>9}  {'fits EPC?':>10}"
    )
    print("-" * 78)
    for result in results:
        info = layout.get(result.arm, {})
        per_partition = info.get("rows_per_partition", 0)
        # MODELLED: C5's threshold is 867,787 rows at the measured 116 B/row for a
        # 128 MB EPC at 75% utilisation. Whether a partition of this size fits is
        # arithmetic on a measured width and a configured budget -- there is no
        # enclave here to check the consequence against.
        fits = "yes" if per_partition and per_partition <= epc_rows else "NO"
        print(
            f"{result.arm:>7} {result.p50:>7.2f}s {info.get('files', 0):>7} "
            f"{info.get('mean_file_kb', 0):>9.0f}KB {per_partition:>11,} "
            f"{info.get('total_mb', 0):>8.1f}MB  {fits:>10}"
        )

    print(
        f"\nMEASURED: write time, file count and file size on this machine.\n"
        f"MODELLED: the EPC column -- {epc_rows:,} rows per partition at C5's measured\n"
        "116 B/row and a 101 MB budget. There is no enclave here to check it against."
    )

    if persist:
        record(
            results,
            metric="write_seconds",
            unit="s",
            label="MEASURED",
            notes=f"{rows} rows, zipf-skewed key, partitioned by event_date",
        )
