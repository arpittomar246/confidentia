"""Remote attestation: evidence generation and verification.

**Read this before trusting anything below.**

Under the ``sim`` backend the measurement is computed by the very process it
describes. A compromised process can therefore report any measurement it likes.
This provides **no security guarantee whatsoever**. What it does provide is the
complete mechanism — measurement, nonce freshness, signed evidence, policy
allowlist, audit trail — and the real operational consequences that follow from it,
notably that changing pipeline code stops key release until the new measurement is
approved.

Under ``gramine-sgx`` or ``snp-cvm`` the measurement would come from hardware
(MRENCLAVE / SNP report) and the signature would chain to a vendor root, at which
point the same code path becomes a genuine security boundary. The interface is
identical; only ``AttestationBackend.produce_measurement`` changes.

Every artefact records which backend produced it, and
``AttestationEvidence.hardware_rooted`` is False under simulation. Nothing in this
codebase may present a simulated attestation as a hardware-rooted one.

The cryptography here is real regardless of backend: ECDSA P-256 signatures over a
canonical encoding, nonce-based freshness, SHA-256 measurements.
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

from confidentia.common.hashing import canonical_json, sha256_file, sha256_json
from confidentia.common.logging import get_logger
from confidentia.config.settings import TeeBackend, get_settings

logger = get_logger(__name__)

SIGNING_KEY_FILENAME = "attestation_signing_key.pem"

# Source directories whose contents constitute "the code running in the enclave".
# Deliberately narrow: the measurement must change when transformation logic
# changes, and must not change when an unrelated test or document is edited, or
# every commit would revoke every key.
MEASURED_PACKAGES = ("crypto", "pipelines", "quality", "lineage")


class AttestationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AttestationEvidence:
    """A signed claim about what code is running."""

    measurement: str
    nonce: str
    produced_at: str
    backend: str
    hardware_rooted: bool
    public_key_pem: str
    signature: str
    claims: dict[str, Any] = field(default_factory=dict)

    def signed_payload(self) -> dict[str, Any]:
        """Exactly the fields covered by the signature.

        The signature must not cover itself, and must cover the nonce — otherwise
        evidence from an earlier session could be replayed verbatim.
        """
        return {
            "measurement": self.measurement,
            "nonce": self.nonce,
            "produced_at": self.produced_at,
            "backend": self.backend,
            "hardware_rooted": self.hardware_rooted,
            "claims": self.claims,
        }

    def digest(self) -> str:
        return sha256_json(self.signed_payload())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def measure_code(root: Path | None = None) -> str:
    """Compute the code measurement.

    SHA-256 over the sorted (relative path, file digest) pairs of the measured
    packages. Sorted so the result is independent of filesystem ordering; paths
    included so that renaming a module changes the measurement, because a rename
    changes what the enclave is.

    This is the simulator's analogue of MRENCLAVE. It is not MRENCLAVE.
    """
    base = root or Path(__file__).resolve().parents[1]
    entries: list[tuple[str, str]] = []
    for package in MEASURED_PACKAGES:
        directory = base / package
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            entries.append((path.relative_to(base).as_posix(), sha256_file(path)))
    return sha256_json(entries)


def issue_nonce() -> str:
    """A fresh challenge. The verifier issues this, never the prover."""
    return secrets.token_hex(16)


class AttestationBackend:
    """Produces signed evidence about the running code."""

    def __init__(self, signing_key_path: Path | None = None) -> None:
        settings = get_settings()
        self.settings = settings
        self.backend = settings.tee.backend
        self.signing_key_path = signing_key_path or (settings.keystore_path / SIGNING_KEY_FILENAME)

    # ------------------------------------------------------------------

    def _load_or_create_key(self) -> ec.EllipticCurvePrivateKey:
        if self.signing_key_path.exists():
            loaded = serialization.load_pem_private_key(
                self.signing_key_path.read_bytes(), password=None
            )
            # A PEM file can hold RSA, Ed25519, DH or a post-quantum key, each with
            # a different sign()/verify() signature. Accepting whatever is on disk
            # would mean the attestation signature algorithm is decided by the
            # contents of a file rather than by policy.
            if not isinstance(loaded, ec.EllipticCurvePrivateKey):
                raise AttestationError(
                    f"attestation signing key at {self.signing_key_path} is a "
                    f"{type(loaded).__name__}, not an elliptic-curve key. "
                    "Attestation evidence must be ECDSA-signed."
                )
            return loaded

        curve = ec.SECP384R1() if self.settings.crypto.attestation_curve == "P-384" else ec.SECP256R1()
        key = ec.generate_private_key(curve)
        self.signing_key_path.parent.mkdir(parents=True, exist_ok=True)
        self.signing_key_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                # Unencrypted on disk under the simulator; the file is owner-only.
                # Under a real backend this key would never leave the enclave.
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        self.signing_key_path.chmod(0o600)
        logger.info("attestation_key_created", path=str(self.signing_key_path))
        return key

    def produce_measurement(self) -> tuple[str, bool]:
        """Return ``(measurement, hardware_rooted)``."""
        if self.backend is TeeBackend.SIM:
            # Self-measured. See the module docstring.
            return measure_code(), False
        # Real backends would read MRENCLAVE from the SGX report or the launch
        # measurement from the SNP attestation report here. Failing loudly rather
        # than silently falling back to the simulated value: a silent fallback
        # would let a misconfigured deployment claim hardware rooting it does not
        # have, which is the single most damaging failure this project could have.
        raise AttestationError(
            f"backend {self.backend.value} is selected but no hardware attestation "
            "source is available on this host"
        )

    def attest(self, nonce: str, claims: dict[str, Any] | None = None) -> AttestationEvidence:
        measurement, hardware_rooted = self.produce_measurement()
        key = self._load_or_create_key()

        produced_at = datetime.now(UTC).isoformat()
        evidence_claims: dict[str, Any] = dict(claims or {})
        payload: dict[str, Any] = {
            "measurement": measurement,
            "nonce": nonce,
            "produced_at": produced_at,
            "backend": self.backend.value,
            "hardware_rooted": hardware_rooted,
            "claims": evidence_claims,
        }
        digest = hashes.Hash(hashes.SHA256())
        digest.update(canonical_json(payload))
        signature = key.sign(digest.finalize(), ec.ECDSA(Prehashed(hashes.SHA256())))

        public_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

        return AttestationEvidence(
            measurement=measurement,
            nonce=nonce,
            produced_at=produced_at,
            backend=self.backend.value,
            hardware_rooted=hardware_rooted,
            public_key_pem=public_pem,
            signature=base64.b64encode(signature).decode(),
            claims=evidence_claims,
        )


class AttestationVerifier:
    """Checks evidence: signature, freshness, and backend consistency.

    Policy (which measurements are allowed) lives in the key broker, not here.
    This class answers only "is this evidence internally valid and fresh".
    """

    def __init__(self, max_age_seconds: int | None = None) -> None:
        settings = get_settings()
        # `is None`, not `or`: a caller passing 0 means "accept nothing older than
        # this instant", and `or` would silently substitute the 300s default --
        # turning a deliberately strict freshness window into a lax one.
        if max_age_seconds is None:
            max_age_seconds = settings.crypto.attestation_nonce_ttl_seconds
        self.max_age = timedelta(seconds=max_age_seconds)

    def verify(self, evidence: AttestationEvidence, expected_nonce: str) -> None:
        """Raise ``AttestationError`` if the evidence is not acceptable."""
        # Nonce first: it is the cheapest check and the one that stops replay.
        if not secrets.compare_digest(evidence.nonce, expected_nonce):
            raise AttestationError("nonce mismatch: evidence does not answer this challenge")

        try:
            produced = datetime.fromisoformat(evidence.produced_at)
        except ValueError as exc:
            raise AttestationError(f"unparseable produced_at: {exc}") from exc
        if produced.tzinfo is None:
            produced = produced.replace(tzinfo=UTC)

        age = datetime.now(UTC) - produced
        if age > self.max_age:
            raise AttestationError(
                f"evidence is stale: {age.total_seconds():.0f}s old, limit "
                f"{self.max_age.total_seconds():.0f}s"
            )
        if age < timedelta(seconds=-30):
            # Clock skew beyond a small tolerance suggests a forged timestamp.
            raise AttestationError("evidence is dated in the future")

        try:
            public_key = serialization.load_pem_public_key(evidence.public_key_pem.encode())
        except Exception as exc:
            raise AttestationError(f"unusable public key: {exc}") from exc

        # Pin the key type before verifying. `verify()` takes different arguments
        # per algorithm, and letting the evidence choose its own algorithm is a
        # key-confusion opening: the prover supplies this public key, so it could
        # otherwise pick a type whose verification semantics it controls.
        if not isinstance(public_key, ec.EllipticCurvePublicKey):
            raise AttestationError(
                f"evidence carries a {type(public_key).__name__}; only ECDSA "
                "attestation keys are accepted"
            )

        digest = hashes.Hash(hashes.SHA256())
        digest.update(canonical_json(evidence.signed_payload()))
        try:
            public_key.verify(
                base64.b64decode(evidence.signature),
                digest.finalize(),
                ec.ECDSA(Prehashed(hashes.SHA256())),
            )
        except InvalidSignature as exc:
            raise AttestationError("signature does not verify") from exc

        # A simulated backend must never present itself as hardware-rooted.
        if evidence.backend == TeeBackend.SIM.value and evidence.hardware_rooted:
            raise AttestationError(
                "evidence claims hardware rooting under the sim backend; rejected"
            )
