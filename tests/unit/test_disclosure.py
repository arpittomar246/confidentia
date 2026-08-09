"""Tests for disclosure control policy and the mart registry.

The Spark-dependent suppression logic is exercised end-to-end by the Gold build;
these tests cover the invariants that must hold *before* any data is touched — a
mart that ships without a policy, or with a policy that protects the wrong column,
would publish unprotected counts and nothing would fail.
"""

from __future__ import annotations

import pytest

from confidentia.pipelines.gold.disclosure import DisclosurePolicy, DisclosureReport
from confidentia.pipelines.gold.marts import MARTS, get_mart, marts_for

pytestmark = pytest.mark.unit


class TestDisclosurePolicy:
    def test_defaults_come_from_settings(self) -> None:
        policy = DisclosurePolicy.from_settings()
        assert policy.min_cell_count >= 2
        assert policy.contributor_column == "distinct_entities"

    def test_overrides_apply(self) -> None:
        policy = DisclosurePolicy.from_settings(min_cell_count=25)
        assert policy.min_cell_count == 25

    def test_dp_is_off_by_default(self) -> None:
        # The epsilon accounting here is per-release, not per-analyst, so it does
        # not provide the guarantee differential privacy is usually taken to mean.
        assert DisclosurePolicy.from_settings().differential_privacy is False


class TestDisclosureReport:
    def test_rate_is_zero_when_nothing_published(self) -> None:
        assert DisclosureReport(dataset="x").suppression_rate == 0.0

    def test_rate_counts_both_kinds(self) -> None:
        report = DisclosureReport(
            dataset="x", total_cells=100, primary_suppressed=8, complementary_suppressed=2
        )
        assert report.suppressed == 10
        assert report.suppression_rate == pytest.approx(0.10)


class TestMartRegistry:
    def test_every_mart_declares_a_policy(self) -> None:
        for mart in MARTS:
            assert mart.policy is not None
            assert mart.policy.min_cell_count >= 2

    def test_every_mart_protects_its_measures(self) -> None:
        # A policy with no measure columns marks cells suppressed but blanks
        # nothing, publishing the values it claims to withhold.
        for mart in MARTS:
            assert mart.policy.measure_columns, f"{mart.name} suppresses nothing"

    def test_every_mart_can_do_complementary_suppression(self) -> None:
        # Without row groups, primary suppression alone leaves values recoverable
        # by subtraction from a published row total.
        for mart in MARTS:
            assert mart.policy.row_group_columns, f"{mart.name} has no row grouping"

    def test_contributor_column_is_a_distinct_entity_count(self) -> None:
        # Counting rows would understate identifiability: one patient with forty
        # encounters is one person, not forty.
        for mart in MARTS:
            assert mart.policy.contributor_column == "distinct_entities"

    def test_count_columns_are_declared_measures(self) -> None:
        for mart in MARTS:
            for column in mart.count_columns:
                assert column in mart.policy.measure_columns, (
                    f"{mart.name}.{column} would receive DP noise but is not "
                    "suppressed when the cell is small"
                )

    def test_both_tenants_have_marts(self) -> None:
        assert marts_for("healthcare")
        assert marts_for("financial")

    def test_mart_names_are_unique(self) -> None:
        names = [m.name for m in MARTS]
        assert len(names) == len(set(names))

    def test_lookup_by_name(self) -> None:
        assert get_mart("mart_fraud_by_hour").tenant == "financial"
        with pytest.raises(KeyError):
            get_mart("mart_does_not_exist")


class TestGoldColumnClassification:
    def test_partition_and_grouping_columns_stay_plaintext(self) -> None:
        from confidentia.pipelines.gold.writer import GOLD_PLAINTEXT_COLUMNS

        # Encrypting these would defeat partition pruning and group-by pushdown.
        for column in ("event_date", "date_sk", "year_month", "event_hour"):
            assert column in GOLD_PLAINTEXT_COLUMNS

    def test_no_identifier_is_allow_listed_as_plaintext(self) -> None:
        from confidentia.pipelines.gold.writer import GOLD_PLAINTEXT_COLUMNS

        forbidden = ("mrn", "ssn", "email", "phone", "patient_token", "account_number")
        assert not (set(forbidden) & GOLD_PLAINTEXT_COLUMNS)
