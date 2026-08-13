"""Tests for the SEV-SNP backend (ADR 0024).

**None of this proves the reader works.** The parts that can only be exercised on
hardware — the ioctl, the struct offsets, the vTPM NV layout — are not tested here
and cannot be, which is the whole reason `snp.VALIDATED_ON_HARDWARE` exists.

What *is* tested is everything that would still be a vulnerability if the reader
were perfect:

* the report parser rejects the shapes a wrong offset produces, rather than
  returning a plausible measurement;
* the MAA token is verified against a JWKS, not trusted;
* **the measurement in the evidence must be the one MAA signed**, because that is
  the value the key broker's allowlist is checked against;
* a token from a genuine but non-compliant guest is refused.

The token tests mint their own RSA keypair and serve a JWKS, so they exercise the
real `jwt.decode` path with real signatures rather than a mock.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import jwt
import pytest

from confidentia.crypto import snp
from confidentia.crypto.attestation import (
    AttestationError,
    AttestationEvidence,
    AttestationVerifier,
)

pytestmark = pytest.mark.unit

ISSUER = "https://sharedeus.eus.attest.azure.net"
MEASUREMENT = "ab" * 48


class TestTheMarkerIsHonest:
    def test_the_implementation_is_declared_unvalidated(self) -> None:
        # Deliberately brittle. Flipping this to True must be a deliberate act with
        # a diff attached, taken only after the reader has produced a report on an
        # Azure confidential VM and MAA has agreed with the measurement it parsed.
        assert snp.VALIDATED_ON_HARDWARE is False


class TestReportParsing:
    def _report(self, measurement: bytes, report_data: bytes = b"") -> bytes:
        raw = bytearray(snp.REPORT_SIZE)
        raw[snp.MEASUREMENT_OFFSET : snp.MEASUREMENT_OFFSET + snp.MEASUREMENT_SIZE] = measurement
        padded = report_data.ljust(snp.REPORT_DATA_SIZE, b"\x00")[: snp.REPORT_DATA_SIZE]
        raw[snp.REPORT_DATA_OFFSET : snp.REPORT_DATA_OFFSET + snp.REPORT_DATA_SIZE] = padded
        return bytes(raw)

    def test_a_short_report_is_refused(self) -> None:
        with pytest.raises(snp.SnpError, match="expected at least"):
            snp.AttestationReport.parse(b"\x00" * 100)

    def test_an_all_zero_measurement_is_refused(self) -> None:
        # This is what reading the wrong offset of a mostly-zero struct produces.
        # Returning it would hand the broker a measurement that looks like data.
        with pytest.raises(snp.SnpError, match="wrong struct offset"):
            snp.AttestationReport.parse(self._report(bytes(snp.MEASUREMENT_SIZE)))

    def test_a_valid_report_yields_the_measurement(self) -> None:
        report = snp.AttestationReport.parse(self._report(b"\xab" * snp.MEASUREMENT_SIZE))
        assert report.launch_measurement == MEASUREMENT

    def test_the_nonce_binding_is_checked_not_assumed(self) -> None:
        nonce = b"\x01\x02\x03\x04"
        bound = snp.AttestationReport.parse(self._report(b"\xab" * 48, nonce))
        assert bound.binds_nonce(nonce) is True
        # A firmware or driver that ignored REPORT_DATA would otherwise produce
        # replayable evidence and nothing would notice.
        assert bound.binds_nonce(b"\x09\x09") is False


@pytest.fixture
def signing_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks_verifier(signing_key, monkeypatch):
    """A verifier whose JWKS lookup returns our test key."""
    from confidentia.config.settings import get_settings

    monkeypatch.setenv("CRYPTO_ATTESTATION_URI", ISSUER)
    monkeypatch.setenv("CONFIDENTIA_KEYSTORE_PASSPHRASE", "test-passphrase")
    get_settings.cache_clear()

    verifier = AttestationVerifier()

    class _Key:
        key = signing_key.public_key()

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def get_signing_key_from_jwt(self, token):
            return _Key()

    monkeypatch.setattr(jwt, "PyJWKClient", _Client)
    yield verifier
    get_settings.cache_clear()


def _token(signing_key, **overrides: Any) -> str:
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "exp": int(time.time()) + 600,
        "x-ms-attestation-type": "sevsnpvm",
        "x-ms-compliance-status": "azure-compliant-uvm",
        "x-ms-sevsnpvm-is-debuggable": False,
        "x-ms-sevsnpvm-launchmeasurement": MEASUREMENT,
    }
    claims.update(overrides)
    return jwt.encode(claims, signing_key, algorithm="RS256")


def _evidence(token: str | None, measurement: str = MEASUREMENT) -> AttestationEvidence:
    claims: dict[str, Any] = {"snp_implementation_validated": False}
    if token is not None:
        claims["maa_token"] = token
    return AttestationEvidence(
        measurement=measurement,
        nonce="00" * 16,
        produced_at="2026-08-14T00:00:00+00:00",
        backend="snp-cvm",
        hardware_rooted=True,
        public_key_pem="",
        signature="",
        claims=claims,
    )


class TestMaaTokenVerification:
    def test_a_valid_token_is_accepted(self, jwks_verifier, signing_key) -> None:
        jwks_verifier._verify_maa_token(_evidence(_token(signing_key)))

    def test_missing_token_is_refused(self, jwks_verifier) -> None:
        with pytest.raises(AttestationError, match="no MAA token"):
            jwks_verifier._verify_maa_token(_evidence(None))

    def test_a_forged_signature_is_refused(self, jwks_verifier, signing_key) -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa

        attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with pytest.raises(AttestationError, match="MAA token rejected"):
            jwks_verifier._verify_maa_token(_evidence(_token(attacker)))

    def test_a_measurement_the_token_did_not_attest_is_refused(
        self, jwks_verifier, signing_key
    ) -> None:
        # The sharpest case. The token is genuine — it could come from any
        # confidential VM — but the evidence carries a different measurement, and
        # the measurement is what the broker's allowlist is checked against.
        forged = _evidence(_token(signing_key), measurement="cd" * 48)
        with pytest.raises(AttestationError, match="not the one MAA attested"):
            jwks_verifier._verify_maa_token(forged)

    def test_a_non_snp_guest_is_refused(self, jwks_verifier, signing_key) -> None:
        token = _token(signing_key, **{"x-ms-attestation-type": "tpm"})
        with pytest.raises(AttestationError, match="not an"):
            jwks_verifier._verify_maa_token(_evidence(token))

    def test_a_non_compliant_uvm_is_refused(self, jwks_verifier, signing_key) -> None:
        token = _token(signing_key, **{"x-ms-compliance-status": "unknown"})
        with pytest.raises(AttestationError, match="compliance status"):
            jwks_verifier._verify_maa_token(_evidence(token))

    def test_a_debuggable_vm_is_refused(self, jwks_verifier, signing_key) -> None:
        # Memory of a debuggable confidential VM is readable by the host, so the
        # confidentiality claim does not hold even though attestation succeeds.
        token = _token(signing_key, **{"x-ms-sevsnpvm-is-debuggable": True})
        with pytest.raises(AttestationError, match="debuggable"):
            jwks_verifier._verify_maa_token(_evidence(token))

    def test_an_expired_token_is_refused(self, jwks_verifier, signing_key) -> None:
        token = _token(signing_key, exp=int(time.time()) - 60)
        with pytest.raises(AttestationError, match="MAA token rejected"):
            jwks_verifier._verify_maa_token(_evidence(token))

    def test_a_token_from_another_issuer_is_refused(self, jwks_verifier, signing_key) -> None:
        token = _token(signing_key, iss="https://attacker.example")
        with pytest.raises(AttestationError, match="MAA token rejected"):
            jwks_verifier._verify_maa_token(_evidence(token))


class TestMaaCrossCheck:
    def test_a_measurement_disagreement_names_the_offset(self, monkeypatch) -> None:
        """The check that catches a struct offset that is plausible and wrong.

        MAA parses the same bytes independently. If this module's offsets are wrong,
        the two disagree — and this is the only place that disagreement can be seen,
        because a laptop cannot produce a real report to compare against.
        """
        report = snp.AttestationReport(
            raw=b"\x00" * snp.REPORT_SIZE,
            launch_measurement="ab" * 48,
            report_data=b"",
        )
        claims = {"x-ms-sevsnpvm-launchmeasurement": "cd" * 48}
        token = "x." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".y"

        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict[str, Any]:
                return {"token": token}

        import httpx

        monkeypatch.setattr(httpx, "post", lambda *a, **k: _Response())

        with pytest.raises(snp.SnpError, match="disagree about the launch measurement"):
            snp.attest_via_maa(ISSUER, report, b"")
