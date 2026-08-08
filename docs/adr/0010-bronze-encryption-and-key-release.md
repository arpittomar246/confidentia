# ADR 0010 — Bronze layer: key hierarchy, attestation gating, and verified writes

- **Status:** Accepted
- **Date:** 2026-08-08
- **Stage:** 4 (Bronze)
- **Realises:** C1 (crypto-partitioned storage), C2 (attestation-gated key release)

## Decision 1 — Derive keys, do not store them

```
MEK  ──HKDF(info="tenant:<t>")──▶  KEK per tenant
                                    └─HKDF(info="class:<s>")──▶  DEK per sensitivity class
```

Storing a key per tenant per class means a key database that must itself be backed
up, replicated and access-controlled — a second security-critical system. Deriving
from one root means the only secret at rest is the MEK, and adding a tenant or a
class requires no key ceremony.

**Cost, accepted deliberately:** a single DEK cannot be rotated independently.
Rotation is per-MEK-version and re-encrypts everything beneath it. Acceptable
because sensitivity classes are few and stable.

**Verified:** healthcare and financial key material has zero overlap; derivation is
deterministic across processes; each class gets a distinct key.

## Decision 2 — Tenant isolation is cryptographic, not policy

`healthcare` and `financial` derive from different KEKs. A principal holding the
healthcare PHI key cannot read financial columns — not because a check forbids it,
but because the key material is unrelated. Policy checks can be bypassed by a bug;
key separation cannot.

## Decision 3 — No key material in SparkConf

The obvious wiring is `.config("spark.hadoop.parquet.encryption.key.list", ...)`,
which is what the Stage 1 spike did. **Every `spark.*` property is rendered in the
Spark UI's Environment tab** and returned by `sc.getConf().toDebugString()`, and
Spark's default `spark.redaction.regex` matches only `secret|password|token` — so
base64 master keys would have been displayed in a browser.

Key material is therefore set on the **Hadoop Configuration after** session
creation, which the UI does not display. The redaction regex is additionally
widened to cover `key.list|passphrase|credential` as defence in depth.

This was found by reasoning about the deployment surface, not by a test. It is
worth recording precisely because nothing would have failed.

## Decision 4 — A Spark session is scoped to one tenant

PME configuration is session-global (Decision 3 of ADR 0007: it must live on the
Hadoop Configuration, not writer options). Serving two tenants from one session
would mean mutating that configuration between writes, and a mistake there writes
one tenant's data under another tenant's key — silently, and undetectably after the
fact. `session_for_tenant` makes the scope explicit and releases keys through the
broker as part of session setup.

## Decision 5 — Attestation-gated key release, and what it costs

No component gets keys by asking. It presents evidence answering a broker-issued
nonce; the measurement must appear in an approved policy for the requested tenant.

Four refusal paths, all verified:

| Attack / condition | Result |
|---|---|
| Replayed evidence (stale nonce) | denied — nonce mismatch |
| Forged measurement | denied — signature does not verify |
| Pipeline code changed | denied — measurement not approved |
| Approved measurement, wrong tenant | denied — tenant not permitted |

**The operational cost is real and was felt immediately.** Adding
`pipelines/bronze/writer.py` changed the measurement from `10de202e…` to
`9baf7e8e…`, and the pipeline could no longer decrypt its own data. Every
subsequent code change to a measured package required `confidentia keys approve`.

That is the feature, not a defect: it makes "which code touched this data"
enforceable rather than merely recorded. But it is a genuine tax on development,
and in a real deployment approval must be a human decision against a reviewed
build. The measured set is deliberately narrow (`crypto`, `pipelines`, `quality`,
`lineage`) so that editing a test or a document does not revoke every key.

**Under `sim` this is a mechanism, not a security boundary.** The process measures
itself and could lie. Policy rows and audit rows both carry `hardware_rooted =
false` so a simulated approval can never be mistaken for a hardware-backed one.

## Decision 6 — Every write is verified, never trusted

ADR 0007 established that Delta writes plaintext silently when encryption is
misconfigured. Every Bronze write therefore ends by reading the bytes of the
largest output file and asserting `PARE` magic; failure raises
`EncryptionNotAppliedError` and fails the run.

**A byte search alone is not sufficient evidence.** Parquet dictionary-encodes and
Snappy-compresses, so a value's absence from the raw bytes can be a compression
artefact. Observed directly: `encounter_type` is classified PUBLIC and therefore
written in plaintext, yet the literal `outpatient` did not appear in the file.

The definitive test is a **partial-key read**, run in a separate process because
Parquet's `KeyToolkit` caches unwrapped keys per JVM:

| Keys held | `primary_diagnosis_code` (PHI) | `encounter_id` (INTERNAL) | `facility_id`, `encounter_type` (PUBLIC) |
|---|---|---|---|
| all | OK | OK | OK |
| all except PHI | **DENIED** | OK | OK |
| all except INTERNAL | — | **DENIED** | OK |

`scripts/verify_bronze_isolation.sh` runs this.

## Decision 7 — Public columns stay in plaintext

Columns classified `public` are omitted from the column-key mapping entirely, so
Parquet leaves them unencrypted. That omission is what preserves predicate pushdown
and partition pruning. `validate_contract` refuses any contract whose partition
column is not public — an encrypted partition column makes every pruned query a
full scan, which is worse than not partitioning.

## Decision 8 — Bronze partitions on ingest date, not event date

A late-arriving record belongs to the batch that delivered it. Partitioning raw
data on a value inside the data means a backfill rewrites historical partitions,
destroying the "what did the source send us on Tuesday" property that landing and
Bronze exist to preserve. Event-time partitioning starts at Silver.

**Current limitation:** all ingestion so far happened on one day, so there is a
single partition and pruning is not yet demonstrated.

## Decision 9 — Reconcile schemas per batch, against the contract

Batches of the same dataset genuinely arrive with different physical types: the
file collector lands `admit_ts` as an INT64 timestamp, the REST collector lands it
as a string because the payload round-tripped through JSON. Reading them as one
path fails with `SchemaColumnConvertNotSupportedException`.

Each batch is therefore read separately and cast to the contract before being
unioned. The contract is the reconciliation point, which also absorbs additive
schema drift — a declared-but-absent column materialises as a typed NULL.

## Decision 10 — Bronze keeps values raw, including bad ones

Monetary columns are typed `string` in Bronze, and casts are permissive: a value
that will not cast becomes NULL rather than failing the batch.

Bronze must accept what arrived. The type-drift defects injected in Stage 2 (`"N/A"`
in a numeric column) exist precisely so Stage 7 can detect and quarantine them.
Rejecting them here would lose the records and defeat the purpose of a raw layer.

## Bug found: promotion tracking must not depend on ingest status

`_promoted_batches` originally filtered on `status = 'ingested'`. A batch left
`pending` by the interrupted ingest run from the Stage 3 corruption test was
therefore invisible to the check and re-promoted on **every** run — the encounter
table silently doubled to 241,596 rows.

Promotion to Bronze and the ingest registry's status are independent facts.
Coupling them was wrong. `_mark_promoted_by_prefix` now also raises if a landing
batch has no registry row, rather than leaving it permanently un-promotable.
