"""Data quality rules across six dimensions.

Two sources of rules, deliberately:

**Derived from the schema contract.** Every non-nullable column gets a completeness
rule; every typed column gets a validity rule; the business key gets a uniqueness
rule. Marking a column ``NOT NULL`` therefore *creates a check*, and no one has to
remember to add one. A hand-maintained rule list drifts from the schema within
weeks — this cannot.

**Explicit, where meaning is needed.** A regex for an email, a plausible range for a
clinical amount, the ordering of admission and discharge. These encode domain
knowledge the contract does not carry.

Each rule declares the **defect family** it targets, using the same vocabulary the
generator injects (``null_violation``, ``format_violation``, …). That alignment is
what lets Stage 7b join detections to the ground-truth answer key without a
translation layer — and a translation layer is exactly where a scoring harness
would quietly become wrong.

A rule's predicate returns True when the record **fails**. Naming it for the failure
rather than the pass avoids a class of inversion bug that is invisible in review:
an accidentally negated pass-predicate reports 100% quality.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from confidentia.catalog.contracts import SchemaContract
from confidentia.common.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import Column

logger = get_logger(__name__)


class Dimension(StrEnum):
    """The six dimensions. Standard vocabulary, chosen so the scores are
    comparable to how data quality is reported elsewhere."""

    COMPLETENESS = "completeness"   # is the value there
    VALIDITY = "validity"           # is it well-formed and in range
    CONSISTENCY = "consistency"     # does it agree with itself and its neighbours
    UNIQUENESS = "uniqueness"       # is it recorded once
    INTEGRITY = "integrity"         # does it point at something real
    TIMELINESS = "timeliness"       # did it arrive when expected


class Severity(StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"


# Severity weights for scoring. A critical failure should not be offset by a
# hundred passing cosmetic checks, so the weights are deliberately far apart.
SEVERITY_WEIGHT: dict[Severity, float] = {
    Severity.CRITICAL: 10.0,
    Severity.MAJOR: 3.0,
    Severity.MINOR: 1.0,
}

# Patterns for the validity rules. Deliberately loose: a rule that rejects valid
# data is worse than one that misses some invalid data, because analysts stop
# trusting the quarantine and start bypassing it.
PATTERNS: dict[str, str] = {
    "email": r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$",
    "phone": r"^\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}$",
    "ssn": r"^\d{3}-\d{2}-\d{4}$",
    "postal_code": r"^\d{5}(-\d{4})?$",
    "mrn": r"^MRN\d{9}$",
    "icd10": r"^[A-Z]\d{2}(\.[A-Z0-9]{1,4})?$",
    "cpt": r"^\d{5}$",
    "mcc": r"^\d{4}$",
    "ip_address": r"^(\d{1,3}\.){3}\d{1,3}$",
    "currency": r"^[A-Z]{3}$",
}

# Which pattern applies to which column name.
FIELD_PATTERNS: dict[str, str] = {
    "email": "email",
    "phone": "phone",
    "ssn": "ssn",
    "postal_code": "postal_code",
    "mrn": "mrn",
    "primary_diagnosis_code": "icd10",
    "procedure_code": "cpt",
    "mcc": "mcc",
    "ip_address": "ip_address",
    "currency": "currency",
}

# Columns whose values must be non-negative, with a plausible upper bound. The
# bounds are generous on purpose -- they exist to catch unit errors and sign
# errors, not to encode a business policy.
NUMERIC_RANGES: dict[str, tuple[float, float]] = {
    "billed_amount": (0.0, 500_000.0),
    "allowed_amount": (0.0, 500_000.0),
    "paid_amount": (0.0, 500_000.0),
    "patient_responsibility": (0.0, 500_000.0),
    "amount": (0.0, 100_000.0),
    "disputed_amount": (0.0, 100_000.0),
    "credit_limit": (0.0, 1_000_000.0),
    "risk_score": (0.0, 100.0),
    "triage_level": (1.0, 5.0),
    "units": (1.0, 100.0),
}


@dataclass(frozen=True)
class Rule:
    """One check. ``predicate`` is True when the record FAILS."""

    rule_id: str
    dataset: str
    tenant: str
    dimension: Dimension
    family: str
    severity: Severity
    description: str
    # `Callable[..., Column]`, not `Callable[[], Column]`: the predicates bind
    # their column names as default arguments so each closure captures the value it
    # was built with rather than the loop variable's last value.
    predicate: Callable[..., Column]
    field: str | None = None
    is_derived: bool = False
    # Rules whose failure means the record cannot be used at all, as opposed to
    # being usable but wrong.
    quarantines: bool = False

    @property
    def weight(self) -> float:
        return SEVERITY_WEIGHT[self.severity]


def _rule_id(tenant: str, dataset: str, family: str, field: str | None) -> str:
    return f"{tenant}.{dataset}.{family}.{field or '_record'}"


# ------------------------------------------------------------ derived rules


def completeness_rules(contract: SchemaContract) -> list[Rule]:
    """One rule per required column. Created by the contract, not by hand."""
    from pyspark.sql import functions as F

    rules = []
    for spec in contract.columns:
        if spec.nullable or spec.name.startswith("_") or spec.name == "ingest_date":
            continue
        rules.append(
            Rule(
                rule_id=_rule_id(contract.tenant.value, contract.dataset,
                                 "null_violation", spec.name),
                dataset=contract.dataset,
                tenant=contract.tenant.value,
                dimension=Dimension.COMPLETENESS,
                family="null_violation",
                severity=Severity.CRITICAL,
                description=f"{spec.name} is required by the contract but is null",
                predicate=lambda name=spec.name: F.col(name).isNull(),
                field=spec.name,
                is_derived=True,
                quarantines=True,
            )
        )
    return rules


def validity_rules(contract: SchemaContract, layer: str = "bronze") -> list[Rule]:
    """Format and range checks for columns whose shape is known.

    **Format rules are skipped for tokenised columns outside Bronze.** Silver
    replaces `mrn` with `tk_mrn_a172fa…`, so checking it against `^MRN\\d{9}$`
    fails every row — a category error, not a quality finding. Running the rules
    unaware of this scored the patient dataset at 0.20 validity with 5,000 of 5,000
    rows "failing", which is how the mistake was noticed.

    The consequence is that **format validity is a Bronze-layer measurement**. It
    describes what the source sent, which is the right place to judge it; by Silver
    the value has been deliberately replaced.
    """
    from pyspark.sql import functions as F

    rules = []
    present = {c.name for c in contract.columns}
    tokenised = set(contract.tokenised_columns) if layer != "bronze" else set()

    for field, pattern_name in FIELD_PATTERNS.items():
        if field not in present or field in tokenised:
            continue
        pattern = PATTERNS[pattern_name]
        rules.append(
            Rule(
                rule_id=_rule_id(contract.tenant.value, contract.dataset,
                                 "format_violation", field),
                dataset=contract.dataset,
                tenant=contract.tenant.value,
                dimension=Dimension.VALIDITY,
                family="format_violation",
                severity=Severity.MAJOR,
                description=f"{field} does not match the {pattern_name} pattern",
                # Nulls are the completeness rule's business, not validity's.
                # Counting them here would double-penalise one defect.
                predicate=lambda f=field, p=pattern: F.col(f).isNotNull() & ~F.col(f).rlike(p),
                field=field,
                is_derived=True,
            )
        )

    for field, (low, high) in NUMERIC_RANGES.items():
        if field not in present:
            continue
        rules.append(
            Rule(
                rule_id=_rule_id(contract.tenant.value, contract.dataset,
                                 "range_violation", field),
                dataset=contract.dataset,
                tenant=contract.tenant.value,
                dimension=Dimension.VALIDITY,
                family="range_violation",
                severity=Severity.MAJOR,
                description=f"{field} outside [{low:,.0f}, {high:,.0f}]",
                predicate=lambda f=field, lo=low, hi=high: F.col(f).isNotNull()
                & ((F.col(f) < F.lit(lo)) | (F.col(f) > F.lit(hi))),
                field=field,
                is_derived=True,
            )
        )
    return rules


def consistency_rules(contract: SchemaContract) -> list[Rule]:
    """Self-agreement: casing, whitespace, encoding, temporal ordering."""
    from pyspark.sql import functions as F

    rules = []
    present = {c.name for c in contract.columns}

    for spec in contract.columns:
        if spec.dtype != "string" or spec.name.startswith("_"):
            continue
        rules.append(
            Rule(
                rule_id=_rule_id(contract.tenant.value, contract.dataset,
                                 "whitespace_padding", spec.name),
                dataset=contract.dataset,
                tenant=contract.tenant.value,
                dimension=Dimension.CONSISTENCY,
                family="whitespace_padding",
                severity=Severity.MINOR,
                description=f"{spec.name} has leading, trailing or repeated whitespace",
                predicate=lambda n=spec.name: F.col(n).isNotNull()
                & (F.col(n) != F.trim(F.regexp_replace(F.col(n), r"\s+", " "))),
                field=spec.name,
                is_derived=True,
            )
        )
        rules.append(
            Rule(
                rule_id=_rule_id(contract.tenant.value, contract.dataset,
                                 "encoding_corruption", spec.name),
                dataset=contract.dataset,
                tenant=contract.tenant.value,
                dimension=Dimension.CONSISTENCY,
                family="encoding_corruption",
                severity=Severity.MINOR,
                description=f"{spec.name} contains mojibake (UTF-8 read as Latin-1)",
                # The signature of UTF-8 decoded as Latin-1.
                predicate=lambda n=spec.name: F.col(n).rlike("[ÃÂ][-¿]"),
                field=spec.name,
                is_derived=True,
            )
        )

    # Categorical columns that Silver lower-cases: anything still carrying upper
    # case has bypassed conforming.
    for field in ("encounter_type", "disposition", "status", "channel",
                  "auth_response", "entry_mode", "account_type"):
        if field not in present:
            continue
        rules.append(
            Rule(
                rule_id=_rule_id(contract.tenant.value, contract.dataset,
                                 "case_inconsistency", field),
                dataset=contract.dataset,
                tenant=contract.tenant.value,
                dimension=Dimension.CONSISTENCY,
                family="case_inconsistency",
                severity=Severity.MINOR,
                description=f"{field} is not lower-cased",
                predicate=lambda f=field: F.col(f).isNotNull() & (F.col(f) != F.lower(F.col(f))),
                field=field,
                is_derived=True,
            )
        )

    if {"admit_ts", "discharge_ts"} <= present:
        rules.append(
            Rule(
                rule_id=_rule_id(contract.tenant.value, contract.dataset,
                                 "temporal_inconsistency", "discharge_ts"),
                dataset=contract.dataset,
                tenant=contract.tenant.value,
                dimension=Dimension.CONSISTENCY,
                family="temporal_inconsistency",
                severity=Severity.MAJOR,
                description="discharge_ts precedes admit_ts",
                predicate=lambda: F.col("discharge_ts") < F.col("admit_ts"),
                field="discharge_ts",
            )
        )

    # A unit error keeps the value positive and in range, so only a
    # distribution-aware check finds it: flag amounts implausibly far above the
    # column's own median. This is the rule expected to score worst, and that is
    # the interesting part rather than a reason to omit it.
    for field in ("billed_amount", "amount"):
        if field not in present:
            continue
        rules.append(
            Rule(
                rule_id=_rule_id(contract.tenant.value, contract.dataset,
                                 "unit_inconsistency", field),
                dataset=contract.dataset,
                tenant=contract.tenant.value,
                dimension=Dimension.CONSISTENCY,
                family="unit_inconsistency",
                severity=Severity.MAJOR,
                description=f"{field} looks like cents reported as units (~100x typical)",
                predicate=lambda f=field: F.col(f).isNotNull() & (F.col(f) > F.lit(50_000.0)),
                field=field,
            )
        )
    return rules


def timeliness_rules(contract: SchemaContract) -> list[Rule]:
    from pyspark.sql import functions as F

    rules = []
    present = {c.name for c in contract.columns}

    if "event_ts" in present:
        rules.append(
            Rule(
                rule_id=_rule_id(contract.tenant.value, contract.dataset,
                                 "future_event", "event_ts"),
                dataset=contract.dataset,
                tenant=contract.tenant.value,
                dimension=Dimension.TIMELINESS,
                family="future_event",
                severity=Severity.MAJOR,
                description="event_ts is in the future",
                predicate=lambda: F.col("event_ts") > F.current_timestamp(),
                field="event_ts",
            )
        )
    if {"event_ts", "ingest_ts"} <= present:
        rules.append(
            Rule(
                rule_id=_rule_id(contract.tenant.value, contract.dataset,
                                 "late_arrival", "ingest_ts"),
                dataset=contract.dataset,
                tenant=contract.tenant.value,
                dimension=Dimension.TIMELINESS,
                family="late_arrival",
                severity=Severity.MINOR,
                description="ingested more than 24 hours after the event",
                predicate=lambda: F.col("ingest_ts")
                > F.col("event_ts") + F.expr("INTERVAL 24 HOURS"),
                field="ingest_ts",
            )
        )
    return rules


def build_rules(contract: SchemaContract, layer: str = "bronze") -> list[Rule]:
    """Every row-level rule for a dataset, for a given layer.

    Uniqueness and integrity are absent here on purpose: neither can be decided
    from a single row. Duplicates need a window over the business key and
    referential checks need the parent dataset, so both are evaluated separately
    by the runner.

    **The layer matters.** The same dataset answers different questions at
    different layers:

    * *Bronze* — what the source actually sent. Format and completeness are
      meaningful here, and this is the layer Stage 7b scores against the
      ground-truth answer key.
    * *Silver* — what the platform is prepared to publish. Completeness will look
      perfect because Silver quarantined the failures, and format rules on
      tokenised columns are meaningless. This layer is the promotion gate, not a
      measurement of incoming quality.

    Reporting a Silver score as though it described the source would flatter the
    platform: it would be measuring its own cleaning.
    """
    return [
        *completeness_rules(contract),
        *validity_rules(contract, layer),
        *consistency_rules(contract),
        *timeliness_rules(contract),
    ]


def rule_catalogue(layer: str = "bronze") -> list[Rule]:
    from confidentia.catalog.contracts import all_contracts

    rules: list[Rule] = []
    for contract in all_contracts():
        rules.extend(build_rules(contract, layer))
    return rules
