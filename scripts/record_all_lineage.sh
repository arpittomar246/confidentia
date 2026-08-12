#!/usr/bin/env bash
# Record a lineage entry for every table in the lake, bottom layer first.
#
# Order matters and is not cosmetic. A Silver entry cites the current Bronze
# entry's digest, and a Gold entry cites every current Silver entry; running them
# out of order would produce Gold provenance pointing at Silver commitments that
# predate the Silver data it was actually built from.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
# shellcheck disable=SC1091
source .venv/bin/activate

: "${CONFIDENTIA_KEYSTORE_PASSPHRASE:?set CONFIDENTIA_KEYSTORE_PASSPHRASE first}"

for layer in bronze silver gold; do
    for tenant in healthcare financial; do
        echo
        echo "======== ${tenant} / ${layer} ========"
        python -m confidentia.cli lineage record "$tenant" --layer "$layer" 2>&1 \
            | grep -E "^  |^      " || true
    done
done
