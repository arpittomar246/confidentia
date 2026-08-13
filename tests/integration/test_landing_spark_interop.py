"""Spark must read what pandas wrote (ADR 0022). Requires a JVM.

This is the load-bearing assumption of the landing-zone fix and the only part of it
that is not obviously true. Landing parts are written by pyarrow's ``CryptoFactory``
and read by Bronze through Spark's Java ``PropertiesDrivenCryptoFactory``. The two
interoperate because both implement the Parquet key-tools ``PKMT1`` key material
format, and because ``crypto/landing.py`` and ``crypto/java/LocalKmsClient.java``
wrap data keys with a byte-identical envelope — AES-256-GCM, ``base64(nonce ||
ciphertext || tag)``, master key identifier as AAD.

Nothing enforces that agreement but this test. A change to either wrapping routine
would leave every landing part written afterwards unreadable by Bronze, and the
failure would surface at the next promotion rather than at the change — so the
round trip is checked in both directions on every CI run that has a JVM.
"""

from __future__ import annotations

import uuid

import pandas as pd
import pytest

from confidentia.catalog.contracts import get_contract
from confidentia.crypto.keys import KeyHierarchy, Keystore
from confidentia.crypto.landing import LandingCrypto
from confidentia.ingestion.base import add_envelope, read_landing_part, write_landing

pytestmark = [pytest.mark.integration, pytest.mark.spark]

TENANT = "healthcare"
DATASET = "patient"
ROWS = 60


@pytest.fixture(scope="module")
def spark_session():
    from confidentia.spark.session import build_session

    session = build_session("test-landing-interop")
    yield session
    session.stop()


@pytest.fixture
def crypto(tmp_path, monkeypatch) -> LandingCrypto:
    monkeypatch.setenv("CONFIDENTIA_KEYSTORE_PASSPHRASE", "test-passphrase")
    store = Keystore(path=tmp_path / "keystore", passphrase="test-passphrase")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.initialise()
    return LandingCrypto(
        tenant=TENANT, material=KeyHierarchy(keystore=store).key_material(TENANT)
    )


@pytest.fixture
def landed(tmp_path, crypto: LandingCrypto):
    contract = get_contract(TENANT, DATASET)
    frame = pd.DataFrame(
        {
            spec.name: (
                [f"123-45-{i:04d}" for i in range(ROWS)]
                if spec.name == "ssn"
                else [f"{spec.name}-{i}" for i in range(ROWS)]
            )
            for spec in contract.columns
        }
    )
    enveloped = add_envelope(
        frame,
        run_id=uuid.uuid4(),
        batch_digest="a" * 64,
        source_file="interop.parquet",
        source_system="test",
    )
    path = write_landing(
        enveloped,
        lake_root=tmp_path,
        tenant=TENANT,
        dataset=DATASET,
        batch_digest="a" * 64,
        part=0,
        crypto=crypto,
        contract=contract,
    )
    return path, enveloped, contract


def _configure(session, crypto: LandingCrypto) -> None:
    """Install the same key material on the Hadoop Configuration Bronze uses."""
    import base64

    from confidentia.spark.session import (
        CRYPTO_FACTORY,
        KMS_CLIENT,
        PROP_FACTORY,
        PROP_KEY_LIST,
        PROP_KMS_CLIENT,
    )

    hadoop_conf = session.sparkContext._jsc.hadoopConfiguration()
    hadoop_conf.set(PROP_FACTORY, CRYPTO_FACTORY)
    hadoop_conf.set(PROP_KMS_CLIENT, KMS_CLIENT)
    # Never on SparkConf: it is rendered in full in the Spark UI's Environment tab.
    hadoop_conf.set(
        PROP_KEY_LIST,
        ",".join(
            f"{key_id}:{base64.b64encode(key).decode()}"
            for key_id, key in crypto.material.items()
        ),
    )


class TestSparkReadsWhatPandasWrote:
    def test_row_count_and_protected_values_survive(
        self, spark_session, crypto: LandingCrypto, landed
    ) -> None:
        path, expected, _ = landed
        _configure(spark_session, crypto)

        frame = spark_session.read.parquet(str(path.parent))
        assert frame.count() == ROWS

        # A row count alone proves only that the footer unwrapped — counts live in
        # metadata. Materialising an encrypted column proves the column keys did too.
        rows = frame.select("ssn").orderBy("ssn").collect()
        assert [r["ssn"] for r in rows] == sorted(expected["ssn"].tolist())

    def test_pyarrow_reads_it_back_too(self, crypto: LandingCrypto, landed) -> None:
        # The other direction, so a one-sided change cannot pass by breaking both.
        path, expected, _ = landed
        assert read_landing_part(path, crypto)["ssn"].tolist() == expected["ssn"].tolist()

    def test_spark_is_refused_without_key_material(self, spark_session, landed) -> None:
        from confidentia.spark.session import PROP_KEY_LIST

        path, _, _ = landed
        # Landing writes an encrypted footer, so a keyless reader cannot even
        # discover the schema — PUBLIC columns stay unencrypted for pushdown once
        # the footer opens, not before it.
        #
        # Deliberately last: it clears key material from the module-scoped
        # session's Hadoop Configuration, and anything after it would be reading
        # against a session it did not configure.
        hadoop_conf = spark_session.sparkContext._jsc.hadoopConfiguration()
        hadoop_conf.unset(PROP_KEY_LIST)
        with pytest.raises(Exception):  # noqa: B017 - any refusal is correct
            spark_session.read.parquet(str(path.parent)).count()
