"""Ingestion: file drops, the REST source, and the CDC stream.

Produces the ``landing`` dataset, which is what wakes the Bronze/Silver DAG.

**The mock source API is not started here.** It represents an external system, and
a DAG that started its own upstream would be modelling something false — you do
not boot your vendor's API before reading from it. The REST task therefore fails
if nothing is listening on :8081, which is the correct behaviour for a source that
is down, and `uvicorn confidentia.ingestion.source_api:app --port 8081` is an
operator step documented in the stage report.

**CDC ordering is not cosmetic.** The slot must exist before the OLTP source is
mutated: a slot created afterwards has no write-ahead log to replay, and the
change stream comes back empty with no error anywhere. That is why `slot create`
is a task rather than a setup step someone remembers.
"""

from __future__ import annotations

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.empty import EmptyOperator

from confidentia_common import DAG_KWARGS, LANDING, TENANTS, cli, doc

with DAG(
    dag_id="confidentia_ingest",
    description="File, REST and CDC collectors into the landing zone",
    schedule=None,  # manually triggered; see the module docstring on the REST source
    tags=["confidentia", "ingest"],
    **DAG_KWARGS,
) as dag:
    start = EmptyOperator(task_id="start")

    file_drops = [
        BashOperator(
            task_id=f"file_{tenant}",
            bash_command=cli("ingest", "file", tenant),
            doc_md=doc(
                f"""
                SHA-256 verified per file. A checksum mismatch is a `TerminalError`
                and is never retried -- corrupt input does not become correct on a
                second read.

                Idempotent by content: re-running this task against unchanged
                input lands 0 records rather than duplicating {tenant}.
                """
            ),
        )
        for tenant in TENANTS
    ]

    rest = BashOperator(
        task_id="rest_encounter",
        bash_command=cli("ingest", "rest", "healthcare", "encounter"),
        doc_md=doc(
            """
            Redelivers every encounter the file drop already landed. This is
            deliberate and load-bearing: it is what gives Bronze its double
            delivery, which is what Silver's cross-source deduplication exists to
            resolve (120,798 -> 59,121).

            Requires the mock source API on :8081. It is not started by this DAG.
            """
        ),
    )

    slot = BashOperator(
        task_id="cdc_slot_create",
        bash_command=cli("ingest", "slot", "create"),
        doc_md=doc(
            """
            Idempotent. A replication slot pins write-ahead log on the source until
            it is consumed, so it is created explicitly rather than implicitly.
            """
        ),
    )
    seed = BashOperator(task_id="oltp_seed", bash_command=cli("ingest", "oltp", "seed"))
    cdc_initial = BashOperator(task_id="cdc_initial", bash_command=cli("ingest", "cdc"))
    mutate = BashOperator(
        task_id="oltp_mutate",
        bash_command=cli("ingest", "oltp", "mutate", "--limit", "2000"),
        doc_md=doc(
            """
            Replays the generated mutation script against the source of record.
            Without changes there is nothing for SCD2 to track, so the Silver
            patient dimension could only be asserted, never exercised.
            """
        ),
    )
    cdc_changes = BashOperator(
        task_id="cdc_changes",
        bash_command=cli("ingest", "cdc"),
        doc_md=doc(
            """
            Consumes the change stream and acknowledges the slot **only after a
            durable write** (ADR 0009). Acknowledging on receipt would drop
            changes on a crash between the two.
            """
        ),
        outlets=[LANDING],
    )

    start >> file_drops >> rest >> slot >> seed >> cdc_initial >> mutate >> cdc_changes
