"""Which pipeline stages must run inside the enclave, and why (C5).

**This half of the planner is a derivation, not a model.** The cost of an enclave
is modelled because there is no hardware to measure; the *placement* is decided by
a rule applied to the schema contracts, and the answer would be the same on real
SGX. Keeping the two apart is what lets the plan say "this stage must be inside"
without qualification while every millisecond next to it is labelled MODELLED.

The rule, stated once:

    A stage must run inside the enclave if and only if it observes the plaintext
    of a column classified PII or PHI.

Everything else follows from the contracts and from what each stage does to the
data. Two consequences are worth stating because they are the interesting results:

**Tokenisation is what shrinks the boundary.** Silver replaces direct identifiers
with HMAC tokens, so a stage reading Silver no longer observes those values. That
is the mechanism by which anything downstream can leave the enclave, and the
planner derives it rather than assuming it — remove ``tokenise=True`` from the
contracts and the plan will correctly place Gold inside.

**Quasi-identifiers do not tokenise away.** A date of birth or a diagnosis code is
still PHI after Silver has run, because tokenising it would destroy the join or the
aggregation it exists for. So Gold stays inside the boundary, and what makes its
*output* publishable is disclosure control (C4), not tokenisation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from confidentia.catalog.contracts import SchemaContract, all_contracts
from confidentia.config.settings import Layer, Sensitivity, get_settings

# The classifications that oblige a stage to be inside the boundary. PUBLIC and
# INTERNAL do not: internal columns are encrypted at rest and in transit, but
# seeing one does not disclose a person.
PROTECTED = (Sensitivity.PII, Sensitivity.PHI)


class Verdict(StrEnum):
    INSIDE = "inside"
    OUTSIDE = "outside"


@dataclass(frozen=True)
class Stage:
    """One step of the pipeline, described by what it reads rather than what it is.

    ``reads_layer`` is the layer whose *plaintext* the stage observes. A stage that
    only moves ciphertext around — a copy, a checksum, a compaction that does not
    decrypt — would have ``reads_layer=None`` and fall outside the boundary. None
    of the current stages is like that, which is itself worth knowing.
    """

    name: str
    layer: Layer
    reads_layer: Layer | None
    description: str
    # Set where the stage is the one that performs tokenisation, so it necessarily
    # sees the pre-tokenisation values regardless of what its input layer holds.
    performs_tokenisation: bool = False
    # Stages that touch no tenant data at all: the gate reads scores, the API reads
    # aggregates. Named rather than inferred, because "reads no data" is a claim.
    reads_tenant_data: bool = True
    # Which recorded run this stage's duration comes from. The platform records one
    # run per layer, so Silver's three steps share one measurement and Gold's two
    # share another. Stages in the same group are charged that duration **once**
    # between them -- charging each the full amount would triple-count Silver.
    cost_group: str = ""


# The real pipeline, in order. Each entry corresponds to a CLI command and an
# Airflow task, so a stage added to the platform without being added here shows up
# as a coverage gap in the tests rather than being silently unplanned.
PIPELINE: tuple[Stage, ...] = (
    Stage(
        name="ingest.collect",
        cost_group="ingest",
        layer=Layer.BRONZE,
        reads_layer=None,
        description="Read source files/REST/CDC and write the landing zone",
    ),
    Stage(
        name="bronze.write",
        cost_group="bronze",
        layer=Layer.BRONZE,
        reads_layer=None,
        description="Type, envelope and encrypt landing data into Bronze Delta",
    ),
    Stage(
        name="silver.conform",
        cost_group="silver",
        layer=Layer.SILVER,
        reads_layer=Layer.BRONZE,
        description="Conform, validate, quarantine, deduplicate",
    ),
    Stage(
        name="silver.tokenise",
        cost_group="silver",
        layer=Layer.SILVER,
        reads_layer=Layer.BRONZE,
        description="Replace direct identifiers with field-bound HMAC tokens",
        performs_tokenisation=True,
    ),
    Stage(
        name="silver.scd2",
        cost_group="silver",
        layer=Layer.SILVER,
        reads_layer=Layer.SILVER,
        description="Close and open validity intervals from the CDC stream",
    ),
    Stage(
        name="gold.star",
        cost_group="gold",
        layer=Layer.GOLD,
        reads_layer=Layer.SILVER,
        description="Build dimensions and facts",
    ),
    Stage(
        name="gold.marts",
        cost_group="gold",
        layer=Layer.GOLD,
        reads_layer=Layer.GOLD,
        description="Aggregate, apply k-anonymity suppression, publish to Postgres",
    ),
    Stage(
        name="quality.run",
        cost_group="quality",
        layer=Layer.SILVER,
        reads_layer=Layer.SILVER,
        description="Evaluate rules row by row and write detections",
    ),
    Stage(
        name="lineage.record",
        cost_group="lineage",
        layer=Layer.BRONZE,
        reads_layer=Layer.BRONZE,
        description="Digest every record and sign the Merkle root",
    ),
    Stage(
        name="quality.gate",
        cost_group="gate",
        layer=Layer.SILVER,
        reads_layer=None,
        description="Compare scores against the threshold and block promotion",
        reads_tenant_data=False,
    ),
    Stage(
        name="api.serve",
        cost_group="api",
        layer=Layer.GOLD,
        reads_layer=None,
        description="Serve disclosure-controlled aggregates and platform metadata",
        reads_tenant_data=False,
    ),
)


def visible_protected_columns(
    stage: Stage, contracts: list[SchemaContract] | None = None
) -> dict[str, list[str]]:
    """The protected columns this stage sees **in plaintext**, per dataset.

    The layer decides whether tokenisation has happened yet. At Bronze nothing is
    tokenised, so every PII/PHI column counts. From Silver onward the tokenised
    ones no longer disclose anything, so they drop out — and what remains is the
    quasi-identifiers, which is exactly the set that keeps Gold inside.
    """
    contracts = contracts if contracts is not None else all_contracts()
    if not stage.reads_tenant_data:
        return {}

    # A stage that reads the landing zone or a source system sees raw records,
    # whatever layer it writes to.
    reading_raw = stage.reads_layer is None or stage.reads_layer is Layer.BRONZE
    tokenised_still_plaintext = reading_raw or stage.performs_tokenisation

    visible: dict[str, list[str]] = {}
    for contract in contracts:
        names = [
            column.name
            for column in contract.columns
            if column.sensitivity in PROTECTED
            and (tokenised_still_plaintext or not column.tokenise)
        ]
        if names:
            visible[f"{contract.tenant.value}/{contract.dataset}"] = sorted(names)
    return visible


@dataclass(frozen=True)
class Placement:
    stage: Stage
    verdict: Verdict
    reason: str
    protected_columns: dict[str, list[str]]

    @property
    def inside(self) -> bool:
        return self.verdict is Verdict.INSIDE

    @property
    def column_count(self) -> int:
        return sum(len(names) for names in self.protected_columns.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.name,
            "layer": self.stage.layer.value,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "protected_columns": self.column_count,
            "datasets": sorted(self.protected_columns),
        }


def place(stage: Stage, contracts: list[SchemaContract] | None = None) -> Placement:
    """Apply the rule. No cost, no model, no hardware assumption."""
    visible = visible_protected_columns(stage, contracts)

    if not stage.reads_tenant_data:
        return Placement(
            stage=stage,
            verdict=Verdict.OUTSIDE,
            reason="reads no tenant data: platform metadata and published aggregates only",
            protected_columns={},
        )
    if not visible:
        return Placement(
            stage=stage,
            verdict=Verdict.OUTSIDE,
            reason="every protected column it reads has been tokenised upstream",
            protected_columns={},
        )

    if stage.performs_tokenisation:
        why = "performs tokenisation, so it necessarily sees the pre-token values"
    elif stage.reads_layer is None:
        why = "reads raw source records, where nothing has been tokenised yet"
    else:
        why = f"reads plaintext {stage.reads_layer.value} columns classified PII/PHI"

    total = sum(len(names) for names in visible.values())
    return Placement(
        stage=stage,
        verdict=Verdict.INSIDE,
        reason=f"{why} ({total} columns across {len(visible)} datasets)",
        protected_columns=visible,
    )


def place_all(contracts: list[SchemaContract] | None = None) -> list[Placement]:
    contracts = contracts if contracts is not None else all_contracts()
    return [place(stage, contracts) for stage in PIPELINE]


# ------------------------------------------------------------------ violations


@dataclass(frozen=True)
class BoundaryViolation:
    """Somewhere protected plaintext exists outside the enclave boundary."""

    location: str
    detail: str
    protected_columns: int
    remedy: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "location": self.location,
            "detail": self.detail,
            "protected_columns": self.protected_columns,
            "remedy": self.remedy,
        }


def boundary_violations(
    contracts: list[SchemaContract] | None = None, *, lake_root: Path | None = None
) -> list[BoundaryViolation]:
    """Places where the platform stores protected plaintext at rest.

    A placement planner that only costs the stages inside the boundary would miss
    the more important question, which is whether the boundary encloses everything
    it should. The first time this ran, it did not: the landing zone was written
    before Bronze encrypted anything, so raw source records sat on disk in
    cleartext Parquet — 19 SSN-shaped strings in one ``patient`` part. ADR 0022
    closed that.

    **This now reads the disk rather than asserting a conclusion.** The original
    version returned a hard-coded finding, which was honest while the hole was open
    and would have been a lie the moment it was fixed — and, worse, would have gone
    on being printed by a planner nobody re-checked. Scanning for the ``PAR1``
    footer magic means the report is re-derived from the bytes on every run, so it
    goes quiet when the lake is clean and speaks up again if a future collector
    reintroduces a plaintext write.
    """
    from confidentia.crypto.landing import plaintext_parts

    contracts = contracts if contracts is not None else all_contracts()
    protected = sum(
        1
        for contract in contracts
        for column in contract.columns
        if column.sensitivity in PROTECTED
    )
    root = lake_root if lake_root is not None else get_settings().lake_root
    offenders = plaintext_parts(root / "landing")
    if not offenders:
        return []

    sample = ", ".join(p.name for p in offenders[:3])
    return [
        BoundaryViolation(
            location="lake/landing",
            detail=(
                f"{len(offenders)} landing file(s) carry the plaintext Parquet "
                f"footer magic (PAR1) instead of PARE — for example {sample}. Raw "
                "source records, including direct identifiers, are readable by "
                "anyone with filesystem access."
            ),
            protected_columns=protected,
            remedy=(
                "Re-encrypt them in place with `confidentia landing encrypt`, which "
                "rewrites each part under the same attestation-released keys Bronze "
                "uses, or re-ingest the affected batches. Files written before "
                "ADR 0022 are the expected cause."
            ),
        )
    ]
