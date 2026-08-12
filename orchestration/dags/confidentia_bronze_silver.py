"""Bronze, the quality gate, then Silver.

Triggered by the ``landing`` dataset rather than by a clock, so Silver is rebuilt
because new data arrived and not because it is 03:00.

**This DAG is where Stage 7a's `promotion_blocked` finally does something.** It has
been computed since Stage 7a and consumed by nothing; a score nobody acts on is a
dashboard, not a control. The gate sits between Bronze and Silver, and when it
blocks, the task goes **red** and Silver does not run.

Three orderings here are correctness constraints, not preferences:

1. **Quality runs on Bronze, before the gate.** Bronze is what the source sent.
   Scoring Silver and gating on that would be grading our own cleaning, because
   Silver has already quarantined the failures.
2. **The gate fails, it does not skip.** A skipped task in Airflow is green and its
   downstream tasks still run, so a gate built on `ShortCircuitOperator` would let
   Silver build from a Bronze layer just judged unfit while the DAG looked healthy.
   `quality gate` exits non-zero; `verify_gate.sh` asserts that it does.
3. **Nothing may run Silver-layer quality between here and `evaluate`.** The
   detections table is overwritten per run and carries its layer, so a Silver run
   in between makes the evaluation refuse. That constraint used to live in
   somebody's head; here it is in a dependency graph.
"""

from __future__ import annotations

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.empty import EmptyOperator

from confidentia_common import (
    BRONZE,
    DAG_KWARGS,
    LANDING,
    SILVER,
    TENANTS,
    cli,
    doc,
    preflight_attestation,
)

with DAG(
    dag_id="confidentia_bronze_silver",
    description="Bronze write, Bronze quality, promotion gate, Silver write",
    schedule=[LANDING],
    tags=["confidentia", "bronze", "silver", "gate"],
    **DAG_KWARGS,
) as dag:
    start = preflight_attestation()
    bronze_done = EmptyOperator(task_id="bronze_done", outlets=[BRONZE])

    for tenant in TENANTS:
        bronze = BashOperator(
            task_id=f"bronze_{tenant}",
            bash_command=cli("bronze", "write", tenant),
            doc_md=doc(
                f"""
                Writes {tenant} to encrypted Delta with **per-column keys by
                sensitivity class** (C1), released only against a valid attestation
                (C2).

                Asserts the `PARE` magic on the written files afterwards. That
                check is not paranoia: configured as DataFrameWriter options
                instead of on the Hadoop Configuration, Delta writes **plaintext**
                silently -- no error, no warning (ADR 0007).
                """
            ),
        )
        quality = BashOperator(
            task_id=f"quality_bronze_{tenant}",
            bash_command=cli("quality", "run", tenant, "--layer", "bronze"),
            doc_md=doc(
                """
                Scores what the source actually sent. Must be the last quality run
                before `evaluate`, because the detections table is overwritten per
                run.
                """
            ),
        )
        gate = BashOperator(
            task_id=f"gate_{tenant}",
            bash_command=cli("quality", "gate", tenant, "--layer", "bronze"),
            # No retry. Every other task retries once for the transient case of a
            # Spark session losing its JVM, but a gate decision is deterministic:
            # the score will not have changed in two minutes, so a retry only
            # delays the alert while making the UI show a task still "trying".
            retries=0,
            doc_md=doc(
                """
                **Fails the task when quality is below the promotion threshold**,
                which stops Silver for this tenant.

                Recorded in `platform.promotion_gate` (passes as well as blocks)
                and as a failed run in `platform.run`, so a pipeline that stopped
                overnight explains itself from the database -- `confidentia runs`
                -- without opening this UI.

                A missing score blocks rather than passes: no score means nothing
                was checked, not that nothing is wrong.

                To promote anyway, deliberately and audibly:
                `confidentia quality gate <tenant> --override --reason "..."`.
                """
            ),
        )
        silver = BashOperator(
            task_id=f"silver_{tenant}",
            bash_command=cli("silver", "write", tenant),
            doc_md=doc(
                """
                Conform, validate, quarantine, deduplicate, tokenise. Cross-source
                deduplication resolves the double delivery here.
                """
            ),
        )
        # Provenance is recorded after the write rather than inside it (ADR 0015),
        # which leaves a window where the table has changed and no entry says so.
        # Placing the recording immediately after the write is what closes that
        # window, so these edges are the mechanism, not decoration.
        bronze_lineage = BashOperator(
            task_id=f"lineage_bronze_{tenant}",
            bash_command=cli("lineage", "record", tenant, "--layer", "bronze"),
            doc_md=doc(
                """
                Signs a Merkle root over every Bronze record for this tenant, bound
                to the code measurement and the key IDs released to produce it
                (C3).

                Runs **before** the gate deliberately. The commitment must cover
                what the source actually sent, including the batch a gate is about
                to reject -- provenance for rejected data is exactly the provenance
                someone asks for afterwards.
                """
            ),
        )
        silver_lineage = BashOperator(
            task_id=f"lineage_silver_{tenant}",
            bash_command=cli("lineage", "record", tenant, "--layer", "silver"),
            doc_md=doc(
                """
                The Silver entry cites the current Bronze entry's digest, so the
                edge from Bronze to Silver is a hash reference rather than a name
                that could be pointed elsewhere.
                """
            ),
        )
        start >> bronze >> bronze_lineage >> quality >> gate >> silver
        silver >> silver_lineage >> bronze_done

    dimension = BashOperator(
        task_id="silver_dimension_healthcare",
        bash_command=cli("silver", "dimension", "healthcare"),
        doc_md=doc(
            """
            Builds the SCD2 patient dimension from the CDC change stream.

            Transaction-time, not valid-time: `valid_from` is when the platform
            *learned* of a change, so a point-in-time query for January returns
            nothing. Bi-temporal is future work and the limitation is deliberate
            rather than unnoticed.
            """
        ),
        outlets=[SILVER],
    )

    bronze_done >> dimension
