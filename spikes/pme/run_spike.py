"""Stage 1 spike: can CONFIDENTIA encrypt individual Parquet columns and still work?

This decides ``CryptoSettings.column_encryption`` before any pipeline code is written.
Contribution C1 assumes per-column encryption keys, so if this does not hold the whole
storage design changes, and it is much cheaper to learn that now than in Stage 5.

Six arms, each answering one question:

A  plaintext baseline          -- what does an unencrypted write/read cost?
B  Parquet Modular Encryption  -- do per-column keys work at all through Spark?
C  PME with the wrong keys     -- does it actually FAIL, or silently return garbage?
D  PME under Delta Lake        -- does Delta preserve the encryption configuration?
E  delta-rs (Rust) reader      -- can the JVM-free API layer read encrypted tables?
F  envelope-encryption fallback-- if PME is unusable, what does the plan B cost?

Arm C is the one that matters most for the report. An encryption scheme that does not
fail closed is not an encryption scheme, and a viva panel will ask whether the data was
ever really unreadable without the key.

Timings here are indicative only: single machine, small data, cold JVM. They are a
feasibility signal, not experiment E4. E4 runs later under the ``bench`` profile.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
JAR_PATH = HERE / "confidentia-localkms.jar"
KMS_CLIENT = "org.confidentia.crypto.LocalKmsClient"
CRYPTO_FACTORY = "org.apache.parquet.crypto.keytools.PropertiesDrivenCryptoFactory"

ROWS = int(os.environ.get("SPIKE_ROWS", "50000"))

# Column sensitivity for the healthcare tenant, mirroring the classification the
# Silver layer will use. Two distinct keys so the spike proves key *separation*,
# not merely that encryption is on.
KEY_IDENTITY = "k_identity"  # direct identifiers
KEY_CLINICAL = "k_clinical"  # clinical payload
KEY_FOOTER = "k_footer"  # schema/metadata

IDENTITY_COLUMNS = ["mrn", "patient_name", "ssn"]
CLINICAL_COLUMNS = ["diagnosis_code", "note"]
PLAINTEXT_COLUMNS = ["encounter_id", "facility_id", "event_date", "amount"]


@dataclass
class ArmResult:
    arm: str
    question: str
    passed: bool
    detail: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


results: list[ArmResult] = []


def record(arm: str, question: str, passed: bool, detail: str = "", **metrics: Any) -> None:
    results.append(ArmResult(arm, question, passed, detail, metrics))
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {arm}: {detail}")


@contextmanager
def timed() -> Iterator[dict[str, float]]:
    holder: dict[str, float] = {}
    start = time.perf_counter()
    try:
        yield holder
    finally:
        holder["seconds"] = round(time.perf_counter() - start, 3)


def dir_size_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*.parquet") if f.is_file())


def new_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def resolve_java_home() -> str:
    sys.path.insert(0, str(HERE))
    from build_kms_jar import find_java_home

    return str(find_java_home())


def build_spark(key_list: str, work_dir: Path):
    """A memory-capped local SparkSession.

    ``local[2]`` with a 1 GB heap: this machine has ~7.4 GB total and the spike must
    not push it into swap, which would make every timing below meaningless.
    """
    from delta import configure_spark_with_delta_pip
    from pyspark.sql import SparkSession

    builder = (
        SparkSession.builder.appName("confidentia-pme-spike")
        .master("local[2]")
        .config("spark.driver.memory", "1g")
        .config("spark.sql.shuffle.partitions", "4")
        .config("spark.default.parallelism", "2")
        .config("spark.ui.enabled", "false")
        .config("spark.local.dir", str(work_dir / "spark-tmp"))
        .config("spark.sql.warehouse.dir", str(work_dir / "warehouse"))
        .config("spark.jars", JAR_PATH.as_uri())
        .config("spark.driver.extraClassPath", str(JAR_PATH))
        .config("spark.executor.extraClassPath", str(JAR_PATH))
        # Parquet Modular Encryption wiring.
        .config("spark.hadoop.parquet.crypto.factory.class", CRYPTO_FACTORY)
        .config("spark.hadoop.parquet.encryption.kms.client.class", KMS_CLIENT)
        .config("spark.hadoop.parquet.encryption.key.list", key_list)
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config(
            "spark.sql.catalog.spark_catalog",
            "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        )
    )
    return configure_spark_with_delta_pip(builder).getOrCreate()


def make_dataframe(spark, rows: int):
    """Synthetic encounter records shaped like the real Silver schema.

    Partition and filter columns (``event_date``, ``facility_id``) are kept separate
    from sensitive columns on purpose: arm B tests whether those can stay plaintext so
    that predicate pushdown survives encryption.
    """
    from pyspark.sql import functions as F

    return (
        spark.range(0, rows)
        .withColumn("encounter_id", F.concat(F.lit("ENC-"), F.col("id").cast("string")))
        .withColumn("facility_id", F.concat(F.lit("FAC-"), (F.col("id") % 20).cast("string")))
        .withColumn(
            "event_date",
            F.date_add(F.lit("2026-01-01").cast("date"), (F.col("id") % 180).cast("int")),
        )
        .withColumn("amount", (F.rand(seed=42) * 5000).cast("decimal(10,2)"))
        .withColumn("mrn", F.concat(F.lit("MRN"), F.lpad(F.col("id").cast("string"), 9, "0")))
        .withColumn("patient_name", F.concat(F.lit("Patient_"), F.col("id").cast("string")))
        .withColumn(
            "ssn",
            F.concat_ws(
                "-",
                F.lpad(((F.col("id") * 7) % 900 + 100).cast("string"), 3, "0"),
                F.lpad(((F.col("id") * 13) % 90 + 10).cast("string"), 2, "0"),
                F.lpad(((F.col("id") * 17) % 9000 + 1000).cast("string"), 4, "0"),
            ),
        )
        .withColumn(
            "diagnosis_code",
            F.element_at(
                F.array(*[F.lit(c) for c in ("E11.9", "I10", "J45.909", "F32.9", "M54.5")]),
                (F.col("id") % 5 + 1).cast("int"),
            ),
        )
        .withColumn("note", F.concat(F.lit("clinical note body for encounter "), F.col("id")))
        .drop("id")
    )


def column_keys_property() -> str:
    return (
        f"{KEY_IDENTITY}:{','.join(IDENTITY_COLUMNS)};{KEY_CLINICAL}:{','.join(CLINICAL_COLUMNS)}"
    )


# --------------------------------------------------------------------------
# Arms
# --------------------------------------------------------------------------


def arm_a_plaintext(spark, df, work_dir: Path) -> None:
    path = work_dir / "a_plaintext"
    with timed() as t:
        df.write.mode("overwrite").parquet(str(path))
    write_s = t["seconds"]

    with timed() as t:
        count = spark.read.parquet(str(path)).count()
    read_s = t["seconds"]

    record(
        "A",
        "Baseline: unencrypted Parquet write/read",
        count == ROWS,
        f"{count:,} rows, write {write_s}s, read {read_s}s, {dir_size_bytes(path):,} bytes",
        write_seconds=write_s,
        read_seconds=read_s,
        bytes=dir_size_bytes(path),
    )


def arm_b_pme(spark, df, work_dir: Path) -> bool:
    path = work_dir / "b_pme"
    try:
        with timed() as t:
            (
                df.write.mode("overwrite")
                .option("parquet.encryption.footer.key", KEY_FOOTER)
                .option("parquet.encryption.column.keys", column_keys_property())
                .parquet(str(path))
            )
        write_s = t["seconds"]
    except Exception as exc:  # noqa: BLE001 - the spike must report, not crash
        record("B", "Per-column Parquet encryption through Spark", False, f"write failed: {exc}")
        return False

    try:
        with timed() as t:
            read_df = spark.read.parquet(str(path))
            count = read_df.count()
            sample = read_df.select("ssn", "diagnosis_code", "facility_id").first()
        read_s = t["seconds"]
    except Exception as exc:  # noqa: BLE001
        record("B", "Per-column Parquet encryption through Spark", False, f"read failed: {exc}")
        return False

    plausible = count == ROWS and sample is not None and sample["ssn"].count("-") == 2
    record(
        "B",
        "Per-column Parquet encryption through Spark",
        plausible,
        f"{count:,} rows round-tripped, write {write_s}s, read {read_s}s, "
        f"{dir_size_bytes(path):,} bytes",
        write_seconds=write_s,
        read_seconds=read_s,
        bytes=dir_size_bytes(path),
    )
    return plausible


def arm_c_wrong_key(spark_factory, work_dir: Path, wrong_key_list: str) -> None:
    """Restart Spark holding only the footer key, then try to read.

    Requires a fresh JVM because Parquet's KeyToolkit caches unwrapped keys per
    process; reusing the session would read from cache and produce a false pass.
    """
    path = work_dir / "b_pme"
    spark = spark_factory(wrong_key_list)
    denied = False
    detail = ""
    try:
        spark.read.parquet(str(path)).select("ssn").limit(5).collect()
        detail = "read SUCCEEDED without the column key - encryption is NOT enforcing access"
    except Exception as exc:  # noqa: BLE001
        denied = True
        message = str(exc).splitlines()[0][:180]
        detail = f"correctly refused: {message}"
    finally:
        spark.stop()

    record("C", "Reading encrypted columns WITHOUT the key must fail", denied, detail)


def arm_d_delta_pme(spark, df, work_dir: Path) -> bool:
    path = work_dir / "d_delta_pme"
    try:
        with timed() as t:
            (
                df.write.format("delta")
                .mode("overwrite")
                .option("parquet.encryption.footer.key", KEY_FOOTER)
                .option("parquet.encryption.column.keys", column_keys_property())
                .save(str(path))
            )
        write_s = t["seconds"]

        with timed() as t:
            count = spark.read.format("delta").load(str(path)).count()
        read_s = t["seconds"]
    except Exception as exc:  # noqa: BLE001
        record("D", "Delta Lake table with encrypted columns", False, f"failed: {exc}")
        return False

    record(
        "D",
        "Delta Lake table with encrypted columns",
        count == ROWS,
        f"{count:,} rows, write {write_s}s, read {read_s}s",
        write_seconds=write_s,
        read_seconds=read_s,
    )
    return count == ROWS


def arm_e_delta_rs(work_dir: Path) -> None:
    """Can the JVM-free API layer read an encrypted Delta table?

    A negative result is not a failure of the spike -- it is a design constraint that
    decides whether the FastAPI serving layer can read Silver directly or must be
    restricted to the Gold layer.
    """
    path = work_dir / "d_delta_pme"
    if not path.exists():
        record(
            "E",
            "delta-rs (no JVM) reading encrypted Delta",
            False,
            "skipped: arm D produced no table",
        )
        return
    try:
        from deltalake import DeltaTable
    except ImportError:
        record(
            "E",
            "delta-rs (no JVM) reading encrypted Delta",
            False,
            "deltalake not installed; install the 'lake' extra to evaluate",
        )
        return

    try:
        table = DeltaTable(str(path))
        arrow = table.to_pyarrow_table()
        record(
            "E",
            "delta-rs (no JVM) reading encrypted Delta",
            True,
            f"delta-rs READ the encrypted table ({arrow.num_rows:,} rows) - API layer may read Silver",
        )
    except Exception as exc:  # noqa: BLE001
        record(
            "E",
            "delta-rs (no JVM) reading encrypted Delta",
            False,
            f"delta-rs cannot read encrypted Parquet: {str(exc).splitlines()[0][:150]}",
        )


def arm_f_envelope(spark, df, work_dir: Path) -> None:
    """Fallback: encrypt sensitive columns in the DataFrame with AES-256-GCM UDFs.

    Portable across every reader (the ciphertext is just a binary column) at the cost
    of losing column statistics and any pushdown on those columns, plus per-row Python
    UDF overhead. Measured here so the fallback decision is made on numbers.
    """
    from pyspark.sql import functions as F
    from pyspark.sql.types import BinaryType

    dek = secrets.token_bytes(32)

    def encrypt(value: str | None) -> bytes | None:
        if value is None:
            return None
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = secrets.token_bytes(12)
        return nonce + AESGCM(dek).encrypt(nonce, value.encode("utf-8"), None)

    encrypt_udf = F.udf(encrypt, BinaryType())

    path = work_dir / "f_envelope"
    encrypted = df
    for column in IDENTITY_COLUMNS + CLINICAL_COLUMNS:
        encrypted = encrypted.withColumn(column, encrypt_udf(F.col(column)))

    try:
        with timed() as t:
            encrypted.write.mode("overwrite").parquet(str(path))
        write_s = t["seconds"]

        with timed() as t:
            count = spark.read.parquet(str(path)).count()
        read_s = t["seconds"]
    except Exception as exc:  # noqa: BLE001
        record("F", "Envelope-encrypted columns fallback", False, f"failed: {exc}")
        return

    record(
        "F",
        "Envelope-encrypted columns fallback",
        count == ROWS,
        f"{count:,} rows, write {write_s}s, read {read_s}s, {dir_size_bytes(path):,} bytes",
        write_seconds=write_s,
        read_seconds=read_s,
        bytes=dir_size_bytes(path),
    )


def main() -> int:
    if not JAR_PATH.exists():
        print(f"KMS jar missing at {JAR_PATH}. Run build_kms_jar.py first.", file=sys.stderr)
        return 2

    java_home = resolve_java_home()
    os.environ["JAVA_HOME"] = java_home
    os.environ["PATH"] = str(Path(java_home) / "bin") + os.pathsep + os.environ["PATH"]
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)

    work_dir = Path(os.environ.get("SPIKE_WORK_DIR", "C:/confidentia-data/spike"))
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    keys = {KEY_FOOTER: new_key(), KEY_IDENTITY: new_key(), KEY_CLINICAL: new_key()}
    full_key_list = ",".join(f"{k}:{v}" for k, v in keys.items())
    # Arm C's principal holds the footer key only -- it can see the schema but must
    # not be able to read identity or clinical columns.
    footer_only_key_list = f"{KEY_FOOTER}:{keys[KEY_FOOTER]}"

    print(f"\nJAVA_HOME  : {java_home}")
    print(f"Work dir   : {work_dir}")
    print(f"Rows       : {ROWS:,}\n")

    spark = build_spark(full_key_list, work_dir)
    print(f"Spark      : {spark.version}\n")
    try:
        df = make_dataframe(spark, ROWS).cache()
        df.count()

        arm_a_plaintext(spark, df, work_dir)
        pme_ok = arm_b_pme(spark, df, work_dir)
        delta_ok = arm_d_delta_pme(spark, df, work_dir) if pme_ok else False
        arm_f_envelope(spark, df, work_dir)
    finally:
        spark.stop()

    if pme_ok:
        arm_c_wrong_key(lambda kl: build_spark(kl, work_dir), work_dir, footer_only_key_list)
    if delta_ok:
        arm_e_delta_rs(work_dir)

    report = {
        "rows": ROWS,
        "java_home": java_home,
        "arms": [asdict(r) for r in results],
    }
    out = HERE / "spike_results.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    passed = sum(1 for r in results if r.passed)
    print(f"\n{'=' * 70}")
    print(f"  {passed}/{len(results)} arms passed. Full results: {out}")
    print(f"{'=' * 70}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
