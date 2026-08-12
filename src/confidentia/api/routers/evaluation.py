"""Precision/recall/F1 of the quality rules against the ground-truth answer key.

This is the one family of numbers in the platform that is a claim *about* the
platform rather than about the data, so two things are enforced here.

``rule_exists`` is surfaced, never filtered out. A family the generator corrupts
and no rule covers scores recall 0 for a reason that has nothing to do with rule
quality -- Stage 7b's headline finding. Hiding those rows would restore exactly the
flattering picture the evaluation harness exists to prevent.

The ``duplicate_record`` caveat is attached to the payload rather than left in a
report. Bronze cannot distinguish a duplicate from a legitimate redelivery, so its
encounter precision is contaminated; a client rendering that number without the
caveat would be presenting a measurement artefact as a result.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from confidentia.api.deps import DatasetQuery, LayerQuery, TenantQuery, fetch_all, optional_filters
from confidentia.api.security import ViewerDep

router = APIRouter(prefix="/evaluation", tags=["evaluation"], dependencies=[ViewerDep])

_CONTAMINATED_FAMILY = "duplicate_record"


@router.get("/latest", summary="Latest P/R/F1 per dataset and defect family")
def latest(
    tenant: TenantQuery = None,
    dataset: DatasetQuery = None,
    layer: LayerQuery = None,
) -> dict[str, Any]:
    where, params = optional_filters({"tenant": tenant, "dataset": dataset, "layer": layer})
    items = fetch_all(
        f"""
        SELECT tenant, dataset, layer, family, true_positives, false_positives,
               false_negatives, truth_claims, detected_claims,
               precision, recall, f1, rule_exists, evaluated_at
          FROM platform.dq_evaluation_latest WHERE 1=1 {where}
         ORDER BY tenant, dataset, f1 NULLS FIRST, family
        """,
        params,
    )
    return {
        "items": items,
        "caveats": [
            {
                "applies_to": {"layer": "bronze", "family": _CONTAMINATED_FAMILY},
                "note": (
                    "At Bronze a duplicate and a legitimate redelivery are the same "
                    "bytes, so precision for this family is not a measure of rule "
                    "quality. Exclude it when comparing datasets."
                ),
            }
        ],
    }


@router.get("/summary", summary="Weighted P/R/F1 per dataset")
def summary(tenant: TenantQuery = None) -> dict[str, Any]:
    """Aggregated from the confusion-matrix counts, not by averaging F1.

    Averaging per-family F1 weights a family with three defects the same as one with
    four hundred. Summing TP/FP/FN and dividing once is the micro-average, and it is
    the number that answers "how good is the quality layer on this dataset".
    """
    where, params = optional_filters({"tenant": tenant})
    return {
        "items": fetch_all(
            f"""
            WITH counted AS (
                SELECT tenant, dataset, layer,
                       sum(true_positives)  AS tp,
                       sum(false_positives) AS fp,
                       sum(false_negatives) AS fn,
                       count(*) FILTER (WHERE NOT rule_exists) AS families_uncovered,
                       count(*) AS families
                  FROM platform.dq_evaluation_latest WHERE 1=1 {where}
                 GROUP BY 1, 2, 3
            )
            SELECT tenant, dataset, layer, tp, fp, fn, families, families_uncovered,
                   CASE WHEN tp + fp > 0 THEN tp::float / (tp + fp) END AS precision,
                   CASE WHEN tp + fn > 0 THEN tp::float / (tp + fn) END AS recall,
                   CASE WHEN 2 * tp + fp + fn > 0
                        THEN (2.0 * tp) / (2 * tp + fp + fn) END AS f1
              FROM counted ORDER BY tenant, dataset, layer
            """,
            params,
        ),
        "method": "micro-average over defect families",
    }


@router.get("/coverage", summary="Defect families with no rule at all")
def coverage() -> dict[str, Any]:
    """Should be empty. Stage 7c took this from 5 to 0, and CI keeps it there.

    Kept as an endpoint precisely because it should be empty: a dashboard tile that
    is normally blank and occasionally is not is how the next coverage gap gets
    noticed before it depresses a recall number in a report.
    """
    gaps = fetch_all(
        """
        SELECT tenant, dataset, layer, family, truth_claims, evaluated_at
          FROM platform.dq_evaluation_latest
         WHERE NOT rule_exists AND truth_claims > 0
         ORDER BY truth_claims DESC
        """
    )
    return {"items": gaps, "uncovered_families": len(gaps)}
