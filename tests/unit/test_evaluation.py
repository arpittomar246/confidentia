"""Tests for the Stage 7b evaluation harness.

The properties worth pinning are the ones that would make the headline numbers
*flattering* rather than merely wrong, because a flattering measurement is the one
nobody questions:

* loose matching would inflate precision — a rule firing on the wrong column must
  not be credited with whatever defect that record happened to carry;
* counting rows instead of claims would push recall above 1.0 on the two datasets
  that carry duplicates or a double delivery;
* scoring the platform-only families would count correct detections as errors;
* reporting an undefined ratio as 0.0 would invent a failure that did not happen.

Everything here runs on synthetic fixtures. No Spark, no Postgres, no lake.
"""

from __future__ import annotations

import pandas as pd
import pytest

from confidentia.evaluation.ground_truth import GroundTruthMissingError, load_claims
from confidentia.evaluation.scoring import (
    MATCHING_CRITERION,
    NON_DEFECT_TYPES,
    UNSCOREABLE_FAMILIES,
    Claim,
    score_claims,
)
from confidentia.generators.defects import MANIFEST_COLUMNS
from confidentia.generators.writers import DEFECTS_NAME

pytestmark = pytest.mark.unit


def score(detected, truth, **kwargs):
    return score_claims(
        detected, truth, tenant="healthcare", dataset="encounter", layer="bronze", **kwargs
    )


def family(result, name: str):
    return next(s for s in result.families if s.family == name)


class TestMatchingCriterion:
    def test_exact_triple_is_a_true_positive(self) -> None:
        claim = Claim("E1", "encounter_type", "null_violation")
        result = score([claim], [claim])
        assert (result.overall.true_positives, result.overall.false_positives) == (1, 0)
        assert result.overall.false_negatives == 0
        assert result.overall.precision == 1.0
        assert result.overall.recall == 1.0

    def test_right_record_wrong_field_is_not_a_true_positive(self) -> None:
        # The whole reason detections are recorded at field level. Under loose,
        # record-only matching this would score as a perfect hit.
        detected = [Claim("E1", "disposition", "null_violation")]
        truth = [Claim("E1", "encounter_type", "null_violation")]
        result = score(detected, truth)
        assert result.overall.true_positives == 0
        assert result.overall.false_positives == 1
        assert result.overall.false_negatives == 1

    def test_right_record_and_field_wrong_family_is_not_a_true_positive(self) -> None:
        detected = [Claim("E1", "encounter_type", "case_inconsistency")]
        truth = [Claim("E1", "encounter_type", "null_violation")]
        result = score(detected, truth)
        assert result.overall.true_positives == 0
        assert family(result, "case_inconsistency").false_positives == 1
        assert family(result, "null_violation").false_negatives == 1

    def test_criterion_is_recorded_on_the_result(self) -> None:
        # Stored per row, so a future change of criterion cannot be mistaken for a
        # change in the platform's accuracy.
        assert score([], [Claim("E1", "f", "null_violation")]).matching_criterion == (
            MATCHING_CRITERION
        )


class TestClaimsNotRows:
    def test_repeated_detections_of_one_defect_count_once(self) -> None:
        """Bronze `encounter` holds a double delivery, so every key appears twice.

        Counting detection rows would give recall 2.0 here.
        """
        claim = Claim("E1", "encounter_type", "null_violation")
        result = score([claim, claim, claim], [claim])
        assert result.overall.true_positives == 1
        assert result.overall.recall == 1.0
        assert result.overall.detected_claims == 1

    def test_duplicate_record_both_copies_score_as_one_claim(self) -> None:
        """`duplicate_record` appends a near-copy; the rule flags both rows.

        The manifest records the corruption once, so the claim must too.
        """
        claim = Claim("E1", "encounter_id", "duplicate_record")
        result = score([claim, claim], [claim])
        assert family(result, "duplicate_record").true_positives == 1
        assert family(result, "duplicate_record").false_positives == 0

    def test_overlapping_defects_on_one_field_stay_separate(self) -> None:
        # A record nulled and then format-corrupted carries two claims on the same
        # field. Joining on (record, field) alone would collapse them into one.
        truth = [
            Claim("E1", "procedure_code", "null_violation"),
            Claim("E1", "procedure_code", "format_violation"),
        ]
        result = score([truth[0]], truth)
        assert result.overall.true_positives == 1
        assert result.overall.false_negatives == 1


class TestUnscoreableFamilies:
    def test_platform_only_detections_are_not_false_positives(self) -> None:
        # `future_event` and `late_arrival` are never injected. Scoring them would
        # report precision 0.0 for rules that may be working perfectly.
        detected = [
            Claim("E1", "encounter_type", "null_violation"),
            Claim("E2", "event_ts", "future_event"),
            Claim("E3", "ingest_ts", "late_arrival"),
        ]
        truth = [Claim("E1", "encounter_type", "null_violation")]
        result = score(detected, truth)
        assert result.overall.precision == 1.0
        assert result.overall.false_positives == 0
        assert {s.family for s in result.unscoreable} == UNSCOREABLE_FAMILIES
        assert all(s.scoreable is False for s in result.unscoreable)

    def test_unscoreable_detections_are_still_counted(self) -> None:
        # Excluded from the metrics, never from the report.
        result = score([Claim("E2", "event_ts", "future_event")], [])
        reported = next(s for s in result.unscoreable if s.family == "future_event")
        assert reported.detected_claims == 1


class TestUndefinedIsNotZero:
    def test_recall_is_undefined_when_nothing_was_injected(self) -> None:
        # `whitespace_padding` is deliberately absent from the transaction defect
        # plan. Reporting recall 0.0 would read as a detection failure.
        result = score([Claim("T1", "channel", "whitespace_padding")], [])
        score_row = family(result, "whitespace_padding")
        assert score_row.recall is None
        assert score_row.f1 is None
        assert score_row.precision == 0.0
        assert score_row.scoreable is False

    def test_precision_is_undefined_when_nothing_was_detected(self) -> None:
        result = score([], [Claim("E1", "triage_level", "range_violation")])
        score_row = family(result, "range_violation")
        assert score_row.precision is None
        assert score_row.f1 is None
        assert score_row.recall == 0.0
        assert score_row.scoreable is True

    def test_f1_is_zero_when_both_metrics_are_defined_and_zero(self) -> None:
        result = score(
            [Claim("E1", "a", "null_violation")], [Claim("E2", "b", "null_violation")]
        )
        score_row = family(result, "null_violation")
        assert (score_row.precision, score_row.recall) == (0.0, 0.0)
        assert score_row.f1 == 0.0


class TestRuleCoverage:
    def test_missing_rule_is_distinguished_from_a_rule_that_missed(self) -> None:
        # Both score recall 0.0. Only one of them is a detection failure.
        truth = [
            Claim("C1", "allowed_amount", "unit_inconsistency"),
            Claim("C2", "billed_amount", "unit_inconsistency"),
        ]
        result = score([], truth, rule_coverage={("unit_inconsistency", "billed_amount")})
        by_field = {s.field: s for s in result.fields}
        assert by_field["billed_amount"].rule_exists is True
        assert by_field["allowed_amount"].rule_exists is False

    def test_coverage_is_none_when_not_supplied(self) -> None:
        result = score([], [Claim("E1", "f", "null_violation")])
        assert family(result, "null_violation").rule_exists is None


class TestAggregation:
    def test_overall_is_micro_averaged_not_macro(self) -> None:
        """Pooling, not the mean of per-family ratios.

        Macro-averaging would let a family with one claim outvote a family with a
        thousand, which overstates whichever family is rarest.
        """
        truth = [Claim(f"E{i}", "encounter_type", "null_violation") for i in range(100)]
        truth.append(Claim("E999", "triage_level", "range_violation"))
        # Everything found except the single range violation.
        result = score(truth[:100], truth)

        assert family(result, "null_violation").recall == 1.0
        assert family(result, "range_violation").recall == 0.0
        # Macro would be 0.5; micro is 100/101.
        assert result.overall.recall == pytest.approx(100 / 101)

    def test_field_rows_sum_to_the_family_row(self) -> None:
        truth = [
            Claim("E1", "encounter_type", "null_violation"),
            Claim("E2", "primary_diagnosis_code", "null_violation"),
        ]
        result = score([truth[0]], truth)
        rows = [s for s in result.fields if s.family == "null_violation"]
        assert sum(s.true_positives for s in rows) == family(result, "null_violation").true_positives
        assert sum(s.false_negatives for s in rows) == (
            family(result, "null_violation").false_negatives
        )


def write_manifest(root, tenant: str, dataset: str, records: list[dict]) -> None:
    target = root / tenant / dataset
    target.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(records, columns=list(MANIFEST_COLUMNS))
    frame.to_parquet(target / DEFECTS_NAME, index=False)


def defect(**overrides) -> dict:
    base = {
        "dataset": "transaction",
        "tenant": "financial",
        "chunk_index": 0,
        "record_key": "T1",
        "defect_type": "null_violation",
        "severity": "critical",
        "field": "amount",
        "original_value": "10.0",
        "corrupted_value": "None",
    }
    return {**base, **overrides}


class TestGroundTruthLoading:
    def test_latent_fraud_is_excluded(self, tmp_path) -> None:
        # A label, not a defect. It also carries no field, so a claim built from it
        # could never match and would depress recall for an unrelated reason.
        write_manifest(
            tmp_path,
            "financial",
            "transaction",
            [
                defect(),
                defect(record_key="T2", defect_type="latent_fraud", field=None),
            ],
        )
        claims, row_counts = load_claims("financial", "transaction", root=tmp_path)

        assert claims == {Claim("T1", "amount", "null_violation")}
        # The raw counts still report it, so its absence is visible rather than silent.
        assert row_counts["latent_fraud"] == 1
        assert {"latent_fraud"} == NON_DEFECT_TYPES

    def test_record_keys_are_coerced_to_string(self, tmp_path) -> None:
        # The detections table casts business keys to string. A silent int/str
        # mismatch produces a clean-looking zero-recall result.
        write_manifest(tmp_path, "financial", "transaction", [defect(record_key=42)])
        claims, _ = load_claims("financial", "transaction", root=tmp_path)
        assert claims == {Claim("42", "amount", "null_violation")}

    def test_repeated_injections_collapse_to_one_claim(self, tmp_path) -> None:
        write_manifest(
            tmp_path, "financial", "transaction", [defect(chunk_index=0), defect(chunk_index=1)]
        )
        claims, row_counts = load_claims("financial", "transaction", root=tmp_path)
        assert len(claims) == 1
        assert row_counts["null_violation"] == 2

    def test_missing_manifest_raises_rather_than_scoring_zero(self, tmp_path) -> None:
        # An empty answer key would make every detection a false positive and
        # report precision 0.0 -- a fabricated result.
        with pytest.raises(GroundTruthMissingError):
            load_claims("financial", "account", root=tmp_path)
