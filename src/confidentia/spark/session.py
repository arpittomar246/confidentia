"""SparkSession construction, with Parquet Modular Encryption wired correctly.

Two things here are easy to get wrong and both are security-relevant.

**Key material must not go into SparkConf.** The obvious way to configure PME is
``.config("spark.hadoop.parquet.encryption.key.list", ...)``, which is what the
Stage 1 spike did. Every ``spark.*`` property is displayed in the Spark UI's
Environment tab and returned by ``sc.getConf().toDebugString()``, and Spark's
default ``spark.redaction.regex`` matches only ``secret|password|token`` — so
``parquet.encryption.key.list`` would be rendered in full, in a browser, as
base64 master keys. The key list is therefore set on the **Hadoop Configuration
after** the session exists, which the UI does not display, and the redaction regex
is widened as defence in depth.

**Encryption properties must go on the Hadoop Configuration, not writer options.**
Delta silently ignores DataFrameWriter encryption options and writes plaintext
(ADR 0007). No error, no warning.

Because encryption configuration is therefore session-global, a session is scoped
to one tenant. Writing two tenants' data from one session would require mutating
the Hadoop Configuration between writes, and a mistake there means one tenant's
data written under another's key. ``session_for_tenant`` makes the scoping explicit.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from confidentia.common.logging import get_logger
from confidentia.config.settings import Sensitivity, Settings, get_settings
from confidentia.crypto.keys import KeyId

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import SparkSession

    from confidentia.crypto.broker import ReleasedKeys

logger = get_logger(__name__)

CRYPTO_FACTORY = "org.apache.parquet.crypto.keytools.PropertiesDrivenCryptoFactory"
KMS_CLIENT = "org.confidentia.crypto.LocalKmsClient"
KMS_JAR = Path(__file__).resolve().parents[1] / "crypto" / "java" / "confidentia-localkms.jar"

# Hadoop property names.
PROP_FACTORY = "parquet.crypto.factory.class"
PROP_KMS_CLIENT = "parquet.encryption.kms.client.class"
PROP_KEY_LIST = "parquet.encryption.key.list"
PROP_FOOTER_KEY = "parquet.encryption.footer.key"
PROP_COLUMN_KEYS = "parquet.encryption.column.keys"


def _resolve_java_home(settings: Settings) -> str | None:
    if settings.spark.java_home:
        return str(settings.spark.java_home)
    for candidate in (
        os.environ.get("CONFIDENTIA_JAVA_HOME"),
        os.environ.get("JAVA_HOME"),
        "/usr/lib/jvm/java-17-openjdk-amd64",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def build_session(
    app_name: str = "confidentia",
    *,
    settings: Settings | None = None,
    extra_conf: dict[str, str] | None = None,
    with_kafka: bool = False,
) -> SparkSession:
    """A memory-capped local session with Delta enabled.

    No encryption configuration is applied here; call ``apply_encryption`` (or use
    ``session_for_tenant``) once keys have been released.

    ``with_kafka`` pulls the Structured Streaming Kafka connector, which is a Maven
    package rather than a Python one — pyspark ships the streaming engine but not
    the connector. It is **opt-in** because resolving it costs an Ivy round trip on
    a cold cache, and every batch stage would pay that for a dependency it never
    uses.
    """
    from delta import configure_spark_with_delta_pip
    from pyspark.sql import SparkSession

    settings = settings or get_settings()
    java_home = _resolve_java_home(settings)
    if java_home:
        os.environ["JAVA_HOME"] = java_home
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)

    settings.ensure_directories()

    builder = (
        SparkSession.builder.appName(app_name)
        .master(settings.spark.master)
        .config("spark.driver.memory", settings.spark.driver_memory)
        .config("spark.executor.memory", settings.spark.executor_memory)
        .config("spark.driver.maxResultSize", settings.spark.driver_max_result_size)
        .config("spark.sql.shuffle.partitions", str(settings.spark.shuffle_partitions))
        .config("spark.default.parallelism", str(settings.spark.default_parallelism))
        .config("spark.sql.adaptive.enabled", str(settings.spark.adaptive_enabled).lower())
        .config("spark.ui.enabled", str(settings.spark.ui_enabled).lower())
        .config("spark.ui.port", str(settings.spark.ui_port))
        .config("spark.local.dir", str(settings.spark_local_dir))
        .config("spark.sql.warehouse.dir", str(settings.spark_warehouse))
        .config("spark.sql.session.timeZone", "UTC")
        # Widen redaction beyond Spark's default secret|password|token. Belt and
        # braces: key material is kept out of SparkConf entirely, but if a future
        # change puts it back, this stops it reaching the UI.
        .config(
            "spark.redaction.regex",
            "(?i)secret|password|token|key[._]list|passphrase|credential",
        )
        .config("spark.jars", KMS_JAR.as_uri())
        .config("spark.driver.extraClassPath", str(KMS_JAR))
        .config("spark.executor.extraClassPath", str(KMS_JAR))
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config(
            "spark.sql.catalog.spark_catalog",
            "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        )
        # Target file size for compaction. Experiment E2 sweeps this against the
        # enclave memory budget, so it must come from settings.
        .config(
            "spark.databricks.delta.optimize.maxFileSize",
            str(settings.spark.target_file_size_mb * 1024 * 1024),
        )
    )
    for key, value in (extra_conf or {}).items():
        builder = builder.config(key, value)

    if not KMS_JAR.exists():
        logger.warning(
            "kms_jar_missing",
            path=str(KMS_JAR),
            hint="run scripts/build_kms_jar.sh; encrypted writes will fail",
        )

    # The connector version must track pyspark's exactly. Hard-coding "3.5.3" here
    # would silently diverge the day pyspark is bumped, and the resulting failure
    # is a Maven resolution error a long way from the cause.
    extra_packages = (
        [f"org.apache.spark:spark-sql-kafka-0-10_2.12:{pyspark_version()}"]
        if with_kafka
        else []
    )

    session = configure_spark_with_delta_pip(
        builder, extra_packages=extra_packages
    ).getOrCreate()
    session.sparkContext.setLogLevel("WARN")
    logger.info(
        "spark_session_started",
        version=session.version,
        master=settings.spark.master,
        driver_memory=settings.spark.driver_memory,
        kafka=with_kafka,
    )
    return session


def pyspark_version() -> str:
    import pyspark

    return str(pyspark.__version__)


def apply_encryption(session: SparkSession, keys: ReleasedKeys) -> None:
    """Install key material on the Hadoop Configuration.

    Never on SparkConf: see the module docstring. After this call the session can
    read and write encrypted Parquet for exactly one tenant.
    """
    hadoop_conf = session.sparkContext._jsc.hadoopConfiguration()
    hadoop_conf.set(PROP_FACTORY, CRYPTO_FACTORY)
    hadoop_conf.set(PROP_KMS_CLIENT, KMS_CLIENT)

    from confidentia.crypto.keys import KeyHierarchy

    hadoop_conf.set(PROP_KEY_LIST, KeyHierarchy().key_list_property(keys.material))
    hadoop_conf.set(PROP_FOOTER_KEY, str(KeyId.footer(keys.tenant)))

    logger.info(
        "encryption_configured",
        tenant=keys.tenant,
        key_count=len(keys.material),
        # Identifiers only. Material never reaches a log line.
        key_ids=keys.key_ids,
    )


def set_column_keys(session: SparkSession, column_keys_property: str) -> None:
    """Set which columns get which key for the next write.

    Session-global, so it must be set immediately before the write it applies to.
    An empty value would encrypt nothing but the footer, so it is refused rather
    than silently producing a table with plaintext columns.
    """
    if not column_keys_property:
        raise ValueError(
            "empty column key mapping: every sensitive column would be written in "
            "plaintext. Check the dataset's schema contract."
        )
    session.sparkContext._jsc.hadoopConfiguration().set(
        PROP_COLUMN_KEYS, column_keys_property
    )


class ComplexColumnEncryptionError(RuntimeError):
    """A sensitive column has a type Parquet cannot encrypt by name."""


def column_keys_for_frame(
    frame: Any,
    tenant: str,
    *,
    plaintext: frozenset[str] | set[str],
    sensitivity: Sensitivity = Sensitivity.INTERNAL,
) -> str:
    """Build a column-key mapping from the columns a frame actually has.

    Every derived table -- Gold facts, quality detections, benchmark output --
    has a different schema from the Silver contract that produced it. Reusing the
    contract's mapping names columns the target does not have, and Parquet fails
    the write with "Encrypted column [x] not in file schema column list". That has
    now bitten three separate writers, so the derivation lives here rather than
    being rewritten per call site.

    Anything not in ``plaintext`` and not a surrogate key is encrypted. Surrogate
    keys are SHA-256 of a natural key and disclose nothing alone, so leaving them
    readable preserves joins without weakening anything.

    Complex types raise rather than being skipped: Parquet addresses leaf paths
    (``col.list.element``), so naming the column fails, and silently omitting it
    would write it in **plaintext** while the code appeared to encrypt it.
    """
    key_id = key_ids_by_sensitivity(tenant)[sensitivity]

    complex_columns = {
        f.name for f in frame.schema.fields
        if f.dataType.typeName() in {"array", "map", "struct"}
    }
    unhandled = complex_columns - set(plaintext)
    if unhandled:
        raise ComplexColumnEncryptionError(
            f"columns {sorted(unhandled)} have a complex type that Parquet cannot "
            "encrypt by name. Flatten them, or add them to the plaintext set if "
            "they carry nothing sensitive. Omitting them would write plaintext."
        )

    encrypted = sorted(
        c for c in frame.columns
        if c not in plaintext and c not in complex_columns and not c.endswith("_sk")
    )
    if not encrypted:
        # Nothing sensitive: encrypt one column so the footer key still applies and
        # the table is not silently written unencrypted.
        return f"{key_id}:{frame.columns[0]}"
    return f"{key_id}:{','.join(encrypted)}"


def key_ids_by_sensitivity(tenant: str, version: int = 1) -> dict[Sensitivity, str]:
    return {
        sensitivity: str(KeyId(tenant=tenant, sensitivity=sensitivity.value, version=version))
        for sensitivity in (Sensitivity.INTERNAL, Sensitivity.PII, Sensitivity.PHI)
    }


@contextmanager
def session_for_tenant(
    tenant: str,
    *,
    app_name: str | None = None,
    run_id: Any = None,
    settings: Settings | None = None,
    with_kafka: bool = False,
) -> Iterator[tuple[SparkSession, ReleasedKeys]]:
    """A session scoped to one tenant, with keys released by attestation.

    Scoped deliberately: PME configuration is session-global, so serving two
    tenants from one session risks writing one tenant's data under another's key.
    That matters more for streaming than for batch: a consumer holds its session
    open for hours, so a session shared across tenants would be a standing risk
    rather than a momentary one.
    """
    from confidentia.crypto.broker import request_keys

    keys = request_keys(tenant, run_id=run_id)
    session = build_session(
        app_name or f"confidentia-{tenant}", settings=settings, with_kafka=with_kafka
    )
    try:
        apply_encryption(session, keys)
        yield session, keys
    finally:
        session.stop()
