"""Tests for the hashing primitives underpinning verifiable lineage (C3)."""

from __future__ import annotations

import pytest

from confidentia.common.hashing import (
    GENESIS_DIGEST,
    canonical_json,
    chain_digest,
    merkle_root,
    sha256_json,
    verify_chain,
)

pytestmark = pytest.mark.unit


class TestCanonicalisation:
    def test_key_order_does_not_change_the_digest(self) -> None:
        # Two provenance records built by different code paths must hash alike,
        # or the ledger raises false tamper alarms.
        assert sha256_json({"a": 1, "b": 2}) == sha256_json({"b": 2, "a": 1})

    def test_nested_key_order_does_not_change_the_digest(self) -> None:
        left = {"outer": {"x": 1, "y": [1, 2]}, "z": 3}
        right = {"z": 3, "outer": {"y": [1, 2], "x": 1}}
        assert sha256_json(left) == sha256_json(right)

    def test_list_order_does_change_the_digest(self) -> None:
        # Input dataset order is meaningful in a lineage record.
        assert sha256_json([1, 2]) != sha256_json([2, 1])

    def test_encoding_is_compact_and_stable(self) -> None:
        assert canonical_json({"b": 1, "a": "x"}) == b'{"a":"x","b":1}'

    def test_distinguishes_types_that_stringify_alike(self) -> None:
        assert sha256_json({"v": 1}) != sha256_json({"v": "1"})


class TestMerkleRoot:
    def test_empty_input_returns_genesis(self) -> None:
        assert merkle_root([]) == GENESIS_DIGEST

    def test_single_leaf_is_its_own_root(self) -> None:
        leaf = sha256_json({"only": True})
        assert merkle_root([leaf]) == leaf

    def test_root_changes_when_any_leaf_changes(self) -> None:
        leaves = [sha256_json({"i": i}) for i in range(5)]
        original = merkle_root(leaves)
        leaves[3] = sha256_json({"i": 99})
        assert merkle_root(leaves) != original

    def test_odd_leaf_is_promoted_not_duplicated(self) -> None:
        # Duplicating the final leaf (the Bitcoin approach) lets a 3-leaf set and a
        # 4-leaf set with a repeated tail collide, which would let a tampered record
        # set reproduce a valid root.
        three = [sha256_json({"i": i}) for i in range(3)]
        four_with_repeat = [*three, three[-1]]
        assert merkle_root(three) != merkle_root(four_with_repeat)


class TestProvenanceChain:
    def test_untouched_chain_verifies(self) -> None:
        records = [{"task": f"t{i}", "rows": i * 10} for i in range(4)]
        chain: list[tuple[str, dict[str, object]]] = []
        previous = GENESIS_DIGEST
        for record in records:
            previous = chain_digest(previous, record)
            chain.append((previous, record))

        ok, bad_index = verify_chain(chain)
        assert ok
        assert bad_index is None

    def test_editing_a_historical_record_is_detected(self) -> None:
        records = [{"task": f"t{i}", "rows": i * 10} for i in range(4)]
        chain: list[tuple[str, dict[str, object]]] = []
        previous = GENESIS_DIGEST
        for record in records:
            previous = chain_digest(previous, record)
            chain.append((previous, record))

        # An attacker rewrites the row count of an already-committed task.
        chain[1][1]["rows"] = 999_999

        ok, bad_index = verify_chain(chain)
        assert not ok
        assert bad_index == 1, "verification must point at the first tampered record"

    def test_reordering_records_is_detected(self) -> None:
        records = [{"task": f"t{i}"} for i in range(3)]
        chain: list[tuple[str, dict[str, object]]] = []
        previous = GENESIS_DIGEST
        for record in records:
            previous = chain_digest(previous, record)
            chain.append((previous, record))

        chain[0], chain[1] = chain[1], chain[0]
        ok, _ = verify_chain(chain)
        assert not ok

    def test_truncating_the_chain_is_not_detected_by_verify_alone(self) -> None:
        # Documents a real limitation rather than asserting a guarantee the design
        # does not provide: a prefix of a valid chain is itself a valid chain.
        # Detecting truncation needs an externally anchored head digest, which the
        # ledger stores separately in Postgres.
        records = [{"task": f"t{i}"} for i in range(4)]
        chain: list[tuple[str, dict[str, object]]] = []
        previous = GENESIS_DIGEST
        for record in records:
            previous = chain_digest(previous, record)
            chain.append((previous, record))

        ok, _ = verify_chain(chain[:2])
        assert ok, "a truncated prefix still verifies - hence the anchored head digest"
