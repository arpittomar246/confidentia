#!/usr/bin/env bash
# The full pipeline from nothing, in the one order that works.
#
# This is PROJECT_CONTEXT §9 as an executable script. It exists because the
# ordering carries three constraints that are easy to get wrong by hand:
#
#   * `keys approve` must come after any edit to a measured package, or key
#     release is refused for the whole run;
#   * Bronze quality must be the LAST quality run before `evaluate`, because the
#     detections table is overwritten per run and the evaluator refuses to score
#     Silver detections against the answer key;
#   * CDC needs its slot created before the OLTP source is mutated, or the
#     mutations happen with nothing listening and the change stream is empty.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
# shellcheck disable=SC1091
source .venv/bin/activate

: "${CONFIDENTIA_KEYSTORE_PASSPHRASE:?set CONFIDENTIA_KEYSTORE_PASSPHRASE first}"
ROWS="${ROWS:-60000}"
cli() { python -m confidentia.cli "$@"; }

step() {
    local name="$1"; shift
    echo
    echo "======== $name ========"
    if ! "$@"; then
        echo "FAILED: $name"
        exit 1
    fi
}

step "migrate"            cli db migrate

# The keystore deliberately survives a data reset -- it is the one thing in the
# data root that is not regenerable -- and `keys init` refuses to overwrite it.
# Initialise only when there genuinely is not one.
if [[ -f "${CONFIDENTIA_DATA_ROOT:-$HOME/confidentia-data}/keystore/master.json" ]]; then
    echo
    echo "======== keys init ========"
    echo "  keystore already present; keeping it"
else
    step "keys init"      cli keys init
fi
step "keys approve"       cli keys approve

step "generate healthcare" cli generate healthcare --rows "$ROWS"
step "generate financial"  cli generate financial  --rows "$ROWS"

step "ingest file healthcare" cli ingest file healthcare
step "ingest file financial"  cli ingest file financial

# The REST delivery of `encounter` is deliberate and load-bearing: it redelivers
# every record the file drop already landed, which is what gives Bronze its
# double delivery and Silver something real to deduplicate. It is also the case
# that made Bronze uniqueness meaningless before Stage 7c, so a rebuild without
# it would quietly remove the condition the fix addresses.
echo
echo "======== mock source API ========"
python -m uvicorn confidentia.ingestion.source_api:app --port 8081 --log-level warning &
SOURCE_API_PID=$!
trap 'kill "$SOURCE_API_PID" 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do
    curl -sf http://localhost:8081/health >/dev/null 2>&1 && break
    sleep 1
done
curl -sf http://localhost:8081/health >/dev/null || { echo "source API never came up"; exit 1; }
echo "  up on :8081"

step "ingest rest encounter" cli ingest rest healthcare encounter

kill "$SOURCE_API_PID" 2>/dev/null || true
trap - EXIT

step "cdc slot"            cli ingest slot create
step "oltp seed"           cli ingest oltp seed
step "cdc initial"         cli ingest cdc
step "oltp mutate"         cli ingest oltp mutate --limit 2000
step "cdc changes"         cli ingest cdc

step "bronze healthcare"   cli bronze write healthcare --full-refresh
step "bronze financial"    cli bronze write financial  --full-refresh

step "silver healthcare"   cli silver write healthcare
step "silver financial"    cli silver write financial
step "silver dimension"    cli silver dimension healthcare

step "gold healthcare"     cli gold build healthcare
step "gold financial"      cli gold build financial

# Bronze last, and nothing may run Silver quality after it.
step "quality healthcare"  cli quality run healthcare --layer bronze
step "quality financial"   cli quality run financial  --layer bronze

step "evaluate healthcare" cli evaluate quality healthcare
step "evaluate financial"  cli evaluate quality financial

echo
echo "pipeline rebuilt."
