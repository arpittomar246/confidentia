"""Healthcare tenant generators — the batch and CDC side of the platform.

Produces a small set of dimensions and two large fact streams, plus a *mutation
script* for patients. The mutations are the point: without records that change
over time there is nothing for a Slowly Changing Dimension to track, and the SCD2
implementation in Silver could only be asserted, never exercised.

Entity identifiers are deterministic functions of an integer index
(``PAT-000000123``), which lets a downstream generator reference an entity without
the upstream dataset being materialised. Claim lines can therefore be produced for
5M encounters without holding 5M encounters in memory.

All PII here is synthetic. No real patient, provider or facility exists in these
vocabularies.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import pandas as pd

from confidentia.generators import reference as ref
from confidentia.generators.base import (
    Chunk,
    GeneratorConfig,
    Tenant,
    apply_late_arrival,
    derive_rng,
    diurnal_offsets,
    to_timestamps,
    zipf_choice,
)
from confidentia.generators.defects import DefectPlan, DefectType, FieldRole, injector_for


def patient_id(index: np.ndarray | int) -> np.ndarray | str:
    if isinstance(index, int):
        return f"PAT-{index:09d}"
    return np.char.add("PAT-", np.char.zfill(index.astype(str), 9))


def encounter_id(index: np.ndarray) -> np.ndarray:
    return np.char.add("ENC-", np.char.zfill(index.astype(str), 12))


def provider_id(index: np.ndarray | int) -> np.ndarray | str:
    if isinstance(index, int):
        return f"PRV-{index:06d}"
    return np.char.add("PRV-", np.char.zfill(index.astype(str), 6))


def facility_id(index: np.ndarray | int) -> np.ndarray | str:
    if isinstance(index, int):
        return f"FAC-{index:04d}"
    return np.char.add("FAC-", np.char.zfill(index.astype(str), 4))


@dataclass(frozen=True)
class HealthcareScale:
    """Entity counts derived from the requested fact volume.

    Ratios are chosen so that aggregates stay plausible: roughly 12 encounters per
    patient over six months is high but defensible for a system that skews toward
    chronic-care populations, and it keeps the patient dimension small enough to
    hold in memory while the fact tables scale freely.
    """

    encounters: int
    patients: int
    providers: int
    facilities: int
    plans: int

    @classmethod
    def for_rows(cls, encounters: int) -> HealthcareScale:
        patients = max(500, min(encounters // 12, 2_000_000))
        providers = max(25, min(patients // 120, 20_000))
        facilities = max(5, min(providers // 15, 1_500))
        return cls(
            encounters=encounters,
            patients=patients,
            providers=providers,
            facilities=facilities,
            plans=len(ref.PAYERS),
        )


# ------------------------------------------------------------------ dimensions


def generate_facilities(config: GeneratorConfig, scale: HealthcareScale) -> pd.DataFrame:
    rng = derive_rng(config.seed, "facility")
    n = scale.facilities
    cities = ref.CITIES
    weights = np.array([c[5] for c in cities], dtype=float)
    weights /= weights.sum()
    city_idx = rng.choice(len(cities), size=n, p=weights)

    types, type_weights = ref.weighted(ref.FACILITY_TYPES)
    chosen_type = rng.choice(types, size=n, p=type_weights)

    return pd.DataFrame(
        {
            "facility_id": facility_id(np.arange(n)),
            "facility_name": [
                f"{cities[i][0]} {t} {j % 9 + 1}"
                for j, (i, t) in enumerate(zip(city_idx, chosen_type, strict=True))
            ],
            "facility_type": chosen_type,
            "city": [cities[i][0] for i in city_idx],
            "state": [cities[i][1] for i in city_idx],
            "postal_code": [
                f"{cities[i][2]}{rng.integers(10, 99):02d}" for i in city_idx
            ],
            "latitude": np.round([cities[i][3] for i in city_idx], 4),
            "longitude": np.round([cities[i][4] for i in city_idx], 4),
            "bed_count": np.where(
                chosen_type == "Community Hospital",
                rng.integers(60, 400, size=n),
                np.where(chosen_type == "Academic Medical Centre", rng.integers(400, 1200, size=n), 0),
            ),
            "is_active": rng.uniform(size=n) > 0.03,
        }
    )


def generate_plans(config: GeneratorConfig) -> pd.DataFrame:
    rng = derive_rng(config.seed, "plan")
    n = len(ref.PAYERS)
    return pd.DataFrame(
        {
            "plan_id": [f"PLN-{i:03d}" for i in range(n)],
            "payer_name": [p[0] for p in ref.PAYERS],
            "plan_type": [p[1] for p in ref.PAYERS],
            "annual_deductible": np.round(rng.choice([500, 1000, 1500, 2500, 5000], size=n), 2),
            "coinsurance_rate": np.round(rng.uniform(0.10, 0.30, size=n), 2),
        }
    )


def generate_providers(config: GeneratorConfig, scale: HealthcareScale) -> pd.DataFrame:
    rng = derive_rng(config.seed, "provider")
    n = scale.providers
    first = rng.choice(ref.FIRST_NAMES, size=n)
    last = rng.choice(ref.LAST_NAMES, size=n)
    return pd.DataFrame(
        {
            "provider_id": provider_id(np.arange(n)),
            # NPI is a real 10-digit identifier format; these are synthetic values
            # in the 19xxxxxxxx range, which is not an issued NPI range.
            "npi": [f"19{rng.integers(0, 10**8):08d}" for _ in range(n)],
            "provider_name": [f"Dr. {f} {ln}" for f, ln in zip(first, last, strict=True)],
            "specialty": rng.choice(ref.SPECIALTIES, size=n),
            "facility_id": facility_id(rng.integers(0, scale.facilities, size=n)),
            "is_accepting_patients": rng.uniform(size=n) > 0.25,
        }
    )


def generate_patients(config: GeneratorConfig, scale: HealthcareScale) -> Iterator[Chunk]:
    """Patient dimension. Mutable, PII-bearing, and the SCD2 subject."""
    plan = _patient_defect_plan()
    injector = injector_for(plan, config)
    emitted = 0
    chunk_index = 0

    while emitted < scale.patients:
        size = min(config.chunk_rows, scale.patients - emitted)
        rng = derive_rng(config.seed, "patient", chunk_index)
        offset = np.arange(emitted, emitted + size)

        first = rng.choice(ref.FIRST_NAMES, size=size)
        last = rng.choice(ref.LAST_NAMES, size=size)
        cities = ref.CITIES
        weights = np.array([c[5] for c in cities], dtype=float)
        weights /= weights.sum()
        city_idx = rng.choice(len(cities), size=size, p=weights)

        # Age: mixture of a broad adult population and an older, higher-utilisation
        # cohort. A uniform age would make every age-band aggregate identical.
        young = rng.normal(38, 14, size=size)
        old = rng.normal(71, 9, size=size)
        is_old = rng.uniform(size=size) < 0.32
        age = np.clip(np.where(is_old, old, young), 0, 101)
        birth = pd.Timestamp(config.start_date, tz="UTC") - pd.to_timedelta(
            (age * 365.25).astype(np.int64), unit="D"
        )

        frame = pd.DataFrame(
            {
                "patient_id": patient_id(offset),
                "mrn": np.char.add("MRN", np.char.zfill(offset.astype(str), 9)),
                "first_name": first,
                "last_name": last,
                "date_of_birth": birth.date if hasattr(birth, "date") else birth,
                "sex": rng.choice(["F", "M", "U"], size=size, p=[0.505, 0.485, 0.010]),
                "ssn": [
                    f"{rng.integers(100, 900):03d}-{rng.integers(10, 100):02d}-{rng.integers(1000, 10000):04d}"
                    for _ in range(size)
                ],
                "email": [
                    f"{f.lower()}.{ln.lower()}{i}@{rng.choice(ref.EMAIL_DOMAINS)}"
                    for f, ln, i in zip(first, last, offset, strict=True)
                ],
                "phone": [
                    f"({rng.integers(200, 990):03d}) {rng.integers(200, 990):03d}-{rng.integers(1000, 10000):04d}"
                    for _ in range(size)
                ],
                "address_line1": [
                    f"{rng.integers(1, 9999)} {rng.choice(ref.LAST_NAMES)} "
                    f"{rng.choice(['St', 'Ave', 'Rd', 'Blvd', 'Ln'])}"
                    for _ in range(size)
                ],
                "city": [cities[i][0] for i in city_idx],
                "state": [cities[i][1] for i in city_idx],
                "postal_code": [f"{cities[i][2]}{rng.integers(10, 99):02d}" for i in city_idx],
                "plan_id": [f"PLN-{rng.integers(0, scale.plans):03d}" for _ in range(size)],
                "primary_care_provider_id": provider_id(
                    rng.integers(0, scale.providers, size=size)
                ),
                "created_at": pd.Timestamp(config.start_date, tz="UTC")
                - pd.to_timedelta(rng.integers(30, 3650, size=size), unit="D"),
                "updated_at": pd.Timestamp(config.start_date, tz="UTC"),
            }
        )
        frame["date_of_birth"] = birth.normalize()

        frame, manifest = injector.inject(frame, rng, chunk_index)
        yield Chunk("patient", Tenant.HEALTHCARE, chunk_index, frame, manifest)

        emitted += size
        chunk_index += 1


def generate_patient_changes(config: GeneratorConfig, scale: HealthcareScale) -> Iterator[Chunk]:
    """A script of patient mutations, to be replayed as UPDATEs against the OLTP
    database so that the CDC path produces genuine change events.

    Change kinds are weighted by how often they really happen: people move, change
    insurance at renewal, switch doctors, and occasionally change surname. Each is
    a *type 2* change for the dimension — the old value must remain queryable, or
    a claim adjudicated under last year's plan cannot be explained.
    """
    # Roughly 18% of the population changes something over the window.
    total = max(1, int(scale.patients * 0.18))
    kinds = ("address", "plan", "pcp", "surname", "phone")
    kind_weights = (0.40, 0.24, 0.18, 0.06, 0.12)

    emitted = 0
    chunk_index = 0
    while emitted < total:
        size = min(config.chunk_rows, total - emitted)
        rng = derive_rng(config.seed, "patient_change", chunk_index)

        targets = rng.choice(scale.patients, size=size, replace=not (size <= scale.patients))
        kind = rng.choice(kinds, size=size, p=kind_weights)
        change_offset = rng.uniform(0, config.days * 86400, size=size)
        change_ts = to_timestamps(np.sort(change_offset), config.start_date)

        cities = ref.CITIES
        city_idx = rng.integers(0, len(cities), size=size)

        new_value = np.empty(size, dtype=object)
        field = np.empty(size, dtype=object)
        for i, k in enumerate(kind):
            if k == "address":
                field[i] = "address_line1"
                new_value[i] = (
                    f"{rng.integers(1, 9999)} {rng.choice(ref.LAST_NAMES)} "
                    f"{rng.choice(['St', 'Ave', 'Rd'])}"
                )
            elif k == "plan":
                field[i] = "plan_id"
                new_value[i] = f"PLN-{rng.integers(0, scale.plans):03d}"
            elif k == "pcp":
                field[i] = "primary_care_provider_id"
                new_value[i] = provider_id(int(rng.integers(0, scale.providers)))
            elif k == "surname":
                field[i] = "last_name"
                new_value[i] = str(rng.choice(ref.LAST_NAMES))
            else:
                field[i] = "phone"
                new_value[i] = (
                    f"({rng.integers(200, 990):03d}) {rng.integers(200, 990):03d}-"
                    f"{rng.integers(1000, 10000):04d}"
                )

        frame = pd.DataFrame(
            {
                "change_id": [f"CHG-{chunk_index:04d}-{i:08d}" for i in range(size)],
                "patient_id": patient_id(targets),
                "change_type": kind,
                "changed_field": field,
                "new_value": new_value,
                "new_city": [cities[i][0] for i in city_idx],
                "new_state": [cities[i][1] for i in city_idx],
                "change_ts": change_ts,
                "operation": "UPDATE",
            }
        )
        yield Chunk("patient_change", Tenant.HEALTHCARE, chunk_index, frame)

        emitted += size
        chunk_index += 1


# ----------------------------------------------------------------- fact tables


def generate_encounters(config: GeneratorConfig, scale: HealthcareScale) -> Iterator[Chunk]:
    plan = _encounter_defect_plan()
    injector = injector_for(plan, config)

    codes, systems, code_weights = ref.icd10_arrays()
    types, type_weights = ref.weighted(ref.ENCOUNTER_TYPES)
    dispositions, disp_weights = ref.weighted(ref.DISPOSITIONS)

    emitted = 0
    chunk_index = 0
    while emitted < scale.encounters:
        size = min(config.chunk_rows, scale.encounters - emitted)
        rng = derive_rng(config.seed, "encounter", chunk_index)
        offset = np.arange(emitted, emitted + size)

        # Heavy-tailed patient utilisation: a minority of patients generate a
        # large share of encounters, which is both realistic and the source of the
        # partition skew that experiment E2 measures.
        patient_index = zipf_choice(rng, scale.patients, size, a=1.25)

        admit_seconds = diurnal_offsets(rng, size, days=config.days)
        admit_ts = to_timestamps(admit_seconds, config.start_date)

        encounter_type = rng.choice(types, size=size, p=type_weights)
        # Length of stay depends on encounter type; an inpatient stay is days, an
        # outpatient visit is minutes.
        los_minutes = np.where(
            encounter_type == "inpatient",
            rng.gamma(shape=2.0, scale=1800.0, size=size),
            np.where(
                encounter_type == "emergency",
                rng.gamma(shape=2.0, scale=140.0, size=size),
                np.where(
                    encounter_type == "observation",
                    rng.gamma(shape=2.0, scale=420.0, size=size),
                    rng.gamma(shape=2.0, scale=14.0, size=size),
                ),
            ),
        )
        discharge_ts = admit_ts + pd.to_timedelta(np.round(los_minutes).astype(np.int64), unit="m")

        primary_idx = rng.choice(len(codes), size=size, p=code_weights)
        secondary_count = rng.choice([0, 1, 2, 3], size=size, p=[0.45, 0.30, 0.17, 0.08])
        secondary = [
            ";".join(rng.choice(codes, size=n, replace=False)) if n else ""
            for n in secondary_count
        ]

        ingest_seconds, is_late = apply_late_arrival(
            rng,
            admit_seconds,
            late_rate=config.late_arrival_rate,
            max_late_hours=config.max_late_hours,
        )

        frame = pd.DataFrame(
            {
                "encounter_id": encounter_id(offset),
                "patient_id": patient_id(patient_index),
                "provider_id": provider_id(rng.integers(0, scale.providers, size=size)),
                "facility_id": facility_id(rng.integers(0, scale.facilities, size=size)),
                "encounter_type": encounter_type,
                "admit_ts": admit_ts,
                "discharge_ts": discharge_ts,
                "primary_diagnosis_code": [codes[i] for i in primary_idx],
                "diagnosis_system": [systems[i] for i in primary_idx],
                "secondary_diagnosis_codes": secondary,
                "disposition": rng.choice(dispositions, size=size, p=disp_weights),
                "triage_level": rng.integers(1, 6, size=size),
                "event_ts": admit_ts,
                "ingest_ts": to_timestamps(ingest_seconds, config.start_date),
                "is_late_arrival": is_late,
                "source_system": "EHR-CORE",
            }
        )

        frame, manifest = injector.inject(frame, rng, chunk_index)
        yield Chunk("encounter", Tenant.HEALTHCARE, chunk_index, frame, manifest)

        emitted += size
        chunk_index += 1


def generate_claim_lines(config: GeneratorConfig, scale: HealthcareScale) -> Iterator[Chunk]:
    """Claim lines derived from encounters.

    Encounter identifiers are recomputed from their index rather than joined,
    so this scales to millions of encounters without materialising them.
    """
    plan = _claim_defect_plan()
    injector = injector_for(plan, config)

    cpt_codes = [c for c, _, _ in ref.CPT_CODES]
    cpt_charges = np.array([c for _, _, c in ref.CPT_CODES], dtype=float)

    # Average 2.4 lines per encounter.
    total = int(scale.encounters * 2.4)
    emitted = 0
    chunk_index = 0

    while emitted < total:
        size = min(config.chunk_rows, total - emitted)
        rng = derive_rng(config.seed, "claim_line", chunk_index)
        offset = np.arange(emitted, emitted + size)

        source_encounter = rng.integers(0, scale.encounters, size=size)
        cpt_idx = rng.integers(0, len(cpt_codes), size=size)

        # Billed amount varies around the procedure's typical charge; log-normal
        # noise keeps the right skew that makes outlier detection non-trivial.
        billed = np.round(
            cpt_charges[cpt_idx] * rng.lognormal(mean=0.0, sigma=0.35, size=size), 2
        )
        allowed = np.round(billed * rng.uniform(0.35, 0.85, size=size), 2)
        is_denied = rng.uniform(size=size) < 0.082
        paid = np.where(is_denied, 0.0, np.round(allowed * rng.uniform(0.78, 1.0, size=size), 2))

        service_seconds = rng.uniform(0, config.days * 86400, size=size)
        service_ts = to_timestamps(service_seconds, config.start_date)

        frame = pd.DataFrame(
            {
                "claim_line_id": np.char.add("CL-", np.char.zfill(offset.astype(str), 12)),
                "claim_number": np.char.add(
                    "CLM-", np.char.zfill((offset // 3).astype(str), 11)
                ),
                "encounter_id": encounter_id(source_encounter),
                "line_number": (offset % 3 + 1),
                "procedure_code": [cpt_codes[i] for i in cpt_idx],
                "units": rng.choice([1, 1, 1, 2, 3], size=size),
                "billed_amount": billed,
                "allowed_amount": allowed,
                "paid_amount": paid,
                "patient_responsibility": np.round(np.maximum(allowed - paid, 0.0), 2),
                "is_denied": is_denied,
                "denial_reason": pd.Series(
                    rng.choice(ref.DENIAL_REASONS, size=size), dtype="object"
                ).where(pd.Series(is_denied), other=None),
                "service_date": service_ts.normalize(),
                "event_ts": service_ts,
                "source_system": "CLAIMS-EDI",
            }
        )

        frame, manifest = injector.inject(frame, rng, chunk_index)
        yield Chunk("claim_line", Tenant.HEALTHCARE, chunk_index, frame, manifest)

        emitted += size
        chunk_index += 1


# --------------------------------------------------------------- defect plans


def _patient_defect_plan() -> DefectPlan:
    return DefectPlan(
        dataset="patient",
        tenant=Tenant.HEALTHCARE.value,
        key_field="patient_id",
        fields=(
            # No CASE_INCONSISTENCY on names: the platform's casing convention
            # applies to *categorical* columns, which Silver lower-cases. A patient
            # called JANE is unusual, not wrong, and no rule claims otherwise --
            # so injecting it would be a defect nothing is allowed to catch.
            FieldRole("first_name", (DefectType.NULL_VIOLATION, DefectType.WHITESPACE_PADDING,
                                     DefectType.ENCODING_CORRUPTION)),
            FieldRole("last_name", (DefectType.WHITESPACE_PADDING,
                                    DefectType.ENCODING_CORRUPTION)),
            # No NULL_VIOLATION on these three: the contract marks them nullable,
            # so a missing patient email is permitted data, not a defect. Injecting
            # one would put a defect in the answer key that no derived completeness
            # rule can ever exist for. See test_generators.py's plan/contract
            # agreement test, which pins this.
            FieldRole("email", (DefectType.FORMAT_VIOLATION,)),
            FieldRole("phone", (DefectType.FORMAT_VIOLATION,)),
            FieldRole("ssn", (DefectType.FORMAT_VIOLATION,)),
            FieldRole("postal_code", (DefectType.FORMAT_VIOLATION, DefectType.WHITESPACE_PADDING)),
            # `state` is a two-letter code that is upper-case by convention, so
            # upper-casing it was a no-op the manifest recorded as a defect.
            FieldRole("state", (DefectType.WHITESPACE_PADDING,)),
            FieldRole("plan_id", (DefectType.REFERENTIAL_ORPHAN,)),
            FieldRole("primary_care_provider_id", (DefectType.REFERENTIAL_ORPHAN,)),
        ),
    )


def _encounter_defect_plan() -> DefectPlan:
    return DefectPlan(
        dataset="encounter",
        tenant=Tenant.HEALTHCARE.value,
        key_field="encounter_id",
        fields=(
            FieldRole("patient_id", (DefectType.REFERENTIAL_ORPHAN,)),
            FieldRole("provider_id", (DefectType.REFERENTIAL_ORPHAN,)),
            FieldRole("facility_id", (DefectType.REFERENTIAL_ORPHAN,)),
            FieldRole("encounter_type", (DefectType.CASE_INCONSISTENCY, DefectType.NULL_VIOLATION)),
            FieldRole("primary_diagnosis_code", (DefectType.NULL_VIOLATION,
                                                 DefectType.FORMAT_VIOLATION,
                                                 DefectType.WHITESPACE_PADDING)),
            FieldRole("disposition", (DefectType.CASE_INCONSISTENCY,)),
            FieldRole("triage_level", (DefectType.RANGE_VIOLATION, DefectType.TYPE_DRIFT)),
        ),
        temporal_pairs=(("admit_ts", "discharge_ts"),),
    )


def _claim_defect_plan() -> DefectPlan:
    return DefectPlan(
        dataset="claim_line",
        tenant=Tenant.HEALTHCARE.value,
        key_field="claim_line_id",
        fields=(
            FieldRole("encounter_id", (DefectType.REFERENTIAL_ORPHAN,)),
            FieldRole("procedure_code", (DefectType.NULL_VIOLATION, DefectType.FORMAT_VIOLATION,
                                         DefectType.WHITESPACE_PADDING)),
            FieldRole("billed_amount", (DefectType.RANGE_VIOLATION, DefectType.UNIT_INCONSISTENCY,
                                        DefectType.NULL_VIOLATION, DefectType.TYPE_DRIFT)),
            FieldRole("allowed_amount", (DefectType.RANGE_VIOLATION,
                                         DefectType.UNIT_INCONSISTENCY)),
            FieldRole("paid_amount", (DefectType.RANGE_VIOLATION,)),
            FieldRole("denial_reason", (DefectType.WHITESPACE_PADDING,)),
        ),
    )


def defect_plans() -> list[DefectPlan]:
    """Every healthcare defect plan, for cross-checking against the contracts.

    A plan that targets a column the contract permits to be null (or does not
    have) injects a defect no derived rule can ever exist for. That is a permanent
    false negative, invisible until Stage 7b measured it.
    """
    return [_patient_defect_plan(), _encounter_defect_plan(), _claim_defect_plan()]
