"""Drain the transaction topic into Bronze on a schedule.

**This is a bounded drain, not a long-running consumer.** A continuously running
Structured Streaming query is not a DAG task: Airflow would show one task running
for days, its retry semantics would restart a query that was already checkpointed,
and `execution_timeout` would kill a healthy stream. The consumer therefore runs
with Spark's `availableNow` trigger — it processes what is in the topic, commits,
and exits — which is a task with a beginning and an end.

The cost is latency floored at the schedule interval. That is the honest trade for
a platform whose other stages are batch, and `stream consume --follow` remains
available for a genuinely continuous run outside the orchestrator.

Exactly-once survives the schedule because it does not depend on it: each
micro-batch is written with Delta's `txnAppId`/`txnVersion`, so a task killed
between the Delta commit and the checkpoint write replays the batch as a no-op
rather than duplicating it.
"""

from __future__ import annotations

from airflow import DAG
from airflow.operators.bash import BashOperator

from confidentia_common import BRONZE, DAG_KWARGS, cli, doc, preflight_attestation

with DAG(
    dag_id="confidentia_streaming",
    description="Drain the Kafka transaction topic into the shared Bronze table",
    # Every fifteen minutes rather than continuously: one Spark driver at a time is
    # the memory constraint the whole platform runs under, and this DAG must not
    # hold that budget while the batch DAGs need it.
    schedule="*/15 * * * *",
    tags=["confidentia", "streaming", "bronze"],
    **DAG_KWARGS,
) as dag:
    drain = BashOperator(
        task_id="drain_transactions",
        bash_command=cli("stream", "consume", "financial", "transaction"),
        doc_md=doc(
            """
            Consumes everything currently in `fin.txn.raw` into
            `lake/bronze/financial/transaction` — the **same table** the file and
            REST collectors write (ADR 0014), tagged `_source_system = 'kafka'`.

            Silver deduplicates across it without knowing streaming exists, and
            Stage 7c's per-source Bronze uniqueness already distinguishes a
            redelivery from a duplicate.

            Asserts the `PARE` magic after writing. A streaming writer is a fresh
            place to make ADR 0007's mistake, where Delta writes plaintext with no
            error anywhere.
            """
        ),
        outlets=[BRONZE],
    )

    status = BashOperator(
        task_id="topic_status",
        bash_command=cli("stream", "status"),
        # Reporting only. A topic that is empty because nothing was produced is
        # not a failure, and failing here would page someone for a quiet period.
        retries=0,
        doc_md=doc(
            """
            Partition count, message count, and the recent stream runs. Exits
            non-zero only if the topic does not exist at all, which means the
            broker is not running or was never configured.
            """
        ),
    )

    preflight_attestation() >> drain >> status
