"""Quality scoring and evaluation against the ground-truth answer key.

This DAG exists to encode an ordering that used to be tribal knowledge, and which
silently produces a flattering result when it is broken:

    quality run --layer bronze  ->  evaluate quality  ->  quality run --layer silver

The detections table is **overwritten per run**. Scoring Silver before evaluating
would replace the Bronze detections in place, and the evaluation would then be
measuring the platform's own cleaning against an answer key describing what the
source sent. Stage 7b found exactly this had already happened by accident, which is
why detections now carry their layer and `evaluate` refuses the wrong one.

Silver scoring runs last rather than not at all: the Silver score is still the
promotion-relevant number for the layer the platform publishes, and it is safe once
the evaluation has consumed the Bronze detections.
"""

from __future__ import annotations

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.empty import EmptyOperator

from confidentia_common import (
    BRONZE,
    DAG_KWARGS,
    TENANTS,
    cli,
    doc,
    preflight_attestation,
)

with DAG(
    dag_id="confidentia_quality",
    description="Bronze scoring, evaluation vs ground truth, then Silver scoring",
    schedule=[BRONZE],
    tags=["confidentia", "quality", "evaluation"],
    **DAG_KWARGS,
) as dag:
    start = preflight_attestation()
    scored = EmptyOperator(task_id="bronze_scored")
    evaluated = EmptyOperator(task_id="evaluated")

    for tenant in TENANTS:
        bronze_quality = BashOperator(
            task_id=f"quality_bronze_{tenant}",
            bash_command=cli("quality", "run", tenant, "--layer", "bronze"),
        )
        start >> bronze_quality >> scored

    for tenant in TENANTS:
        evaluate = BashOperator(
            task_id=f"evaluate_{tenant}",
            bash_command=cli("evaluate", "quality", tenant),
            doc_md=doc(
                """
                Precision, recall and F1 per defect family against the Stage 2
                manifest, matched strictly on `(record_key, field, family)`
                (ADR 0012).

                Refuses outright if the detections on disk describe a layer other
                than Bronze, rather than producing a plausible number.
                """
            ),
        )
        scored >> evaluate >> evaluated

    for tenant in TENANTS:
        silver_quality = BashOperator(
            task_id=f"quality_silver_{tenant}",
            bash_command=cli("quality", "run", tenant, "--layer", "silver"),
            doc_md=doc(
                """
                Runs **after** the evaluation, never before. This is the whole
                reason the two are in one DAG: the ordering is a correctness
                constraint, and a dependency edge is the only place to put it where
                it cannot be forgotten.
                """
            ),
        )
        evaluated >> silver_quality
