"""Tests for deterministic tokenisation.

Determinism is what makes tokens joinable, and field binding is what stops two
different attributes collapsing into one identity. Both are load-bearing, so both
are asserted rather than assumed.
"""

from __future__ import annotations

import pytest

from confidentia.crypto.keys import Keystore
from confidentia.crypto.tokenisation import (
    LOW_CARDINALITY_FIELDS,
    TOKEN_HEX_LENGTH,
    TokenisationError,
    Tokeniser,
    normalise,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def tokeniser(tmp_path, monkeypatch) -> Tokeniser:
    from confidentia.crypto.keys import KeyHierarchy

    monkeypatch.setenv("CONFIDENTIA_KEYSTORE_PASSPHRASE", "test-passphrase")
    store = Keystore(path=tmp_path, passphrase="test-passphrase")
    store.initialise()
    return Tokeniser("healthcare", hierarchy=KeyHierarchy(store))


class TestNormalisation:
    @pytest.mark.parametrize(
        "raw",
        ["MRN000123", "  MRN000123  ", "mrn000123", "MRN000123\t", "MRN  000123"],
    )
    def test_cosmetic_variants_normalise_together(self, raw: str) -> None:
        # Whitespace and case defects are injected in Stage 2 and repaired in
        # conform; this is the second line of defence. Without it the same person
        # tokenises into several identities.
        assert normalise(raw) == normalise("mrn 000123") or normalise(raw) == "mrn000123"

    def test_unicode_is_canonicalised(self) -> None:
        # NFKC: composed and decomposed forms of the same character must agree.
        assert normalise("José") == normalise("José")


class TestTokenDeterminism:
    def test_same_value_gives_same_token(self, tokeniser: Tokeniser) -> None:
        assert tokeniser.token_for("MRN000123", "mrn") == tokeniser.token_for(
            "MRN000123", "mrn"
        )

    def test_cosmetic_variants_give_the_same_token(self, tokeniser: Tokeniser) -> None:
        # This is what lets a patient in `encounter` join to the same patient in
        # `claim_line` after de-identification.
        assert tokeniser.token_for("  mrn000123 ", "mrn") == tokeniser.token_for(
            "MRN000123", "mrn"
        )

    def test_different_values_give_different_tokens(self, tokeniser: Tokeniser) -> None:
        assert tokeniser.token_for("MRN000123", "mrn") != tokeniser.token_for(
            "MRN000124", "mrn"
        )

    def test_same_value_in_different_fields_does_not_collide(
        self, tokeniser: Tokeniser
    ) -> None:
        # An email and a phone number that happened to match must not merge two
        # entities, so the field name is part of the HMAC input.
        assert tokeniser.token_for("12345", "phone") != tokeniser.token_for("12345", "ssn")

    def test_tokens_differ_between_tenants(self, tmp_path, monkeypatch) -> None:
        from confidentia.crypto.keys import KeyHierarchy

        monkeypatch.setenv("CONFIDENTIA_KEYSTORE_PASSPHRASE", "test-passphrase")
        store = Keystore(path=tmp_path, passphrase="test-passphrase")
        store.initialise()
        healthcare = Tokeniser("healthcare", hierarchy=KeyHierarchy(store))
        financial = Tokeniser("financial", hierarchy=KeyHierarchy(store))
        # Cross-tenant correlation must be impossible even for identical values.
        assert healthcare.token_for("x@example.com", "email") != financial.token_for(
            "x@example.com", "email"
        )

    def test_null_passes_through(self, tokeniser: Tokeniser) -> None:
        assert tokeniser.token_for(None, "mrn") is None

    def test_token_shape(self, tokeniser: Tokeniser) -> None:
        token = tokeniser.token_for("MRN000123", "mrn")
        assert token is not None
        prefix, field, digest = token.split("_", 2)
        assert prefix == "tk"
        assert field == "mrn"
        assert len(digest) == TOKEN_HEX_LENGTH


class TestLowCardinalityRefusal:
    @pytest.mark.parametrize("field", sorted(LOW_CARDINALITY_FIELDS))
    def test_low_cardinality_fields_are_refused(self, field: str) -> None:
        # Deterministic tokens over a small domain can be brute-forced by anyone
        # holding the key. These are protected by column encryption instead.
        with pytest.raises(TokenisationError, match="too small a domain"):
            Tokeniser.assert_tokenisable(field)

    @pytest.mark.parametrize("field", ["mrn", "ssn", "email", "phone", "last_name"])
    def test_high_cardinality_fields_are_allowed(self, field: str) -> None:
        Tokeniser.assert_tokenisable(field)

    def test_contracts_never_tokenise_a_low_cardinality_field(self) -> None:
        from confidentia.catalog.contracts import all_contracts

        for contract in all_contracts():
            for name in contract.tokenised_columns:
                assert name not in LOW_CARDINALITY_FIELDS, (
                    f"{contract.dataset}.{name} is marked for tokenisation but its "
                    "domain is small enough to enumerate"
                )


class TestKeySeparation:
    def test_token_and_vault_keys_differ(self, tokeniser: Tokeniser) -> None:
        # Compromising the token key must not also decrypt the vault.
        assert tokeniser._token_key() != tokeniser._vault_key()

    def test_token_key_differs_from_column_keys(self, tokeniser: Tokeniser) -> None:
        column_keys = set(tokeniser.hierarchy.key_material("healthcare").values())
        assert tokeniser._token_key() not in column_keys
