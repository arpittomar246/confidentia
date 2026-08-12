#!/usr/bin/env bash
# Bring up (or check) the background services the pipeline needs.
#
#   bash scripts/services.sh status | start
#
# This exists because PostgreSQL has twice been found dead mid-session, both times
# during sustained Spark work, and both times abruptly: the log shows normal
# checkpoints and then an end-of-recovery restart, with no "received shutdown
# request" in between. Kafka survived both, so it is not the WSL VM stopping.
#
# The cause is not established. What is established is the symptom -- a
# `PoolTimeout: couldn't get a connection` from the middle of a long run -- and
# that a run which checks first fails in two seconds with a clear message instead
# of a hundred lines of traceback. That is worth having whether or not the
# underlying cause is ever pinned down.
#
# Kafka is only needed for Stage 9 onwards, so it is reported but never required.
set -uo pipefail

PGHOST="${POSTGRES_HOST:-localhost}"
PGUSER="${POSTGRES_USER:-confidentia}"
export PGPASSWORD="${POSTGRES_PASSWORD:-confidentia}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

postgres_up() { pg_isready -q -h "$PGHOST" 2>/dev/null; }
kafka_up()    { pgrep -f "kafka.Kafka.*confidentia-kraft" >/dev/null 2>&1; }

report() {
    local down=0
    if postgres_up; then
        echo "  postgres  up"
    else
        echo "  postgres  DOWN"
        down=1
    fi
    if kafka_up; then
        echo "  kafka     up"
    else
        echo "  kafka     down (only needed from Stage 9)"
    fi
    return $down
}

case "${1:-status}" in
status)
    echo "services:"
    report
    exit $?
    ;;
start)
    echo "services:"
    if ! postgres_up; then
        echo "  postgres  starting..."
        sudo -n service postgresql start >/dev/null 2>&1
        for _ in $(seq 1 20); do postgres_up && break; sleep 1; done
    fi
    if ! kafka_up && [[ -x "${KAFKA_HOME:-$HOME/kafka}/bin/kafka-server-start.sh" ]]; then
        echo "  kafka     starting..."
        bash "$REPO_DIR/scripts/kafka.sh" start >/dev/null 2>&1
    fi
    report
    status=$?
    if [[ $status -ne 0 ]]; then
        echo
        echo "PostgreSQL did not come up. Everything except the generators needs it."
        exit 1
    fi
    ;;
*)
    echo "usage: services.sh {status|start}" >&2
    exit 2
    ;;
esac
