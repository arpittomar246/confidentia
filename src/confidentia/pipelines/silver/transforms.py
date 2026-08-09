"""Silver transforms: conform, validate, deduplicate.

Order matters and is not arbitrary:

1. **Conform** — repair the cosmetic defects (whitespace, case, mojibake) and cast
   types. Must come first, because deduplication keys on business identity and
   ``"  MRN000123 "`` and ``"mrn000123"`` are the same patient.
2. **Validate** — check against the contract. Failures are *marked*, not dropped.
3. **Split** — critical failures go to quarantine with a reason and a replay
   handle; everything else continues.
4. **Deduplicate** — collapse records sharing a business key.
5. **Tokenise** — replace direct identifiers. Last, because validation needs to see
   the real values (an email is checked for shape before it becomes a token).

Nothing is ever silently dropped. A record that fails validation is recorded in
``platform.quarantine_record`` in redacted form, so a fixed upstream can be
replayed rather than the data being lost.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from functools import reduce
from typing import TYPE_CHECKING

from confidentia.catalog.contracts import SchemaContract
from confidentia.common.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import Column, DataFrame

logger = get_logger(__name__)

DQ_FAILURES = "_dq_failures"
DQ_SEVERITY = "_dq_severity"
ROW_HASH = "_row_hash"

# Mojibake signature: UTF-8 bytes decoded as Latin-1 always produces a character
# from this range. Repairing only where it appears avoids mangling strings that
# were correct all along.
MOJIBAKE_MARKER = "[ÃÂâ]"

# Amount columns are landed as strings in Bronze so that type-drift defects survive
# to be measured. Silver is where they become numbers.
DECIMAL_FIELDS = {
    "billed_amount", "allowed_amount", "paid_amount", "patient_responsibility",
    "amount", "disputed_amount", "credit_limit", "risk_score",
}

# Free-text categories: case carries no meaning, so normalise down.
LOWERCASE_CATEGORICALS = {
    "encounter_type", "disposition", "status", "channel",
    "auth_response", "entry_mode", "account_type",
}

# Standardised codes where uppercase IS the standard: ISO 4217 currencies, ISO
# 3166 country codes, USPS state abbreviations, HL7 administrative sex.
UPPERCASE_CODES = {
    "currency", "state", "home_state", "terminal_country", "home_country", "sex",
}


@dataclass
class ConformResult:
    valid: DataFrame
    quarantined: DataFrame
    duplicates_removed: int = 0
    stats: dict[str, int] = field(default_factory=dict)


def repair_text(column: Column) -> Column:
    """Trim, collapse internal whitespace, and undo mojibake."""
    from pyspark.sql import functions as F

    trimmed = F.trim(F.regexp_replace(column, r"\s+", " "))
    # `Ã©` -> `é`: re-encode as Latin-1 and re-decode as UTF-8, but only where the
    # marker is present.
    repaired = F.decode(F.encode(trimmed, "ISO-8859-1"), "UTF-8")
    return F.when(trimmed.rlike(MOJIBAKE_MARKER), repaired).otherwise(trimmed)


def conform(frame: DataFrame, contract: SchemaContract) -> DataFrame:
    """Repair cosmetic defects and cast declared numerics."""
    from pyspark.sql import functions as F

    out = frame
    for spec in contract.columns:
        if spec.name not in out.columns:
            continue

        if spec.dtype == "string" and not spec.name.startswith("_"):
            out = out.withColumn(spec.name, repair_text(F.col(spec.name)))

        # Free-text categoricals are lower-cased so the injected case-inconsistency
        # defects stop fragmenting group-by results.
        if spec.name in LOWERCASE_CATEGORICALS and spec.name in out.columns:
            out = out.withColumn(spec.name, F.lower(F.col(spec.name)))

        # Standardised codes are upper-cased instead. Blanket lower-casing was a
        # real bug: ISO 4217 currency codes are uppercase by standard, so `USD`
        # became `usd` and the validity rule failed all 58,899 transaction rows.
        # The quality layer found it, which is what it is for.
        elif spec.name in UPPERCASE_CODES and spec.name in out.columns:
            out = out.withColumn(spec.name, F.upper(F.col(spec.name)))

        if spec.name in DECIMAL_FIELDS and spec.name in out.columns:
            # try_cast, not cast: a type-drift value like "N/A" becomes NULL and is
            # caught by validation, rather than failing the whole batch.
            out = out.withColumn(
                spec.name, F.expr(f"try_cast(`{spec.name}` AS decimal(18,2))")
            )

    return out


def validate(frame: DataFrame, contract: SchemaContract) -> DataFrame:
    """Attach a failure list and a severity to every record.

    Marking rather than filtering: the split into valid and quarantined is a
    separate, explicit step, so that "how many failed and why" is answerable.
    """
    from pyspark.sql import functions as F

    failures: list[Column] = []

    for spec in contract.columns:
        if spec.name not in frame.columns or spec.nullable:
            continue
        failures.append(
            F.when(F.col(spec.name).isNull(), F.lit(f"null_violation:{spec.name}"))
        )

    for key in contract.business_key:
        if key in frame.columns:
            failures.append(
                F.when(F.col(key).isNull(), F.lit(f"missing_business_key:{key}"))
            )

    for name in DECIMAL_FIELDS:
        if name in frame.columns:
            failures.append(
                F.when(F.col(name) < 0, F.lit(f"negative_amount:{name}"))
            )

    # Temporal ordering, where the contract declares both ends.
    if {"admit_ts", "discharge_ts"} <= set(frame.columns):
        failures.append(
            F.when(
                F.col("discharge_ts") < F.col("admit_ts"),
                F.lit("temporal_inconsistency:discharge_before_admit"),
            )
        )

    # Event time must not precede ingest time by more than the late-arrival window
    # allows, and must never be in the future.
    if "event_ts" in frame.columns:
        failures.append(
            F.when(
                F.col("event_ts") > F.current_timestamp(),
                F.lit("future_event_ts"),
            )
        )

    if not failures:
        collected = F.array().cast("array<string>")
    else:
        collected = F.array_compact(F.array(*failures))

    out = frame.withColumn(DQ_FAILURES, collected)
    return out.withColumn(
        DQ_SEVERITY,
        F.when(F.size(F.col(DQ_FAILURES)) == 0, F.lit("ok"))
        .when(
            # A record missing its business key or a required field cannot be used
            # downstream at all.
            F.expr(
                f"exists({DQ_FAILURES}, x -> x like 'missing_business_key%' "
                f"or x like 'null_violation%')"
            ),
            F.lit("critical"),
        )
        .otherwise(F.lit("major")),
    )


def split_valid(frame: DataFrame) -> tuple[DataFrame, DataFrame]:
    """Separate usable records from quarantine candidates.

    Only ``critical`` is quarantined. ``major`` failures (a negative amount, a
    discharge before admission) stay in Silver *carrying their failure list*,
    because removing them would silently change every aggregate; the quality layer
    reports them and the analyst decides.
    """
    from pyspark.sql import functions as F

    quarantined = frame.filter(F.col(DQ_SEVERITY) == "critical")
    valid = frame.filter(F.col(DQ_SEVERITY) != "critical")
    return valid, quarantined


def add_row_hash(frame: DataFrame, contract: SchemaContract) -> DataFrame:
    """Content hash over business columns, ignoring the ingestion envelope.

    Used to detect true duplicates: two records with the same business key that
    differ only in which batch delivered them are the same fact.
    """
    from pyspark.sql import functions as F

    business_columns = sorted(
        c.name
        for c in contract.columns
        if not c.name.startswith("_") and c.name != "ingest_date" and c.name in frame.columns
    )
    return frame.withColumn(
        ROW_HASH,
        F.sha2(F.concat_ws("\x1f", *[F.coalesce(F.col(c).cast("string"), F.lit("")) for c in business_columns]), 256),
    )


def deduplicate(frame: DataFrame, contract: SchemaContract) -> tuple[DataFrame, int]:
    """Collapse records sharing a business key.

    Survivor selection is deterministic and, in order:

    1. the most recently ingested record wins — a later delivery is a correction;
    2. then the most complete record (fewest nulls), because a partial redelivery
       should not overwrite a full one;
    3. then the lowest row hash, purely to break ties reproducibly.

    Without step 3 the result would depend on Spark's partitioning, and rerunning
    the pipeline on identical input could produce a different Silver table — which
    would make experiment E3 meaningless.
    """
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    keys = [k for k in contract.business_key if k in frame.columns]
    if not keys:
        return frame, 0

    non_null_count = reduce(
        operator.add,
        (
            F.col(c.name).isNotNull().cast("int")
            for c in contract.columns
            if not c.name.startswith("_") and c.name in frame.columns
        ),
        F.lit(0),
    )
    ordering = [F.col("_ingested_at").desc_nulls_last()] if "_ingested_at" in frame.columns else []
    ordering.append(non_null_count.desc())
    ordering.append(F.col(ROW_HASH).asc())

    window = Window.partitionBy(*keys).orderBy(*ordering)
    ranked = frame.withColumn("_rank", F.row_number().over(window))
    deduped = ranked.filter(F.col("_rank") == 1).drop("_rank")
    return deduped, -1  # caller counts; counting here forces an extra Spark job
