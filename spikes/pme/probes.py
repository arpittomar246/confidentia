"""Follow-up probes for the Stage 1 spike.

The first run established that Parquet Modular Encryption works through Spark
(``PARE`` magic, sensitive values absent from the bytes) but that Delta Lake wrote
plaintext. It also produced two unsound results that these probes redo properly:

``keyless-read``
    Must run in its own OS process. ``SparkSession.stop()`` does not tear down the
    JVM under PySpark, and Parquet's ``KeyToolkit`` caches unwrapped keys statically,
    so a second session in the same process reads from cache and appears to succeed
    without holding the key. A fresh process is the only honest test.

``delta-hadoopconf``
    Delta routes writes through its own commit protocol and does not forward
    DataFrameWriter options into the Parquet crypto factory. This probe instead sets
    the properties on the Hadoop Configuration, which Delta's writer does inherit.

``delta-rs``
    Only meaningful against a genuinely encrypted table, which the first run did not
    have. Decides whether the JVM-free API layer can read Silver directly.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
from pathlib import Path

KMS_CLIENT = "org.confidentia.crypto.LocalKmsClient"
CRYPTO_FACTORY = "org.apache.parquet.crypto.keytools.PropertiesDrivenCryptoFactory"

KEY_FOOTER = "k_footer"
KEY_IDENTITY = "k_identity"
KEY_CLINICAL = "k_clinical"

IDENTITY_COLUMNS = ["mrn", "patient_name", "ssn"]
CLINICAL_COLUMNS = ["diagnosis_code", "note"]

HERE = Path(__file__).resolve().parent
JAR_PATH = HERE / "confidentia-localkms.jar"
KEYS_FILE = HERE / "spike_keys.json"


def load_or_create_keys() -> dict[str, str]:
    """Persist keys across processes so probes can share the same key material."""
    if KEYS_FILE.exists():
        return json.loads(KEYS_FILE.read_text())
    keys = {
        k: base64.b64encode(secrets.token_bytes(32)).decode()
        for k in (KEY_FOOTER, KEY_IDENTITY, KEY_CLINICAL)
    }
    KEYS_FILE.write_text(json.dumps(keys, indent=2))
    return keys


def build_spark(key_list: str, work_dir: Path, *, with_delta: bool = True):
    from pyspark.sql import SparkSession

    builder = (
        SparkSession.builder.appName("confidentia-pme-probe")
        .master("local[2]")
        .config("spark.driver.memory", "1g")
        .config("spark.sql.shuffle.partitions", "4")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.warehouse.dir", str(work_dir / "warehouse"))
        .config("spark.jars", JAR_PATH.as_uri())
        .config("spark.driver.extraClassPath", str(JAR_PATH))
        .config("spark.executor.extraClassPath", str(JAR_PATH))
        .config("spark.hadoop.parquet.crypto.factory.class", CRYPTO_FACTORY)
        .config("spark.hadoop.parquet.encryption.kms.client.class", KMS_CLIENT)
        .config("spark.hadoop.parquet.encryption.key.list", key_list)
    )
    if with_delta:
        from delta import configure_spark_with_delta_pip

        builder = builder.config(
            "spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension"
        ).config(
            "spark.sql.catalog.spark_catalog",
            "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        )
        return configure_spark_with_delta_pip(builder).getOrCreate()
    return builder.getOrCreate()


def make_dataframe(spark, rows: int):
    from pyspark.sql import functions as F

    return (
        spark.range(0, rows)
        .withColumn("encounter_id", F.concat(F.lit("ENC-"), F.col("id").cast("string")))
        .withColumn("facility_id", F.concat(F.lit("FAC-"), (F.col("id") % 20).cast("string")))
        .withColumn(
            "event_date",
            F.date_add(F.lit("2026-01-01").cast("date"), (F.col("id") % 180).cast("int")),
        )
        .withColumn("mrn", F.concat(F.lit("MRN"), F.lpad(F.col("id").cast("string"), 9, "0")))
        .withColumn("patient_name", F.concat(F.lit("Patient_"), F.col("id").cast("string")))
        .withColumn("ssn", F.concat(F.lit("SSN-"), F.col("id").cast("string")))
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


def magic_of(directory: Path) -> str:
    files = sorted(directory.rglob("*.parquet"))
    if not files:
        return "no parquet files"
    blob = max(files, key=lambda f: f.stat().st_size).read_bytes()
    return f"{blob[:4].decode(errors='replace')}/{blob[-4:].decode(errors='replace')}"


def has_plaintext(directory: Path) -> list[str]:
    """Which sensitive values survive verbatim into the bytes."""
    needles = {
        "patient_name": b"Patient_1234",
        "note": b"clinical note body",
        "diagnosis": b"J45.909",
    }
    files = sorted(directory.rglob("*.parquet"))
    if not files:
        return []
    blob = max(files, key=lambda f: f.stat().st_size).read_bytes()
    return [name for name, needle in needles.items() if needle in blob]


# ---------------------------------------------------------------- probes


def probe_write_parquet(work_dir: Path, keys: dict[str, str]) -> int:
    """Rewrite the encrypted Parquet table using the persisted key material.

    The first spike run generated keys in-process and discarded them, so nothing
    else can be tested against that table. Every later probe reads what this writes.
    """
    key_list = ",".join(f"{k}:{v}" for k, v in keys.items())
    spark = build_spark(key_list, work_dir, with_delta=False)
    target = work_dir / "h_parquet_pme"
    try:
        df = make_dataframe(spark, 50_000)
        (
            df.write.mode("overwrite")
            .option("parquet.encryption.footer.key", KEY_FOOTER)
            .option("parquet.encryption.column.keys", column_keys_property())
            .parquet(str(target))
        )
        count = spark.read.parquet(str(target)).count()
    finally:
        spark.stop()

    leaked = has_plaintext(target)
    magic = magic_of(target)
    encrypted = magic.startswith("PARE") and not leaked
    print(f"  rows written      : {count:,}")
    print(f"  parquet magic     : {magic}")
    print(f"  plaintext leaked  : {leaked or 'none'}")
    print(f"  VERDICT           : {'ENCRYPTED' if encrypted else 'NOT ENCRYPTED'}")
    return 0 if encrypted else 1


def probe_delta_hadoopconf(work_dir: Path, keys: dict[str, str]) -> int:
    """Write a Delta table with encryption set on the Hadoop Configuration."""
    key_list = ",".join(f"{k}:{v}" for k, v in keys.items())
    spark = build_spark(key_list, work_dir)
    target = work_dir / "g_delta_hadoopconf"

    hadoop_conf = spark.sparkContext._jsc.hadoopConfiguration()
    hadoop_conf.set("parquet.encryption.footer.key", KEY_FOOTER)
    hadoop_conf.set("parquet.encryption.column.keys", column_keys_property())

    try:
        df = make_dataframe(spark, 50_000)
        df.write.format("delta").mode("overwrite").save(str(target))
        count = spark.read.format("delta").load(str(target)).count()
    finally:
        spark.stop()

    leaked = has_plaintext(target)
    magic = magic_of(target)
    encrypted = magic.startswith("PARE") and not leaked

    print(f"  rows written      : {count:,}")
    print(f"  parquet magic     : {magic}")
    print(f"  plaintext leaked  : {leaked or 'none'}")
    print(f"  VERDICT           : {'ENCRYPTED' if encrypted else 'NOT ENCRYPTED'}")
    return 0 if encrypted else 1


def probe_keyless_read(work_dir: Path, keys: dict[str, str], table: str, fmt: str) -> int:
    """Fresh process, footer key only. Reading a sensitive column must fail."""
    key_list = f"{KEY_FOOTER}:{keys[KEY_FOOTER]}"
    spark = build_spark(key_list, work_dir)
    target = work_dir / table
    try:
        reader = spark.read.format(fmt) if fmt != "parquet" else spark.read
        rows = reader.load(str(target)) if fmt != "parquet" else reader.parquet(str(target))
        sample = rows.select("patient_name").limit(3).collect()
        print(f"  read SUCCEEDED without the column key: {[r[0] for r in sample]}")
        print("  VERDICT           : ACCESS NOT ENFORCED")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"  read refused: {str(exc).splitlines()[0][:200]}")
        print("  VERDICT           : ACCESS CORRECTLY DENIED")
        return 0
    finally:
        spark.stop()


def probe_delta_rs(work_dir: Path, table: str) -> int:
    """Can the JVM-free reader used by the API layer open an encrypted table?"""
    target = work_dir / table
    try:
        from deltalake import DeltaTable
    except ImportError:
        print("  deltalake not installed")
        return 1
    try:
        arrow = DeltaTable(str(target)).to_pyarrow_table()
        print(f"  delta-rs READ the table: {arrow.num_rows:,} rows")
        print("  VERDICT           : delta-rs CAN read it")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"  delta-rs failed: {str(exc).splitlines()[0][:200]}")
        print("  VERDICT           : delta-rs CANNOT read encrypted Parquet")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "probe",
        choices=["write-parquet", "delta-hadoopconf", "keyless-read", "delta-rs"],
    )
    parser.add_argument("--table", default="b_pme")
    parser.add_argument("--format", default="parquet", choices=["parquet", "delta"])
    args = parser.parse_args()

    java_home = os.environ.get("JAVA_HOME", "/usr/lib/jvm/java-17-openjdk-amd64")
    os.environ["JAVA_HOME"] = java_home
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)

    work_dir = Path(os.environ.get("SPIKE_WORK_DIR", str(Path.home() / "confidentia-data/spike")))
    work_dir.mkdir(parents=True, exist_ok=True)
    keys = load_or_create_keys()

    print(f"\n>>> probe: {args.probe}  (table={args.table}, format={args.format})")
    if args.probe == "write-parquet":
        return probe_write_parquet(work_dir, keys)
    if args.probe == "delta-hadoopconf":
        return probe_delta_hadoopconf(work_dir, keys)
    if args.probe == "keyless-read":
        return probe_keyless_read(work_dir, keys, args.table, args.format)
    return probe_delta_rs(work_dir, args.table)


if __name__ == "__main__":
    raise SystemExit(main())
