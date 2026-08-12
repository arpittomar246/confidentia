"""Print one business-key value from a landing file, for demos and smoke tests.

Landing is plaintext parquet, so this needs no keys and no Spark -- which is the
point: a value picked here is independent of the machinery being demonstrated.
"""

from __future__ import annotations

import sys

import pandas as pd

from confidentia.catalog.contracts import get_contract
from confidentia.config.settings import get_settings

tenant, dataset = sys.argv[1], sys.argv[2]
column = sys.argv[3] if len(sys.argv) > 3 else get_contract(tenant, dataset).business_key[0]

root = get_settings().lake_root / "landing" / tenant / dataset
files = sorted(root.rglob("*.parquet"))
if not files:
    raise SystemExit(f"no landing files under {root}")

frame = pd.read_parquet(files[0], columns=[column])
print(frame[column].dropna().iloc[0])
