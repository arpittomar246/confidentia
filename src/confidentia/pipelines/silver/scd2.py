"""Type-2 slowly changing dimensions, built from the CDC change stream.

A Type-2 dimension answers "what did this record look like *at the time*". A claim
adjudicated in March under one insurance plan cannot be explained by a dimension
that only knows today's plan.

Two things make this correct rather than approximate:

**Ordering by LSN, not by timestamp.** Two changes committed in the same
millisecond have a well-defined order in the write-ahead log and no well-defined
order by timestamp. Sorting by ``_cdc_commit_ts`` would occasionally invert them
and produce a dimension whose history is subtly wrong. Postgres LSNs are
``hex/hex`` and must be converted to a number first — sorting them as strings puts
``0/A`` after ``0/10``.

**Suppressing no-op changes using the before-image.** An UPDATE that touched only
``updated_at`` must not create a new version, or the dimension grows without
carrying information and every point-in-time join gets slower for nothing. The
before-image captured via ``REPLICA IDENTITY FULL`` (Stage 3) is what makes this
detectable, and it also lets each version record *which* attributes moved.

**This is a transaction-time dimension, not a valid-time one.** ``valid_from`` is
the WAL commit timestamp — when the platform *learned* of the change — not when the
change became true in the business. Log-based CDC cannot supply the latter: the WAL
records when a row was written, and a source system that back-dates a correction
looks identical to one making it live. Answering "which plan was in force when this
claim was adjudicated in March" needs the source to carry an effective date, which
this one does not. A bi-temporal dimension (both axes) is the correct target and is
noted as future work rather than silently approximated here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from confidentia.common.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import Column, DataFrame

logger = get_logger(__name__)

# Open-ended versions use a sentinel rather than NULL so that a point-in-time
# predicate is a plain BETWEEN and needs no OR IS NULL branch -- which would defeat
# predicate pushdown on the partition column.
FAR_FUTURE = "9999-12-31 23:59:59"

SCD2_COLUMNS = (
    "valid_from", "valid_to", "is_current", "version_number",
    "change_reason", "changed_attributes", "_scd_hash",
)


@dataclass(frozen=True)
class Scd2Spec:
    """Which attributes are versioned, and which merely ride along."""

    natural_key: str
    tracked_attributes: tuple[str, ...]
    # Columns that change constantly and carry no business meaning. Versioning on
    # these would create a new row for every touch.
    ignored_attributes: tuple[str, ...] = ("updated_at", "_ingested_at", "_record_ordinal")


PATIENT_SCD2 = Scd2Spec(
    natural_key="patient_id",
    tracked_attributes=(
        "first_name", "last_name", "email", "phone",
        "address_line1", "city", "state", "postal_code",
        "plan_id", "primary_care_provider_id",
    ),
)


def lsn_to_numeric(column: Column) -> Column:
    """Convert a Postgres ``hex/hex`` LSN into a sortable number.

    String ordering is wrong: ``0/A`` sorts after ``0/10`` but happened earlier.
    """
    from pyspark.sql import functions as F

    high = F.conv(F.split(column, "/").getItem(0), 16, 10).cast("decimal(38,0)")
    low = F.conv(F.split(column, "/").getItem(1), 16, 10).cast("decimal(38,0)")
    return high * F.lit(4294967296).cast("decimal(38,0)") + low


def build_scd2(changes: DataFrame, spec: Scd2Spec) -> DataFrame:
    """Turn an ordered change stream into a versioned dimension."""
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    tracked = [a for a in spec.tracked_attributes if a in changes.columns]
    if not tracked:
        raise ValueError(f"none of the tracked attributes exist: {spec.tracked_attributes}")

    frame = changes
    if "_cdc_lsn" in frame.columns:
        frame = frame.withColumn("_lsn_num", lsn_to_numeric(F.col("_cdc_lsn")))
        order_column = F.col("_lsn_num")
    else:
        # Snapshot source with no WAL position; fall back to ingest order.
        frame = frame.withColumn("_lsn_num", F.lit(None).cast("decimal(38,0)"))
        order_column = F.col("_ingested_at")

    # Hash over tracked attributes only. Two consecutive events with the same hash
    # differ only in ignored columns and must not create a version.
    frame = frame.withColumn(
        "_scd_hash",
        F.sha2(
            F.concat_ws(
                "\x1f", *[F.coalesce(F.col(a).cast("string"), F.lit("\x00")) for a in tracked]
            ),
            256,
        ),
    )

    ordered = Window.partitionBy(spec.natural_key).orderBy(order_column)
    frame = frame.withColumn("_prev_hash", F.lag("_scd_hash").over(ordered))

    # Keep the first event for each key, plus every event that actually changed a
    # tracked attribute.
    versions = frame.filter(
        F.col("_prev_hash").isNull() | (F.col("_prev_hash") != F.col("_scd_hash"))
    )

    # Name the attributes that moved, by comparing against the before-image.
    #
    # Both sides must be normalised the same way. The current value has already
    # been through `conform` (trimmed, and lower-cased for categoricals) while the
    # before-image is raw from the WAL, so a naive comparison reports every
    # conformed column as changed -- a plan change appeared as `[state, plan_id]`
    # purely because `NY` had become `ny`.
    if "_cdc_old" in versions.columns:

        def normalised(column: Column) -> Column:
            return F.lower(F.trim(column.cast("string")))

        changed = F.array_compact(
            F.array(
                *[
                    F.when(
                        F.get_json_object(F.col("_cdc_old"), f"$.{a}").isNotNull()
                        & (
                            normalised(F.get_json_object(F.col("_cdc_old"), f"$.{a}"))
                            != normalised(F.col(a))
                        ),
                        F.lit(a),
                    )
                    for a in tracked
                ]
            )
        )
        versions = versions.withColumn("changed_attributes", changed)
    else:
        versions = versions.withColumn(
            "changed_attributes", F.array().cast("array<string>")
        )

    version_window = Window.partitionBy(spec.natural_key).orderBy(order_column)
    versions = (
        versions.withColumn("version_number", F.row_number().over(version_window))
        .withColumn(
            "valid_from",
            F.coalesce(
                F.col("_cdc_commit_ts").cast("timestamp"),
                F.col("_ingested_at").cast("timestamp"),
            ),
        )
        .withColumn(
            "change_reason",
            F.when(F.col("version_number") == 1, F.lit("initial_load")).otherwise(
                F.coalesce(F.col("_cdc_operation"), F.lit("UPDATE"))
            ),
        )
    )

    # valid_to is the instant the *next* version begins. Half-open intervals
    # [valid_from, valid_to) mean a point-in-time lookup never matches two versions,
    # which closed intervals would do at the boundary.
    versions = versions.withColumn(
        "valid_to",
        F.coalesce(
            F.lead("valid_from").over(version_window),
            F.lit(FAR_FUTURE).cast("timestamp"),
        ),
    ).withColumn(
        "is_current",
        F.lead("valid_from").over(version_window).isNull(),
    )

    # Surrogate key: stable across reruns because it is derived from the natural
    # key and the version, not from a monotonically increasing counter that would
    # differ every time the pipeline ran.
    versions = versions.withColumn(
        f"{spec.natural_key.replace('_id', '')}_sk",
        F.sha2(F.concat_ws("|", F.col(spec.natural_key), F.col("version_number")), 256).substr(1, 32),
    )

    return versions.drop("_prev_hash", "_lsn_num")


def point_in_time(dimension: DataFrame, spec: Scd2Spec, as_of: str) -> DataFrame:
    """The version of each record that was in force at ``as_of``."""
    from pyspark.sql import functions as F

    moment = F.lit(as_of).cast("timestamp")
    return dimension.filter((F.col("valid_from") <= moment) & (F.col("valid_to") > moment))


def validate_scd2(dimension: DataFrame, spec: Scd2Spec) -> dict[str, int]:
    """Structural checks a Type-2 dimension must satisfy.

    A dimension that violates these produces silently wrong point-in-time joins,
    which is far worse than failing loudly.
    """
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    key = spec.natural_key

    # Exactly one current version per key.
    current_counts = (
        dimension.filter(F.col("is_current"))
        .groupBy(key)
        .count()
        .filter(F.col("count") != 1)
        .count()
    )

    # No overlapping intervals: each version must start exactly where the previous
    # ended.
    window = Window.partitionBy(key).orderBy("valid_from")
    gaps = (
        dimension.withColumn("_prev_to", F.lag("valid_to").over(window))
        .filter(F.col("_prev_to").isNotNull() & (F.col("_prev_to") != F.col("valid_from")))
        .count()
    )

    inverted = dimension.filter(F.col("valid_to") <= F.col("valid_from")).count()

    return {
        "keys_without_exactly_one_current": current_counts,
        "interval_gaps_or_overlaps": gaps,
        "inverted_intervals": inverted,
    }
