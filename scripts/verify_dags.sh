#!/usr/bin/env bash
# Parse and execute every DAG, and assert the promotion gate is a control.
#
# `airflow dags test` runs a DAG in-process against the real database and the real
# lake -- no scheduler, no webserver, no queue. That makes it the honest check for
# this project: it exercises the same tasks the scheduler would, on a 3.9 GB box,
# without a second set of processes competing with the Spark driver for memory.
#
# The last check is the one worth having. Every other property here would survive a
# gate that printed "BLOCKED" and returned 0; that one would not.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
# shellcheck disable=SC1091
source scripts/airflow_env.sh

: "${CONFIDENTIA_KEYSTORE_PASSPHRASE:?set CONFIDENTIA_KEYSTORE_PASSPHRASE first}"

LOGICAL_DATE="${LOGICAL_DATE:-2026-08-09}"

# The restatement sensor waits fifty minutes in production. Verification cannot,
# so it is shortened here -- the only behaviour this script changes, and it changes
# how long the sensor waits, not what it decides.
export CONFIDENTIA_RESTATEMENT_SENSOR_TIMEOUT="${CONFIDENTIA_RESTATEMENT_SENSOR_TIMEOUT:-20}"
export CONFIDENTIA_RESTATEMENT_SENSOR_POKE="${CONFIDENTIA_RESTATEMENT_SENSOR_POKE:-5}"
DAGS=(
    confidentia_ingest
    confidentia_bronze_silver
    confidentia_gold
    confidentia_quality
    confidentia_restatement
)

failures=0
note() { printf '  %-8s %s\n' "$1" "$2"; }

echo "=== every DAG parses, with no import errors ==="
# Matched on Airflow's own "no rows" marker rather than by counting lines that
# look like paths: the command's INFO preamble mentions .py files, so a line count
# reports import errors that do not exist.
if airflow dags list-import-errors 2>/dev/null | grep -q "No data found"; then
    note PASS "no import errors"
else
    airflow dags list-import-errors 2>/dev/null | grep -v "INFO" | tail -20
    note FAIL "at least one DAG file failed to import"
    failures=$((failures + 1))
fi

for dag in "${DAGS[@]}"; do
    if airflow dags list 2>/dev/null | grep -q "^$dag "; then
        note PASS "$dag registered"
    else
        note FAIL "$dag is missing"
        failures=$((failures + 1))
    fi
done

if [[ "${PARSE_ONLY:-0}" == "1" ]]; then
    echo
    [[ $failures -eq 0 ]] && echo "parse-only: DAGs are importable." || echo "parse-only FAILED"
    exit $((failures > 0))
fi

echo
echo "=== each DAG runs end to end ==="
for dag in "${DAGS[@]}"; do
    state=$(airflow dags test "$dag" "$LOGICAL_DATE" 2>&1 \
            | sed -nE 's/.*DagRun Finished.*state=([a-z]+).*/\1/p' | tail -1)
    case "$state" in
        success) note PASS "$dag -> success" ;;
        # The restatement DAG is a sensor with `soft_fail`, so a run with no late
        # arrivals ends `skipped`. That is the normal case, not a failure.
        skipped) note PASS "$dag -> skipped (nothing to restate)" ;;
        *)       note FAIL "$dag -> ${state:-no dagrun}"; failures=$((failures + 1)) ;;
    esac
done

echo
echo "=== the gate stops the DAG rather than reporting on it ==="
# Threshold above any achievable score: the gate task must fail, which is what
# stops Silver. A gate that logged and returned 0 would leave this green.
state=$(DQ_PROMOTION_THRESHOLD=0.999999 \
        airflow dags test confidentia_bronze_silver "$LOGICAL_DATE" 2>&1 \
        | sed -nE 's/.*DagRun Finished.*state=([a-z]+).*/\1/p' | tail -1)
if [[ "$state" == "failed" ]]; then
    note PASS "an unreachable threshold fails the DAG run"
else
    note FAIL "unreachable threshold left the DAG run '$state'; the gate is not a control"
    failures=$((failures + 1))
fi

# ...and Silver must not have run behind a blocked gate. Parsed as JSON: the
# table output is column-aligned with `|` separators, and splitting it on
# whitespace picks up a separator rather than the state.
silver_state=$(airflow tasks states-for-dag-run confidentia_bronze_silver \
    "manual__${LOGICAL_DATE}T00:00:00+00:00" --output json 2>/dev/null \
    | python3 -c "
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    print('unreadable'); raise SystemExit
states = [r.get('state') for r in rows if r.get('task_id') == 'silver_healthcare']
print(states[0] if states else 'not scheduled')
")
case "$silver_state" in
    # `upstream_failed` is the expected outcome; `None`/absent means the task was
    # never even scheduled, which is equally fine. `success` is the failure.
    upstream_failed|None|none|"not scheduled"|"")
        note PASS "Silver did not run behind a blocked gate (state: ${silver_state:-absent})" ;;
    *)
        note FAIL "Silver ran anyway (state: $silver_state)"
        failures=$((failures + 1)) ;;
esac

echo
if [[ $failures -gt 0 ]]; then
    echo "DAG verification FAILED ($failures)"
    exit 1
fi
echo "DAGs verified: they run, and the gate stops them."
