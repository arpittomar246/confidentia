"""Disclosure control for published aggregates (contribution C4).

Gold is the layer that *leaves* the confidentiality boundary. Everything upstream
is protected by encryption and tokenisation; a published aggregate is protected by
nothing except what this module removes. A KPI card reading "3 patients with HIV in
postcode 10021" defeats the entire platform, however well the underlying Parquet was
encrypted.

Two controls, and the second is the one people forget:

**Primary suppression.** Any cell aggregating fewer than *k* contributing entities is
withheld. With k=5, a count of 1 that identifies a person never reaches a dashboard.

**Complementary suppression.** Suppressing one cell in a row while publishing the
row total makes the suppressed value recoverable by subtraction. If a row has
exactly one suppressed cell, a second cell must also be suppressed — chosen as the
smallest remaining, since that leaks the least. Primary suppression alone gives the
*appearance* of protection while providing none, which is worse than doing nothing
because it is trusted.

Neither control is differential privacy. Both are deterministic and therefore
vulnerable to differencing across repeated queries with slightly different filters:
an attacker who can ask for "encounters in March" and "encounters in March excluding
patient X" recovers X by subtraction. Defending against that requires a query budget
the platform does not have. `add_laplace_noise` provides a calibrated-noise option
for count columns, off by default, and its epsilon accounting is per-release, not
per-analyst — which is not the guarantee differential privacy actually promises.
This limitation is stated rather than glossed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from confidentia.common.logging import get_logger
from confidentia.config.settings import get_settings

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import DataFrame

logger = get_logger(__name__)

SUPPRESSED_FLAG = "_suppressed"
SUPPRESSION_REASON = "_suppression_reason"


@dataclass(frozen=True)
class DisclosurePolicy:
    """What a published dataset is allowed to reveal."""

    # Minimum distinct contributing entities per cell.
    min_cell_count: int = 5
    # The column counting contributing entities. Counting *rows* is wrong: one
    # patient with 40 encounters is one person, not 40, and a cell containing only
    # that patient identifies them however large the row count.
    contributor_column: str = "distinct_entities"
    # Measures blanked when a cell is suppressed.
    measure_columns: tuple[str, ...] = ()
    # Columns that define a row for complementary suppression.
    row_group_columns: tuple[str, ...] = ()
    apply_complementary: bool = True
    differential_privacy: bool = False
    epsilon: float = 1.0

    @classmethod
    def from_settings(cls, **overrides: Any) -> DisclosurePolicy:
        settings = get_settings().disclosure
        base = {
            "min_cell_count": settings.min_cell_count,
            "differential_privacy": settings.differential_privacy_enabled,
            "epsilon": settings.dp_epsilon,
        }
        base.update(overrides)
        return cls(**base)


@dataclass
class DisclosureReport:
    dataset: str
    total_cells: int = 0
    primary_suppressed: int = 0
    complementary_suppressed: int = 0
    noise_applied: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def suppressed(self) -> int:
        return self.primary_suppressed + self.complementary_suppressed

    @property
    def suppression_rate(self) -> float:
        return self.suppressed / self.total_cells if self.total_cells else 0.0

    def summary(self) -> str:
        return (
            f"{self.dataset}: {self.total_cells:,} cells, "
            f"{self.primary_suppressed:,} primary + "
            f"{self.complementary_suppressed:,} complementary suppressed "
            f"({self.suppression_rate:.2%})"
        )


def apply_primary_suppression(frame: DataFrame, policy: DisclosurePolicy) -> DataFrame:
    """Mark cells below the threshold and blank their measures."""
    from pyspark.sql import functions as F

    if policy.contributor_column not in frame.columns:
        raise ValueError(
            f"policy counts contributors in {policy.contributor_column!r}, which the "
            f"aggregate does not have. Counting rows instead would understate "
            "identifiability: one patient with 40 encounters is one person."
        )

    below = F.col(policy.contributor_column) < F.lit(policy.min_cell_count)
    out = frame.withColumn(SUPPRESSED_FLAG, below).withColumn(
        SUPPRESSION_REASON,
        F.when(below, F.lit(f"below_k_{policy.min_cell_count}")).otherwise(F.lit(None)),
    )

    for measure in policy.measure_columns:
        if measure in out.columns:
            out = out.withColumn(
                measure, F.when(F.col(SUPPRESSED_FLAG), F.lit(None)).otherwise(F.col(measure))
            )
    # The contributor count is itself a measure: publishing "4 patients, value
    # withheld" discloses the count that triggered suppression.
    out = out.withColumn(
        policy.contributor_column,
        F.when(F.col(SUPPRESSED_FLAG), F.lit(None)).otherwise(F.col(policy.contributor_column)),
    )
    return out


def apply_complementary_suppression(
    frame: DataFrame, policy: DisclosurePolicy
) -> DataFrame:
    """Suppress a second cell where exactly one is suppressed in a row.

    With one suppressed cell and a published row total, the missing value is
    recoverable by subtraction. A second suppression breaks that: the smallest
    remaining cell is chosen, because it is the one whose loss costs the least
    analytical value.
    """
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    if not policy.row_group_columns or not policy.apply_complementary:
        return frame

    group = Window.partitionBy(*policy.row_group_columns)
    suppressed_in_row = F.sum(F.col(SUPPRESSED_FLAG).cast("int")).over(group)
    cells_in_row = F.count(F.lit(1)).over(group)

    # Rank unsuppressed cells by contributor count so the smallest is picked.
    smallest_unsuppressed = F.row_number().over(
        Window.partitionBy(*policy.row_group_columns)
        .orderBy(F.col(policy.contributor_column).asc_nulls_last())
    )

    marked = (
        frame.withColumn("_suppressed_in_row", suppressed_in_row)
        .withColumn("_cells_in_row", cells_in_row)
        .withColumn("_size_rank", smallest_unsuppressed)
    )

    needs_complement = (
        (F.col("_suppressed_in_row") == 1)
        & (F.col("_cells_in_row") > 1)
        & (~F.col(SUPPRESSED_FLAG))
        & (F.col("_size_rank") == 1)
    )

    out = marked.withColumn(
        SUPPRESSION_REASON,
        F.when(needs_complement, F.lit("complementary")).otherwise(F.col(SUPPRESSION_REASON)),
    ).withColumn(SUPPRESSED_FLAG, F.col(SUPPRESSED_FLAG) | needs_complement)

    for measure in (*policy.measure_columns, policy.contributor_column):
        if measure in out.columns:
            out = out.withColumn(
                measure, F.when(F.col(SUPPRESSED_FLAG), F.lit(None)).otherwise(F.col(measure))
            )

    return out.drop("_suppressed_in_row", "_cells_in_row", "_size_rank")


def add_laplace_noise(
    frame: DataFrame, policy: DisclosurePolicy, count_columns: tuple[str, ...]
) -> DataFrame:
    """Add calibrated Laplace noise to count measures.

    Sensitivity is 1: one individual's presence changes any count by at most one.
    Scale is therefore 1/epsilon.

    **This is not differential privacy as usually promised.** The epsilon accounting
    here is per-release, not per-analyst across a session, and there is no query
    budget. It raises the cost of a differencing attack; it does not bound it.
    Off by default for that reason.
    """
    from pyspark.sql import functions as F

    scale = 1.0 / policy.epsilon
    out = frame
    for column in count_columns:
        if column not in out.columns:
            continue
        # Inverse-CDF sampling of Laplace from a uniform draw.
        uniform = F.rand() - F.lit(0.5)
        noise = -F.lit(scale) * F.signum(uniform) * F.log1p(-2 * F.abs(uniform))
        out = out.withColumn(
            column,
            F.when(F.col(column).isNull(), F.lit(None)).otherwise(
                F.greatest(F.round(F.col(column) + noise), F.lit(0)).cast("long")
            ),
        )
    logger.info("dp_noise_applied", columns=list(count_columns), epsilon=policy.epsilon)
    return out


def enforce(
    frame: DataFrame,
    policy: DisclosurePolicy,
    *,
    dataset: str,
    count_columns: tuple[str, ...] = (),
) -> tuple[DataFrame, DisclosureReport]:
    """Apply the full disclosure pipeline and report what was withheld."""
    from pyspark.sql import functions as F

    report = DisclosureReport(dataset=dataset)
    if not get_settings().disclosure.enabled:
        logger.warning("disclosure_control_disabled", dataset=dataset)
        return frame, report

    protected = apply_primary_suppression(frame, policy)
    protected.cache()
    report.total_cells = protected.count()
    report.primary_suppressed = protected.filter(F.col(SUPPRESSED_FLAG)).count()

    protected = apply_complementary_suppression(protected, policy)
    total_suppressed = protected.filter(F.col(SUPPRESSED_FLAG)).count()
    report.complementary_suppressed = total_suppressed - report.primary_suppressed

    if policy.differential_privacy and count_columns:
        protected = add_laplace_noise(protected, policy, count_columns)
        report.noise_applied = True

    logger.info(
        "disclosure_enforced",
        dataset=dataset,
        cells=report.total_cells,
        primary=report.primary_suppressed,
        complementary=report.complementary_suppressed,
    )
    return protected, report


def assert_no_small_cells(
    frame: DataFrame, policy: DisclosurePolicy, *, dataset: str
) -> None:
    """Fail if any publishable cell is below the threshold.

    Called immediately before anything leaves the boundary. Disclosure control that
    is applied but never verified is a comment, not a control.
    """
    from pyspark.sql import functions as F

    if policy.contributor_column not in frame.columns:
        return
    leaking = frame.filter(
        F.col(policy.contributor_column).isNotNull()
        & (F.col(policy.contributor_column) < policy.min_cell_count)
    ).count()
    if leaking:
        raise ValueError(
            f"{dataset}: {leaking} cell(s) below k={policy.min_cell_count} would be "
            "published. Refusing to release."
        )
