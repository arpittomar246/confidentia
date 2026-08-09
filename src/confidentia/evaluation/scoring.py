"""Precision, recall and F1 of the quality layer against the answer key.

Pure Python and pandas-free by design: everything here operates on sets of
``Claim``, so the matching rules can be tested exhaustively with a handful of
synthetic fixtures rather than by standing up Spark and a 145,000-row table. The
Spark work — reading the encrypted detections table — lives in ``detections.py``
and hands this module plain tuples.

The matching criterion
----------------------

A true positive requires **(record_key, field, family)** to match on both sides.

The loose alternative — matching on ``record_key`` alone — was rejected. Under it,
a null check that fired on the wrong column would count as a correct detection of
whatever defect that record happened to carry, and precision would approach 1.0
for reasons that have nothing to do with the rules being right. The whole reason
Stage 7a records detections at *field* level is so this measurement can be strict;
relaxing it here would waste that.

The unit of evaluation is a **claim**, not a row
------------------------------------------------

Both sides are deduplicated to distinct ``(record_key, field, family)`` triples
before matching. This is what makes two otherwise-nasty complications disappear:

* ``duplicate_record`` appends a near-copy of an existing row, so the detection
  side flags *both* copies while the manifest records the corruption once.
* Bronze ``encounter`` holds a double delivery (the file and REST collectors each
  delivered all 60,000 rows), so every record key appears twice.

Counting rows would make recall exceed 1.0 in both cases. Counting distinct claims
answers the question actually being asked — *did the platform identify this defect
on this field of this record* — which is answered once regardless of how many
physical rows carry it.

Undefined is not zero
---------------------

A family with no ground truth in a dataset has no recall: 0/0 is undefined, and
reporting it as 0.0 would say the detector missed everything when in fact nothing
was injected. Same for precision when a family produced no detections. Both are
reported as ``None`` and stored as SQL ``NULL``. The counts are always present, so
nothing is hidden by the absence of a ratio.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field

MATCHING_CRITERION = "strict:(record_key,field,family)"

# Families the platform detects but the generator never injects, so there is no
# answer key for them. Scoring them would count every detection as a false
# positive and slander a rule that may be working perfectly. They are reported
# separately as "detected, unscoreable" with their counts intact.
UNSCOREABLE_FAMILIES = frozenset({"future_event", "late_arrival"})

# Present in the manifest but not a data quality defect: `latent_fraud` is the
# label the analytics evaluation is scored against, confirmed by a chargeback days
# later. A DQ rule that flagged it would be a fraud model, not a quality check.
NON_DEFECT_TYPES = frozenset({"latent_fraud"})


@dataclass(frozen=True, slots=True)
class Claim:
    """One assertion that a defect of ``family`` affects ``field`` of a record.

    Made by the quality layer (a detection) or by the generator (ground truth).
    Identical structure on both sides is deliberate: the two vocabularies were
    aligned in Stage 2 and Stage 7a precisely so that no translation layer sits
    between them, because a translation layer is where a scoring harness quietly
    becomes wrong.
    """

    record_key: str
    field: str
    family: str


@dataclass(frozen=True)
class FamilyScore:
    """Confusion counts and metrics for one family, or one family/field pair."""

    family: str
    field: str | None
    true_positives: int
    false_positives: int
    false_negatives: int
    truth_claims: int
    detected_claims: int
    # Whether a rule for this family (and field, on field-level rows) was
    # registered at all. Separates "a rule ran and missed it" from "nothing was
    # ever checked", which are very different findings and look identical in a
    # recall number.
    rule_exists: bool | None = None
    scoreable: bool = True

    @property
    def precision(self) -> float | None:
        predicted = self.true_positives + self.false_positives
        return self.true_positives / predicted if predicted else None

    @property
    def recall(self) -> float | None:
        actual = self.true_positives + self.false_negatives
        return self.true_positives / actual if actual else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None:
            return None
        return 0.0 if (p + r) == 0 else 2 * p * r / (p + r)

    def summary(self) -> str:
        def fmt(value: float | None) -> str:
            return f"{value:.3f}" if value is not None else "  -- "

        name = self.family if self.field is None else f"{self.family}/{self.field}"
        return (
            f"{name:44s} P={fmt(self.precision)} R={fmt(self.recall)} "
            f"F1={fmt(self.f1)}  tp={self.true_positives:<6} "
            f"fp={self.false_positives:<6} fn={self.false_negatives:<6}"
        )


@dataclass
class EvaluationResult:
    tenant: str
    dataset: str
    layer: str
    overall: FamilyScore
    families: list[FamilyScore] = dataclass_field(default_factory=list)
    fields: list[FamilyScore] = dataclass_field(default_factory=list)
    # Detections for families with no ground truth. Counts only; no metrics.
    unscoreable: list[FamilyScore] = dataclass_field(default_factory=list)
    matching_criterion: str = MATCHING_CRITERION

    def summary(self) -> str:
        return (
            f"{self.tenant}/{self.dataset} @{self.layer}: "
            f"P={_fmt(self.overall.precision)} R={_fmt(self.overall.recall)} "
            f"F1={_fmt(self.overall.f1)} over {self.overall.truth_claims:,} injected "
            f"defects, {len(self.families)} families"
        )


def _fmt(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "--"


def _confusion(
    detected: set[Claim],
    truth: set[Claim],
    *,
    family: str,
    field: str | None,
    rule_exists: bool | None,
) -> FamilyScore:
    return FamilyScore(
        family=family,
        field=field,
        true_positives=len(detected & truth),
        false_positives=len(detected - truth),
        false_negatives=len(truth - detected),
        truth_claims=len(truth),
        detected_claims=len(detected),
        rule_exists=rule_exists,
        scoreable=bool(truth),
    )


def score_claims(
    detected: Iterable[Claim],
    truth: Iterable[Claim],
    *,
    tenant: str,
    dataset: str,
    layer: str,
    rule_coverage: set[tuple[str, str]] | None = None,
) -> EvaluationResult:
    """Score detections against ground truth, per family and per family/field.

    ``rule_coverage`` is the set of ``(family, field)`` pairs for which a rule was
    actually registered — read from ``platform.dq_rule``, the authoritative record
    of what ran. Passing it turns a bare recall of 0.0 into a diagnosis: either the
    rule fired and missed, or no rule existed for that column at all.
    """
    detected_set = set(detected)
    truth_set = set(truth)

    # Peel off the families that cannot be scored before anything is counted,
    # otherwise their detections land in the overall false-positive total.
    unscoreable_claims = {c for c in detected_set if c.family in UNSCOREABLE_FAMILIES}
    detected_set -= unscoreable_claims

    covered_families = {family for family, _ in rule_coverage} if rule_coverage else set()

    families: list[FamilyScore] = []
    fields: list[FamilyScore] = []

    for family in sorted({c.family for c in detected_set} | {c.family for c in truth_set}):
        family_detected = {c for c in detected_set if c.family == family}
        family_truth = {c for c in truth_set if c.family == family}
        families.append(
            _confusion(
                family_detected,
                family_truth,
                family=family,
                field=None,
                rule_exists=(family in covered_families) if rule_coverage else None,
            )
        )

        for name in sorted(
            {c.field for c in family_detected} | {c.field for c in family_truth}
        ):
            fields.append(
                _confusion(
                    {c for c in family_detected if c.field == name},
                    {c for c in family_truth if c.field == name},
                    family=family,
                    field=name,
                    rule_exists=(
                        (family, name) in rule_coverage if rule_coverage else None
                    ),
                )
            )

    unscoreable = [
        FamilyScore(
            family=family,
            field=None,
            true_positives=0,
            false_positives=0,
            false_negatives=0,
            truth_claims=0,
            detected_claims=sum(1 for c in unscoreable_claims if c.family == family),
            rule_exists=True,
            scoreable=False,
        )
        for family in sorted({c.family for c in unscoreable_claims})
    ]

    # Micro-average: pool the claims rather than averaging the per-family ratios.
    # Macro-averaging would give `type_drift` (a handful of claims) the same
    # influence as `null_violation` (thousands), which overstates whichever family
    # happens to be rarest.
    overall = _confusion(
        detected_set, truth_set, family="_all", field=None, rule_exists=None
    )

    return EvaluationResult(
        tenant=tenant,
        dataset=dataset,
        layer=layer,
        overall=overall,
        families=families,
        fields=fields,
        unscoreable=unscoreable,
    )
