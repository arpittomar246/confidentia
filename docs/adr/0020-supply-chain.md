# ADR 0020 — What the security gate fails on, and what it only reports

- **Status:** Accepted
- **Date:** 2026-08-13
- **Stage:** 14 (CI/CD and supply chain)

## Context

Stage 13 built four images. Nobody had scanned them. The first scan found 39
CRITICAL and 427 HIGH across the four, which is the point at which a project either
designs a policy or starts ignoring a number.

The failure mode is specific: a gate that fails on everything is red forever, and a
red job that cannot be made green is one people stop reading. It is the same
reasoning that shaped the alert rules in ADR 0019 — a signal nobody can act on
trains people to ignore the channel — applied to a scanner instead of a pager.

## Decision 1 — Gate on what this repository can act on

Two filters, applied in order.

**`--ignore-unfixed`.** The Debian and Alpine bases carry CVEs with no published fix
at all: `perl-base`, `ncurses`, `linux-libc-dev`. Nobody can do anything about those
today, so they are reported to the Security tab and never gate.

**Only two of the four images are gated.** Measured 2026-08-13, fixable
HIGH/CRITICAL after the fixes described below:

| Image | Fixable | Gated | Why |
|---|---|---|---|
| `frontend` | **0** | yes | Base image is ours to choose |
| `api` | **0** | yes | Dependencies are ours to pin |
| `spark` | 55 | no | Java jars **inside the pyspark wheel** |
| `airflow` | 125 | no | The upstream official image and its providers |

Spark's findings are `org.apache.derby`, `zookeeper`, `avro`, `thrift`,
`protobuf-java` — shipped inside `pyspark==3.5.3`. Airflow's include `ray` and
`litellm`, which arrive with the official image's provider set. Trivy calls these
fixable because a fix exists *in the upstream library*, not because this repository
can apply it without vendoring a patched Spark or abandoning the official Airflow
image.

Gating on them would mean permanent red. Reporting them means the number is visible
in the Security tab and in the job summary, and the decision to accept it is
written here rather than implied by a passing build.

**`gate: true` is a claim**: these two images are held at zero, and the build fails
if that stops being true.

## Decision 2 — Fix what is cheap, and say what the fix actually was

Three changes came out of the first scan, and none of them was a code change.

**`nginx:1.27-alpine` → `1.31-alpine`.** 2 CRITICAL and 33 HIGH in the base, all
openssl and libxml2 with published fixes. Not nginx bugs — an Alpine layer that had
stopped being rebuilt. 1.29 was measured too and still carried 11 fixable HIGH; 1.31
measures zero. Pinned to a minor version rather than floating `alpine`, so a base
bump is a reviewed commit and the number above stays checkable.

**pip's build tooling brought current in the API image**, for `wheel` and
`jaraco.context`.

**pip removed from the API runtime image entirely.** This one is worth stating
because the scanner was right and the obvious reading was wrong. Trivy reported
`setuptools 70.3.0` while `site-packages` held **84.0.0** — the finding was pip's
own *vendored* `pkg_resources`, which the scanner detects as a separate package.
The same was true of a `msgpack` advisory that appeared mid-session: also vendored
by pip. Removing pip removes both, and it is right independently: a runtime
container has no business installing packages, since `pip install` inside a running
service is how a compromised process fetches its second stage. The editable install
survives, because it is a `.pth` file rather than a pip feature.

## Decision 3 — One dependency advisory is accepted, with the upstream text as the reason

`pip-audit` reports **PYSEC-2026-113** against `pyarrow 17.0.0`, fixed in 23.0.1.

It is ignored, and not because upgrading is inconvenient. The advisory says so
itself: the use-after-free is in Arrow C++ behind
`RecordBatchFileReader::PreBufferMetadata`, and *"the functionality is not exposed
in language bindings (Python, Ruby, C GLib), so these bindings are not
vulnerable."* This project is Python only.

Worth recording what was checked rather than assumed: **nothing actually requires
`pyarrow<18`.** `pyspark 3.5.3` asks for `>=4.0.0`, `delta-spark 3.2.1` has no pin,
`deltalake 0.25.5` wants `>=16,!=19.0.0`. The upper bound is this project's own
conservatism about Spark's Arrow integration. So even if the advisory did apply,
"upgrade to 23.0.1" would be a Spark compatibility exercise rather than a patch —
which is exactly the kind of thing that should be written down before someone reads
`pyarrow<18` as a hard constraint.

## Decision 4 — Provenance, because that is what this project is about

The release workflow attests every image it publishes using
`actions/attest-build-provenance`, signed through Sigstore with the workflow's OIDC
identity. No key to manage and none to lose. What the attestation states is "this
digest was built by this workflow, from this commit", checkable afterwards with
`gh attestation verify`.

The parallel with the platform is exact and is the reason this is here rather than
being skipped as ceremony. CONFIDENTIA refuses to release a key to code whose
measurement it has not approved (C2), and signs a Merkle root over every table so
provenance can be verified rather than believed (C3). The release pipeline does the
same thing one layer up, for the artefacts rather than the data. A project arguing
that data provenance should be verifiable and shipping unattested images would be
making its own case badly.

CycloneDX SBOMs are generated for every image whether or not it is published,
because "which packages went into this artefact" is the same question the lineage
chain answers about data.

## Decision 5 — The release pipeline has a dry run, and it was used

Stage 13's lesson was that a compose file "validated" for eleven stages had never
been built. A release workflow nobody has executed is the same mistake with a worse
failure window, since the first execution would be a tagged release.

`workflow_dispatch` with `push_images: false` builds every image and generates every
SBOM while pushing nothing and attesting nothing.

## Consequences

- The gate will go red when a new advisory lands against a fixable package in the
  API or frontend image, on someone else's schedule rather than this project's.
  That is the design working, not a defect: the weekly schedule exists precisely
  because code not changing is when a new advisory is most likely to be missed.
- The unfixable and un-actionable findings are visible in the Security tab and
  excluded from the gate. Anyone claiming this stack is free of known
  vulnerabilities is contradicted by the same tab.
- `.trivyignore` is empty and documented as needing a reason and a review date per
  entry. An ignore file without them is how a scanner becomes decoration.
