"""Tests for the streaming logic that does not need a broker.

The watermark policy is the piece worth pinning. Structured Streaming drops
records beyond the watermark from stateful operations **silently**, so a boundary
that is subtly wrong loses real data and reports nothing; the loss shows up much
later as a Bronze row count that does not match the topic. Keeping the arithmetic
in plain Python is what makes it assertable at all.

The payload schema is the other: it is derived from the contract rather than
written out, and the property that matters is that it excludes the ingest envelope
— a source filling in `_ingest_run_id` would be fiction.
"""

from __future__ import annotations

import datetime as dt

import pytest

from confidentia.catalog.contracts import get_contract
from confidentia.streaming.consumer import SOURCE_SYSTEM, payload_schema
from confidentia.streaming.watermark import Lateness, WatermarkPolicy

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.UTC)
POLICY = WatermarkPolicy(delay_minutes=10)


class TestWatermarkPolicy:
    def test_boundary_trails_the_newest_event_not_wall_clock(self) -> None:
        # Spark's watermark follows the maximum observed event time. A stream that
        # stalls must not advance its watermark, or the backlog that follows the
        # quiet period is discarded on arrival.
        assert POLICY.boundary(NOW) == NOW - dt.timedelta(minutes=10)

    def test_a_current_event_is_on_time(self) -> None:
        assert POLICY.classify(NOW, NOW) is Lateness.ON_TIME

    def test_an_event_inside_the_window_is_late_but_accepted(self) -> None:
        event = NOW - dt.timedelta(minutes=5)
        assert POLICY.classify(event, NOW) is Lateness.LATE
        assert POLICY.would_drop(event, NOW) is False

    def test_an_event_beyond_the_window_is_dropped(self) -> None:
        event = NOW - dt.timedelta(minutes=11)
        assert POLICY.classify(event, NOW) is Lateness.DROPPED
        assert POLICY.would_drop(event, NOW) is True

    def test_exactly_on_the_boundary_is_accepted(self) -> None:
        # Spark's comparison is inclusive at the boundary; an off-by-one here would
        # discard a record the engine would have kept, and the two would disagree.
        assert POLICY.classify(NOW - POLICY.delay, NOW) is Lateness.LATE

    def test_an_event_ahead_of_the_maximum_is_on_time(self) -> None:
        # A clock-skewed source can produce an event newer than anything seen. It
        # is not late by any definition, and must not be classified as dropped.
        assert POLICY.classify(NOW + dt.timedelta(minutes=5), NOW) is Lateness.ON_TIME

    def test_a_chargeback_arriving_days_later_is_dropped(self) -> None:
        """The financial tenant's central problem, stated as a test.

        A chargeback confirms fraud 1 to 45 days after the transaction, which no
        watermark that keeps state bounded could accept. That is not a watermark to
        widen -- it is the case that belongs in the batch restatement path, and
        this asserts the boundary is drawn where that reasoning says it is.
        """
        assert POLICY.would_drop(NOW - dt.timedelta(days=1), NOW) is True
        assert POLICY.would_drop(NOW - dt.timedelta(days=45), NOW) is True

    def test_spark_delay_string_is_derived_from_the_same_number(self) -> None:
        # The consumer configures Spark from this, so the value in the config and
        # the value in the report cannot diverge.
        assert WatermarkPolicy(delay_minutes=30).spark_delay == "30 minutes"

    def test_policy_comes_from_settings(self) -> None:
        assert WatermarkPolicy.from_settings().delay_minutes > 0


class TestPayloadSchema:
    def test_excludes_the_ingest_envelope(self) -> None:
        # The producer is a source. It does not know about this platform's ingest
        # metadata, and a topic carrying `_ingest_run_id` would be fiction.
        schema = payload_schema(get_contract("financial", "transaction"))
        for envelope_column in (
            "_ingest_run_id", "_batch_digest", "_source_file",
            "_source_system", "_ingested_at", "_record_ordinal",
        ):
            assert envelope_column not in schema
        assert "ingest_date" not in schema

    def test_includes_the_business_columns(self) -> None:
        contract = get_contract("financial", "transaction")
        schema = payload_schema(contract)
        for column in ("transaction_id", "account_id", "amount", "currency"):
            assert f"`{column}`" in schema

    def test_is_derived_from_the_contract(self) -> None:
        # Adding a column to the contract must not require remembering to add it
        # here, so every non-envelope contract column appears.
        contract = get_contract("financial", "transaction")
        expected = [
            c.name for c in contract.columns
            if not c.name.startswith("_") and c.name != "ingest_date"
        ]
        schema = payload_schema(contract)
        assert len(schema.split(",")) == len(expected)


def test_source_system_marks_the_stream_as_a_fourth_source() -> None:
    # ADR 0014: streaming shares Bronze with batch and is distinguished by
    # `_source_system`, which is also what makes Stage 7c's per-source uniqueness
    # treat a redelivery correctly.
    assert SOURCE_SYSTEM == "kafka"
