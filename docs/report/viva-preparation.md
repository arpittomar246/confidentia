# Viva preparation

Questions an examiner is likely to ask, with answers grounded in what this project
actually did. Where a question has no good answer, the honest one is given — a
concession made early is worth more than a defence that collapses.

---

## The three questions that will decide the viva

### Q1. "You have no TEE hardware. So what have you actually shown?"

**Do not get defensive. Concede the boundary immediately, then draw the line.**

> Nothing about enclave *performance* is measured, and I label every such number
> MODELLED in the code, the database and the report. `TeeSettings.is_measured`
> returns False under `sim` and every audit row carries `hardware_rooted = false`.
>
> What is measured is everything that does not need an enclave: column encryption
> costs 4.2% of write time and 0.1% of storage, at 3 million rows with warm-up and
> five interleaved repeats. Quality rule precision and recall against a ground-truth
> answer key the pipeline is structurally forbidden from reading. Four tamper
> attacks on the lineage chain, each detected for the correct reason.
>
> And the main contribution does not depend on hardware at all. **Where the boundary
> falls is a derivation from the schema contracts, not a measurement.** It would give
> the same answer on real SGX. The cost of putting it there is modelled; the
> placement is not.

**If pressed — "isn't attestation meaningless then?"**

> Under `sim`, yes, as a guarantee. The process measures itself and could lie, and I
> say so in §4. What is real is the mechanism: an allowlist, four refusal paths, and
> an audit that records 15 refusals out of 347 requests. On Azure Managed HSM the
> same policy is enforced by a service that will not release the key, and §9 shows
> the change is configuration plus one Java class.

### Q2. "What is the actual contribution? This looks like engineering."

**Lead with the finding, not the platform.**

> The finding is that **tokenising direct identifiers does not shrink the
> confidentiality boundary.** That is what the platform spends most of its privacy
> effort on, and the placement planner shows it removes 17 of 42 protected columns
> and moves *no stage* out of the enclave — because 25 quasi-identifiers remain and
> cannot be tokenised without destroying the aggregations they exist for.
>
> Which reframes what disclosure control is for. It is not a nice-to-have on the
> output; it is the only thing that makes anything publishable, because
> quasi-identifiers keep the *computation* inside the boundary right up until the
> aggregate is suppressed.
>
> The engineering is what makes that claim checkable rather than asserted. The rule
> is applied to the contracts by code, so changing a contract changes the answer,
> and a unit test pins the current one so a change shows up as a failure.

### Q3. "How do I know any of these numbers are real?"

> Three ways, and I would rather you check than take my word.
>
> **They are recorded, not typed.** Every experiment result is in
> `platform.benchmark_result` with the machine configuration, the repeat count and
> the spread. `confidentia bench results` prints them.
>
> **The checks run on every push.** 14 CI jobs. The encryption guarantee is
> re-proved from the bytes on every commit, because it once produced a false pass —
> the "encrypted" arm asserted only a round-trip, which an unencrypted write also
> satisfies.
>
> **The experiments caught themselves being wrong.** E4 at 200,000 rows reported
> encryption being *faster* than plaintext. That is impossible. Rather than publish
> it I found the cause — Spark's fixed job overhead swamping the signal — and the
> tool now refuses to let you run it small without warning.

---

## On each contribution

### C1 — "Why per-column encryption rather than encrypting the file?"

> Because predicate pushdown dies with the file. If the whole Parquet file is
> encrypted, the query engine cannot read column statistics or skip row groups, so
> every query becomes a full scan. Per-column encryption keeps PUBLIC columns
> readable — dates, geography, categories — so filters push down while PII and PHI
> stay encrypted with separate keys.
>
> The envelope alternative was measured in the Stage 1 spike and inflates storage
> about 8× at 50k rows, because ciphertext does not compress and column statistics
> are lost.

**"How do you know the files are actually encrypted?"**

> The footer magic, read from the bytes: `PAR1` is plaintext, `PARE` is encrypted.
> Plus a grep of the raw file for values that should not be there, plus a keyless
> read attempted **in a separate OS process** — because Parquet's `KeyToolkit`
> caches unwrapped keys per JVM, and an in-process retry falsely passes. That is not
> hypothetical; it is what the Stage 1 spike did wrong.

### C2 — "What stops the application lying about its measurement?"

**This is the sharpest question in the viva. Answer it directly.**

> Under `sim`, nothing. The measurement is a hash of the source tree computed by the
> process that then presents it. I do not claim otherwise anywhere.
>
> On real hardware the measurement comes from SEV-SNP firmware, is signed by AMD's
> key, and is verified by Microsoft Azure Attestation before the HSM will release
> anything. The application never produces its own measurement.
>
> And even then there is a gap I have written down: the release policy I wrote
> proves the *hardware* — `sevsnpvm`, `azure-compliant-uvm`, `is-debuggable=false` —
> not the *code*. Any Azure confidential VM meeting those claims gets the key.
> Pinning the workload needs the `hostdata` claim and confidential containers.

### C3 — "Why per-record Merkle roots? Isn't that expensive?"

> Yes — measured at 7,330 ms against 8 ms for per-file, about 900× on 145,136
> records. I chose it anyway because it is the only granularity that answers the
> claim. A per-file root proves a *file* was committed to; proving a specific row
> was in it means handing over the file.
>
> The cost was measured before the choice, which is the point. "We measured it and
> chose per-record" is a result; "we chose per-record" is an assertion.

**"What does the chain actually protect against?"**

> Four attacks, each verified: editing a signed payload, editing a denormalised
> column beside it, deleting a mid-chain entry, and editing one row in the encrypted
> Bronze table. The last is the interesting one — the table is the same size, one
> field differs, and the Merkle root still moves. The verifier reports *which* of the
> four it is, because sending an operator to look for a broken link when the problem
> is a forged signature wastes the incident.

### C4 — "Is k-anonymity not a solved problem?"

> The mechanism is. Applying it correctly is where implementations fail, and I have
> a measurement that shows it: `diagnosis_prevalence` has 86 primary suppressions
> and **zero** complementary suppressions. That mart is not actually protected —
> a suppressed cell is recoverable by subtracting the published cells from the
> published total.
>
> The platform surfaces that rather than hiding it, and the dashboard flags it. The
> contribution is not k-anonymity; it is that the suppression is audited per publish
> and the failure mode is visible.

### C5 — "Your planner says everything is inside the enclave. What use is that?"

> The verdict is not the output — the *reason* is. For each stage it reports which
> columns keep it inside and what would have to change:
>
> - `ingest` and `bronze` read raw records, so nothing has been tokenised yet.
> - `silver.tokenise` sees pre-token values by definition; no placement decision can
>   move it.
> - `gold`, `quality` and `lineage` are held by 25 named quasi-identifiers.
>
> And it found a hole while doing it: **the landing zone stored plaintext**. Files
> carried `PAR1` where Bronze carried `PARE`, and one landing patient file held 19
> SSN-shaped strings. A planner that only costed the stages inside the boundary
> would have missed whether the boundary encloses what it should. That is now
> closed — ADR 0022 — and the planner's violation report reads the disk instead of
> repeating its old conclusion.

---

## Questions with uncomfortable answers

Rehearse these. An examiner who finds one you have not thought about will press it.

### "You shipped a report saying the landing zone leaks. Did you just fix it to make the report look better?"

> I fixed it because it was the one finding that contradicted a claim the platform
> otherwise makes. The sequence is in the commit history: the planner reported it,
> ADR 0017 recorded two remedies and deliberately chose neither, and ADR 0022 chose
> the first — encrypt landing under the same attestation-released keys as Bronze,
> rather than delete each batch once Bronze commits.
>
> I rejected the cheaper remedy because deleting the evidence to avoid protecting it
> solves the wrong problem, and it would take byte-verifiable replay with it.
>
> Measured cost: ×1.014 on storage across 369,097 rows. The real cost is that three
> call sites could previously read raw source records with no key, no attestation
> and no audit entry, and now cannot. That convenience *was* the vulnerability.

### "Landing is written by pandas and read by Spark. How does that even work?"

> That was the hard part, and I proved it before building on it. pyarrow writes the
> landing part; Spark's Java `PropertiesDrivenCryptoFactory` reads it. They
> interoperate because both implement the Parquet key-tools `PKMT1` key material
> format, and because I implemented the *same* wrapping envelope on both sides —
> AES-256-GCM, `base64(nonce ‖ ciphertext ‖ tag)`, master key identifier as
> additional authenticated data.
>
> The honest risk is that nothing but one test enforces that agreement. If either
> wrapping routine drifts, every landing part written afterwards becomes unreadable
> by Bronze, and it surfaces at the next promotion rather than at the change. So
> that test runs in CI, in the job that provisions a JVM.

### "What did fixing it break, or reveal?"

> Two things, and the second is worse than the original.
>
> First, an undeclared column would have landed in the clear. Bronze casts to the
> contract before writing so it never sees one; landing is deliberately
> pre-contract and does. An undeclared column now takes the strictest key the
> contract uses. It fired immediately on a `batch` column the collectors add.
>
> Second: the migration found `healthcare/patient_change` — 900 rows of patient
> address changes with **no schema contract at all**. Never promoted, because Bronze
> refuses what it cannot type. Never classified or quality-checked, for the same
> reason. And never encrypted, not because anything decided it should not be, but
> because nothing knew it was there. It is a source-side mutation script that was
> never meant to be a lake dataset, and a collector pointed at a directory tree
> landed it anyway.
>
> Ingestion now refuses to land a dataset the catalog does not declare. But the
> general lesson is uncomfortable and I would rather say it than be asked: every
> check I had built covered the path the data was *supposed* to take.

### "Your violation detector now reports nothing. How do I know it still works?"

> Because it no longer returns a conclusion — it scans the landing zone for the
> `PAR1` footer magic on every run. The old version returned a hard-coded finding,
> which was honest while the hole was open and would have become a lie the moment I
> closed it.
>
> If you want to see it fire, drop any plaintext Parquet file under `lake/landing`
> and re-run `confidentia plan placement`. The unit test does exactly that, and its
> sibling asserts silence on a clean zone, so neither state passes by default.

### "Your F1 for transaction is 0.891 in one place and 0.251 in another. Which is it?"

> Both, and the difference is the interesting part. At Bronze a duplicate and a
> legitimate redelivery are byte-identical, so every redelivered row counts as a
> false positive for `duplicate_record` — 19,855 of that dataset's 20,450.
>
> 0.251 includes it and measures the *layer's* blindness. 0.891 excludes it and
> measures the *rules*. The API returns both and names which to report; the dashboard
> shows the corrected one with the raw one beside it. Hiding either would be
> dishonest in a different direction.

### "You said recall went from 0.390 to 0.997. Did the rules get 2.5× better?"

**Do not accept the framing.**

> No, and I say so in the report. Almost nothing about the rules' method changed.
> What changed is that the answer key now describes defects that actually exist —
> `_break_format` was not breaking formats, `case_inconsistency` was upper-casing
> already-upper-case values, four foreign keys were never declared.
>
> Stage 7b was measuring the measurement apparatus. Stage 7c measures the rules.

### "Only six of eight experiments. What happened?"

> E3 compares incremental against full reprocessing, and the incremental arm does
> not exist — Silver and Gold are both full-refresh, which Stage 5 and Stage 6 each
> recorded at the time. Running it means building incremental processing first.
>
> E5, E6 and E7 were numbered in the original plan and never specified — they appear
> nowhere in the repository. I proposed candidates rather than invent three and imply
> they were always planned. One of them, lineage granularity, has effectively already
> been run and reported under a different name.

### "Is any of this novel, or is it careful assembly of known techniques?"

> The techniques are known — PME, SEV-SNP, k-anonymity, Merkle chains. What I have
> not seen assembled is the confidentiality boundary treated as a *derivable*
> property of the schema rather than a deployment choice, and the consequence
> measured.
>
> The specific claim I would defend as novel: applying that derivation to a full
> medallion pipeline shows the intervention everyone reaches for — tokenising
> identifiers — does not move the boundary, and that quasi-identifiers make
> disclosure control load-bearing rather than cosmetic. I did not expect that result
> and the planner produced it.

### "How much of this would survive contact with real data?"

> The data engineering, mostly — the ingestion guarantees, quality, lineage and
> disclosure control are not synthetic-specific. Two things would not survive
> unchanged.
>
> Schema drift: E8 shows three of four drift kinds destroy 100% of a column
> silently at Bronze. Real sources drift constantly, so that needs a detector before
> real data.
>
> And the contracts. Every column in the Bronze contract is declared `string`, so
> type violations are undetectable there by construction. That is deliberate — Bronze
> keeps raw values and Silver types them — but a reviewer should know the check
> happens one layer later than they might assume.

---

## Questions about method

### "Why so much CI for a student project?"

> Because six stages passed locally and failed on GitHub, and the cause was that my
> local checks were not the ones CI ran. After that I wrote `ci_local.sh` to run CI's
> *exact* commands.
>
> The pattern repeated at every stage where I asserted something without a check.
> `compose-validate` passed for eleven stages against four Dockerfiles that did not
> exist, because `docker compose config` validates syntax and nothing else. The job
> now asserts every referenced file is really there.

### "What was the hardest bug?"

> The Stage 1 false pass, because it passed. Both arms asserted a successful
> round-trip, which an unencrypted write also satisfies, and the keyless-read test
> reused the JVM so Parquet's key cache served it. The spike concluded encryption
> worked and it had not been tested.
>
> It is why every subsequent guarantee is checked at the level the guarantee is
> made: bytes on disk for encryption, a separate process for the keyless read, footer
> magic for the benchmark arms.

### "If you had another month?"

> Three things, in order. **Deploy to Azure once** — roughly a day's work, mostly
> the KMS client, and it would replace the single largest caveat in the
> dissertation with a measurement. **Build incremental Silver and Gold**, which
> unblocks E3 and is the largest missing piece of pipeline. **Decide what
>  is** — right now it is encrypted under the PHI key because
> nothing classifies it, which is the safe failure rather than the right one.
>
> The landing-zone hole was first on this list a week ago and is now closed
> (ADR 0022). I would rather show you that than describe it.

---

## Things to have open during the viva

| | Command / file |
|---|---|
| The placement derivation | `confidentia plan placement` |
| The refusals | `confidentia keys audit` |
| Tamper detection | `bash scripts/verify_lineage.sh` |
| The dashboard | `bash scripts/serve_dashboard.sh` → http://localhost:8001 |
| Experiment results | `confidentia bench results` |
| The landing-zone finding, and the fix | ADR 0017 §"The boundary violation this exposed", then ADR 0022 |
| The landing zone, re-proved live |  |

**Do not open a terminal you have not run in the last hour.** Rehearse the demo
(`docs/report/demo-script.md`) end to end at least twice.

---

## Two habits for the room

**Concede fast and precisely.** Every weakness in this project is already written
down. Reaching a limitation before the examiner does converts it from a hole into
evidence of judgement — and every one of them is in §8 of the report.

**Separate what you measured from what you modelled, every time you say a number.**
It is the discipline the whole project is built on, and an examiner who hears it
maintained under pressure will believe the rest.
