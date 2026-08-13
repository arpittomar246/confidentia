"""The enclave cost model (C5).

**Everything in this module is modelled.** There is no confidential-computing
hardware on this project, so no number produced here is a measurement, and every
result carries ``is_measured = False`` and the label ``MODELLED`` so that a caller
cannot present one as observed. That is the whole reason results are wrapped in
``Modelled`` rather than returned as bare floats: a float loses its provenance the
moment it is put in a table.

The model has four terms, and each corresponds to a real cost that published SGX
microbenchmarks report:

``cold start``
    Creating and attesting an enclave. Paid once per enclave instantiation, so a
    stage split across many short tasks pays it repeatedly — which is the main
    reason placement matters at all.

``transitions``
    Every crossing of the boundary is an ``ecall`` (in) or ``ocall`` (out) with a
    fixed cost. A stage that streams data through the enclave in small batches
    pays this per batch.

``paging``
    The Enclave Page Cache is bounded — 128 MB by default on SGX v1. A working set
    larger than the EPC forces encrypted paging to unprotected memory, and that
    cost is the one that grows non-linearly with partition size. Avoiding it is
    what "EPC-aware partition sizing" means.

``compute``
    Memory encryption slows ordinary work even when nothing pages. Modelled as a
    flat multiplier on the measured baseline.

The inputs to all four come from ``TeeSettings`` and are calibrated from published
figures rather than measured here — see ADR 0006 for where each number comes from
and what changes if the hardware assumption changes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from confidentia.config.settings import Settings, TeeBackend, get_settings

# SGX pages are 4 KiB, like the host's. Not a tunable: it is a property of the
# hardware the model describes.
PAGE_BYTES = 4096

# How much of the EPC a working set may occupy before the model treats it as
# paging. Not 1.0 because the enclave's own code, stack and runtime live there
# too; 0.75 is the fraction commonly assumed in the literature for application
# data on a 128 MB EPC.
DEFAULT_EPC_UTILISATION = 0.75

# Memory-encryption overhead on ordinary compute, independent of paging. Applied
# multiplicatively to the measured baseline.
DEFAULT_COMPUTE_FACTOR = 1.20


@dataclass(frozen=True)
class Modelled:
    """A number that came from a model, carrying that fact.

    ``label`` is what reporting code must print next to the value. Under a real
    backend the same struct is returned with ``is_measured = True`` and the label
    ``MEASURED``, so call sites do not branch on the backend.
    """

    value: float
    unit: str
    is_measured: bool = False
    basis: str = ""

    @property
    def label(self) -> str:
        return "MEASURED" if self.is_measured else "MODELLED"

    def __str__(self) -> str:
        return f"{self.value:,.1f} {self.unit} [{self.label}]"

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "label": self.label,
            "basis": self.basis,
        }


@dataclass(frozen=True)
class PartitionPlan:
    """How to split a dataset so one partition's working set fits the EPC."""

    rows_total: int
    bytes_per_row: int
    epc_budget_bytes: int
    rows_per_partition: int
    partitions: int
    working_set_bytes: int
    pages_per_partition: int = 0
    pages_resident: int = 0

    @property
    def fits(self) -> bool:
        return self.working_set_bytes <= self.epc_budget_bytes

    @property
    def pages_faulted_per_partition(self) -> int:
        """Pages that must be evicted and re-read because the EPC cannot hold them.

        Zero when the partition fits. The model charges one fault per page beyond
        the budget rather than simulating a replacement policy: the point is the
        cliff at the EPC boundary, and a more elaborate policy would be a more
        precise answer to a question the hardware is not here to check.
        """
        return max(0, self.pages_per_partition - self.pages_resident)


@dataclass(frozen=True)
class StageCost:
    """Modelled cost of running one stage inside the enclave."""

    stage: str
    baseline_ms: float
    baseline_is_measured: bool
    cold_start_ms: float
    transition_ms: float
    paging_ms: float
    compute_ms: float
    partition_plan: PartitionPlan | None = None

    @property
    def overhead_ms(self) -> float:
        return self.cold_start_ms + self.transition_ms + self.paging_ms + self.compute_ms

    @property
    def enclaved_ms(self) -> float:
        return self.baseline_ms + self.overhead_ms

    @property
    def factor(self) -> float:
        """Modelled slowdown. 1.0 means the enclave costs nothing, which it never does."""
        return self.enclaved_ms / self.baseline_ms if self.baseline_ms > 0 else math.inf

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "baseline_ms": round(self.baseline_ms, 1),
            "baseline_label": "MEASURED" if self.baseline_is_measured else "MODELLED",
            "cold_start_ms": round(self.cold_start_ms, 1),
            "transition_ms": round(self.transition_ms, 1),
            "paging_ms": round(self.paging_ms, 1),
            "compute_ms": round(self.compute_ms, 1),
            "overhead_ms": round(self.overhead_ms, 1),
            "enclaved_ms": round(self.enclaved_ms, 1),
            "factor": round(self.factor, 3),
            # The composite is modelled even when the baseline was measured, and
            # this is the field that says so.
            "label": "MODELLED",
        }


@dataclass
class EnclaveCostModel:
    """Turns measured pipeline facts into modelled enclave costs."""

    epc_size_mb: int
    ecall_latency_us: float
    ocall_latency_us: float
    page_fault_us: float
    cold_start_ms: float
    backend: TeeBackend
    epc_utilisation: float = DEFAULT_EPC_UTILISATION
    compute_factor: float = DEFAULT_COMPUTE_FACTOR
    _settings: Settings | None = field(default=None, repr=False)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> EnclaveCostModel:
        settings = settings or get_settings()
        tee = settings.tee
        return cls(
            epc_size_mb=tee.epc_size_mb,
            ecall_latency_us=tee.sim_ecall_latency_us,
            ocall_latency_us=tee.sim_ocall_latency_us,
            page_fault_us=tee.sim_page_fault_us,
            cold_start_ms=tee.sim_cold_start_ms,
            backend=tee.backend,
            _settings=settings,
        )

    @property
    def is_measured(self) -> bool:
        """False under ``sim``. Nothing this model produces is observed there."""
        return self.backend is not TeeBackend.SIM

    @property
    def epc_budget_bytes(self) -> int:
        return int(self.epc_size_mb * 1024 * 1024 * self.epc_utilisation)

    # ------------------------------------------------------------- partitions

    def plan_partitions(self, rows_total: int, bytes_per_row: int) -> PartitionPlan:
        """Split so that one partition's working set fits the EPC budget.

        ``bytes_per_row`` must be the *uncompressed* width. Using the on-disk
        Parquet size understates the working set by whatever the compression ratio
        happens to be, which on this data is between 3x and 8x — enough to turn a
        partition that pages heavily into one the model says fits comfortably.
        """
        bytes_per_row = max(1, bytes_per_row)
        budget = self.epc_budget_bytes
        rows_per_partition = max(1, budget // bytes_per_row)
        partitions = max(1, math.ceil(rows_total / rows_per_partition)) if rows_total else 0
        working_set = min(rows_total, rows_per_partition) * bytes_per_row
        return PartitionPlan(
            rows_total=rows_total,
            bytes_per_row=bytes_per_row,
            epc_budget_bytes=budget,
            rows_per_partition=rows_per_partition,
            partitions=partitions,
            working_set_bytes=working_set,
            pages_per_partition=math.ceil(working_set / PAGE_BYTES),
            pages_resident=budget // PAGE_BYTES,
        )

    def working_set_for(self, rows: int, bytes_per_row: int) -> PartitionPlan:
        """What happens if the whole dataset is processed as one partition.

        This is the counterfactual the recommendation is measured against: the
        difference between this and ``plan_partitions`` is what EPC-aware sizing
        is worth.
        """
        bytes_per_row = max(1, bytes_per_row)
        working_set = rows * bytes_per_row
        budget = self.epc_budget_bytes
        return PartitionPlan(
            rows_total=rows,
            bytes_per_row=bytes_per_row,
            epc_budget_bytes=budget,
            rows_per_partition=rows,
            partitions=1 if rows else 0,
            working_set_bytes=working_set,
            pages_per_partition=math.ceil(working_set / PAGE_BYTES),
            pages_resident=budget // PAGE_BYTES,
        )

    # ------------------------------------------------------------------ costs

    def cost_of(
        self,
        stage: str,
        *,
        baseline_ms: float,
        baseline_is_measured: bool,
        rows: int,
        bytes_per_row: int,
        batch_rows: int = 10_000,
        enclave_instances: int = 1,
        partition_plan: PartitionPlan | None = None,
    ) -> StageCost:
        """Modelled cost of running ``stage`` inside the enclave.

        ``batch_rows`` is how many rows cross the boundary per transition. Spark
        moves data in columnar batches; 10,000 is its default for the vectorised
        reader, and it is a parameter here because the sweep varies it.
        """
        plan = partition_plan or self.plan_partitions(rows, bytes_per_row)

        cold = self.cold_start_ms * max(1, enclave_instances)

        # One ecall in and one ocall out per batch. Batches, not rows: a per-row
        # crossing would be an absurd design and modelling it would produce an
        # absurd number that no one would believe, which is its own kind of dishonest.
        batches = math.ceil(rows / batch_rows) if rows else 0
        transitions_us = batches * (self.ecall_latency_us + self.ocall_latency_us)

        faults = plan.pages_faulted_per_partition * max(1, plan.partitions)
        paging_us = faults * self.page_fault_us

        compute = baseline_ms * (self.compute_factor - 1.0)

        return StageCost(
            stage=stage,
            baseline_ms=baseline_ms,
            baseline_is_measured=baseline_is_measured,
            cold_start_ms=cold,
            transition_ms=transitions_us / 1000.0,
            paging_ms=paging_us / 1000.0,
            compute_ms=compute,
            partition_plan=plan,
        )

    def describe(self) -> dict[str, Any]:
        """The model's inputs, for printing next to any result it produced."""
        return {
            "backend": self.backend.value,
            "hardware_rooted": self.is_measured,
            "label": "MEASURED" if self.is_measured else "MODELLED",
            "epc_size_mb": self.epc_size_mb,
            "epc_utilisation": self.epc_utilisation,
            "epc_budget_bytes": self.epc_budget_bytes,
            "ecall_latency_us": self.ecall_latency_us,
            "ocall_latency_us": self.ocall_latency_us,
            "page_fault_us": self.page_fault_us,
            "cold_start_ms": self.cold_start_ms,
            "compute_factor": self.compute_factor,
        }
