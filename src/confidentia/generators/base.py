"""Foundations for the synthetic data generators.

Three properties matter more than realism here, because the experiments depend on
them:

**Determinism.** The same seed must produce byte-identical output. Experiment E3
compares incremental against full reprocessing of the *same* data; E8 replays a
dataset under a drifted schema. Neither is meaningful if the generator wanders.
Seeds are derived hierarchically through ``numpy.random.SeedSequence``, so chunk 7
of the encounter dataset is reproducible without generating chunks 0-6 first.

**Bounded memory.** The platform must generate 5M+ rows on a 4 GB WSL instance, so
generation is chunked and streamed to the writer. Nothing accumulates a full
dataset in memory, and no generator returns a list.

**Ground truth.** Every defect the generator injects is recorded in a manifest.
That turns Stage 7 from "the quality layer flagged some rows" into a measurable
claim: precision and recall per check family against a known answer key. Without
it, a data quality framework can only be demonstrated, not evaluated.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Protocol

import numpy as np
import pandas as pd

# Chunk size is a memory/throughput trade-off. At ~50 columns of mixed types,
# 100k rows is roughly 40-80 MB in pandas -- comfortably inside a 4 GB budget while
# still large enough that per-chunk overhead is negligible.
DEFAULT_CHUNK_ROWS = 100_000

# Fixed epoch so that "days since" arithmetic is stable across runs and machines.
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class Tenant(StrEnum):
    HEALTHCARE = "healthcare"
    FINANCIAL = "financial"


@dataclass(frozen=True)
class GeneratorConfig:
    """Everything that determines what a generator produces.

    Frozen because a generator's output must be a pure function of its config; a
    mutated config mid-run would silently break reproducibility.
    """

    rows: int
    seed: int = 20260808
    chunk_rows: int = DEFAULT_CHUNK_ROWS
    start_date: date = date(2026, 1, 1)
    days: int = 180

    # Fraction of records carrying at least one injected defect. 0.0 produces a
    # clean dataset, which is the control arm for the data quality experiments.
    defect_rate: float = 0.06

    # Fraction of events whose ingest timestamp lags the event timestamp beyond
    # the streaming watermark. Drives the late-arriving-data path.
    late_arrival_rate: float = 0.02
    max_late_hours: int = 72

    # Emit this schema version. Later versions add, rename, widen or drop columns
    # so that the schema-evolution experiment has real inputs.
    schema_version: int = 1

    # Only meaningful for CSV/JSON output, where a column's physical type is not
    # fixed by the format. Set by the writer, not by the caller.
    enable_type_drift: bool = False

    def __post_init__(self) -> None:
        if self.rows <= 0:
            raise ValueError("rows must be positive")
        if not 0.0 <= self.defect_rate <= 1.0:
            raise ValueError("defect_rate must be a fraction")
        if not 0.0 <= self.late_arrival_rate <= 1.0:
            raise ValueError("late_arrival_rate must be a fraction")
        if self.schema_version not in (1, 2, 3):
            raise ValueError("schema_version must be 1, 2 or 3")

    @property
    def chunk_count(self) -> int:
        return (self.rows + self.chunk_rows - 1) // self.chunk_rows

    def chunk_sizes(self) -> Iterator[int]:
        remaining = self.rows
        while remaining > 0:
            size = min(self.chunk_rows, remaining)
            yield size
            remaining -= size


def derive_rng(seed: int, *path: str | int) -> np.random.Generator:
    """Deterministically derive an independent generator for a named sub-stream.

    ``derive_rng(seed, "encounter", 7)`` always yields the same generator, and is
    statistically independent of ``derive_rng(seed, "encounter", 8)``. This is what
    makes chunk 7 reproducible in isolation -- important because a failed run at
    chunk 40 of 50 should be resumable, and because the writer may parallelise.
    """
    # SeedSequence mixes arbitrary integers; map string components through a stable
    # hash rather than Python's, which is salted per process.
    entropy: list[int] = [seed]
    for part in path:
        if isinstance(part, int):
            entropy.append(part)
        else:
            entropy.append(int.from_bytes(part.encode("utf-8")[:8].ljust(8, b"\0"), "big"))
    return np.random.default_rng(np.random.SeedSequence(entropy))


@dataclass
class Chunk:
    """One unit of generated output plus the defects injected into it."""

    dataset: str
    tenant: Tenant
    index: int
    frame: pd.DataFrame
    defects: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())

    @property
    def rows(self) -> int:
        return len(self.frame)


class Generator(Protocol):
    """A dataset generator streams chunks; it never materialises the whole thing."""

    dataset: str
    tenant: Tenant

    def generate(self, config: GeneratorConfig) -> Iterator[Chunk]: ...


# ---------------------------------------------------------------- distributions


def lognormal_amounts(
    rng: np.random.Generator, size: int, *, median: float, sigma: float, cap: float
) -> np.ndarray:
    """Monetary amounts.

    Log-normal rather than uniform because real billed amounts and transaction
    values are strongly right-skewed: many small values, a long thin tail of large
    ones. A uniform distribution would make outlier detection trivially easy and
    the anomaly experiments meaningless.
    """
    values = rng.lognormal(mean=np.log(median), sigma=sigma, size=size)
    return np.round(np.minimum(values, cap), 2)


def zipf_choice(rng: np.random.Generator, n_items: int, size: int, *, a: float = 1.3) -> np.ndarray:
    """Pick from ``n_items`` with a power-law preference for lower indices.

    Encounters per patient, transactions per account and merchant popularity are
    all heavy-tailed: a small number of entities account for a large share of
    activity. Uniform assignment would produce unrealistically flat aggregates and
    would hide the partition-skew effects that experiment E2 is meant to surface.
    """
    draws = rng.zipf(a, size=size)
    return (draws - 1) % n_items


def diurnal_offsets(rng: np.random.Generator, size: int, *, days: int) -> np.ndarray:
    """Seconds-from-start timestamps following a daily activity cycle.

    Transactions peak around midday and early evening and fall away overnight.
    Modelled by rejection-sampling a two-peak intensity curve. Without this the
    hourly aggregates in Gold are flat and the time-series dashboards show nothing
    a human would recognise.
    """
    accepted = np.empty(0, dtype=np.float64)
    # Rejection sampling; the intensity curve peaks at ~1.0 so acceptance is high.
    while accepted.size < size:
        need = int((size - accepted.size) * 1.6) + 64
        candidate = rng.uniform(0.0, days * 86400.0, size=need)
        hour = (candidate % 86400.0) / 3600.0
        intensity = (
            0.15
            + 0.55 * np.exp(-0.5 * ((hour - 12.5) / 3.0) ** 2)
            + 0.40 * np.exp(-0.5 * ((hour - 19.0) / 2.0) ** 2)
        )
        keep = candidate[rng.uniform(0.0, 1.0, size=need) < intensity]
        accepted = np.concatenate([accepted, keep])
    return np.sort(accepted[:size])


def weekday_weights(timestamps: np.ndarray) -> np.ndarray:
    """Multiplier reflecting lower weekend activity, for volume shaping."""
    day_of_week = ((timestamps // 86400).astype(np.int64) + 3) % 7  # 2026-01-01 is a Thursday
    return np.where(day_of_week >= 5, 0.6, 1.0)


def apply_late_arrival(
    rng: np.random.Generator,
    event_ts: np.ndarray,
    *,
    late_rate: float,
    max_late_hours: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Split event time from ingest time.

    Most records land within seconds; a configurable minority arrive hours later,
    which is what a watermark has to tolerate. Returns ``(ingest_ts, is_late)``;
    ``is_late`` feeds the ground-truth manifest so the streaming layer's handling of
    late data can be scored rather than eyeballed.
    """
    size = event_ts.size
    # Normal path: a short, right-skewed pipeline delay.
    delay = rng.exponential(scale=20.0, size=size)

    is_late = rng.uniform(0.0, 1.0, size=size) < late_rate
    if is_late.any():
        late_delay = rng.uniform(1800.0, max_late_hours * 3600.0, size=int(is_late.sum()))
        delay[is_late] = late_delay
    return event_ts + delay, is_late


def to_timestamps(seconds_from_start: np.ndarray, start: date) -> pd.Series:
    base = pd.Timestamp(start, tz="UTC")
    return base + pd.to_timedelta(np.round(seconds_from_start).astype(np.int64), unit="s")
