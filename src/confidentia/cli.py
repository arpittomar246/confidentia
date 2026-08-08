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


if __name__ == "__main__":
    app()
