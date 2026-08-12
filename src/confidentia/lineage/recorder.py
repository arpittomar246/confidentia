"""Compute a dataset's commitment and append it to the chain.

The claim this serves is "given a Gold row, prove which code, which keys and which
input records produced it". Recording it is three steps:

1. digest every record in the table, giving a Merkle root the platform commits to;
2. bind that root to the code measurement, the released key IDs and the upstream
   entries this dataset was derived from;
3. sign the result and append it under a lock, so the chain cannot fork.

**Leaves are recomputed, never stored.** Storing 145,000 leaf digests alongside the
root would let anyone who can edit the data edit the leaves to match. Because the
tree is rebuilt from the table each time a proof is needed, a proof can only be
produced if the data still hashes to the signed root -- which makes proof generation
itself the tamper check, rather than something layered on top of it.

**This records the state a run produced, not the run as it happens.** The recorder
runs after a write rather than inside it, so there is a window in which a table has
changed and no entry says so. That is a real gap and it is the price of not
rewriting four working writers to emit provenance inline; what closes it is the
orchestrator, which runs `lineage record` as the task immediately after each write.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from confidentia.common.hashing import GENESIS_DIGEST
from confidentia.common.logging import get_logger
from confidentia.config.settings import Layer, Tenant, get_settings
from confidentia.crypto.attestation import SIGNING_KEY_FILENAME, measure_code
from confidentia.lineage.chain import (
    LineageInput,
    LineageRecord,
    SignedEntry,
    entry_digest,
    key_fingerprint,
    load_signing_key,
    public_key_pem,
    sign_digest,
)
from confidentia.lineage.merkle import MerkleTree, leaf_digest
from confidentia.store import db
from confidentia.store.repositories import RunType, track_run

if TYPE_CHECKING:
    import pandas as pd
    from pyspark.sql import DataFrame

logger = get_logger(__name__)

# The lock every appender takes before reading the tip. `pg_advisory_xact_lock` is
# released when the transaction ends, including when it aborts, so a crashed
# recorder cannot leave the chain permanently unappendable. The unique constraint
# on `previous_digest` is the backstop if a second process somehow skips this.
_CHAIN_LOCK_KEY = 0x1_1EA6E  # 'lineage'

# Layers whose upstream is another chained dataset. Bronze's upstream is a set of
# landing files, which are digested by the ingest registry rather than chained --
# so a Bronze entry cites batch digests in its notes instead of an entry digest.
DEFAULT_UPSTREAM: dict[str, str] = {"silver": "bronze"}


class LineageTableMissingError(RuntimeError):
    pass


@dataclass
class RecordingResult:
    tenant: str
    dataset: str
    layer: str
    seq: int
    entry_digest: str
    merkle_root: str
    record_count: int
    hardware_rooted: bool
    # Timings kept apart from the signed payload: a duration is a property of the
    # machine that computed it, not a claim about the data, and putting it inside
    # the signature would make an identical dataset produce a different digest on
    # a busier host.
    read_ms: float = 0.0
    digest_ms: float = 0.0
    tree_ms: float = 0.0
    total_ms: float = 0.0
    inputs: tuple[str, ...] = ()

    def summary(self) -> str:
        return (
            f"{self.tenant}/{self.dataset}@{self.layer}  #{self.seq}  "
            f"{self.record_count:,} records  root {self.merkle_root[:12]}...  "
            f"entry {self.entry_digest[:12]}...  "
            f"({self.total_ms / 1000:.1f}s: read {self.read_ms / 1000:.1f}s, "
            f"digest {self.digest_ms / 1000:.1f}s, tree {self.tree_ms / 1000:.1f}s)"
        )


# ----------------------------------------------------------------------------
# Leaves


def _digest_partition(frames: Iterable[pd.DataFrame]) -> Iterable[pd.DataFrame]:
    """Hash each row of each Arrow batch, executor-side.

    A pandas UDF calling the *same* `leaf_digest` the driver uses is the point:
    the canonicalisation must be one implementation, or a proof computed one way
    would fail against a root computed the other. Doing this in Spark SQL with
    `sha2(to_json(...))` would be faster and would be a second implementation of
    canonical JSON whose null and numeric handling differs from Python's in ways
    that only show up as a failed verification months later.

    Only the digests cross back to the driver -- 64 bytes a row rather than the row.
    """
    import pandas as pd

    for frame in frames:
        records = frame.to_dict(orient="records")
        yield pd.DataFrame({"leaf": [leaf_digest(record) for record in records]})


def leaf_digests(frame: DataFrame) -> list[str]:
    """Every row's leaf digest, sorted.

    Sorted because Spark's partition order is not stable across reads, and an
    unsorted tree would produce a different root for identical data -- a false
    tamper alarm on every verification. Sorting on the driver rather than in Spark:
    145,000 hex digests is about 9 MB and Python sorts them in well under a second,
    where a distributed sort would cost a shuffle of the same data.
    """
    digests = [row["leaf"] for row in frame.mapInPandas(_digest_partition, "leaf string").collect()]
    digests.sort()
    return digests


# ----------------------------------------------------------------------------
# Chain append


def _tip(cursor: Any) -> tuple[int, str]:
    cursor.execute("SELECT seq, entry_digest FROM platform.lineage_entry ORDER BY seq DESC LIMIT 1")
    row = cursor.fetchone()
    if row is None:
        return 0, GENESIS_DIGEST
    return int(row["seq"]), str(row["entry_digest"])


def append(record: LineageRecord, *, run_id: Any = None) -> SignedEntry:
    """Sign `record` against the current tip and append it, atomically.

    Reading the tip and inserting the successor must be one transaction under one
    lock. Two recorders that both read tip *N* would otherwise both sign an entry
    whose predecessor is *N*, and a chain with two valid continuations is not a
    chain -- the unique constraint on `previous_digest` turns that race into a
    failed insert rather than a silent fork, and this lock stops it happening.
    """
    from psycopg.types.json import Jsonb

    settings = get_settings()
    key = load_signing_key(settings.keystore_path / SIGNING_KEY_FILENAME)
    fingerprint = key_fingerprint(key.public_key())
    if record.signing_key_fingerprint != fingerprint:
        # The fingerprint is inside the signed payload, so it must be the key that
        # is about to sign it. Signing a payload that names a different key would
        # produce an entry that fails its own verification.
        raise ValueError(
            "record names a signing key that is not the one loaded "
            f"({record.signing_key_fingerprint[:12]}... vs {fingerprint[:12]}...)"
        )
    pem = public_key_pem(key)

    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_CHAIN_LOCK_KEY,))
        _, previous = _tip(cur)
        digest = entry_digest(previous, record)
        signature = sign_digest(digest, key)
        payload = record.payload()
        cur.execute(
            """
            INSERT INTO platform.lineage_entry
                (entry_digest, previous_digest, run_id, tenant, dataset, layer,
                 transformation, record_count, merkle_root, code_measurement,
                 hardware_rooted, payload, signature, public_key_pem)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING seq
            """,
            (
                digest,
                previous,
                run_id,
                record.tenant,
                record.dataset,
                record.layer,
                record.transformation,
                record.record_count,
                record.merkle_root,
                record.code_measurement,
                record.hardware_rooted,
                Jsonb(payload),
                signature,
                pem,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        # The pool is configured with `dict_row`, which psycopg's stubs do not
        # model on a cursor taken directly from a connection.
        seq = int(cast("dict[str, Any]", row)["seq"])

    logger.info(
        "lineage_entry_appended",
        seq=seq,
        tenant=record.tenant,
        dataset=record.dataset,
        layer=record.layer,
        record_count=record.record_count,
    )
    return SignedEntry(
        seq=seq,
        previous_digest=previous,
        digest=digest,
        record=record,
        signature=signature,
        public_key_pem=pem,
    )


# ----------------------------------------------------------------------------
# Inputs


def latest_entry_digest(tenant: str, dataset: str, layer: str) -> str | None:
    row = db.fetch_one(
        """
        SELECT entry_digest FROM platform.lineage_latest
         WHERE tenant = %s AND dataset = %s AND layer = %s
        """,
        (tenant, dataset, layer),
    )
    return None if row is None else str(row["entry_digest"])


def _resolve_inputs(tenant: str, dataset: str, layer: str) -> tuple[LineageInput, ...]:
    """Which upstream commitments this entry depends on.

    Derived from the layer topology rather than declared per dataset, because the
    topology is the same fact the pipeline already encodes and a second declaration
    would be a second thing to keep in step. Gold is the exception: a mart is built
    from several Silver datasets, so it cites every Silver commitment for the
    tenant rather than one.
    """
    if layer == "gold":
        rows = db.fetch_all(
            "SELECT dataset, entry_digest FROM platform.lineage_latest "
            "WHERE tenant = %s AND layer = 'silver' ORDER BY dataset",
            (tenant,),
        )
        return tuple(
            LineageInput(
                tenant=tenant,
                dataset=str(row["dataset"]),
                layer="silver",
                entry_digest=str(row["entry_digest"]),
            )
            for row in rows
        )

    upstream = DEFAULT_UPSTREAM.get(layer)
    if upstream is None:
        return ()
    digest = latest_entry_digest(tenant, dataset, upstream)
    if digest is None:
        return ()
    return (LineageInput(tenant=tenant, dataset=dataset, layer=upstream, entry_digest=digest),)


def _source_batches(tenant: str, dataset: str) -> list[str]:
    """The landing batches promoted into this dataset.

    Bronze's inputs are files, not chained datasets, and the ingest registry
    already content-addresses them. Citing those digests is what makes "which
    input records" answerable at the bottom of the stack, where there is no
    upstream entry to point at.
    """
    rows = db.fetch_all(
        """
        SELECT batch_digest FROM platform.ingest_batch
         WHERE tenant = %s AND dataset = %s AND status = 'ingested'
         ORDER BY batch_digest
        """,
        (tenant, dataset),
    )
    return [str(row["batch_digest"]) for row in rows]


def _table_version(spark: Any, path: Path) -> int | None:
    """The Delta version the commitment was taken over.

    Recorded so a later verification can read exactly the version that was signed
    rather than whatever the table has become. Best-effort: a table without a
    readable history still gets an entry, because a missing version number is a
    weaker record and no record at all is a worse one.
    """
    try:
        history = spark.sql(f"DESCRIBE HISTORY delta.`{path}` LIMIT 1").collect()
    except Exception as exc:  # noqa: BLE001 - diagnostics only, never fatal
        logger.debug("delta_history_unavailable", path=str(path), error=str(exc))
        return None
    return int(history[0]["version"]) if history else None


# ----------------------------------------------------------------------------
# The entry point


def layer_path(tenant: str, dataset: str, layer: str) -> Path:
    return get_settings().layer_path(Layer(layer), Tenant(tenant), dataset)


def record_dataset(
    tenant: str,
    dataset: str,
    *,
    layer: str = "bronze",
    transformation: str | None = None,
) -> RecordingResult:
    """Digest a table, commit to it, and append the signed entry."""
    from confidentia.spark.session import session_for_tenant

    path = layer_path(tenant, dataset, layer)
    if not path.exists():
        raise LineageTableMissingError(f"no {layer} table at {path}")

    started = time.perf_counter()
    with track_run(
        RunType.LINEAGE,
        tenant=tenant,
        dataset=dataset,
        source=layer,
        metadata={"layer": layer, "stage": "lineage"},
    ) as run:
        with session_for_tenant(tenant, run_id=run.run_id) as (spark, keys):
            read_start = time.perf_counter()
            frame = spark.read.format("delta").load(str(path))
            # Cached because the count and the digest pass would otherwise read
            # and decrypt the table twice, which would double the measured cost of
            # provenance and make the overhead figure wrong in our favour.
            frame.cache()
            count = frame.count()
            read_ms = (time.perf_counter() - read_start) * 1000

            digest_start = time.perf_counter()
            leaves = leaf_digests(frame)
            digest_ms = (time.perf_counter() - digest_start) * 1000

            tree_start = time.perf_counter()
            root = MerkleTree(leaves).root
            tree_ms = (time.perf_counter() - tree_start) * 1000

            version = _table_version(spark, path)
            frame.unpersist()

            settings = get_settings()
            signing_key = load_signing_key(settings.keystore_path / SIGNING_KEY_FILENAME)
            notes: dict[str, Any] = {"leaf_count": len(leaves)}
            if layer == "bronze":
                batches = _source_batches(tenant, dataset)
                notes["input_batch_digests"] = batches
                notes["input_batch_count"] = len(batches)

            record = LineageRecord(
                tenant=tenant,
                dataset=dataset,
                layer=layer,
                transformation=transformation or f"{layer}.state",
                record_count=count,
                merkle_root=root,
                code_measurement=measure_code(),
                tee_backend=settings.tee.backend.value,
                hardware_rooted=keys.hardware_rooted,
                signing_key_fingerprint=key_fingerprint(signing_key.public_key()),
                recorded_at=datetime.now(UTC).isoformat(),
                run_id=str(run.run_id),
                table_version=version,
                key_ids=tuple(keys.key_ids),
                inputs=_resolve_inputs(tenant, dataset, layer),
                notes=notes,
            )

        entry = append(record, run_id=run.run_id)
        run.add(read=count)
        run.note(
            merkle_root=root,
            entry_digest=entry.digest,
            seq=entry.seq,
            digest_ms=round(digest_ms, 1),
        )

    total_ms = (time.perf_counter() - started) * 1000
    return RecordingResult(
        tenant=tenant,
        dataset=dataset,
        layer=layer,
        seq=entry.seq,
        entry_digest=entry.digest,
        merkle_root=root,
        record_count=count,
        hardware_rooted=record.hardware_rooted,
        read_ms=read_ms,
        digest_ms=digest_ms,
        tree_ms=tree_ms,
        total_ms=total_ms,
        inputs=tuple(item.entry_digest for item in record.inputs),
    )


def recordable_datasets(tenant: str, layer: str) -> list[str]:
    """Datasets that actually exist at this layer, in a stable order."""
    root = get_settings().lake_root / layer / tenant
    if not root.exists():
        return []
    return sorted(
        child.name for child in root.iterdir() if child.is_dir() and (child / "_delta_log").exists()
    )


def record_tenant(
    tenant: str, *, layer: str = "bronze", datasets: Sequence[str] | None = None
) -> list[RecordingResult]:
    names = list(datasets) if datasets else recordable_datasets(tenant, layer)
    results: list[RecordingResult] = []
    for name in names:
        results.append(record_dataset(tenant, name, layer=layer))
    return results


@dataclass
class ProofResult:
    """An inclusion proof checked against what the chain actually signed."""

    tenant: str
    dataset: str
    layer: str
    seq: int
    entry_digest: str
    signed_root: str
    recomputed_root: str
    matches: list[dict[str, Any]] = field(default_factory=list)
    proof_depth: int = 0
    verified: bool = False
    record_count: int = 0

    @property
    def root_matches(self) -> bool:
        return self.signed_root == self.recomputed_root

    def summary(self) -> str:
        if not self.matches:
            return "no row matched the key"
        verdict = "PROVEN" if self.verified else "NOT PROVEN"
        return (
            f"{verdict}: row is {'in' if self.verified else 'not in'} the set committed "
            f"by entry #{self.seq} ({self.record_count:,} records, "
            f"{self.proof_depth} sibling digests)"
        )
