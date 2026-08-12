#!/usr/bin/env bash
# Kafka as a native WSL process, in KRaft mode. No Docker, no ZooKeeper.
#
# ADR 0001 rules out Docker as the development path, and KRaft removes the second
# JVM ZooKeeper would need. On a 3.9 GB box that is not a preference: a Spark
# streaming driver takes 2 GB, so the broker has to fit in what is left.
#
# The version matches the `apache/kafka` image pinned in docker-compose.yml. The
# local and portable paths should not be running different brokers -- that is
# exactly the kind of difference that shows up as an unreproducible result.
set -euo pipefail

KAFKA_VERSION="${KAFKA_VERSION:-3.8.0}"
SCALA_VERSION="${SCALA_VERSION:-2.13}"
KAFKA_HOME="${KAFKA_HOME:-$HOME/kafka}"
KAFKA_DATA="${KAFKA_DATA:-$HOME/confidentia-data/kafka}"
ARCHIVE="kafka_${SCALA_VERSION}-${KAFKA_VERSION}.tgz"
MIRROR="${KAFKA_MIRROR:-https://archive.apache.org/dist/kafka/${KAFKA_VERSION}}"

if [[ -x "$KAFKA_HOME/bin/kafka-server-start.sh" ]]; then
    echo "Kafka already installed at $KAFKA_HOME"
else
    echo "==> downloading Kafka $KAFKA_VERSION (Scala $SCALA_VERSION)"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    curl -fSL --retry 3 -o "$tmp/$ARCHIVE" "$MIRROR/$ARCHIVE"

    echo "==> unpacking to $KAFKA_HOME"
    rm -rf "$KAFKA_HOME"
    mkdir -p "$KAFKA_HOME"
    tar -xzf "$tmp/$ARCHIVE" -C "$KAFKA_HOME" --strip-components=1
fi

# The log directory lives under the data root, not under KAFKA_HOME: it is data,
# and `reset_data.sh` should be able to clear it with everything else. Note that
# resetting the lake without resetting Kafka would leave a consumer resuming from
# an offset into a topic whose data no longer matches Bronze.
mkdir -p "$KAFKA_DATA"

CONFIG="$KAFKA_HOME/config/confidentia-kraft.properties"
cat > "$CONFIG" <<PROPS
# Single-node KRaft: this machine is broker and controller at once. Fine for a
# development box, and explicitly not a claim about production topology.
process.roles=broker,controller
node.id=1
controller.quorum.voters=1@localhost:9093
listeners=PLAINTEXT://:9092,CONTROLLER://:9093
inter.broker.listener.name=PLAINTEXT
advertised.listeners=PLAINTEXT://localhost:9092
controller.listener.names=CONTROLLER
listener.security.protocol.map=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT
log.dirs=$KAFKA_DATA

# Replication factor 1 throughout. A single broker cannot replicate, and leaving
# the defaults (3) makes every internal topic fail to create with an error that
# does not mention replication.
offsets.topic.replication.factor=1
transaction.state.log.replication.factor=1
transaction.state.log.min.isr=1
default.replication.factor=1
min.insync.replicas=1

num.partitions=6
# Seven days. The experiments replay the same topic more than once, and a
# retention shorter than the working session silently changes what is there.
log.retention.hours=168
PROPS

STORAGE_ID_FILE="$KAFKA_DATA/meta.properties"
if [[ ! -f "$STORAGE_ID_FILE" ]]; then
    echo "==> formatting the KRaft storage directory"
    CLUSTER_ID="$("$KAFKA_HOME/bin/kafka-storage.sh" random-uuid)"
    "$KAFKA_HOME/bin/kafka-storage.sh" format \
        -t "$CLUSTER_ID" -c "$CONFIG" --ignore-formatted >/dev/null
    echo "    cluster id $CLUSTER_ID"
else
    echo "==> storage already formatted, keeping it"
fi

echo
echo "installed:"
echo "  KAFKA_HOME  $KAFKA_HOME"
echo "  log.dirs    $KAFKA_DATA"
echo "  config      $CONFIG"
echo
echo "next: bash scripts/kafka.sh start"
