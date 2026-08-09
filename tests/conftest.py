"""Shared test fixtures.

**Tests must not touch the real data root.** Several modules resolve paths from
settings at call time — the attestation backend writes its signing key to
``settings.keystore_path`` — so a test that does not redirect the data root writes
into the developer's actual keystore directory, and in CI into
``/var/lib/confidentia``, which does not exist and is not writable.

That is exactly what happened: the attestation tests passed locally only because a
developer `.env` pointed the data root at a writable directory, while quietly
polluting the real keystore. They failed in CI for the right reason.

The autouse fixture below redirects the data root to a per-session temporary
directory and clears the settings cache, so every test runs against a throwaway
filesystem regardless of what `.env` says.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_data_root(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Point every settings-derived path at a temporary directory."""
    import os

    from confidentia.config.settings import get_settings

    root = tmp_path_factory.mktemp("confidentia-data")
    previous = {
        "CONFIDENTIA_DATA_ROOT": os.environ.get("CONFIDENTIA_DATA_ROOT"),
        "CONFIDENTIA_KEYSTORE_PASSPHRASE": os.environ.get("CONFIDENTIA_KEYSTORE_PASSPHRASE"),
    }
    os.environ["CONFIDENTIA_DATA_ROOT"] = str(root)
    # Deterministic and obviously fake: the keystore under test is a throwaway.
    os.environ.setdefault("CONFIDENTIA_KEYSTORE_PASSPHRASE", "test-suite-passphrase")

    # Settings are cached process-wide; the cache must be dropped for the new
    # environment to take effect, and again afterwards so a later import does not
    # see the temporary paths.
    get_settings.cache_clear()
    (root / "keystore").mkdir(parents=True, exist_ok=True)

    yield root

    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()
