#!/usr/bin/env bash
# Start, stop and inspect the local broker.
#
#   bash scripts/kafka.sh start | stop | status | topics | reset
#
# The heap is capped at 512 MB, matching the limit docker-compose.yml declares.
# That is the whole budget the broker gets: a Spark streaming driver takes 2 GB of
# a 3.9 GB box, and an unbounded broker heap turns a slow consumer into an
# OOM-killed one.
set -uo pipefail

KAFKA_HOME="${KAFKA_HOME:-$HOME/kafka}"
KAFKA_DATA="${KAFKA_DATA:-$HOME/confidentia-data/kafka}"
CONFIG="$KAFKA_HOME/config/confidentia-kraft.properties"
LOG="${KAFKA_LOG:-/tmp/kafka-server.log}"
BOOTSTRAP="${KAFKA_BOOTSTRAP_SERVERS:-localhost:9092}"

export KAFKA_HEAP_OPTS="${KAFKA_HEAP_OPTS:--Xmx512m -Xms512m}"

running() { pgrep -f "kafka.Kafka.*confidentia-kraft" >/dev/null 2>&1; }

case "${1:-status}" in
start)
    if running; then echo "already running"; exit 0; fi
    [[ -x "$KAFKA_HOME/bin/kafka-server-start.sh" ]] || {
        echo "Kafka not installed; run scripts/install_kafka.sh" >&2; exit 1; }
    echo "==> starting broker (heap $KAFKA_HEAP_OPTS)"
    nohup "$KAFKA_HOME/bin/kafka-server-start.sh" "$CONFIG" > "$LOG" 2>&1 &
    for _ in $(seq 1 60); do
        "$KAFKA_HOME/bin/kafka-broker-api-versions.sh" \
            --bootstrap-server "$BOOTSTRAP" >/dev/null 2>&1 && break
        sleep 2
    done
    if "$KAFKA_HOME/bin/kafka-broker-api-versions.sh" \
           --bootstrap-server "$BOOTSTRAP" >/dev/null 2>&1; then
        echo "    up on $BOOTSTRAP  (log: $LOG)"
    else
        echo "    broker did not come up; see $LOG" >&2
        tail -20 "$LOG" >&2
        exit 1
    fi
    ;;
stop)
    if ! running; then echo "not running"; exit 0; fi
    # The bundled stop script uses a pattern that also matches unrelated JVMs on a
    # busy box, so match this project's config file instead.
    pkill -f "kafka.Kafka.*confidentia-kraft"
    for _ in $(seq 1 30); do running || break; sleep 1; done
    running && { echo "did not stop cleanly" >&2; exit 1; }
    echo "stopped"
    ;;
status)
    if running; then
        echo "broker: running"
        "$KAFKA_HOME/bin/kafka-broker-api-versions.sh" \
            --bootstrap-server "$BOOTSTRAP" 2>/dev/null | head -1
    else
        echo "broker: not running"
        exit 1
    fi
    ;;
topics)
    "$KAFKA_HOME/bin/kafka-topics.sh" --bootstrap-server "$BOOTSTRAP" --describe 2>/dev/null \
        | grep -E "^Topic:" || echo "no topics"
    ;;
reset)
    # Deletes every message. Kept explicit and separate from `stop` because a
    # consumer's Spark checkpoint records offsets into a topic that would no
    # longer exist -- resetting one without the other makes the consumer resume
    # from an offset that means nothing.
    echo "This deletes all topic data in $KAFKA_DATA."
    echo "Spark streaming checkpoints under the data root must be cleared too."
    [[ "${2:-}" == "--yes" ]] || { echo "re-run with: kafka.sh reset --yes"; exit 1; }
    bash "$0" stop
    rm -rf "${KAFKA_DATA:?}"/*
    bash "$(dirname "${BASH_SOURCE[0]}")/install_kafka.sh" >/dev/null
    echo "topic data cleared and storage reformatted"
    ;;
*)
    echo "usage: kafka.sh {start|stop|status|topics|reset --yes}" >&2
    exit 2
    ;;
esac
