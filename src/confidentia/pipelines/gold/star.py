"""Gold star schema: conformed dimensions and facts.

Dimensional rather than normalised, because the questions this layer answers are
"claims by facility by month" and "transactions by merchant category by hour", and
a star schema answers those with one join per attribute instead of a chain.

Three modelling decisions worth defending:

**Surrogate keys are hashes, not sequences.** A monotonically increasing key would
differ on every rerun, so a fact table built today could not be compared against one
built yesterday — which experiment E3 requires. Hash keys are stable functions of
the natural key.

**Facts store tokens, not identifiers.** `fact_encounter.patient_token` joins to
`dim_patient.patient_token`. The star works entirely on tokenised keys, so an
analyst can slice by patient without ever holding a patient identifier.

**`dim_date` is generated, not derived from the data.** A date dimension built from
observed dates has holes wherever nothing happened, and a query grouping by month
then silently omits empty months rather than showing zero.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from confidentia.common.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import Column, DataFrame, SparkSession

logger = get_logger(__name__)


def surrogate_key(*columns: Column) -> Column:
    """Stable 32-hex surrogate key derived from natural keys."""
    from pyspark.sql import functions as F

    return F.sha2(
        F.concat_ws("|", *[F.coalesce(c.cast("string"), F.lit("\x00")) for c in columns]), 256
    ).substr(1, 32)


# ------------------------------------------------------------------ dimensions


def build_dim_date(spark: SparkSession, start: str = "2025-01-01", end: str = "2027-12-31") -> DataFrame:
    """A complete calendar. Generated, so empty periods still appear."""
    from pyspark.sql import functions as F

    days = spark.sql(
        f"SELECT explode(sequence(to_date('{start}'), to_date('{end}'), interval 1 day)) AS date_value"
    )
    return (
        days.withColumn("date_sk", F.date_format("date_value", "yyyyMMdd").cast("int"))
        .withColumn("year", F.year("date_value"))
        .withColumn("quarter", F.quarter("date_value"))
        .withColumn("month", F.month("date_value"))
        .withColumn("month_name", F.date_format("date_value", "MMMM"))
        .withColumn("day_of_month", F.dayofmonth("date_value"))
        .withColumn("day_of_week", F.dayofweek("date_value"))
        .withColumn("day_name", F.date_format("date_value", "EEEE"))
        .withColumn("week_of_year", F.weekofyear("date_value"))
        .withColumn("is_weekend", F.dayofweek("date_value").isin(1, 7))
        .withColumn("year_month", F.date_format("date_value", "yyyy-MM"))
        .select(
            "date_sk", "date_value", "year", "quarter", "month", "month_name",
            "day_of_month", "day_of_week", "day_name", "week_of_year",
            "is_weekend", "year_month",
        )
    )


def build_dim_patient(silver_dim: DataFrame) -> DataFrame:
    """Project the SCD2 dimension into the star.

    Keeps every version: the whole point of SCD2 is that a fact joins to the version
    in force when it happened. Facts join on ``patient_token`` plus a validity
    predicate, not on the token alone.
    """
    from pyspark.sql import functions as F

    return silver_dim.select(
        F.col("patient_sk"),
        F.col("patient_id").alias("patient_token_source"),
        F.col("mrn").alias("patient_token"),
        F.col("city"),
        F.col("state"),
        F.col("plan_id"),
        F.col("primary_care_provider_id"),
        F.col("valid_from"),
        F.col("valid_to"),
        F.col("is_current"),
        F.col("version_number"),
        F.col("changed_attributes"),
    )


def build_dim_from_reference(
    frame: DataFrame, *, key: str, sk_name: str, columns: list[str]
) -> DataFrame:
    """Type-1 dimension from a reference table (facility, provider, merchant)."""
    from pyspark.sql import functions as F

    available = [c for c in columns if c in frame.columns]
    return (
        frame.dropDuplicates([key])
        .withColumn(sk_name, surrogate_key(F.col(key)))
        .select(sk_name, key, *available)
    )


def build_dim_diagnosis(encounters: DataFrame) -> DataFrame:
    """Diagnosis dimension, snowflaked to a body-system category.

    Snowflaked rather than flattened because the ICD-10 hierarchy is reused across
    facts and denormalising it would repeat the category on every encounter row.
    """
    from pyspark.sql import functions as F

    return (
        encounters.select(
            F.col("primary_diagnosis_code").alias("diagnosis_code"),
            F.col("diagnosis_system").alias("body_system"),
        )
        .where(F.col("diagnosis_code").isNotNull())
        .dropDuplicates(["diagnosis_code"])
        .withColumn("diagnosis_sk", surrogate_key(F.col("diagnosis_code")))
        .select("diagnosis_sk", "diagnosis_code", "body_system")
    )


# ---------------------------------------------------------------------- facts


def build_fact_encounter(encounters: DataFrame) -> DataFrame:
    """Grain: one clinical encounter."""
    from pyspark.sql import functions as F

    return (
        encounters.withColumn("encounter_sk", surrogate_key(F.col("encounter_id")))
        .withColumn("date_sk", F.date_format(F.col("event_ts"), "yyyyMMdd").cast("int"))
        .withColumn("patient_sk_ref", surrogate_key(F.col("patient_id")))
        .withColumn("provider_sk", surrogate_key(F.col("provider_id")))
        .withColumn("facility_sk", surrogate_key(F.col("facility_id")))
        .withColumn("diagnosis_sk", surrogate_key(F.col("primary_diagnosis_code")))
        .withColumn(
            "length_of_stay_minutes",
            (F.col("discharge_ts").cast("long") - F.col("admit_ts").cast("long")) / 60,
        )
        .select(
            "encounter_sk", "date_sk", "patient_sk_ref", "provider_sk", "facility_sk",
            "diagnosis_sk",
            F.col("encounter_id").alias("encounter_number"),  # degenerate dimension
            "encounter_type", "disposition", "triage_level",
            F.col("length_of_stay_minutes").cast("double"),
            F.col("is_late_arrival"),
            F.col("event_ts"),
            F.to_date("event_ts").alias("event_date"),
        )
    )


def build_fact_claim_line(claims: DataFrame) -> DataFrame:
    """Grain: one claim line item."""
    from pyspark.sql import functions as F

    return (
        claims.withColumn("claim_line_sk", surrogate_key(F.col("claim_line_id")))
        .withColumn("date_sk", F.date_format(F.col("event_ts"), "yyyyMMdd").cast("int"))
        .withColumn("encounter_sk", surrogate_key(F.col("encounter_id")))
        .select(
            "claim_line_sk", "date_sk", "encounter_sk",
            F.col("claim_number"),  # degenerate dimension
            "procedure_code", "units",
            F.col("billed_amount").cast("decimal(18,2)"),
            F.col("allowed_amount").cast("decimal(18,2)"),
            F.col("paid_amount").cast("decimal(18,2)"),
            F.col("patient_responsibility").cast("decimal(18,2)"),
            "is_denied", "denial_reason",
            F.col("event_ts"),
            F.to_date("event_ts").alias("event_date"),
        )
    )


def build_fact_transaction(transactions: DataFrame) -> DataFrame:
    """Grain: one card authorisation. Carries no fraud label by design."""
    from pyspark.sql import functions as F

    return (
        transactions.withColumn("transaction_sk", surrogate_key(F.col("transaction_id")))
        .withColumn("date_sk", F.date_format(F.col("event_ts"), "yyyyMMdd").cast("int"))
        .withColumn("account_sk", surrogate_key(F.col("account_id")))
        .withColumn("merchant_sk", surrogate_key(F.col("merchant_id")))
        .withColumn("event_hour", F.hour(F.col("event_ts")))
        .select(
            "transaction_sk", "date_sk", "account_sk", "merchant_sk",
            F.col("transaction_id").alias("transaction_number"),
            "mcc", "channel", "entry_mode", "auth_response", "currency",
            "terminal_country", "event_hour",
            F.col("amount").cast("decimal(18,2)"),
            F.col("is_late_arrival"),
            F.col("event_ts"),
            F.to_date("event_ts").alias("event_date"),
        )
    )


def attach_chargebacks(fact_transaction: DataFrame, chargebacks: DataFrame) -> DataFrame:
    """Left-join dispute outcomes onto the transaction fact.

    A left join, not an inner one: the overwhelming majority of transactions are
    never disputed, and an inner join would silently reduce the fact table to the
    disputed minority — a mistake that looks like a working pipeline.

    ``is_disputed`` is derived rather than assumed absent: a transaction with no
    chargeback row is not disputed *as far as is currently known*, which is a
    different statement from "is legitimate". The column name says `disputed`, not
    `fraud`, for that reason.
    """
    from pyspark.sql import functions as F

    disputes = chargebacks.select(
        F.col("transaction_id").alias("_cb_txn"),
        F.col("chargeback_id"),
        F.col("reason_code").alias("chargeback_reason"),
        F.col("filed_ts").alias("chargeback_filed_ts"),
        F.col("status").alias("chargeback_status"),
        F.col("is_confirmed_fraud"),
    ).dropDuplicates(["_cb_txn"])

    return (
        fact_transaction.join(
            disputes, fact_transaction["transaction_number"] == disputes["_cb_txn"], "left"
        )
        .withColumn("is_disputed", F.col("chargeback_id").isNotNull())
        .withColumn(
            "days_to_dispute",
            F.datediff(F.col("chargeback_filed_ts"), F.col("event_ts")),
        )
        .drop("_cb_txn")
    )
