"""Content hashing and Merkle chaining.

This module is small but load-bearing for two of the project's contributions:

*Idempotent ingestion* -- a batch is identified by the hash of its bytes, not by
its filename, so re-delivering the same file under a new name is a no-op rather
than a duplicate.

*Verifiable lineage (C3)* -- each pipeline task emits a provenance record whose
digest is chained into the previous record's digest. Any later edit to a
historical record breaks every digest after it, which is what makes the lineage
ledger tamper-evident rather than merely informative.

Canonicalisation matters more than the hash function here: two structurally
identical records must produce identical bytes, or the chain produces false
tamper alarms. ``canonical_json`` fixes key order, separators and encoding.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

# 1 MiB. Large enough to keep syscall overhead negligible, small enough that
# hashing a multi-GB file does not add a GB to resident memory.
_CHUNK_BYTES = 1024 * 1024

GENESIS_DIGEST = "0" * 64


def canonical_json(payload: Any) -> bytes:
    """Deterministic JSON encoding suitable for hashing and signing."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(payload: Any) -> str:
    return sha256_bytes(canonical_json(payload))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_directory(root: Path, pattern: str = "**/*") -> str:
    """Hash a directory's contents order-independently.

    Used to fingerprint a Delta table version or a code directory. Paths are
    sorted and included in the digest so that renaming a file changes the
    result -- a rename is a real change to a dataset's identity.
    """
    entries: list[tuple[str, str]] = []
    for path in sorted(root.glob(pattern)):
        if path.is_file():
            entries.append((path.relative_to(root).as_posix(), sha256_file(path)))
    return sha256_json(entries)


def merkle_root(leaves: Sequence[str]) -> str:
    """Binary Merkle root over hex digests.

    An odd node at any level is promoted rather than duplicated. Duplicating the
    last leaf -- the Bitcoin approach -- admits a known second-preimage quirk
    where two different leaf sets yield the same root, which would let a tampered
    record set produce a matching root.
    """
    if not leaves:
        return GENESIS_DIGEST
    level = list(leaves)
    while len(level) > 1:
        nxt: list[str] = []
        for i in range(0, len(level) - 1, 2):
            nxt.append(sha256_bytes(bytes.fromhex(level[i]) + bytes.fromhex(level[i + 1])))
        if len(level) % 2 == 1:
            nxt.append(level[-1])
        level = nxt
    return level[0]


def chain_digest(previous_digest: str, record: Any) -> str:
    """Digest of a provenance record bound to its predecessor.

    ``H(previous || H(record))``. Recomputing the chain over the stored records
    and comparing against the stored digests is the entire verification
    procedure for the lineage ledger.
    """
    return sha256_bytes(bytes.fromhex(previous_digest) + bytes.fromhex(sha256_json(record)))


def verify_chain(records: Iterable[tuple[str, Any]]) -> tuple[bool, int | None]:
    """Recompute a chain of ``(stored_digest, record)`` pairs.

    Returns ``(ok, first_bad_index)``. The index is what the dashboard's lineage
    view highlights when it renders an edge as tampered.
    """
    previous = GENESIS_DIGEST
    for index, (stored, record) in enumerate(records):
        expected = chain_digest(previous, record)
        if expected != stored:
            return False, index
        previous = stored
    return True, None
