#!/usr/bin/env bash
# Prove the promotion gate is a control and not a report.
#
# The gate's entire mechanism is its exit status: the orchestrator runs it as a
# task and reads the return code. A gate that prints "BLOCKED" and exits 0 is
# worse than no gate, because the DAG goes green and everyone downstream believes
# the data was checked. That failure mode is invisible in a log and invisible in
# the UI, so it is asserted here.
#
# Four properties, each the negation of a plausible bug:
#   1. a passing score exits 0
#   2. a failing score exits non-zero
#   3. an override exits 0 -- and is recorded as an override, not as a pass
#   4. an override without a reason is refused
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
# shellcheck disable=SC1091
source .venv/bin/activate

: "${CONFIDENTIA_KEYSTORE_PASSPHRASE:?set CONFIDENTIA_KEYSTORE_PASSPHRASE first}"

TENANT="${TENANT:-healthcare}"
DATASET="${DATASET:-claim_line}"
cli() { python -m confidentia.cli "$@" >/dev/null 2>&1; }

failures=0
check() {
    local name="$1" expected="$2" actual="$3"
    if [[ "$actual" == "$expected" ]]; then
        echo "  PASS  $name (exit $actual)"
    else
        echo "  FAIL  $name: expected exit $expected, got $actual"
        failures=$((failures + 1))
    fi
}

echo "=== promotion gate: exit status is the control ==="

# 1. Threshold well below the real score.
DQ_PROMOTION_THRESHOLD=0.10 cli quality gate "$TENANT" "$DATASET"
check "passing score exits 0" 0 $?

# 2. Threshold above any achievable score. Must fail the task.
DQ_PROMOTION_THRESHOLD=0.999999 cli quality gate "$TENANT" "$DATASET"
check "failing score exits non-zero" 1 $?

# 3. Same failing threshold, explicitly overridden.
DQ_PROMOTION_THRESHOLD=0.999999 cli quality gate "$TENANT" "$DATASET" \
    --override --reason "verify_gate.sh"
check "audited override exits 0" 0 $?

# 4. An override with no reason is a silent bypass and must be refused.
#    Typer reports a bad parameter as exit 2.
DQ_PROMOTION_THRESHOLD=0.999999 cli quality gate "$TENANT" "$DATASET" --override
check "override without a reason is refused" 2 $?

echo
echo "=== the decisions were recorded, including the ones that passed ==="
PGPASSWORD="${POSTGRES_PASSWORD:-confidentia}" psql -h "${POSTGRES_HOST:-localhost}" \
    -U "${POSTGRES_USER:-confidentia}" -d "${POSTGRES_DATABASE:-confidentia}" -x -c \
    "SELECT decision, blocked, overridden, round(overall_score::numeric,4) AS score,
            threshold, left(coalesce(reason,''), 60) AS reason
       FROM platform.promotion_gate
      WHERE tenant = '$TENANT' AND dataset = '$DATASET'
      ORDER BY decided_at DESC LIMIT 4;"

echo "=== a block is visible in platform.run, not only in the gate table ==="
PGPASSWORD="${POSTGRES_PASSWORD:-confidentia}" psql -h "${POSTGRES_HOST:-localhost}" \
    -U "${POSTGRES_USER:-confidentia}" -d "${POSTGRES_DATABASE:-confidentia}" -c \
    "SELECT status, error_type, left(coalesce(error_message,''), 50) AS error
       FROM platform.run
      WHERE source = 'gate' AND dataset = '$DATASET'
      ORDER BY started_at DESC LIMIT 4;"

echo
if [[ $failures -gt 0 ]]; then
    echo "promotion gate FAILED ($failures of 4)"
    exit 1
fi
echo "promotion gate holds: it stops the pipeline rather than reporting on it."
