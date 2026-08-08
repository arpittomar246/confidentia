# ADR 0001 — Local runtime environment

- **Status:** Accepted
- **Date:** 2026-08-08
- **Stage:** 1

## Context

Development host: Windows 11, **7.4 GB RAM**, 4 cores, ~36 GB free disk. The platform
needs Spark, and eventually Postgres, Kafka, MinIO, Airflow and a web stack.

Three constraints surfaced during Stage 1 setup:

1. **Spark cannot run on Windows without `winutils`.** Hadoop's `Shell` class fails at
   static initialisation with `FileNotFoundException: HADOOP_HOME and hadoop.home.dir
   are unset`, and unsetting the variable does not help — it is a hard dependency on
   `winutils.exe` and `hadoop.dll`, which are distributed only through unofficial
   community mirrors. (The host also carried a stale `HADOOP_HOME` pointing at a
   non-existent `C:\hadoop-3.3.1`, unrelated to this project.)
2. **Ubuntu 26.04 ships Python 3.14 only.** PySpark 3.5 does not support it.
3. **Docker Desktop would cost 1.5–2 GB of RAM** before a single service starts, on a
   host with 7.4 GB total.

## Decision

**Develop inside WSL2 Ubuntu, running services as native Linux processes. No Docker
locally.**

- Repository lives in the Linux filesystem at `~/confidentia`, reachable from Windows
  at `\\wsl.localhost\Ubuntu\home\arpit\confidentia`.
- Data root is `~/confidentia-data`, also in the Linux filesystem.
- Python 3.11 is provisioned by `uv` (standalone build), sidestepping the distro's
  Python 3.14 without adding a PPA or a second distro.
- WSL is capped at 4 GB RAM / 4 GB swap via `.wslconfig`, leaving Windows ~3 GB.
- `docker-compose.yml` is maintained as the **portable and cloud** path and validated in
  CI on every push, but is not the local development loop.

## Alternatives rejected

**Download `winutils` and run on Windows.** Fastest to unblock, but it means executing
an unofficial binary with the developer's privileges, and Spark-on-Windows carries
ongoing friction (file locking, path handling, slower local filesystem) while diverging
from the Linux target the project actually deploys to.

**Docker Desktop locally.** Closest to the deployment story and would make Stage 14
trivial, but it needs WSL2 anyway, adds ~5 GB disk, and its VM overhead would squeeze
Spark on a 7.4 GB host. Deferred; it can be adopted later without changing any code,
since the Compose files are kept current.

**A second distro (Ubuntu 24.04) for Python 3.12.** Would have worked, but costs several
GB of disk for something `uv` solves in a 30 MB download, and 3.11 is the better-tested
pairing with PySpark 3.5 regardless.

## Consequences

- One runtime to reason about (Linux), matching containers and cloud.
- Keeping the repository out of OneDrive is now structural rather than advisory. Delta
  transaction logs and Spark spill files must not be touched by a sync agent;
  `Settings` refuses to start if `data_root` resolves inside OneDrive or Dropbox.
- Source is no longer backed up by OneDrive as a side effect. Git is the backup
  mechanism, so committing regularly is not optional.
- Postgres, Kafka and MinIO are installed in the stages that first need them, not
  up front, to spend the disk budget as late as possible.
