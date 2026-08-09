"""Evaluation harness: score what the platform detected against what was injected.

This package lives **outside** ``quality/`` on purpose. The ground-truth manifest
the generator writes beside each dataset is the answer key, and
``scripts/check_ground_truth_isolation.sh`` forbids any pipeline package from
reading it — a quality layer that could see the answers would score perfectly and
mean nothing. The guard greps for the manifest's filename anywhere under
``src/confidentia``, so it is not named here either; that is the guard working, not
a limitation of it.

Evaluation is not a pipeline. It runs after the fact, reads both sides, and
reports precision, recall and F1 per defect family. It is the only place in the
codebase besides the generator's own writer that is allowed near the answer key,
and it reaches it through the single sanctioned accessor,
``generators.writers.iter_defects``, never by opening the file itself.
"""

from confidentia.evaluation.scoring import (
    MATCHING_CRITERION,
    UNSCOREABLE_FAMILIES,
    Claim,
    FamilyScore,
    score_claims,
)

__all__ = [
    "MATCHING_CRITERION",
    "UNSCOREABLE_FAMILIES",
    "Claim",
    "FamilyScore",
    "score_claims",
]
