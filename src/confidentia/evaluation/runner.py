"""Orchestrate an evaluation run and persist the result.

Bronze is the default and the only layer that means anything here. The answer key
describes what the *source* sent; Silver has already quarantined those records, so
recall measured there is the platform grading its own cleaning. ``detections.py``
refuses outright if the detections on disk describe another layer, so the default
cannot be defeated by forgetting which run happened last.
"""

from __future__ import annotations

import time
from typing import Any

from confidentia.common.logging import get_logger
from confidentia.evaluation import detections as detections_source
from confidentia.evaluation import ground_truth
from confidentia.evaluation.scoring import EvaluationResult, FamilyScore, score_claims
from confidentia.store import db
from confidentia.store.repositories import RunType, track_run

logger = get_logger(__name__)

# Datasets the generator never corrupts, so there is nothing to score them
# against. `patient_cdc` is a replication stream, and the reference tables are
# small dimensions written whole.
NO_GROUND_TRUTH = frozenset(
    {"patient_cdc", "patient_change", "facility", "provider", "insurance_plan", "merchant"}
)


def evaluate_dataset(
    tenant: str, dataset: str, *, layer: str = "bronze"
) -> EvaluationResult:
    """Score one dataset's detections against its answer key."""
    started = time.perf_counter()

    truth, row_counts = ground_truth.load_claims(tenant, dataset)
    coverage = detections_source.rule_coverage(tenant, dataset)

    with track_run(
        RunType.EVALUATION,
        tenant=tenant,
        dataset=dataset,
        source="evaluation",
        metadata={"layer": layer, "manifest_rows": sum(row_counts.values())},
    ) as run:
        detected = detections_source.load_claims(tenant, dataset, layer=layer)
        result = score_claims(
            detected,
            truth,
            tenant=tenant,
            dataset=dataset,
            layer=layer,
            rule_coverage=coverage,
        )
        _persist(run.run_id, result)

        run.add(read=len(detected), written=result.overall.true_positives)
        run.note(
            precision=_round(result.overall.precision),
            recall=_round(result.overall.recall),
            f1=_round(result.overall.f1),
            families=len(result.families),
        )

    logger.info(
        "evaluation_complete",
        tenant=tenant,
        dataset=dataset,
        layer=layer,
        precision=_round(result.overall.precision),
        recall=_round(result.overall.recall),
        seconds=round(time.perf_counter() - started, 1),
    )
    return result


def evaluatable_datasets(tenant: str) -> list[str]:
    """Datasets that have both a contract and an answer key."""
    from confidentia.catalog.contracts import all_contracts

    return [
        c.dataset
        for c in all_contracts()
        if c.tenant.value == tenant
        and c.dataset not in NO_GROUND_TRUTH
        and ground_truth.has_ground_truth(tenant, c.dataset)
    ]


def _round(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


def _rows(run_id: Any, result: EvaluationResult) -> list[tuple[Any, ...]]:
    def row(score: FamilyScore, scope: str) -> tuple[Any, ...]:
        return (
            run_id,
            result.tenant,
            result.dataset,
            result.layer,
            scope,
            score.family,
            score.field,
            score.true_positives,
            score.false_positives,
            score.false_negatives,
            score.truth_claims,
            score.detected_claims,
            score.precision,
            score.recall,
            score.f1,
            score.rule_exists,
            score.scoreable,
            result.matching_criterion,
        )

    return [
        row(result.overall, "overall"),
        *[row(s, "family") for s in result.families],
        *[row(s, "field") for s in result.fields],
        *[row(s, "unscoreable") for s in result.unscoreable],
    ]


def _persist(run_id: Any, result: EvaluationResult) -> None:
    with db.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO platform.dq_evaluation
                (run_id, tenant, dataset, layer, scope, family, field,
                 true_positives, false_positives, false_negatives,
                 truth_claims, detected_claims, precision, recall, f1,
                 rule_exists, scoreable, matching_criterion)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s)
            """,
            _rows(run_id, result),
        )
