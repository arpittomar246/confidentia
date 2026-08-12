#!/usr/bin/env bash
# Copy the keystore somewhere `reset_data.sh` cannot reach.
#
# Everything in the data root is regenerable in about ten minutes -- except this.
# The keystore holds the master key every tenant KEK and per-class DEK is derived
# from, so losing it makes every existing encrypted Delta table unreadable
# permanently. There is no recovery path and there is deliberately no escrow.
#
# The realistic threat is not disk failure, it is `scripts/reset_data.sh --yes`,
# which deletes the data root and is a script this project ships. It preserves the
# keystore on purpose, but one edit or one mistyped path is all it would take.
#
# WHERE THIS DOES NOT GO
#   * not the repo    -- key material must never be committed, and .gitignore is
#                        one mistake away from not covering it
#   * not the data root -- that is the thing being defended against
#
# This is a *local* copy. It survives a data-root reset; it does not survive the
# disk. Copying it off-machine is a decision about where key material is allowed
# to live, which is not a decision a script should make silently.
set -euo pipefail

DATA_ROOT="${CONFIDENTIA_DATA_ROOT:-$HOME/confidentia-data}"
KEYSTORE="$DATA_ROOT/keystore"
BACKUP_ROOT="${CONFIDENTIA_BACKUP_ROOT:-$HOME/confidentia-backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$BACKUP_ROOT/keystore-$STAMP"

if [[ ! -d "$KEYSTORE" ]]; then
    echo "no keystore at $KEYSTORE -- nothing to back up" >&2
    exit 1
fi

# Refuse to write inside the repo or the data root, whatever the environment says.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
case "$(readlink -f "$BACKUP_ROOT")/" in
    "$(readlink -f "$REPO_DIR")"/*|"$(readlink -f "$DATA_ROOT")"/*)
        echo "refusing: backup root is inside the repo or the data root" >&2
        exit 1 ;;
esac

mkdir -p "$BACKUP_ROOT"
chmod 700 "$BACKUP_ROOT"
cp -a "$KEYSTORE" "$TARGET"
chmod -R go-rwx "$TARGET"

echo "keystore backed up:"
echo "  from  $KEYSTORE"
echo "  to    $TARGET"
ls -l "$TARGET" | tail -n +2 | sed 's/^/    /'

# Keep the last few. Unbounded copies of key material is its own problem: every
# stale copy is another place it can leak from, and none of them are encrypted at
# rest beyond file permissions.
KEEP="${CONFIDENTIA_BACKUP_KEEP:-3}"
mapfile -t stale < <(find "$BACKUP_ROOT" -maxdepth 1 -name 'keystore-*' -type d | sort -r | tail -n +$((KEEP + 1)))
if [[ ${#stale[@]} -gt 0 ]]; then
    echo
    echo "  pruning $((${#stale[@]})) older copies (keeping $KEEP):"
    for old in "${stale[@]}"; do
        rm -rf "$old"
        echo "    removed $(basename "$old")"
    done
fi

echo
echo "NOTE: this survives a data-root reset, not a disk failure."
echo "      Copying it off this machine is your call -- it is key material."
