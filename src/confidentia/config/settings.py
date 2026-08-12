"""Central configuration for the CONFIDENTIA platform.

Every subsystem reads its configuration from here rather than from ``os.environ``
directly, so that a single ``Settings`` instance can be constructed per process,
validated once, and logged. Values come from (in increasing precedence):

1. the defaults declared below,
2. a ``.env`` file at the repository root,
3. real environment variables.

Two design points that are load-bearing for this project:

``data_root``
    Deliberately defaults to a path *outside* OneDrive. The lakehouse, Spark
    scratch space, checkpoints and node_modules must never be sync-scanned by
    OneDrive -- it is slow, it burns the disk budget, and a half-synced Delta
    transaction log is a corrupted table.

``tee.backend``
    The single switch that selects which Trusted Execution Environment
    implementation the pipeline runs against. Defaults to ``sim``. Anything
    measured under ``sim`` is *modelled*, not observed, and the reporting layer
    is required to label it as such -- see ``TeeSettings.is_measured``.
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]

# The development JWT secret. Named rather than inlined because two places must
# agree: the default below, and the validator that refuses to let it be the value
# in force outside the local environment.
DEV_JWT_SECRET = "dev-only-change-me-and-long-enough-for-hs256"


class Environment(StrEnum):
    LOCAL = "local"
    CI = "ci"
    CLOUD = "cloud"


class ComposeProfile(StrEnum):
    """Which slice of the platform is expected to be running.

    The development machine for this project has ~7.4 GB of RAM, so the stack is
    never brought up in full outside of a demo. Code that needs a service should
    assert its profile is active rather than failing with an opaque connection
    error.
    """

    CORE = "core"  # Postgres + MinIO + API + UI
    BATCH = "batch"  # core + Spark + Airflow
    STREAM = "stream"  # core + Kafka + Spark Structured Streaming
    BENCH = "bench"  # Postgres + MinIO + Spark only; no UI, no scheduler
    DEMO = "demo"  # everything, including Prometheus/Grafana


class TeeBackend(StrEnum):
    SIM = "sim"  # calibrated cost model; NOT a real enclave
    GRAMINE_SGX = "gramine-sgx"  # Intel SGX via Gramine LibOS
    SNP_CVM = "snp-cvm"  # AMD SEV-SNP confidential VM


class Tenant(StrEnum):
    HEALTHCARE = "healthcare"
    FINANCIAL = "financial"


class Layer(StrEnum):
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"
    QUARANTINE = "quarantine"


class Sensitivity(StrEnum):
    """Column sensitivity classification.

    Drives three things: which columns get their own Parquet encryption key,
    which columns are tokenised in Silver, and which pipeline stages the enclave
    placement planner is obliged to run inside the TEE boundary.
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    PII = "pii"
    PHI = "phi"


class SparkSettings(BaseSettings):
    """Spark tuning, sized for a memory-constrained single machine.

    Defaults target ~2.2 GB resident. ``local[2]`` rather than ``local[*]``
    because oversubscribing cores on a 4-core laptop makes benchmark variance
    worse than the effects being measured.
    """

    model_config = SettingsConfigDict(env_prefix="SPARK_", extra="ignore")

    master: str = "local[2]"
    driver_memory: str = "2g"
    executor_memory: str = "2g"
    driver_max_result_size: str = "512m"
    shuffle_partitions: int = 8
    default_parallelism: int = 4
    adaptive_enabled: bool = True
    java_home: Path | None = None
    event_log_enabled: bool = False
    ui_enabled: bool = True
    ui_port: int = 4040
    # Target size for compacted Delta data files. Experiment E2 sweeps this
    # against the enclave memory budget, so it must stay configurable.
    target_file_size_mb: int = 128

    @field_validator("driver_memory", "executor_memory", "driver_max_result_size")
    @classmethod
    def _valid_jvm_size(cls, v: str) -> str:
        if not v[:-1].isdigit() or v[-1].lower() not in {"m", "g"}:
            raise ValueError(f"expected a JVM size like '2g' or '512m', got {v!r}")
        return v


class StorageSettings(BaseSettings):
    """Object storage. ``local`` for laptop work, ``minio`` and ``adls`` share an
    identical code path so nothing changes when the platform moves to Azure."""

    model_config = SettingsConfigDict(env_prefix="STORAGE_", extra="ignore")

    backend: Literal["local", "minio", "adls"] = "local"
    bucket: str = "confidentia"
    endpoint_url: str | None = None
    access_key: SecretStr | None = None
    secret_key: SecretStr | None = None
    region: str = "us-east-1"


class PostgresSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POSTGRES_", extra="ignore")

    host: str = "localhost"
    port: int = 5432
    user: str = "confidentia"
    password: SecretStr = SecretStr("confidentia")
    database: str = "confidentia"
    # Separate logical database for the simulated source-of-record OLTP system
    # that the CDC path replicates from.
    oltp_database: str = "ehr_oltp"
    pool_size: int = 5

    def dsn(self, database: str | None = None, *, driver: str = "postgresql+psycopg") -> str:
        db = database or self.database
        return (
            f"{driver}://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{db}"
        )

    def replication_dsn(self) -> str:
        """libpq DSN for the logical-replication CDC client (psycopg2)."""
        return (
            f"host={self.host} port={self.port} user={self.user} "
            f"password={self.password.get_secret_value()} dbname={self.oltp_database}"
        )


class KafkaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KAFKA_", extra="ignore")

    bootstrap_servers: str = "localhost:9092"
    topic_transactions_raw: str = "fin.txn.raw"
    topic_clinical_raw: str = "clin.event.raw"
    topic_dq_events: str = "platform.dq.events"
    topic_lineage_attest: str = "platform.lineage.attest"
    transactions_partitions: int = 6
    clinical_partitions: int = 3
    consumer_group: str = "confidentia-silver"
    # Structured Streaming watermark for late-arriving events.
    watermark_minutes: int = 10


class CryptoSettings(BaseSettings):
    """Key hierarchy and column encryption.

    The master key never appears here as a value -- only as a reference the Key
    Broker Service resolves after verifying an attestation report.
    """

    model_config = SettingsConfigDict(env_prefix="CRYPTO_", extra="ignore")

    # 'inmemory' is for tests only; 'file' is the local dev KMS backed by a
    # sealed keystore; 'azure-mhsm' is Managed HSM with Secure Key Release.
    kms_backend: Literal["inmemory", "file", "azure-mhsm"] = "file"
    kms_keystore_path: Path | None = None
    master_key_id: str = "confidentia-mek-v1"
    data_key_rotation_days: int = 90
    aes_key_bits: Literal[128, 256] = 256
    # Column encryption strategy. Set to 'pme' by the Stage 1 spike, which
    # verified encrypted-footer output, absence of plaintext in the bytes, and
    # fail-closed reads. See docs/adr/0007-column-encryption-strategy.md.
    # 'pme'      -> Parquet Modular Encryption (per-column keys)
    # 'envelope' -> encrypted binary columns + crypto UDFs (fallback)
    # 'none'     -> plaintext; only valid as the experiment E4 baseline arm
    column_encryption: Literal["pme", "envelope", "none"] = "pme"
    # Delta ignores DataFrameWriter encryption options -- they must be set on the
    # Hadoop Configuration instead. Proven by spike probe 'delta-hadoopconf'.
    encryption_via_hadoop_conf: bool = True
    attestation_curve: Literal["P-256", "P-384"] = "P-256"
    attestation_nonce_ttl_seconds: int = 300


class TeeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TEE_", extra="ignore")

    backend: TeeBackend = TeeBackend.SIM
    # Enclave Page Cache budget. Under 'sim' this is the modelled ceiling that
    # triggers simulated paging; under real backends it is read from hardware.
    epc_size_mb: int = 128
    # Modelled cost parameters, calibrated from published microbenchmarks. These
    # are inputs to a model, not measurements -- see docs/adr/0006.
    sim_ecall_latency_us: float = 8.0
    sim_ocall_latency_us: float = 12.0
    sim_page_fault_us: float = 50.0
    sim_cold_start_ms: float = 120.0
    # Fail closed: if attestation cannot be verified, no key is released and the
    # pipeline stops rather than falling back to plaintext.
    require_attestation: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_measured(self) -> bool:
        """False when TEE-specific timings are modelled rather than observed.

        Reporting code must consult this and label output ``MODELLED`` when it is
        False. Rule 8 of the project brief: never present a modelled number as a
        measurement.
        """
        return self.backend is not TeeBackend.SIM

    @computed_field  # type: ignore[prop-decorator]
    @property
    def measurement_label(self) -> str:
        return "MEASURED" if self.is_measured else "MODELLED"


class DisclosureSettings(BaseSettings):
    """Controls what a Gold-layer aggregate is allowed to reveal."""

    model_config = SettingsConfigDict(env_prefix="DISCLOSURE_", extra="ignore")

    enabled: bool = True
    # Small-cell suppression: aggregates covering fewer than k entities are
    # withheld, because a count of 1 in a PHI mart identifies a person.
    min_cell_count: int = 5
    suppression_marker: str = "SUPPRESSED"
    differential_privacy_enabled: bool = False
    dp_epsilon: float = 1.0


class QualitySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DQ_", extra="ignore")

    # Below this score, promotion from Silver to Gold is blocked.
    promotion_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    # Above this quarantine rate, the run is failed outright rather than
    # producing a technically-valid but meaningless Gold table.
    max_quarantine_rate: float = Field(default=0.10, ge=0.0, le=1.0)
    freshness_sla_minutes: int = 60


class AuthMode(StrEnum):
    """How bearer tokens presented to the API are verified.

    ``OIDC`` is the real mode: tokens are signed by an external identity provider
    with an asymmetric key, and the API verifies them against the provider's
    published JWKS. It never holds a signing key, so a compromised API cannot mint
    a token for itself.

    ``LOCAL`` exists because this project has no identity provider to point at. It
    verifies HS256 tokens minted by ``confidentia api token`` against a shared
    secret, which means the verifier *is* the issuer -- acceptable for development
    and refused outside it (see ``AuthSettings``).
    """

    OIDC = "oidc"
    LOCAL = "local"


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUTH_", extra="ignore")

    mode: AuthMode = AuthMode.LOCAL

    # --- LOCAL mode ---
    # At least 32 bytes: PyJWT warns below that for HS256, and a development
    # default that trips a security warning on every token teaches people to
    # ignore the warning.
    jwt_secret: SecretStr = SecretStr(DEV_JWT_SECRET)
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 30
    refresh_token_days: int = 7
    argon2_time_cost: int = 2
    argon2_memory_kib: int = 65536

    # --- OIDC mode ---
    # No defaults that would let a misconfigured deployment silently accept
    # anything: `issuer` and `audience` are both checked, and a blank one is a
    # startup error rather than a skipped claim check.
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    # Which claim carries the role list. Providers disagree: Entra uses `roles`,
    # Keycloak nests them under `realm_access.roles`, Auth0 uses a namespaced URI.
    # A dotted path is resolved against the decoded claims.
    oidc_roles_claim: str = "roles"
    # Asymmetric only. Permitting HS256 here would let anyone who can read the
    # JWKS document sign tokens with the public key as the HMAC secret -- the
    # classic algorithm-confusion attack.
    oidc_algorithms: tuple[str, ...] = ("RS256", "ES256")
    jwks_cache_seconds: int = 300
    # Tolerance for clock skew between the provider and this host, in seconds.
    leeway_seconds: int = 60

    @model_validator(mode="after")
    def _reject_default_secret_outside_local(self) -> AuthSettings:
        env = os.getenv("CONFIDENTIA_ENV", Environment.LOCAL)
        if env == Environment.LOCAL:
            return self
        if self.mode is AuthMode.LOCAL:
            # A shared-secret verifier in a deployed environment means anyone who
            # can read the config can mint an auditor token. Fail at startup, not
            # at the first request.
            raise ValueError(
                "AUTH_MODE=local is refused outside the local environment: the API "
                "would both issue and verify its own tokens. Set AUTH_MODE=oidc."
            )
        if self.jwt_secret.get_secret_value() == DEV_JWT_SECRET:
            raise ValueError("AUTH_JWT_SECRET must be set outside the local environment")
        return self

    @model_validator(mode="after")
    def _oidc_requires_issuer_and_audience(self) -> AuthSettings:
        if self.mode is AuthMode.OIDC and not (
            self.oidc_issuer and self.oidc_audience and self.oidc_jwks_url
        ):
            raise ValueError(
                "AUTH_MODE=oidc requires AUTH_OIDC_ISSUER, AUTH_OIDC_AUDIENCE and "
                "AUTH_OIDC_JWKS_URL. Verifying a signature without checking the "
                "issuer and audience accepts tokens minted for a different service."
            )
        return self


class ApiSettings(BaseSettings):
    """The read-only serving layer.

    ``read_only`` is a setting rather than a constant so that the decision is
    visible in configuration and in ``/meta``, but it defaults closed and nothing
    in the codebase flips it: ADR 0016 records why the API does not trigger
    pipeline runs.
    """

    model_config = SettingsConfigDict(env_prefix="API_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    root_path: str = ""
    # Browsers block cross-origin XHR, so the dashboard's dev server needs naming.
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
    default_page_size: int = 50
    # A hard ceiling. `platform.quarantine_record` has ~31k rows; an unbounded
    # limit would let one request materialise all of them into JSON.
    max_page_size: int = 500
    read_only: bool = True
    # Slow-query visibility without an APM: anything over this is logged with its
    # route and duration.
    slow_query_ms: int = 1000


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        env_prefix="CONFIDENTIA_",
        extra="ignore",
        nested_model_default_partial_update=True,
    )

    env: Environment = Environment.LOCAL
    profile: ComposeProfile = ComposeProfile.CORE
    log_level: str = "INFO"
    log_json: bool = False

    # Kept off OneDrive on purpose. See module docstring.
    data_root: Path = (
        Path("C:/confidentia-data") if os.name == "nt" else Path("/var/lib/confidentia")
    )

    spark: SparkSettings = Field(default_factory=SparkSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    crypto: CryptoSettings = Field(default_factory=CryptoSettings)
    tee: TeeSettings = Field(default_factory=TeeSettings)
    disclosure: DisclosureSettings = Field(default_factory=DisclosureSettings)
    quality: QualitySettings = Field(default_factory=QualitySettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)

    # ---- derived paths -------------------------------------------------

    @property
    def lake_root(self) -> Path:
        return self.data_root / "lake"

    def layer_path(self, layer: Layer, tenant: Tenant, dataset: str) -> Path:
        return self.lake_root / layer.value / tenant.value / dataset

    @property
    def checkpoint_root(self) -> Path:
        return self.data_root / "checkpoints"

    @property
    def spark_warehouse(self) -> Path:
        return self.data_root / "spark-warehouse"

    @property
    def spark_local_dir(self) -> Path:
        """Shuffle spill directory. On a 2 GB executor this sees heavy use, which
        is exactly why it must not sit inside a synced OneDrive folder."""
        return self.data_root / "spark-tmp"

    @property
    def keystore_path(self) -> Path:
        return self.crypto.kms_keystore_path or (self.data_root / "keystore")

    def ensure_directories(self) -> None:
        for path in (
            self.lake_root,
            self.checkpoint_root,
            self.spark_warehouse,
            self.spark_local_dir,
            self.keystore_path,
            self.data_root / "raw",
            self.data_root / "logs",
        ):
            path.mkdir(parents=True, exist_ok=True)

    @model_validator(mode="after")
    def _warn_on_synced_data_root(self) -> Settings:
        parts = {p.lower() for p in self.data_root.parts}
        if "onedrive" in parts or "dropbox" in parts:
            raise ValueError(
                f"data_root {self.data_root} is inside a file-sync folder. Delta "
                "transaction logs and Spark spill files must not be synced; set "
                "CONFIDENTIA_DATA_ROOT to a local path."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
