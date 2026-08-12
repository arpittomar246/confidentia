"""Read the chain back and check every claim it makes.

Three checks, and they fail for different reasons, which is why they are reported
separately rather than as one boolean:

*Chain* -- links, digests and signatures, computed from the database alone. Catches
an edit to a stored entry. Cheap; runs over the whole chain in milliseconds.

*Columns* -- the denormalised columns against the signed payload. Catches the one
edit the chain cannot see, because only `payload` is signed and an operator reading
`merkle_root` from the column is reading unprotected data.

*Deep* -- the data itself, re-digested and compared to the signed root. Catches an
edit to the lake that never touched the ledger. Expensive: it reads and decrypts
every table, so it is opt-in rather than part of every verification.

A chain check that passes while a deep check fails is the interesting case: it means
nobody forged provenance, they simply changed the data underneath it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from confidentia.common.logging import get_logger
from confidentia.lineage.chain import (
    ChainFailure,
    ChainVerification,
    LineageRecord,
    SignedEntry,
    columns_match_payload,
    verify_chain,
)
from confidentia.lineage.merkle import MerkleTree, verify_proof
from confidentia.lineage.recorder import ProofResult, layer_path, leaf_digests
from confidentia.store import db

logger = get_logger(__name__)

_ENTRY_COLUMNS = """
    seq, entry_digest, previous_digest, payload, signature, public_key_pem,
    tenant, dataset, layer, record_count, merkle_root, code_measurement,
    hardware_rooted, recorded_at
"""


def _to_entry(row: dict[str, Any]) -> SignedEntry:
    return SignedEntry(
        seq=int(row["seq"]),
        previous_digest=str(row["previous_digest"]),
        digest=str(row["entry_digest"]),
        record=LineageRecord.from_payload(row["payload"]),
        signature=str(row["signature"]),
        public_key_pem=str(row["public_key_pem"]),
    )


def load_chain() -> list[dict[str, Any]]:
    """The whole chain in `seq` order.

    Always the whole chain, never a filtered slice. Verifying only the entries for
    one dataset would recompute links across gaps and report every one of them as
    broken -- the chain is global by construction, so verification is too.
    """
    return db.fetch_all(f"SELECT {_ENTRY_COLUMNS} FROM platform.lineage_entry ORDER BY seq")


@dataclass
class VerificationReport:
    chain: ChainVerification
    column_failures: tuple[ChainFailure, ...] = ()
    deep: tuple[DeepCheck, ...] = ()

    @property
    def ok(self) -> bool:
        return self.chain.ok and not self.column_failures and all(check.ok for check in self.deep)

    @property
    def failures(self) -> tuple[ChainFailure, ...]:
        return tuple(self.chain.failures) + tuple(self.column_failures)

    def summary(self) -> str:
        if self.ok:
            deep = f", {len(self.deep)} datasets re-digested" if self.deep else ""
            return f"lineage verified: {self.chain.entries_checked} entries intact{deep}"
        broken_deep = [check for check in self.deep if not check.ok]
        parts = []
        if not self.chain.ok:
            parts.append(f"{len(self.chain.failures)} chain failures")
        if self.column_failures:
            parts.append(f"{len(self.column_failures)} column divergences")
        if broken_deep:
            parts.append(f"{len(broken_deep)} datasets no longer match their root")
        return "lineage VERIFICATION FAILED: " + ", ".join(parts)


@dataclass
class DeepCheck:
    tenant: str
    dataset: str
    layer: str
    seq: int
    signed_root: str
    recomputed_root: str
    signed_count: int
    actual_count: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.signed_root == self.recomputed_root

    def summary(self) -> str:
        if self.error:
            return f"{self.tenant}/{self.dataset}@{self.layer}: {self.error}"
        if self.ok:
            return (
                f"{self.tenant}/{self.dataset}@{self.layer}: matches entry #{self.seq} "
                f"({self.actual_count:,} records)"
            )
        drift = self.actual_count - self.signed_count
        moved = f", row count moved by {drift:+,}" if drift else ", row count unchanged"
        return (
            f"{self.tenant}/{self.dataset}@{self.layer}: DOES NOT MATCH entry #{self.seq} "
            f"(signed {self.signed_root[:12]}..., recomputed {self.recomputed_root[:12]}...{moved})"
        )


def verify(*, deep: bool = False, tenants: Sequence[str] | None = None) -> VerificationReport:
    rows = load_chain()
    entries = [_to_entry(row) for row in rows]
    chain = verify_chain(entries)

    column_failures: list[ChainFailure] = []
    for index, (row, entry) in enumerate(zip(rows, entries, strict=True)):
        mismatched = columns_match_payload(entry.record, row)
        if mismatched:
            column_failures.append(
                ChainFailure(
                    seq=entry.seq,
                    index=index,
                    reason="column diverges from signed payload",
                    detail=", ".join(mismatched),
                )
            )

    deep_checks: tuple[DeepCheck, ...] = ()
    if deep:
        deep_checks = tuple(_deep_check(entries, tenants=tenants))

    return VerificationReport(chain=chain, column_failures=tuple(column_failures), deep=deep_checks)


def _latest_per_dataset(entries: Sequence[SignedEntry]) -> list[SignedEntry]:
    """The newest entry for each dataset.

    Only the newest is re-digested. An older entry commits to a state the table no
    longer holds, so checking it against the current data would report tampering
    for every dataset that has legitimately been rewritten since -- which is all of
    them, on a platform that reprocesses.
    """
    latest: dict[tuple[str, str, str], SignedEntry] = {}
    for entry in entries:
        key = (entry.record.tenant, entry.record.dataset, entry.record.layer)
        latest[key] = entry
    return [latest[key] for key in sorted(latest)]


def _deep_check(
    entries: Sequence[SignedEntry], *, tenants: Sequence[str] | None = None
) -> list[DeepCheck]:
    from confidentia.spark.session import session_for_tenant

    targets = _latest_per_dataset(entries)
    if tenants:
        wanted = set(tenants)
        targets = [entry for entry in targets if entry.record.tenant in wanted]

    checks: list[DeepCheck] = []
    by_tenant: dict[str, list[SignedEntry]] = {}
    for entry in targets:
        by_tenant.setdefault(entry.record.tenant, []).append(entry)

    for tenant, tenant_entries in by_tenant.items():
        # One session per tenant, not per dataset: PME configuration is
        # session-global and scoped to a tenant, and paying Spark's startup cost
        # once per dataset would dominate the whole check.
        with session_for_tenant(tenant, app_name=f"confidentia-lineage-verify-{tenant}") as (
            spark,
            _keys,
        ):
            for entry in tenant_entries:
                record = entry.record
                path = layer_path(record.tenant, record.dataset, record.layer)
                if not path.exists():
                    checks.append(
                        DeepCheck(
                            tenant=record.tenant,
                            dataset=record.dataset,
                            layer=record.layer,
                            seq=entry.seq,
                            signed_root=record.merkle_root,
                            recomputed_root="",
                            signed_count=record.record_count,
                            actual_count=0,
                            error=f"table is gone: {path}",
                        )
                    )
                    continue
                frame = spark.read.format("delta").load(str(path))
                frame.cache()
                actual = frame.count()
                root = MerkleTree(leaf_digests(frame)).root
                frame.unpersist()
                checks.append(
                    DeepCheck(
                        tenant=record.tenant,
                        dataset=record.dataset,
                        layer=record.layer,
                        seq=entry.seq,
                        signed_root=record.merkle_root,
                        recomputed_root=root,
                        signed_count=record.record_count,
                        actual_count=actual,
                    )
                )
    return checks


# ----------------------------------------------------------------------------
# Proving one row


class NoCommitmentError(RuntimeError):
    pass


def prove_record(
    tenant: str,
    dataset: str,
    *,
    layer: str = "bronze",
    column: str | None = None,
    value: str,
) -> ProofResult:
    """Prove a single row belongs to the set the platform signed.

    This is the claim from the project brief made concrete: point at one row, get
    back a path of sibling digests that reconstructs a root the platform signed
    with its attested identity, together with the code measurement and key IDs
    that were in force when it did.

    The proof is generated against leaves re-derived from the table, so a row that
    has been altered simply cannot be proven: its digest is no longer in the tree,
    and the tree no longer builds the signed root.
    """
    from confidentia.catalog.contracts import ContractError, get_contract
    from confidentia.spark.session import session_for_tenant

    row = db.fetch_one(
        f"SELECT {_ENTRY_COLUMNS} FROM platform.lineage_entry "
        "WHERE tenant = %s AND dataset = %s AND layer = %s ORDER BY seq DESC LIMIT 1",
        (tenant, dataset, layer),
    )
    if row is None:
        raise NoCommitmentError(
            f"no lineage entry for {tenant}/{dataset}@{layer}; run `lineage record` first"
        )
    entry = _to_entry(row)

    if column is None:
        # Gold marts have no contract -- they are built, not ingested -- so a
        # missing one is a signal to ask for the column rather than an error.
        try:
            contract = get_contract(tenant, dataset)
        except ContractError as exc:
            raise ValueError(
                f"no contract for {tenant}/{dataset}, so no business key to search on; "
                "pass --column explicitly"
            ) from exc
        if not contract.business_key:
            raise ValueError(
                f"no business key declared for {tenant}/{dataset}; pass --column explicitly"
            )
        column = contract.business_key[0]

    path = layer_path(tenant, dataset, layer)
    result = ProofResult(
        tenant=tenant,
        dataset=dataset,
        layer=layer,
        seq=entry.seq,
        entry_digest=entry.digest,
        signed_root=entry.record.merkle_root,
        recomputed_root="",
    )

    with session_for_tenant(tenant, app_name=f"confidentia-lineage-prove-{tenant}") as (
        spark,
        _keys,
    ):
        frame = spark.read.format("delta").load(str(path))
        frame.cache()
        if column not in frame.columns:
            frame.unpersist()
            raise ValueError(f"column {column!r} is not in {tenant}/{dataset}@{layer}")

        leaves = leaf_digests(frame)
        tree = MerkleTree(leaves)
        result.recomputed_root = tree.root
        result.record_count = tree.size

        # The target row's leaf comes from the same UDF over the same frame, so it
        # is byte-identical to the leaf already in the tree. Recomputing it from a
        # dict the caller supplied would be a second canonicalisation and a
        # different digest.
        target_leaves = leaf_digests(frame.filter(frame[column].cast("string") == value))
        frame.unpersist()

    for leaf in target_leaves:
        index = tree.index_of(leaf)
        if index is None:  # pragma: no cover - the leaf came from the same tree's data
            result.matches.append({"leaf": leaf, "verified": False, "reason": "leaf not in tree"})
            continue
        proof = tree.proof(index)
        # Against the *signed* root, not the recomputed one. Verifying against a
        # root derived from the same read would prove only that the data is
        # self-consistent, which it always is.
        verified = verify_proof(proof, entry.record.merkle_root)
        result.proof_depth = proof.depth
        result.matches.append(
            {"leaf": leaf, "index": index, "depth": proof.depth, "verified": verified}
        )

    result.verified = bool(result.matches) and all(item["verified"] for item in result.matches)
    return result


@dataclass
class DatasetHistory:
    tenant: str
    dataset: str
    layer: str
    entries: list[dict[str, Any]] = field(default_factory=list)


def history(tenant: str, dataset: str, layer: str, limit: int = 20) -> DatasetHistory:
    rows = db.fetch_all(
        f"SELECT {_ENTRY_COLUMNS} FROM platform.lineage_entry "
        "WHERE tenant = %s AND dataset = %s AND layer = %s ORDER BY seq DESC LIMIT %s",
        (tenant, dataset, layer, limit),
    )
    return DatasetHistory(tenant=tenant, dataset=dataset, layer=layer, entries=rows)
