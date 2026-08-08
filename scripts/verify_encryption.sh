#!/usr/bin/env bash
# Prove, from scratch, that the lakehouse encryption guarantee holds.
#
# Each probe runs in its own OS process on purpose: Parquet's KeyToolkit caches
# unwrapped keys statically per JVM, and PySpark does not restart the JVM between
# SparkSessions, so an in-process retry would read from cache and falsely pass.
#
# Expected: encrypted tables show PARE magic with no plaintext in the bytes,
# keyless reads are refused with KeyAccessDeniedException, and delta-rs cannot
# open the table at all.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# shellcheck disable=SC1091
source .venv/bin/activate

export SPIKE_WORK_DIR="${SPIKE_WORK_DIR:-$HOME/confidentia-data/spike}"
# Fresh key material every run; it is never persisted beyond the run.
rm -f spikes/pme/spike_keys.json

if [[ ! -f spikes/pme/confidentia-localkms.jar ]]; then
    echo "==> Building the KMS client jar"
    python spikes/pme/build_kms_jar.py
fi

failures=0
run() {
    echo "======================================================================"
    python spikes/pme/probes.py "$@" 2>/dev/null
    local rc=$?
    echo "  exit=${rc}"
    return $rc
}

run write-parquet                                        || failures=$((failures + 1))
run delta-hadoopconf                                     || failures=$((failures + 1))
run keyless-read --table h_parquet_pme     --format parquet || failures=$((failures + 1))
run keyless-read --table g_delta_hadoopconf --format delta  || failures=$((failures + 1))

# delta-rs is EXPECTED to fail: it cannot read encrypted Parquet. A non-zero exit
# here is the correct outcome and is not counted as a failure.
run delta-rs --table g_delta_hadoopconf

echo "======================================================================"
python spikes/pme/inspect_files.py "$SPIKE_WORK_DIR"

rm -f spikes/pme/spike_keys.json

if [[ $failures -gt 0 ]]; then
    echo "ENCRYPTION GUARANTEE FAILED (${failures} probe(s))"
    exit 1
fi
echo "Encryption guarantee holds."
