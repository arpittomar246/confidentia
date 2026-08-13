# Stage 14 — CI/CD and supply chain

Two new workflows and one change to the existing one. The security workflow is not
decoration: the first scan of the images Stage 13 built found **39 CRITICAL and 427
HIGH**, and deciding what to do about that is the whole content of this stage.

## What was built

| | |
|---|---|
| `.github/workflows/security.yml` | pip-audit, Trivy on four images, secret scan of full history, weekly |
| `.github/workflows/release.yml` | Tag → GHCR + SBOM + Sigstore provenance, with a dry run |
| `.github/workflows/ci.yml` | Concurrency group: cancel superseded runs |
| `.trivyignore` | Empty, and documented as needing a reason and review date per entry |

## The first scan, and what it actually said

```
image       CRITICAL  HIGH
frontend           2    33     openssl 3.3.3-r0, libxml2 2.13.4-r5 — fixes published
api                4    21     perl-base (no fix), wheel + jaraco.context (fixes)
spark              ?     ?     jars inside the pyspark wheel
airflow           33   373     upstream base: ray, litellm, linux-libc-dev
```

Filtering to **what a fix exists for** changes the picture completely:

| Image | Fixable HIGH/CRITICAL | Can this repo act? |
|---|---|---|
| `frontend` | 33 → **0** | Yes — chose a newer base |
| `api` | 2 → **0** | Yes — pinned and removed pip |
| `spark` | 55 | No — inside `pyspark==3.5.3` |
| `airflow` | 125 | No — the official image and its providers |

So the gate fails on `api` and `frontend` only, and reports the other two. Spark's
findings are `org.apache.derby`, `zookeeper`, `avro`, `thrift`, `protobuf-java` —
jars shipped inside the pyspark wheel. Trivy calls them fixable because a fix exists
in the upstream library, not because this repository can apply it without vendoring
a patched Spark.

Gating on those would mean permanent red, and a red job that cannot be made green
is one people stop reading. Same reasoning as the alert rules in Stage 12.

## Three fixes, none of them a code change

**`nginx:1.27-alpine` → `1.31-alpine`.** The base had stopped being rebuilt.
Measured all three candidates rather than assuming newer is better:

| Base | nginx | Fixable HIGH/CRITICAL |
|---|---|---|
| `1.27-alpine` | 1.27.5 | 2 CRITICAL, 33 HIGH |
| `1.29-alpine` | 1.29.8 | 0 CRITICAL, 11 HIGH |
| `1.31-alpine` | 1.31.3 | **0** |

**pip's build tooling brought current** in the API image — `wheel`,
`jaraco.context`.

**pip removed from the API runtime image**, and this is the one worth reading. Trivy
reported `setuptools 70.3.0` while `site-packages` held **84.0.0**. The finding was
pip's own *vendored* `pkg_resources`, detected as a separate package. A `msgpack`
advisory that appeared mid-session was the same thing — also vendored by pip.

Removing pip removes both, and it is right independently: a runtime container has no
business installing packages, since `pip install` inside a running service is how a
compromised process fetches its second stage. The editable install survives, because
it is a `.pth` file rather than a pip feature. Verified the image still runs:

```
$ docker run --rm --entrypoint confidentia confidentia-api:latest info
environment        : local
data root          : /var/lib/confidentia
```

Both gated images now measure zero, and the whole stack still comes up — the Stage
12 and 13 verifications were re-run after the base changes, not assumed.

## The dependency advisory that is accepted

`pip-audit` reports **PYSEC-2026-113** against `pyarrow 17.0.0`, fixed in 23.0.1.
Ignored, with the advisory's own words as the reason:

> The functionality is not exposed in language bindings (Python, Ruby, C GLib), so
> **these bindings are not vulnerable.**

The use-after-free is in Arrow C++ behind `RecordBatchFileReader::PreBufferMetadata`.
This project is Python only.

Checked rather than assumed while there: **nothing actually requires `pyarrow<18`.**
`pyspark 3.5.3` asks `>=4.0.0`, `delta-spark 3.2.1` has no pin, `deltalake 0.25.5`
wants `>=16,!=19.0.0`. The upper bound is this project's own conservatism about
Spark's Arrow integration — worth knowing before someone reads it as a hard
constraint.

## Provenance, because that is what this project argues for

The release workflow attests every published image with
`actions/attest-build-provenance` — Sigstore, signed with the workflow's OIDC
identity, no key to manage. The attestation states that a digest was built by this
workflow from this commit, checkable with:

```bash
gh attestation verify oci://ghcr.io/arpittomar246/confidentia-api:0.1.0 \
  --repo arpittomar246/confidentia
```

The parallel is exact and is why this is here rather than skipped as ceremony. The
platform refuses to release a key to code whose measurement it has not approved
(C2), and signs a Merkle root over every table so provenance is verified rather than
believed (C3). The release pipeline does the same one layer up, for artefacts
instead of data. A project arguing that data provenance should be verifiable, while
shipping unattested images, would be making its own case badly.

CycloneDX SBOMs are produced for every image whether or not it is published.

## The release pipeline has a dry run

Stage 13's lesson was a compose file "validated" for eleven stages that had never
been built. A release workflow nobody has executed is the same mistake with a worse
failure window — the first execution would be a tagged release.

`workflow_dispatch` with `push_images: false` builds everything and generates every
SBOM while pushing nothing and attesting nothing.

## Concurrency

`ci.yml` now cancels superseded runs. Two of the last four pushes were a fix landing
minutes after the commit it fixed, and the older run kept building images nobody
would look at — `container-build` alone is six minutes.

`main` is included deliberately. Cancelling on `main` sounds wrong until you notice
that a superseded run reports on a commit that is no longer HEAD.

The release workflow sets `cancel-in-progress: false`: never cancel a release
half-published.

## Known gaps

1. **No release has been published.** The workflow has a dry run and the tag path
   is untested end to end — GHCR push, attestation and the GitHub Release step have
   never executed. Publishing creates packages under the account, so it needs a
   deliberate `git tag v0.1.0 && git push --tags`.
2. **The gate will go red on someone else's schedule.** A new advisory against a
   fixable package in the API or frontend image fails the build without anything
   here changing. That is the design working — the weekly schedule exists because
   code not changing is exactly when a new advisory gets missed — but it will look
   like a broken build the first time.
3. **`spark` and `airflow` carry 180 fixable findings between them and ship
   anyway.** Reported, not gated, and the reason is written down. Nobody should read
   a green CI badge as "this stack has no known vulnerabilities".
4. **No branch protection or required checks.** Those are repository settings rather
   than files, and this repository is a single-author private one where they would
   be ceremony.
5. **No dependency update automation.** Dependabot or Renovate would keep the two
   gated images green with less effort than a person; not configured.

## Reproducing

```bash
pip-audit --strict --ignore-vuln PYSEC-2026-113
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy image \
  --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed confidentia-api:latest
gh workflow run release.yml -f push_images=false      # the dry run
```
