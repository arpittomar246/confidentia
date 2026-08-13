"""What a hardware TEE backend does today, pinned so nobody assumes otherwise.

Preparing the Azure deployment turned up a gap that reads, from the outside, like a
success. Selecting `TEE_BACKEND=snp-cvm` makes `is_measured` return True and every
label read MEASURED — those key off the enum alone — and *then* the first
attestation raises, because nothing reads a SEV-SNP report. The infrastructure comes
up and the platform refuses to run on it.

Two properties are asserted here and they pull in opposite directions on purpose:

- the labels **do** flip, because that is the designed behaviour and the report
  documents it;
- attestation **does not** succeed, and must keep failing loudly until someone
  implements the report reader.

The second is the one that matters. A silent fallback to the simulated measurement
would let a misconfigured deployment claim hardware rooting it does not have, which
is the single most damaging failure this project could have — so the test asserts
the refusal, not the eventual feature.

**Delete the last test when the SNP reader lands, not before**, and replace it with
one that asserts a real launch measurement.
"""

from __future__ import annotations

import pytest

from confidentia.config.settings import TeeBackend, get_settings

pytestmark = pytest.mark.unit


@pytest.fixture
def snp_settings(monkeypatch):
    monkeypatch.setenv("TEE_BACKEND", "snp-cvm")
    monkeypatch.setenv("CONFIDENTIA_KEYSTORE_PASSPHRASE", "test-passphrase")
    get_settings.cache_clear()
    yield get_settings()
    monkeypatch.delenv("TEE_BACKEND", raising=False)
    get_settings.cache_clear()


class TestHardwareBackendIsDeclaredNotImplemented:
    def test_the_labels_flip(self, snp_settings) -> None:
        # Nothing that consumes these branches on the backend, which is the design:
        # select a hardware backend and every report follows.
        assert snp_settings.tee.backend is TeeBackend.SNP_CVM
        assert snp_settings.tee.is_measured is True
        assert snp_settings.tee.measurement_label == "MEASURED"

    def test_but_attestation_refuses_rather_than_falling_back(self, snp_settings) -> None:
        from confidentia.crypto.attestation import (
            AttestationBackend,
            AttestationError,
            issue_nonce,
        )

        # The refusal is the feature. A fallback to measure_code() here would produce
        # evidence indistinguishable from a laptop's, carrying hardware_rooted=True.
        with pytest.raises(AttestationError) as caught:
            AttestationBackend().attest(issue_nonce())

        # The message has to name the gap, because the symptom points elsewhere: by
        # the time this raises, everything the operator can see already says MEASURED.
        message = str(caught.value)
        assert "no hardware attestation source" in message
        assert "sev-guest" in message or "vTPM" in message
