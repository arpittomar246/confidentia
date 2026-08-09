"""Persist generated chunks as landing-zone files.

Output layout mirrors what a real source system drops into object storage:

    raw/<tenant>/<dataset>/part-00000.parquet
    raw/<tenant>/<dataset>/_ingest_manifest.json
    raw/<tenant>/<dataset>/_defects.parquet

Three things deserve explanation.

**Files are not partitioned here.** A source system drops files; it does not know
about the lakehouse's partitioning scheme. Bronze partitions on ingest date in
Stage 4. Partitioning at generation time would also be wrong mechanically — each
chunk spans the full date range, so date-partitioning per chunk would produce
thousands of tiny files, which is the small-file problem the platform is supposed
to avoid.

**The ingest manifest carries a SHA-256 per file.** Stage 3 uses it for two
things: verifying the file arrived intact, and idempotency. A batch is identified
by content hash, not filename, so redelivering the same data under a new name is a
no-op rather than a duplicate load.

**Ground truth is written beside the data, never inside it.** ``_defects.parquet``
is the answer key for Stage 7 and the fraud labels for the analytics layer. The
pipeline must never read it; only the evaluation harness may.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from confidentia.common.hashing import sha256_file, sha256_json
from confidentia.common.logging import get_logger
from confidentia.generators.base import Chunk, GeneratorConfig

logger = get_logger(__name__)

Format = Literal["parquet", "csv", "jsonl"]

MANIFEST_NAME = "_ingest_manifest.json"
DEFECTS_NAME = "_defects.parquet"


@dataclass
class FileEntry:
    path: str
    rows: int
    bytes: int
    sha256: str


@dataclass
class WriteResult:
    dataset: str
    tenant: str
    format: str
    rows: int
    defects: int
    files: list[FileEntry] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.bytes for f in self.files)


def _write_frame(frame: pd.DataFrame, path: Path, fmt: Format) -> None:
    if fmt == "parquet":
        # Snappy rather than zstd: it is the Spark/Delta default, so the generated
        # landing files compress the same way the lakehouse will.
        frame.to_parquet(path, index=False, compression="snappy")
    elif fmt == "csv":
        frame.to_csv(path, index=False)
    else:
        frame.to_json(path, orient="records", lines=True, date_format="iso")


def write_dataset(
    chunks: Iterable[Chunk],
    *,
    root: Path,
    config: GeneratorConfig,
    fmt: Format = "parquet",
) -> WriteResult:
    """Stream chunks to disk, accumulating only the manifest in memory."""
    result: WriteResult | None = None
    defect_frames: list[pd.DataFrame] = []
    target: Path | None = None
    suffix = {"parquet": "parquet", "csv": "csv", "jsonl": "jsonl"}[fmt]

    for chunk in chunks:
        if result is None:
            target = root / chunk.tenant.value / chunk.dataset
            target.mkdir(parents=True, exist_ok=True)
            # Remove prior output so a regenerate does not leave orphaned parts
            # that the manifest does not describe.
            for stale in target.glob(f"part-*.{suffix}"):
                stale.unlink()
            result = WriteResult(
                dataset=chunk.dataset, tenant=chunk.tenant.value, format=fmt, rows=0, defects=0
            )

        assert target is not None
        path = target / f"part-{chunk.index:05d}.{suffix}"
        _write_frame(chunk.frame, path, fmt)

        result.files.append(
            FileEntry(
                path=path.name,
                rows=len(chunk.frame),
                bytes=path.stat().st_size,
                sha256=sha256_file(path),
            )
        )
        result.rows += len(chunk.frame)

        if not chunk.defects.empty:
            defect_frames.append(chunk.defects)
            result.defects += len(chunk.defects)

    if result is None or target is None:
        raise ValueError("generator produced no chunks")

    if defect_frames:
        pd.concat(defect_frames, ignore_index=True).to_parquet(
            target / DEFECTS_NAME, index=False, compression="snappy"
        )

    manifest: dict[str, Any] = {
        "dataset": result.dataset,
        "tenant": result.tenant,
        "format": fmt,
        "generated_at": datetime.now(UTC).isoformat(),
        "generator_config": {
            "rows": config.rows,
            "seed": config.seed,
            "chunk_rows": config.chunk_rows,
            "start_date": config.start_date.isoformat(),
            "days": config.days,
            "defect_rate": config.defect_rate,
            "late_arrival_rate": config.late_arrival_rate,
            "schema_version": config.schema_version,
            "enable_type_drift": config.enable_type_drift,
        },
        "total_rows": result.rows,
        "total_bytes": result.total_bytes,
        "injected_defects": result.defects,
        "files": [asdict(f) for f in result.files],
    }
    # The manifest is self-describing: its own digest covers every file digest, so
    # a single value identifies the entire batch for idempotency purposes.
    manifest["batch_digest"] = sha256_json(manifest["files"])
    (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    logger.info(
        "dataset_written",
        dataset=result.dataset,
        tenant=result.tenant,
        rows=result.rows,
        files=len(result.files),
        megabytes=round(result.total_bytes / 1_048_576, 2),
        injected_defects=result.defects,
        batch_digest=manifest["batch_digest"][:16],
    )
    return result


def write_reference_table(
    frame: pd.DataFrame, *, root: Path, tenant: str, dataset: str, fmt: Format = "parquet"
) -> WriteResult:
    """Small dimension tables that fit in one file."""
    target = root / tenant / dataset
    target.mkdir(parents=True, exist_ok=True)
    suffix = {"parquet": "parquet", "csv": "csv", "jsonl": "jsonl"}[fmt]
    path = target / f"part-00000.{suffix}"
    _write_frame(frame, path, fmt)

    entry = FileEntry(
        path=path.name, rows=len(frame), bytes=path.stat().st_size, sha256=sha256_file(path)
    )
    manifest: dict[str, Any] = {
        "dataset": dataset,
        "tenant": tenant,
        "format": fmt,
        "generated_at": datetime.now(UTC).isoformat(),
        "total_rows": len(frame),
        "total_bytes": entry.bytes,
        "injected_defects": 0,
        "files": [asdict(entry)],
    }
    manifest["batch_digest"] = sha256_json(manifest["files"])
    (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    logger.info("reference_written", dataset=dataset, tenant=tenant, rows=len(frame))
    return WriteResult(dataset, tenant, fmt, len(frame), 0, [entry])


def read_manifest(root: Path, tenant: str, dataset: str) -> dict[str, Any]:
    return json.loads((root / tenant / dataset / MANIFEST_NAME).read_text(encoding="utf-8"))


def iter_defects(root: Path, tenant: str, dataset: str) -> Iterator[pd.DataFrame]:
    path = root / tenant / dataset / DEFECTS_NAME
    if path.exists():
        yield pd.read_parquet(path)
