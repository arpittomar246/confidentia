"""Integration tests for ingestion state. Requires PostgreSQL.

These assert the properties the whole ingestion design rests on: the same content
never loads twice, a failed run is recorded as failed rather than left dangling,
and a watermark can be rolled back after a bad advance.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator

import pytest

from confidentia.store import db
from confidentia.store.repositories import (
    BatchDecision,
    BatchRegistry,
    RunStatus,
    RunType,
    WatermarkStore,
    record_reject,
    track_run,
)

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module", autouse=True)
def require_database() -> None:
    if not db.healthcheck():
        pytest.skip("PostgreSQL not reachable; run scripts/install_postgres.sh")


@pytest.fixture(scope="module", autouse=True)
def clean_up_test_batches(require_database: None) -> Iterator[None]:
    """Remove the rows this module writes into the real registry.

    `conftest.py` redirects the *data root* to a temporary directory but not the
    database, because these tests exist to exercise the real Postgres. The
    consequence went unnoticed for days: every run left `healthcare/unit_test`
    batches behind, 170 of them had accumulated by August, and they were only
    spotted when the ingestion chart started plotting `unit_test · test` beside
    the real datasets.

    Scoped to the module and keyed on the dataset name, so a parallel run of
    anything else is untouched.
    """
    yield
    db.execute("DELETE FROM platform.ingest_batch WHERE dataset = %s", ("unit_test",))


@pytest.fixture
def digest() -> str:
    return secrets.token_hex(32)


def _offer(registry: BatchRegistry, digest: str, **overrides) -> BatchDecision:
    params = {
        "batch_digest": digest,
        "source": "test",
        "tenant": "healthcare",
        "dataset": "unit_test",
        "file_count": 1,
        "row_count": 100,
        "bytes_": 4096,
    }
    params.update(overrides)
    return registry.offer(**params)


class TestBatchRegistry:
    def test_first_offer_is_new(self, digest: str) -> None:
        assert _offer(BatchRegistry(), digest) is BatchDecision.NEW

    def test_reoffering_completed_content_is_a_duplicate(self, digest: str) -> None:
        registry = BatchRegistry()
        assert _offer(registry, digest) is BatchDecision.NEW
        registry.mark(digest, "ingested")
        # The core idempotency guarantee: identical content never loads twice,
        # whatever the source calls the file.
        assert _offer(registry, digest) is BatchDecision.DUPLICATE

    def test_reoffering_an_incomplete_batch_is_a_retry(self, digest: str) -> None:
        registry = BatchRegistry()
        _offer(registry, digest)
        # Still 'pending': a previous attempt crashed before marking it ingested.
        assert _offer(registry, digest) is BatchDecision.RETRY

    def test_redeliveries_are_counted_not_discarded(self, digest: str) -> None:
        registry = BatchRegistry()
        _offer(registry, digest)
        registry.mark(digest, "ingested")
        for _ in range(3):
            _offer(registry, digest)
        row = registry.get(digest)
        assert row is not None
        # A rising redelivery rate is a real signal about the upstream system.
        assert row["seen_count"] == 4

    def test_file_records_capture_verification_state(self, digest: str) -> None:
        registry = BatchRegistry()
        _offer(registry, digest)
        registry.record_files(
            digest,
            [
                {
                    "relative_path": "part-00000.parquet",
                    "file_digest": "aa" * 32,
                    "row_count": 50,
                    "bytes": 2048,
                    "verified": True,
                }
            ],
        )
        rows = db.fetch_all(
            "SELECT * FROM platform.ingest_file WHERE batch_digest = %s", (digest,)
        )
        assert len(rows) == 1
        assert rows[0]["verified"] is True


class TestRunTracking:
    def test_successful_run_is_recorded_with_counts(self) -> None:
        with track_run(RunType.INGEST, tenant="healthcare", dataset="t_ok", source="test") as run:
            run.add(read=10, written=9, rejected=1, bytes_read=1024)
            run_id = run.run_id

        row = db.fetch_one("SELECT * FROM platform.run WHERE run_id = %s", (run_id,))
        assert row is not None
        assert row["status"] == RunStatus.SUCCEEDED.value
        assert row["records_read"] == 10
        assert row["records_rejected"] == 1
        assert row["finished_at"] is not None

    def test_failed_run_is_closed_not_left_running(self) -> None:
        run_id = None
        with (
            pytest.raises(ValueError, match="simulated collector failure"),
            track_run(RunType.INGEST, tenant="healthcare", dataset="t_fail") as run,
        ):
            run_id = run.run_id
            run.add(read=5)
            raise ValueError("simulated collector failure")

        row = db.fetch_one("SELECT * FROM platform.run WHERE run_id = %s", (run_id,))
        assert row is not None
        # A dashboard full of perpetually-running ghost runs hides the real ones.
        assert row["status"] == RunStatus.FAILED.value
        assert row["error_type"] == "ValueError"
        assert "simulated collector failure" in row["error_message"]
        assert row["finished_at"] is not None
        # Partial progress before the failure is preserved for diagnosis.
        assert row["records_read"] == 5

    def test_rejects_are_recorded_never_dropped(self) -> None:
        with track_run(RunType.INGEST, tenant="healthcare", dataset="t_rej") as run:
            record_reject(
                run_id=run.run_id,
                source="test",
                tenant="healthcare",
                dataset="t_rej",
                reason_code="checksum_mismatch",
                detail="expected aa, got bb",
                relative_path="part-00000.parquet",
            )
            run_id = run.run_id

        rows = db.fetch_all(
            "SELECT * FROM platform.ingest_reject WHERE run_id = %s", (run_id,)
        )
        assert len(rows) == 1
        assert rows[0]["reason_code"] == "checksum_mismatch"


class TestWatermarks:
    def test_advance_then_read_back(self) -> None:
        store = WatermarkStore()
        dataset = f"wm_{secrets.token_hex(4)}"
        store.advance(
            source="test", tenant="healthcare", dataset=dataset,
            kind="timestamp", value="2026-01-01T00:00:00+00:00",
        )
        assert store.value("test", "healthcare", dataset) == "2026-01-01T00:00:00+00:00"

    def test_previous_value_is_retained_for_rollback(self) -> None:
        store = WatermarkStore()
        dataset = f"wm_{secrets.token_hex(4)}"
        store.advance(source="test", tenant="healthcare", dataset=dataset,
                      kind="timestamp", value="A")
        store.advance(source="test", tenant="healthcare", dataset=dataset,
                      kind="timestamp", value="B")

        row = store.get("test", "healthcare", dataset)
        assert row is not None
        assert row["watermark_value"] == "B"
        assert row["previous_value"] == "A"

        # A bad advance must be recoverable without reconstructing it from logs.
        assert store.rollback("test", "healthcare", dataset) is True
        assert store.value("test", "healthcare", dataset) == "A"

    def test_rollback_without_history_is_a_no_op(self) -> None:
        store = WatermarkStore()
        dataset = f"wm_{secrets.token_hex(4)}"
        store.advance(source="test", tenant="healthcare", dataset=dataset,
                      kind="lsn", value="0/1")
        assert store.rollback("test", "healthcare", dataset) is False

    def test_watermark_kinds_coexist(self) -> None:
        # The four collectors resume on genuinely different things; forcing them
        # into a timestamp column would mean three of them lying.
        store = WatermarkStore()
        suffix = secrets.token_hex(4)
        for kind, value in (("timestamp", "2026-01-01T00:00:00+00:00"),
                            ("lsn", "0/2C92610"),
                            ("offset", "42"),
                            ("sequence", "99887766")):
            dataset = f"wm_{kind}_{suffix}"
            store.advance(source="test", tenant="healthcare", dataset=dataset,
                          kind=kind, value=value)
            row = store.get("test", "healthcare", dataset)
            assert row is not None
            assert row["watermark_kind"] == kind
            assert row["watermark_value"] == value
