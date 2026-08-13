# Stage 13 — Containerisation

`docker-compose.yml` has existed since Stage 1 as the portable and cloud
deliverable. It referenced four Dockerfiles that did not exist, and the
`compose-validate` CI job passed anyway for eleven stages, because `docker compose
config` checks syntax and interpolation and **nothing else**.

Stage 13 writes the four images, builds them, brings the stack up, and replaces the
job that was asserting less than its name.

Every claim below was produced by running `scripts/verify_docker.sh`.

## What was built

| Image | Base | Contents | Size | Build |
|---|---|---|---|---|
| `api` | `python:3.11-slim` | `[api,storage]` | **414 MB** | 59 s |
| `frontend` | `nginx:1.27-alpine` | static build | **79.2 MB** | 26 s |
| `spark` | `python:3.11-slim-bookworm` + JRE 17 | `[spark,lake,storage,generators,ingestion,cdc,stream]` | **2.08 GB** | ~11 min |
| `airflow` | `apache/airflow:2.11.2-python3.11` | `[storage,ingestion,cdc]` | **3.45 GB** | 2 m 24 s |

Plus `.dockerignore`, without which the build context includes `.venv`,
`frontend/node_modules` and `.git` — several gigabytes uploaded on every build to be
discarded.

## The image boundary is the trust boundary

Four images rather than one, and the argument is not size.

**The API image must not contain the code that reads encrypted Delta.** ADR 0016 put
the serving layer outside the attested boundary: it reads PostgreSQL, holds no key
material, is not in `MEASURED_PACKAGES`. Shipping pyspark and
`confidentia.pipelines` into the one container with a port open to the outside would
contradict that in the only place it is physically checkable.

**The Airflow image must not contain Spark.** The DAGs shell out to `confidentia
...` (ADR 0013) and a Spark stage runs in the `spark` container. Installing pyspark
into Airflow would let a misconfigured DAG run a pipeline stage inside the
orchestrator.

So the image split falls where C5 derives the enclave boundary and where ADR 0016
puts the serving layer. Three independent lines landing in the same place.

## Five things that only building could find

Every one of these was invisible to `docker compose config`.

### 1. `python:3.11-slim` no longer has OpenJDK 17

```
E: Unable to locate package openjdk-17-jdk-headless
```

The tag now resolves to Debian 13 (trixie), which dropped the openjdk-17 packages.
Spark 3.5 supports Java 8/11/17 **only** — 21 fails with opaque module-access errors
rather than a version message. The base is pinned to `python:3.11-slim-bookworm`
rather than the JDK worked around, which is a real dependency on Debian 12's support
window.

### 2. postgres:18 changed where its data lives

```
Error: in 18+, these Docker images are configured to store database data in a
       format which is compatible with "pg_ctlcluster"...
       Counter to that, there appears to be PostgreSQL data in:
         /var/lib/postgresql/data (unused mount/volume)
```

The compose file mounted `pgdata:/var/lib/postgresql/data`, the correct path for the
16-era image it was written against. From 18 the mount must be
`/var/lib/postgresql`, so that data lands in a major-version subdirectory and
`pg_upgrade --link` can work across one mount point. The container **exits 1** with
the old path, which then fails `api` on its `depends_on` — one wrong path, three
services down.

This surfaced only because the image was also bumped 16 → 18 to match the
development host and the CI service container.

### 3. `scripts/sql/init` did not exist

Compose mounts it into the Postgres container's `docker-entrypoint-initdb.d`.
Docker does not error on a missing bind-mount source — **it silently creates an
empty directory and mounts that**. So Postgres would start with no init script, the
`airflow` and `ehr_oltp` databases would never be created, and the failure would
appear later as Airflow being unable to reach a database nobody had made.

Now written, and idempotent by omission: it creates databases and nothing else.
Schema is the migrations' job.

### 4. Publishing the default ports collides with the development machine

```
failed to bind host port 0.0.0.0:5432/tcp: address already in use
```

Postgres, the API and Vite all run natively on this host on exactly the numbers the
compose file published. Verifying the containers would have required stopping the
developer's database, which nobody would do twice.

Every published host port is now overridable — `${POSTGRES_PORT:-5432}` and so on —
with container-side ports unchanged, so service-to-service traffic is unaffected.
`verify_docker.sh` publishes on an offset set (15432/18000/15173) and therefore runs
happily alongside the native stack.

### 5. A required-variable marker breaks profile validation

`CONFIDENTIA_KEYSTORE_PASSPHRASE` was first declared with `:?`, which is the right
instinct — the platform has no fallback for it. But compose interpolates **before**
it filters profiles, so `docker compose --profile core config` failed on a variable
the core profile does not use, turning the validation job into a secrets check. All
five profiles failed at once.

Now `:-`, with the reason written down. The platform already refuses to open the
keystore without it, and says so more clearly than compose would.

## Replacing `compose-validate`

`scripts/verify_docker.sh` checks four things in increasing cost:

1. **Every `dockerfile:` a service names exists.** The gap that hid for eleven
   stages. Parsed out of the compose file, not hard-coded — a hard-coded list goes
   stale the moment a service is added, which is exactly the failure being replaced.
2. **Every `./` bind mount exists and is non-empty.** Emptiness counts as missing:
   `monitoring/prometheus` exists on this machine as an empty directory from Stage 1,
   and git does not track empty directories, so an existence-only check would pass
   locally and fail in CI. Paths whose owning stage has not run are reported
   `pending` with the stage named, and do not fail.
3. **Every profile interpolates and parses.**
4. **Every image builds, and the `core` profile comes up and answers.**

```
core profile
  ok    api /health
  ok    frontend serves the console
  ok    frontend proxies /api to the api service
  ok    SPA history fallback on /evaluation
  ok    api refuses an unauthenticated request
  ok    api reaches postgres over the compose network
  ok    database ehr_oltp created by the init script
  ok    database airflow created by the init script
```

Two CI jobs now: `compose-validate` runs steps 1–3 in seconds with no daemon;
`container-build` runs the whole thing, reclaiming runner disk first because the
images total about 6 GB with layers.

## Docker in WSL, and ADR 0001

ADR 0001 chose native processes over Docker for local development because Docker
Desktop's VM costs 1.5–2 GB on a 7.4 GB host. **That decision is unchanged.** This
stage installed Docker **Engine** inside the WSL distro — a daemon, not a VM, which
costs nothing when idle. Local development remains native processes; containers are
the portable path, and now a checked one.

The distro has no systemd (`pid 1` is `init(Ubuntu)`), so `dockerd` is started
directly rather than through a unit.

## Known gaps

1. **The `demo` profile still cannot come up.** `monitoring/prometheus` and
   `monitoring/grafana` are empty. `verify_docker.sh` reports them as `pending`
   rather than letting Docker invent empty directories. Stage 12.
2. **`batch` and `stream` are built but not brought up.** Both build and both
   validate; whether Spark and Airflow run together inside a 3.9 GB WSL VM is a
   different question, and the memory limits in the compose file are the answer
   rather than a claim I have tested.
3. **The Airflow image is 3.45 GB.** Almost all of it is the upstream base. Slimming
   it means building Airflow from a plain Python base against its constraints file,
   which `scripts/install_airflow.sh` does locally and which is a fragile thing to
   own in a Dockerfile.
4. **No image is published anywhere.** No registry, no tags beyond `latest`, no
   multi-arch build. Everything here is `docker compose build` on one machine.

## Reproducing

```bash
bash scripts/verify_docker.sh --static   # seconds, no daemon: what CI runs on push
bash scripts/verify_docker.sh            # builds everything and brings core up
POSTGRES_PORT=15432 API_PUBLISHED_PORT=18000 FRONTEND_PORT=15173 \
  docker compose --profile core up -d
```
