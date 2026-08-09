"""Tests for the quality rule catalogue and scoring.

The scoring properties are the ones worth pinning: a score that can be improved by
*adding checks*, or that reports 1.0 for a rule that evaluated nothing, is worse
than no score at all because it will be trusted.
"""

from __future__ import annotations

import pytest

from confidentia.catalog.contracts import all_contracts, get_contract
from confidentia.generators.defects import DefectType
from confidentia.quality.rules import (
    NUMERIC_RANGES,
    PATTERNS,
    SEVERITY_WEIGHT,
    Dimension,
    Rule,
    Severity,
    build_rules,
    rule_catalogue,
)
from confidentia.quality.scoring import RuleOutcome, score_dataset, weighted_pass_rate

pytestmark = pytest.mark.unit


def make_rule(
    severity: Severity = Severity.MAJOR,
    dimension: Dimension = Dimension.VALIDITY,
    family: str = "format_violation",
) -> Rule:
    return Rule(
        rule_id=f"t.{family}.{severity.value}.{dimension.value}",
        dataset="t",
        tenant="healthcare",
        dimension=dimension,
        family=family,
        severity=severity,
        description="test rule",
        predicate=lambda: None,  # never invoked in these tests
    )


class TestRuleCatalogue:
    def test_rules_exist_for_every_contract(self) -> None:
        for contract in all_contracts():
            assert build_rules(contract), f"{contract.dataset} has no rules"

    def test_every_required_column_gets_a_completeness_rule(self) -> None:
        # The contract creates the check. Nobody has to remember to add one.
        contract = get_contract("healthcare", "encounter")
        required = {
            c.name for c in contract.columns
            if not c.nullable and not c.name.startswith("_") and c.name != "ingest_date"
        }
        covered = {
            r.field for r in build_rules(contract)
            if r.family == "null_violation"
        }
        assert required <= covered

    def test_families_match_the_generator_vocabulary(self) -> None:
        # Stage 7b joins detections to the answer key on `family`. A rule using a
        # name the generator never injects can never be scored.
        injected = {d.value for d in DefectType}
        # Families the platform detects but the generator does not inject.
        platform_only = {"future_event", "late_arrival"}
        for rule in rule_catalogue():
            assert rule.family in injected | platform_only, rule.family

    def test_tokenised_columns_lose_format_rules_outside_bronze(self) -> None:
        # `mrn` becomes `tk_mrn_...` in Silver, so checking it against ^MRN\d{9}$
        # is a category error, not a quality finding.
        contract = get_contract("healthcare", "patient")
        bronze = {r.field for r in build_rules(contract, "bronze") if r.family == "format_violation"}
        silver = {r.field for r in build_rules(contract, "silver") if r.family == "format_violation"}
        assert "mrn" in bronze
        assert "mrn" not in silver

    def test_rule_ids_are_unique(self) -> None:
        ids = [r.rule_id for r in rule_catalogue()]
        assert len(ids) == len(set(ids))

    def test_completeness_rules_quarantine(self) -> None:
        contract = get_contract("healthcare", "encounter")
        for rule in build_rules(contract):
            if rule.family == "null_violation":
                assert rule.quarantines

    def test_patterns_compile(self) -> None:
        import re

        for name, pattern in PATTERNS.items():
            re.compile(pattern), f"{name} is not a valid regex"

    def test_numeric_ranges_are_ordered(self) -> None:
        for field, (low, high) in NUMERIC_RANGES.items():
            assert low < high, f"{field} has an inverted range"


class TestScoring:
    def test_vacuous_rule_scores_one_but_is_flagged(self) -> None:
        outcome = RuleOutcome(rule=make_rule(), rows_evaluated=0, rows_failed=0)
        assert outcome.pass_rate == 1.0
        # "100% valid across 0 rows" must be distinguishable from a real pass.
        assert outcome.vacuous

    def test_pass_rate_is_the_complement_of_the_failure_rate(self) -> None:
        outcome = RuleOutcome(rule=make_rule(), rows_evaluated=100, rows_failed=25)
        assert outcome.pass_rate == pytest.approx(0.75)

    def test_severity_weighting_favours_critical_failures(self) -> None:
        # One critical rule failing outweighs one minor rule failing.
        critical = RuleOutcome(make_rule(Severity.CRITICAL), 100, 50)
        minor = RuleOutcome(make_rule(Severity.MINOR), 100, 50)
        assert SEVERITY_WEIGHT[Severity.CRITICAL] > SEVERITY_WEIGHT[Severity.MINOR]
        both = weighted_pass_rate([critical, minor])
        assert both is not None
        # The blended score sits below the unweighted mean because the critical
        # rule pulls harder.
        assert both == pytest.approx(0.5)

    def test_adding_passing_cosmetic_rules_cannot_mask_a_critical_failure(self) -> None:
        critical = RuleOutcome(make_rule(Severity.CRITICAL), 100, 100)  # total failure
        before = weighted_pass_rate([critical])
        after = weighted_pass_rate([critical, *[RuleOutcome(make_rule(Severity.MINOR), 100, 0)
                                                for _ in range(20)]])
        assert before == 0.0
        # It does rise -- but a 10:1 weight means twenty cosmetic passes cannot
        # take a total critical failure anywhere near acceptable.
        assert after is not None and after < 0.7

    def test_vacuous_rules_are_excluded_from_the_score(self) -> None:
        real = RuleOutcome(make_rule(), 100, 10)
        vacuous = RuleOutcome(make_rule(), 0, 0)
        assert weighted_pass_rate([real, vacuous]) == pytest.approx(real.pass_rate)

    def test_empty_outcome_list_scores_none(self) -> None:
        assert weighted_pass_rate([]) is None

    def test_dimensions_are_reported_separately(self) -> None:
        outcomes = [
            RuleOutcome(make_rule(Severity.CRITICAL, Dimension.COMPLETENESS), 100, 0),
            RuleOutcome(make_rule(Severity.MAJOR, Dimension.INTEGRITY), 100, 60),
        ]
        score = score_dataset(
            outcomes, tenant="healthcare", dataset="t", layer="silver",
            rows_total=100, rows_failing=60, promotion_threshold=0.95,
        )
        # The headline must always be decomposable: a good overall score can hide a
        # collapsed dimension.
        assert score.dimensions[Dimension.COMPLETENESS] == pytest.approx(1.0)
        assert score.dimensions[Dimension.INTEGRITY] == pytest.approx(0.4)
        assert score.dimensions[Dimension.VALIDITY] is None

    def test_promotion_blocks_below_threshold(self) -> None:
        outcomes = [RuleOutcome(make_rule(Severity.CRITICAL), 100, 50)]
        score = score_dataset(
            outcomes, tenant="healthcare", dataset="t", layer="silver",
            rows_total=100, rows_failing=50, promotion_threshold=0.95,
        )
        assert score.promotion_blocked

    def test_overall_weights_rules_not_dimensions(self) -> None:
        # Averaging dimension scores would give one sparse dimension the same
        # influence as twenty rules in another.
        outcomes = [
            *[RuleOutcome(make_rule(Severity.MAJOR, Dimension.VALIDITY), 100, 0) for _ in range(9)],
            RuleOutcome(make_rule(Severity.MAJOR, Dimension.INTEGRITY), 100, 100),
        ]
        score = score_dataset(
            outcomes, tenant="healthcare", dataset="t", layer="silver",
            rows_total=100, rows_failing=100, promotion_threshold=0.95,
        )
        assert score.overall == pytest.approx(0.9)  # 9 of 10 rules pass
        # The mean of the two dimension scores would have been 0.5.
        assert score.dimensions[Dimension.INTEGRITY] == pytest.approx(0.0)
