#!/usr/bin/env bash
# The ground-truth answer key (_defects.parquet) records every injected defect and
# every latent fraud label. A pipeline that read it would score perfectly and mean
# nothing, so no pipeline package may reference it.
#
# Exactly two places may: writers.py, which produces it, and iter_defects, the one
# sanctioned accessor for evaluation tooling. Run in CI.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

PIPELINE_PACKAGES=(
    src/confidentia/ingestion
    src/confidentia/streaming
    src/confidentia/pipelines
    src/confidentia/quality
    src/confidentia/lineage
    src/confidentia/catalog
    backend
)

violations=0
for package in "${PIPELINE_PACKAGES[@]}"; do
    [[ -d "$package" ]] || continue
    if grep -rn --include='*.py' -e '_defects\.parquet' -e 'iter_defects' -e 'latent_fraud' "$package"; then
        echo "VIOLATION: $package references the ground-truth answer key"
        violations=$((violations + 1))
    fi
done

# Outside the pipeline, direct file access is still restricted to the writer.
direct=$(grep -rn --include='*.py' '_defects\.parquet' src/confidentia \
         | grep -v 'generators/writers.py' || true)
if [[ -n "$direct" ]]; then
    echo "VIOLATION: answer key opened outside generators/writers.py"
    echo "$direct"
    violations=$((violations + 1))
fi

if [[ $violations -gt 0 ]]; then
    echo "ground-truth isolation FAILED (${violations} violation(s))"
    exit 1
fi
echo "ground-truth isolation holds."
