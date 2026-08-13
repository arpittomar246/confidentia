# ADR 0018 — Four images, and where the boundaries between them fall

- **Status:** Accepted
- **Date:** 2026-08-13
- **Stage:** 13 (containerisation)

## Context

`docker-compose.yml` has existed since Stage 1 as the portable and cloud
deliverable, validated in CI and never built. It referenced four Dockerfiles that
did not exist, and `compose-validate` passed anyway for eleven stages, because
`docker compose config` checks syntax and interpolation and nothing else.

ADR 0001 chose native WSL processes over Docker for *local development*, on the
grounds that Docker Desktop's VM costs 1.5–2 GB on a 7.4 GB host. That decision is
unchanged. Stage 13 makes the containerised path real without making it the
development path — and it does so using Docker **Engine** inside WSL, which is a
daemon rather than a VM and costs nothing when idle.

## Decision 1 — Four images, and the split mirrors the trust boundary

| Image | Base | Contents | Size |
|---|---|---|---|
| `api` | `python:3.11-slim` | `[api,storage]` | 414 MB |
| `frontend` | `nginx:1.27-alpine` | static build | 79 MB |
| `spark` | `python:3.11-slim-bookworm` + JRE 17 | `[spark,lake,storage,generators,ingestion,cdc,stream]` | ~2 GB |
| `airflow` | `apache/airflow:2.11.2-python3.11` | `[storage,ingestion,cdc]` | 3.45 GB |

One image carrying every extra would be simpler and about 4 GB, and the argument
against it is not size.

**The API image must not contain the code that reads encrypted Delta.** ADR 0016
put the serving layer outside the attested boundary: it reads PostgreSQL, holds no
key material, and is not in `MEASURED_PACKAGES`. Shipping pyspark, delta-spark and
`confidentia.pipelines` inside the one container with a port open to the outside
would contradict that in the only place it is physically checkable.

**The Airflow image must not contain Spark.** The DAGs shell out to `confidentia
...` (ADR 0013), and a Spark stage is invoked in the `spark` container, not in the
scheduler. Installing pyspark into Airflow would add ~400 MB to an image that would
never use it, and would let a misconfigured DAG run a pipeline stage inside the
orchestrator.

So the image boundary is the same boundary C5 derives and ADR 0016 records. That it
falls in the same place three times is the point.

## Decision 2 — The KMS jar is compiled in the image, never shipped into it

`LocalKmsClient.java` is compiled against **PySpark's own Parquet jars**, not
against a Maven-resolved Parquet, so the client cannot be built against a different
Parquet version than the one Spark loads — a mismatch that surfaces as an
`AbstractMethodError` deep inside a Spark task.

That forces a two-stage build whose first stage installs the whole of PySpark to
compile a single file. It is worth it: the alternative is copying in a jar built on
a developer's machine, and this stage is the only thing that guarantees the jar and
the Spark beside it agree. `.dockerignore` excludes `*.jar` from the context so a
stale local build cannot be picked up by accident.

## Decision 3 — The Spark base is pinned to Debian 12

`python:3.11-slim` now resolves to Debian 13 (trixie), which has **dropped the
openjdk-17 packages**; the build fails with `Unable to locate package
openjdk-17-jdk-headless`. Spark 3.5 supports Java 8/11/17 only, and 21 fails with
opaque module-access errors rather than a version message.

So the base is pinned to `python:3.11-slim-bookworm` rather than the JDK being
worked around. This is a real dependency on Debian 12's support window: when it
expires, the choice is Spark 4 (which supports 17 and 21) or a Temurin base.

## Decision 4 — Secrets at run time, never in a layer

`CONFIDENTIA_KEYSTORE_PASSPHRASE` reaches Airflow's `BashOperator` tasks through
the environment and is never a build argument. A build arg is recorded in the image
history and readable with `docker history`.

It is declared as `${CONFIDENTIA_KEYSTORE_PASSPHRASE:-}` and **not** with the
required-variable marker `:?`. Compose interpolates *before* it filters profiles,
so `:?` makes `docker compose --profile core config` fail on a variable the core
profile does not use — turning the validation job into a secrets check. The platform
already refuses to open the keystore without it, with a clearer message than compose
would give.

## Decision 5 — `compose-validate` is replaced by something that means what it says

The old job proved only that the YAML parsed. `scripts/verify_docker.sh` checks four
things in increasing cost:

1. Every `dockerfile:` a service names exists — the gap that hid for eleven stages.
2. Every `./` bind mount exists. A missing host path is not an error to Docker: it
   silently creates an empty directory, which is how Prometheus would start with no
   configuration and look like a broken container instead of a missing file.
3. Every profile interpolates and parses.
4. Every image builds, the `core` profile comes up, and it answers — including that
   nginx proxies `/api`, that the SPA history fallback works, that an
   unauthenticated request is still refused, and that the init script created both
   databases.

Steps 1–3 take seconds and run on every push. Step 4 needs a daemon and minutes,
and runs as its own job.

The list of Dockerfiles is **parsed out of the compose file**, not hard-coded — a
hard-coded list goes stale the moment a service is added, which is precisely the
failure this replaces.

## Consequences

- `docker compose --profile core up -d` now works and serves the console. The
  `batch` and `stream` profiles build; whether they run on a 3.9 GB WSL VM is a
  separate question and the memory limits in the compose file are the answer.
- The `demo` profile still cannot come up: `monitoring/prometheus` and
  `monitoring/grafana` do not exist, and `verify_docker.sh` reports them as
  missing bind mounts rather than letting Docker invent empty directories. That is
  Stage 12.
- ADR 0001 stands. Local development remains native processes in WSL; containers
  are the portable path, and now a checked one.
