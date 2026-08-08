#!/usr/bin/env bash
# Compile the Parquet KMS client against the Parquet/Hadoop jars inside PySpark.
#
# Compiled against PySpark's own jars rather than resolving Parquet from Maven, so
# the client can never be built against a different Parquet version than the one
# Spark loads at runtime -- that mismatch surfaces as an AbstractMethodError deep
# inside a Spark task and is miserable to diagnose.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="${REPO_DIR}/src/confidentia/crypto/java"
BUILD_DIR="${SRC_DIR}/build"
JAR_PATH="${SRC_DIR}/confidentia-localkms.jar"

JAVA_HOME_PATH="${CONFIDENTIA_JAVA_HOME:-${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk-amd64}}"
JARS_DIR="$("${REPO_DIR}/.venv/bin/python" -c 'import pyspark,pathlib;print(pathlib.Path(pyspark.__file__).parent/"jars")')"

echo "JDK        : ${JAVA_HOME_PATH}"
echo "Spark jars : ${JARS_DIR}"

rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"

"${JAVA_HOME_PATH}/bin/javac" -nowarn -source 17 -target 17 \
    -cp "${JARS_DIR}/*" -d "${BUILD_DIR}" "${SRC_DIR}/LocalKmsClient.java"

"${JAVA_HOME_PATH}/bin/jar" --create --file "${JAR_PATH}" -C "${BUILD_DIR}" .
rm -rf "${BUILD_DIR}"

echo "Built      : ${JAR_PATH} ($(stat -c%s "${JAR_PATH}") bytes)"
