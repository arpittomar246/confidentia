"""Tests for the key hierarchy, attestation, and schema contracts.

The properties asserted here are the ones the confidentiality claim rests on:
tenant keys are unrelated, derivation is deterministic, evidence cannot be replayed
or forged, and no identifier column can be misclassified into a weak key class.
"""

from __future__ import annotations

import dataclasses

import pytest

from confidentia.catalog.contracts import (
    CONTRACTS,
    ColumnSpec,
    ContractError,
    SchemaContract,
    all_contracts,
    get_contract,
    validate_contract,
)
from confidentia.config.settings import Sensitivity, Tenant
from confidentia.crypto.attestation import (
    AttestationBackend,
    AttestationError,
    AttestationVerifier,
    issue_nonce,
    measure_code,
)
from confidentia.crypto.keys import KeyHierarchy, KeyId, Keystore

pytestmark = pytest.mark.unit


@pytest.fixture
def keystore(tmp_path, monkeypatch) -> Keystore:
    monkeypatch.setenv("CONFIDENTIA_KEYSTORE_PASSPHRASE", "test-passphrase")
    store = Keystore(path=tmp_path, passphrase="test-passphrase")
    store.initialise()
    return store


class TestKeystore:
    def test_unseal_round_trips(self, keystore: Keystore) -> None:
        assert len(keystore.unseal()) == 32

    def test_wrong_passphrase_is_rejected(self, keystore: Keystore, tmp_path) -> None:
        from confidentia.crypto.keys import KeystoreError

        other = Keystore(path=tmp_path, passphrase="wrong-passphrase")
        with pytest.raises(KeystoreError, match="could not unseal"):
            other.unseal()

    def test_tampering_is_detected(self, keystore: Keystore, tmp_path) -> None:
        import json

        from confidentia.crypto.keys import KeystoreError

        path = tmp_path / "master.json"
        document = json.loads(path.read_text())
        blob = bytearray(__import__("base64").b64decode(document["wrapped_mek"]))
        blob[-1] ^= 0xFF  # break the GCM tag
        document["wrapped_mek"] = __import__("base64").b64encode(bytes(blob)).decode()
        path.write_text(json.dumps(document))

        fresh = Keystore(path=tmp_path, passphrase="test-passphrase")
        with pytest.raises(KeystoreError):
            fresh.unseal()

    def test_refuses_to_overwrite_without_force(self, keystore: Keystore) -> None:
        from confidentia.crypto.keys import KeystoreError

        # Overwriting makes every existing encrypted file permanently unreadable.
        with pytest.raises(KeystoreError, match="already exists"):
            keystore.initialise()

    def test_keystore_is_owner_only(self, keystore: Keystore) -> None:
        assert keystore.metadata()["owner_only"] is True

    def test_records_that_it_is_not_hardware_sealed(self, keystore: Keystore) -> None:
        # A passphrase-protected keystore must never be mistaken for a sealed one.
        assert keystore.metadata()["hardware_sealed"] is False


class TestKeyHierarchy:
    def test_derivation_is_deterministic(self, keystore: Keystore) -> None:
        hierarchy = KeyHierarchy(keystore)
        assert hierarchy.key_material("healthcare") == hierarchy.key_material("healthcare")

    def test_tenants_share_no_key_material(self, keystore: Keystore) -> None:
        hierarchy = KeyHierarchy(keystore)
        healthcare = set(hierarchy.key_material("healthcare").values())
        financial = set(hierarchy.key_material("financial").values())
        # Isolation is cryptographic, not a policy check that could be bypassed.
        assert not (healthcare & financial)

    def test_sensitivity_classes_get_distinct_keys(self, keystore: Keystore) -> None:
        hierarchy = KeyHierarchy(keystore)
        material = hierarchy.key_material("healthcare")
        assert len(set(material.values())) == len(material)

    def test_key_ids_are_stable(self) -> None:
        # Key ids are written into Parquet metadata; changing the format makes
        # previously written files unreadable.
        assert str(KeyId("healthcare", "phi")) == "k_healthcare_phi_v1"
        assert str(KeyId.footer("financial")) == "k_financial_footer_v1"

    def test_versions_produce_different_keys(self, keystore: Keystore) -> None:
        hierarchy = KeyHierarchy(keystore)
        v1 = hierarchy.column_key("healthcare", "phi", version=1)
        v2 = hierarchy.column_key("healthcare", "phi", version=2)
        assert v1 != v2

    def test_key_list_property_is_parseable(self, keystore: Keystore) -> None:
        hierarchy = KeyHierarchy(keystore)
        material = hierarchy.key_material("healthcare")
        rendered = hierarchy.key_list_property(material)
        entries = dict(part.split(":", 1) for part in rendered.split(","))
        assert set(entries) == set(material)


class TestAttestation:
    def test_measurement_is_stable_and_non_trivial(self) -> None:
        assert measure_code() == measure_code()
        assert len(measure_code()) == 64

    def test_valid_evidence_verifies(self) -> None:
        nonce = issue_nonce()
        evidence = AttestationBackend().attest(nonce)
        AttestationVerifier().verify(evidence, nonce)  # must not raise

    def test_replayed_nonce_is_rejected(self) -> None:
        evidence = AttestationBackend().attest(issue_nonce())
        with pytest.raises(AttestationError, match="nonce mismatch"):
            AttestationVerifier().verify(evidence, issue_nonce())

    def test_tampered_measurement_breaks_the_signature(self) -> None:
        nonce = issue_nonce()
        evidence = AttestationBackend().attest(nonce)
        forged = dataclasses.replace(evidence, measurement="0" * 64)
        with pytest.raises(AttestationError, match="signature does not verify"):
            AttestationVerifier().verify(forged, nonce)

    def test_stale_evidence_is_rejected(self) -> None:
        nonce = issue_nonce()
        evidence = AttestationBackend().attest(nonce)
        with pytest.raises(AttestationError, match="stale"):
            AttestationVerifier(max_age_seconds=0).verify(evidence, nonce)

    def test_simulated_evidence_cannot_claim_hardware_rooting(self) -> None:
        nonce = issue_nonce()
        evidence = AttestationBackend().attest(nonce)
        assert evidence.hardware_rooted is False
        lying = dataclasses.replace(evidence, hardware_rooted=True)
        # Caught by the signature first, which is the stronger guarantee.
        with pytest.raises(AttestationError):
            AttestationVerifier().verify(lying, nonce)

    def test_signature_covers_the_nonce(self) -> None:
        nonce = issue_nonce()
        evidence = AttestationBackend().attest(nonce)
        swapped = dataclasses.replace(evidence, nonce=issue_nonce())
        with pytest.raises(AttestationError):
            AttestationVerifier().verify(swapped, swapped.nonce)


class TestSchemaContracts:
    def test_every_shipped_contract_is_valid(self) -> None:
        for contract in all_contracts():
            validate_contract(contract)

    def test_identifier_columns_cannot_be_weakly_classified(self) -> None:
        bad = SchemaContract(
            dataset="x",
            tenant=Tenant.HEALTHCARE,
            version=1,
            business_key=("id",),
            columns=(
                ColumnSpec("id", "string", False, Sensitivity.INTERNAL),
                ColumnSpec("ssn", "string", True, Sensitivity.INTERNAL),
            ),
        )
        with pytest.raises(ContractError, match="direct identifier"):
            validate_contract(bad)

    def test_partition_columns_must_be_public(self) -> None:
        bad = SchemaContract(
            dataset="x",
            tenant=Tenant.HEALTHCARE,
            version=1,
            business_key=("id",),
            partition_columns=("region",),
            columns=(
                ColumnSpec("id", "string", False, Sensitivity.INTERNAL),
                ColumnSpec("region", "string", True, Sensitivity.PHI),
            ),
        )
        # An encrypted partition column makes every pruned query a full scan.
        with pytest.raises(ContractError, match="partition pruning"):
            validate_contract(bad)

    def test_business_key_must_exist(self) -> None:
        bad = SchemaContract(
            dataset="x",
            tenant=Tenant.HEALTHCARE,
            version=1,
            business_key=("missing",),
            columns=(ColumnSpec("id", "string", False, Sensitivity.INTERNAL),),
        )
        with pytest.raises(ContractError, match="business key"):
            validate_contract(bad)

    def test_column_key_mapping_omits_public_columns(self) -> None:
        contract = get_contract("healthcare", "encounter")
        key_ids = {
            Sensitivity.INTERNAL: "k_i",
            Sensitivity.PII: "k_pii",
            Sensitivity.PHI: "k_phi",
        }
        mapping = contract.column_keys_property(key_ids)
        # Public columns must be absent, which is what leaves them readable for
        # predicate pushdown.
        for public_column in contract.plaintext_columns:
            assert f",{public_column}," not in f",{mapping},"
        assert "k_phi:" in mapping

    def test_partition_column_is_plaintext_in_every_contract(self) -> None:
        for contract in all_contracts():
            for partition in contract.partition_columns:
                spec = contract.column(partition)
                if spec is not None:
                    assert spec.sensitivity is Sensitivity.PUBLIC

    def test_unknown_dataset_raises(self) -> None:
        with pytest.raises(ContractError, match="no contract"):
            get_contract("healthcare", "not_a_dataset")

    def test_tokenised_columns_are_at_least_pii(self) -> None:
        for contract in all_contracts():
            for name in contract.tokenised_columns:
                spec = contract.column(name)
                assert spec is not None
                assert spec.sensitivity in (Sensitivity.PII, Sensitivity.PHI)

    def test_contracts_cover_both_tenants(self) -> None:
        tenants = {contract.tenant for contract in CONTRACTS.values()}
        assert tenants == {Tenant.HEALTHCARE, Tenant.FINANCIAL}


def test_column_keys_property_format() -> None:
    contract = get_contract("financial", "transaction")
    mapping = contract.column_keys_property(
        {Sensitivity.INTERNAL: "kA", Sensitivity.PII: "kB", Sensitivity.PHI: "kC"}
    )
    for group in mapping.split(";"):
        key_id, columns = group.split(":", 1)
        assert key_id in {"kA", "kB", "kC"}
        assert columns
