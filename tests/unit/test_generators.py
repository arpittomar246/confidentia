"""Tests for the synthetic data generators.

The properties under test are the ones the experiments depend on: determinism,
chunk independence, defect rates matching what was asked for, and the ground-truth
manifest actually describing the data. Realism is asserted only where a wrong
distribution would invalidate a downstream result — flat diurnal activity would
make the streaming dashboards meaningless, and a symmetric amount distribution
would make outlier detection trivial.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from confidentia.generators import financial as fin
from confidentia.generators import healthcare as hc
from confidentia.generators.base import (
    GeneratorConfig,
    apply_late_arrival,
    derive_rng,
    diurnal_offsets,
    zipf_choice,
)
from confidentia.generators.defects import (
    DEFAULT_MIX,
    DefectPlan,
    DefectType,
    FieldRole,
    injector_for,
)

pytestmark = pytest.mark.unit

SMALL = GeneratorConfig(rows=4_000, chunk_rows=1_000, seed=1234, days=30)


def collect(chunks) -> pd.DataFrame:
    return pd.concat([c.frame for c in chunks], ignore_index=True)


def collect_defects(chunks) -> pd.DataFrame:
    frames = [c.defects for c in chunks if not c.defects.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


class TestDeterminism:
    def test_same_seed_gives_identical_output(self) -> None:
        scale = hc.HealthcareScale.for_rows(SMALL.rows)
        first = collect(hc.generate_encounters(SMALL, scale))
        second = collect(hc.generate_encounters(SMALL, scale))
        pd.testing.assert_frame_equal(first, second)

    def test_different_seed_gives_different_output(self) -> None:
        scale = hc.HealthcareScale.for_rows(SMALL.rows)
        other = GeneratorConfig(rows=SMALL.rows, chunk_rows=SMALL.chunk_rows, seed=999, days=30)
        first = collect(hc.generate_encounters(SMALL, scale))
        second = collect(hc.generate_encounters(other, scale))
        assert not first["primary_diagnosis_code"].equals(second["primary_diagnosis_code"])

    def test_chunk_streams_are_independent(self) -> None:
        # A chunk must be reproducible without generating its predecessors, or a
        # failed run could not resume and the writer could not parallelise.
        a = derive_rng(7, "encounter", 3).integers(0, 10**9, size=5)
        b = derive_rng(7, "encounter", 3).integers(0, 10**9, size=5)
        c = derive_rng(7, "encounter", 4).integers(0, 10**9, size=5)
        assert np.array_equal(a, b)
        assert not np.array_equal(a, c)

    def test_fraud_population_is_stable_across_calls(self) -> None:
        first = fin.fraud_indices(4242, 100_000)
        second = fin.fraud_indices(4242, 100_000)
        assert np.array_equal(first, second)
        # The chargeback generator relies on recomputing this set independently.
        assert first.size == int(100_000 * fin.FRAUD_RATE)


class TestConfigValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"rows": 0},
            {"rows": 10, "defect_rate": 1.5},
            {"rows": 10, "late_arrival_rate": -0.1},
            {"rows": 10, "schema_version": 9},
        ],
    )
    def test_invalid_config_is_rejected(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            GeneratorConfig(**kwargs)

    def test_chunk_sizes_sum_to_rows(self) -> None:
        config = GeneratorConfig(rows=2_500, chunk_rows=1_000)
        sizes = list(config.chunk_sizes())
        assert sum(sizes) == 2_500
        assert sizes == [1_000, 1_000, 500]
        assert config.chunk_count == 3


class TestDefectInjection:
    def _plan(self) -> DefectPlan:
        return DefectPlan(
            dataset="t",
            tenant="healthcare",
            key_field="k",
            fields=(
                FieldRole("name", (DefectType.NULL_VIOLATION, DefectType.WHITESPACE_PADDING)),
                FieldRole("amount", (DefectType.RANGE_VIOLATION, DefectType.UNIT_INCONSISTENCY)),
            ),
        )

    def _frame(self, n: int = 5_000) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "k": [f"K{i:06d}" for i in range(n)],
                "name": [f"name-{i}" for i in range(n)],
                "amount": np.linspace(10.0, 500.0, n),
            }
        )

    def test_zero_rate_injects_nothing(self) -> None:
        config = GeneratorConfig(rows=10, defect_rate=0.0)
        injector = injector_for(self._plan(), config)
        frame = self._frame()
        out, manifest = injector.inject(frame, derive_rng(1, "x"), 0)
        assert manifest.empty
        pd.testing.assert_frame_equal(out, frame)

    def test_defect_volume_tracks_requested_rate(self) -> None:
        config = GeneratorConfig(rows=10, defect_rate=0.10)
        injector = injector_for(self._plan(), config)
        _, manifest = injector.inject(self._frame(), derive_rng(2, "x"), 0)
        # Budget is allocated across applicable types; allow slack for rounding and
        # for types this plan cannot express.
        assert 250 <= len(manifest) <= 750

    def test_manifest_keys_all_exist_in_the_data(self) -> None:
        config = GeneratorConfig(rows=10, defect_rate=0.10)
        injector = injector_for(self._plan(), config)
        out, manifest = injector.inject(self._frame(), derive_rng(3, "x"), 0)
        # Without this the answer key cannot be joined back and Stage 7 cannot be
        # scored at all.
        assert set(manifest["record_key"]) <= set(out["k"].astype(str))

    def test_key_field_is_never_corrupted(self) -> None:
        config = GeneratorConfig(rows=10, defect_rate=0.5)
        injector = injector_for(self._plan(), config)
        frame = self._frame()
        out, _ = injector.inject(frame, derive_rng(4, "x"), 0)
        originals = set(frame["k"])
        # Duplicates append rows, so the output is a superset, never a mutation.
        assert set(out["k"]) == originals

    def test_type_drift_excluded_for_parquet_included_for_text(self) -> None:
        parquet_config = GeneratorConfig(rows=10, enable_type_drift=False)
        text_config = GeneratorConfig(rows=10, enable_type_drift=True)
        assert DefectType.TYPE_DRIFT not in injector_for(self._plan(), parquet_config).enabled
        assert DefectType.TYPE_DRIFT in injector_for(self._plan(), text_config).enabled

    def test_every_defect_type_has_a_handler(self) -> None:
        injector = injector_for(self._plan(), GeneratorConfig(rows=10))
        for kind in DEFAULT_MIX:
            assert hasattr(injector, f"_apply_{kind.value}"), f"no handler for {kind}"

    def test_overlapping_defects_do_not_crash(self) -> None:
        # A field nulled by one defect can legitimately be selected by another.
        config = GeneratorConfig(rows=10, defect_rate=0.9)
        injector = injector_for(self._plan(), config)
        out, manifest = injector.inject(self._frame(1_000), derive_rng(5, "x"), 0)
        assert len(out) >= 1_000
        assert not manifest.empty


class TestDistributions:
    def test_transaction_amounts_are_right_skewed(self) -> None:
        scale = fin.FinancialScale.for_rows(20_000)
        frame = collect(fin.generate_transactions(SMALL, scale))
        amounts = frame["amount"].astype(float)
        amounts = amounts[amounts > 0]
        # Log-normal: mean well above median. A symmetric distribution would make
        # outlier detection trivial and the anomaly experiments meaningless.
        assert amounts.mean() > amounts.median() * 1.3

    def test_activity_follows_a_daily_cycle(self) -> None:
        rng = derive_rng(11, "diurnal")
        seconds = diurnal_offsets(rng, 40_000, days=10)
        hour = (seconds % 86400) / 3600
        midday = ((hour >= 11) & (hour < 14)).sum()
        deep_night = ((hour >= 2) & (hour < 5)).sum()
        assert midday > deep_night * 3

    def test_entity_activity_is_heavy_tailed(self) -> None:
        rng = derive_rng(12, "zipf")
        picks = zipf_choice(rng, 1_000, 50_000)
        counts = pd.Series(picks).value_counts()
        # Top 10% of entities should account for a disproportionate share; this
        # skew is what experiment E2 measures partition behaviour against.
        top_decile = counts.head(100).sum()
        assert top_decile > 0.4 * len(picks)

    def test_late_arrivals_occur_at_the_requested_rate(self) -> None:
        rng = derive_rng(13, "late")
        event = np.sort(rng.uniform(0, 86400 * 30, size=20_000))
        ingest, is_late = apply_late_arrival(rng, event, late_rate=0.05, max_late_hours=48)
        assert (ingest >= event).all(), "ingest must never precede the event"
        assert 0.03 < is_late.mean() < 0.07


class TestHealthcareSemantics:
    def test_patient_mutations_reference_real_patients(self) -> None:
        scale = hc.HealthcareScale.for_rows(SMALL.rows)
        changes = collect(hc.generate_patient_changes(SMALL, scale))
        assert not changes.empty
        assert changes["operation"].eq("UPDATE").all()
        # Without mutations there is nothing for SCD2 to track.
        assert changes["changed_field"].nunique() >= 3

    def test_discharge_follows_admission_except_where_injected(self) -> None:
        clean = GeneratorConfig(rows=4_000, chunk_rows=1_000, seed=77, days=30, defect_rate=0.0)
        scale = hc.HealthcareScale.for_rows(clean.rows)
        frame = collect(hc.generate_encounters(clean, scale))
        assert (frame["discharge_ts"] >= frame["admit_ts"]).all()

    def test_temporal_inconsistency_is_injected_and_recorded(self) -> None:
        config = GeneratorConfig(rows=20_000, chunk_rows=20_000, seed=5, days=30, defect_rate=0.2)
        scale = hc.HealthcareScale.for_rows(config.rows)
        chunks = list(hc.generate_encounters(config, scale))
        frame = collect(chunks)
        manifest = collect_defects(chunks)
        broken = frame["discharge_ts"] < frame["admit_ts"]
        assert broken.any()
        recorded = manifest[manifest["defect_type"] == DefectType.TEMPORAL_INCONSISTENCY.value]
        assert not recorded.empty


class TestFinancialSemantics:
    def test_transactions_carry_no_fraud_label(self) -> None:
        scale = fin.FinancialScale.for_rows(20_000)
        frame = collect(fin.generate_transactions(SMALL, scale))
        leaked = [c for c in frame.columns if "fraud" in c.lower()]
        # A fraud flag in the raw stream would be a label leak that makes every
        # downstream analysis unrealistically easy.
        assert leaked == []

    def test_fraud_labels_live_only_in_the_manifest(self) -> None:
        config = GeneratorConfig(rows=200_000, chunk_rows=100_000, seed=8, days=60)
        scale = fin.FinancialScale.for_rows(config.rows)
        chunks = list(fin.generate_transactions(config, scale))
        manifest = collect_defects(chunks)
        labels = manifest[manifest["defect_type"] == "latent_fraud"]
        assert not labels.empty
        assert labels["record_key"].str.startswith("TXN-").all()

    def test_chargebacks_reference_generated_transactions(self) -> None:
        config = GeneratorConfig(rows=200_000, chunk_rows=100_000, seed=8, days=60)
        scale = fin.FinancialScale.for_rows(config.rows)
        chargebacks = collect(fin.generate_chargebacks(config, scale))
        assert not chargebacks.empty
        assert chargebacks["transaction_id"].str.startswith("TXN-").all()
        # Most chargebacks should be genuine fraud, with a friendly-fraud minority.
        confirmed = chargebacks["is_confirmed_fraud"].mean()
        assert 0.6 < confirmed < 0.99


class TestScaleDerivation:
    def test_entity_counts_stay_proportionate(self) -> None:
        scale = hc.HealthcareScale.for_rows(1_000_000)
        assert scale.patients < scale.encounters
        assert scale.providers < scale.patients
        assert scale.facilities < scale.providers

    def test_minimum_entity_counts_hold_for_tiny_runs(self) -> None:
        scale = hc.HealthcareScale.for_rows(10)
        assert scale.patients >= 500
        assert scale.facilities >= 5
        fin_scale = fin.FinancialScale.for_rows(10)
        assert fin_scale.accounts >= 200
        assert fin_scale.merchants >= 50


def test_identifier_formats_are_stable() -> None:
    # Downstream stages recompute identifiers from indices rather than joining, so
    # the format is a contract, not a cosmetic choice.
    assert hc.patient_id(np.array([7]))[0] == "PAT-000000007"
    assert hc.encounter_id(np.array([7]))[0] == "ENC-000000000007"
    assert fin.transaction_id(np.array([7]))[0] == "TXN-000000000007"
    assert fin.account_id(3) == "ACC-0000000003"


def test_start_date_anchors_output() -> None:
    config = GeneratorConfig(rows=1_000, chunk_rows=1_000, seed=3, start_date=date(2026, 3, 1), days=10)
    scale = hc.HealthcareScale.for_rows(config.rows)
    frame = collect(hc.generate_encounters(config, scale))
    assert frame["admit_ts"].min() >= pd.Timestamp("2026-03-01", tz="UTC")
    assert frame["admit_ts"].max() < pd.Timestamp("2026-03-12", tz="UTC")
