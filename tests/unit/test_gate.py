"""Tests for the promotion gate's decision policy.

The gate is the first thing in this platform that *stops* something, so the
properties worth pinning are the ones where a plausible implementation quietly
lets data through:

* a missing score reading as "nothing wrong yet" rather than "nothing was checked";
* trusting the `promotion_blocked` flag stored at scoring time, so that changing
  the threshold has no effect until everything is re-scored;
* an override that silently looks like a pass in the audit trail.

`decide` is pure, so all of this is a handful of dicts. The exit-status behaviour
that makes the gate a control rather than a report is asserted separately, by
`scripts/verify_gate.sh`, because it can only be observed from outside the process.
"""

from __future__ import annotations

import pytest

from confidentia.quality.gate import GateDecision, decide

pytestmark = pytest.mark.unit

THRESHOLD = 0.95


def score(overall: float | None, **overrides) -> dict:
    base = {
        "score_id": 1,
        "overall_score": overall,
        "promotion_blocked": False,
        "rules_failed": 2,
        "rules_evaluated": 40,
        "rows_failing": 100,
        "rows_total": 10_000,
    }
    return {**base, **overrides}


def call(score_row, **kwargs) -> GateDecision:
    params = {
        "tenant": "healthcare",
        "dataset": "claim_line",
        "layer": "bronze",
        "threshold": THRESHOLD,
    }
    return decide(score_row, **{**params, **kwargs})


class TestMissingScoreBlocks:
    def test_no_score_blocks(self) -> None:
        # "No score" means the quality run never happened, so nothing has been
        # checked. A gate that opens when its input is absent protects nothing at
        # exactly the moment the pipeline is already broken.
        result = call(None)
        assert result.blocked is True
        assert result.decision == "blocked_no_score"
        assert "nothing has been checked" in (result.reason or "")

    def test_no_score_can_still_be_overridden(self) -> None:
        result = call(None, override=True, override_reason="first run, no history")
        assert result.blocked is False
        assert result.decision == "overridden"
        assert result.overridden is True

    def test_null_overall_score_blocks_rather_than_passing(self) -> None:
        # A NULL score is an absence of evidence, not evidence of quality.
        result = call(score(None))
        assert result.blocked is True
        assert result.decision == "blocked_low_score"


class TestThresholdIsPolicyNotMeasurement:
    def test_decides_against_the_given_threshold(self) -> None:
        assert call(score(0.96)).blocked is False
        assert call(score(0.94)).blocked is True

    def test_ignores_the_stored_promotion_blocked_flag(self) -> None:
        """The flag is the scorer's verdict at scoring time; the gate re-decides.

        Trusting it would mean raising the threshold has no effect until every
        dataset is re-scored, and lowering it leaves stale blocks in place.
        """
        # Stored flag says blocked, but the score clears the current threshold.
        assert call(score(0.99, promotion_blocked=True)).blocked is False
        # Stored flag says fine, but the score is below the current threshold.
        assert call(score(0.10, promotion_blocked=False)).blocked is True

    def test_exactly_at_the_threshold_passes(self) -> None:
        # The comparison is strictly-less-than, so a dataset that exactly meets
        # policy is not blocked by a floating-point tie.
        assert call(score(THRESHOLD)).blocked is False

    def test_threshold_is_recorded_on_the_decision(self) -> None:
        # Stored per decision so a historical block stays interpretable after the
        # policy moves.
        assert call(score(0.9), threshold=0.99).threshold == 0.99


class TestOverride:
    def test_override_unblocks_and_is_marked_as_an_override(self) -> None:
        result = call(score(0.10), override=True, override_reason="backfill")
        assert result.blocked is False
        assert result.overridden is True
        assert result.decision == "overridden"
        assert result.reason == "backfill"

    def test_override_of_a_passing_score_is_not_recorded_as_an_override(self) -> None:
        # Otherwise "how often is this gate bypassed" counts every run that
        # happened to pass while the flag was set, and the number is meaningless.
        result = call(score(0.99), override=True, override_reason="unnecessary")
        assert result.decision == "passed"
        assert result.overridden is False
        assert result.blocked is False


class TestReason:
    def test_a_block_explains_itself_with_numbers(self) -> None:
        result = call(score(0.80, rules_failed=9, rules_evaluated=36))
        assert result.reason is not None
        assert "0.800" in result.reason
        assert "9/36 rules failing" in result.reason

    def test_the_worst_rules_are_included_when_supplied(self) -> None:
        # Saying only "0.80 < 0.95" sends whoever is on call to find out why.
        result = call(score(0.80), detail="worst: null_violation/amount (2,524)")
        assert "null_violation/amount" in (result.reason or "")

    def test_a_pass_needs_no_reason(self) -> None:
        assert call(score(0.99)).reason is None


class TestSummary:
    def test_summary_names_the_verdict(self) -> None:
        assert "BLOCKED" in call(score(0.10)).summary()
        assert "passed" in call(score(0.99)).summary()
        assert "OVERRIDDEN" in call(
            score(0.10), override=True, override_reason="x"
        ).summary()

    def test_summary_survives_a_missing_score(self) -> None:
        # Formatting `None` as a float is the obvious crash here, and it would
        # happen only on the no-score path, which is the rarest and worst moment.
        assert "--" in call(None).summary()
