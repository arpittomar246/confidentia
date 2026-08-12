"""Tests for the provenance chain that need neither Spark nor a database.

Everything load-bearing about C3 is pure: a Merkle tree, a hash chain and an ECDSA
signature. That is deliberate — the property being claimed is "tampering is
detectable", and a claim like that is only worth as much as the test that tries to
tamper. So most of what follows is an attack: edit a leaf, edit a payload, move an
entry, swap the key, re-sign the tail. Each one has to be caught, and each one has
to be caught *for the right reason*, because a verifier that reports "broken link"
when the real problem is a forged signature would send an operator to the wrong
place.

The one test doing no attacking is the first, and it is the most important: this
tree must produce the same root as `hashing.merkle_root`, which has been in the
codebase since Stage 1. Two implementations of the same tree that disagree by one
promoted node would issue proofs that fail against roots computed the other way.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from confidentia.common.hashing import GENESIS_DIGEST, merkle_root, sha256_json
from confidentia.lineage.chain import (
    CHAIN_VERSION,
    LineageError,
    LineageInput,
    LineageRecord,
    SignedEntry,
    columns_match_payload,
    entry_digest,
    key_fingerprint,
    public_key_pem,
    sign_digest,
    verify_chain,
    verify_signature,
)
from confidentia.lineage.merkle import InclusionProof, MerkleTree, leaf_digest, verify_proof

pytestmark = pytest.mark.unit


def leaves(count: int) -> list[str]:
    return sorted(leaf_digest({"id": index, "value": f"row-{index}"}) for index in range(count))


class TestMerkleTree:
    # Odd sizes are the interesting ones: an odd node is promoted rather than
    # duplicated, and 5, 7 and 9 promote at different levels.
    @pytest.mark.parametrize("size", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 16, 17, 33])
    def test_root_agrees_with_the_stage_1_implementation(self, size: int) -> None:
        items = leaves(size)
        assert MerkleTree(items).root == merkle_root(items)

    def test_empty_tree_is_genesis(self) -> None:
        assert MerkleTree([]).root == GENESIS_DIGEST

    def test_single_leaf_is_its_own_root(self) -> None:
        items = leaves(1)
        tree = MerkleTree(items)
        assert tree.root == items[0]
        assert tree.proof(0).path == ()
        assert verify_proof(tree.proof(0), tree.root)

    @pytest.mark.parametrize("size", [1, 2, 3, 5, 8, 13, 17])
    def test_every_leaf_proves(self, size: int) -> None:
        tree = MerkleTree(leaves(size))
        for index in range(size):
            assert verify_proof(tree.proof(index), tree.root), f"leaf {index} of {size}"

    def test_proof_depth_is_logarithmic(self) -> None:
        # The reason inclusion proofs are worth having: 145k records, 18 digests.
        tree = MerkleTree(leaves(1000))
        assert tree.proof(500).depth == 10

    def test_a_tampered_leaf_does_not_verify(self) -> None:
        tree = MerkleTree(leaves(9))
        proof = tree.proof(4)
        forged = InclusionProof(
            leaf=leaf_digest({"id": 4, "value": "tampered"}), index=proof.index, path=proof.path
        )
        assert not verify_proof(forged, tree.root)

    def test_a_proof_does_not_verify_against_another_root(self) -> None:
        tree = MerkleTree(leaves(9))
        assert not verify_proof(tree.proof(3), MerkleTree(leaves(10)).root)

    def test_reordering_a_sibling_path_breaks_it(self) -> None:
        # Ordering is what makes H(a||b) different from H(b||a). A proof that
        # verified regardless of side would accept a sibling from anywhere.
        tree = MerkleTree(leaves(8))
        proof = tree.proof(2)
        flipped = InclusionProof(
            leaf=proof.leaf,
            index=proof.index,
            path=tuple(
                ("left" if side == "right" else "right", digest) for side, digest in proof.path
            ),
        )
        assert not verify_proof(flipped, tree.root)

    def test_out_of_range_index_raises(self) -> None:
        with pytest.raises(IndexError):
            MerkleTree(leaves(4)).proof(4)

    def test_proof_survives_a_round_trip_through_json(self) -> None:
        tree = MerkleTree(leaves(11))
        proof = tree.proof(7)
        assert verify_proof(InclusionProof.from_dict(proof.to_dict()), tree.root)


class TestLeafDigest:
    def test_key_order_does_not_change_the_leaf(self) -> None:
        # The whole reason canonical JSON exists here: two structurally identical
        # records assembled in different orders must not look like tampering.
        assert leaf_digest({"a": 1, "b": 2}) == leaf_digest({"b": 2, "a": 1})

    def test_a_changed_value_changes_the_leaf(self) -> None:
        assert leaf_digest({"a": 1}) != leaf_digest({"a": 2})

    def test_null_and_missing_are_distinguishable(self) -> None:
        # Spark's `to_json` omits nulls, which is exactly why leaves are computed
        # in Python instead. If these collided, a nulled-out column would be
        # invisible to the chain.
        assert leaf_digest({"a": None, "b": 1}) != leaf_digest({"b": 1})


# ----------------------------------------------------------------------------


def make_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def make_record(
    key: ec.EllipticCurvePrivateKey,
    *,
    dataset: str = "encounter",
    layer: str = "bronze",
    count: int = 100,
    root: str | None = None,
    inputs: tuple[LineageInput, ...] = (),
) -> LineageRecord:
    return LineageRecord(
        tenant="healthcare",
        dataset=dataset,
        layer=layer,
        transformation=f"{layer}.state",
        record_count=count,
        merkle_root=root or leaf_digest({"root": dataset}),
        code_measurement=sha256_json({"code": "v1"}),
        tee_backend="sim",
        hardware_rooted=False,
        signing_key_fingerprint=key_fingerprint(key.public_key()),
        recorded_at="2026-08-12T10:00:00+00:00",
        run_id="0a5b0f2e-0000-0000-0000-000000000001",
        key_ids=("healthcare:restricted:v1", "healthcare:confidential:v1"),
        inputs=inputs,
    )


def build_chain(key: ec.EllipticCurvePrivateKey, length: int = 5) -> list[SignedEntry]:
    entries: list[SignedEntry] = []
    previous = GENESIS_DIGEST
    pem = public_key_pem(key)
    for index in range(length):
        record = make_record(key, dataset=f"dataset_{index}", count=100 + index)
        digest = entry_digest(previous, record)
        entries.append(
            SignedEntry(
                seq=index + 1,
                previous_digest=previous,
                digest=digest,
                record=record,
                signature=sign_digest(digest, key),
                public_key_pem=pem,
            )
        )
        previous = digest
    return entries


def replace(entry: SignedEntry, **changes: object) -> SignedEntry:
    from dataclasses import replace as dataclass_replace

    return dataclass_replace(entry, **changes)  # type: ignore[arg-type]


class TestChain:
    def test_an_untouched_chain_verifies(self) -> None:
        key = make_key()
        result = verify_chain(build_chain(key))
        assert result.ok
        assert result.entries_checked == 5

    def test_an_empty_chain_verifies(self) -> None:
        # Vacuously, and that is correct: nothing has been claimed yet.
        assert verify_chain([]).ok

    def test_editing_a_payload_is_caught_at_that_entry(self) -> None:
        key = make_key()
        entries = build_chain(key)
        tampered = replace(
            entries[2],
            record=make_record(key, dataset="dataset_2", count=999_999),
        )
        entries[2] = tampered

        result = verify_chain(entries)
        assert not result.ok
        first = result.first_failure
        assert first is not None
        assert first.seq == 3
        assert first.reason == "payload altered"

    def test_an_edit_leaves_the_attacker_no_good_option(self) -> None:
        # The property that makes this a chain rather than a column of hashes, and
        # it is worth stating precisely because the obvious phrasing is wrong.
        #
        # An attacker who edits a payload has two options and both are caught.
        # Leave the digest column stale and the entry fails to recompute. Repair
        # the digest and the entry now disagrees with the predecessor its
        # successor recorded, so the link at N+1 fails -- and the signature at N
        # fails too, because it was made over the digest the entry used to have.
        #
        # Note what is *not* claimed: the failures are local, not a cascade to the
        # end of the chain. Verification walks the stored digests, so one edit
        # produces one link failure at the boundary rather than a failure at every
        # later entry. Localised detection is the more useful result anyway -- it
        # points at where the tampering happened -- but it is a weaker statement
        # than the one usually made about hash chains, and it is the true one.
        key = make_key()

        stale = build_chain(key, length=6)
        stale[1] = replace(stale[1], record=make_record(key, dataset="dataset_1", count=7))
        result = verify_chain(stale)
        assert [(f.seq, f.reason) for f in result.failures] == [(2, "payload altered")]

        repaired = build_chain(key, length=6)
        forged = make_record(key, dataset="dataset_1", count=7)
        repaired[1] = replace(
            repaired[1],
            record=forged,
            digest=entry_digest(repaired[1].previous_digest, forged),
        )
        result = verify_chain(repaired)
        assert [(f.seq, f.reason) for f in result.failures] == [
            (2, "signature invalid"),
            (3, "broken link"),
        ]

    def test_rewriting_the_tail_still_fails_without_the_key(self) -> None:
        # The strongest attack the chain alone does not stop: recompute every
        # digest after the edit so all the links agree again. Only the signature
        # catches this, which is why both bindings exist.
        key = make_key()
        attacker = make_key()
        entries = build_chain(key, length=4)

        previous = entries[1].digest
        for index in range(2, 4):
            record = make_record(key, dataset=f"dataset_{index}", count=1)
            digest = entry_digest(previous, record)
            entries[index] = SignedEntry(
                seq=index + 1,
                previous_digest=previous,
                digest=digest,
                record=record,
                # Signed with a key the attacker has, presented as the real one.
                signature=sign_digest(digest, attacker),
                public_key_pem=public_key_pem(key),
            )
            previous = digest

        result = verify_chain(entries)
        assert not result.ok
        assert {f.reason for f in result.failures} == {"signature invalid"}
        assert [f.seq for f in result.failures] == [3, 4]

    def test_swapping_in_the_attackers_public_key_is_caught(self) -> None:
        # A re-signed chain presented with the attacker's own public key would
        # verify on signatures alone. The fingerprint inside the signed payload is
        # what closes it, provided the verifier knows which key to expect.
        key = make_key()
        attacker = make_key()
        entries = build_chain(key, length=3)
        for index, entry in enumerate(entries):
            digest = entry.digest
            entries[index] = replace(
                entry,
                signature=sign_digest(digest, attacker),
                public_key_pem=public_key_pem(attacker),
            )

        loose = verify_chain(entries)
        assert loose.ok, "signatures alone cannot tell whose key this is"

        strict = verify_chain(entries, expected_fingerprint=key_fingerprint(attacker.public_key()))
        assert not strict.ok
        assert strict.first_failure is not None
        assert strict.first_failure.reason == "foreign signing key"

    def test_an_entry_replayed_at_another_position_fails(self) -> None:
        key = make_key()
        entries = build_chain(key, length=5)
        entries[3] = replace(entries[1], seq=4)

        result = verify_chain(entries)
        assert not result.ok
        assert any(f.reason == "broken link" for f in result.failures)

    def test_stop_early_reports_only_the_first_break(self) -> None:
        key = make_key()
        entries = build_chain(key, length=6)
        entries[1] = replace(entries[1], record=make_record(key, dataset="dataset_1", count=7))
        assert len(verify_chain(entries, stop_early=True).failures) == 1

    def test_deleting_an_entry_breaks_the_chain(self) -> None:
        key = make_key()
        entries = build_chain(key, length=5)
        del entries[2]
        assert not verify_chain(entries).ok


class TestSignature:
    def test_signature_verifies_under_its_own_key(self) -> None:
        key = make_key()
        digest = leaf_digest({"x": 1})
        assert verify_signature(digest, sign_digest(digest, key), public_key_pem(key))

    def test_signature_does_not_verify_over_a_different_digest(self) -> None:
        key = make_key()
        signature = sign_digest(leaf_digest({"x": 1}), key)
        assert not verify_signature(leaf_digest({"x": 2}), signature, public_key_pem(key))

    def test_garbage_key_and_signature_are_rejected_not_raised(self) -> None:
        # A verifier that raises on malformed input is a verifier an attacker can
        # turn into a crash instead of a "false".
        key = make_key()
        digest = leaf_digest({"x": 1})
        assert not verify_signature(digest, "not-base64!!", public_key_pem(key))
        assert not verify_signature(digest, sign_digest(digest, key), "not a pem")


class TestRecordPayload:
    def test_payload_round_trips(self) -> None:
        key = make_key()
        record = make_record(
            key,
            inputs=(LineageInput("healthcare", "encounter", "bronze", leaf_digest({"e": 1})),),
        )
        assert LineageRecord.from_payload(record.payload()) == record

    def test_an_unknown_chain_version_is_refused(self) -> None:
        # Skipping a field a verifier does not understand is how a signature stops
        # covering it, so an unknown version has to be fatal.
        key = make_key()
        payload = make_record(key).payload()
        payload["version"] = CHAIN_VERSION + 1
        with pytest.raises(LineageError):
            LineageRecord.from_payload(payload)

    def test_key_ids_are_recorded_and_material_has_nowhere_to_go(self) -> None:
        key = make_key()
        payload = make_record(key).payload()
        assert payload["key_ids"] == [
            "healthcare:restricted:v1",
            "healthcare:confidential:v1",
        ]
        assert "material" not in payload and "key_material" not in payload

    def test_hardware_rooted_is_false_and_travels_inside_the_signature(self) -> None:
        key = make_key()
        record = make_record(key)
        assert record.payload()["hardware_rooted"] is False
        digest = record.digest()
        from dataclasses import replace as dataclass_replace

        assert dataclass_replace(record, hardware_rooted=True).digest() != digest


class TestColumnDivergence:
    def test_matching_columns_report_nothing(self) -> None:
        key = make_key()
        record = make_record(key)
        columns = {
            "tenant": "healthcare",
            "dataset": "encounter",
            "layer": "bronze",
            "record_count": 100,
            "merkle_root": record.merkle_root,
            "code_measurement": record.code_measurement,
            "hardware_rooted": False,
        }
        assert columns_match_payload(record, columns) == []

    def test_an_edited_column_is_reported(self) -> None:
        # The one edit the chain itself cannot see, because only the payload is
        # signed. An UPDATE on this column changes what an operator reads without
        # touching a single digest.
        key = make_key()
        record = make_record(key)
        assert columns_match_payload(record, {"merkle_root": "0" * 64}) == ["merkle_root"]
        assert columns_match_payload(record, {"hardware_rooted": True}) == ["hardware_rooted"]

    def test_absent_columns_are_not_divergences(self) -> None:
        assert columns_match_payload(make_record(make_key()), {}) == []
