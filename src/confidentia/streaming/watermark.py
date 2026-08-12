"""Event-time watermark policy, expressed so it can be tested without a broker.

Structured Streaming applies watermarks internally — `withWatermark` tells it when
state for a window may be discarded, and records arriving after that boundary are
dropped from stateful operations silently. "Silently" is the problem: a
deduplication watermark set too tight discards real records and reports nothing,
and the loss shows up much later as a Bronze row count that does not match the
topic.

So the same boundary arithmetic lives here, in plain Python, for two uses:

* the consumer configures Spark from this policy rather than from a literal, so
  the number in the config and the number in the report cannot diverge;
* records can be *classified* before they reach a stateful operator, which turns
  a silent drop into a counted one.

This is the financial tenant's central problem in miniature. A chargeback confirms
fraud 1 to 45 days after the transaction, which is far beyond any watermark that
would keep deduplication state bounded. That is not a watermark to widen — it is
the case that belongs in the batch restatement path, and knowing which records
fall outside the window is how that boundary gets drawn honestly.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum


class Lateness(StrEnum):
    ON_TIME = "on_time"
    # Behind the newest event seen, but still inside the watermark: stateful
    # operators will accept it.
    LATE = "late"
    # Beyond the watermark. Spark would drop this from a stateful operation, so a
    # deduplication cannot see it and a windowed aggregate will not include it.
    DROPPED = "dropped"


@dataclass(frozen=True)
class WatermarkPolicy:
    """How far behind the newest event the pipeline still accepts records."""

    delay_minutes: int

    @classmethod
    def from_settings(cls) -> WatermarkPolicy:
        from confidentia.config.settings import get_settings

        return cls(delay_minutes=get_settings().kafka.watermark_minutes)

    @property
    def spark_delay(self) -> str:
        """The string `withWatermark` wants, derived rather than written twice."""
        return f"{self.delay_minutes} minutes"

    @property
    def delay(self) -> dt.timedelta:
        return dt.timedelta(minutes=self.delay_minutes)

    def boundary(self, max_event_ts: dt.datetime) -> dt.datetime:
        """The oldest event time still accepted, given the newest one seen.

        Spark's watermark trails the maximum observed event time, not wall clock.
        A stream that stalls does not advance its watermark, which is what stops a
        quiet period from discarding the backlog that follows it.
        """
        return max_event_ts - self.delay

    def classify(self, event_ts: dt.datetime, max_event_ts: dt.datetime) -> Lateness:
        if event_ts >= max_event_ts:
            return Lateness.ON_TIME
        return (
            Lateness.LATE
            if event_ts >= self.boundary(max_event_ts)
            else Lateness.DROPPED
        )

    def would_drop(self, event_ts: dt.datetime, max_event_ts: dt.datetime) -> bool:
        return self.classify(event_ts, max_event_ts) is Lateness.DROPPED
