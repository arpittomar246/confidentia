#!/usr/bin/env bash
# Prove the lineage chain actually detects tampering.
#
# A chain that is never tested against tampering is a hash column. Every claim
# below is checked by breaking something real and requiring the verifier to notice
# -- and to notice for the *right reason*, because a verifier that reports "broken
# link" when the problem is a forged signature sends an operator to the wrong place.
#
#   1. an edit to a signed payload in Postgres
#   2. an edit to a denormalised column, which the signature does not cover
#   3. a deleted mid-chain entry
#   4. an edit to a row in the encrypted Bronze table itself
#
# Everything is restored afterwards and the chain is re-verified, so this script is
# safe to run on the working lake. Claim 4 edits a real Delta table and undoes it
# with time travel; if that restore ever fails the script stops loudly rather than
# leaving the lake in a state the chain disagrees with.
#
#   bash scripts/verify_lineage.sh
#   bash scripts/verify_lineage.sh --no-spark    # claims 1-3 only, seconds not minutes
#
# Claims 1-3 are ledger attacks and run entirely in Postgres. Claim 4 opens the
# encrypted Bronze table through Spark, and on a developer box that is the whole
# runtime: the full run takes minutes, the ledger claims take seconds. `--no-spark`
# runs the fast three and says plainly that the fourth did not run, which is the
# only honest way to shorten this -- a demo that skips claim 4 silently would be
# claiming a check it had not made.
set -uo pipefail

WITH_SPARK=1
for arg in "$@"; do
    case "$arg" in
    --no-spark) WITH_SPARK=0 ;;
    *)
        echo "usage: verify_lineage.sh [--no-spark]" >&2
        exit 2
        ;;
    esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
# shellcheck disable=SC1091
source .venv/bin/activate

: "${CONFIDENTIA_KEYSTORE_PASSPHRASE:?set CONFIDENTIA_KEYSTORE_PASSPHRASE first}"

# Claim 4 opens the encrypted Bronze table, so it needs key release, so it needs an
# approved measurement. Checked here rather than discovered there: the failure
# surfaces sixty seconds in, as "could not read the Delta version", which sends the
# reader to look at Spark and Delta when the actual cause is an edit to a measured
# package and the fix is one command. That happened, in front of nobody, which is
# the good version of finding out.
#
# Only claim 4 needs it, so --no-spark does not ask: an unapproved build is not a
# reason to refuse the three checks that never touch a key.
if [[ $WITH_SPARK -eq 1 ]] && ! python -m confidentia.cli keys check >/dev/null 2>&1; then
    echo "The running code is not approved for key release, so claim 4 cannot open"
    echo "the Bronze table. A measured package changed. Approve this build first:"
    echo ""
    echo "    python -m confidentia.cli keys approve"
    echo ""
    echo "Or run the ledger claims alone:  bash scripts/verify_lineage.sh --no-spark"
    echo ""
    exit 1
fi

export PGPASSWORD="${POSTGRES_PASSWORD:-confidentia}"
PG=(psql -qAt -h "${POSTGRES_HOST:-localhost}" -U "${POSTGRES_USER:-confidentia}"
    -d "${POSTGRES_DB:-confidentia}")

# The tamper target: a five-column reference table of 41 rows, fully regenerable
# with `bronze write healthcare --full-refresh`. `specialty` is PUBLIC, so it is
# stored in plaintext -- which makes it the column an attacker with filesystem
# access but no keys could actually change, and therefore the honest one to test.
TENANT=healthcare
DATASET=provider
COLUMN=specialty

FAILURES=0
pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; FAILURES=$((FAILURES + 1)); }

cli() { python -m confidentia.cli "$@" 2>&1; }

# Verification output, with Spark's progress noise and the structlog stream
# removed. Returns the exit code of the verify itself.
verify_out() {
    cli lineage verify "$@" | grep -aE "^  |^    " | sed 's/\r.*\x1b\[[0-9]*m//'
    return "${PIPESTATUS[0]}"
}

section() {
    echo
    echo "======== $1 ========"
}

# ---------------------------------------------------------------------------
section "baseline"

# Re-record the target so its latest entry commits to the table as it is now.
# Without this, a lake changed by an earlier pipeline run would make claim 4 pass
# for the wrong reason -- the deep check would already be failing.
#
# Recording reads the table to compute its root, so this is a Spark call and exists
# only for claim 4's benefit. Skipped with the claim it serves.
if [[ $WITH_SPARK -eq 1 ]]; then
    cli lineage record "$TENANT" "$DATASET" --layer bronze | grep -aE "^  " || true
fi

OUT="$(verify_out)"
if [[ $? -eq 0 ]]; then
    pass "chain verifies before tampering"
    echo "        ${OUT}"
else
    fail "chain does not verify before tampering -- nothing below is meaningful"
    echo "$OUT"
    exit 1
fi

TOTAL="$("${PG[@]}" -c "SELECT count(*) FROM platform.lineage_entry")"
# A mid-chain entry, never the tip: the tip has no successor whose link could
# break, which is a real limitation and is stated in the stage report rather than
# quietly avoided here.
VICTIM="$("${PG[@]}" -c "SELECT seq FROM platform.lineage_entry ORDER BY seq OFFSET 1 LIMIT 1")"
echo "        ${TOTAL} entries; tampering with entry #${VICTIM}"

# ---------------------------------------------------------------------------
section "claim 1: an edited payload is detected"

"${PG[@]}" -c "UPDATE platform.lineage_entry
                  SET payload = jsonb_set(payload, '{record_count}', '999999')
                WHERE seq = ${VICTIM}" >/dev/null

OUT="$(verify_out)"
if [[ $? -ne 0 ]] && grep -q "payload altered" <<<"$OUT"; then
    pass "edited payload rejected"
    echo "        $(grep -m1 'payload altered' <<<"$OUT" | sed 's/^ *//')"
else
    fail "an edited payload was not reported as such"
    echo "$OUT"
fi

"${PG[@]}" -c "UPDATE platform.lineage_entry
                  SET payload = jsonb_set(payload, '{record_count}', to_jsonb(record_count))
                WHERE seq = ${VICTIM}" >/dev/null

# ---------------------------------------------------------------------------
section "claim 2: an edited column is detected"
# The one edit the signature cannot see. Only `payload` is signed, so an UPDATE
# here changes what every dashboard and join reads while leaving every digest
# intact. Verification compares the columns against the payload for this reason.

"${PG[@]}" -c "UPDATE platform.lineage_entry
                  SET merkle_root = repeat('a', 64) WHERE seq = ${VICTIM}" >/dev/null

OUT="$(verify_out)"
if [[ $? -ne 0 ]] && grep -q "column diverges" <<<"$OUT"; then
    pass "edited column rejected"
    echo "        $(grep -m1 'column diverges' <<<"$OUT" | sed 's/^ *//')"
else
    fail "an edited column was not reported"
    echo "$OUT"
fi

"${PG[@]}" -c "UPDATE platform.lineage_entry
                  SET merkle_root = payload->>'merkle_root' WHERE seq = ${VICTIM}" >/dev/null

OUT="$(verify_out)"
[[ $? -eq 0 ]] || { fail "restoring the column did not restore the chain"; echo "$OUT"; }

# ---------------------------------------------------------------------------
section "claim 3: a deleted entry is detected"

"${PG[@]}" -c "DROP TABLE IF EXISTS platform._lineage_backup;
               CREATE TABLE platform._lineage_backup AS
                    SELECT * FROM platform.lineage_entry WHERE seq = ${VICTIM};
               DELETE FROM platform.lineage_entry WHERE seq = ${VICTIM}" >/dev/null 2>&1

OUT="$(verify_out)"
if [[ $? -ne 0 ]] && grep -q "broken link" <<<"$OUT"; then
    pass "removing an entry breaks the chain"
    echo "        $(grep -m1 'broken link' <<<"$OUT" | sed 's/^ *//')"
else
    fail "a deleted entry left the chain looking intact"
    echo "$OUT"
fi

"${PG[@]}" -c "INSERT INTO platform.lineage_entry OVERRIDING SYSTEM VALUE
                    SELECT * FROM platform._lineage_backup;
               DROP TABLE platform._lineage_backup" >/dev/null

OUT="$(verify_out)"
if [[ $? -eq 0 ]]; then
    pass "chain restored"
else
    fail "the chain did not come back after re-inserting the entry"
    echo "$OUT"
    exit 1
fi

# ---------------------------------------------------------------------------
if [[ $WITH_SPARK -eq 0 ]]; then
    echo
    echo "======== claim 4: SKIPPED (--no-spark) ========"
    echo "  The three claims above are ledger attacks. Claim 4 -- an edited row in"
    echo "  the encrypted Bronze table -- is the one they cannot make, and it needs"
    echo "  Spark. It has NOT been checked by this run."
    echo
    if [[ $FAILURES -eq 0 ]]; then
        echo "three of four tamper claims hold; claim 4 not attempted."
    else
        echo "${FAILURES} claims FAILED."
    fi
    exit $((FAILURES > 0))
fi

section "claim 4: an edited row in the lake is detected"
# The check the other three cannot make. Claims 1-3 all assume the attacker went
# after the ledger; this one assumes they left the ledger alone and changed the
# data underneath it, which is both easier and more useful to them.

VERSION="$(python scripts/lineage_tamper.py version "$TENANT" "$DATASET" 2>/dev/null | tail -1)"
if ! [[ "$VERSION" =~ ^[0-9]+$ ]]; then
    fail "could not read the Delta version of ${TENANT}/${DATASET}; skipping claim 4"
    echo
    echo "${FAILURES} failing claims"
    exit $((FAILURES > 0))
fi
echo "        ${TENANT}/${DATASET} is at Delta version ${VERSION}"

TAMPERED="$(python scripts/lineage_tamper.py tamper "$TENANT" "$DATASET" "$COLUMN" 2>/dev/null \
    | grep -a '^tampered')"
echo "        ${TAMPERED}"
# The key of the row that was actually edited, so the proof below is about that
# row rather than an arbitrary one.
KEY="$(sed -n 's/.*=\([^,]*\),.*/\1/p' <<<"$TAMPERED")"

OUT="$(verify_out --deep --tenant "$TENANT")"
DEEP_RC=$?
if [[ $DEEP_RC -ne 0 ]] && grep -q "DOES NOT MATCH" <<<"$OUT"; then
    pass "the tampered table no longer hashes to its signed root"
    echo "        $(grep -m1 'DOES NOT MATCH' <<<"$OUT" | sed 's/^ *//')"
else
    fail "a tampered table still matched its signed root"
    echo "$OUT"
fi

if [[ -n "$KEY" ]]; then
    PROOF="$(cli lineage prove "$TENANT" "$DATASET" "$KEY" --layer bronze | grep -aE "PROVEN|NOT PROVEN")"
    if grep -q "NOT PROVEN" <<<"$PROOF"; then
        pass "the tampered row can no longer be proven"
    else
        fail "a row in a tampered table was still provable: ${PROOF}"
    fi
fi

python scripts/lineage_tamper.py restore "$TENANT" "$DATASET" "$VERSION" 2>/dev/null \
    | grep -a restored

OUT="$(verify_out --deep --tenant "$TENANT")"
if [[ $? -eq 0 ]]; then
    pass "restoring the table restores the match to the signed root"
else
    fail "the lake did not come back to its signed state -- run: bronze write ${TENANT} --full-refresh"
    echo "$OUT"
fi

# ---------------------------------------------------------------------------
echo
if [[ $FAILURES -eq 0 ]]; then
    echo "all four tamper claims hold."
else
    echo "${FAILURES} claims FAILED."
fi
echo
echo "Not claimed, and not testable here: truncating the chain at the tip. Deleting"
echo "the newest entries leaves a shorter chain that verifies perfectly. Detecting"
echo "that needs the head digest published somewhere the platform cannot rewrite."
exit $((FAILURES > 0))
