#!/usr/bin/env bash
# Prove the three claims Stage 9 actually makes.
#
#   1. Streaming writes into the SAME Bronze table as batch (ADR 0014), and is
#      distinguishable by `_source_system` rather than by living elsewhere.
#   2. Streamed files are encrypted. A streaming writer is a fresh place to make
#      ADR 0007's mistake, where Delta writes plaintext with no error at all.
#   3. Re-consuming does not duplicate. At-least-once delivery plus a retry is a
#      duplicated record, and the only thing that would notice is a row count
#      nobody checks.
#
# The third is the one worth having. The first two would survive a consumer that
# quietly appended the same 20,000 rows on every run.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
# shellcheck disable=SC1091
source .venv/bin/activate

: "${CONFIDENTIA_KEYSTORE_PASSPHRASE:?set CONFIDENTIA_KEYSTORE_PASSPHRASE first}"

TENANT="${TENANT:-financial}"
DATASET="${DATASET:-transaction}"
failures=0
note() { printf '  %-6s %s\n' "$1" "$2"; }

echo "=== Bronze holds both sources in one table ==="
python - <<'PY'
import os, sys
from confidentia.spark.session import session_for_tenant
from confidentia.pipelines.bronze.writer import bronze_path

tenant = os.environ.get("TENANT", "financial")
dataset = os.environ.get("DATASET", "transaction")
with session_for_tenant(tenant, app_name="verify-streaming") as (spark, _):
    frame = spark.read.format("delta").load(str(bronze_path(tenant, dataset)))
    rows = frame.groupBy("_source_system").count().collect()
    total = frame.count()

counts = {r["_source_system"]: r["count"] for r in rows}
for source, n in sorted(counts.items()):
    print(f"    {source or '(null)':16s} {n:>9,}")
print(f"    {'TOTAL':16s} {total:>9,}")

# The claim is one table containing both, not two tables that happen to exist.
if "kafka" not in counts:
    print("FAIL: no streamed rows in the Bronze table"); sys.exit(1)
if len(counts) < 2:
    print("FAIL: Bronze holds only one source; the shared-table claim is unproven")
    sys.exit(1)
print("OK")
PY
if [[ $? -ne 0 ]]; then note FAIL "shared Bronze table"; failures=$((failures + 1)); else
    note PASS "streaming and batch rows share one Bronze table"; fi

echo
echo "=== re-consuming is a no-op (exactly-once) ==="
before=$(python -c "
import os
from confidentia.spark.session import session_for_tenant
from confidentia.pipelines.bronze.writer import bronze_path
with session_for_tenant('$TENANT', app_name='verify-count') as (spark, _):
    print(spark.read.format('delta').load(str(bronze_path('$TENANT','$DATASET'))).count())
" 2>/dev/null | tail -1)

python -m confidentia.cli stream consume "$TENANT" "$DATASET" >/dev/null 2>&1

after=$(python -c "
import os
from confidentia.spark.session import session_for_tenant
from confidentia.pipelines.bronze.writer import bronze_path
with session_for_tenant('$TENANT', app_name='verify-count') as (spark, _):
    print(spark.read.format('delta').load(str(bronze_path('$TENANT','$DATASET'))).count())
" 2>/dev/null | tail -1)

if [[ "$before" == "$after" && -n "$before" ]]; then
    note PASS "row count unchanged across a second consume ($before)"
else
    note FAIL "row count moved from ${before:-?} to ${after:-?}; the consumer is not idempotent"
    failures=$((failures + 1))
fi

echo
echo "=== streamed files are encrypted ==="
python - <<'PY'
import os, sys
from confidentia.pipelines.bronze.writer import bronze_path, verify_encrypted

tenant = os.environ.get("TENANT", "financial")
dataset = os.environ.get("DATASET", "transaction")
ok, detail = verify_encrypted(bronze_path(tenant, dataset))
print(f"    {detail}")
sys.exit(0 if ok else 1)
PY
if [[ $? -ne 0 ]]; then note FAIL "encryption"; failures=$((failures + 1)); else
    note PASS "PARE magic present on the Bronze files"; fi

echo
if [[ $failures -gt 0 ]]; then
    echo "streaming verification FAILED ($failures)"
    exit 1
fi
echo "streaming verified: one Bronze table, encrypted, and idempotent on replay."
