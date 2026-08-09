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
    from confidentia.generators import financial as fin
    from confidentia.generators import healthcare as hc
    from confidentia.generators.base import GeneratorConfig
    from confidentia.generators.writers import write_dataset, write_reference_table

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
        scale = hc.HealthcareScale.for_rows(rows)
        typer.echo(
            f"healthcare scale: {scale.encounters:,} encounters, {scale.patients:,} patients, "
            f"{scale.providers:,} providers, {scale.facilities:,} facilities"
        )
        write_reference_table(
            hc.generate_facilities(config, scale), root=root, tenant=tenant, dataset="facility"
        )
        write_reference_table(
            hc.generate_plans(config), root=root, tenant=tenant, dataset="insurance_plan"
        )
        write_reference_table(
            hc.generate_providers(config, scale), root=root, tenant=tenant, dataset="provider"
        )
        for produce in (
            hc.generate_patients,
            hc.generate_patient_changes,
            hc.generate_encounters,
            hc.generate_claim_lines,
        ):
            result = write_dataset(produce(config, scale), root=root, config=config, fmt=fmt)
            total_rows += result.rows
            total_defects += result.defects
    else:
        scale = fin.FinancialScale.for_rows(rows)
        typer.echo(
            f"financial scale: {scale.transactions:,} transactions, "
            f"{scale.accounts:,} accounts, {scale.merchants:,} merchants"
        )
        write_reference_table(
            fin.generate_merchants(config, scale), root=root, tenant=tenant, dataset="merchant"
        )
        for produce in (
            fin.generate_accounts,
            fin.generate_transactions,
            fin.generate_chargebacks,
        ):
            result = write_dataset(produce(config, scale), root=root, config=config, fmt=fmt)
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


@keys_app.command("approve")
def keys_approve(
    approved_by: Annotated[str, typer.Option(help="Who is approving this build.")] = "developer",
    tenants: Annotated[str, typer.Option(help="Comma-separated tenants, or '*'.")] = "*",
) -> None:
    """Approve the currently running code for key release.

    Needed after any change to a measured package (crypto, pipelines, quality,
    lineage). That inconvenience is the feature: it is what makes "which code
    touched this data" enforceable rather than merely recorded.
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
        else [
            c.dataset
            for c in all_contracts()
            if c.tenant.value == tenant and c.dataset != "patient_cdc"
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


if __name__ == "__main__":
    app()
