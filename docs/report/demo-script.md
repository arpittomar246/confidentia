# Demo script

A 12-minute live demonstration. Every command here has been run; none of it needs
a rebuild during the demo.

**Rehearse it twice before the viva.** The failure mode is not a broken platform —
it is a terminal that has gone stale, a WSL distro that has wound down, or Postgres
not running.

---

## Before the room (15 minutes ahead)

```bash
# WSL terminal 1 — leave this open, it keeps the distro alive
cd ~/confidentia
sudo service postgresql start
export CONFIDENTIA_KEYSTORE_PASSPHRASE="dev-passphrase-not-for-real-use"
source .venv/bin/activate
confidentia info                      # confirms settings load and the label reads MODELLED
```

```bash
# WSL terminal 2 — the console, leave running
cd ~/confidentia && bash scripts/serve_dashboard.sh
```

Open **http://localhost:8001**, sign in with the token it prints, and leave it on
the Overview tab. Check the orange `MODELLED` banner is visible — you will refer
to it.

> **If the dashboard is not reachable**, WSL wound the distro down. Re-run the
> command in a terminal you keep open. Do not debug this in front of an examiner —
> fall back to the CLI, which needs no browser.

---

## The demo

### 1 · Frame it (60 seconds, no terminal)

> "The question is where a confidentiality boundary should fall in a data platform,
> and what putting it there costs. I'll show you the boundary being derived, the
> guarantee being checked from the bytes, and the one place the architecture fails
> its own test."

Point at the **MODELLED** banner.

> "That banner is on every page. There's no TEE hardware behind this — the enclave
> is simulated, and everything derived from it is labelled. I'll be explicit each
> time about which numbers are measured and which are modelled."

### 2 · The encryption guarantee, from the bytes (2 minutes)

```bash
bash scripts/verify_encryption.sh
```

While it runs:

> "This doesn't trust the code. It reads the Parquet footer magic off disk — `PAR1`
> is plaintext, `PARE` is an encrypted footer — greps the raw file for values that
> shouldn't be there, and attempts a keyless read **in a separate OS process**,
> because Parquet caches unwrapped keys per JVM and an in-process retry falsely
> passes.
>
> I know that because my first attempt did exactly that and reported a pass on
> unencrypted data."

### 3 · The boundary, derived (2 minutes) — **the centrepiece**

```bash
confidentia plan placement
```

> "This isn't a diagram I drew. The rule is: a stage runs inside the enclave if and
> only if it sees plaintext of a PII or PHI column. It's applied to the schema
> contracts, so changing a contract changes the answer."

Point at the column counts — 42 at Bronze, 25 from Silver.

> "Tokenisation removes 17 of 42 protected columns — and moves **nothing** out of
> the enclave. Twenty-five quasi-identifiers remain: dates of birth, diagnosis
> codes, geography. They can't be tokenised without destroying the aggregations
> they exist for.
>
> So the thing the platform spends most of its privacy effort on isn't what shrinks
> the boundary. Disclosure control is."

Then point at the last line — `boundary violations: none`:

> "That line used to name a hole. The landing zone wrote unencrypted Parquet before
> Bronze encrypted anything — one landing patient file held nineteen SSN-shaped
> strings — and I found it with the tool I'd built for a different purpose.
>
> It's fixed now, and I'd rather show you the fix than the report of it."

```bash
bash scripts/verify_landing.sh --no-spark
```

> "Footer magic on every file, a raw-byte grep for SSN and email patterns, and a
> keyless read that has to be refused. Twenty-three files, zero identifier-shaped
> strings.
>
> The awkward part was that Bronze is written by Spark and ingestion is pandas, so
> the landing zone is encrypted by pyarrow and read by Spark's Java crypto factory.
> They agree because I implemented the same key-wrapping envelope on both sides.
> That agreement is held up by exactly one CI test — if it drifts, every landing
> file written afterwards becomes unreadable, and you'd find out weeks later at a
> promotion.
>
> Cost: one-point-four percent of storage, measured over 369,000 rows."

And the part worth conceding before being asked:

> "Closing it turned up something worse. The migration found a dataset in the
> landing zone with no schema contract at all — 900 rows of patient address
> changes, landed once by mistake. Never promoted, because Bronze refuses what it
> can't type. Never classified. Never encrypted, not because anything decided it
> shouldn't be, but because nothing knew it was there.
>
> It's a source-side mutation script — the CDC path replays it into Postgres, and
> its effects reach the lake as `patient_cdc`, which is contracted and encrypted. So
> I deleted it rather than writing a contract for it."

```bash
confidentia landing withdrawn
```

> "The ingest ledger keeps the record. Deleting the files while the registry still
> said 'ingested' would make it lie about what's on disk; deleting the row would
> erase the fact this ever arrived, which is the one question the landing zone
> exists to answer. So the row stays, marked withdrawn, with the reason.
>
> And the rule that stops it recurring is one line: the catalog decides what the
> lake ingests, not the shape of a directory tree.
>
> The uncomfortable general lesson, which I'd rather say than be asked: every check
> I'd built covered the path the data was *supposed* to take."

### 4 · Attestation refusing (90 seconds)

```bash
confidentia keys audit | head -20
```

> "Fifteen refusals out of 347 requests. Four paths: unknown measurement, revoked
> policy, tenant not permitted, stale evidence.
>
> Under `sim` this is a mechanism, not a guarantee — the process measures itself and
> could lie. On Azure Managed HSM the same policy is enforced by a service that
> won't release the key.
>
> I used to say that swap was 'configuration plus one Java class'. Writing the
> deployment runbook proved otherwise — nothing read a SEV-SNP report, so selecting
> the hardware backend flipped every label to MEASURED and then failed every key
> release. The reader exists now and has never run on hardware, which the code says
> about itself: `VALIDATED_ON_HARDWARE` is False and rides along in every audit row."

### 5 · Tamper detection (2 minutes)

```bash
bash scripts/verify_lineage.sh
```

> "Four attacks. The last one edits a single row inside the encrypted Bronze table
> and undoes it with Delta time travel."

When it prints `row count unchanged`:

> "That's the point — the table is the same size, one field differs, and the Merkle
> root moves. And it reports *which* of the four attacks it was, because telling an
> operator 'broken link' when the problem is a forged signature wastes the incident."

### 6 · The console (2 minutes)

Switch to the browser. Three clicks, no more:

**Evaluation** →

> "0.891 and 0.251 for the same dataset. At Bronze a duplicate and a legitimate
> redelivery are identical bytes, so every redelivery counts as a false positive.
> The corrected figure is the headline, the raw one sits beside it. Hiding either
> would be dishonest in a different direction."

**Gold marts → Encounters Daily** → scroll to a suppressed row.

> "Purple 'suppressed' is k-anonymity, not missing data. `_suppression_reason` says
> `below_k_5`. If the UI rendered that as zero or as blank, someone would
> reconstruct it by subtracting from the published total — which is exactly what
> complementary suppression prevents."

**Lineage → Re-verify** →

> "Three ticks and one cross. The cross is deliberate: this checks the *record* of
> provenance, not that the lake still matches. Deep verification needs Spark and is
> CLI-only. A green badge that didn't say what it hadn't checked would be a lie by
> omission."

### 7 · One measured number (90 seconds)

```bash
confidentia bench results
```

> "Encryption costs 4.2% of write time and 0.1% of storage — measured, 3 million
> rows, five interleaved repeats.
>
> Run at 200,000 rows it reported encryption being *faster*, which is impossible.
> Not warm-up — scale. Below about a million rows Spark's fixed job cost swamps the
> signal. The tool now warns you."

### 8 · Close (30 seconds)

> "Five contributions, six of eight experiments measured, 403 tests, 14 CI jobs.
>
> The result I didn't expect is that tokenising identifiers doesn't shrink the
> boundary — quasi-identifiers do that work, which makes disclosure control
> load-bearing rather than cosmetic.
>
> And the tool that produced that result found a plaintext leak in my own
> architecture, which I then closed — and closing it turned up a second one the
> tool hadn't been looking for. That's the part I'd want judged: every claim here
> has a check behind it, the checks found things, and when a check's conclusion
> stopped being true I rewrote it to re-derive the answer instead of repeating it."

---

## Fallbacks

| If | Do |
|---|---|
| Dashboard unreachable | Skip §6. The CLI carries the demo; say "the console is a read-only view over what you've just seen." |
| Postgres down | `sudo service postgresql start`, wait 5 s. Everything except §2 needs it. |
| A script is slow | `verify_lineage.sh` takes ~40 s. Talk over it — that is what §5's narration is for. |
| Asked to show the code | `src/confidentia/tee/placement.py` — the rule is one function, `place()`, about 30 lines. |
| Asked "show me a failure" | `confidentia quality failures --limit 5`, or the 15 refusals in §4. |

## What not to do

- **Do not run `rebuild_pipeline.sh`.** Ten minutes, and it rewrites the data the
  rest of the demo shows.
- **Do not open a fresh terminal mid-demo.** The distro may have wound down.
- **Do not claim anything is hardware-attested.** The banner says MODELLED; say it
  too, and the rest of your claims gain credibility rather than lose it.
