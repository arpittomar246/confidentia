# Spark: the pipeline runtime.
#
# Two stages, because the KMS client has to be *compiled* and the compiler is not
# needed afterwards. `LocalKmsClient.java` is built against PySpark's own Parquet
# jars rather than against a Maven-resolved Parquet, so that the client can never
# be compiled against a different Parquet version than the one Spark loads --
# that mismatch surfaces as an AbstractMethodError deep inside a Spark task and is
# miserable to diagnose (see scripts/build_kms_jar.sh).
#
# Which means the build stage needs the full PySpark install just to compile one
# file. It is worth it: the alternative is shipping a jar built on a developer's
# machine, and this stage is the only thing that guarantees the jar and the Spark
# in the image agree.

# Pinned to bookworm (Debian 12), not the floating `python:3.11-slim`.
#
# Spark 3.5 supports Java 8/11/17 **only** -- 21 fails with opaque module-access
# errors rather than a version message. `python:3.11-slim` now resolves to Debian
# 13 (trixie), which has dropped the openjdk-17 packages entirely: the build fails
# with "Unable to locate package openjdk-17-jdk-headless". Bookworm still carries
# them, so the base is pinned rather than the JDK worked around.
#
# This pin is a dependency on Debian 12's support window. When it expires the
# choice is Spark 4 (which supports 17 and 21) or a Temurin base image.
FROM python:3.11-slim-bookworm AS jar-builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-17-jdk-headless \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/confidentia/__init__.py src/confidentia/__init__.py
RUN pip install --no-cache-dir ".[spark]"

COPY src/confidentia/crypto/java/LocalKmsClient.java ./
RUN JARS="$(python -c 'import pyspark, pathlib; print(pathlib.Path(pyspark.__file__).parent / "jars")')" \
    && mkdir -p classes \
    && javac -nowarn -source 17 -target 17 -cp "${JARS}/*" -d classes LocalKmsClient.java \
    && jar --create --file confidentia-localkms.jar -C classes .


# Same pin, same reason as the builder stage above.
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
    CONFIDENTIA_JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

# JRE, not JDK: nothing at runtime compiles. procps is for `jps`/`ps` when a Spark
# job has to be inspected inside the container, which is the only debugging tool
# available once it is running.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-17-jre-headless procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/confidentia

COPY pyproject.toml README.md ./
COPY src/confidentia/__init__.py src/confidentia/__init__.py

# Every extra the pipeline runs. `api` is excluded: this container executes the
# pipeline and must not also be able to serve it.
RUN pip install --no-cache-dir ".[spark,lake,storage,generators,ingestion,cdc,stream]"

COPY src/ src/
COPY scripts/ scripts/

# The jar the runtime never builds, from the stage that could.
COPY --from=jar-builder /build/confidentia-localkms.jar \
     src/confidentia/crypto/java/confidentia-localkms.jar

RUN pip install --no-cache-dir --no-deps -e .

# `spark.local.dir` and the lake both live under /data, which compose mounts as a
# volume. Created here so the directory exists and is writable when the volume is
# empty on first start.
RUN useradd --create-home --uid 10002 spark \
    && mkdir -p /data \
    && chown -R spark:spark /opt/confidentia /data
USER spark

# The Spark UI. Bound inside the container only; compose decides whether to publish it.
EXPOSE 4040

# No default command. This image is a *runtime for the CLI*, not a service: it is
# driven by `docker compose run spark confidentia silver write ...`, and an image
# that starts a long-running process by default would make that awkward and would
# also start Spark on `compose up` for no reason.
CMD ["confidentia", "info"]
