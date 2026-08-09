"""Read the Stage 7a detections table and reduce it to claims.

This is the only Spark-touching module in the package. It exists so that
``scoring.py`` stays pure: the detections table is encrypted Delta and needs a
tenant-scoped session with released keys to read at all, which is exactly the kind
of dependency that makes a scoring harness untestable if it leaks into the
matching logic.

``record_key`` is encrypted in the detections table, so the reduction to distinct
triples happens **inside** the session, after the keys are released and before
they are dropped. What crosses the boundary into the evaluator is a set of
``Claim`` — still record-identifying, still handled in-process, never written
anywhere but the confusion counts.
"""

from __future__ import annotations

from confidentia.common.logging import get_logger
from confidentia.evaluation.scoring import Claim
from confidentia.quality.runner import detections_path
from confidentia.store import db

logger = get_logger(__name__)

# Above this many distinct claims the driver-side set operations stop being
# sensible on a 2 GB driver and the join belongs in Spark. Nothing in this project
# comes close (the largest dataset produces tens of thousands), so the limit is a
# tripwire for a future scale-up rather than a live constraint.
CLAIM_LIMIT = 5_000_000


class DetectionsMissingError(FileNotFoundError):
    """No detections table for this dataset."""


class WrongLayerError(RuntimeError):
    """The detections on disk describe a layer other than the one requested.

    The detections table is overwritten per run, so a later Silver run replaces a
    Bronze one in place. Scoring Silver detections against the answer key would
    measure the platform's own cleaning — Silver has already quarantined the
    defects — and produce a recall that flatters the quality layer for work the
    quality layer did not do. Refusing is the only correct response; the fix is to
    re-run ``quality run <tenant> --layer bronze``.
    """


def load_claims(tenant: str, dataset: str, *, layer: str = "bronze") -> set[Claim]:
    """Distinct ``(record_key, field, family)`` claims from the detections table."""
    from confidentia.spark.session import session_for_tenant

    path = detections_path(tenant, dataset)
    if not path.exists():
        raise DetectionsMissingError(
            f"no detections at {path}; run `quality run {tenant} {dataset} "
            f"--layer {layer}` first"
        )

    with session_for_tenant(tenant, app_name=f"confidentia-evaluate-{tenant}") as (spark, _):
        frame = spark.read.format("delta").load(str(path))

        if "layer" not in frame.columns:
            raise WrongLayerError(
                f"detections at {path} were written before layer provenance was "
                "recorded, so there is no way to tell which layer they describe. "
                f"Re-run `quality run {tenant} {dataset} --layer {layer}`."
            )

        observed = sorted(
            row["layer"] for row in frame.select("layer").distinct().collect()
        )
        if observed != [layer]:
            raise WrongLayerError(
                f"detections at {path} describe layer(s) {observed}, not {layer!r}. "
                f"Re-run `quality run {tenant} {dataset} --layer {layer}`."
            )

        rows = (
            frame.select("record_key", "field", "family")
            .where("record_key IS NOT NULL AND field IS NOT NULL")
            .distinct()
            .collect()
        )

    if len(rows) > CLAIM_LIMIT:
        raise MemoryError(
            f"{len(rows):,} distinct claims exceeds the driver-side limit of "
            f"{CLAIM_LIMIT:,}; move the match into Spark"
        )

    claims = {
        Claim(record_key=r["record_key"], field=r["field"], family=r["family"])
        for r in rows
    }
    logger.info(
        "detections_loaded",
        tenant=tenant,
        dataset=dataset,
        layer=layer,
        distinct_claims=len(claims),
    )
    return claims


def rule_coverage(tenant: str, dataset: str) -> set[tuple[str, str]]:
    """``(family, field)`` pairs for which a rule was registered.

    Read from ``platform.dq_rule`` rather than rebuilt from the contract, because
    the catalogue is what actually ran: the runner appends the uniqueness rule and
    the referential rules at run time, and neither appears in ``build_rules``.
    Rebuilding here would under-report coverage and turn a working rule into a
    phantom gap.
    """
    rows = db.fetch_all(
        """
        SELECT DISTINCT family, field
          FROM platform.dq_rule
         WHERE tenant = %s AND dataset = %s AND is_active AND field IS NOT NULL
        """,
        (tenant, dataset),
    )
    return {(row["family"], row["field"]) for row in rows}
