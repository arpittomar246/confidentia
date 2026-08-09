#!/usr/bin/env bash
# Run the same checks CI runs, locally, before pushing.
#
# This exists because Stage 1-6 were verified with `ruff` and `pytest` only, while
# CI additionally runs mypy and installs a *different* set of extras. Six stages
# passed locally and failed on GitHub. Local verification that does not match CI is
# not verification.
#
# The jobs needing Java/Docker (encryption-guarantee, compose-validate) are listed
# but skipped here; they are covered by the dedicated verify_*.sh scripts.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
# shellcheck disable=SC1091
source .venv/bin/activate

failed=()

step() {
    local name="$1"; shift
    echo
    echo "======== $name ========"
    if "$@"; then
        echo "  PASS"
    else
        echo "  FAIL"
        failed+=("$name")
    fi
}

step "lint / ruff"        ruff check src tests spikes
step "lint / mypy"        mypy
step "unit tests"         python -m pytest tests/unit -q -m unit
step "integration tests"  python -m pytest tests/integration -q -m integration
step "ground-truth isolation" bash scripts/check_ground_truth_isolation.sh
step "key material handling"  python scripts/check_key_material_handling.py

echo
echo "======== generator determinism (CI job) ========"
rm -rf /tmp/ci-gen-a /tmp/ci-gen-b
if python -m confidentia.cli generate healthcare --rows 20000 --out /tmp/ci-gen-a >/dev/null 2>&1 \
   && python -m confidentia.cli generate healthcare --rows 20000 --out /tmp/ci-gen-b >/dev/null 2>&1 \
   && python - <<'PY'
import json, pathlib, sys
bad = [a.parent.name for a in pathlib.Path("/tmp/ci-gen-a").rglob("_ingest_manifest.json")
       if json.loads(a.read_text())["batch_digest"]
       != json.loads(pathlib.Path(str(a).replace("ci-gen-a", "ci-gen-b")).read_text())["batch_digest"]]
sys.exit(1 if bad else 0)
PY
then
    echo "  PASS"
else
    echo "  FAIL"
    failed+=("generator determinism")
fi
rm -rf /tmp/ci-gen-a /tmp/ci-gen-b

echo
echo "=================================================="
if [[ ${#failed[@]} -eq 0 ]]; then
    echo "all local CI checks passed"
    echo "not covered here: encryption-guarantee (scripts/verify_encryption.sh),"
    echo "                  compose-validate (needs Docker)"
    exit 0
fi
printf 'FAILED: %s\n' "${failed[@]}"
exit 1
