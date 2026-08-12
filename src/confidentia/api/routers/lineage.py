"""The signed, Merkle-chained provenance record (C3).

**What is exposed and what is not.** ``verify(deep=False)`` reads the chain from
Postgres and re-derives every link and signature in process -- it touches no lake
data, needs no JVM, and on this chain completes in milliseconds. It is exposed.

``verify(deep=True)`` re-reads each dataset from the encrypted lake and recomputes
its Merkle root. It needs a SparkSession, key release through attestation, and
minutes of wall clock. It is **not** exposed, and neither is ``prove_record``, for
the same reason: an HTTP handler that holds a Spark session for four minutes ties
up a worker, and the timeout that eventually kills the request leaves the session
behind. ADR 0016 records the decision; when those become available over HTTP they
belong behind a job submission and a polled result, not a blocking GET.

The verification endpoint calls ``confidentia.lineage.verify`` rather than
re-implementing the link check in SQL. A second implementation of "is this chain
intact" is a second thing that can be wrong, and the one in the API would be the
one nobody tested against a real tamper.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path

from confidentia.api.deps import (
    DatasetQuery,
    LayerQuery,
    PageDep,
    TenantQuery,
    fetch_all,
    fetch_one,
    optional_filters,
    paginated,
)
from confidentia.api.errors import NotFoundError
from confidentia.api.security import AuditorDep, OperatorDep, ViewerDep

router = APIRouter(prefix="/lineage", tags=["lineage"])


@router.get("/head", summary="The tip of the chain", dependencies=[ViewerDep])
def head() -> dict[str, Any]:
    """The head digest commits to every entry before it.

    An external party who records this value can later detect any edit to any
    earlier entry -- which is the entire point of chaining, and the reason it is
    viewer-readable while the payloads are not.
    """
    row = fetch_one("SELECT seq, entry_digest, tenant, dataset, layer, recorded_at "
                    "FROM platform.lineage_head")
    total = fetch_one("SELECT count(*) AS n FROM platform.lineage_entry")
    return {"head": row, "entries": int(total["n"]) if total else 0}


@router.get("/latest", summary="Latest entry per dataset", dependencies=[ViewerDep])
def latest(tenant: TenantQuery = None, layer: LayerQuery = None) -> dict[str, Any]:
    where, params = optional_filters({"tenant": tenant, "layer": layer})
    return {
        "items": fetch_all(
            f"""
            SELECT seq, tenant, dataset, layer, transformation, record_count,
                   merkle_root, code_measurement, hardware_rooted, entry_digest,
                   run_id, recorded_at
              FROM platform.lineage_latest WHERE 1=1 {where}
             ORDER BY recorded_at DESC
            """,
            params,
        )
    }


@router.get("/entries", summary="Chain entries without payloads", dependencies=[OperatorDep])
def entries(
    page: PageDep,
    tenant: TenantQuery = None,
    dataset: DatasetQuery = None,
    layer: LayerQuery = None,
) -> dict[str, Any]:
    """``payload``, ``signature`` and ``public_key_pem`` are omitted here.

    The payload names source paths, transformation parameters and record counts for
    a tenant's data. Reading it is an audit action, so it is behind the auditor role
    on the single-entry endpoint rather than returned fifty at a time by a list.
    """
    where, params = optional_filters({"tenant": tenant, "dataset": dataset, "layer": layer})
    rows = fetch_all(
        f"""
        SELECT seq, entry_digest, previous_digest, run_id, tenant, dataset, layer,
               transformation, record_count, merkle_root, code_measurement,
               hardware_rooted, recorded_at
          FROM platform.lineage_entry WHERE 1=1 {where}
         ORDER BY seq DESC LIMIT %s OFFSET %s
        """,
        [*params, page.limit, page.offset],
    )
    total = fetch_one(f"SELECT count(*) AS n FROM platform.lineage_entry WHERE 1=1 {where}", params)
    return paginated(rows, page, total=int(total["n"]) if total else None)


@router.get("/verify", summary="Re-derive every link and signature", dependencies=[OperatorDep])
def verify_chain_endpoint() -> dict[str, Any]:
    """Shallow verification: links, signatures, and column-versus-payload agreement.

    ``deep`` is absent by design, not by omission -- see the module docstring. The
    response says so explicitly so that a caller cannot read a passing result as
    proof the lake still matches its signed roots. It does not.
    """
    from confidentia.lineage import verify as lineage_verify

    report = lineage_verify.verify(deep=False)
    return {
        "ok": report.ok,
        "entries_checked": report.chain.entries_checked,
        "summary": report.summary(),
        "failures": [
            {"seq": f.seq, "index": f.index, "reason": f.reason, "detail": f.detail}
            for f in report.failures
        ],
        "scope": {
            "chain_links": True,
            "signatures": True,
            "columns_match_payload": True,
            # Named as false rather than left out: a client that cannot see this
            # field is a client that will assume it was checked.
            "lake_contents_rehashed": False,
            "note": (
                "Deep verification re-reads the encrypted lake and needs a Spark "
                "session; run `confidentia lineage verify --deep` for that."
            ),
        },
    }


@router.get(
    "/entries/{seq}",
    summary="One entry, with its signed payload and public key",
    dependencies=[AuditorDep],
)
def entry(seq: Annotated[int, Path(ge=1)]) -> dict[str, Any]:
    """Auditor-only, and returns the material needed to verify the entry offline.

    ``payload``, ``signature`` and ``public_key_pem`` together let a third party
    recompute the digest and check the signature without trusting this API -- which
    is what makes the lineage claim verifiable rather than merely asserted.
    """
    row = fetch_one(
        """
        SELECT seq, entry_digest, previous_digest, run_id, tenant, dataset, layer,
               transformation, record_count, merkle_root, code_measurement,
               hardware_rooted, payload, signature, public_key_pem, recorded_at
          FROM platform.lineage_entry WHERE seq = %s
        """,
        (seq,),
    )
    if row is None:
        raise NotFoundError(f"no lineage entry {seq}")
    return row
