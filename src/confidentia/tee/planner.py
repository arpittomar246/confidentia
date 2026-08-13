"""The enclave placement planner (C5).

Puts the derivation and the model together and answers the project's research
question: *where in this pipeline should the confidentiality boundary be drawn, and
what does drawing it there cost?*

**Costs are charged per recorded run, not per stage.** The platform records one run
per layer, so Silver's three steps share a measurement and Gold's two share
another. Charging each step the full recorded duration triples Silver and doubles
Gold — which it did, in the first version of this file, producing a pipeline
baseline about 60% higher than the pipeline actually takes. Placement stays
per-stage because that is the decision; cost is per ``cost_group`` because that is
the measurement.

**Every cost here is MODELLED and every input is MEASURED**, and the report keeps
the two labelled so the distinction survives into whatever renders it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from confidentia.catalog.contracts import all_contracts
from confidentia.config.settings import Settings, get_settings
from confidentia.tee import placement as placement_module
from confidentia.tee import profile
from confidentia.tee.model import EnclaveCostModel, PartitionPlan, StageCost
from confidentia.tee.placement import Placement

# Which layer's row count a cost group processes. A group reading Bronze is sized
# by the Bronze row count, not by what it writes.
GROUP_INPUT_LAYER = {
    "ingest": None,  # sized by what it wrote; there is no upstream layer
    "bronze": "bronze",
    "silver": "bronze",
    "gold": "silver",
    "quality": "silver",
    "lineage": "bronze",
}


@dataclass
class PlannedGroup:
    """One recorded unit of work, with the stages it covers."""

    group: str
    placements: list[Placement]
    cost: StageCost | None
    rows: int
    bytes_per_row: int
    inside: bool

    @property
    def stage_names(self) -> list[str]:
        return [p.stage.name for p in self.placements]

    @property
    def reason(self) -> str:
        """Why the group is inside: the first stage that forces it."""
        forcing = next((p for p in self.placements if p.inside), None)
        if forcing is None:
            return "no stage in this group observes protected plaintext"
        return f"{forcing.stage.name}: {forcing.reason}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "stages": self.stage_names,
            "inside": self.inside,
            "reason": self.reason,
            "rows": self.rows,
            "bytes_per_row": self.bytes_per_row,
            "cost": self.cost.as_dict() if self.cost else None,
        }


@dataclass
class Plan:
    name: str
    description: str
    groups: list[PlannedGroup]

    @property
    def inside_count(self) -> int:
        return sum(1 for g in self.groups if g.cost is not None)

    @property
    def baseline_ms(self) -> float:
        return sum(g.cost.baseline_ms for g in self.groups if g.cost)

    @property
    def enclaved_ms(self) -> float:
        return sum(g.cost.enclaved_ms for g in self.groups if g.cost)

    @property
    def overhead_ms(self) -> float:
        return self.enclaved_ms - self.baseline_ms

    @property
    def paging_ms(self) -> float:
        return sum(g.cost.paging_ms for g in self.groups if g.cost)

    @property
    def factor(self) -> float:
        return self.enclaved_ms / self.baseline_ms if self.baseline_ms else 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan": self.name,
            "description": self.description,
            "groups_inside": self.inside_count,
            "baseline_ms": round(self.baseline_ms, 1),
            "enclaved_ms": round(self.enclaved_ms, 1),
            "overhead_ms": round(self.overhead_ms, 1),
            "paging_ms": round(self.paging_ms, 1),
            "factor": round(self.factor, 3),
            "label": "MODELLED",
            "groups": [g.as_dict() for g in self.groups],
        }


PLAN_DESCRIPTIONS = {
    "everything": "Every group that touches tenant data, processed unpartitioned",
    "derived": "Only groups containing a stage that observes protected plaintext",
    "sized": "Derived placement, with partitions sized to the EPC budget",
}


@dataclass
class Counterfactual:
    """What it would take to move a group out of the enclave."""

    group: str
    blocking_columns: dict[str, list[str]]
    saving_ms: float
    verdict: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "blocking_columns": {k: v for k, v in sorted(self.blocking_columns.items())},
            "blocking_column_count": sum(len(v) for v in self.blocking_columns.values()),
            "saving_ms": round(self.saving_ms, 1),
            "verdict": self.verdict,
            "label": "MODELLED",
        }


@dataclass
class PlanReport:
    model: EnclaveCostModel
    plans: dict[str, Plan]
    violations: list[placement_module.BoundaryViolation]
    widths: list[profile.RowWidth]
    timings: dict[str, profile.StageTiming]
    counterfactuals: list[Counterfactual] = field(default_factory=list)
    epc_threshold_rows: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def recommended(self) -> Plan:
        return self.plans["sized"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model.describe(),
            "plans": {name: plan.as_dict() for name, plan in self.plans.items()},
            "violations": [v.as_dict() for v in self.violations],
            "row_widths": [w.as_dict() for w in self.widths],
            "timings": [t.as_dict() for t in self.timings.values()],
            "counterfactuals": [c.as_dict() for c in self.counterfactuals],
            "epc_threshold_rows": self.epc_threshold_rows,
            "warnings": self.warnings,
        }


def weighted_bytes_per_row(widths: list[profile.RowWidth]) -> int:
    """Row-weighted mean uncompressed width.

    Weighted, not the maximum. The widest dataset here is a **five-row** facility
    lookup at 412 B/row, and using it as the width for every stage made the model
    think the whole pipeline was four times its actual size -- which is the sort of
    error that produces a confident, wrong recommendation.
    """
    rows = sum(w.rows for w in widths)
    if not rows:
        return 0
    return round(sum(w.uncompressed_bytes for w in widths) / rows)


def _layer_rows(counts: dict[tuple[str, str, str], int]) -> dict[str, int]:
    totals = {"bronze": 0, "silver": 0, "gold": 0}
    for (_, _, layer), rows in counts.items():
        if layer in totals:
            totals[layer] += rows
    return totals


def build_plan(settings: Settings | None = None, *, batch_rows: int = 10_000) -> PlanReport:
    settings = settings or get_settings()
    model = EnclaveCostModel.from_settings(settings)

    widths = profile.measure_row_widths(settings)
    timings = profile.stage_timings()
    counts = profile.record_counts()
    placements = placement_module.place_all()

    warnings: list[str] = []
    if not widths:
        warnings.append(
            "No landing-zone Parquet found: row widths are unmeasured, so partition "
            "sizing is not grounded in this dataset."
        )
    if not timings:
        warnings.append("No successful runs recorded: the plan reports structure but no costs.")

    bytes_per_row = weighted_bytes_per_row(widths)
    layer_rows = _layer_rows(counts)

    by_group: dict[str, list[Placement]] = {}
    for place in placements:
        by_group.setdefault(place.stage.cost_group, []).append(place)

    def rows_for(group: str) -> int:
        layer = GROUP_INPUT_LAYER.get(group)
        if layer:
            return layer_rows.get(layer, 0)
        timing = timings.get(group)
        return timing.rows_median if timing else 0

    def build(strategy: str) -> Plan:
        groups: list[PlannedGroup] = []
        for group, members in by_group.items():
            timing = timings.get(group)
            rows = rows_for(group)
            derived_inside = any(p.inside for p in members)
            touches_data = any(p.stage.reads_tenant_data for p in members)
            inside = touches_data if strategy == "everything" else derived_inside

            if not inside or timing is None:
                groups.append(
                    PlannedGroup(group, members, None, rows, bytes_per_row, inside=inside)
                )
                continue

            partition_plan: PartitionPlan = (
                model.plan_partitions(rows, bytes_per_row)
                if strategy == "sized"
                # Unsized: the whole input is one working set, which is what happens
                # when nobody considers the EPC at all.
                else model.working_set_for(rows, bytes_per_row)
            )
            groups.append(
                PlannedGroup(
                    group,
                    members,
                    model.cost_of(
                        group,
                        baseline_ms=timing.median_ms,
                        baseline_is_measured=timing.is_measured,
                        rows=rows,
                        bytes_per_row=bytes_per_row,
                        batch_rows=batch_rows,
                        partition_plan=partition_plan,
                    ),
                    rows,
                    bytes_per_row,
                    inside=True,
                )
            )
        return Plan(strategy, PLAN_DESCRIPTIONS[strategy], groups)

    plans = {name: build(name) for name in PLAN_DESCRIPTIONS}

    return PlanReport(
        model=model,
        plans=plans,
        violations=placement_module.boundary_violations(),
        widths=widths,
        timings=timings,
        counterfactuals=_counterfactuals(plans["sized"]),
        epc_threshold_rows=(model.epc_budget_bytes // bytes_per_row) if bytes_per_row else 0,
        warnings=warnings,
    )


def _counterfactuals(plan: Plan) -> list[Counterfactual]:
    """What would have to change for each enclaved group to move out.

    This is the planner's actual advice. "Everything is inside" is a true answer and
    a useless one; the useful question is *which columns are keeping it there*, and
    whether removing them is possible.

    A column is only reported as blocking if it is **not already tokenised** —
    tokenising it again is not an option, and listing it would suggest an action
    that does not exist. What remains is the quasi-identifiers, and the verdict says
    plainly that they cannot be tokenised without destroying what they are for.
    """
    tokenisable = {
        column.name
        for contract in all_contracts()
        for column in contract.columns
        if column.tokenise
    }
    out: list[Counterfactual] = []
    for group in plan.groups:
        if group.cost is None:
            continue
        blocking: dict[str, list[str]] = {}
        for place in group.placements:
            for dataset, columns in place.protected_columns.items():
                remaining = sorted({c for c in columns if c not in tokenisable})
                if remaining:
                    blocking.setdefault(dataset, [])
                    blocking[dataset] = sorted(set(blocking[dataset]) | set(remaining))

        reads_raw = all(p.stage.reads_layer is None for p in group.placements)
        if any(p.stage.performs_tokenisation for p in group.placements):
            verdict = (
                "Cannot move out at any price: this group performs tokenisation, so it "
                "must see the pre-token values by definition."
            )
        elif reads_raw:
            # Listing quasi-identifiers here would imply an action that does not
            # exist: there is no upstream to tokenise in, because this *is* the
            # upstream. The only lever is at the source system.
            verdict = (
                "Cannot move out: reads raw source records, where nothing has been "
                "tokenised yet. The only lever is to tokenise or suppress at the "
                "source system, before the platform receives the data."
            )
        elif not blocking:
            verdict = "Would move out once upstream tokenisation is applied."
        else:
            verdict = (
                f"Blocked by {sum(len(v) for v in blocking.values())} quasi-identifier "
                "columns. Tokenising them would destroy the joins and aggregations they "
                "exist for; generalisation or suppression at the source is the only "
                "route, and that changes what the platform can answer."
            )
        out.append(
            Counterfactual(
                group=group.group,
                blocking_columns={k: v for k, v in blocking.items() if v},
                saving_ms=group.cost.enclaved_ms,
                verdict=verdict,
            )
        )
    return out


# ----------------------------------------------------------------- EPC sweep


@dataclass(frozen=True)
class SweepPoint:
    """One point of the volume sweep: sized against unsized, at a fixed EPC."""

    scale: float
    rows: int
    epc_size_mb: int
    rows_per_partition: int
    partitions: int
    sized_factor: float
    unsized_factor: float
    unsized_paging_ms: float

    @property
    def sizing_saves_ms(self) -> float:
        return self.unsized_paging_ms

    def as_dict(self) -> dict[str, Any]:
        return {
            "scale": self.scale,
            "rows": self.rows,
            "epc_size_mb": self.epc_size_mb,
            "rows_per_partition": self.rows_per_partition,
            "partitions": self.partitions,
            "sized_factor": round(self.sized_factor, 3),
            "unsized_factor": round(self.unsized_factor, 3),
            "sizing_saves_ms": round(self.sizing_saves_ms, 1),
            "label": "MODELLED",
        }


DEFAULT_SCALES = (1, 2, 5, 10, 25, 50, 100)


def sweep_volume(
    scales: tuple[float, ...] = DEFAULT_SCALES,
    settings: Settings | None = None,
    *,
    epc_size_mb: int | None = None,
    report: PlanReport | None = None,
) -> list[SweepPoint]:
    """Where EPC-aware partition sizing starts to be worth anything.

    Sweeping the **EPC** at this project's volume answers nothing: the whole Bronze
    layer is about 49 MB uncompressed, so it fits inside even a 32 MB budget once
    partitioned, and every EPC size produces the same number. That flatness is a
    real result -- at 422k rows the EPC is not the constraint -- and it is also why
    this sweeps *volume* instead.

    Each point costs the same pipeline twice: partitioned to the EPC budget, and
    unpartitioned. The gap between them is the entire value of the sizing, and it is
    zero until the working set crosses the budget.
    """
    settings = settings or get_settings()
    base = report or build_plan(settings)

    model = EnclaveCostModel.from_settings(settings)
    if epc_size_mb is not None:
        model.epc_size_mb = epc_size_mb

    points: list[SweepPoint] = []
    for scale in scales:
        sized_total = unsized_total = baseline_total = 0.0
        paging = 0.0
        rows_per_partition = partitions = largest_rows = 0
        for group in base.plans["sized"].groups:
            if group.cost is None:
                continue
            rows = int(group.rows * scale)
            baseline = group.cost.baseline_ms * scale

            sized_plan = model.plan_partitions(rows, group.bytes_per_row)
            unsized_plan = model.working_set_for(rows, group.bytes_per_row)

            # Spelled out rather than splatted from a dict: mypy cannot narrow a
            # heterogeneous **kwargs against a typed signature, and silencing that
            # with an ignore would also silence a genuinely wrong argument.
            sized = model.cost_of(
                group.group,
                baseline_ms=baseline,
                baseline_is_measured=group.cost.baseline_is_measured,
                rows=rows,
                bytes_per_row=group.bytes_per_row,
                partition_plan=sized_plan,
            )
            unsized = model.cost_of(
                group.group,
                baseline_ms=baseline,
                baseline_is_measured=group.cost.baseline_is_measured,
                rows=rows,
                bytes_per_row=group.bytes_per_row,
                partition_plan=unsized_plan,
            )

            sized_total += sized.enclaved_ms
            unsized_total += unsized.enclaved_ms
            baseline_total += baseline
            paging += unsized.paging_ms
            rows_per_partition = max(rows_per_partition, sized_plan.rows_per_partition)
            partitions = max(partitions, sized_plan.partitions)
            largest_rows = max(largest_rows, rows)

        points.append(
            SweepPoint(
                scale=scale,
                rows=largest_rows,
                epc_size_mb=model.epc_size_mb,
                rows_per_partition=rows_per_partition,
                partitions=partitions,
                sized_factor=sized_total / baseline_total if baseline_total else 1.0,
                unsized_factor=unsized_total / baseline_total if baseline_total else 1.0,
                unsized_paging_ms=paging,
            )
        )
    return points
