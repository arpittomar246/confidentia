"""The lineage chain: what a provenance record says, and how it is bound.

A lineage **table** records what happened. A lineage **chain** makes altering that
record after the fact detectable. The difference is the whole contribution, and it
comes down to two bindings applied to every entry:

*Backward* -- the entry's digest is ``H(previous_digest || H(payload))``, and the
next entry records that digest as its predecessor. An edited payload therefore no
longer produces the digest stored beside it; repairing that digest instead breaks
the link its successor recorded.

*Outward* -- the entry digest is ECDSA-signed with the attestation key, so an
attacker who rewrites the chain from the tampered point onward -- fixing every link
as they go -- still cannot produce signatures for it without that key.

Both are needed. The chain alone stops an edit but not a rewrite; the signature
alone stops a forgery but leaves each entry independently replaceable.

**What is not claimed:** that one edit invalidates the entire tail. Verification
walks the digests as stored, so a repaired entry produces a failure at itself and
at the link immediately after it, not at every entry to the end of the chain. That
is the more useful result -- it locates the tampering -- but it is a narrower claim
than hash chains are usually described as making, and the narrower one is true.

**What this does not establish.** Under the ``sim`` backend the process measures
itself and holds its own signing key in the keystore. A chain signed that way
detects tampering by anything that is not the platform, and cannot detect a
dishonest platform -- one that signs a false record at the moment of writing has
produced a perfectly valid chain. Every entry therefore carries
``hardware_rooted = false``, and no claim beyond that is available here. Moving the
signing key into an enclave is what would change the answer, and that is hardware
this project does not have.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

from confidentia.common.hashing import GENESIS_DIGEST, canonical_json, sha256_bytes, sha256_json

# The chain's schema version travels inside the signed payload. A verifier that
# meets an entry it does not understand must fail rather than skip fields, because
# ignoring an unknown field is exactly how a signature stops covering it.
CHAIN_VERSION = 1


class LineageError(RuntimeError):
    pass


class SigningKeyMissingError(LineageError):
    """No attestation key on disk.

    Deliberately fatal rather than self-healing. `AttestationBackend` creates a key
    when it finds none, which is right for attestation -- a fresh deployment must
    be able to attest. It is wrong here: minting a new identity to sign provenance
    would let a chain be re-signed end to end simply by deleting a file.
    """


@dataclass(frozen=True)
class LineageInput:
    """An upstream commitment this entry depends on.

    Naming the input *dataset* would give a lineage graph. Naming the input
    dataset's most recent **entry digest** gives a lineage graph in which an edge
    cannot be silently redirected: change the Bronze entry and the Gold entry that
    cited it no longer refers to anything that exists.
    """

    tenant: str
    dataset: str
    layer: str
    entry_digest: str

    def payload(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "dataset": self.dataset,
            "layer": self.layer,
            "entry_digest": self.entry_digest,
        }

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> LineageInput:
        return cls(
            tenant=str(data["tenant"]),
            dataset=str(data["dataset"]),
            layer=str(data["layer"]),
            entry_digest=str(data["entry_digest"]),
        )


@dataclass(frozen=True)
class LineageRecord:
    """One provenance claim, in the exact form that gets signed.

    Key *IDs*, never key material -- the same rule `platform.key_release_audit`
    follows. A provenance record naming which DEKs were released is auditable; one
    carrying the DEKs is a keystore with extra steps.
    """

    tenant: str
    dataset: str
    layer: str
    transformation: str
    record_count: int
    merkle_root: str
    code_measurement: str
    tee_backend: str
    hardware_rooted: bool
    signing_key_fingerprint: str
    recorded_at: str
    run_id: str | None = None
    table_version: int | None = None
    key_ids: tuple[str, ...] = ()
    inputs: tuple[LineageInput, ...] = ()
    notes: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        """Exactly the bytes the digest covers.

        Everything the platform claims goes in here. A field held only in a
        database column is a field the signature does not protect, so the columns
        on `platform.lineage_entry` are projections of this payload and the
        verifier checks them against it rather than trusting them.
        """
        return {
            "version": CHAIN_VERSION,
            "tenant": self.tenant,
            "dataset": self.dataset,
            "layer": self.layer,
            "transformation": self.transformation,
            "record_count": self.record_count,
            "merkle_root": self.merkle_root,
            "code_measurement": self.code_measurement,
            "tee_backend": self.tee_backend,
            "hardware_rooted": self.hardware_rooted,
            "signing_key_fingerprint": self.signing_key_fingerprint,
            "recorded_at": self.recorded_at,
            "run_id": self.run_id,
            "table_version": self.table_version,
            "key_ids": list(self.key_ids),
            "inputs": [item.payload() for item in self.inputs],
            "notes": self.notes,
        }

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> LineageRecord:
        version = int(data.get("version", 0))
        if version != CHAIN_VERSION:
            raise LineageError(
                f"lineage entry declares chain version {version}, this build "
                f"understands {CHAIN_VERSION}"
            )
        return cls(
            tenant=str(data["tenant"]),
            dataset=str(data["dataset"]),
            layer=str(data["layer"]),
            transformation=str(data["transformation"]),
            record_count=int(data["record_count"]),
            merkle_root=str(data["merkle_root"]),
            code_measurement=str(data["code_measurement"]),
            tee_backend=str(data["tee_backend"]),
            hardware_rooted=bool(data["hardware_rooted"]),
            signing_key_fingerprint=str(data["signing_key_fingerprint"]),
            recorded_at=str(data["recorded_at"]),
            run_id=data.get("run_id"),
            table_version=data.get("table_version"),
            key_ids=tuple(data.get("key_ids") or ()),
            inputs=tuple(LineageInput.from_payload(item) for item in data.get("inputs") or ()),
            notes=dict(data.get("notes") or {}),
        )

    def digest(self) -> str:
        return sha256_json(self.payload())


def entry_digest(previous_digest: str, record: LineageRecord) -> str:
    """``H(previous || H(payload))``.

    Written out here rather than calling `hashing.chain_digest` on the record
    object, because that helper hashes whatever it is handed and the thing that
    must be hashed is the payload -- not the dataclass, whose field order and
    defaults are an implementation detail that a refactor could change.
    """
    return sha256_bytes(bytes.fromhex(previous_digest) + bytes.fromhex(record.digest()))


@dataclass(frozen=True)
class SignedEntry:
    """A chain link as it is stored and as it is verified."""

    seq: int
    previous_digest: str
    digest: str
    record: LineageRecord
    signature: str
    public_key_pem: str


# ----------------------------------------------------------------------------
# Signing


def key_fingerprint(public_key: ec.EllipticCurvePublicKey) -> str:
    """SHA-256 over the DER SubjectPublicKeyInfo.

    Recorded *inside* the signed payload while the PEM itself sits in a column, so
    swapping the stored public key for an attacker's own is detected: the swapped
    key verifies nothing, and even a re-signed chain carries a fingerprint that no
    longer matches the key presented alongside it.
    """
    return sha256_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def public_key_pem(key: ec.EllipticCurvePrivateKey | ec.EllipticCurvePublicKey) -> str:
    public = key.public_key() if isinstance(key, ec.EllipticCurvePrivateKey) else key
    return public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def sign_digest(digest_hex: str, key: ec.EllipticCurvePrivateKey) -> str:
    """Sign the *entry* digest, which already covers the payload and the link.

    Signing the entry digest rather than the payload is what makes the signature
    cover the chain position too: an entry lifted from one point in the chain and
    replayed at another has a different predecessor, so a different entry digest,
    so a signature that no longer verifies.
    """
    prehashed = hashes.Hash(hashes.SHA256())
    prehashed.update(canonical_json({"entry_digest": digest_hex}))
    signature = key.sign(prehashed.finalize(), ec.ECDSA(Prehashed(hashes.SHA256())))
    return base64.b64encode(signature).decode()


def verify_signature(digest_hex: str, signature_b64: str, public_pem: str) -> bool:
    try:
        loaded = serialization.load_pem_public_key(public_pem.encode())
    except Exception:  # noqa: BLE001 - the PEM is attacker-reachable; any parse
        # failure means "does not verify", and letting an exception escape would
        # turn a malformed stored key into a crash instead of a rejection.
        return False
    # Pin the key type before verifying: `verify()` takes different arguments per
    # algorithm, and the stored PEM is attacker-reachable, so accepting whatever
    # type it declares would let it choose verification semantics it controls.
    if not isinstance(loaded, ec.EllipticCurvePublicKey):
        return False
    prehashed = hashes.Hash(hashes.SHA256())
    prehashed.update(canonical_json({"entry_digest": digest_hex}))
    try:
        loaded.verify(
            base64.b64decode(signature_b64),
            prehashed.finalize(),
            ec.ECDSA(Prehashed(hashes.SHA256())),
        )
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def load_signing_key(path: Path) -> ec.EllipticCurvePrivateKey:
    """Load the attestation signing key. Never create one -- see the class above."""
    if not path.exists():
        raise SigningKeyMissingError(
            f"no attestation signing key at {path}. Provenance is signed with the "
            "attested platform identity; run `keys init` before recording lineage."
        )
    loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(loaded, ec.EllipticCurvePrivateKey):
        raise LineageError(
            f"attestation signing key at {path} is a {type(loaded).__name__}, "
            "not an elliptic-curve key"
        )
    return loaded


# ----------------------------------------------------------------------------
# Verification


@dataclass(frozen=True)
class ChainFailure:
    seq: int
    index: int
    reason: str
    detail: str

    def summary(self) -> str:
        return f"entry #{self.seq}: {self.reason} -- {self.detail}"


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    entries_checked: int
    failures: tuple[ChainFailure, ...] = ()

    @property
    def first_failure(self) -> ChainFailure | None:
        return self.failures[0] if self.failures else None

    def summary(self) -> str:
        if self.ok:
            return f"chain intact: {self.entries_checked} entries verified"
        first = self.first_failure
        assert first is not None
        return (
            f"chain BROKEN at entry #{first.seq} of {self.entries_checked} "
            f"({len(self.failures)} failing): {first.reason}"
        )


def verify_chain(
    entries: Iterable[SignedEntry],
    *,
    expected_fingerprint: str | None = None,
    stop_early: bool = False,
) -> ChainVerification:
    """Recompute a chain and check every binding on it.

    Continues past the first failure by default, because failures here are local
    (see the module docstring) and stopping at the first would report one edited
    entry where there may be twenty. "How much of this ledger is trustworthy" is
    the question being asked, and it cannot be answered from the first break.
    """
    failures: list[ChainFailure] = []
    previous = GENESIS_DIGEST
    checked = 0

    for index, entry in enumerate(entries):
        checked += 1

        if entry.previous_digest != previous:
            failures.append(
                ChainFailure(
                    seq=entry.seq,
                    index=index,
                    reason="broken link",
                    detail=(
                        f"stored previous {entry.previous_digest[:12]}..., "
                        f"expected {previous[:12]}..."
                    ),
                )
            )

        expected_digest = entry_digest(entry.previous_digest, entry.record)
        if expected_digest != entry.digest:
            failures.append(
                ChainFailure(
                    seq=entry.seq,
                    index=index,
                    reason="payload altered",
                    detail=(f"recomputed {expected_digest[:12]}..., stored {entry.digest[:12]}..."),
                )
            )
        elif not verify_signature(entry.digest, entry.signature, entry.public_key_pem):
            # Only meaningful when the digest itself recomputed correctly; a
            # mismatched digest already explains a failing signature and reporting
            # both would double-count one edit.
            failures.append(
                ChainFailure(
                    seq=entry.seq,
                    index=index,
                    reason="signature invalid",
                    detail="the stored signature does not verify under the stored public key",
                )
            )

        if expected_fingerprint and entry.record.signing_key_fingerprint != expected_fingerprint:
            failures.append(
                ChainFailure(
                    seq=entry.seq,
                    index=index,
                    reason="foreign signing key",
                    detail=(
                        f"entry names key {entry.record.signing_key_fingerprint[:12]}..., "
                        f"this platform holds {expected_fingerprint[:12]}..."
                    ),
                )
            )

        if failures and stop_early:
            break
        # Advance on the *stored* digest, not the recomputed one: the goal is to
        # reproduce what the chain claims about itself, and substituting a
        # corrected value here would repair the chain during verification.
        previous = entry.digest

    return ChainVerification(ok=not failures, entries_checked=checked, failures=tuple(failures))


def columns_match_payload(record: LineageRecord, columns: Mapping[str, Any]) -> list[str]:
    """Which denormalised columns disagree with the signed payload.

    `platform.lineage_entry` repeats a handful of payload fields as columns so the
    table can be indexed and joined. Only the payload is signed, so those columns
    are the one place an edit is invisible to the chain -- an operator reading
    `merkle_root` from the column would be reading unprotected data. Verification
    therefore treats a divergence as tampering rather than as drift.
    """
    expected = {
        "tenant": record.tenant,
        "dataset": record.dataset,
        "layer": record.layer,
        "record_count": record.record_count,
        "merkle_root": record.merkle_root,
        "code_measurement": record.code_measurement,
        "hardware_rooted": record.hardware_rooted,
    }
    mismatched: list[str] = []
    for name, value in expected.items():
        if name in columns and columns[name] != value:
            mismatched.append(name)
    return mismatched


def head_digest(entries: Sequence[SignedEntry]) -> str:
    return entries[-1].digest if entries else GENESIS_DIGEST
