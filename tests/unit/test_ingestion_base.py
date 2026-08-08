"""Tests for the ingestion framework primitives (no database required).

The retryable/terminal split is the important one. A checksum mismatch that gets
retried until it "succeeds" would hide corruption, which is strictly worse than
failing the run.
"""

from __future__ import annotations

import uuid

import pandas as pd
import pytest

from confidentia.ingestion.base import (
    ENVELOPE_BATCH,
    ENVELOPE_COLUMNS,
    ENVELOPE_ORDINAL,
    ENVELOPE_RUN_ID,
    RetryableError,
    RetryPolicy,
    TerminalError,
    add_envelope,
    iter_frame_chunks,
    with_retry,
)

pytestmark = pytest.mark.unit


class TestRetryPolicy:
    def test_backoff_grows_and_is_capped(self) -> None:
        policy = RetryPolicy(base_seconds=1.0, multiplier=2.0, max_seconds=8.0)
        # Full jitter means each delay is a draw from [0, backoff], so the ceiling
        # is what is asserted rather than the exact value.
        assert all(0.0 <= policy.delay(1) <= 1.0 for _ in range(50))
        assert all(0.0 <= policy.delay(3) <= 4.0 for _ in range(50))
        assert all(0.0 <= policy.delay(9) <= 8.0 for _ in range(50))

    def test_jitter_actually_varies(self) -> None:
        # Unjittered clients reconverge on the same instant when a source recovers
        # and knock it over again.
        policy = RetryPolicy(base_seconds=4.0)
        assert len({round(policy.delay(3), 4) for _ in range(30)}) > 5


class TestWithRetry:
    def test_returns_on_first_success(self) -> None:
        calls = []
        result = with_retry(lambda: calls.append(1) or "ok", description="test")
        assert result == "ok"
        assert len(calls) == 1

    def test_retries_then_succeeds(self) -> None:
        attempts = {"n": 0}

        def flaky() -> str:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RetryableError("503")
            return "ok"

        result = with_retry(
            flaky, policy=RetryPolicy(max_attempts=5, base_seconds=0.001), description="flaky"
        )
        assert result == "ok"
        assert attempts["n"] == 3

    def test_terminal_error_is_never_retried(self) -> None:
        attempts = {"n": 0}

        def corrupt() -> None:
            attempts["n"] += 1
            raise TerminalError("checksum mismatch")

        with pytest.raises(TerminalError):
            with_retry(
                corrupt, policy=RetryPolicy(max_attempts=5, base_seconds=0.001), description="x"
            )
        # One attempt only. Retrying corruption until it passes would mask it.
        assert attempts["n"] == 1

    def test_exhausted_retries_raise(self) -> None:
        def always_fails() -> None:
            raise RetryableError("timeout")

        with pytest.raises(RetryableError, match="after 3 attempts"):
            with_retry(
                always_fails,
                policy=RetryPolicy(max_attempts=3, base_seconds=0.001),
                description="always",
            )

    def test_attempt_callback_records_each_try(self) -> None:
        seen: list[tuple[int, bool]] = []
        attempts = {"n": 0}

        def flaky() -> str:
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise RetryableError("boom")
            return "ok"

        with_retry(
            flaky,
            policy=RetryPolicy(max_attempts=3, base_seconds=0.001),
            description="cb",
            on_attempt=lambda n, exc: seen.append((n, exc is not None)),
        )
        assert seen == [(1, True), (2, False)]


class TestEnvelope:
    def _frame(self) -> pd.DataFrame:
        return pd.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]})

    def test_envelope_columns_are_added(self) -> None:
        run_id = uuid.uuid4()
        out = add_envelope(
            self._frame(),
            run_id=run_id,
            batch_digest="abc123",
            source_file="part-00000.parquet",
            source_system="EHR",
        )
        for column in ENVELOPE_COLUMNS:
            assert column in out.columns
        assert (out[ENVELOPE_RUN_ID] == str(run_id)).all()
        assert (out[ENVELOPE_BATCH] == "abc123").all()

    def test_ordinals_are_contiguous_and_offset(self) -> None:
        # Ordinals must continue across parts, or record-level provenance within a
        # batch becomes ambiguous.
        out = add_envelope(
            self._frame(),
            run_id=uuid.uuid4(),
            batch_digest="d",
            source_file="f",
            source_system="s",
            ordinal_offset=100,
        )
        assert list(out[ENVELOPE_ORDINAL]) == [100, 101, 102]

    def test_source_columns_are_preserved(self) -> None:
        frame = self._frame()
        out = add_envelope(
            frame, run_id=uuid.uuid4(), batch_digest="d", source_file="f", source_system="s"
        )
        pd.testing.assert_frame_equal(out[frame.columns], frame)

    def test_envelope_names_cannot_collide_with_source_fields(self) -> None:
        # Underscore prefix is the guard; a source column called "run_id" is
        # plausible, "_ingest_run_id" is not.
        assert all(name.startswith("_") for name in ENVELOPE_COLUMNS)


def test_chunking_covers_every_row_exactly_once() -> None:
    frame = pd.DataFrame({"i": range(2_500)})
    chunks = list(iter_frame_chunks(frame, 1_000))
    assert [len(c) for c in chunks] == [1_000, 1_000, 500]
    assert pd.concat(chunks)["i"].tolist() == list(range(2_500))
