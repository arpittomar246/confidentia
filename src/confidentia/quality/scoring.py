"""Data quality scoring.

A single score is what a dashboard needs and is also a lossy compression: a dataset
can score 0.97 while its integrity dimension has collapsed to 0.4, because there
are many more completeness rules than referential ones. Both the headline and its
components are therefore always computed and stored together, and the headline is
never reported alone.

Weighting is by severity, not by rule count. Without that, adding twenty cosmetic
whitespace rules would dilute a critical completeness failure until it disappeared
— the score would improve by *adding checks*, which is precisely backwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from confidentia.quality.rules import SEVERITY_WEIGHT, Dimension, Rule, Severity


@dataclass
class RuleOutcome:
    rule: Rule
    rows_evaluated: int
    rows_failed: int

    @property
    def pass_rate(self) -> float:
        """Fraction of evaluated rows that passed.

        A rule that evaluated nothing scores 1.0 by convention, but
        ``rows_evaluated`` is retained so that a vacuous pass is distinguishable
        from a real one — "100% valid across 0 rows" is not a quality signal.
        """
        if self.rows_evaluated == 0:
            return 1.0
        return 1.0 - (self.rows_failed / self.rows_evaluated)

    @property
    def vacuous(self) -> bool:
        return self.rows_evaluated == 0


@dataclass
class QualityScore:
    tenant: str
    dataset: str
    layer: str
    overall: float
    dimensions: dict[Dimension, float | None] = field(default_factory=dict)
    rows_total: int = 0
    rows_failing: int = 0
    rules_evaluated: int = 0
    rules_failed: int = 0
    vacuous_rules: int = 0
    promotion_blocked: bool = False

    def summary(self) -> str:
        parts = " ".join(
            f"{d.value[:4]}={v:.3f}" if v is not None else f"{d.value[:4]}=--"
            for d, v in sorted(self.dimensions.items(), key=lambda kv: kv[0].value)
        )
        gate = "  BLOCKED" if self.promotion_blocked else ""
        return (
            f"{self.tenant}/{self.dataset}: score={self.overall:.3f}  {parts}  "
            f"({self.rules_failed}/{self.rules_evaluated} rules failing, "
            f"{self.rows_failing:,}/{self.rows_total:,} rows){gate}"
        )


def weighted_pass_rate(outcomes: list[RuleOutcome]) -> float | None:
    """Severity-weighted mean pass rate. None when nothing was evaluated."""
    scored = [o for o in outcomes if not o.vacuous]
    if not scored:
        return None
    total_weight = sum(SEVERITY_WEIGHT[Severity(o.rule.severity)] for o in scored)
    if total_weight == 0:
        return None
    return sum(
        o.pass_rate * SEVERITY_WEIGHT[Severity(o.rule.severity)] for o in scored
    ) / total_weight


def score_dataset(
    outcomes: list[RuleOutcome],
    *,
    tenant: str,
    dataset: str,
    layer: str,
    rows_total: int,
    rows_failing: int,
    promotion_threshold: float,
) -> QualityScore:
    by_dimension: dict[Dimension, list[RuleOutcome]] = {}
    for outcome in outcomes:
        by_dimension.setdefault(Dimension(outcome.rule.dimension), []).append(outcome)

    dimensions = {
        dimension: weighted_pass_rate(group) for dimension, group in by_dimension.items()
    }
    for dimension in Dimension:
        dimensions.setdefault(dimension, None)

    # The overall score is the weighted mean over *rules*, not the mean of the
    # dimension scores. Averaging dimensions would give a single referential rule
    # the same influence as twenty completeness rules, which overstates whichever
    # dimension happens to be sparsest.
    overall = weighted_pass_rate(outcomes)

    return QualityScore(
        tenant=tenant,
        dataset=dataset,
        layer=layer,
        overall=overall if overall is not None else 1.0,
        dimensions=dimensions,
        rows_total=rows_total,
        rows_failing=rows_failing,
        rules_evaluated=len([o for o in outcomes if not o.vacuous]),
        rules_failed=len([o for o in outcomes if o.rows_failed > 0]),
        vacuous_rules=len([o for o in outcomes if o.vacuous]),
        promotion_blocked=(overall or 1.0) < promotion_threshold,
    )
