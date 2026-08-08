"""Key Broker Service — attestation-gated key release (contribution C2).

No component gets key material by asking. It must present attestation evidence
answering a nonce the broker issued, and the measurement in that evidence must
appear in an approved policy for the tenant it wants keys for.

The operational consequence is the interesting part, and it is deliberately
inconvenient: **change the pipeline code and it can no longer decrypt its own
data** until someone approves the new measurement. "Which code touched this data"
becomes enforceable rather than merely recorded.

Every decision is audited, including — especially including — the refusals. A gate
that has never refused anything is indistinguishable from no gate.

Under the ``sim`` backend this is a mechanism, not a security boundary: the process
measures itself and could lie. Both the policy rows and the audit rows carry
``hardware_rooted = false`` so that a simulated approval can never be mistaken for
a hardware-backed one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from confidentia.common.logging import get_logger
from confidentia.config.settings import get_settings
from confidentia.crypto.attestation import (
    AttestationBackend,
    AttestationError,
    AttestationEvidence,
    AttestationVerifier,
    issue_nonce,
    measure_code,
)
from confidentia.crypto.keys import KeyHierarchy
from confidentia.store import db

logger = get_logger(__name__)

WILDCARD_TENANT = "*"


class KeyReleaseDeniedError(RuntimeError):
    """The broker refused. Deliberately distinct from a crypto failure."""


@dataclass
class ReleasedKeys:
    tenant: str
    measurement: str
    material: dict[str, bytes]
    hardware_rooted: bool

    @property
    def key_ids(self) -> list[str]:
        return sorted(self.material)

    def __repr__(self) -> str:
        # Never let key material reach a log, a traceback or a REPL transcript.
        return (
            f"ReleasedKeys(tenant={self.tenant!r}, keys={len(self.material)}, "
            f"hardware_rooted={self.hardware_rooted})"
        )


class KeyBroker:
    def __init__(self, hierarchy: KeyHierarchy | None = None) -> None:
        self.settings = get_settings()
        self.hierarchy = hierarchy or KeyHierarchy()
        self.verifier = AttestationVerifier()

    # ---------------------------------------------------------------- policy

    def approve(
        self,
        measurement: str,
        *,
        description: str,
        tenants: list[str],
        approved_by: str,
        notes: str | None = None,
    ) -> None:
        db.execute(
            """
            INSERT INTO platform.attestation_policy
                (measurement, description, tenants, approved_by, tee_backend,
                 hardware_rooted, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (measurement) DO UPDATE
                SET description = EXCLUDED.description,
                    tenants     = EXCLUDED.tenants,
                    approved_by = EXCLUDED.approved_by,
                    approved_at = now(),
                    revoked_at  = NULL,
                    notes       = EXCLUDED.notes
            """,
            (
                measurement,
                description,
                tenants,
                approved_by,
                self.settings.tee.backend.value,
                self.settings.tee.is_measured,
                notes,
            ),
        )
        logger.info(
            "measurement_approved",
            measurement=measurement[:16],
            tenants=tenants,
            hardware_rooted=self.settings.tee.is_measured,
        )

    def revoke(self, measurement: str) -> bool:
        return (
            db.execute(
                """
                UPDATE platform.attestation_policy
                   SET revoked_at = now()
                 WHERE measurement = %s AND revoked_at IS NULL
                """,
                (measurement,),
            )
            > 0
        )

    def approve_current_code(self, *, approved_by: str = "bootstrap") -> str:
        """Approve whatever is running now.

        A bootstrap convenience: without it the first run of a fresh checkout
        cannot obtain keys at all. In a real deployment approval is a human
        decision made against a reviewed build, which is why the approver is
        recorded rather than assumed.
        """
        measurement = measure_code()
        self.approve(
            measurement,
            description="current working tree",
            tenants=[WILDCARD_TENANT],
            approved_by=approved_by,
            notes="self-approved from the running code; not a reviewed build",
        )
        return measurement

    def policy_for(self, measurement: str) -> dict | None:
        return db.fetch_one(
            """
            SELECT measurement, tenants, description, approved_by, approved_at, hardware_rooted
              FROM platform.attestation_policy
             WHERE measurement = %s AND revoked_at IS NULL
            """,
            (measurement,),
        )

    # --------------------------------------------------------------- release

    def _audit(
        self,
        *,
        measurement: str,
        tenant: str,
        granted: bool,
        reason: str,
        key_ids: list[str],
        nonce: str | None,
        evidence_digest: str | None,
        run_id: uuid.UUID | None,
        hardware_rooted: bool,
    ) -> None:
        db.execute(
            """
            INSERT INTO platform.key_release_audit
                (measurement, tenant, key_ids, granted, reason, nonce,
                 evidence_digest, run_id, tee_backend, hardware_rooted)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                measurement,
                tenant,
                key_ids,
                granted,
                reason,
                nonce,
                evidence_digest,
                run_id,
                self.settings.tee.backend.value,
                hardware_rooted,
            ),
        )

    def release(
        self,
        *,
        tenant: str,
        evidence: AttestationEvidence,
        expected_nonce: str,
        run_id: uuid.UUID | None = None,
        version: int = 1,
    ) -> ReleasedKeys:
        """Verify evidence against policy, then derive and return key material."""
        # 1. Is the evidence internally valid and fresh?
        try:
            self.verifier.verify(evidence, expected_nonce)
        except AttestationError as exc:
            self._audit(
                measurement=evidence.measurement,
                tenant=tenant,
                granted=False,
                reason=f"attestation_invalid: {exc}",
                key_ids=[],
                nonce=expected_nonce,
                evidence_digest=evidence.digest(),
                run_id=run_id,
                hardware_rooted=evidence.hardware_rooted,
            )
            logger.warning("key_release_denied", tenant=tenant, reason=str(exc))
            raise KeyReleaseDeniedError(f"attestation rejected: {exc}") from exc

        # 2. Is this measurement approved at all?
        policy = self.policy_for(evidence.measurement)
        if policy is None:
            reason = "measurement_not_approved"
            self._audit(
                measurement=evidence.measurement,
                tenant=tenant,
                granted=False,
                reason=reason,
                key_ids=[],
                nonce=expected_nonce,
                evidence_digest=evidence.digest(),
                run_id=run_id,
                hardware_rooted=evidence.hardware_rooted,
            )
            logger.warning(
                "key_release_denied",
                tenant=tenant,
                measurement=evidence.measurement[:16],
                reason=reason,
            )
            raise KeyReleaseDeniedError(
                f"measurement {evidence.measurement[:16]}... is not approved. "
                "The pipeline code changed; approve the new measurement or restore "
                "the approved build."
            )

        # 3. Approved for *this* tenant? Cross-tenant key access is the failure
        #    mode that multi-tenant isolation exists to prevent.
        allowed = set(policy["tenants"] or [])
        if WILDCARD_TENANT not in allowed and tenant not in allowed:
            reason = "tenant_not_permitted"
            self._audit(
                measurement=evidence.measurement,
                tenant=tenant,
                granted=False,
                reason=reason,
                key_ids=[],
                nonce=expected_nonce,
                evidence_digest=evidence.digest(),
                run_id=run_id,
                hardware_rooted=evidence.hardware_rooted,
            )
            raise KeyReleaseDeniedError(
                f"measurement is approved, but not for tenant {tenant!r} "
                f"(permitted: {sorted(allowed)})"
            )

        # 4. Only now derive key material.
        material = self.hierarchy.key_material(tenant, version)
        self._audit(
            measurement=evidence.measurement,
            tenant=tenant,
            granted=True,
            reason="approved",
            key_ids=sorted(material),
            nonce=expected_nonce,
            evidence_digest=evidence.digest(),
            run_id=run_id,
            hardware_rooted=evidence.hardware_rooted,
        )
        logger.info(
            "keys_released",
            tenant=tenant,
            measurement=evidence.measurement[:16],
            key_count=len(material),
            hardware_rooted=evidence.hardware_rooted,
        )
        return ReleasedKeys(
            tenant=tenant,
            measurement=evidence.measurement,
            material=material,
            hardware_rooted=evidence.hardware_rooted,
        )


def request_keys(tenant: str, *, run_id: uuid.UUID | None = None) -> ReleasedKeys:
    """The full handshake, as a pipeline would perform it.

    The nonce is issued by the broker side and echoed by the prover, so evidence
    captured from an earlier run cannot be replayed.
    """
    broker = KeyBroker()
    nonce = issue_nonce()
    evidence = AttestationBackend().attest(nonce, claims={"tenant": tenant})
    if not broker.settings.tee.require_attestation:
        logger.warning("attestation_not_required", tenant=tenant)
    return broker.release(
        tenant=tenant, evidence=evidence, expected_nonce=nonce, run_id=run_id
    )
