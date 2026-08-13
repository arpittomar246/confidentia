"""The C5 placement derivation and cost model.

Split the way the package is: the placement tests assert a *rule* and would hold on
real hardware; the model tests assert the arithmetic of something no one has
measured, so they check internal consistency and the honesty labelling rather than
any absolute number.
"""

from __future__ import annotations

import pytest

from confidentia.catalog.contracts import ColumnSpec, SchemaContract, all_contracts
from confidentia.config.settings import Layer, Sensitivity, Tenant
from confidentia.tee import placement as placement_module
from confidentia.tee.model import PAGE_BYTES, EnclaveCostModel
from confidentia.tee.placement import PIPELINE, Stage, Verdict, place, place_all

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ the rule


def _contract(*columns: ColumnSpec) -> SchemaContract:
    return SchemaContract(
        dataset="t",
        tenant=Tenant.HEALTHCARE,
        version=1,
        columns=columns,
        business_key=(columns[0].name,),
    )


PUBLIC_ONLY = _contract(ColumnSpec("id", "string", sensitivity=Sensitivity.PUBLIC))
TOKENISED_PII = _contract(
    ColumnSpec("id", "string", sensitivity=Sensitivity.PUBLIC),
    ColumnSpec("ssn", "string", sensitivity=Sensitivity.PII, tokenise=True),
)
QUASI_PHI = _contract(
    ColumnSpec("id", "string", sensitivity=Sensitivity.PUBLIC),
    ColumnSpec("diagnosis", "string", sensitivity=Sensitivity.PHI),
)


def _stage(**kwargs: object) -> Stage:
    defaults: dict[str, object] = {
        "name": "test.stage",
        "layer": Layer.SILVER,
        "reads_layer": Layer.SILVER,
        "description": "",
    }
    return Stage(**{**defaults, **kwargs})  # type: ignore[arg-type]


def test_a_stage_seeing_no_protected_column_stays_outside() -> None:
    assert place(_stage(), [PUBLIC_ONLY]).verdict is Verdict.OUTSIDE


def test_a_stage_reading_raw_records_is_inside_even_for_tokenisable_columns() -> None:
    """Nothing is tokenised before Silver, so Bronze sees the real SSN."""
    result = place(_stage(reads_layer=Layer.BRONZE), [TOKENISED_PII])
    assert result.verdict is Verdict.INSIDE
    assert result.protected_columns == {"healthcare/t": ["ssn"]}


def test_tokenisation_moves_a_downstream_stage_out() -> None:
    """The mechanism the whole architecture rests on, asserted rather than assumed."""
    assert place(_stage(reads_layer=Layer.SILVER), [TOKENISED_PII]).verdict is Verdict.OUTSIDE


def test_a_quasi_identifier_keeps_a_downstream_stage_inside() -> None:
    """A diagnosis code cannot be tokenised without destroying what it is for."""
    result = place(_stage(reads_layer=Layer.SILVER), [QUASI_PHI])
    assert result.verdict is Verdict.INSIDE
    assert result.protected_columns == {"healthcare/t": ["diagnosis"]}


def test_the_tokenising_stage_is_inside_regardless_of_its_input_layer() -> None:
    """It sees the pre-token values by definition; the input layer is irrelevant."""
    result = place(
        _stage(reads_layer=Layer.SILVER, performs_tokenisation=True), [TOKENISED_PII]
    )
    assert result.verdict is Verdict.INSIDE


def test_a_stage_reading_no_tenant_data_is_outside() -> None:
    result = place(_stage(reads_tenant_data=False), [QUASI_PHI])
    assert result.verdict is Verdict.OUTSIDE
    assert result.protected_columns == {}


# --------------------------------------------------------- the real pipeline


def test_every_stage_has_a_cost_group() -> None:
    """A stage with no group is a stage whose duration silently becomes zero."""
    assert all(stage.cost_group for stage in PIPELINE)


def test_the_serving_layer_is_outside_the_boundary() -> None:
    """ADR 0016 says the API holds no key material; the placement must agree."""
    verdicts = {p.stage.name: p.verdict for p in place_all()}
    assert verdicts["api.serve"] is Verdict.OUTSIDE
    assert verdicts["quality.gate"] is Verdict.OUTSIDE


def test_every_stage_touching_tenant_data_is_inside_for_this_platform() -> None:
    """The headline finding, pinned so a contract change that alters it is visible.

    If a future change tokenises or generalises the quasi-identifiers, this test
    fails -- and that failure is the signal that the boundary just got smaller,
    which is a result worth noticing rather than a test worth deleting.
    """
    for result in place_all():
        if result.stage.reads_tenant_data:
            assert result.verdict is Verdict.INSIDE, result.stage.name


def test_tokenisation_reduces_the_visible_protected_set() -> None:
    """Bronze sees strictly more than Silver, and the difference is the tokenised set."""
    contracts = all_contracts()
    at_bronze = placement_module.visible_protected_columns(
        _stage(reads_layer=Layer.BRONZE), contracts
    )
    at_silver = placement_module.visible_protected_columns(
        _stage(reads_layer=Layer.SILVER), contracts
    )
    bronze_total = sum(len(v) for v in at_bronze.values())
    silver_total = sum(len(v) for v in at_silver.values())
    assert bronze_total > silver_total > 0


def test_the_landing_zone_is_reported_as_a_boundary_violation() -> None:
    """Landing is written as PAR1 before Bronze encrypts anything."""
    violations = placement_module.boundary_violations()
    assert [v.location for v in violations] == ["lake/landing"]
    assert violations[0].protected_columns > 0
    assert violations[0].remedy


# ------------------------------------------------------------------- the model


@pytest.fixture
def model() -> EnclaveCostModel:
    from confidentia.config.settings import TeeBackend

    return EnclaveCostModel(
        epc_size_mb=128,
        ecall_latency_us=8.0,
        ocall_latency_us=12.0,
        page_fault_us=50.0,
        cold_start_ms=120.0,
        backend=TeeBackend.SIM,
    )


def test_nothing_the_model_produces_claims_to_be_measured(model: EnclaveCostModel) -> None:
    """Under `sim` there is no enclave, so no cost here was observed."""
    assert model.is_measured is False
    assert model.describe()["label"] == "MODELLED"
    cost = model.cost_of(
        "s", baseline_ms=100.0, baseline_is_measured=True, rows=1000, bytes_per_row=100
    )
    # The baseline was measured and the composite still is not: adding a modelled
    # term to a measured one gives a modelled result.
    assert cost.baseline_is_measured is True
    assert cost.as_dict()["label"] == "MODELLED"


def test_a_partition_is_sized_to_fit_the_epc_budget(model: EnclaveCostModel) -> None:
    plan = model.plan_partitions(rows_total=10_000_000, bytes_per_row=100)
    assert plan.fits
    assert plan.working_set_bytes <= model.epc_budget_bytes
    assert plan.rows_per_partition * plan.partitions >= plan.rows_total


def test_an_unpartitioned_working_set_larger_than_the_epc_faults(
    model: EnclaveCostModel,
) -> None:
    plan = model.working_set_for(rows=10_000_000, bytes_per_row=100)
    assert not plan.fits
    expected = plan.pages_per_partition - (model.epc_budget_bytes // PAGE_BYTES)
    assert plan.pages_faulted_per_partition == expected


def test_partitioning_never_costs_more_paging_than_not_partitioning(
    model: EnclaveCostModel,
) -> None:
    """The property the recommendation depends on."""
    for rows in (1_000, 500_000, 5_000_000, 50_000_000):
        sized = model.cost_of(
            "s", baseline_ms=1000.0, baseline_is_measured=True, rows=rows, bytes_per_row=116,
            partition_plan=model.plan_partitions(rows, 116),
        )
        unsized = model.cost_of(
            "s", baseline_ms=1000.0, baseline_is_measured=True, rows=rows, bytes_per_row=116,
            partition_plan=model.working_set_for(rows, 116),
        )
        assert sized.paging_ms <= unsized.paging_ms


def test_a_smaller_epc_means_more_partitions(model: EnclaveCostModel) -> None:
    big = model.plan_partitions(1_000_000, 116)
    model.epc_size_mb = 32
    small = model.plan_partitions(1_000_000, 116)
    assert small.partitions > big.partitions
    assert small.rows_per_partition < big.rows_per_partition


def test_zero_rows_does_not_divide_by_zero(model: EnclaveCostModel) -> None:
    plan = model.plan_partitions(0, 116)
    assert plan.partitions == 0
    cost = model.cost_of(
        "s", baseline_ms=0.0, baseline_is_measured=False, rows=0, bytes_per_row=0
    )
    assert cost.transition_ms == 0.0
