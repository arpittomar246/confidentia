"""Merkle trees over record digests, with inclusion proofs.

`common/hashing.py` has had `merkle_root` since Stage 1 and it computes a root and
nothing else. A root alone supports one claim -- "this set of records is unchanged"
-- and the claim C3 actually needs is narrower and more useful:

    given *this* Gold row, show it was among the records the platform committed to,
    without re-reading the other 145,000.

That is an inclusion proof, and it is the reason this module exists rather than a
second call to `merkle_root`. The proof is O(log n) digests: for a 145k-row table,
18 hashes instead of a full table scan.

**The promotion rule must match `hashing.merkle_root` exactly.** An odd node at any
level is promoted unchanged rather than duplicated -- duplicating the last leaf, as
Bitcoin does, admits the second-preimage quirk where two different leaf sets share a
root. Two implementations of the same tree that disagree by one branch would produce
proofs that fail against roots computed by the other, so `MerkleTree.root` is tested
against `merkle_root` over the same leaves rather than trusted to agree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from confidentia.common.hashing import GENESIS_DIGEST, sha256_bytes, sha256_json

Side = Literal["left", "right"]


def leaf_digest(record: Mapping[str, Any]) -> str:
    """The digest of one record, as it enters the tree.

    Canonical JSON, so two structurally identical records hash identically
    regardless of key order or how they were assembled. Anything that reaches a
    leaf must go through here; a leaf built from a plain `str(record)` would make
    the root depend on Python's repr and break on a version upgrade.
    """
    return sha256_json(record)


def _combine(left: str, right: str) -> str:
    return sha256_bytes(bytes.fromhex(left) + bytes.fromhex(right))


@dataclass(frozen=True)
class InclusionProof:
    """A path from one leaf to the root.

    Carries the leaf index for diagnostics only -- verification uses the siblings
    and the ordering recorded with them, never the index, so an attacker who
    rewrites the index cannot steer the recomputation.
    """

    leaf: str
    index: int
    path: tuple[tuple[Side, str], ...]

    @property
    def depth(self) -> int:
        return len(self.path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "leaf": self.leaf,
            "index": self.index,
            "path": [[side, digest] for side, digest in self.path],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> InclusionProof:
        return cls(
            leaf=str(payload["leaf"]),
            index=int(payload["index"]),
            path=tuple((str(side), str(digest)) for side, digest in payload["path"]),  # type: ignore[misc]
        )


class MerkleTree:
    """A binary Merkle tree built once and queried many times.

    Levels are retained because proof generation needs the siblings, and a table
    of 145k leaves costs about 9 MB of hex digests at the leaf level and half that
    again for everything above it. That is affordable on this driver and is the
    reason proofs are generated from a rebuilt tree rather than from a stored one:
    rebuilding re-derives the leaves from the data, so a proof can only be produced
    if the data still matches.
    """

    def __init__(self, leaves: Sequence[str]) -> None:
        self._leaves = tuple(leaves)
        self._levels: list[tuple[str, ...]] = []
        if not self._leaves:
            return
        level: tuple[str, ...] = self._leaves
        self._levels.append(level)
        while len(level) > 1:
            nxt: list[str] = []
            for i in range(0, len(level) - 1, 2):
                nxt.append(_combine(level[i], level[i + 1]))
            if len(level) % 2 == 1:
                # Promoted, not duplicated. See the module docstring.
                nxt.append(level[-1])
            level = tuple(nxt)
            self._levels.append(level)

    @classmethod
    def from_records(cls, records: Sequence[Mapping[str, Any]]) -> MerkleTree:
        return cls(sorted(leaf_digest(record) for record in records))

    @property
    def leaves(self) -> tuple[str, ...]:
        return self._leaves

    @property
    def size(self) -> int:
        return len(self._leaves)

    @property
    def root(self) -> str:
        if not self._levels:
            return GENESIS_DIGEST
        return self._levels[-1][0]

    def index_of(self, leaf: str) -> int | None:
        try:
            return self._leaves.index(leaf)
        except ValueError:
            return None

    def proof(self, index: int) -> InclusionProof:
        if not 0 <= index < len(self._leaves):
            raise IndexError(f"leaf {index} out of range for a tree of {len(self._leaves)}")
        path: list[tuple[Side, str]] = []
        position = index
        for level in self._levels[:-1]:
            if position % 2 == 1:
                path.append(("left", level[position - 1]))
            elif position + 1 < len(level):
                path.append(("right", level[position + 1]))
            # else: the node was promoted at this level and has no sibling, which
            # is exactly the case a duplicate-the-last-leaf tree would have hidden.
            position //= 2
        return InclusionProof(leaf=self._leaves[index], index=index, path=tuple(path))

    def proof_for(self, record: Mapping[str, Any]) -> InclusionProof | None:
        index = self.index_of(leaf_digest(record))
        return None if index is None else self.proof(index)


def verify_proof(proof: InclusionProof, root: str) -> bool:
    """Recompute the root from a leaf and its path.

    The whole point is that this needs neither the tree nor the other records --
    a verifier holding only the signed root and this proof can check the claim.
    """
    current = proof.leaf
    for side, sibling in proof.path:
        current = _combine(sibling, current) if side == "left" else _combine(current, sibling)
    return current == root
