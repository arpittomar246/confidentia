"""Print one business-key value from a landing file, for demos and smoke tests.

This needed no keys until ADR 0022, and the docstring used to say so approvingly:
landing was plaintext, so a value picked here was independent of the machinery
being demonstrated. That independence was the boundary violation the placement
planner found — anything that can read a business key from disk without a key can
read the identifiers next to it.

So it now requests keys like everything else. It still needs no Spark, which keeps
it fast enough for a demo, and pyarrow decrypts the part directly.
"""

from __future__ import annotations

import sys

import pyarrow.parquet as pq

from confidentia.catalog.contracts import get_contract
from confidentia.config.settings import get_settings
from confidentia.crypto.landing import LandingCrypto

tenant, dataset = sys.argv[1], sys.argv[2]
column = sys.argv[3] if len(sys.argv) > 3 else get_contract(tenant, dataset).business_key[0]

root = get_settings().lake_root / "landing" / tenant / dataset
files = sorted(root.rglob("*.parquet"))
if not files:
    raise SystemExit(f"no landing files under {root}")

crypto = LandingCrypto.for_tenant(tenant)
table = pq.read_table(
    files[0], columns=[column], decryption_properties=crypto.decryption_properties()
)
series = table.column(column).to_pandas().dropna()
if series.empty:
    raise SystemExit(f"{column} is entirely null in {files[0].name}")
print(series.iloc[0])
