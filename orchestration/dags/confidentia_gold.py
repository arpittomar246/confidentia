"""Gold: star schema, disclosure control, and the Postgres marts.

Triggered by the ``silver`` dataset.

Disclosure control (C4) runs inside `gold build`, not as a separate task, and that
is deliberate: a suppression step that can be skipped independently of the publish
it protects is a step that will eventually be skipped. The k-anonymity threshold
and complementary suppression are part of writing a mart, not a stage after it.
"""

from __future__ import annotations

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.empty import EmptyOperator

from confidentia_common import (
    DAG_KWARGS,
    GOLD,
    SILVER,
    TENANTS,
    cli,
    doc,
    preflight_attestation,
)

with DAG(
    dag_id="confidentia_gold",
    description="Star schema, disclosure control, Postgres marts",
    schedule=[SILVER],
    tags=["confidentia", "gold", "disclosure"],
    **DAG_KWARGS,
) as dag:
    start = preflight_attestation()
    published = EmptyOperator(task_id="published", outlets=[GOLD])

    for tenant in TENANTS:
        build = BashOperator(
            task_id=f"gold_{tenant}",
            bash_command=cli("gold", "build", tenant),
            doc_md=doc(
                f"""
                Builds the {tenant} star schema as encrypted Delta, then publishes
                disclosure-controlled aggregates to the `gold` schema in Postgres.

                Only aggregates leave the lake. `delta-rs` cannot read encrypted
                Parquet, so a JVM-free API layer could not query Bronze, Silver or
                Gold Delta at all -- publishing to Postgres is what makes a serving
                layer possible without weakening the encryption.
                """
            ),
        )
        start >> build >> published

    disclosure = BashOperator(
        task_id="disclosure_report",
        bash_command=cli("gold", "disclosure"),
        doc_md=doc(
            """
            What each publish released and what it withheld.

            Complementary suppression is mandatory, not optional: primary
            suppression alone leaves a suppressed value recoverable by subtracting
            the published cells from a published row total.
            """
        ),
    )

    published >> disclosure
