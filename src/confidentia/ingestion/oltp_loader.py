"""Seed and mutate the source-of-record OLTP database.

This is the *source system*, not part of the platform. It exists so the CDC
collector has a real write-ahead log to replicate from: without genuine INSERTs
and UPDATEs landing in `ehr_oltp`, a replication slot would produce nothing and
the CDC path could only be asserted, never demonstrated.

``replay_mutations`` applies the ``patient_change`` script generated in Stage 2 —
address moves, plan changes, PCP switches, surname changes. Those UPDATEs are what
Silver's SCD2 logic will eventually have to version correctly.
"""

from __future__ import annotations

import io
import time
from pathlib import Path

import pandas as pd

from confidentia.common.logging import get_logger
from confidentia.config.settings import get_settings
from confidentia.store import db

logger = get_logger(__name__)

PATIENT_COLUMNS = (
    "patient_id",
    "mrn",
    "first_name",
    "last_name",
    "date_of_birth",
    "sex",
    "ssn",
    "email",
    "phone",
    "address_line1",
    "city",
    "state",
    "postal_code",
    "plan_id",
    "primary_care_provider_id",
    "created_at",
    "updated_at",
)


def _raw_root() -> Path:
    return get_settings().data_root / "raw"


def load_patients(*, tenant: str = "healthcare", truncate: bool = True) -> int:
    """Bulk-load the generated patient dimension into the OLTP database.

    Uses COPY rather than executemany. At 100k+ rows the difference is not
    marginal: COPY streams a single protocol message, while executemany pays a
    round trip per row. It also means the initial load produces one large WAL burst
    rather than 100k transactions, which is a more honest starting point for
    measuring CDC lag.
    """
    directory = _raw_root() / tenant / "patient"
    parts = sorted(directory.glob("part-*.parquet"))
    if not parts:
        raise FileNotFoundError(f"no generated patient data at {directory}")

    started = time.perf_counter()
    total = 0

    with db.connection("ehr_oltp") as conn, conn.cursor() as cur:
        if truncate:
            cur.execute("TRUNCATE TABLE patient")

        for part in parts:
            frame = pd.read_parquet(part)
            # Defect injection can null a column that the source system declares
            # NOT NULL. Those rows never existed in the real OLTP system; they are
            # a file-drop pathology, so they are excluded here rather than being
            # forced into a database that would reject them.
            frame = frame.dropna(subset=["patient_id", "mrn"])
            frame = frame.drop_duplicates(subset=["patient_id"], keep="first")
            frame = frame.reindex(columns=list(PATIENT_COLUMNS))

            buffer = io.StringIO()
            frame.to_csv(buffer, index=False, header=False, na_rep="\\N")
            buffer.seek(0)

            columns = ", ".join(PATIENT_COLUMNS)
            with cur.copy(f"COPY patient ({columns}) FROM STDIN WITH (FORMAT csv, NULL '\\N')") as copy:
                copy.write(buffer.read())
            total += len(frame)

    elapsed = time.perf_counter() - started
    logger.info(
        "oltp_patients_loaded",
        rows=total,
        seconds=round(elapsed, 2),
        rows_per_second=round(total / max(elapsed, 1e-9)),
    )
    return total


def replay_mutations(*, tenant: str = "healthcare", limit: int | None = None) -> int:
    """Apply the generated mutation script as real UPDATEs.

    Each becomes a WAL record the CDC collector will observe. Applied in change
    order so the resulting change stream is causally sensible.
    """
    directory = _raw_root() / tenant / "patient_change"
    parts = sorted(directory.glob("part-*.parquet"))
    if not parts:
        raise FileNotFoundError(f"no generated mutations at {directory}")

    changes = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    changes = changes.sort_values("change_ts")
    if limit:
        changes = changes.head(limit)

    started = time.perf_counter()
    applied = 0

    # Only these columns may be updated by the replayer. The changed_field value
    # comes from generated data, and interpolating it into SQL unchecked would be a
    # straightforward injection vector even against synthetic input.
    allowed = {"address_line1", "plan_id", "primary_care_provider_id", "last_name", "phone"}

    with db.connection("ehr_oltp") as conn, conn.cursor() as cur:
        for field in changes["changed_field"].unique():
            if field not in allowed:
                logger.warning("skipping_unknown_mutation_field", field=field)
                continue
            subset = changes[changes["changed_field"] == field]
            if field == "address_line1":
                cur.executemany(
                    "UPDATE patient SET address_line1 = %s, city = %s, state = %s "
                    "WHERE patient_id = %s",
                    list(
                        zip(
                            subset["new_value"],
                            subset["new_city"],
                            subset["new_state"],
                            subset["patient_id"],
                            strict=True,
                        )
                    ),
                )
            else:
                cur.executemany(
                    f"UPDATE patient SET {field} = %s WHERE patient_id = %s",
                    list(zip(subset["new_value"], subset["patient_id"], strict=True)),
                )
            applied += len(subset)

    elapsed = time.perf_counter() - started
    logger.info(
        "oltp_mutations_replayed",
        applied=applied,
        seconds=round(elapsed, 2),
    )
    return applied


def patient_count() -> int:
    row = db.fetch_one("SELECT count(*) AS n FROM patient", database="ehr_oltp")
    return int(row["n"]) if row else 0
