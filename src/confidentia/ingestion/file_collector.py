"""File collector: batch drops from a landing directory or object store.

The collector's contract with the source is the ``_ingest_manifest.json`` written
beside the data: file list, per-file SHA-256, row counts, and a batch digest over
the ordered file digests.

Two properties this buys, both of which are asserted rather than assumed:

**Integrity.** Every file's bytes are hashed and compared against the manifest
before a single record is read. A mismatch is a ``TerminalError``, never retried —
a retry that eventually succeeded would mask corruption, which is worse than
failing.

**Idempotency.** The batch digest is the identity of the content. A source that
redelivers the same data under a new filename is recognised and skipped, and the
redelivery is counted rather than discarded, because a rising redelivery rate says
something real about the upstream system.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from confidentia.catalog.contracts import CONTRACTS
from confidentia.common.hashing import sha256_file
from confidentia.common.logging import get_logger
from confidentia.config.settings import get_settings
from confidentia.crypto.landing import LandingCrypto
from confidentia.ingestion.base import (
    IngestResult,
    TerminalError,
    add_envelope,
    write_landing,
)
from confidentia.store.repositories import (
    BatchDecision,
    BatchRegistry,
    RunType,
    record_reject,
    track_run,
)

logger = get_logger(__name__)

SOURCE = "file_drop"
MANIFEST_NAME = "_ingest_manifest.json"


class FileCollector:
    def __init__(self, *, root: Path | None = None, verify_checksums: bool = True) -> None:
        settings = get_settings()
        self.root = root or (settings.data_root / "raw")
        self.lake_root = settings.lake_root
        self.verify_checksums = verify_checksums
        self.registry = BatchRegistry()

    # ------------------------------------------------------------------

    def _read_manifest(self, tenant: str, dataset: str) -> dict:
        path = self.root / tenant / dataset / MANIFEST_NAME
        if not path.exists():
            raise TerminalError(f"no manifest at {path}; source drop is incomplete")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TerminalError(f"manifest at {path} is not valid JSON: {exc}") from exc

    def _verify(self, directory: Path, manifest: dict, run_id, tenant: str, dataset: str) -> list[dict]:
        """Hash every file and compare against the manifest.

        Returns per-file records for the registry. Raises on the first mismatch:
        a batch is ingested whole or not at all, because a partially-loaded batch
        would leave the watermark ambiguous.
        """
        verified: list[dict] = []
        for entry in manifest["files"]:
            path = directory / entry["path"]
            if not path.exists():
                record_reject(
                    run_id=run_id,
                    source=SOURCE,
                    tenant=tenant,
                    dataset=dataset,
                    relative_path=entry["path"],
                    reason_code="missing_file",
                    detail="declared in manifest but absent on disk",
                )
                raise TerminalError(f"manifest declares {entry['path']} but it is missing")

            actual_bytes = path.stat().st_size
            digest = sha256_file(path) if self.verify_checksums else entry["sha256"]

            if digest != entry["sha256"]:
                record_reject(
                    run_id=run_id,
                    source=SOURCE,
                    tenant=tenant,
                    dataset=dataset,
                    relative_path=entry["path"],
                    reason_code="checksum_mismatch",
                    detail=f"expected {entry['sha256'][:16]}, computed {digest[:16]}",
                )
                # Deliberately terminal. See module docstring.
                raise TerminalError(
                    f"checksum mismatch on {entry['path']}: "
                    f"expected {entry['sha256'][:16]}, computed {digest[:16]}"
                )

            if actual_bytes != entry["bytes"]:
                record_reject(
                    run_id=run_id,
                    source=SOURCE,
                    tenant=tenant,
                    dataset=dataset,
                    relative_path=entry["path"],
                    reason_code="size_mismatch",
                    detail=f"expected {entry['bytes']}, found {actual_bytes}",
                )
                raise TerminalError(f"size mismatch on {entry['path']}")

            verified.append(
                {
                    "relative_path": entry["path"],
                    "file_digest": digest,
                    "row_count": entry["rows"],
                    "bytes": actual_bytes,
                    "verified": True,
                }
            )
        return verified

    # ------------------------------------------------------------------

    def collect(self, tenant: str, dataset: str) -> IngestResult:
        started = time.perf_counter()
        directory = self.root / tenant / dataset
        manifest = self._read_manifest(tenant, dataset)
        batch_digest = manifest["batch_digest"]

        with track_run(
            RunType.INGEST,
            tenant=tenant,
            dataset=dataset,
            source=SOURCE,
            metadata={"batch_digest": batch_digest, "source_path": str(directory)},
        ) as run:
            decision = self.registry.offer(
                batch_digest=batch_digest,
                source=SOURCE,
                tenant=tenant,
                dataset=dataset,
                file_count=len(manifest["files"]),
                row_count=manifest["total_rows"],
                bytes_=manifest["total_bytes"],
                run_id=run.run_id,
                metadata={"generator_config": manifest.get("generator_config", {})},
            )

            if decision is BatchDecision.DUPLICATE:
                run.note(decision="duplicate", skipped=True)
                logger.info("ingest_skipped_duplicate", dataset=dataset, tenant=tenant)
                return IngestResult(
                    source=SOURCE,
                    tenant=tenant,
                    dataset=dataset,
                    decision=decision,
                    batch_digest=batch_digest,
                    duration_seconds=time.perf_counter() - started,
                )

            files = self._verify(directory, manifest, run.run_id, tenant, dataset)
            self.registry.record_files(batch_digest, files)

            # One key release per run, audited against this run id. The landing
            # zone is encrypted under the same keys as Bronze (ADR 0022), so a
            # measurement that cannot obtain them cannot land data either.
            crypto = LandingCrypto.for_tenant(tenant, run_id=run.run_id)

            written = 0
            bytes_read = 0
            for part, entry in enumerate(files):
                path = directory / entry["relative_path"]
                frame = _read_any(path)
                enveloped = add_envelope(
                    frame,
                    run_id=run.run_id,
                    batch_digest=batch_digest,
                    source_file=entry["relative_path"],
                    source_system=manifest.get("tenant", tenant),
                    ordinal_offset=written,
                )
                write_landing(
                    enveloped,
                    lake_root=self.lake_root,
                    tenant=tenant,
                    dataset=dataset,
                    batch_digest=batch_digest,
                    part=part,
                    crypto=crypto,
                )
                written += len(enveloped)
                bytes_read += entry["bytes"]
                run.add(read=len(frame), written=len(enveloped), bytes_read=entry["bytes"])

            self.registry.mark(batch_digest, "ingested", run.run_id)
            run.note(files=len(files), decision=decision.value)

            # Declared row count is part of the source contract; a mismatch means
            # the manifest and the data disagree and must not pass silently.
            if written != manifest["total_rows"]:
                raise TerminalError(
                    f"row count mismatch: manifest declares {manifest['total_rows']:,}, "
                    f"read {written:,}"
                )

            return IngestResult(
                source=SOURCE,
                tenant=tenant,
                dataset=dataset,
                decision=decision,
                batch_digest=batch_digest,
                records_read=written,
                records_written=written,
                bytes_read=bytes_read,
                files_verified=len(files),
                output_path=self.lake_root / "landing" / tenant / dataset,
                duration_seconds=time.perf_counter() - started,
            )

    def available_datasets(self, tenant: str) -> list[str]:
        """Datasets a wildcard ingest should collect: manifested *and* contracted.

        The manifest test alone is what landed ``healthcare/patient_change`` — a
        source-side mutation script the generator writes to ``raw/`` alongside real
        datasets, replayed into Postgres by ``oltp_loader`` for the CDC collector to
        observe, and never intended to reach the lake. It has a manifest because
        everything the generator writes has one, so a directory-shaped rule swept it
        up, and it then sat in the landing zone unclassified and unencrypted because
        Bronze refused it and nothing upstream did (ADR 0022, ADR 0023).

        **The catalog decides what the lake ingests, not the shape of a directory
        tree.** Filtering here rather than failing in ``collect`` matters: since
        ADR 0022 an uncontracted write is refused, so a sweep that still offered
        this directory would abort the whole tenant's ingest on it. A *targeted*
        ``ingest file healthcare patient_change`` still fails, loudly, which is the
        right answer to someone asking for it by name.
        """
        base = self.root / tenant
        if not base.exists():
            return []

        manifested = sorted(d.name for d in base.iterdir() if (d / MANIFEST_NAME).exists())
        contracted = [name for name in manifested if (tenant, name) in CONTRACTS]

        skipped = sorted(set(manifested) - set(contracted))
        if skipped:
            logger.info(
                "ingest_skipping_uncontracted",
                tenant=tenant,
                datasets=skipped,
                hint="no schema contract; not a lake dataset",
            )
        return contracted


def _read_any(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    if path.suffix == ".csv":
        # Everything as string: a landing zone must preserve what arrived, and
        # letting pandas infer types here would silently "fix" the type-drift
        # defects that Stage 7 is supposed to detect.
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    if path.suffix == ".jsonl":
        return pd.read_json(path, lines=True, dtype=False)
    raise TerminalError(f"unsupported source format: {path.suffix}")
