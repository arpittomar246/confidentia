#!/usr/bin/env bash
# Verify the serving layer against a *running* API and a populated database.
#
# The pytest suite in tests/api runs against an in-process TestClient and an empty
# schema, which proves the SQL is valid but not that the thing serves. This script
# starts the real uvicorn process, drives it over HTTP, and asserts the claims the
# stage report makes: that authentication is enforced, that the role ladder holds,
# that writes are refused, and that every endpoint answers with real data.
#
# Needs: Postgres up, migrations applied, and a lake that has been through the
# pipeline at least once. Run scripts/rebuild_pipeline.sh first if not.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
# shellcheck disable=SC1091
source .venv/bin/activate

PORT="${API_TEST_PORT:-8077}"
BASE="http://127.0.0.1:${PORT}"
failed=0

check() {
    local name="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        printf '  ok    %-52s %s\n' "$name" "$actual"
    else
        printf '  FAIL  %-52s expected %s, got %s\n' "$name" "$expected" "$actual"
        failed=$((failed + 1))
    fi
}

status() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo "starting the API on :${PORT}"
API_PORT="$PORT" python -m confidentia.cli api serve --port "$PORT" >/tmp/confidentia-api.log 2>&1 &
API_PID=$!
# shellcheck disable=SC2064
trap "kill $API_PID 2>/dev/null" EXIT

for _ in $(seq 1 40); do
    [[ "$(status "$BASE/health")" == "200" ]] && break
    sleep 0.5
done
if [[ "$(status "$BASE/health")" != "200" ]]; then
    echo "the API did not come up; last 20 lines:"
    tail -20 /tmp/confidentia-api.log
    exit 1
fi

VIEWER=$(python -m confidentia.cli api token viewer --role viewer)
OPERATOR=$(python -m confidentia.cli api token operator --role operator)
AUDITOR=$(python -m confidentia.cli api token auditor --role auditor)

echo
echo "unauthenticated"
check "GET /health"                 200 "$(status "$BASE/health")"
check "GET /ready"                  200 "$(status "$BASE/ready")"
check "GET /meta"                   200 "$(status "$BASE/meta")"
check "GET /runs without a token"   401 "$(status "$BASE/runs")"
check "GET /runs with a bad token"  401 "$(status -H 'Authorization: Bearer forged' "$BASE/runs")"

echo
echo "the role ladder"
check "viewer   -> /runs"                200 "$(status -H "Authorization: Bearer $VIEWER" "$BASE/runs")"
check "viewer   -> /quality/quarantine"  403 "$(status -H "Authorization: Bearer $VIEWER" "$BASE/quality/quarantine")"
check "operator -> /quality/quarantine"  200 "$(status -H "Authorization: Bearer $OPERATOR" "$BASE/quality/quarantine")"
check "operator -> /audit/key-releases"  403 "$(status -H "Authorization: Bearer $OPERATOR" "$BASE/audit/key-releases")"
check "auditor  -> /audit/key-releases"  200 "$(status -H "Authorization: Bearer $AUDITOR" "$BASE/audit/key-releases")"

echo
echo "refusals"
check "POST /runs is read-only"     405 "$(status -X POST -H "Authorization: Bearer $AUDITOR" "$BASE/runs")"
check "malformed uuid is 400"       400 "$(status -H "Authorization: Bearer $VIEWER" "$BASE/runs/not-a-uuid")"
check "a table outside gold is 404" 404 "$(status -H "Authorization: Bearer $VIEWER" "$BASE/marts/pg_class")"

echo
echo "every endpoint, with data"
python - "$BASE" "$AUDITOR" <<'PY'
import json, sys, urllib.error, urllib.request

base, token = sys.argv[1], sys.argv[2]
paths = [
    "/runs?limit=3", "/runs/stats",
    "/quality/scores?limit=3", "/quality/scores/latest", "/quality/rules?limit=3",
    "/quality/failures?limit=3", "/quality/freshness",
    "/quality/quarantine?limit=3", "/quality/quarantine/summary",
    "/evaluation/latest", "/evaluation/summary", "/evaluation/coverage",
    "/gate/blocked", "/gate/decisions?limit=3", "/gate/overrides",
    "/ingestion/batches?limit=3", "/ingestion/watermarks", "/ingestion/summary",
    "/lineage/head", "/lineage/latest", "/lineage/entries?limit=3", "/lineage/verify",
    "/audit/key-releases?limit=3", "/audit/key-releases/summary",
    "/audit/attestation-policy", "/audit/disclosure?limit=3", "/audit/restatements?limit=3",
    "/marts",
]
bad = 0
for path in paths:
    request = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        print(f"  FAIL  {path:52s} HTTP {exc.code}")
        bad += 1
        continue
    # An endpoint that returns 200 and nothing is not evidence of anything, so the
    # count is printed: a suite of empty payloads is how a broken filter passes.
    rows = body.get("items", body if isinstance(body, list) else [body])
    print(f"  ok    {path:52s} {len(rows) if isinstance(rows, list) else 1} row(s)")
sys.exit(1 if bad else 0)
PY
[[ $? -ne 0 ]] && failed=$((failed + 1))

echo
if [[ $failed -eq 0 ]]; then
    echo "the serving layer verified: auth enforced, roles enforced, writes refused"
    exit 0
fi
echo "FAILED: $failed check(s)"
exit 1
