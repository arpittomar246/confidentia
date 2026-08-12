"""Restatement: rebuild the mart partitions a late arrival invalidated.

A chargeback confirms fraud 1 to 45 days after the transaction, by which time the
daily aggregate for that date has long been published. The fact is not wrong when
it is written and it is not wrong when the chargeback arrives -- it becomes wrong
retroactively, which no amount of care at write time prevents. Stage 6 marks the
affected partitions dirty; this DAG is what acts on the mark.

**The sensor polls, it does not schedule.** Restatement is driven by a condition in
the data, not by a clock: `gold dirty` exits 0 when there is work and 1 when there
is not, so the sensor succeeds only when something is actually invalidated. A
nightly rebuild-regardless would burn a full Gold build most nights and still be up
to 24 hours late on the nights it mattered.
"""

from __future__ import annotations

import os

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.sensors.bash import BashSensor

from confidentia_common import (
    DAG_KWARGS,
    GOLD,
    TENANTS,
    cli,
    doc,
    preflight_attestation,
)

# The sensor must give up before the next scheduled run would start. With
# `max_active_runs=1` a sensor that outlives its own interval blocks the very run
# it is waiting to hand over to, and the DAG quietly stops firing -- a deadlock
# that looks like "nothing to restate" from the outside.
#
# Overridable because `verify_dags.sh` cannot wait fifty minutes to observe a
# sensor time out. Production behaviour is the default; only the test shortens it.
SENSOR_TIMEOUT = int(os.environ.get("CONFIDENTIA_RESTATEMENT_SENSOR_TIMEOUT", 50 * 60))
SENSOR_POKE = int(os.environ.get("CONFIDENTIA_RESTATEMENT_SENSOR_POKE", 300))

KWARGS = {**DAG_KWARGS}

with DAG(
    dag_id="confidentia_restatement",
    description="Detect late-arrival damage and rebuild the affected marts",
    schedule="@hourly",
    tags=["confidentia", "gold", "restatement"],
    **KWARGS,
) as dag:
    dirty = BashSensor(
        task_id="await_dirty_partitions",
        bash_command=cli("gold", "dirty"),
        poke_interval=SENSOR_POKE,
        timeout=SENSOR_TIMEOUT,
        mode="reschedule",
        soft_fail=True,
        doc_md=doc(
            """
            Succeeds when `platform.mart_partition_state` holds dirty partitions.

            `soft_fail=True` so a timeout marks the run **skipped**, not failed: an
            hour with no late arrivals is the normal case and must not page anyone.
            This is the one place in the project where skipping is right, and it is
            the opposite of the promotion gate, where skipping would be a defect.

            The timeout is deliberately shorter than the schedule interval. With
            `max_active_runs=1`, a sensor that outlives its own interval blocks the
            run it is waiting to hand over to, and the DAG stops firing entirely --
            a deadlock that looks like "nothing to restate" from the outside.
            """
        ),
    )

    restate = [
        BashOperator(
            task_id=f"restate_{tenant}",
            bash_command=cli("gold", "build", tenant),
            doc_md=doc(
                f"""
                Rebuilds {tenant}'s marts, clearing the dirty flags.

                Currently a **full refresh**: restatement identifies which
                partitions are invalid but rebuilds the whole table. Incremental
                recompute is what experiment E3 is meant to measure, and the gap
                is deliberate rather than unnoticed.
                """
            ),
            outlets=[GOLD],
        )
        for tenant in TENANTS
    ]

    report = BashOperator(
        task_id="restatement_report",
        bash_command=cli("gold", "restatements"),
    )

    preflight_attestation() >> dirty >> restate >> report
