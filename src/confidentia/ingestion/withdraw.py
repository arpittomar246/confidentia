"""Withdrawing a dataset that should never have been landed (ADR 0023).

ADR 0022 closed the landing zone's plaintext hole and, in doing so, found
``healthcare/patient_change``: 900 rows the file collector landed once, belonging
to no schema contract, never promoted and never classified. It is a *source-side*
mutation script — ``oltp_loader`` replays it into Postgres so the CDC collector can
observe the resulting WAL records — and the lake already holds its effects, typed
and contracted, as ``patient_cdc``.

So it is deleted rather than given a contract. A contract would commit the platform
to a second path into Bronze for data that is already there by a better route.

**The ledger keeps the record.** Deleting the files while leaving
``platform.ingest_batch`` saying ``ingested`` would make the registry lie about what
is on disk; deleting the row instead would erase the fact that this arrived at all,
which is the one thing the landing zone exists to answer. So the row stays and gains
``withdrawn_at`` and ``withdrawn_reason``. "What did the source send us on Tuesday"
is still answerable: *this, and it was withdrawn on this date, for this reason.*

Written as a command rather than performed as a shell one-liner because it will be
needed again — uncontracted data reaching a landing zone is a recurring class of
mistake, not a one-off — and because an audited, repeatable withdrawal is worth more
than an ``rm -rf`` recorded only in a terminal scrollback.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from confidentia.catalog.contracts import CONTRACTS
from confidentia.common.logging import get_logger
from confidentia.config.settings import get_settings
from confidentia.store import db

logger = get_logger(__name__)


class WithdrawalRefusedError(RuntimeError):
    """The dataset is contracted, or there is nothing to withdraw."""


@dataclass
class WithdrawalPlan:
    tenant: str
    dataset: str
    directory: Path
    parts: list[Path]
    rows: int
    bytes_: int
    batches: list[dict]

    @property
    def batch_digests(self) -> list[str]:
        return [row["batch_digest"] for row in self.batches]


def plan_withdrawal(tenant: str, dataset: str, *, lake_root: Path | None = None) -> WithdrawalPlan:
    """Work out what withdrawing would remove, without removing anything.

    Refuses a contracted dataset outright. Deleting landing data the platform
    genuinely depends on is a different and far more serious operation than
    removing something that was never supposed to be there, and one command should
    not be able to do both.
    """
    if (tenant, dataset) in CONTRACTS:
        raise WithdrawalRefusedError(
            f"{tenant}/{dataset} has a schema contract, so it is a real lake dataset. "
            "This command only withdraws data that was never supposed to be landed. "
            "Removing a contracted dataset means re-ingesting or losing replay, and "
            "is not something to do by flag."
        )

    root = lake_root if lake_root is not None else get_settings().lake_root
    directory = root / "landing" / tenant / dataset
    if not directory.exists():
        raise WithdrawalRefusedError(f"nothing landed at {directory}")

    parts = sorted(p for p in directory.rglob("*.parquet") if p.is_file())
    batches = db.fetch_all(
        """
        SELECT batch_digest, row_count, bytes, source, first_seen_at
          FROM platform.ingest_batch
         WHERE tenant = %s AND dataset = %s
        """,
        (tenant, dataset),
    )
    return WithdrawalPlan(
        tenant=tenant,
        dataset=dataset,
        directory=directory,
        parts=parts,
        rows=sum(int(row["row_count"]) for row in batches),
        bytes_=sum(p.stat().st_size for p in parts),
        batches=[dict(row) for row in batches],
    )


def withdraw(plan: WithdrawalPlan, *, reason: str, withdrawn_by: str) -> WithdrawalPlan:
    """Remove the landing data and mark the ledger, in that order.

    Files first: if the registry update failed after the delete, the ledger would
    disagree with the disk and ``landing verify`` would still pass, which is
    recoverable by re-running. The other order can leave rows marked withdrawn
    beside data that is still there — a registry that under-reports what exists is
    the worse failure for a platform whose whole argument is that its checks match
    its bytes.
    """
    shutil.rmtree(plan.directory)

    marker = json.dumps(
        {
            "withdrawn_at": datetime.now(UTC).isoformat(),
            "withdrawn_by": withdrawn_by,
            "withdrawn_reason": reason,
        }
    )
    if plan.batch_digests:
        db.execute(
            """
            UPDATE platform.ingest_batch
               SET metadata = metadata || %s::jsonb
             WHERE batch_digest = ANY(%s)
            """,
            (marker, plan.batch_digests),
        )

    logger.info(
        "landing_dataset_withdrawn",
        tenant=plan.tenant,
        dataset=plan.dataset,
        parts=len(plan.parts),
        rows=plan.rows,
        batches=len(plan.batches),
        reason=reason,
    )
    return plan


def withdrawn_datasets() -> list[dict]:
    """Every batch the ledger records as withdrawn, for the audit trail."""
    return [
        dict(row)
        for row in db.fetch_all(
            """
            SELECT tenant, dataset, batch_digest, row_count,
                   metadata ->> 'withdrawn_at'     AS withdrawn_at,
                   metadata ->> 'withdrawn_by'     AS withdrawn_by,
                   metadata ->> 'withdrawn_reason' AS withdrawn_reason
              FROM platform.ingest_batch
             WHERE metadata ? 'withdrawn_at'
             ORDER BY metadata ->> 'withdrawn_at' DESC
            """
        )
    ]
