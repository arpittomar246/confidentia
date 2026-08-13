"""CONFIDENTIA command line interface."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from confidentia.config.settings import get_settings

app = typer.Typer(
    name="confidentia",
    help="Confidential lakehouse platform: generation, pipelines, verification.",
    no_args_is_help=True,
    add_completion=False,
)
generate_app = typer.Typer(help="Synthetic data generation.", no_args_is_help=True)
app.add_typer(generate_app, name="generate")


@app.command()
def info() -> None:
    """Show the resolved configuration, including whether TEE timings are real."""
    settings = get_settings()
    typer.echo(f"environment        : {settings.env.value}")
    typer.echo(f"profile            : {settings.profile.value}")
    typer.echo(f"data root          : {settings.data_root}")
    typer.echo(f"lake root          : {settings.lake_root}")
    typer.echo(f"column encryption  : {settings.crypto.column_encryption}")
    typer.echo(f"TEE backend        : {settings.tee.backend.value}")
    typer.echo(
        f"TEE timings        : {settings.tee.measurement_label}"
        + ("" if settings.tee.is_measured else "  (enclave costs are modelled, not measured)")
    )


VALID_FORMATS = ("parquet", "csv", "jsonl")


def _run(
    tenant: str,
    rows: int,
    seed: int,
    defect_rate: float,
    late_rate: float,
    fmt: str,
    out: Path | None,
    days: int,
    chunk_rows: int,
) -> None:
    from typing import cast

    from confidentia.generators import financial as fin
    from confidentia.generators import healthcare as hc
    from confidentia.generators.base import GeneratorConfig
    from confidentia.generators.writers import Format, write_dataset, write_reference_table

    if fmt not in VALID_FORMATS:
        raise typer.BadParameter(f"format must be one of {VALID_FORMATS}, got {fmt!r}")
    output_format = cast("Format", fmt)

    settings = get_settings()
    root = out or (settings.data_root / "raw")
    root.mkdir(parents=True, exist_ok=True)

    config = GeneratorConfig(
        rows=rows,
        seed=seed,
        chunk_rows=chunk_rows,
        start_date=date(2026, 1, 1),
        days=days,
        defect_rate=defect_rate,
        late_arrival_rate=late_rate,
        # Type drift can only exist where the format does not fix the physical type.
        enable_type_drift=fmt in {"csv", "jsonl"},
    )

    started = time.perf_counter()
    total_rows = 0
    total_defects = 0

    if tenant == "healthcare":
        hc_scale = hc.HealthcareScale.for_rows(rows)
        typer.echo(
            f"healthcare scale: {hc_scale.encounters:,} encounters, {hc_scale.patients:,} patients, "
            f"{hc_scale.providers:,} providers, {hc_scale.facilities:,} facilities"
        )
        write_reference_table(
            hc.generate_facilities(config, hc_scale), root=root, tenant=tenant, dataset="facility"
        )
        write_reference_table(
            hc.generate_plans(config), root=root, tenant=tenant, dataset="insurance_plan"
        )
        write_reference_table(
            hc.generate_providers(config, hc_scale), root=root, tenant=tenant, dataset="provider"
        )
        for produce in (
            hc.generate_patients,
            hc.generate_patient_changes,
            hc.generate_encounters,
            hc.generate_claim_lines,
        ):
            result = write_dataset(
                produce(config, hc_scale), root=root, config=config, fmt=output_format
            )
            total_rows += result.rows
            total_defects += result.defects
    else:
        fin_scale = fin.FinancialScale.for_rows(rows)
        typer.echo(
            f"financial scale: {fin_scale.transactions:,} transactions, "
            f"{fin_scale.accounts:,} accounts, {fin_scale.merchants:,} merchants"
        )
        write_reference_table(
            fin.generate_merchants(config, fin_scale), root=root, tenant=tenant, dataset="merchant"
        )
        for produce_financial in (
            fin.generate_accounts,
            fin.generate_transactions,
            fin.generate_chargebacks,
        ):
            result = write_dataset(
                produce_financial(config, fin_scale), root=root, config=config,
                fmt=output_format
            )
            total_rows += result.rows
            total_defects += result.defects

    elapsed = time.perf_counter() - started
    typer.echo("")
    typer.echo(f"rows written   : {total_rows:,}")
    typer.echo(f"defects seeded : {total_defects:,}")
    typer.echo(f"elapsed        : {elapsed:.1f}s  ({total_rows / max(elapsed, 1e-9):,.0f} rows/s)")
    typer.echo(f"output         : {root / tenant}")


@generate_app.command("healthcare")
def generate_healthcare(
    rows: Annotated[int, typer.Option(help="Number of encounters to generate.")] = 100_000,
    seed: Annotated[int, typer.Option(help="Master seed; identical seeds give identical data.")] = 20260808,
    defect_rate: Annotated[float, typer.Option(help="Fraction of records carrying a defect.")] = 0.06,
    late_rate: Annotated[float, typer.Option(help="Fraction of events arriving late.")] = 0.02,
    fmt: Annotated[str, typer.Option("--format", help="parquet | csv | jsonl")] = "parquet",
    out: Annotated[Path | None, typer.Option(help="Output root. Defaults to <data_root>/raw.")] = None,
    days: Annotated[int, typer.Option(help="Length of the event window.")] = 180,
    chunk_rows: Annotated[int, typer.Option(help="Rows held in memory at once.")] = 100_000,
) -> None:
    """Generate the healthcare tenant: patients, encounters, claims, and mutations."""
    _run("healthcare", rows, seed, defect_rate, late_rate, fmt, out, days, chunk_rows)


@generate_app.command("financial")
def generate_financial(
    rows: Annotated[int, typer.Option(help="Number of transactions to generate.")] = 100_000,
    seed: Annotated[int, typer.Option(help="Master seed; identical seeds give identical data.")] = 20260808,
    defect_rate: Annotated[float, typer.Option(help="Fraction of records carrying a defect.")] = 0.06,
    late_rate: Annotated[float, typer.Option(help="Fraction of events arriving late.")] = 0.02,
    fmt: Annotated[str, typer.Option("--format", help="parquet | csv | jsonl")] = "parquet",
    out: Annotated[Path | None, typer.Option(help="Output root. Defaults to <data_root>/raw.")] = None,
    days: Annotated[int, typer.Option(help="Length of the event window.")] = 180,
    chunk_rows: Annotated[int, typer.Option(help="Rows held in memory at once.")] = 100_000,
) -> None:
    """Generate the financial tenant: accounts, transactions, and chargebacks."""
    _run("financial", rows, seed, defect_rate, late_rate, fmt, out, days, chunk_rows)


@generate_app.command("profile")
def profile_dataset(
    tenant: Annotated[str, typer.Argument(help="healthcare | financial")],
    dataset: Annotated[str, typer.Argument(help="Dataset name, e.g. encounter")],
    out: Annotated[Path | None, typer.Option(help="Raw root. Defaults to <data_root>/raw.")] = None,
) -> None:
    """Summarise a generated dataset and its injected defects.

    Reads the ground-truth answer key, which only evaluation tooling may do. It
    goes through ``writers.iter_defects`` rather than opening the file directly,
    so that the single sanctioned accessor stays greppable and the CI guard can
    assert no pipeline package ever touches it.
    """
    from confidentia.generators.writers import iter_defects, read_manifest

    settings = get_settings()
    root = out or (settings.data_root / "raw")
    manifest = read_manifest(root, tenant, dataset)

    typer.echo(f"dataset       : {tenant}/{dataset}")
    typer.echo(f"rows          : {manifest['total_rows']:,}")
    typer.echo(f"files         : {len(manifest['files'])}")
    typer.echo(f"size          : {manifest['total_bytes'] / 1_048_576:.1f} MB")
    typer.echo(f"batch digest  : {manifest['batch_digest'][:32]}")
    typer.echo(f"defects       : {manifest.get('injected_defects', 0):,}")

    for defects in iter_defects(root, tenant, dataset):
        typer.echo("")
        typer.echo("injected defects by type:")
        for kind, count in defects["defect_type"].value_counts().items():
            share = count / max(int(manifest["total_rows"]), 1)
            typer.echo(f"  {kind:26s} {count:>8,}  ({share:.3%} of rows)")


db_app = typer.Typer(help="Platform metadata store.", no_args_is_help=True)
ingest_app = typer.Typer(help="Ingestion collectors.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(ingest_app, name="ingest")


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending SQL migrations to the platform and OLTP databases."""
    from confidentia.store import migrations

    applied = migrations.migrate()
    for item in applied:
        typer.echo(f"applied {item}")
    if not applied:
        typer.echo("already up to date")


@db_app.command("status")
def db_status() -> None:
    """Show migration state and connectivity."""
    from confidentia.store import db as store_db
    from confidentia.store import migrations

    settings = get_settings()
    typer.echo(f"platform db : {settings.postgres.database}  reachable={store_db.healthcheck()}")
    typer.echo(
        f"oltp db     : {settings.postgres.oltp_database}  "
        f"reachable={store_db.healthcheck(settings.postgres.oltp_database)}"
    )
    typer.echo("")
    for row in migrations.status():
        state = "DRIFTED" if row["drifted"] else ("applied" if row["applied"] else "pending")
        typer.echo(f"  {row['version']:24s} {row['database']:14s} {state}")


@ingest_app.command("file")
def ingest_file(
    tenant: Annotated[str, typer.Argument(help="healthcare | financial")],
    dataset: Annotated[str | None, typer.Argument(help="Dataset, or omit for all.")] = None,
    no_verify: Annotated[bool, typer.Option("--no-verify", help="Skip checksum verification.")] = False,
) -> None:
    """Ingest batch file drops, verifying checksums and skipping duplicates."""
    from confidentia.ingestion.file_collector import FileCollector

    collector = FileCollector(verify_checksums=not no_verify)
    datasets = [dataset] if dataset else collector.available_datasets(tenant)
    if not datasets:
        typer.echo(f"no datasets with a manifest under {tenant}")
        raise typer.Exit(1)

    for name in datasets:
        result = collector.collect(tenant, name)
        typer.echo(f"  {result.summary()}")


@ingest_app.command("rest")
def ingest_rest(
    tenant: Annotated[str, typer.Argument()],
    dataset: Annotated[str, typer.Argument()],
    base_url: Annotated[str, typer.Option(help="Source API base URL.")] = "http://localhost:8081",
    page_size: Annotated[int, typer.Option(help="Records per page.")] = 1000,
    full_refresh: Annotated[bool, typer.Option(help="Ignore the stored watermark.")] = False,
) -> None:
    """Ingest from the paged REST source, resuming from the stored watermark."""
    from confidentia.ingestion.rest_collector import RestCollector

    collector = RestCollector(base_url=base_url, page_size=page_size)
    result = collector.collect(tenant, dataset, full_refresh=full_refresh)
    typer.echo(f"  {result.summary()}")
    for key, value in result.detail.items():
        typer.echo(f"    {key}: {value}")


@ingest_app.command("cdc")
def ingest_cdc(
    max_events: Annotated[int, typer.Option(help="Stop after this many change events.")] = 100_000,
    idle_timeout: Annotated[float, typer.Option(help="Seconds of silence before stopping.")] = 3.0,
) -> None:
    """Consume PostgreSQL logical replication and land the change events."""
    from confidentia.ingestion.cdc_collector import CdcCollector

    collector = CdcCollector(max_events=max_events, idle_timeout_seconds=idle_timeout)
    result = collector.collect()
    typer.echo(f"  {result.summary()}")
    for key, value in result.detail.items():
        typer.echo(f"    {key}: {value}")
    lag = collector.slot_lag_bytes()
    if lag is not None:
        typer.echo(f"    slot lag: {lag:,} bytes")


@ingest_app.command("slot")
def ingest_slot(
    action: Annotated[str, typer.Argument(help="create | drop | status")],
) -> None:
    """Manage the logical replication slot.

    A slot pins write-ahead log on the source until consumed, so creating one and
    walking away will eventually fill the source system's disk. Management is
    explicit for that reason.
    """
    from confidentia.ingestion.cdc_collector import CdcCollector

    collector = CdcCollector()
    if action == "create":
        typer.echo("created" if collector.ensure_slot() else "already exists")
    elif action == "drop":
        collector.drop_slot()
        typer.echo("dropped")
    else:
        lag = collector.slot_lag_bytes()
        typer.echo(f"slot   : {collector.slot_name}")
        typer.echo(f"lag    : {lag:,} bytes" if lag is not None else "lag    : slot absent")


@ingest_app.command("oltp")
def ingest_oltp(
    action: Annotated[str, typer.Argument(help="seed | mutate | count")],
    limit: Annotated[int | None, typer.Option(help="Cap mutations applied.")] = None,
) -> None:
    """Seed and mutate the source-of-record OLTP database (the CDC source)."""
    from confidentia.ingestion import oltp_loader

    if action == "seed":
        typer.echo(f"loaded {oltp_loader.load_patients():,} patients")
    elif action == "mutate":
        typer.echo(f"applied {oltp_loader.replay_mutations(limit=limit):,} mutations")
    else:
        typer.echo(f"{oltp_loader.patient_count():,} patients in the OLTP source")


landing_app = typer.Typer(help="The landing zone (ADR 0022).", no_args_is_help=True)
app.add_typer(landing_app, name="landing")


@landing_app.command("verify")
def landing_verify() -> None:
    """Prove, from the bytes, that no landing file is plaintext.

    **Exits non-zero when one is.** The landing zone was plaintext until ADR 0022,
    and the way that went unnoticed for eleven stages was that nothing looked. This
    command looks — at the four-byte Parquet footer magic of every file under
    ``lake/landing``, not at configuration that claims encryption is on.

    It is wired into ``scripts/verify_encryption.sh`` and CI for the same reason
    Bronze's write verification is: an encryption guarantee that is not re-checked
    is a guarantee about the past.
    """
    from confidentia.crypto.landing import PARQUET_ENCRYPTED_MAGIC, footer_magic, plaintext_parts

    root = get_settings().lake_root / "landing"
    if not root.exists():
        typer.echo(f"no landing zone at {root}; nothing to verify")
        return

    everything = [p for p in root.rglob("*.parquet") if p.is_file()]
    offenders = plaintext_parts(root)

    if not everything:
        typer.echo(f"landing zone at {root} holds no parquet files")
        return

    encrypted = len(everything) - len(offenders)
    typer.echo(f"{encrypted}/{len(everything)} landing files carry the PARE footer magic")

    if offenders:
        typer.echo("")
        typer.echo("PLAINTEXT landing files — protected columns are readable on disk:")
        for path in offenders[:20]:
            typer.echo(f"  {footer_magic(path)!r}  {path.relative_to(root)}")
        if len(offenders) > 20:
            typer.echo(f"  ... and {len(offenders) - 20} more")
        typer.echo("")
        typer.echo("Fix them without re-ingesting:")
        typer.echo("")
        typer.echo("    python -m confidentia.cli landing encrypt --apply")
        raise typer.Exit(code=1)

    typer.echo(f"expected magic {PARQUET_ENCRYPTED_MAGIC!r} on every file; landing is clean")


def _report_orphans(orphans: list) -> None:
    """Name landing datasets no schema contract classifies.

    Worth its own paragraph in the output rather than a line in the table: a
    dataset the catalog has never heard of cannot be promoted, cannot be quality
    checked, and — until this migration ran — was not encrypted either. Every
    column of it is now under the tenant's PHI key, which protects it and is not
    the same as classifying it.
    """
    if not orphans:
        return
    datasets = sorted({f"{r.tenant}/{r.dataset}" for r in orphans})
    typer.echo("")
    typer.echo("Landed without a schema contract, so every column was encrypted:")
    for name in datasets:
        typer.echo(f"  {name}")
    typer.echo("")
    typer.echo("These cannot reach Bronze. Either add a contract or remove them;")
    typer.echo("ingestion now refuses to land a dataset the catalog does not declare.")


@landing_app.command("encrypt")
def landing_encrypt(
    tenant: Annotated[str | None, typer.Option(help="Restrict to one tenant.")] = None,
    dataset: Annotated[str | None, typer.Option(help="Restrict to one dataset.")] = None,
    apply: Annotated[bool, typer.Option("--apply", help="Actually rewrite the files.")] = False,
) -> None:
    """Re-encrypt landing parts written before ADR 0022.

    Defaults to a dry run. It rewrites data in place, and a command that does that
    on the strength of a typo is a command that will eventually be regretted — so
    the destructive form has to be asked for.

    Each part is rewritten under the same attestation-released, per-sensitivity keys
    Bronze uses, verified from its footer magic, and only then moved over the
    original. The batch registry is untouched: this preserves the record of what
    arrived when, which re-ingesting would not.
    """
    from confidentia.crypto.landing import reencrypt_landing

    results = reencrypt_landing(
        get_settings().lake_root, tenant=tenant, dataset=dataset, dry_run=not apply
    )
    if not results:
        typer.echo("no plaintext landing parts found; nothing to do")
        return

    rows = sum(r.rows for r in results)
    orphans = [r for r in results if r.unclassified]

    if not apply:
        typer.echo(f"{len(results)} plaintext part(s), {rows:,} rows, would be re-encrypted:")
        for result in results[:20]:
            mark = "  [no contract]" if result.unclassified else ""
            typer.echo(
                f"  {result.tenant}/{result.dataset}  {result.path.name}  "
                f"{result.rows:,} rows  {result.plaintext_bytes / 1024:,.0f} KiB{mark}"
            )
        if len(results) > 20:
            typer.echo(f"  ... and {len(results) - 20} more")
        typer.echo("")
        typer.echo("Re-run with --apply to rewrite them.")
        _report_orphans(orphans)
        return

    before = sum(r.plaintext_bytes for r in results)
    after = sum(r.encrypted_bytes for r in results)
    typer.echo(f"re-encrypted {len(results)} part(s), {rows:,} rows")
    typer.echo(
        f"{before / 1024 / 1024:.2f} MiB plaintext -> {after / 1024 / 1024:.2f} MiB "
        f"encrypted (x{after / before:.3f})"
    )
    _report_orphans(orphans)
    typer.echo("")
    typer.echo("The old plaintext blocks are unlinked, not shredded. Wiping them is a")
    typer.echo("disk-level operation; on a lake holding real data, use full-disk")
    typer.echo("encryption rather than trusting an unlink.")


keys_app = typer.Typer(help="Key hierarchy and attestation policy.", no_args_is_help=True)
bronze_app = typer.Typer(help="Bronze layer.", no_args_is_help=True)
app.add_typer(keys_app, name="keys")
app.add_typer(bronze_app, name="bronze")


@keys_app.command("init")
def keys_init(
    force: Annotated[bool, typer.Option(help="Overwrite an existing keystore.")] = False,
) -> None:
    """Generate the master key and seal the keystore.

    Overwriting makes every existing encrypted file permanently unreadable, which
    is why --force exists and is not the default.
    """
    from confidentia.crypto.keys import Keystore

    keystore = Keystore()
    keystore.initialise(force=force)
    for key, value in keystore.metadata().items():
        typer.echo(f"  {key:16s} {value}")


@keys_app.command("status")
def keys_status() -> None:
    """Keystore state and the current code measurement."""
    from confidentia.crypto.attestation import measure_code
    from confidentia.crypto.broker import KeyBroker
    from confidentia.crypto.keys import Keystore

    settings = get_settings()
    for key, value in Keystore().metadata().items():
        typer.echo(f"  {key:18s} {value}")

    measurement = measure_code()
    policy = KeyBroker().policy_for(measurement)
    typer.echo(f"  {'measurement':18s} {measurement}")
    typer.echo(f"  {'approved':18s} {policy is not None}")
    if policy:
        typer.echo(f"  {'approved for':18s} {policy['tenants']}")
        typer.echo(f"  {'approved by':18s} {policy['approved_by']}")
    typer.echo(f"  {'tee backend':18s} {settings.tee.backend.value}")
    typer.echo(
        f"  {'hardware rooted':18s} {settings.tee.is_measured}"
        + ("" if settings.tee.is_measured else "   (measurement is self-reported)")
    )


@keys_app.command("check")
def keys_check() -> None:
    """Is the currently running code approved for key release?

    **Exits non-zero when it is not.** This exists for the orchestrator. Changing
    any measured package revokes key release, so the first pipeline run after a
    deploy fails — correctly, and that development tax is the feature. But without
    this check it fails *badly*: sixty seconds into starting a Spark session, deep
    in a traceback, once per task, and the operator has to read a stack trace to
    learn that the fix is one command.

    As a preflight task it fails the DAG in about two seconds and says what to do.
    It does not weaken the attestation check in any way — it asks the same
    question earlier and reports the answer legibly.
    """
    from confidentia.crypto.attestation import measure_code
    from confidentia.crypto.broker import KeyBroker

    measurement = measure_code()
    policy = KeyBroker().policy_for(measurement)
    if policy is None:
        typer.echo(f"measurement {measurement[:16]}... is NOT approved for key release.")
        typer.echo("")
        from confidentia.crypto.attestation import MEASURED_PACKAGES

        # Listed from the constant rather than repeated in prose: this message is
        # the one an operator reads at 03:00, and a hard-coded list that has
        # drifted from the code sends them looking in the wrong package.
        typer.echo(f"A measured package ({', '.join(MEASURED_PACKAGES)}) has changed")
        typer.echo("since the last approval, so every task that touches the lake will be")
        typer.echo("refused its keys. Approve this build, or restore the approved one:")
        typer.echo("")
        typer.echo("    python -m confidentia.cli keys approve")
        raise typer.Exit(code=1)

    typer.echo(f"measurement {measurement[:16]}... approved for {policy['tenants']}")


@keys_app.command("approve")
def keys_approve(
    approved_by: Annotated[str, typer.Option(help="Who is approving this build.")] = "developer",
    tenants: Annotated[str, typer.Option(help="Comma-separated tenants, or '*'.")] = "*",
) -> None:
    """Approve the currently running code for key release.

    Needed after any change to a measured package (see
    `attestation.MEASURED_PACKAGES`). That inconvenience is the feature: it is what
    makes "which code touched this data" enforceable rather than merely recorded.
    """
    from confidentia.crypto.broker import KeyBroker

    broker = KeyBroker()
    measurement = broker.approve_current_code(approved_by=approved_by)
    if tenants != "*":
        broker.approve(
            measurement,
            description="current working tree",
            tenants=[t.strip() for t in tenants.split(",")],
            approved_by=approved_by,
        )
    typer.echo(f"approved {measurement}")
    typer.echo(f"  for tenants: {tenants}")


@keys_app.command("audit")
def keys_audit(
    limit: Annotated[int, typer.Option()] = 20,
    denied_only: Annotated[bool, typer.Option(help="Show refusals only.")] = False,
) -> None:
    """Key release decisions, including the refusals."""
    from confidentia.store import db as store_db

    where = "WHERE granted = FALSE" if denied_only else ""
    rows = store_db.fetch_all(
        f"""
        SELECT requested_at, tenant, granted, reason, measurement, hardware_rooted
          FROM platform.key_release_audit {where}
         ORDER BY requested_at DESC LIMIT %s
        """,
        (limit,),
    )
    if not rows:
        typer.echo("no key release attempts recorded")
        return
    for row in rows:
        mark = "GRANT " if row["granted"] else "DENY  "
        typer.echo(
            f"{row['requested_at']:%Y-%m-%d %H:%M:%S}  {mark} "
            f"{row['tenant']:12s} {row['measurement'][:12]}  {row['reason'][:70]}"
        )


@bronze_app.command("write")
def bronze_write(
    tenant: Annotated[str, typer.Argument(help="healthcare | financial")],
    dataset: Annotated[str | None, typer.Argument(help="Dataset, or omit for all.")] = None,
    full_refresh: Annotated[bool, typer.Option(help="Rewrite rather than append.")] = False,
) -> None:
    """Promote landed batches into the encrypted Bronze Delta table."""
    from confidentia.catalog.contracts import all_contracts
    from confidentia.pipelines.bronze.writer import write_bronze

    datasets = (
        [dataset]
        if dataset
        else [c.dataset for c in all_contracts() if c.tenant.value == tenant]
    )
    for name in datasets:
        try:
            result = write_bronze(tenant, name, full_refresh=full_refresh)
            typer.echo(f"  {result.summary()}")
        except FileNotFoundError as exc:
            typer.echo(f"  {tenant}/{name}: skipped ({exc})")


@bronze_app.command("verify")
def bronze_verify(
    tenant: Annotated[str, typer.Argument()],
    dataset: Annotated[str, typer.Argument()],
) -> None:
    """Check the bytes on disk are actually encrypted."""
    from confidentia.pipelines.bronze.writer import bronze_path, verify_encrypted

    path = bronze_path(tenant, dataset)
    if not path.exists():
        typer.echo(f"no Bronze table at {path}")
        raise typer.Exit(1)
    ok, detail = verify_encrypted(path)
    typer.echo(f"  {'ENCRYPTED' if ok else 'NOT ENCRYPTED'}  {detail}")
    raise typer.Exit(0 if ok else 1)


silver_app = typer.Typer(help="Silver layer.", no_args_is_help=True)
app.add_typer(silver_app, name="silver")


@silver_app.command("write")
def silver_write(
    tenant: Annotated[str, typer.Argument(help="healthcare | financial")],
    dataset: Annotated[str | None, typer.Argument(help="Dataset, or omit for all.")] = None,
    no_gate: Annotated[bool, typer.Option("--no-gate", help="Bypass the quarantine-rate circuit breaker.")] = False,
) -> None:
    """Conform, validate, deduplicate and tokenise Bronze into Silver."""
    from confidentia.catalog.contracts import all_contracts
    from confidentia.pipelines.silver.writer import SilverQualityGateError, write_silver

    datasets = (
        [dataset]
        if dataset
        # Reference tables stop at Bronze: they exist so the referential rules
        # have a parent to check, and Gold builds its own dimensions from the
        # star schema rather than from them.
        else [
            c.dataset
            for c in all_contracts()
            if c.tenant.value == tenant and c.dataset != "patient_cdc" and not c.is_reference
        ]
    )
    for name in datasets:
        try:
            result = write_silver(tenant, name, enforce_gate=not no_gate)
            typer.echo(f"  {result.summary()}")
        except FileNotFoundError as exc:
            typer.echo(f"  {tenant}/{name}: skipped ({exc})")
        except SilverQualityGateError as exc:
            typer.echo(f"  {tenant}/{name}: GATE FAILED -- {exc}")


@silver_app.command("dimension")
def silver_dimension(
    tenant: Annotated[str, typer.Argument()] = "healthcare",
) -> None:
    """Build the SCD2 patient dimension from the CDC change stream."""
    from confidentia.pipelines.silver.writer import build_patient_dimension

    stats = build_patient_dimension(tenant=tenant)
    for key, value in stats.items():
        typer.echo(f"  {key:24s} {value}")


@silver_app.command("quarantine")
def silver_quarantine(
    limit: Annotated[int, typer.Option()] = 15,
) -> None:
    """Records held in quarantine, grouped by reason."""
    from confidentia.store import db as store_db

    rows = store_db.fetch_all(
        """
        SELECT tenant, dataset, reason_code, count(*) AS n,
               max(quarantined_at) AS latest
          FROM platform.quarantine_record
         WHERE replayed_at IS NULL
         GROUP BY 1,2,3 ORDER BY n DESC LIMIT %s
        """,
        (limit,),
    )
    if not rows:
        typer.echo("quarantine is empty")
        return
    typer.echo(f"{'tenant':12s} {'dataset':14s} {'reason':28s} {'count':>8s}")
    for row in rows:
        typer.echo(
            f"{row['tenant']:12s} {row['dataset']:14s} {row['reason_code'][:28]:28s} "
            f"{row['n']:>8,}"
        )


gold_app = typer.Typer(help="Gold layer.", no_args_is_help=True)
app.add_typer(gold_app, name="gold")


@gold_app.command("build")
def gold_build(
    tenant: Annotated[str, typer.Argument(help="healthcare | financial")],
) -> None:
    """Build the star schema and publish disclosure-controlled marts."""
    from confidentia.pipelines.gold.writer import build_gold

    result = build_gold(tenant)
    typer.echo(f"  {result.summary()}")
    typer.echo("")
    typer.echo("  star tables:")
    for table, rows in sorted(result.tables_built.items()):
        typer.echo(f"    {table:22s} {rows:>10,} rows")
    typer.echo("")
    typer.echo("  published marts:")
    for mart, rows in sorted(result.marts_published.items()):
        typer.echo(f"    {mart:26s} {rows:>8,} rows")
    typer.echo("")
    typer.echo("  disclosure:")
    for line in result.suppression.values():
        typer.echo(f"    {line}")


@gold_app.command("dirty")
def gold_dirty(
    tenant: Annotated[str | None, typer.Argument(help="Tenant, or omit for all.")] = None,
) -> None:
    """Which mart partitions a late arrival has invalidated.

    **Exits 0 when there is work to do and 1 when there is not**, which is the
    inverse of the usual convention and is deliberate: the orchestrator polls this
    as a sensor, and a sensor succeeds when its condition is met. Reporting "all
    clean" as success would fire the restatement DAG continuously.
    """
    from confidentia.store import db as store_db

    rows = store_db.fetch_all(
        """
        SELECT tenant, mart, count(*) AS partitions,
               min(event_date) AS oldest, max(event_date) AS newest,
               min(dirty_reason) AS reason
          FROM platform.mart_partition_state
         WHERE is_dirty AND (%s IS NULL OR tenant = %s)
         GROUP BY tenant, mart
         ORDER BY partitions DESC
        """,
        (tenant, tenant),
    )
    if not rows:
        typer.echo("no dirty partitions")
        raise typer.Exit(code=1)

    total = sum(r["partitions"] for r in rows)
    typer.echo(f"{total:,} dirty partitions across {len(rows)} mart(s)")
    for row in rows:
        typer.echo(
            f"  {row['tenant'][:4]}/{row['mart']:26s} {row['partitions']:>5,} "
            f"{row['oldest']} -> {row['newest']}  {row['reason'] or ''}"
        )


@gold_app.command("disclosure")
def gold_disclosure(limit: Annotated[int, typer.Option()] = 15) -> None:
    """What each publish released and what it withheld."""
    from confidentia.store import db as store_db

    rows = store_db.fetch_all(
        """
        SELECT tenant, mart, min_cell_count, total_cells, primary_suppressed,
               complementary_suppressed, rows_published, published_at
          FROM platform.disclosure_log ORDER BY published_at DESC LIMIT %s
        """,
        (limit,),
    )
    if not rows:
        typer.echo("nothing published yet")
        return
    typer.echo(f"{'mart':28s} {'k':>3s} {'cells':>8s} {'primary':>8s} {'compl.':>8s} {'published':>10s}")
    for row in rows:
        typer.echo(
            f"{row['mart']:28s} {row['min_cell_count']:>3d} {row['total_cells']:>8,} "
            f"{row['primary_suppressed']:>8,} {row['complementary_suppressed']:>8,} "
            f"{row['rows_published']:>10,}"
        )


@gold_app.command("restatements")
def gold_restatements(limit: Annotated[int, typer.Option()] = 10) -> None:
    """Aggregates invalidated by late-arriving facts."""
    from confidentia.store import db as store_db

    rows = store_db.fetch_all(
        """
        SELECT tenant, mart, partitions_restated, trigger_reason,
               oldest_partition, newest_partition, lag_days, restated_at
          FROM platform.restatement_log ORDER BY restated_at DESC LIMIT %s
        """,
        (limit,),
    )
    if not rows:
        typer.echo("no restatements recorded")
        return
    for row in rows:
        typer.echo(
            f"  {row['mart']:26s} {row['partitions_restated']:>4} partitions  "
            f"{row['oldest_partition']} -> {row['newest_partition']} "
            f"({row['lag_days']} day span)  {row['trigger_reason']}"
        )


quality_app = typer.Typer(help="Data quality.", no_args_is_help=True)
app.add_typer(quality_app, name="quality")


@quality_app.command("run")
def quality_run(
    tenant: Annotated[str, typer.Argument(help="healthcare | financial")],
    dataset: Annotated[str | None, typer.Argument(help="Dataset, or omit for all.")] = None,
    layer: Annotated[str, typer.Option(help="Layer to evaluate.")] = "silver",
) -> None:
    """Evaluate quality rules and score the dataset."""
    from confidentia.catalog.contracts import all_contracts
    from confidentia.quality.runner import run_quality

    datasets = (
        [dataset]
        if dataset
        else [
            c.dataset
            for c in all_contracts()
            if c.tenant.value == tenant and c.dataset != "patient_cdc"
        ]
    )
    for name in datasets:
        try:
            result = run_quality(tenant, name, layer=layer)
            typer.echo(f"  {result.summary()}")
        except FileNotFoundError as exc:
            typer.echo(f"  {tenant}/{name}: skipped ({exc})")


@quality_app.command("gate")
def quality_gate(
    tenant: Annotated[str, typer.Argument(help="healthcare | financial")],
    dataset: Annotated[str | None, typer.Argument(help="Dataset, or omit for all.")] = None,
    layer: Annotated[str, typer.Option(help="Layer whose score decides.")] = "bronze",
    override: Annotated[
        bool, typer.Option("--override", help="Promote despite a failing score. Audited.")
    ] = False,
    reason: Annotated[
        str | None, typer.Option(help="Required with --override. Recorded in the audit trail.")
    ] = None,
) -> None:
    """Refuse to promote a dataset whose quality is below the threshold.

    **Exits non-zero when it blocks.** That is the whole mechanism: the
    orchestrator reads the exit status, so a blocked gate fails the task and stops
    the DAG rather than skipping it, which in Airflow is green and lets downstream
    tasks run.
    """
    from confidentia.catalog.contracts import all_contracts
    from confidentia.quality.gate import PromotionBlockedError, evaluate

    if override and not reason:
        raise typer.BadParameter("--override requires --reason; a silent bypass is not allowed")

    datasets = (
        [dataset]
        if dataset
        else [
            c.dataset
            for c in all_contracts()
            if c.tenant.value == tenant and c.dataset != "patient_cdc" and not c.is_reference
        ]
    )

    blocked: list[str] = []
    for name in datasets:
        try:
            decision = evaluate(
                tenant, name, layer=layer, override=override, override_reason=reason
            )
            typer.echo(f"  {decision.summary()}")
        except PromotionBlockedError as exc:
            typer.echo(f"  {exc}")
            blocked.append(name)

    if blocked:
        # Every dataset is evaluated before exiting, so one run of the gate reports
        # everything that is wrong. Stopping at the first failure would mean
        # fixing one dataset only to be blocked by the next.
        typer.echo("")
        typer.echo(f"promotion BLOCKED for {len(blocked)}: {', '.join(blocked)}")
        raise typer.Exit(code=1)


@quality_app.command("rules")
def quality_rules(
    tenant: Annotated[str | None, typer.Option(help="Filter by tenant.")] = None,
) -> None:
    """The rule catalogue, grouped by dimension."""
    from confidentia.quality.rules import rule_catalogue

    rules = [r for r in rule_catalogue() if tenant is None or r.tenant == tenant]
    by_dimension: dict[str, int] = {}
    by_family: dict[str, int] = {}
    for rule in rules:
        by_dimension[rule.dimension.value] = by_dimension.get(rule.dimension.value, 0) + 1
        by_family[rule.family] = by_family.get(rule.family, 0) + 1

    typer.echo(f"{len(rules)} row-level rules (uniqueness and integrity added at run time)")
    typer.echo("")
    typer.echo("by dimension:")
    for name, count in sorted(by_dimension.items()):
        typer.echo(f"  {name:16s} {count:>4}")
    typer.echo("")
    typer.echo("by defect family:")
    for name, count in sorted(by_family.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {name:24s} {count:>4}")


@quality_app.command("scores")
def quality_scores(limit: Annotated[int, typer.Option()] = 15) -> None:
    """Most recent dataset scores, with their dimension breakdown."""
    from confidentia.store import db as store_db

    rows = store_db.fetch_all(
        """
        SELECT DISTINCT ON (tenant, dataset)
               tenant, dataset, overall_score, completeness, validity, consistency,
               uniqueness, integrity, timeliness, rows_total, rows_failing,
               rules_failed, rules_evaluated, promotion_blocked
          FROM platform.dq_score
         ORDER BY tenant, dataset, scored_at DESC
         LIMIT %s
        """,
        (limit,),
    )
    if not rows:
        typer.echo("no quality runs recorded")
        return

    def fmt(value: float | None) -> str:
        return f"{value:.3f}" if value is not None else "  -- "

    typer.echo(
        f"{'dataset':22s} {'score':>6s} {'compl':>6s} {'valid':>6s} {'consis':>6s} "
        f"{'uniq':>6s} {'integ':>6s} {'timel':>6s}  {'rules':>7s}"
    )
    for row in rows:
        flag = "  BLOCKED" if row["promotion_blocked"] else ""
        typer.echo(
            f"{row['tenant'][:4]}/{row['dataset'][:16]:17s} {row['overall_score']:.3f} "
            f"{fmt(row['completeness'])} {fmt(row['validity'])} {fmt(row['consistency'])} "
            f"{fmt(row['uniqueness'])} {fmt(row['integrity'])} {fmt(row['timeliness'])} "
            f"{row['rules_failed']:>3}/{row['rules_evaluated']:<3}{flag}"
        )


@quality_app.command("failures")
def quality_failures(
    tenant: Annotated[str, typer.Argument()],
    dataset: Annotated[str, typer.Argument()],
    limit: Annotated[int, typer.Option()] = 15,
) -> None:
    """Which rules are failing, worst first."""
    from confidentia.store import db as store_db

    rows = store_db.fetch_all(
        """
        SELECT r.family, r.field, r.severity, d.rows_failed, d.rows_evaluated, d.pass_rate
          FROM platform.dq_result d
          JOIN platform.dq_rule r USING (rule_id)
         WHERE d.tenant = %s AND d.dataset = %s
           AND d.run_id = (SELECT run_id FROM platform.dq_score
                            WHERE tenant = %s AND dataset = %s
                            ORDER BY scored_at DESC LIMIT 1)
           AND d.rows_failed > 0
         ORDER BY d.rows_failed DESC LIMIT %s
        """,
        (tenant, dataset, tenant, dataset, limit),
    )
    if not rows:
        typer.echo("no failing rules in the most recent run")
        return
    typer.echo(f"{'family':24s} {'field':24s} {'sev':9s} {'failed':>9s} {'pass rate':>10s}")
    for row in rows:
        typer.echo(
            f"{row['family']:24s} {(row['field'] or '-')[:24]:24s} {row['severity']:9s} "
            f"{row['rows_failed']:>9,} {row['pass_rate']:>10.4f}"
        )


stream_app = typer.Typer(help="Kafka streaming (Stage 9).", no_args_is_help=True)
app.add_typer(stream_app, name="stream")


@stream_app.command("produce")
def stream_produce(
    tenant: Annotated[str, typer.Argument(help="Tenant to replay.")] = "financial",
    dataset: Annotated[str, typer.Argument(help="Dataset to replay.")] = "transaction",
    limit: Annotated[int | None, typer.Option(help="Stop after this many records.")] = None,
    rate: Annotated[
        int, typer.Option(help="Records per second. 0 replays as fast as possible.")
    ] = 2000,
    topic: Annotated[str | None, typer.Option(help="Override the configured topic.")] = None,
) -> None:
    """Replay landing-zone records into Kafka, standing in for the payment switch.

    Reads the **same files the file collector reads**, so the streaming and batch
    paths see identical records. A producer that generated its own data would make
    every difference between the two paths ambiguous.
    """
    from confidentia.streaming.producer import produce

    result = produce(tenant, dataset, topic=topic, limit=limit, rate=rate)
    typer.echo(f"  {result.summary()}")


@stream_app.command("consume")
def stream_consume(
    tenant: Annotated[str, typer.Argument()] = "financial",
    dataset: Annotated[str, typer.Argument()] = "transaction",
    follow: Annotated[
        bool, typer.Option("--follow", help="Keep running instead of draining once.")
    ] = False,
    trigger_seconds: Annotated[int, typer.Option(help="Trigger interval when following.")] = 10,
    max_offsets: Annotated[int, typer.Option(help="Cap per micro-batch.")] = 20_000,
    timeout: Annotated[int | None, typer.Option(help="Stop after this many seconds.")] = None,
    topic: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Stream a topic into the **shared** Bronze table as a fourth source.

    Drains what is in the topic and stops, unless `--follow`. Bounded is the right
    default: it terminates, it is testable, and it is what an orchestrated task
    needs.
    """
    from confidentia.streaming.consumer import consume

    result = consume(
        tenant, dataset, topic=topic, once=not follow,
        trigger_seconds=trigger_seconds, max_offsets_per_trigger=max_offsets,
        timeout_seconds=timeout,
    )
    typer.echo(f"  {result.summary()}")


@stream_app.command("status")
def stream_status(
    topic: Annotated[str | None, typer.Option(help="Topic to inspect.")] = None,
) -> None:
    """Partition count, message count, and what Bronze has consumed so far."""
    from confidentia.store import db as store_db
    from confidentia.streaming.consumer import checkpoint_path
    from confidentia.streaming.producer import topic_status

    info = topic_status(topic)
    if not info["exists"]:
        typer.echo(f"topic {info['topic']}: does not exist")
        raise typer.Exit(code=1)

    typer.echo(f"topic {info['topic']}")
    typer.echo(f"  partitions   {info['partitions']}")
    typer.echo(f"  messages     {info['messages']:,}")

    checkpoint = checkpoint_path("financial", "transaction")
    typer.echo(f"  checkpoint   {'present' if checkpoint.exists() else 'absent'} ({checkpoint})")

    rows = store_db.fetch_all(
        """
        SELECT status, records_read, started_at, finished_at
          FROM platform.run
         WHERE run_type = 'stream'
         ORDER BY started_at DESC LIMIT 5
        """
    )
    if rows:
        typer.echo("")
        typer.echo("recent stream runs:")
        for row in rows:
            typer.echo(
                f"  {row['started_at']:%Y-%m-%d %H:%M:%S}  {row['status']:10s} "
                f"{row['records_read']:>9,} records"
            )


evaluate_app = typer.Typer(
    help="Score the platform against the ground-truth answer key.", no_args_is_help=True
)
app.add_typer(evaluate_app, name="evaluate")


def _echo_evaluation(result: object) -> None:
    from confidentia.evaluation.scoring import EvaluationResult

    assert isinstance(result, EvaluationResult)
    typer.echo(f"  {result.summary()}")
    # Worst recall first: the families the platform misses are the finding, and a
    # table sorted by name buries them among the ones that score 1.0.
    for score in sorted(
        result.families, key=lambda s: (s.recall if s.recall is not None else 2.0)
    ):
        gap = "" if score.rule_exists is not False else "   (no rule registered)"
        typer.echo(f"    {score.summary()}{gap}")
    for score in result.unscoreable:
        typer.echo(
            f"    {score.family:44s} {score.detected_claims:>7,} detected, "
            "unscoreable (never injected)"
        )


@evaluate_app.command("quality")
def evaluate_quality(
    tenant: Annotated[str, typer.Argument(help="healthcare | financial")],
    dataset: Annotated[str | None, typer.Argument(help="Dataset, or omit for all.")] = None,
    layer: Annotated[
        str, typer.Option(help="Layer whose detections to score. Bronze is the only "
                               "one that measures the source rather than our cleaning.")
    ] = "bronze",
    fields: Annotated[
        bool, typer.Option("--fields", help="Break each family down by column.")
    ] = False,
) -> None:
    """Precision, recall and F1 per defect family against the Stage 2 manifest."""
    from confidentia.evaluation.detections import DetectionsMissingError, WrongLayerError
    from confidentia.evaluation.ground_truth import GroundTruthMissingError
    from confidentia.evaluation.runner import evaluatable_datasets, evaluate_dataset

    datasets = [dataset] if dataset else evaluatable_datasets(tenant)
    if not datasets:
        typer.echo(f"no dataset with a ground-truth manifest for {tenant}")
        raise typer.Exit(code=1)

    for name in datasets:
        try:
            result = evaluate_dataset(tenant, name, layer=layer)
        except (GroundTruthMissingError, DetectionsMissingError) as exc:
            typer.echo(f"  {tenant}/{name}: skipped ({exc})")
            continue
        except WrongLayerError as exc:
            typer.echo(f"  {tenant}/{name}: REFUSED  {exc}")
            continue

        _echo_evaluation(result)
        if fields:
            typer.echo("")
            for score in sorted(
                result.fields, key=lambda s: (s.recall if s.recall is not None else 2.0)
            ):
                gap = "" if score.rule_exists is not False else "   (no rule registered)"
                typer.echo(f"      {score.summary()}{gap}")
        typer.echo("")


@evaluate_app.command("report")
def evaluate_report(limit: Annotated[int, typer.Option()] = 40) -> None:
    """The stored per-family scores from the most recent evaluation of each dataset."""
    from confidentia.store import db as store_db

    rows = store_db.fetch_all(
        """
        SELECT tenant, dataset, family, true_positives, false_positives,
               false_negatives, truth_claims, precision, recall, f1, rule_exists
          FROM platform.dq_evaluation_latest
         LIMIT %s
        """,
        (limit,),
    )
    if not rows:
        typer.echo("no evaluation runs recorded")
        return

    def fmt(value: float | None) -> str:
        return f"{value:.3f}" if value is not None else "  -- "

    typer.echo(
        f"{'dataset':22s} {'family':24s} {'P':>6s} {'R':>6s} {'F1':>6s} "
        f"{'tp':>7s} {'fp':>7s} {'fn':>7s}"
    )
    for row in rows:
        gap = "  no rule" if row["rule_exists"] is False else ""
        typer.echo(
            f"{row['tenant'][:4]}/{row['dataset'][:16]:17s} {row['family']:24s} "
            f"{fmt(row['precision'])} {fmt(row['recall'])} {fmt(row['f1'])} "
            f"{row['true_positives']:>7,} {row['false_positives']:>7,} "
            f"{row['false_negatives']:>7,}{gap}"
        )


lineage_app = typer.Typer(
    help="Signed, hash-chained provenance (C3).", no_args_is_help=True
)
app.add_typer(lineage_app, name="lineage")


@lineage_app.command("record")
def lineage_record(
    tenant: Annotated[str, typer.Argument(help="healthcare | financial")],
    dataset: Annotated[str | None, typer.Argument(help="Dataset, or omit for all.")] = None,
    layer: Annotated[str, typer.Option(help="bronze | silver | gold")] = "bronze",
) -> None:
    """Digest a table, bind it to the code and keys in force, and sign the result.

    Appends one chain entry per dataset. The entry commits to a Merkle root over
    every record; the leaves are not stored, so proving a row later re-derives
    them from the table and only succeeds if the data still matches.
    """
    from confidentia.lineage.recorder import (
        LineageTableMissingError,
        record_dataset,
        recordable_datasets,
    )

    names = [dataset] if dataset else recordable_datasets(tenant, layer)
    if not names:
        typer.echo(f"no {layer} tables for {tenant}")
        raise typer.Exit(code=1)

    for name in names:
        try:
            result = record_dataset(tenant, name, layer=layer)
        except LineageTableMissingError as exc:
            typer.echo(f"  {tenant}/{name}: skipped ({exc})")
            continue
        typer.echo(f"  {result.summary()}")
        if not result.hardware_rooted:
            # Printed every time, deliberately. The one thing this stage must not
            # do is let a signed chain read as a stronger claim than it is.
            typer.echo("      hardware_rooted=false: self-measured, self-signed under `sim`")


@lineage_app.command("verify")
def lineage_verify(
    deep: Annotated[
        bool,
        typer.Option("--deep", help="Also re-digest the tables and compare to the signed roots."),
    ] = False,
    tenant: Annotated[str | None, typer.Option(help="Limit the deep check to one tenant.")] = None,
) -> None:
    """Recompute the chain, the columns and — with --deep — the data itself.

    Exits non-zero when anything fails to verify, so it can be a gate rather than
    a report.
    """
    from confidentia.lineage.verify import verify as verify_lineage

    report = verify_lineage(deep=deep, tenants=[tenant] if tenant else None)
    typer.echo(f"  {report.summary()}")
    for failure in report.failures:
        typer.echo(f"    {failure.summary()}")
    for check in report.deep:
        typer.echo(f"    {check.summary()}")
    if not report.ok:
        raise typer.Exit(code=1)


@lineage_app.command("show")
def lineage_show(
    tenant: Annotated[str | None, typer.Argument()] = None,
    dataset: Annotated[str | None, typer.Argument()] = None,
    layer: Annotated[str | None, typer.Option(help="Filter to one layer.")] = None,
    limit: Annotated[int, typer.Option()] = 20,
) -> None:
    """The chain, newest first, or one dataset's history."""
    from confidentia.store import db as store_db

    clauses: list[str] = []
    params: list[object] = []
    for column, value in (("tenant", tenant), ("dataset", dataset), ("layer", layer)):
        if value:
            clauses.append(f"{column} = %s")
            params.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    rows = store_db.fetch_all(
        f"""
        SELECT seq, tenant, dataset, layer, record_count, merkle_root,
               entry_digest, previous_digest, code_measurement, hardware_rooted,
               recorded_at, payload
          FROM platform.lineage_entry
          {where}
         ORDER BY seq DESC
         LIMIT %s
        """,
        tuple(params),
    )
    if not rows:
        typer.echo("no lineage entries recorded")
        return

    typer.echo(
        f"{'seq':>5s} {'recorded':16s} {'dataset':26s} {'records':>9s} "
        f"{'root':14s} {'entry':14s} {'code':10s} rooted"
    )
    for row in rows:
        name = f"{row['tenant'][:4]}/{row['dataset'][:14]}@{row['layer'][:6]}"
        inputs = len(row["payload"].get("inputs") or [])
        typer.echo(
            f"{row['seq']:>5d} {row['recorded_at']:%Y-%m-%d %H:%M} {name:26s} "
            f"{row['record_count']:>9,} {row['merkle_root'][:12]}.. "
            f"{row['entry_digest'][:12]}.. {row['code_measurement'][:8]}.. "
            f"{str(row['hardware_rooted']).lower():5s} <-{inputs}"
        )


@lineage_app.command("prove")
def lineage_prove(
    tenant: Annotated[str, typer.Argument(help="healthcare | financial")],
    dataset: Annotated[str, typer.Argument()],
    value: Annotated[str, typer.Argument(help="The key value identifying the row.")],
    layer: Annotated[str, typer.Option(help="bronze | silver | gold")] = "bronze",
    column: Annotated[
        str | None, typer.Option(help="Column to match on. Defaults to the business key.")
    ] = None,
) -> None:
    """Prove one row belongs to the record set the platform signed.

    The brief's claim made concrete: point at a row, get back the sibling digests
    that rebuild a root this platform signed, plus the code measurement and key
    IDs that were in force. An altered row cannot be proven — its digest is no
    longer in the tree.
    """
    from confidentia.lineage.verify import NoCommitmentError, prove_record

    try:
        result = prove_record(tenant, dataset, layer=layer, column=column, value=value)
    except NoCommitmentError as exc:
        typer.echo(f"  {exc}")
        raise typer.Exit(code=1) from exc

    typer.echo(f"  {result.summary()}")
    typer.echo(f"    entry          #{result.seq}  {result.entry_digest}")
    typer.echo(f"    signed root    {result.signed_root}")
    typer.echo(f"    recomputed     {result.recomputed_root}")
    if not result.root_matches:
        typer.echo("    the table no longer hashes to the signed root: it has been changed")
    for match in result.matches:
        mark = "ok " if match["verified"] else "BAD"
        typer.echo(
            f"    [{mark}] leaf {match['leaf'][:16]}.. at index {match.get('index')} "
            f"via {match.get('depth', 0)} siblings"
        )
    if not result.verified:
        raise typer.Exit(code=1)


@app.command("runs")
def list_runs(
    limit: Annotated[int, typer.Option(help="How many recent runs to show.")] = 15,
) -> None:
    """Recent pipeline runs from the platform metadata store."""
    from confidentia.store import db as store_db

    rows = store_db.fetch_all(
        """
        SELECT run_type, source, tenant, dataset, status, duration_ms,
               records_read, records_written, records_rejected, started_at
          FROM platform.run_summary
         ORDER BY started_at DESC
         LIMIT %s
        """,
        (limit,),
    )
    if not rows:
        typer.echo("no runs recorded")
        return

    typer.echo(
        f"{'started':20s} {'type':10s} {'source':10s} {'dataset':18s} "
        f"{'status':10s} {'ms':>8s} {'read':>10s} {'written':>10s}"
    )
    for row in rows:
        typer.echo(
            f"{row['started_at']:%Y-%m-%d %H:%M:%S}  {row['run_type']:10s} "
            f"{(row['source'] or '-'):10s} {row['dataset'][:18]:18s} {row['status']:10s} "
            f"{int(row['duration_ms'] or 0):>8,} {row['records_read']:>10,} "
            f"{row['records_written']:>10,}"
        )


bench_app = typer.Typer(help="Experiments E1-E8.", no_args_is_help=True)
app.add_typer(bench_app, name="bench")


@bench_app.command("e4")
def bench_e4(
    rows: Annotated[int, typer.Option(help="Rows per arm.")] = 3_000_000,
    repeats: Annotated[int, typer.Option(help="Timed repeats after warm-up.")] = 5,
) -> None:
    """E4 - what Parquet Modular Encryption costs.

    Do not lower `--rows` much below a million. At 200,000 the write takes under a
    second, Spark's fixed per-job overhead swamps the encryption cost, and the
    result inverts to "encryption is faster" -- which is exactly the impossible
    number the Stage 1 spike reported.
    """
    from confidentia.benchmarks import e4_encryption

    e4_encryption.main(rows=rows, repeats=repeats)


@bench_app.command("e2")
def bench_e2(
    rows: Annotated[int, typer.Option(help="Rows written per arm.")] = 3_000_000,
    repeats: Annotated[int, typer.Option(help="Timed repeats after warm-up.")] = 3,
) -> None:
    """E2 - output partition count against write time, file size and the EPC budget."""
    from confidentia.benchmarks import e2_partitioning

    e2_partitioning.main(rows=rows, repeats=repeats)


@bench_app.command("e1")
def bench_e1(
    records: Annotated[int, typer.Option(help="Records each arm ingests.")] = 60_000,
    repeats: Annotated[int, typer.Option(help="Timed repeats after warm-up.")] = 3,
) -> None:
    """E1 - streaming against batch throughput into the same Bronze table.

    Needs a broker (`bash scripts/kafka.sh start`) holding exactly the records this
    compares against. The streaming arm reads from `earliest` with a fresh
    checkpoint each repeat, so anything left in the topic from an earlier run is
    counted too and the arms stop being comparable -- reset with
    `bash scripts/kafka.sh reset --yes` and re-produce first.
    """
    from confidentia.benchmarks import e1_streaming

    e1_streaming.main(records=records, repeats=repeats)


@bench_app.command("e8")
def bench_e8(
    rows: Annotated[int, typer.Option(help="Rows replayed under each drift.")] = 60_000,
) -> None:
    """E8 - what the pipeline does when a source schema drifts.

    Measures behaviour rather than time: how many rows lose a column's value when a
    source adds, drops, renames or corrupts a field.
    """
    from confidentia.benchmarks import e8_schema_drift

    e8_schema_drift.main(rows=rows)


@bench_app.command("results")
def bench_results(
    experiment: Annotated[str | None, typer.Argument(help="E2, E4, ... or omit for all.")] = None,
) -> None:
    """Recorded results, newest per experiment/arm/metric."""
    from confidentia.store import db as store_db

    where, params = ("WHERE experiment = %s", (experiment.upper(),)) if experiment else ("", ())
    rows = store_db.fetch_all(
        f"""
        SELECT experiment, arm, metric, value, unit, label, repeats, p50, p90,
               rows_processed, measured_at
          FROM platform.benchmark_latest {where}
         ORDER BY experiment, arm
        """,
        params,
    )
    if not rows:
        typer.echo("no results recorded. Run `confidentia bench e2` or `e4` first.")
        raise typer.Exit(code=1)

    typer.echo(f"{'exp':4s} {'arm':12s} {'metric':16s} {'p50':>9s} {'p90':>9s} "
               f"{'rows':>11s} {'label':9s} measured")
    for row in rows:
        typer.echo(
            f"{row['experiment']:4s} {row['arm']:12s} {row['metric']:16s} "
            f"{row['p50']:>8.2f}{row['unit']} {row['p90']:>8.2f}{row['unit']} "
            f"{(row['rows_processed'] or 0):>11,} {row['label']:9s} "
            f"{row['measured_at']:%Y-%m-%d %H:%M}"
        )


plan_app = typer.Typer(
    help="C5 - enclave placement planner: where the boundary goes, and what it costs.",
    no_args_is_help=True,
)
app.add_typer(plan_app, name="plan")


def _modelled_banner() -> None:
    """Printed above every costed output. Not optional.

    Under the `sim` backend no enclave exists, so no cost here was observed. The
    placement *derivation* is not affected by that -- it follows from the contracts
    -- and the banner says which is which so the distinction survives being pasted
    into a report.
    """
    settings = get_settings()
    if settings.tee.is_measured:
        typer.echo(f"[{settings.tee.measurement_label}] backend {settings.tee.backend.value}")
        return
    typer.echo(
        f"[{settings.tee.measurement_label}] backend {settings.tee.backend.value}: "
        "no enclave hardware. Every duration below is a model output; the placement "
        "itself is derived from the schema contracts and does not depend on it."
    )


@plan_app.command("placement")
def plan_placement() -> None:
    """Which stages must run inside the enclave, and why.

    No costs, no model: this is the derivation alone, and it would give the same
    answer on real SGX hardware.
    """
    from confidentia.tee import placement as placement_module

    typer.echo(f"{'stage':20s} {'verdict':8s} why")
    for place in placement_module.place_all():
        typer.echo(f"{place.stage.name:20s} {place.verdict.value:8s} {place.reason}")

    violations = placement_module.boundary_violations()
    if violations:
        typer.echo("\nboundary violations:")
        for violation in violations:
            typer.echo(f"  {violation.location}: {violation.detail}")
            typer.echo(f"    {violation.protected_columns} protected columns affected")
            typer.echo(f"    remedy: {violation.remedy}")
    else:
        # Said out loud rather than left as an absence. This check found the
        # landing-zone hole (ADR 0022); silence now means it looked and found
        # nothing, which is a different statement from not having looked.
        typer.echo("\nboundary violations: none — every landing file scanned carries PARE")


@plan_app.command("show")
def plan_show(
    json_out: Annotated[Path | None, typer.Option("--json", help="Write the full plan here.")] = None,
) -> None:
    """Build the plan: placement, measured inputs, and modelled cost."""
    from confidentia.tee.planner import build_plan, weighted_bytes_per_row

    _modelled_banner()
    report = build_plan()

    for warning in report.warnings:
        typer.echo(f"  warning: {warning}")

    width = weighted_bytes_per_row(report.widths)
    typer.echo(
        f"\nmeasured inputs: {len(report.widths)} datasets, {width} B/row uncompressed "
        f"(row-weighted), {len(report.timings)} timed stage groups"
    )
    typer.echo(
        f"EPC budget {report.model.epc_budget_bytes / 1024 / 1024:,.0f} MB "
        f"-> {report.epc_threshold_rows:,} rows per partition"
    )

    typer.echo(f"\n{'group':10s} {'rows':>10s} {'parts':>6s} {'baseline':>11s} "
               f"{'overhead':>10s} {'factor':>7s}  stages")
    for group in report.recommended.groups:
        if group.cost is None:
            typer.echo(f"{group.group:10s} {'-':>10s} {'-':>6s} {'outside':>11s} "
                       f"{'-':>10s} {'-':>7s}  {', '.join(group.stage_names)}")
            continue
        partitions = group.cost.partition_plan.partitions if group.cost.partition_plan else 0
        typer.echo(
            f"{group.group:10s} {group.rows:>10,} {partitions:>6} "
            f"{group.cost.baseline_ms:>9,.0f}ms {group.cost.overhead_ms:>8,.0f}ms "
            f"x{group.cost.factor:>6.3f}  {', '.join(group.stage_names)}"
        )

    plan = report.recommended
    typer.echo(
        f"\npipeline: {plan.baseline_ms:,.0f} ms measured -> {plan.enclaved_ms:,.0f} ms "
        f"modelled inside the enclave (x{plan.factor:.3f})"
    )

    typer.echo("\nwhat would have to change to move a group out:")
    for counterfactual in report.counterfactuals:
        typer.echo(f"  {counterfactual.group:10s} {counterfactual.verdict}")

    if json_out:
        import json

        json_out.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
        typer.echo(f"\nwrote {json_out}")


@plan_app.command("sweep")
def plan_sweep(
    epc: Annotated[int | None, typer.Option(help="EPC size in MB. Defaults to the setting.")] = None,
) -> None:
    """At what data volume EPC-aware partition sizing starts to be worth anything.

    Sweeps volume rather than EPC size: at this project's 422k rows the whole
    working set fits in the budget, so every EPC size gives the same answer. The
    interesting question is where that stops being true.
    """
    from confidentia.tee.planner import sweep_volume

    _modelled_banner()
    typer.echo(
        f"\n{'scale':>6} {'rows':>12} {'parts':>6} {'rows/part':>11} "
        f"{'sized':>8} {'unsized':>9} {'sizing saves':>14}"
    )
    for point in sweep_volume(epc_size_mb=epc):
        typer.echo(
            f"{point.scale:>5.0f}x {point.rows:>12,} {point.partitions:>6} "
            f"{point.rows_per_partition:>11,} x{point.sized_factor:>6.3f} "
            f"x{point.unsized_factor:>7.3f} {point.sizing_saves_ms:>11,.0f}ms"
        )
    typer.echo(
        "\n'sizing saves' is the modelled paging cost that partitioning avoids. "
        "Zero means the working set already fits and partitioning buys nothing."
    )


api_app = typer.Typer(help="The read-only serving layer.", no_args_is_help=True)
app.add_typer(api_app, name="api")


@api_app.command("serve")
def api_serve(
    host: Annotated[str | None, typer.Option(help="Bind address.")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port.")] = None,
    reload: Annotated[bool, typer.Option(help="Reload on source change (development).")] = False,
) -> None:
    """Run the API.

    Uses uvicorn's factory mode so the application -- and therefore the connection
    pool -- is constructed inside the worker process. Importing a module-level app
    would open a pool in the parent and hand each forked worker a copy of a socket
    it does not own.
    """
    import uvicorn

    settings = get_settings()
    bind_host = host or settings.api.host
    bind_port = port or settings.api.port
    typer.echo(f"serving on http://{bind_host}:{bind_port}  (docs at /docs)")
    typer.echo(f"auth mode: {settings.auth.mode.value}   read-only: {settings.api.read_only}")
    uvicorn.run(
        "confidentia.api.app:create_app",
        factory=True,
        host=bind_host,
        port=bind_port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )


@api_app.command("token")
def api_token(
    subject: Annotated[str, typer.Argument(help="Who the token identifies.")] = "dev",
    role: Annotated[list[str] | None, typer.Option(help="viewer | operator | auditor")] = None,
    minutes: Annotated[int | None, typer.Option(help="Lifetime in minutes.")] = None,
) -> None:
    """Mint a development bearer token.

    Only works in `local` auth mode, and refuses otherwise: under OIDC the identity
    provider issues tokens and this process holds no signing key.
    """
    from confidentia.api.security import Role, issue_local_token

    try:
        roles = [Role(r) for r in (role or ["viewer"])]
    except ValueError as exc:
        typer.echo(f"unknown role: {exc}")
        typer.echo(f"valid roles: {', '.join(r.value for r in Role)}")
        raise typer.Exit(code=2) from exc

    try:
        token = issue_local_token(subject, roles, ttl_minutes=minutes)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(token)


@api_app.command("routes")
def api_routes() -> None:
    """List every route and the role it requires.

    Reads the role off the real dependency tree rather than a hand-written table,
    so a route whose guard is changed shows the change here. A route reported as
    `public` genuinely has no principal dependency.
    """
    from confidentia.api.app import create_app

    def walk(routes: list) -> list:
        """Flatten the route tree.

        FastAPI 0.141 keeps an included router as a single lazy `_IncludedRouter`
        entry in `app.routes` rather than splicing its routes in, so a flat scan
        finds only `/docs` and `/openapi.json`.
        """
        flat = []
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                flat.extend(walk(inner.routes))
            else:
                flat.append(route)
        return flat

    typer.echo(f"{'method':7s} {'path':44s} role")
    for route in sorted(walk(create_app().routes), key=lambda r: getattr(r, "path", "")):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if path is None or not methods - {"HEAD", "OPTIONS"}:
            continue
        dependant = getattr(route, "dependant", None)
        role = "public"
        if dependant is not None:
            # require_role tags each guard with the role it enforces, so this walks
            # the real dependency tree rather than a table that could go stale.
            for dep in dependant.dependencies:
                required = getattr(dep.call, "required_role", None)
                if required is not None:
                    role = str(required)
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            typer.echo(f"{method:7s} {path:44s} {role}")


if __name__ == "__main__":
    app()
