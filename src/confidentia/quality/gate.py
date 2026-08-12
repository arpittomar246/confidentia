"""The promotion gate: turn a quality score into a decision that stops something.

Stage 7a computed `promotion_blocked` and nothing consumed it. A score nobody acts
on is a dashboard, not a control, and the difference only shows up the day bad data
reaches Gold with a red number sitting beside it in a table.

Three properties matter more than the arithmetic, and each is here because the
obvious implementation gets it wrong:

**Blocking fails, it does not skip.** In Airflow a skipped task is green and the
downstream tasks run. A gate implemented as a skip would let Silver build from a
Bronze layer that had just been judged unfit, and the DAG would look healthy while
doing it. `evaluate` raises, the CLI exits non-zero, the task goes red.

**A missing score blocks.** The natural reading of "no score" is "nothing wrong
yet", which is exactly backwards: it means the quality run did not happen, so
nothing has been checked. A gate that opens when its input is absent protects
nothing precisely when the pipeline is already broken.

**An override is recorded, never silent.** Operators do need to push data through a
known-bad gate. That is a legitimate act and it must leave a row saying who decided
and why, because the alternative is that people learn to comment the gate out.

**The gate re-decides against the current threshold.** `dq_score.promotion_blocked`
is the scorer's verdict at the moment of scoring, which bakes a policy value into a
measurement: raising the threshold would have no effect until every dataset was
re-scored, and lowering it would leave stale blocks in place. The score is the
measurement, the threshold is policy, and they are compared here. Each decision
stores the threshold it used, so a historical block can still be interpreted after
the policy moves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from confidentia.common.logging import get_logger
from confidentia.config.settings import get_settings
from confidentia.store import db
from confidentia.store.repositories import RunType, track_run

logger = get_logger(__name__)


class PromotionBlockedError(RuntimeError):
    """Quality is below the promotion threshold, so downstream work must not run."""


@dataclass(frozen=True)
class GateDecision:
    tenant: str
    dataset: str
    layer: str
    blocked: bool
    decision: str
    threshold: float
    overall_score: float | None = None
    score_id: int | None = None
    reason: str | None = None
    overridden: bool = False

    def summary(self) -> str:
        score = f"{self.overall_score:.3f}" if self.overall_score is not None else "--"
        verdict = "BLOCKED" if self.blocked else ("OVERRIDDEN" if self.overridden else "passed")
        return (
            f"{self.tenant}/{self.dataset} @{self.layer}: {verdict}  "
            f"score={score} threshold={self.threshold:.3f}"
            + (f"  {self.reason}" if self.reason else "")
        )


def _latest_score(tenant: str, dataset: str, layer: str) -> dict[str, Any] | None:
    rows = db.fetch_all(
        """
        SELECT score_id, overall_score, promotion_blocked, rules_failed,
               rules_evaluated, rows_failing, rows_total, scored_at
          FROM platform.dq_score
         WHERE tenant = %s AND dataset = %s AND layer = %s
         ORDER BY scored_at DESC
         LIMIT 1
        """,
        (tenant, dataset, layer),
    )
    return rows[0] if rows else None


def _worst_rules(tenant: str, dataset: str, limit: int = 3) -> str:
    """The rules that dragged the score down, for the reason string.

    A gate that says only "0.94 < 0.95" sends whoever is on call to go and find
    out why. Naming the worst offenders in the decision row usually removes that
    step entirely.
    """
    rows = db.fetch_all(
        """
        SELECT r.family, r.field, d.rows_failed
          FROM platform.dq_result d
          JOIN platform.dq_rule r USING (rule_id)
         WHERE d.tenant = %s AND d.dataset = %s AND d.rows_failed > 0
           AND d.run_id = (SELECT run_id FROM platform.dq_score
                            WHERE tenant = %s AND dataset = %s
                            ORDER BY scored_at DESC LIMIT 1)
         ORDER BY d.rows_failed DESC
         LIMIT %s
        """,
        (tenant, dataset, tenant, dataset, limit),
    )
    if not rows:
        return ""
    return "worst: " + ", ".join(
        f"{r['family']}/{r['field'] or '_record'} ({r['rows_failed']:,})" for r in rows
    )


def decide(
    score: dict[str, Any] | None,
    *,
    tenant: str,
    dataset: str,
    layer: str,
    threshold: float,
    override: bool = False,
    override_reason: str | None = None,
    detail: str = "",
) -> GateDecision:
    """Turn a score row (or its absence) into a decision. No I/O.

    Separated from `evaluate` so the policy can be tested exhaustively against a
    handful of dicts rather than by standing up Postgres and a scored dataset —
    the same split that keeps `evaluation/scoring.py` testable. Everything
    load-bearing about the gate lives here: what a missing score means, what an
    override is allowed to do, and which threshold is compared against.
    """
    if score is None:
        decision = GateDecision(
            tenant=tenant,
            dataset=dataset,
            layer=layer,
            blocked=not override,
            decision="overridden" if override else "blocked_no_score",
            threshold=threshold,
            reason=(
                override_reason
                if override
                else f"no {layer} quality score for {tenant}/{dataset}; nothing has been checked"
            ),
            overridden=override,
        )
    else:
        # Compared here, not read from `promotion_blocked`: see the module
        # docstring. A null score is treated as failing rather than passing, for
        # the same reason a missing score blocks.
        below = score["overall_score"] is None or score["overall_score"] < threshold
        blocked = below and not override
        if below and override:
            verdict = "overridden"
        elif below:
            verdict = "blocked_low_score"
        else:
            verdict = "passed"
        reason = override_reason if override else None
        if below and not override:
            # `overall_score` can be NULL -- a scoring run where every rule was
            # vacuous stores no headline. Formatting it as a float crashes, and it
            # would crash only here, on the path taken when something is already
            # wrong.
            actual = (
                f"{score['overall_score']:.3f}"
                if score["overall_score"] is not None
                else "no score recorded"
            )
            reason = (
                f"score {actual} < threshold {threshold:.3f}; "
                f"{score['rules_failed']}/{score['rules_evaluated']} rules failing, "
                f"{score['rows_failing']:,}/{score['rows_total']:,} rows"
                + (f". {detail}" if detail else "")
            )
        decision = GateDecision(
            tenant=tenant,
            dataset=dataset,
            layer=layer,
            blocked=blocked,
            decision=verdict,
            threshold=threshold,
            overall_score=score["overall_score"],
            score_id=score["score_id"],
            reason=reason,
            overridden=bool(override and below),
        )
    return decision


def evaluate(
    tenant: str,
    dataset: str,
    *,
    layer: str = "bronze",
    override: bool = False,
    override_reason: str | None = None,
) -> GateDecision:
    """Decide whether `dataset` may be promoted, and record the decision.

    Raises `PromotionBlockedError` when it may not. Raising rather than returning a
    falsy value is deliberate: the caller is a CLI command whose exit status is the
    control signal, and a return value is easy to ignore by accident.
    """
    threshold = get_settings().quality.promotion_threshold
    score = _latest_score(tenant, dataset, layer)

    # Only looked up when it will be used, since it is a second query purely to
    # make the reason string useful to whoever is on call.
    detail = ""
    if score is not None and not override:
        below = score["overall_score"] is None or score["overall_score"] < threshold
        if below:
            detail = _worst_rules(tenant, dataset)

    decision = decide(
        score,
        tenant=tenant,
        dataset=dataset,
        layer=layer,
        threshold=threshold,
        override=override,
        override_reason=override_reason,
        detail=detail,
    )

    _record(decision)

    if decision.blocked:
        logger.warning(
            "promotion_blocked",
            tenant=tenant, dataset=dataset, layer=layer,
            score=decision.overall_score, threshold=threshold,
        )
        raise PromotionBlockedError(decision.summary())

    logger.info(
        "promotion_allowed",
        tenant=tenant, dataset=dataset, layer=layer,
        score=decision.overall_score, overridden=decision.overridden,
    )
    return decision


def _record(decision: GateDecision) -> None:
    """Persist the decision, and make a block visible in `platform.run`.

    Two places on purpose. `promotion_gate` is the audit trail; the `run` row is
    what `confidentia runs` shows, so a pipeline that stopped overnight explains
    itself in the same place every other stage reports.
    """
    try:
        with track_run(
            RunType.QUALITY,
            tenant=decision.tenant,
            dataset=decision.dataset,
            source="gate",
            metadata={"layer": decision.layer, "threshold": decision.threshold},
        ) as run:
            db.execute(
                """
                INSERT INTO platform.promotion_gate
                    (run_id, score_id, tenant, dataset, layer, overall_score,
                     threshold, blocked, decision, reason, overridden)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run.run_id, decision.score_id, decision.tenant, decision.dataset,
                    decision.layer, decision.overall_score, decision.threshold,
                    decision.blocked, decision.decision, decision.reason,
                    decision.overridden,
                ),
            )
            run.note(
                decision=decision.decision,
                blocked=decision.blocked,
                score=decision.overall_score,
            )
            if decision.blocked:
                # Raised inside the context so `track_run` closes the run as
                # *failed* with the reason attached. Closing it as succeeded and
                # relying on metadata to say otherwise would make a blocked
                # pipeline look healthy in `run_summary`, which is the first view
                # anyone checks.
                raise PromotionBlockedError(decision.reason or "promotion blocked")
    except PromotionBlockedError:
        # Swallowed here on purpose. The run row has served its purpose by being
        # closed as failed; `evaluate` owns the exception the caller actually sees,
        # and letting this one escape would report the bare reason instead of the
        # full decision summary.
        pass
