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
#
# The test for inclusion is **does this code see plaintext under released keys**,
# not "is it part of the pipeline". Stage 8 forced the question and both additions
# below follow from that test:
#
# * `evaluation` requests keys and decrypts `record_key` to match detections
#   against the answer key. It is offline tooling and deliberately outside the
#   pipeline, which is an argument about *where it sits*, not about what it can
#   read. The measurement answers "which code touched this data", and evaluation
#   touches it.
# * `streaming` is empty until Stage 9 but will write to the lake, so it is listed
#   now rather than being remembered later — the failure mode of forgetting is a
#   consumer writing encrypted Delta from unattested code, which nothing would
#   report.
#
# The cost is real and accepted: editing either package now revokes key release
# until `keys approve` runs. That tax is the mechanism, not a side effect of it.
MEASURED_PACKAGES = (
    "crypto",
    "pipelines",
    "quality",
    "lineage",
    "evaluation",
    "streaming",
)


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


def _is_hex(value: str) -> bool:
    """Can this nonce go into REPORT_DATA as bytes rather than as its ASCII?

    ``issue_nonce`` produces hex, so the 32-character nonce becomes 16 bytes rather
    than 32. Both fit the 64-byte field; decoding is preferred only because it is
    what every other SEV-SNP tool does, and a report that has to be compared against
    one produced by Azure's own tooling should bind the nonce the same way.
    """
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


class AttestationBackend:
    """Produces signed evidence about the running code."""

    def __init__(self, signing_key_path: Path | None = None) -> None:
        settings = get_settings()
        self.settings = settings
        self.backend = settings.tee.backend
        self.signing_key_path = signing_key_path or (settings.keystore_path / SIGNING_KEY_FILENAME)
        # Per-attestation scratch for the SNP path: the nonce to bind into
        # REPORT_DATA, and the report once obtained, so the measurement and the MAA
        # exchange use the same one.
        self._pending_report_data: bytes = b""
        self._pending_report: tuple[Any, bytes] | None = None

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
        if self.backend is TeeBackend.SNP_CVM:
            # Implemented, never executed on hardware. See crypto/snp.py.
            report, _ = self._snp_report()
            return report.launch_measurement, True

        # Failing loudly rather than silently falling back to the simulated value:
        # a silent fallback would let a misconfigured deployment claim hardware
        # rooting it does not have, which is the single most damaging failure this
        # project could have.
        raise AttestationError(
            f"backend {self.backend.value} is selected, and nothing in this codebase "
            "produces a measurement for it. Only 'sim' and 'snp-cvm' are implemented."
        )

    def _snp_report(self) -> tuple[Any, bytes]:
        """The SEV-SNP report, with the nonce bound into REPORT_DATA where possible.

        Kept separate from ``produce_measurement`` so ``attest`` can reuse the same
        report for the MAA exchange rather than asking the firmware twice — two
        reports would carry two different timestamps and only one could be bound to
        the nonce.
        """
        from confidentia.crypto import snp

        if self._pending_report is not None:
            return self._pending_report

        try:
            result = snp.obtain_report(self._pending_report_data or b"")
        except snp.SnpError as exc:
            raise AttestationError(f"SEV-SNP attestation unavailable: {exc}") from exc

        self._pending_report = result
        return result

    def _maa_claims(self) -> dict[str, Any]:
        """Exchange the report for an MAA token and carry it in the evidence.

        The token is what makes this hardware-rooted: a third party, holding AMD's
        root of trust, asserts that the report is genuine. The local ECDSA signature
        still covers the evidence — it binds the nonce and the claims — but it is no
        longer the root, and the verifier checks the token before it trusts the
        measurement.

        ``snp_implementation_validated`` is stamped into every piece of evidence and
        therefore into every key-release audit row, so an audit trail written by this
        code says on its face that the code producing it has never run on hardware.
        """
        from confidentia.crypto import snp

        report, runtime_data = self._snp_report()
        uri = self.settings.crypto.attestation_uri
        if not uri:
            raise AttestationError(
                "CRYPTO_ATTESTATION_URI is not set. A SEV-SNP report is not evidence "
                "until Microsoft Azure Attestation has vouched for it; "
                "`terraform output attestation_uri` is the value."
            )

        try:
            token, maa_claims = snp.attest_via_maa(uri, report, runtime_data)
        except snp.SnpError as exc:
            raise AttestationError(f"MAA attestation failed: {exc}") from exc

        if not snp.VALIDATED_ON_HARDWARE:
            logger.warning(
                "snp_implementation_unvalidated",
                detail=(
                    "this SEV-SNP code path has never been executed on confidential "
                    "computing hardware; treat the measurement as provisional"
                ),
            )

        return {
            "maa_token": token,
            "maa_attestation_type": maa_claims.get("x-ms-attestation-type"),
            "maa_compliance_status": maa_claims.get("x-ms-compliance-status"),
            "snp_implementation_validated": snp.VALIDATED_ON_HARDWARE,
        }

    def attest(self, nonce: str, claims: dict[str, Any] | None = None) -> AttestationEvidence:
        # The nonce has to reach the firmware before the report is requested, so it
        # is stashed here rather than passed down through produce_measurement --
        # whose signature is shared with every other backend and should not grow a
        # parameter only one of them uses.
        self._pending_report_data = bytes.fromhex(nonce) if _is_hex(nonce) else nonce.encode()
        self._pending_report = None

        measurement, hardware_rooted = self.produce_measurement()
        key = self._load_or_create_key()

        produced_at = datetime.now(UTC).isoformat()
        evidence_claims: dict[str, Any] = dict(claims or {})

        if self.backend is TeeBackend.SNP_CVM:
            evidence_claims.update(self._maa_claims())
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
        # Held because the MAA path needs the attestation authority, and reading it
        # from the token's own `iss` would be trusting the evidence to name its own
        # verifier.
        self.settings = settings
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

        # Conversely, a backend that claims hardware rooting must prove it. The
        # ECDSA signature above proves only that whoever holds the local signing key
        # produced this evidence -- which, on the machine being attested, is
        # everybody. The MAA token is the part a compromised guest cannot forge.
        if evidence.backend == TeeBackend.SNP_CVM.value:
            self._verify_maa_token(evidence)

    def _verify_maa_token(self, evidence: AttestationEvidence) -> None:
        """Check the MAA token, and that it describes *this* evidence.

        Three things have to hold, and the third is the one that is easy to forget:

        1. The token is signed by a key in MAA's JWKS. This is the root of trust —
           MAA verifies the AMD signature chain on the report, so a valid token
           means AMD's firmware vouched for the guest.
        2. The token says the guest is what policy requires: an SEV-SNP VM, running
           a compliant Azure utility VM, not debuggable.
        3. **The launch measurement in the token equals the measurement in the
           evidence.** Without this, a guest could present a genuine token from any
           confidential VM alongside whatever measurement it liked, and the broker
           would approve key release against a measurement nothing attested to.

        The measurement carried in the evidence is what the key broker's policy
        allowlist is checked against, which is exactly why it has to be the one MAA
        signed rather than the one the prover typed.
        """
        import jwt

        token = evidence.claims.get("maa_token")
        if not token:
            raise AttestationError(
                "evidence claims the snp-cvm backend but carries no MAA token. A "
                "SEV-SNP report is not evidence until MAA has vouched for it."
            )

        uri = self.settings.crypto.attestation_uri
        if not uri:
            raise AttestationError(
                "CRYPTO_ATTESTATION_URI is not set, so there is no authority to "
                "verify this MAA token against. Refusing rather than trusting the "
                "issuer named inside the token itself."
            )

        issuer = uri.rstrip("/")
        try:
            client = jwt.PyJWKClient(f"{issuer}/certs", cache_keys=True)
            key = client.get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientError as exc:
            raise AttestationError(f"no MAA signing key matches this token: {exc}") from exc

        try:
            claims = jwt.decode(
                token,
                key=key,
                # Named explicitly rather than taken from the token's own header:
                # letting evidence choose its verification algorithm is the
                # algorithm-confusion opening the API layer documents at length.
                algorithms=["RS256"],
                issuer=issuer,
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iss": True,
                    # MAA tokens carry no audience.
                    "verify_aud": False,
                    "require": ["exp", "iss"],
                },
            )
        except jwt.InvalidTokenError as exc:
            raise AttestationError(f"MAA token rejected: {exc}") from exc

        attestation_type = str(claims.get("x-ms-attestation-type", "")).lower()
        if attestation_type != "sevsnpvm":
            raise AttestationError(
                f"MAA attested a {attestation_type or 'unknown'} guest, not an "
                "SEV-SNP confidential VM"
            )

        compliance = str(claims.get("x-ms-compliance-status", "")).lower()
        if compliance != "azure-compliant-uvm":
            raise AttestationError(
                f"MAA reports compliance status {compliance or 'unknown'}; policy "
                "requires azure-compliant-uvm"
            )

        if claims.get("x-ms-sevsnpvm-is-debuggable") is True:
            raise AttestationError(
                "the attested VM is debuggable, so its memory is not confidential"
            )

        attested = str(claims.get("x-ms-sevsnpvm-launchmeasurement", "")).lower()
        if not attested:
            raise AttestationError("MAA token carries no launch measurement")
        if not secrets.compare_digest(attested, evidence.measurement.lower()):
            raise AttestationError(
                "the measurement in this evidence is not the one MAA attested: "
                f"evidence says {evidence.measurement[:16]}..., token says "
                f"{attested[:16]}.... Key release policy is checked against the "
                "evidence, so this mismatch is a forged measurement, not a mismatch."
            )

        if evidence.claims.get("snp_implementation_validated") is False:
            # Not a refusal. The evidence is cryptographically sound; what is
            # unproven is the code that assembled it. Refusing would make the
            # backend unusable and so guarantee it is never validated.
            logger.warning(
                "snp_evidence_from_unvalidated_implementation",
                measurement=evidence.measurement[:16],
                detail="MAA verified this token; the reader that produced it has never run on hardware",
            )
