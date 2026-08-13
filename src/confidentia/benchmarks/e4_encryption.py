"""E4 — what Parquet Modular Encryption costs.

The project's central cost question, and the one the Stage 1 spike got wrong. That
spike reported PME writing *faster* than plaintext, which is impossible; it was JVM
warm-up leaking across arms on a single cold run. ADR 0007 says as much and defers
the real measurement to here.

So this warms up, repeats, interleaves the arms, and — most importantly —
**verifies each arm actually did what it claims** by reading the Parquet footer
magic. An encrypted arm whose files say `PAR1` is measuring plaintext twice, which
is precisely the false pass this project has already had once.

## Arms

`plaintext` and `pme` only. ADR 0007 names `envelope` as the baseline comparison
arm, and **that mode does not exist**: `crypto.column_encryption` is a declared
setting that nothing in `pipelines/` reads. Reporting a third arm would mean
implementing envelope encryption first; reporting two and saying why is the honest
alternative.

Both arms run in one SparkSession, toggling the crypto factory on the Hadoop
Configuration between writes. A second session is not an option — Spark permits one
active SparkContext per process — and separate processes would reintroduce exactly
the cold-JVM asymmetry this experiment exists to eliminate.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from confidentia.benchmarks.harness import ArmResult, record, run_arms, table
from confidentia.common.logging import get_logger
from confidentia.config.settings import Settings, get_settings
from confidentia.spark.session import (
    CRYPTO_FACTORY,
    PROP_COLUMN_KEYS,
    PROP_FACTORY,
)

logger = get_logger(__name__)

EXPERIMENT = "E4"


def _directory_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _footer_magic(path: Path) -> set[str]:
    """`PAR1` is plaintext, `PARE` is an encrypted footer."""
    magics: set[str] = set()
    for parquet in path.rglob("*.parquet"):
        with parquet.open("rb") as handle:
            handle.seek(-4, 2)
            magics.add(handle.read(4).decode("ascii", errors="replace"))
    return magics


def run(
    rows: int = 200_000,
    repeats: int = 3,
    warmup: int = 1,
    settings: Settings | None = None,
) -> list[ArmResult]:
    settings = settings or get_settings()
    work = settings.data_root / "bench" / "e4"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    from confidentia.crypto.broker import request_keys
    from confidentia.spark.session import apply_encryption, build_session

    keys = request_keys("healthcare")
    spark = build_session("bench-e4", settings=settings)

    try:
        apply_encryption(spark, keys)
        hadoop = spark.sparkContext._jsc.hadoopConfiguration()

        # A frame wide enough that per-column encryption has something to do, and
        # shaped like a real Silver table rather than one column of integers.
        from pyspark.sql import functions as F

        frame = (
            spark.range(rows)
            .withColumn("patient_token", F.sha2(F.col("id").cast("string"), 256))
            .withColumn("mrn", F.concat(F.lit("MRN-"), F.col("id").cast("string")))
            .withColumn("diagnosis_code", F.concat(F.lit("D"), (F.col("id") % 500).cast("string")))
            .withColumn("billed_amount", (F.col("id") % 9973) * 1.37)
            .withColumn("event_date", F.date_add(F.lit("2026-01-01").cast("date"), (F.col("id") % 365).cast("int")))
            .withColumn("notes", F.concat(F.lit("visit "), F.col("id").cast("string")))
        ).cache()
        frame.count()  # materialise before any arm is timed

        from confidentia.config.settings import Sensitivity
        from confidentia.spark.session import key_ids_by_sensitivity

        # PHI explicitly. Taking "the first key" out of the mapping worked and said
        # nothing about which sensitivity class was being exercised, which matters
        # when the whole point is the cost of encrypting sensitive columns.
        key_id = key_ids_by_sensitivity("healthcare")[Sensitivity.PHI]
        column_keys = f"{key_id}:patient_token,mrn,diagnosis_code,billed_amount,notes"

        def write(arm: str) -> dict[str, Any]:
            target = work / arm
            shutil.rmtree(target, ignore_errors=True)
            if arm == "pme":
                hadoop.set(PROP_FACTORY, CRYPTO_FACTORY)
                hadoop.set(PROP_COLUMN_KEYS, column_keys)
            else:
                # Unsetting the factory is what makes the write plaintext. Delta
                # silently ignores encryption passed as writer options (ADR 0007),
                # so this has to happen on the Hadoop Configuration.
                hadoop.unset(PROP_FACTORY)
                hadoop.unset(PROP_COLUMN_KEYS)
            frame.write.format("delta").mode("overwrite").save(str(target))
            return {"rows": rows, "bytes": _directory_bytes(target)}

        arms = {"plaintext": lambda: write("plaintext"), "pme": lambda: write("pme")}
        results = run_arms(EXPERIMENT, arms, warmup=warmup, repeats=repeats)

        # The check that makes the numbers mean anything. Written after the runs so
        # it inspects what the final iteration actually produced.
        for result in results:
            magic = _footer_magic(work / result.arm)
            expected = "PARE" if result.arm == "pme" else "PAR1"
            if magic != {expected}:
                raise RuntimeError(
                    f"E4 arm {result.arm!r} produced footer magic {sorted(magic)}, "
                    f"expected {{{expected!r}}}. The arms are not measuring what "
                    "they claim, which is the Stage 1 false pass repeating."
                )
            result.notes = (f"{result.notes} " if result.notes else "") + f"footer {expected}"

        return results
    finally:
        spark.stop()


def main(rows: int = 200_000, repeats: int = 3, persist: bool = True) -> list[ArmResult]:
    results = run(rows=rows, repeats=repeats)
    print(table(results, baseline="plaintext"))

    plaintext = next(r for r in results if r.arm == "plaintext")
    encrypted = next(r for r in results if r.arm == "pme")
    print(
        f"\nwrite overhead : x{encrypted.p50 / plaintext.p50:.3f}"
        f"   storage overhead: x{encrypted.bytes_ / plaintext.bytes_:.3f}"
        f"  ({plaintext.bytes_ / 1e6:.1f} MB -> {encrypted.bytes_ / 1e6:.1f} MB)"
    )
    print(
        "\nMEASURED on this machine under TEE_BACKEND=sim. This is the cost of "
        "column encryption, not of an enclave -- there is no enclave here."
    )

    if persist:
        record(results, metric="write_seconds", unit="s", label="MEASURED",
               notes=f"{rows} rows, delta, interleaved arms")
    return results
