"""Tests for contract-driven discovery and dataset withdrawal (ADR 0023).

Two behaviours, and the first exists because of a bug the second created. ADR 0022
made an uncontracted landing write fail closed, which was right for a targeted
ingest and wrong for a wildcard sweep: ``ingest file healthcare`` would have aborted
the whole tenant on ``patient_change``, taking ``rebuild_pipeline.sh`` with it. The
fix is that discovery asks the catalog rather than the directory tree.

The withdrawal tests are mostly about what must *not* happen — a contracted dataset
must never be removable this way, and the ledger row must survive the data.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from confidentia.ingestion.file_collector import MANIFEST_NAME, FileCollector
from confidentia.ingestion.withdraw import WithdrawalRefusedError, plan_withdrawal

pytestmark = pytest.mark.unit


def _manifested(root, tenant: str, dataset: str) -> None:
    directory = root / tenant / dataset
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MANIFEST_NAME).write_text(json.dumps({"batch_digest": "a" * 64}))


class TestDiscoveryAsksTheCatalog:
    def test_uncontracted_directories_are_not_offered(self, tmp_path) -> None:
        for dataset in ("patient", "encounter", "patient_change"):
            _manifested(tmp_path, "healthcare", dataset)

        collector = FileCollector(root=tmp_path)
        available = collector.available_datasets("healthcare")

        # patient_change has a manifest — everything the generator writes does —
        # and no contract. A directory-shaped rule swept it into the lake once.
        assert "patient_change" not in available
        assert {"patient", "encounter"} <= set(available)

    def test_a_directory_without_a_manifest_is_not_offered(self, tmp_path) -> None:
        (tmp_path / "healthcare" / "patient").mkdir(parents=True)
        assert FileCollector(root=tmp_path).available_datasets("healthcare") == []

    def test_a_missing_tenant_is_empty_not_an_error(self, tmp_path) -> None:
        assert FileCollector(root=tmp_path).available_datasets("nobody") == []


class TestWithdrawalRefusesContractedData:
    def test_a_contracted_dataset_cannot_be_withdrawn(self, tmp_path) -> None:
        landing = tmp_path / "landing" / "healthcare" / "patient" / "batch=deadbeef"
        landing.mkdir(parents=True)
        pd.DataFrame({"patient_id": ["P1"]}).to_parquet(landing / "part-00000.parquet")

        # Removing a real dataset means re-ingesting or losing replay. One command
        # should not be able to do that and a cleanup by the same flag.
        with pytest.raises(WithdrawalRefusedError, match="has a schema contract"):
            plan_withdrawal("healthcare", "patient", lake_root=tmp_path)

        assert (landing / "part-00000.parquet").exists()

    def test_nothing_landed_is_refused_rather_than_silently_succeeding(self, tmp_path) -> None:
        with pytest.raises(WithdrawalRefusedError, match="nothing landed"):
            plan_withdrawal("healthcare", "patient_change", lake_root=tmp_path)
