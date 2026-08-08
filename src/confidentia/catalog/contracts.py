"""Schema contracts with column sensitivity classification.

A contract states, per column: its type, whether it may be null, and how sensitive
it is. The sensitivity classification is not documentation — it is executable, and
it drives three things:

1. **Which encryption key protects the column** (contribution C1). Columns are
   grouped by class, and each class has its own derived key, so a principal holding
   the `internal` key cannot read `phi` columns.
2. **Which columns stay in plaintext.** Partition and filter columns are classified
   `public` deliberately: Parquet Modular Encryption leaves unlisted columns
   unencrypted, and that is what preserves predicate pushdown. Encrypting a
   partition column would make every query a full scan (ADR 0007).
3. **Which columns Silver must tokenise** (Stage 5).

**The classification is the security boundary.** Misclassifying `ssn` as `internal`
would silently place it under the wrong key and expose it to the wrong role — with
no error anywhere. Hence `validate_contract`, which refuses obviously wrong
classifications, and the test that asserts every known identifier column is at
least PII.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from confidentia.config.settings import Sensitivity, Tenant

# Column-name fragments that must never be classified below PII. A cheap guard
# against the most damaging possible mistake in this file.
IDENTIFIER_MARKERS = (
    "ssn", "mrn", "name", "email", "phone", "address", "dob", "date_of_birth",
    "account_number", "ip_address", "device_id", "npi",
)


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    dtype: str
    nullable: bool = True
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    description: str = ""
    # Set where a column carries a direct identifier that Silver must tokenise
    # rather than merely encrypt at rest.
    tokenise: bool = False


@dataclass(frozen=True)
class SchemaContract:
    dataset: str
    tenant: Tenant
    version: int
    columns: tuple[ColumnSpec, ...]
    business_key: tuple[str, ...]
    event_time_column: str | None = None
    # Bronze partitions on ingest date, not event date: a late-arriving record
    # belongs to the batch that delivered it, and partitioning raw data on a value
    # inside the data makes backfill rewrite history.
    partition_columns: tuple[str, ...] = ("ingest_date",)
    description: str = ""

    def column(self, name: str) -> ColumnSpec | None:
        return next((c for c in self.columns if c.name == name), None)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def columns_by_sensitivity(self) -> dict[Sensitivity, list[str]]:
        grouped: dict[Sensitivity, list[str]] = {}
        for spec in self.columns:
            grouped.setdefault(spec.sensitivity, []).append(spec.name)
        return grouped

    @property
    def plaintext_columns(self) -> tuple[str, ...]:
        """Columns left unencrypted so pushdown survives."""
        return tuple(c.name for c in self.columns if c.sensitivity is Sensitivity.PUBLIC)

    @property
    def tokenised_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.tokenise)

    def spark_schema(self) -> str:
        """DDL string for `spark.read.schema(...)`."""
        return ", ".join(
            f"`{c.name}` {c.dtype}{'' if c.nullable else ' NOT NULL'}" for c in self.columns
        )

    def column_keys_property(self, key_ids: dict[Sensitivity, str]) -> str:
        """Build ``parquet.encryption.column.keys``.

        Format: ``keyId:col1,col2;keyId2:col3``. Columns classified PUBLIC are
        omitted entirely — that omission is what leaves them readable for pushdown.
        """
        parts: list[str] = []
        for sensitivity, names in sorted(
            self.columns_by_sensitivity().items(), key=lambda kv: kv[0].value
        ):
            if sensitivity is Sensitivity.PUBLIC:
                continue
            key_id = key_ids.get(sensitivity)
            if key_id and names:
                parts.append(f"{key_id}:{','.join(sorted(names))}")
        return ";".join(parts)


class ContractError(ValueError):
    pass


def validate_contract(contract: SchemaContract) -> None:
    """Refuse contracts that are structurally or classification-wise wrong."""
    names = [c.name for c in contract.columns]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ContractError(f"{contract.dataset}: duplicate columns {sorted(duplicates)}")

    for key in contract.business_key:
        if key not in names:
            raise ContractError(f"{contract.dataset}: business key {key!r} is not a column")

    if contract.event_time_column and contract.event_time_column not in names:
        raise ContractError(
            f"{contract.dataset}: event time column {contract.event_time_column!r} is not a column"
        )

    # A partition column must be plaintext, or every partition-pruned query becomes
    # a full scan and the partitioning is worse than useless.
    for partition in contract.partition_columns:
        spec = contract.column(partition)
        if spec is not None and spec.sensitivity is not Sensitivity.PUBLIC:
            raise ContractError(
                f"{contract.dataset}: partition column {partition!r} is classified "
                f"{spec.sensitivity.value}; partition columns must be public or "
                "partition pruning cannot work"
            )

    for spec in contract.columns:
        lowered = spec.name.lower()
        if any(marker in lowered for marker in IDENTIFIER_MARKERS) and spec.sensitivity in (
            Sensitivity.PUBLIC,
            Sensitivity.INTERNAL,
        ):
            raise ContractError(
                f"{contract.dataset}.{spec.name} looks like a direct identifier but is "
                f"classified {spec.sensitivity.value}. Misclassification here silently "
                "places it under the wrong key."
            )


# ---------------------------------------------------------------- healthcare

_INGEST_ENVELOPE = (
    ColumnSpec("_ingest_run_id", "string", False, Sensitivity.PUBLIC, "Run that landed this record"),
    ColumnSpec("_batch_digest", "string", False, Sensitivity.PUBLIC, "Content-addressed batch id"),
    ColumnSpec("_source_file", "string", True, Sensitivity.PUBLIC),
    ColumnSpec("_source_system", "string", True, Sensitivity.PUBLIC),
    ColumnSpec("_ingested_at", "timestamp", False, Sensitivity.PUBLIC),
    ColumnSpec("_record_ordinal", "bigint", False, Sensitivity.PUBLIC),
    ColumnSpec("ingest_date", "date", False, Sensitivity.PUBLIC, "Bronze partition column"),
)

PATIENT = SchemaContract(
    dataset="patient",
    tenant=Tenant.HEALTHCARE,
    version=1,
    description="Patient master. SCD2 subject in Silver.",
    business_key=("patient_id",),
    event_time_column="updated_at",
    columns=(
        ColumnSpec("patient_id", "string", False, Sensitivity.INTERNAL, "Surrogate source id"),
        ColumnSpec("mrn", "string", False, Sensitivity.PHI, "Medical record number", tokenise=True),
        ColumnSpec("first_name", "string", True, Sensitivity.PII, tokenise=True),
        ColumnSpec("last_name", "string", True, Sensitivity.PII, tokenise=True),
        ColumnSpec("date_of_birth", "date", True, Sensitivity.PHI),
        ColumnSpec("sex", "string", True, Sensitivity.PHI),
        ColumnSpec("ssn", "string", True, Sensitivity.PII, tokenise=True),
        ColumnSpec("email", "string", True, Sensitivity.PII, tokenise=True),
        ColumnSpec("phone", "string", True, Sensitivity.PII, tokenise=True),
        ColumnSpec("address_line1", "string", True, Sensitivity.PII),
        ColumnSpec("city", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("state", "string", True, Sensitivity.PUBLIC),
        ColumnSpec("postal_code", "string", True, Sensitivity.PII),
        ColumnSpec("plan_id", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("primary_care_provider_id", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("created_at", "timestamp", True, Sensitivity.INTERNAL),
        ColumnSpec("updated_at", "timestamp", True, Sensitivity.INTERNAL),
        *_INGEST_ENVELOPE,
    ),
)

ENCOUNTER = SchemaContract(
    dataset="encounter",
    tenant=Tenant.HEALTHCARE,
    version=1,
    description="Clinical encounters. Primary healthcare fact.",
    business_key=("encounter_id",),
    event_time_column="event_ts",
    columns=(
        ColumnSpec("encounter_id", "string", False, Sensitivity.INTERNAL),
        ColumnSpec("patient_id", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("provider_id", "string", True, Sensitivity.INTERNAL),
        # Facility and state stay public so the dashboard can aggregate by site and
        # region without a key release.
        ColumnSpec("facility_id", "string", True, Sensitivity.PUBLIC),
        ColumnSpec("encounter_type", "string", True, Sensitivity.PUBLIC),
        ColumnSpec("admit_ts", "timestamp", True, Sensitivity.PHI),
        ColumnSpec("discharge_ts", "timestamp", True, Sensitivity.PHI),
        ColumnSpec("primary_diagnosis_code", "string", True, Sensitivity.PHI),
        ColumnSpec("diagnosis_system", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("secondary_diagnosis_codes", "string", True, Sensitivity.PHI),
        ColumnSpec("disposition", "string", True, Sensitivity.PHI),
        ColumnSpec("triage_level", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("event_ts", "timestamp", True, Sensitivity.INTERNAL),
        ColumnSpec("ingest_ts", "timestamp", True, Sensitivity.PUBLIC),
        ColumnSpec("is_late_arrival", "boolean", True, Sensitivity.PUBLIC),
        ColumnSpec("source_system", "string", True, Sensitivity.PUBLIC),
        *_INGEST_ENVELOPE,
    ),
)

CLAIM_LINE = SchemaContract(
    dataset="claim_line",
    tenant=Tenant.HEALTHCARE,
    version=1,
    description="Claim line items. Financial detail of an encounter.",
    business_key=("claim_line_id",),
    event_time_column="event_ts",
    columns=(
        ColumnSpec("claim_line_id", "string", False, Sensitivity.INTERNAL),
        ColumnSpec("claim_number", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("encounter_id", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("line_number", "int", True, Sensitivity.PUBLIC),
        ColumnSpec("procedure_code", "string", True, Sensitivity.PHI),
        ColumnSpec("units", "int", True, Sensitivity.INTERNAL),
        ColumnSpec("billed_amount", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("allowed_amount", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("paid_amount", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("patient_responsibility", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("is_denied", "boolean", True, Sensitivity.PUBLIC),
        ColumnSpec("denial_reason", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("service_date", "timestamp", True, Sensitivity.PHI),
        ColumnSpec("event_ts", "timestamp", True, Sensitivity.INTERNAL),
        ColumnSpec("source_system", "string", True, Sensitivity.PUBLIC),
        *_INGEST_ENVELOPE,
    ),
)

# ----------------------------------------------------------------- financial

ACCOUNT = SchemaContract(
    dataset="account",
    tenant=Tenant.FINANCIAL,
    version=1,
    description="Account master. SCD2 subject in Silver.",
    business_key=("account_id",),
    event_time_column="updated_at",
    columns=(
        ColumnSpec("account_id", "string", False, Sensitivity.INTERNAL),
        ColumnSpec("account_number_last4", "string", True, Sensitivity.PII, tokenise=True),
        ColumnSpec("customer_name", "string", True, Sensitivity.PII, tokenise=True),
        ColumnSpec("email", "string", True, Sensitivity.PII, tokenise=True),
        ColumnSpec("phone", "string", True, Sensitivity.PII, tokenise=True),
        ColumnSpec("account_type", "string", True, Sensitivity.PUBLIC),
        ColumnSpec("status", "string", True, Sensitivity.PUBLIC),
        ColumnSpec("risk_score", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("credit_limit", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("home_city", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("home_state", "string", True, Sensitivity.PUBLIC),
        ColumnSpec("home_country", "string", True, Sensitivity.PUBLIC),
        ColumnSpec("opened_at", "timestamp", True, Sensitivity.INTERNAL),
        ColumnSpec("updated_at", "timestamp", True, Sensitivity.INTERNAL),
        *_INGEST_ENVELOPE,
    ),
)

TRANSACTION = SchemaContract(
    dataset="transaction",
    tenant=Tenant.FINANCIAL,
    version=1,
    description="Card authorisations. Primary financial fact. Carries no fraud label.",
    business_key=("transaction_id",),
    event_time_column="event_ts",
    columns=(
        ColumnSpec("transaction_id", "string", False, Sensitivity.INTERNAL),
        ColumnSpec("account_id", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("merchant_id", "string", True, Sensitivity.PUBLIC),
        ColumnSpec("mcc", "string", True, Sensitivity.PUBLIC),
        ColumnSpec("amount", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("currency", "string", True, Sensitivity.PUBLIC),
        ColumnSpec("channel", "string", True, Sensitivity.PUBLIC),
        ColumnSpec("entry_mode", "string", True, Sensitivity.PUBLIC),
        ColumnSpec("auth_response", "string", True, Sensitivity.PUBLIC),
        ColumnSpec("terminal_country", "string", True, Sensitivity.PUBLIC),
        ColumnSpec("terminal_city", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("device_id", "string", True, Sensitivity.PII),
        ColumnSpec("ip_address", "string", True, Sensitivity.PII, tokenise=True),
        ColumnSpec("latitude", "string", True, Sensitivity.PII),
        ColumnSpec("longitude", "string", True, Sensitivity.PII),
        ColumnSpec("event_ts", "timestamp", True, Sensitivity.INTERNAL),
        ColumnSpec("ingest_ts", "timestamp", True, Sensitivity.PUBLIC),
        ColumnSpec("is_late_arrival", "boolean", True, Sensitivity.PUBLIC),
        ColumnSpec("source_system", "string", True, Sensitivity.PUBLIC),
        *_INGEST_ENVELOPE,
    ),
)

CHARGEBACK = SchemaContract(
    dataset="chargeback",
    tenant=Tenant.FINANCIAL,
    version=1,
    description="Disputes. Late-arriving confirmation of fraud; forces Gold restatement.",
    business_key=("chargeback_id",),
    event_time_column="filed_ts",
    columns=(
        ColumnSpec("chargeback_id", "string", False, Sensitivity.INTERNAL),
        ColumnSpec("transaction_id", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("reason_code", "string", True, Sensitivity.PUBLIC),
        ColumnSpec("disputed_amount", "string", True, Sensitivity.INTERNAL),
        ColumnSpec("filed_ts", "timestamp", True, Sensitivity.INTERNAL),
        ColumnSpec("status", "string", True, Sensitivity.PUBLIC),
        ColumnSpec("is_confirmed_fraud", "boolean", True, Sensitivity.INTERNAL),
        ColumnSpec("source_system", "string", True, Sensitivity.PUBLIC),
        *_INGEST_ENVELOPE,
    ),
)

CONTRACTS: dict[tuple[str, str], SchemaContract] = {
    (c.tenant.value, c.dataset): c
    for c in (PATIENT, ENCOUNTER, CLAIM_LINE, ACCOUNT, TRANSACTION, CHARGEBACK)
}


@lru_cache(maxsize=64)
def get_contract(tenant: str, dataset: str) -> SchemaContract:
    contract = CONTRACTS.get((tenant, dataset))
    if contract is None:
        known = sorted(f"{t}/{d}" for t, d in CONTRACTS)
        raise ContractError(f"no contract for {tenant}/{dataset}. Known: {known}")
    return contract


def all_contracts() -> list[SchemaContract]:
    return list(CONTRACTS.values())


# Fail at import time rather than mid-pipeline: a bad classification must never
# reach a write.
for _contract in CONTRACTS.values():
    validate_contract(_contract)
