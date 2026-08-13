"""What a hardware TEE backend does on a host that has no such hardware.

Selecting `TEE_BACKEND=snp-cvm` makes `is_measured` return True and every label read
MEASURED — those key off the enum alone. The reader now exists (ADR 0024), so the
question this file pins is what happens when it runs somewhere there is nothing to
read: **it must refuse, and say which sources it tried.**

Two properties, pulling in opposite directions on purpose:

- the labels **do** flip, because that is the designed behaviour and the report
  documents it;
- attestation **does not** succeed on a machine with no SEV-SNP guest, and must keep
  failing loudly rather than falling back.

The second is the one that matters. A silent fallback to the self-measured value
would let a misconfigured deployment claim hardware rooting it does not have, which
is the single most damaging failure this project could have — so the test asserts
the refusal.

This file used to say the reader was unwritten. It is written now and has still
never run on hardware; `tests/unit/test_snp.py` pins that distinction.
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

        # The message has to name both sources it tried, because the symptom points
        # elsewhere: by the time this raises, everything the operator can see already
        # says MEASURED. "It didn't work" would send them to the wrong place.
        message = str(caught.value)
        assert "no SEV-SNP attestation report could be obtained" in message
        assert "sev-guest" in message
        assert "vtpm" in message
