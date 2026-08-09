"""Load and normalise the Stage 2 answer key.

The manifest is reached through ``generators.writers.iter_defects`` and never by
opening the file directly. That is not stylistic: the isolation guard greps the
whole of ``src/confidentia`` for the manifest's filename and permits it only in the
writer that produces it, so a direct read here would fail CI — correctly, because
the single sanctioned accessor is what makes the isolation auditable.

Normalisation is small but not trivial. The manifest is a *log of corruptions*,
one row per injected defect, written per chunk; the evaluation needs a *set of
claims*. Two adjustments happen here and nowhere else:

* ``latent_fraud`` is dropped. It is a label, not a defect — a fraud that will be
  confirmed by a chargeback 1 to 45 days later, carried in the same file because the
  generator writes one answer key per dataset. It belongs to the analytics
  evaluation. It also carries no field, so leaving it in would create claims that
  cannot match anything and depress recall for a reason unrelated to quality.
* Record keys are coerced to ``str``. The detections table stores them cast to
  string (business keys are text everywhere, but the cast is explicit in the
  runner), and a silent ``int``/``str`` mismatch would produce a clean-looking
  zero-recall result.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from confidentia.common.logging import get_logger
from confidentia.config.settings import get_settings
from confidentia.evaluation.scoring import NON_DEFECT_TYPES, Claim

logger = get_logger(__name__)


class GroundTruthMissingError(FileNotFoundError):
    """No answer key exists for this dataset.

    Raised rather than returning an empty set. An empty answer key scores every
    detection as a false positive and reports precision 0.0, which is a fabricated
    result — the honest answer is that the dataset cannot be evaluated.
    """


def raw_root() -> Path:
    return get_settings().data_root / "raw"


def _frames(tenant: str, dataset: str, root: Path | None = None) -> Iterator[object]:
    from confidentia.generators.writers import iter_defects

    yield from iter_defects(root or raw_root(), tenant, dataset)


def has_ground_truth(tenant: str, dataset: str, root: Path | None = None) -> bool:
    return any(True for _ in _frames(tenant, dataset, root))


def load_claims(
    tenant: str, dataset: str, root: Path | None = None
) -> tuple[set[Claim], dict[str, int]]:
    """Return the answer key as distinct claims, plus the raw per-type row counts.

    Both are returned because they answer different questions and the difference
    between them is itself informative: ``duplicate_record`` collapses when
    deduplicated, so a family whose claim count is below its row count is one where
    a single record carries the same defect more than once.
    """
    import pandas as pd

    frames = [f for f in _frames(tenant, dataset, root) if not f.empty]  # type: ignore[attr-defined]
    if not frames:
        raise GroundTruthMissingError(
            f"no ground-truth manifest for {tenant}/{dataset}; it was either not "
            "generated or generated with defect_rate=0"
        )

    manifest = pd.concat(frames, ignore_index=True)
    row_counts = {
        str(k): int(v) for k, v in manifest["defect_type"].value_counts().items()
    }

    usable = manifest[
        ~manifest["defect_type"].isin(NON_DEFECT_TYPES)
        & manifest["field"].notna()
        & (manifest["field"].astype(str) != "")
    ]

    claims = {
        Claim(
            record_key=str(row.record_key),
            field=str(row.field),
            family=str(row.defect_type),
        )
        for row in usable.itertuples(index=False)
    }

    logger.info(
        "ground_truth_loaded",
        tenant=tenant,
        dataset=dataset,
        manifest_rows=len(manifest),
        scoreable_rows=len(usable),
        distinct_claims=len(claims),
        excluded=sorted(NON_DEFECT_TYPES & set(row_counts)),
    )
    return claims, row_counts
