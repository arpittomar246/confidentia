#!/usr/bin/env bash
# Re-prove the landing zone's encryption guarantee (ADR 0022) from the bytes.
#
# The landing zone was plaintext for eleven stages and nobody noticed, because
# nothing looked. This looks, and it looks at the real lake rather than at a spike
# fixture: footer magic, raw-byte patterns, a keyless read, and a Spark read that
# proves pandas and Spark still agree on the key material format.
#
# Each probe is its own OS process. Parquet's KeyToolkit caches unwrapped keys
# statically per JVM and PySpark does not restart the JVM between sessions, so an
# in-process retry would read from cache and falsely pass.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# shellcheck disable=SC1091
source .venv/bin/activate

WITH_SPARK=1
if [[ "${1:-}" == "--no-spark" ]]; then
    WITH_SPARK=0
fi

failures=0
run() {
    local probe="$1"
    echo "======================================================================"
    echo "==> ${probe}"
    python scripts/landing_probes.py "${probe}" 2>/dev/null
    local rc=$?
    echo "  exit=${rc}"
    return $rc
}

run magic   || failures=$((failures + 1))
run bytes   || failures=$((failures + 1))
run keyless || failures=$((failures + 1))

if [[ $WITH_SPARK -eq 1 ]]; then
    # Costs a Spark session (~40s). Skippable in a fast loop, never in CI: it is
    # the only probe that checks pyarrow and Spark still write the same envelope.
    run spark || failures=$((failures + 1))
else
    echo "======================================================================"
    echo "==> spark  SKIPPED (--no-spark)"
fi

echo "======================================================================"
if [[ $failures -gt 0 ]]; then
    echo "LANDING ENCRYPTION GUARANTEE FAILED (${failures} probe(s))"
    echo ""
    echo "If files are plaintext, they predate ADR 0022. Fix without re-ingesting:"
    echo "    python -m confidentia.cli landing encrypt --apply"
    exit 1
fi
echo "Landing encryption guarantee holds."
