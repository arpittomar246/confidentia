"""Time the three granularities the ADR chooses between, on the same table.

The point is to make the granularity decision a result rather than an assertion.
Per-file digesting reads ciphertext and needs no keys and no Spark; per-record
digesting needs both, and buys the only thing that answers "prove *this row* was in
the committed set".

    python scripts/lineage_granularity_bench.py <tenant> <dataset> [layer]
"""

from __future__ import annotations

import sys
import time

from confidentia.common.hashing import sha256_directory, sha256_file
from confidentia.config.settings import Layer, Tenant, get_settings
from confidentia.lineage.merkle import MerkleTree
from confidentia.lineage.recorder import leaf_digests
from confidentia.spark.session import session_for_tenant

tenant, dataset = sys.argv[1], sys.argv[2]
layer = sys.argv[3] if len(sys.argv) > 3 else "bronze"
path = get_settings().layer_path(Layer(layer), Tenant(tenant), dataset)

files = [p for p in path.rglob("*.parquet")]
size_mb = sum(p.stat().st_size for p in files) / 1024 / 1024

start = time.perf_counter()
whole = sha256_directory(path, "**/*.parquet")
per_run_ms = (time.perf_counter() - start) * 1000

start = time.perf_counter()
per_file = MerkleTree(sorted(sha256_file(p) for p in files)).root
per_file_ms = (time.perf_counter() - start) * 1000

with session_for_tenant(tenant, app_name="confidentia-granularity-bench") as (spark, _keys):
    frame = spark.read.format("delta").load(str(path))
    frame.cache()
    count = frame.count()
    start = time.perf_counter()
    leaves = leaf_digests(frame)
    root = MerkleTree(leaves).root
    per_record_ms = (time.perf_counter() - start) * 1000
    frame.unpersist()

print()
print(f"{tenant}/{dataset}@{layer}: {count:,} records, {len(files)} files, {size_mb:.1f} MB")
print(f"  per-run    (one digest over the directory)  {per_run_ms:9.0f} ms   {whole[:16]}")
print(f"  per-file   (Merkle over file digests)       {per_file_ms:9.0f} ms   {per_file[:16]}")
print(f"  per-record (Merkle over row digests)        {per_record_ms:9.0f} ms   {root[:16]}")
print()
print(f"  per-record costs {per_record_ms / per_file_ms:.0f}x per-file, and is the only")
print("  one of the three that can prove a single row belongs to the committed set.")
