# Getting started

A step-by-step guide for someone who has never seen this project. No prior knowledge
of confidential computing, Spark or data engineering is assumed.

If you only have five minutes, read [What this project is](#what-this-project-is) and
then jump to [The short version](#the-short-version).

---

## What this project is

### The problem, in one paragraph

When you store data in the cloud, it is encrypted on disk and encrypted while it
travels over the network. But to actually *use* it — to add up a column, join two
tables, run a report — the computer has to decrypt it into memory first. At that
moment the data sits in plain view on a machine you do not own. Whoever runs that
machine can, in principle, read it.

For a hospital's patient records or a bank's transactions, that is often the reason
the data never moves to the cloud at all.

### The idea

Modern CPUs can create a **secure enclave**: a region of memory the chip encrypts,
which even the machine's own operating system and administrator cannot read. The chip
can also prove to you, cryptographically, exactly which program is running inside it.
That proof is called **attestation**.

So you can say: *release the decryption key only to a program whose identity I have
verified.* Not a program the administrator claims is running — one the hardware
vouches for.

### What this project asks

That idea is well demonstrated for **one small program**. This project asks the harder
question:

> What happens when you demand the same guarantee of an entire **data platform** — the
> whole chain from raw files arriving, through cleaning and joining, to publishing a
> report? What does it cost, and **which parts of that chain actually need to be inside
> the enclave?**

That last part is the real contribution. Putting everything inside is slow and
expensive. Putting the wrong things outside leaks data. So the project builds a tool
that *derives* the answer from the data's own sensitivity labels, rather than guessing.

**The finding was a surprise.** The obvious protection — replacing names, SSNs and
emails with meaningless tokens — removes 17 of 42 sensitive columns and lets **zero**
stages move out of the enclave. What keeps them in is the other 25: dates of birth,
diagnosis codes, postcodes. You cannot tokenise those without destroying the very
statistics you wanted to compute. So the thing that finally makes data publishable is
not tokenisation but **disclosure control** — blanking out any published figure that
describes too few people to be anonymous.

### What you will actually see when you run it

A working data platform with two pretend customers — a hospital and a bank — that:

1. **Generates** realistic fake data (nobody's real records are involved), including
   deliberately planted errors.
2. **Ingests** it four different ways: files dropped in a folder, a web API, a live
   feed of database changes, and a message stream.
3. **Cleans and transforms** it through three quality tiers, encrypting every sensitive
   column with a different key.
4. **Checks its own work** — 282 rules across six quality dimensions — and refuses to
   publish data that fails.
5. **Signs a tamper-evident record** of everything it did.
6. **Publishes** summary tables with the too-small numbers blanked out.
7. **Shows you all of it** in a web dashboard.

### The one thing to be honest about

**There is no confidential-computing hardware behind this.** The enclave is simulated.

That is stated on every page of the dashboard and next to every figure in the report.
Numbers that were genuinely measured are labelled **MEASURED**; numbers that come from
a model of hardware nobody had are labelled **MODELLED**. The project treats keeping
those apart as its central discipline — if you take one idea away from it, that is the
one.

---

## Before you start

You need a **Windows PC** (or Linux — skip step 1 if so) and about **45 minutes**,
most of it waiting for downloads.

You will need roughly **10 GB of free disk space** and at least **8 GB of RAM**.

**You do not need:** an Azure account, special hardware, or any paid service. It all
runs on your own machine.

---

## The short version

If you know your way around a terminal, this is the whole thing:

```bash
wsl                                    # get into Linux
git clone https://github.com/arpittomar246/confidentia.git ~/confidentia
cd ~/confidentia
bash scripts/bootstrap_wsl.sh          # installs everything, ~15 min
source .venv/bin/activate
export CONFIDENTIA_KEYSTORE_PASSPHRASE="dev-passphrase-not-for-real-use"
bash scripts/rebuild_pipeline.sh       # builds the whole lake, ~10 min
bash scripts/serve_dashboard.sh        # opens the console on :8001
```

Everything below explains those six commands.

---

## Step 1 · Install WSL (Windows only)

This project needs Linux. On Windows you get it through **WSL** (Windows Subsystem for
Linux), which runs a real Ubuntu inside Windows.

Open **PowerShell as Administrator** and run:

```powershell
wsl --install -d Ubuntu
```

Restart your PC when it asks. On first launch Ubuntu will ask you to invent a username
and password — this is a *Linux* account, separate from your Windows login. Remember
the password; you will need it for `sudo`.

> **Why Linux?** Spark, the data-processing engine, needs Unix file semantics and a
> JVM configuration that does not work reliably on Windows. Fighting that was tried and
> abandoned early on — the reasoning is written up in `docs/adr/0001`.

**From here on, every command goes in the Ubuntu terminal, not PowerShell.** Open it by
typing `wsl` in any terminal, or launching "Ubuntu" from the Start menu.

---

## Step 2 · Get the code

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/arpittomar246/confidentia.git ~/confidentia
cd ~/confidentia
```

> **Keep it in the Linux home directory** (`~/confidentia`), not in
> `/mnt/c/Users/...`. Files on the Windows side are 10–20× slower to read from Linux,
> and Spark reads a lot of files.

---

## Step 3 · Install everything else

```bash
bash scripts/bootstrap_wsl.sh
```

This takes **10–15 minutes** and installs:

| What | Why |
|---|---|
| Java 17 | Spark runs on the JVM |
| Python 3.11 + libraries | the platform itself |
| PostgreSQL | stores the platform's own records — runs, scores, audit trail |
| Node.js | builds the dashboard |

It also creates `~/confidentia-data/`, which is where all generated data lives. Nothing
is written outside your home directory.

**It is safe to re-run** if it fails halfway — it skips what is already done.

---

## Step 4 · Turn on the platform

Two things every session needs:

```bash
cd ~/confidentia
source .venv/bin/activate
export CONFIDENTIA_KEYSTORE_PASSPHRASE="dev-passphrase-not-for-real-use"
```

- `source .venv/bin/activate` puts the project's Python on your path. Your prompt
  should now start with `(.venv)`.
- The passphrase unlocks the master encryption key.

> **Why is the passphrase printed in a public document?** Because it protects generated
> fake data on your laptop and nothing else. There is deliberately **no default** — the
> platform refuses to start without one — because a default passphrase would make the
> encryption decorative. On a real deployment this would come from a secret manager.

**You must do this every time you open a new terminal.** Forgetting is the single most
common reason a command fails with a confusing error.

Check it worked:

```bash
confidentia info
```

You should see paths, `column encryption : pme`, and `TEE timings : MODELLED`.

---

## Step 5 · Build everything

```bash
bash scripts/rebuild_pipeline.sh
```

**This takes about 10 minutes.** It does roughly twenty steps; you will see each one
printed as it goes. In order, it:

1. Starts PostgreSQL and creates the platform's tables.
2. Creates the master encryption key.
3. **Approves the code** — see the box below.
4. Generates ~420,000 rows of fake hospital and bank data, with errors planted in it.
5. Ingests it four ways: files, a web API, database change capture, and a message queue.
6. Writes **Bronze** — the raw data, typed and encrypted.
7. Writes **Silver** — cleaned, deduplicated, identifiers replaced with tokens.
8. Writes **Gold** — the summary tables, with small numbers blanked out.
9. Runs 282 quality rules and scores every dataset.
10. Marks its own homework against the list of errors it planted.

> **The "approve the code" step is the interesting one.** The platform will not hand out
> encryption keys to a program it has not been told to trust. It identifies a program by
> hashing its source code — so if you edit any file in the `crypto`, `pipelines`,
> `quality`, `lineage`, `evaluation` or `streaming` folders, the platform stops
> recognising itself and **refuses to release keys until you approve the new version**:
>
> ```bash
> confidentia keys approve
> ```
>
> This is annoying on purpose. It is the whole mechanism: "which code touched this data"
> becomes a question with an enforced answer rather than a promise.

### If it fails partway

Read the last few lines — each step prints `FAILED: <name>`. The two usual causes:

- **PostgreSQL is not running** → `bash scripts/services.sh start`
- **You forgot the passphrase** → go back to step 4.

Then just run the script again. It picks up where it can and skips work already done.

---

## Step 6 · Open the dashboard

```bash
bash scripts/serve_dashboard.sh
```

Leave this terminal open — closing it stops the dashboard. It prints:

```
  dashboard   http://localhost:8001
  paste this into the sign-in box:

  eyJhbGciOiJIUzI1NiIs...
```

Open **http://localhost:8001** in your browser, copy that long token into the box, and
click Sign in.

> **Why a token instead of a password?** The dashboard is read-only and has no user
> database. The token proves which of three roles you have: `viewer`, `operator` or
> `auditor`. It is held in memory only, so **refreshing the page signs you out** — that
> is deliberate, not a bug.

### What to look at, in order

| Page | What it shows you |
|---|---|
| **Overview** | Everything at once. Start here. |
| **Data quality** | The heatmap is the best single picture — six quality dimensions across every dataset. Dark cells are weak spots. |
| **Evaluation** | How good the quality rules are, scored against the planted errors. |
| **Gold marts** | Published tables. **Purple "suppressed" cells are the privacy control working**, not missing data. |
| **Lineage** | The tamper-evident chain. Press **Re-verify**. |
| **Audit** | Every key request, including the refusals. |

---

## Step 7 · Prove it actually works

Charts are easy to fake. These commands check the guarantees against the real bytes on
disk, and each one prints what it found.

```bash
bash scripts/verify_encryption.sh
```
Reads raw files and confirms sensitive values are genuinely not there. It attempts a
read without keys **in a separate process**, because the encryption library caches
unlocked keys and an in-process retry would falsely pass. That mistake was made once
here, and this is the fix.

```bash
bash scripts/verify_landing.sh
```
Same idea for the arrival area. Greps for anything shaped like an SSN or an email.

```bash
bash scripts/verify_lineage.sh
```
**The most interesting one.** It tampers with the data four different ways and requires
the platform to notice each — including editing a single field inside an encrypted
table and putting it back afterwards. It takes about 40 seconds.

```bash
confidentia plan placement
```
The main contribution: which pipeline stages must run inside the enclave, and *why*, per
stage.

---

## Everyday commands

Always after `source .venv/bin/activate` and setting the passphrase.

```bash
confidentia info                 # is everything configured?
confidentia keys approve         # after you edit any measured code
confidentia keys audit           # every key request and refusal
confidentia runs                 # what has run
confidentia quality scores       # quality scores, with the dimension breakdown
confidentia quality failures healthcare encounter   # failing rules, worst first
confidentia evaluate report      # how good the rules are
confidentia gold disclosure      # how much was suppressed and why
confidentia bench results        # experiment results
confidentia landing verify       # is the arrival area encrypted?
```

Add `--help` to anything.

---

## When something goes wrong

| What you see | What it means | Fix |
|---|---|---|
| `command not found: confidentia` | The virtual environment is not active | `source .venv/bin/activate` |
| `CONFIDENTIA_KEYSTORE_PASSPHRASE is not set` | Missing passphrase | Re-run the `export` from step 4 |
| `measurement ... is not approved` | You edited measured code | `confidentia keys approve` |
| `connection refused` on port 5432 | PostgreSQL is stopped | `bash scripts/services.sh start` |
| Dashboard will not load | WSL wound the distro down | Re-run `serve_dashboard.sh` in a terminal you keep open |
| `no space left on device` | Disk full | `du -sh ~/confidentia-data` — the lake is a few GB |
| Spark hangs or is killed | Not enough RAM | Close other apps; the default is a 2 GB Spark driver |

**Starting completely over:**

```bash
rm -rf ~/confidentia-data
bash scripts/rebuild_pipeline.sh
```

The generated data is deterministic — the same seed reproduces byte-for-byte identical
files — so you lose nothing.

---

## The words this project uses

| Term | In plain language |
|---|---|
| **TEE / enclave** | A part of memory the CPU encrypts, which even the machine's owner cannot read |
| **Attestation** | The CPU proving which program is running inside the enclave |
| **Bronze / Silver / Gold** | Three tiers of data: raw, cleaned, summarised |
| **Tokenisation** | Replacing a name or SSN with a meaningless stand-in |
| **k-anonymity** | Never publishing a figure describing fewer than *k* people (here, 5) |
| **Complementary suppression** | Blanking *extra* cells so a hidden one cannot be worked out by subtracting the visible ones from a total |
| **Lineage** | A signed, tamper-evident record of what produced what |
| **Merkle root** | One short fingerprint of a whole table; change any row and it changes |
| **CDC** | Watching a database's change log to capture edits as they happen |
| **PME** | Parquet Modular Encryption — encrypting each column of a file with its own key |
| **MEASURED / MODELLED** | Genuinely measured, versus produced by a model of hardware nobody had |

---

## Where to read more

| File | What it is |
|---|---|
| `README.md` | The short technical summary |
| `docs/report/final-report.md` | The full dissertation |
| `docs/adr/` | Every significant decision, and why — read `0007`, `0017`, `0022` first |
| `docs/report/demo-script.md` | A 12-minute guided tour |
| `PROJECT_CONTEXT.md` | The complete engineering handover |

**If you read one thing beyond this guide**, make it `docs/adr/0022` — the landing zone
was found storing patient records unencrypted, by a tool built for something else
entirely, and the ADR records both the fix and what closing it exposed.
