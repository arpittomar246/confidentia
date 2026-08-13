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

# These must be the *exact* commands from .github/workflows/ci.yml. A local
# variant that merely looks equivalent is how Stage 7a passed here and failed on
# GitHub: `pytest tests/unit` scopes collection to one directory, while CI's
# `pytest -m unit` collects everything under testpaths and then filters, so CI
# imports modules the local run never touched.
step "lint / ruff"        ruff check src tests spikes orchestration
step "lint / mypy"        mypy
step "unit tests"         pytest -m unit --cov=confidentia --cov-report=term-missing -q
step "integration tests"  pytest -m integration -q
step "ground-truth isolation" bash scripts/check_ground_truth_isolation.sh
step "plan/rule coverage"     python scripts/check_plan_rule_coverage.py
step "DAG/CLI contract"       python scripts/check_dag_cli_contract.py
step "key material handling"  python scripts/check_key_material_handling.py
step "declared dependencies"  python scripts/check_declared_dependencies.py
step "openapi schema"         python scripts/export_openapi.py --check

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
    echo "not run here, but CI does run them:"
    echo "  encryption-guarantee   scripts/verify_encryption.sh"
    echo "  compose-validate       scripts/verify_docker.sh --static"
    echo "  container-build        scripts/verify_docker.sh   (needs a Docker daemon)"
    # These verify the project's headline claims and run nowhere automatically:
    # each needs a populated lake and the keystore, which neither this script nor
    # CI has. Naming them is the point -- a footer listing only the CI-covered
    # gaps implies these are covered somewhere, and they are not.
    echo "run by hand after a rebuild -- automated nowhere:"
    echo "  streaming claims       scripts/verify_streaming.sh   (needs Kafka)"
    echo "  lineage tamper claims  scripts/verify_lineage.sh"
    echo "  DAG execution + gate   scripts/verify_dags.sh        (needs Airflow)"
    # tests/api proves the SQL is valid against an empty schema; this proves the
    # service actually serves, against data.
    echo "  serving layer + RBAC   scripts/verify_api.sh         (needs a populated DB)"
    exit 0
fi
printf 'FAILED: %s\n' "${failed[@]}"
exit 1
