"""Tests for landing-zone encryption (ADR 0022).

The hole these close was real and lived in the repository for eleven stages: the
landing zone held raw source records in plaintext Parquet, outside the boundary
Bronze onward maintains. What let it survive was that nothing checked. So the
assertions here are deliberately about *bytes on disk* rather than about the
configuration that produced them — a passing config test is exactly what the
original code would also have passed.

``LandingCrypto`` is built directly from a ``KeyHierarchy`` rather than through the
broker, so these stay unit tests. The broker path is one function call away and is
covered by the CLI and by the collectors; what needs isolating here is the crypto.
"""

from __future__ import annotations

import re

import pandas as pd
import pytest

from confidentia.catalog.contracts import ColumnSpec, SchemaContract, get_contract
from confidentia.config.settings import Sensitivity, Tenant
from confidentia.crypto.keys import KeyHierarchy, Keystore
from confidentia.crypto.landing import (
    PARQUET_ENCRYPTED_MAGIC,
    PARQUET_PLAIN_MAGIC,
    LandingCrypto,
    LandingEncryptionError,
    footer_magic,
    plaintext_parts,
    reencrypt_landing,
)
from confidentia.ingestion.base import (
    ENVELOPE_COLUMNS,
    add_envelope,
    landing_path,
    read_landing_part,
    write_landing,
)

pytestmark = pytest.mark.unit

SSN = re.compile(rb"[0-9]{3}-[0-9]{2}-[0-9]{4}")
TENANT = "healthcare"
DATASET = "patient"


@pytest.fixture
def hierarchy(tmp_path, monkeypatch) -> KeyHierarchy:
    monkeypatch.setenv("CONFIDENTIA_KEYSTORE_PASSPHRASE", "test-passphrase")
    store = Keystore(path=tmp_path / "keystore", passphrase="test-passphrase")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.initialise()
    return KeyHierarchy(keystore=store)


@pytest.fixture
def crypto(hierarchy: KeyHierarchy) -> LandingCrypto:
    return LandingCrypto(tenant=TENANT, material=hierarchy.key_material(TENANT))


@pytest.fixture
def contract() -> SchemaContract:
    return get_contract(TENANT, DATASET)


@pytest.fixture
def frame(contract: SchemaContract) -> pd.DataFrame:
    """A landing-shaped frame: contract columns plus the ingestion envelope."""
    import uuid

    rows = 40
    data = {}
    for spec in contract.columns:
        if spec.name == "ssn":
            data[spec.name] = [f"123-45-{i:04d}" for i in range(rows)]
        else:
            data[spec.name] = [f"{spec.name}-{i}" for i in range(rows)]
    return add_envelope(
        pd.DataFrame(data),
        run_id=uuid.uuid4(),
        batch_digest="a" * 64,
        source_file="test.parquet",
        source_system="test",
    )


class TestColumnKeyMap:
    def test_maps_by_contract_sensitivity(
        self, crypto: LandingCrypto, contract: SchemaContract
    ) -> None:
        mapping = crypto.column_key_map(contract)
        by_column = {col: key for key, cols in mapping.items() for col in cols}
        for spec in contract.columns:
            if spec.sensitivity is Sensitivity.PUBLIC:
                assert spec.name not in by_column
            else:
                assert by_column[spec.name] == crypto.key_id(spec.sensitivity)

    def test_public_columns_stay_readable(
        self, crypto: LandingCrypto, contract: SchemaContract
    ) -> None:
        # The omission is the mechanism that preserves predicate pushdown on
        # partition and filter columns, exactly as in Bronze (ADR 0007).
        encrypted = {col for cols in crypto.column_key_map(contract).values() for col in cols}
        assert not encrypted & set(contract.plaintext_columns)

    def test_undeclared_column_gets_the_strictest_key(
        self, crypto: LandingCrypto, contract: SchemaContract
    ) -> None:
        # E8 showed additive drift arriving unannounced. Under a
        # plaintext-by-default rule a new identifier column would land in the clear.
        present = set(contract.column_names) | {"ssn_alt"} | set(ENVELOPE_COLUMNS)
        mapping = crypto.column_key_map(contract, present)
        assert "ssn_alt" in mapping[crypto.strictest_key_id(contract)]

    def test_envelope_columns_are_not_encrypted(
        self, crypto: LandingCrypto, contract: SchemaContract
    ) -> None:
        # Ingestion's own provenance carries no source data, and leaving it
        # readable is what lets a part be attributed to a batch without a key.
        present = set(contract.column_names) | set(ENVELOPE_COLUMNS)
        encrypted = {
            col for cols in crypto.column_key_map(contract, present).values() for col in cols
        }
        assert not encrypted & set(ENVELOPE_COLUMNS)

    def test_all_public_contract_is_refused(self, crypto: LandingCrypto) -> None:
        harmless = SchemaContract(
            tenant=Tenant.HEALTHCARE,
            dataset="all_public",
            version=1,
            columns=(ColumnSpec("region", "string", True, Sensitivity.PUBLIC),),
            business_key=("region",),
            partition_columns=("region",),
        )
        with pytest.raises(LandingEncryptionError, match="protect nothing"):
            crypto.encryption_properties(harmless)


class TestWriteLanding:
    def test_writes_an_encrypted_footer(
        self, tmp_path, crypto: LandingCrypto, contract: SchemaContract, frame: pd.DataFrame
    ) -> None:
        path = write_landing(
            frame,
            lake_root=tmp_path,
            tenant=TENANT,
            dataset=DATASET,
            batch_digest="b" * 64,
            part=0,
            crypto=crypto,
            contract=contract,
        )
        assert footer_magic(path) == PARQUET_ENCRYPTED_MAGIC

    def test_identifiers_are_absent_from_the_raw_bytes(
        self, tmp_path, crypto: LandingCrypto, contract: SchemaContract, frame: pd.DataFrame
    ) -> None:
        # The check that found the hole. Nineteen SSN-shaped strings turned up in a
        # real landing patient file when this was plaintext.
        path = write_landing(
            frame,
            lake_root=tmp_path,
            tenant=TENANT,
            dataset=DATASET,
            batch_digest="c" * 64,
            part=0,
            crypto=crypto,
            contract=contract,
        )
        assert SSN.findall(path.read_bytes()) == []

    def test_round_trips_with_keys(
        self, tmp_path, crypto: LandingCrypto, contract: SchemaContract, frame: pd.DataFrame
    ) -> None:
        path = write_landing(
            frame,
            lake_root=tmp_path,
            tenant=TENANT,
            dataset=DATASET,
            batch_digest="d" * 64,
            part=0,
            crypto=crypto,
            contract=contract,
        )
        back = read_landing_part(path, crypto)
        assert len(back) == len(frame)
        assert back["ssn"].tolist() == frame["ssn"].tolist()

    def test_a_keyless_read_is_refused(
        self, tmp_path, crypto: LandingCrypto, contract: SchemaContract, frame: pd.DataFrame
    ) -> None:
        import pyarrow.parquet as pq

        path = write_landing(
            frame,
            lake_root=tmp_path,
            tenant=TENANT,
            dataset=DATASET,
            batch_digest="e" * 64,
            part=0,
            crypto=crypto,
            contract=contract,
        )
        # Refused, not silently empty. A read that returns zero rows would look
        # like an empty batch to anything downstream.
        with pytest.raises(Exception):  # noqa: B017 - any refusal is correct
            pq.read_table(path)

    def test_a_tenants_keys_do_not_open_another_tenants_part(
        self,
        tmp_path,
        crypto: LandingCrypto,
        hierarchy: KeyHierarchy,
        contract: SchemaContract,
        frame: pd.DataFrame,
    ) -> None:
        import pyarrow.parquet as pq

        path = write_landing(
            frame,
            lake_root=tmp_path,
            tenant=TENANT,
            dataset=DATASET,
            batch_digest="f" * 64,
            part=0,
            crypto=crypto,
            contract=contract,
        )
        # Tenant isolation is cryptographic, not policy: the financial KEK derives
        # from a different HKDF info string, so its keys are unrelated. Same
        # keystore, same MEK, and still no access.
        other = LandingCrypto(
            tenant="financial", material=hierarchy.key_material("financial")
        )
        with pytest.raises(Exception):  # noqa: B017
            pq.read_table(path, decryption_properties=other.decryption_properties())

    def test_a_dataset_without_a_contract_is_refused(
        self, tmp_path, crypto: LandingCrypto, frame: pd.DataFrame
    ) -> None:
        from confidentia.catalog.contracts import ContractError

        # How healthcare/patient_change spent months in the landing zone
        # unclassified: Bronze refused it, and nothing upstream did.
        with pytest.raises(ContractError, match="no schema contract"):
            write_landing(
                frame,
                lake_root=tmp_path,
                tenant=TENANT,
                dataset="not_a_dataset",
                batch_digest="0" * 64,
                part=0,
                crypto=crypto,
            )


class TestPlaintextDetection:
    def test_finds_a_plaintext_part(self, tmp_path, frame: pd.DataFrame) -> None:
        target = landing_path(tmp_path, TENANT, DATASET) / "batch=deadbeef"
        target.mkdir(parents=True)
        path = target / "part-00000.parquet"
        frame.to_parquet(path, index=False)

        assert footer_magic(path) == PARQUET_PLAIN_MAGIC
        assert plaintext_parts(tmp_path / "landing") == [path]

    def test_says_nothing_when_the_zone_is_clean(
        self, tmp_path, crypto: LandingCrypto, contract: SchemaContract, frame: pd.DataFrame
    ) -> None:
        write_landing(
            frame,
            lake_root=tmp_path,
            tenant=TENANT,
            dataset=DATASET,
            batch_digest="1" * 64,
            part=0,
            crypto=crypto,
            contract=contract,
        )
        assert plaintext_parts(tmp_path / "landing") == []


class TestMigration:
    def test_rewrites_plaintext_in_place(
        self, tmp_path, crypto: LandingCrypto, frame: pd.DataFrame, monkeypatch
    ) -> None:
        target = landing_path(tmp_path, TENANT, DATASET) / "batch=deadbeef"
        target.mkdir(parents=True)
        path = target / "part-00000.parquet"
        frame.to_parquet(path, index=False)
        assert SSN.findall(path.read_bytes())

        monkeypatch.setattr(LandingCrypto, "for_tenant", classmethod(lambda cls, *a, **k: crypto))
        results = reencrypt_landing(tmp_path)

        assert [r.applied for r in results] == [True]
        assert results[0].rows == len(frame)
        assert footer_magic(path) == PARQUET_ENCRYPTED_MAGIC
        assert SSN.findall(path.read_bytes()) == []
        assert read_landing_part(path, crypto)["ssn"].tolist() == frame["ssn"].tolist()

    def test_the_partition_key_is_not_written_into_the_part(
        self, tmp_path, crypto: LandingCrypto, frame: pd.DataFrame, monkeypatch
    ) -> None:
        """The migration changes encryption and nothing else.

        It once changed the schema too. Parts live under `batch=<digest>/`, and
        `pq.read_table` reads a path as a one-file dataset with hive partitioning
        on by default -- so the directory name came back as a column, and the
        rewrite wrote it into the file. Every migrated part gained a `batch`
        column that collided with its own partition directory, which is what made
        Spark warn COLUMN_ALREADY_EXISTS on a landing read.
        """
        import pyarrow.parquet as pq

        target = landing_path(tmp_path, TENANT, DATASET) / "batch=deadbeef"
        target.mkdir(parents=True)
        path = target / "part-00000.parquet"
        frame.to_parquet(path, index=False)

        monkeypatch.setattr(LandingCrypto, "for_tenant", classmethod(lambda cls, *a, **k: crypto))
        reencrypt_landing(tmp_path)

        written = pq.ParquetFile(
            path, decryption_properties=crypto.decryption_properties()
        ).schema_arrow.names
        assert "batch" not in written
        assert written == list(frame.columns)
        # And the reader hands back the file's own columns, not the dataset view's.
        assert list(read_landing_part(path, crypto).columns) == list(frame.columns)

    def test_dry_run_changes_nothing(self, tmp_path, frame: pd.DataFrame) -> None:
        target = landing_path(tmp_path, TENANT, DATASET) / "batch=deadbeef"
        target.mkdir(parents=True)
        path = target / "part-00000.parquet"
        frame.to_parquet(path, index=False)

        results = reencrypt_landing(tmp_path, dry_run=True)
        assert [r.applied for r in results] == [False]
        assert footer_magic(path) == PARQUET_PLAIN_MAGIC

    def test_is_idempotent(
        self, tmp_path, crypto: LandingCrypto, frame: pd.DataFrame, monkeypatch
    ) -> None:
        target = landing_path(tmp_path, TENANT, DATASET) / "batch=deadbeef"
        target.mkdir(parents=True)
        (target / "part-00000.parquet").write_bytes(b"")
        frame.to_parquet(target / "part-00000.parquet", index=False)

        monkeypatch.setattr(LandingCrypto, "for_tenant", classmethod(lambda cls, *a, **k: crypto))
        assert len(reencrypt_landing(tmp_path)) == 1
        # An already-encrypted part is never selected, so a re-run is a no-op
        # rather than a double encryption that would make the file unreadable.
        assert reencrypt_landing(tmp_path) == []

    def test_unclassified_dataset_is_encrypted_not_skipped(
        self, tmp_path, crypto: LandingCrypto, monkeypatch
    ) -> None:
        # healthcare/patient_change, exactly: real rows, no contract, never
        # protected because nothing knew it was there.
        target = landing_path(tmp_path, TENANT, "patient_change") / "batch=deadbeef"
        target.mkdir(parents=True)
        path = target / "part-00000.parquet"
        pd.DataFrame(
            {
                "change_id": range(10),
                "new_value": [f"123-45-{i:04d}" for i in range(10)],
                "_batch_digest": ["x" * 64] * 10,
            }
        ).to_parquet(path, index=False)

        monkeypatch.setattr(LandingCrypto, "for_tenant", classmethod(lambda cls, *a, **k: crypto))
        results = reencrypt_landing(tmp_path)

        assert [r.unclassified for r in results] == [True]
        assert footer_magic(path) == PARQUET_ENCRYPTED_MAGIC
        assert SSN.findall(path.read_bytes()) == []
