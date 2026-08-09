"""KPI marts: the aggregates that leave the confidentiality boundary.

Every mart declares its own disclosure policy alongside its aggregation, so the two
cannot drift apart. A mart added without a policy fails at definition time rather
than publishing unprotected counts.

The contributor column is always a **distinct entity count**, never a row count.
One patient with forty encounters is one person; a cell containing only that patient
identifies them regardless of how many rows it aggregates.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from confidentia.common.logging import get_logger
from confidentia.pipelines.gold.disclosure import DisclosurePolicy

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import DataFrame

logger = get_logger(__name__)


@dataclass(frozen=True)
class MartSpec:
    name: str
    tenant: str
    description: str
    build: Callable[..., DataFrame]
    policy: DisclosurePolicy
    # Marts partitioned by event date support targeted restatement; those without
    # are recomputed whole (they are small).
    partition_column: str | None = "event_date"
    count_columns: tuple[str, ...] = ()


# ------------------------------------------------------------------ healthcare


def encounters_daily(fact_encounter: DataFrame, dim_facility: DataFrame) -> DataFrame:
    from pyspark.sql import functions as F

    return (
        fact_encounter.join(dim_facility, "facility_sk", "left")
        .groupBy("event_date", "facility_id", "city", "state", "encounter_type")
        .agg(
            F.count(F.lit(1)).alias("encounter_count"),
            F.countDistinct("patient_sk_ref").alias("distinct_entities"),
            F.avg("length_of_stay_minutes").alias("avg_length_of_stay_minutes"),
            F.sum(F.col("is_late_arrival").cast("int")).alias("late_arrivals"),
        )
    )


def diagnosis_prevalence(fact_encounter: DataFrame, dim_diagnosis: DataFrame) -> DataFrame:
    """Diagnosis counts by body system and state.

    The mart most likely to disclose: a rare diagnosis in a small state can easily
    involve fewer than five patients, which is exactly the cell suppression exists
    for.
    """
    from pyspark.sql import functions as F

    return (
        fact_encounter.join(dim_diagnosis, "diagnosis_sk", "left")
        .groupBy("body_system", "diagnosis_code", "encounter_type")
        .agg(
            F.count(F.lit(1)).alias("encounter_count"),
            F.countDistinct("patient_sk_ref").alias("distinct_entities"),
        )
    )


def claims_monthly(fact_claim_line: DataFrame, dim_date: DataFrame) -> DataFrame:
    from pyspark.sql import functions as F

    return (
        fact_claim_line.join(dim_date, "date_sk", "left")
        .groupBy("year_month", "procedure_code")
        .agg(
            F.count(F.lit(1)).alias("line_count"),
            F.countDistinct("encounter_sk").alias("distinct_entities"),
            F.sum("billed_amount").alias("total_billed"),
            F.sum("allowed_amount").alias("total_allowed"),
            F.sum("paid_amount").alias("total_paid"),
            F.avg(F.col("is_denied").cast("int")).alias("denial_rate"),
        )
    )


# ------------------------------------------------------------------- financial


def transactions_daily(fact_transaction: DataFrame, dim_merchant: DataFrame) -> DataFrame:
    from pyspark.sql import functions as F

    return (
        fact_transaction.join(dim_merchant, "merchant_sk", "left")
        .groupBy("event_date", "mcc_category", "channel")
        .agg(
            F.count(F.lit(1)).alias("transaction_count"),
            F.countDistinct("account_sk").alias("distinct_entities"),
            F.sum("amount").alias("total_amount"),
            F.avg("amount").alias("avg_amount"),
            F.sum(F.col("is_disputed").cast("int")).alias("disputed_count"),
        )
    )


def merchant_risk(fact_transaction: DataFrame, dim_merchant: DataFrame) -> DataFrame:
    """Dispute rate by merchant category and country.

    Deliberately aggregated to category rather than to individual merchant: a
    per-merchant dispute rate over a handful of transactions is both statistically
    meaningless and commercially damaging.
    """
    from pyspark.sql import functions as F

    return (
        fact_transaction.join(dim_merchant, "merchant_sk", "left")
        .groupBy("mcc_category", "risk_tier", "terminal_country")
        .agg(
            F.count(F.lit(1)).alias("transaction_count"),
            F.countDistinct("account_sk").alias("distinct_entities"),
            F.sum(F.col("is_disputed").cast("int")).alias("disputed_count"),
            F.avg(F.col("is_disputed").cast("int")).alias("dispute_rate"),
            F.avg("days_to_dispute").alias("avg_days_to_dispute"),
        )
    )


def fraud_by_hour(fact_transaction: DataFrame) -> DataFrame:
    from pyspark.sql import functions as F

    return (
        fact_transaction.groupBy("event_hour", "channel")
        .agg(
            F.count(F.lit(1)).alias("transaction_count"),
            F.countDistinct("account_sk").alias("distinct_entities"),
            F.sum(F.col("is_disputed").cast("int")).alias("disputed_count"),
            F.avg(F.col("is_disputed").cast("int")).alias("dispute_rate"),
        )
    )


MARTS: tuple[MartSpec, ...] = (
    MartSpec(
        name="mart_encounters_daily",
        tenant="healthcare",
        description="Encounters by day, facility and type.",
        build=encounters_daily,
        count_columns=("encounter_count",),
        policy=DisclosurePolicy.from_settings(
            measure_columns=("encounter_count", "avg_length_of_stay_minutes", "late_arrivals"),
            row_group_columns=("event_date", "facility_id"),
        ),
    ),
    MartSpec(
        name="mart_diagnosis_prevalence",
        tenant="healthcare",
        description="Diagnosis prevalence by body system.",
        build=diagnosis_prevalence,
        partition_column=None,
        count_columns=("encounter_count",),
        policy=DisclosurePolicy.from_settings(
            measure_columns=("encounter_count",),
            row_group_columns=("body_system", "diagnosis_code"),
        ),
    ),
    MartSpec(
        name="mart_claims_monthly",
        tenant="healthcare",
        description="Claim value and denial rate by month and procedure.",
        build=claims_monthly,
        partition_column=None,
        count_columns=("line_count",),
        policy=DisclosurePolicy.from_settings(
            measure_columns=(
                "line_count", "total_billed", "total_allowed", "total_paid", "denial_rate",
            ),
            row_group_columns=("year_month",),
        ),
    ),
    MartSpec(
        name="mart_transactions_daily",
        tenant="financial",
        description="Transaction volume and value by day, category and channel.",
        build=transactions_daily,
        count_columns=("transaction_count",),
        policy=DisclosurePolicy.from_settings(
            measure_columns=(
                "transaction_count", "total_amount", "avg_amount", "disputed_count",
            ),
            row_group_columns=("event_date", "mcc_category"),
        ),
    ),
    MartSpec(
        name="mart_merchant_risk",
        tenant="financial",
        description="Dispute rate by merchant category, risk tier and country.",
        build=merchant_risk,
        partition_column=None,
        count_columns=("transaction_count", "disputed_count"),
        policy=DisclosurePolicy.from_settings(
            measure_columns=(
                "transaction_count", "disputed_count", "dispute_rate", "avg_days_to_dispute",
            ),
            row_group_columns=("mcc_category",),
        ),
    ),
    MartSpec(
        name="mart_fraud_by_hour",
        tenant="financial",
        description="Dispute rate by hour of day and channel.",
        build=fraud_by_hour,
        partition_column=None,
        count_columns=("transaction_count", "disputed_count"),
        policy=DisclosurePolicy.from_settings(
            measure_columns=("transaction_count", "disputed_count", "dispute_rate"),
            row_group_columns=("event_hour",),
        ),
    ),
)


def marts_for(tenant: str) -> list[MartSpec]:
    return [m for m in MARTS if m.tenant == tenant]


def get_mart(name: str) -> MartSpec:
    for mart in MARTS:
        if mart.name == name:
            return mart
    raise KeyError(f"unknown mart {name!r}; known: {[m.name for m in MARTS]}")
