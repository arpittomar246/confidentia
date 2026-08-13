"""Measured inputs to the placement plan (C5).

Everything here is observed on this machine, and each result says how. The model in
``model.py`` is the only part that is not, and keeping the two in separate modules
is deliberate: it makes "which of these numbers did we actually measure" a question
you answer by looking at the import list.

Three inputs, three sources:

``row width``
    Uncompressed bytes per row, read out of the **Parquet footer metadata** —
    ``total_uncompressed_size`` summed across column chunks, divided by the row
    count. No Spark, no JVM, no key release, because footers of the landing files
    are unencrypted and that is where the widest, least-processed version of each
    record lives. It is the decompressed *Parquet* size, not the JVM heap
    footprint, and the difference is stated wherever it is used.

``stage duration``
    From ``platform.run_summary``, which the pipeline has been writing since
    Stage 3. Median rather than mean: a handful of runs in this history include a
    cold JVM start measured in tens of seconds, and a mean over them describes an
    event that happens once.

``row count``
    From ``platform.lineage_latest`` where a dataset has been digested, because
    that count is signed. Falls back to ``run_summary`` where it has not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from confidentia.common.logging import get_logger
from confidentia.config.settings import Layer, Settings, get_settings
from confidentia.store import db

logger = get_logger(__name__)

# Encrypted Parquet cannot have its footer read without the key, so widths are
# measured from the layer whose footers are plaintext. That this layer exists at
# all is the boundary violation the planner reports.
WIDTH_SOURCE_LAYER = "landing"


@dataclass(frozen=True)
class RowWidth:
    tenant: str
    dataset: str
    layer: str
    rows: int
    compressed_bytes: int
    uncompressed_bytes: int
    files: int

    @property
    def bytes_per_row(self) -> int:
        """Uncompressed. This is the number the EPC budget must be divided by."""
        return round(self.uncompressed_bytes / self.rows) if self.rows else 0

    @property
    def compressed_bytes_per_row(self) -> int:
        return round(self.compressed_bytes / self.rows) if self.rows else 0

    @property
    def compression_ratio(self) -> float:
        return self.uncompressed_bytes / self.compressed_bytes if self.compressed_bytes else 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "dataset": self.dataset,
            "layer": self.layer,
            "rows": self.rows,
            "files": self.files,
            "bytes_per_row": self.bytes_per_row,
            "compressed_bytes_per_row": self.compressed_bytes_per_row,
            "compression_ratio": round(self.compression_ratio, 2),
            "label": "MEASURED",
        }


def measure_row_widths(settings: Settings | None = None) -> list[RowWidth]:
    """Read Parquet footers under the landing zone and derive uncompressed width.

    Reads metadata only — ``pyarrow.parquet.ParquetFile.metadata`` parses the
    footer without touching a data page, so this stays fast on a 22 MB landing
    zone and would stay fast on a much larger one.
    """
    import pyarrow.parquet as pq

    settings = settings or get_settings()
    root = settings.lake_root / WIDTH_SOURCE_LAYER
    if not root.exists():
        logger.warning("landing_zone_missing", path=str(root))
        return []

    accumulated: dict[tuple[str, str], list[int]] = {}
    for path in sorted(root.rglob("*.parquet")):
        try:
            relative = path.relative_to(root).parts
            tenant, dataset = relative[0], relative[1]
        except (ValueError, IndexError):  # pragma: no cover - defensive
            continue
        try:
            metadata = pq.ParquetFile(path).metadata
        except Exception as exc:  # noqa: BLE001 - one unreadable file must not stop a survey
            logger.warning("parquet_footer_unreadable", path=str(path), error=str(exc))
            continue

        compressed = uncompressed = 0
        for group in range(metadata.num_row_groups):
            row_group = metadata.row_group(group)
            for column in range(row_group.num_columns):
                chunk = row_group.column(column)
                compressed += chunk.total_compressed_size
                uncompressed += chunk.total_uncompressed_size

        totals = accumulated.setdefault((tenant, dataset), [0, 0, 0, 0])
        totals[0] += metadata.num_rows
        totals[1] += compressed
        totals[2] += uncompressed
        totals[3] += 1

    return [
        RowWidth(
            tenant=tenant,
            dataset=dataset,
            layer=WIDTH_SOURCE_LAYER,
            rows=rows,
            compressed_bytes=compressed,
            uncompressed_bytes=uncompressed,
            files=files,
        )
        for (tenant, dataset), (rows, compressed, uncompressed, files) in sorted(
            accumulated.items()
        )
        if rows
    ]


@dataclass(frozen=True)
class StageTiming:
    """Median wall-clock duration of a pipeline stage, from its recorded runs."""

    stage: str
    runs: int
    median_ms: float
    p90_ms: float
    rows_median: int

    @property
    def is_measured(self) -> bool:
        return self.runs > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "runs": self.runs,
            "median_ms": round(self.median_ms, 1),
            "p90_ms": round(self.p90_ms, 1),
            "rows_median": self.rows_median,
            "label": "MEASURED" if self.is_measured else "MODELLED",
        }


@dataclass(frozen=True)
class CostGroupQuery:
    """How a cost group maps onto what ``run_summary`` recorded.

    ``source`` carries the layer for transform runs, which is the only reason
    per-layer timing is available at all — ``run_type`` alone would collapse Silver
    and Gold into one number.

    ``require_written`` exists because two kinds of run share a ``run_type``. A
    Bronze "transform" is either the write, which moves hundreds of thousands of
    rows and takes seconds, or the promotion bookkeeping that marks a batch
    promoted and takes 5 ms. Taking the median across both describes neither, and
    the 5 ms one dominates by count — which produced a modelled slowdown of 25x for
    Bronze, from a 120 ms cold start against a 5 ms "baseline" that was not the
    write at all.
    """

    run_type: str
    source: str | None
    require_written: bool = False


# Keyed by *cost group*, not by stage. The platform records one run per layer, so
# Silver's three steps and Gold's two share a measurement between them. Charging
# each of them the full recorded duration would triple-count Silver and
# double-count Gold; see `planner.py`, which sums over groups.
COST_GROUP_QUERY: dict[str, CostGroupQuery] = {
    "ingest": CostGroupQuery("ingest", None, require_written=True),
    "bronze": CostGroupQuery("transform", "bronze", require_written=True),
    "silver": CostGroupQuery("transform", "silver", require_written=True),
    "gold": CostGroupQuery("transform", "gold", require_written=True),
    "quality": CostGroupQuery("quality", "quality"),
    "lineage": CostGroupQuery("lineage", None),
    "gate": CostGroupQuery("quality", "gate"),
}


def stage_timings() -> dict[str, StageTiming]:
    """Median duration per **cost group** from the recorded run history.

    Median rather than mean: this history includes runs with a cold JVM start
    measured in tens of seconds, and a mean over those describes an event that
    happens once per session rather than the steady state.
    """
    rows = db.fetch_all(
        """
        SELECT run_type::text AS run_type, coalesce(source, '') AS source,
               coalesce(records_written, 0) > 0 AS wrote,
               count(*) AS runs,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY duration_ms) AS median_ms,
               percentile_disc(0.9) WITHIN GROUP (ORDER BY duration_ms) AS p90_ms,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY records_written) AS rows_median
          FROM platform.run_summary
         WHERE status::text = 'succeeded' AND duration_ms IS NOT NULL
         GROUP BY 1, 2, 3
        """
    )

    timings: dict[str, StageTiming] = {}
    for group, query in COST_GROUP_QUERY.items():
        matching = [
            row
            for row in rows
            if row["run_type"] == query.run_type
            and (query.source is None or row["source"] == query.source)
            and (not query.require_written or row["wrote"])
        ]
        if not matching:
            continue
        runs = sum(int(row["runs"]) for row in matching)
        # Weight the medians by run count rather than taking the max across
        # sources: ingestion is recorded per collector, and a single slow CDC
        # collector should not become the ingestion baseline on its own.
        median = sum(float(row["median_ms"]) * int(row["runs"]) for row in matching) / runs
        p90 = max(float(row["p90_ms"]) for row in matching)
        rows_median = max(int(row["rows_median"] or 0) for row in matching)
        timings[group] = StageTiming(
            stage=group, runs=runs, median_ms=median, p90_ms=p90, rows_median=rows_median
        )
    return timings


def record_counts() -> dict[tuple[str, str, str], int]:
    """Rows per (tenant, dataset, layer), preferring the signed lineage count."""
    counts: dict[tuple[str, str, str], int] = {}
    for row in db.fetch_all(
        "SELECT tenant, dataset, layer, record_count FROM platform.lineage_latest"
    ):
        counts[(row["tenant"], row["dataset"], row["layer"])] = int(row["record_count"] or 0)
    return counts


def layer_rows(layer: Layer) -> int:
    """Total rows currently committed at a layer, as last signed."""
    return sum(rows for (_, _, lyr), rows in record_counts().items() if lyr == layer.value)
