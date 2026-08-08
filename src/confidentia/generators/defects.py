"""Deliberate data defect injection, with a ground-truth manifest.

Most academic data quality layers are demonstrated rather than evaluated: rules
run, some rows are flagged, and nobody can say whether the flagged rows are the
*right* rows. This module fixes that. Every corruption is recorded — dataset,
record key, field, original value, corrupted value, defect type, severity — so
Stage 7 can be scored with precision and recall per check family against a known
answer key.

That turns a claim ("the quality framework catches bad data") into a measurement
("recall 0.98 on null violations, 0.71 on unit inconsistencies, and here is why
the second number is low"). The low numbers are the interesting ones.

Design constraints:

* **The record key is never corrupted.** If the key moved, the manifest could not
  be joined back to the data and nothing would be measurable.
* **Defect types map one-to-one onto Stage 7 check families**, so the scoring join
  needs no translation layer.
* **Injection is seeded and vectorised**, so a 5M-row dataset can be corrupted at a
  known rate without a Python loop over rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import pandas as pd


class DefectType(StrEnum):
    """One per Stage 7 data quality check family."""

    NULL_VIOLATION = "null_violation"
    DUPLICATE_RECORD = "duplicate_record"
    FORMAT_VIOLATION = "format_violation"
    RANGE_VIOLATION = "range_violation"
    REFERENTIAL_ORPHAN = "referential_orphan"
    TYPE_DRIFT = "type_drift"
    ENCODING_CORRUPTION = "encoding_corruption"
    WHITESPACE_PADDING = "whitespace_padding"
    CASE_INCONSISTENCY = "case_inconsistency"
    UNIT_INCONSISTENCY = "unit_inconsistency"
    TEMPORAL_INCONSISTENCY = "temporal_inconsistency"


class Severity(StrEnum):
    CRITICAL = "critical"  # record is unusable; must be quarantined
    MAJOR = "major"        # record is usable but wrong; must be flagged
    MINOR = "minor"        # cosmetic; should be repaired in Silver, not quarantined


SEVERITY: dict[DefectType, Severity] = {
    DefectType.NULL_VIOLATION: Severity.CRITICAL,
    DefectType.REFERENTIAL_ORPHAN: Severity.CRITICAL,
    DefectType.TYPE_DRIFT: Severity.CRITICAL,
    DefectType.DUPLICATE_RECORD: Severity.MAJOR,
    DefectType.RANGE_VIOLATION: Severity.MAJOR,
    DefectType.UNIT_INCONSISTENCY: Severity.MAJOR,
    DefectType.TEMPORAL_INCONSISTENCY: Severity.MAJOR,
    DefectType.FORMAT_VIOLATION: Severity.MAJOR,
    DefectType.ENCODING_CORRUPTION: Severity.MINOR,
    DefectType.WHITESPACE_PADDING: Severity.MINOR,
    DefectType.CASE_INCONSISTENCY: Severity.MINOR,
}

# Relative share of the defect budget. Roughly mirrors what real ingestion sees:
# nulls and whitespace are common, referential breaks and type drift are rare but
# expensive. Deliberately uneven, so that per-family recall is measured on both
# well-populated and sparse classes.
DEFAULT_MIX: dict[DefectType, float] = {
    DefectType.NULL_VIOLATION: 0.22,
    DefectType.WHITESPACE_PADDING: 0.16,
    DefectType.CASE_INCONSISTENCY: 0.12,
    DefectType.FORMAT_VIOLATION: 0.12,
    DefectType.DUPLICATE_RECORD: 0.10,
    DefectType.RANGE_VIOLATION: 0.09,
    DefectType.TEMPORAL_INCONSISTENCY: 0.06,
    DefectType.ENCODING_CORRUPTION: 0.05,
    DefectType.UNIT_INCONSISTENCY: 0.04,
    DefectType.REFERENTIAL_ORPHAN: 0.03,
    DefectType.TYPE_DRIFT: 0.01,
}

MANIFEST_COLUMNS = (
    "dataset",
    "tenant",
    "chunk_index",
    "record_key",
    "defect_type",
    "severity",
    "field",
    "original_value",
    "corrupted_value",
)

# Mojibake produced by decoding UTF-8 as Latin-1 -- the classic pipeline encoding
# bug, and one that survives naive validation because the result is still a string.
_MOJIBAKE = {"é": "Ã©", "ñ": "Ã±", "ö": "Ã¶", "á": "Ã¡", "ü": "Ã¼"}


@dataclass(frozen=True)
class FieldRole:
    """How a column may be corrupted.

    Explicit rather than inferred from dtype, because the right corruption depends
    on meaning: putting a negative number in ``billed_amount`` is a range
    violation, while putting one in ``latitude`` is not.
    """

    name: str
    kinds: tuple[DefectType, ...]


@dataclass(frozen=True)
class DefectPlan:
    dataset: str
    tenant: str
    key_field: str
    fields: tuple[FieldRole, ...]
    # Pairs of (start_field, end_field) that must stay ordered in time.
    temporal_pairs: tuple[tuple[str, str], ...] = ()

    def fields_for(self, kind: DefectType) -> list[str]:
        return [f.name for f in self.fields if kind in f.kinds and f.name != self.key_field]


class DefectInjector:
    """Applies a defect budget to a chunk and records what it did."""

    def __init__(
        self,
        plan: DefectPlan,
        *,
        rate: float,
        enabled: frozenset[DefectType] | None = None,
        mix: dict[DefectType, float] | None = None,
    ) -> None:
        self.plan = plan
        self.rate = rate
        # TYPE_DRIFT is excluded by default: a Parquet column has a fixed physical
        # type, so a string cannot be smuggled into a numeric column. It is enabled
        # only for CSV and JSON outputs, where the defect is actually expressible.
        self.enabled = enabled if enabled is not None else frozenset(
            DEFAULT_MIX.keys() - {DefectType.TYPE_DRIFT}
        )
        self.mix = mix or DEFAULT_MIX

    def _budget(self, rows: int) -> dict[DefectType, int]:
        """Split the defect budget across enabled types, normalised over what is
        actually applicable to this dataset."""
        applicable = {
            kind: weight
            for kind, weight in self.mix.items()
            if kind in self.enabled
            and (
                self.plan.fields_for(kind)
                or kind is DefectType.DUPLICATE_RECORD
                or (kind is DefectType.TEMPORAL_INCONSISTENCY and self.plan.temporal_pairs)
            )
        }
        total_weight = sum(applicable.values())
        if total_weight == 0:
            return {}
        target = round(rows * self.rate)
        return {
            kind: max(0, round(target * weight / total_weight))
            for kind, weight in applicable.items()
        }

    def inject(
        self, frame: pd.DataFrame, rng: np.random.Generator, chunk_index: int
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        if self.rate <= 0 or frame.empty:
            return frame, _empty_manifest()

        frame = frame.copy()
        records: list[dict[str, object]] = []
        budget = self._budget(len(frame))

        for kind, count in budget.items():
            if count <= 0:
                continue
            handler = getattr(self, f"_apply_{kind.value}", None)
            if handler is None:
                continue
            frame = handler(frame, rng, count, records, chunk_index)

        manifest = (
            pd.DataFrame(records, columns=list(MANIFEST_COLUMNS))
            if records
            else _empty_manifest()
        )
        return frame, manifest

    # ------------------------------------------------------------- helpers

    def _record(
        self,
        records: list[dict[str, object]],
        chunk_index: int,
        keys: pd.Series,
        kind: DefectType,
        field: str,
        originals: pd.Series,
        corrupted: pd.Series | str,
    ) -> None:
        corrupted_values = (
            corrupted.astype(str).tolist()
            if isinstance(corrupted, pd.Series)
            else [corrupted] * len(keys)
        )
        for key, original, corrupt in zip(
            keys.astype(str).tolist(),
            originals.astype(str).tolist(),
            corrupted_values,
            strict=True,
        ):
            records.append(
                {
                    "dataset": self.plan.dataset,
                    "tenant": self.plan.tenant,
                    "chunk_index": chunk_index,
                    "record_key": key,
                    "defect_type": kind.value,
                    "severity": SEVERITY[kind].value,
                    "field": field,
                    "original_value": original,
                    "corrupted_value": corrupt,
                }
            )

    def _pick(
        self, frame: pd.DataFrame, rng: np.random.Generator, kind: DefectType, count: int
    ) -> tuple[str, np.ndarray] | None:
        fields = self.plan.fields_for(kind)
        if not fields or count <= 0:
            return None
        field = fields[rng.integers(0, len(fields))]
        count = min(count, len(frame))
        positions = rng.choice(len(frame), size=count, replace=False)
        return field, positions

    # -------------------------------------------------------- defect kinds

    def _apply_null_violation(self, frame, rng, count, records, chunk_index):
        picked = self._pick(frame, rng, DefectType.NULL_VIOLATION, count)
        if picked is None:
            return frame
        field, positions = picked
        keys = frame.iloc[positions][self.plan.key_field]
        originals = frame.iloc[positions][field]
        frame.iloc[positions, frame.columns.get_loc(field)] = None
        self._record(
            records, chunk_index, keys, DefectType.NULL_VIOLATION, field, originals, "None"
        )
        return frame

    def _apply_whitespace_padding(self, frame, rng, count, records, chunk_index):
        picked = self._pick(frame, rng, DefectType.WHITESPACE_PADDING, count)
        if picked is None:
            return frame
        field, positions = picked
        keys = frame.iloc[positions][self.plan.key_field]
        originals = frame.iloc[positions][field]
        corrupted = originals.astype(str).map(lambda v: f"  {v}\t")
        frame.iloc[positions, frame.columns.get_loc(field)] = corrupted.to_numpy()
        self._record(
            records, chunk_index, keys, DefectType.WHITESPACE_PADDING, field, originals, corrupted
        )
        return frame

    def _apply_case_inconsistency(self, frame, rng, count, records, chunk_index):
        picked = self._pick(frame, rng, DefectType.CASE_INCONSISTENCY, count)
        if picked is None:
            return frame
        field, positions = picked
        keys = frame.iloc[positions][self.plan.key_field]
        originals = frame.iloc[positions][field]
        corrupted = originals.astype(str).str.upper()
        frame.iloc[positions, frame.columns.get_loc(field)] = corrupted.to_numpy()
        self._record(
            records, chunk_index, keys, DefectType.CASE_INCONSISTENCY, field, originals, corrupted
        )
        return frame

    def _apply_encoding_corruption(self, frame, rng, count, records, chunk_index):
        picked = self._pick(frame, rng, DefectType.ENCODING_CORRUPTION, count)
        if picked is None:
            return frame
        field, positions = picked
        keys = frame.iloc[positions][self.plan.key_field]
        originals = frame.iloc[positions][field]

        def mangle(value: str) -> str:
            # Force a mojibake sequence even into pure-ASCII values, so the defect
            # is present at the requested rate rather than only where accents occur.
            text = str(value)
            for accented, broken in _MOJIBAKE.items():
                if accented in text:
                    return text.replace(accented, broken)
            return text[:1] + "Ã©" + text[1:] if text else "Ã©"

        corrupted = originals.astype(str).map(mangle)
        frame.iloc[positions, frame.columns.get_loc(field)] = corrupted.to_numpy()
        self._record(
            records, chunk_index, keys, DefectType.ENCODING_CORRUPTION, field, originals, corrupted
        )
        return frame

    def _apply_format_violation(self, frame, rng, count, records, chunk_index):
        picked = self._pick(frame, rng, DefectType.FORMAT_VIOLATION, count)
        if picked is None:
            return frame
        field, positions = picked
        keys = frame.iloc[positions][self.plan.key_field]
        originals = frame.iloc[positions][field]
        # Break the structure without destroying the value, which is what a real
        # upstream formatting bug looks like: still parseable as text, no longer
        # matching the contract's pattern.
        corrupted = originals.map(_break_format)
        frame.iloc[positions, frame.columns.get_loc(field)] = corrupted.to_numpy()
        self._record(
            records, chunk_index, keys, DefectType.FORMAT_VIOLATION, field, originals, corrupted
        )
        return frame

    def _apply_range_violation(self, frame, rng, count, records, chunk_index):
        picked = self._pick(frame, rng, DefectType.RANGE_VIOLATION, count)
        if picked is None:
            return frame
        field, positions = picked
        keys = frame.iloc[positions][self.plan.key_field]
        originals = frame.iloc[positions][field]
        if pd.api.types.is_numeric_dtype(frame[field]):
            corrupted = originals.astype(float).abs() * -1.0 - 1.0
        else:
            return frame
        frame.iloc[positions, frame.columns.get_loc(field)] = corrupted.to_numpy()
        self._record(
            records, chunk_index, keys, DefectType.RANGE_VIOLATION, field, originals, corrupted
        )
        return frame

    def _apply_unit_inconsistency(self, frame, rng, count, records, chunk_index):
        picked = self._pick(frame, rng, DefectType.UNIT_INCONSISTENCY, count)
        if picked is None:
            return frame
        field, positions = picked
        if not pd.api.types.is_numeric_dtype(frame[field]):
            return frame
        keys = frame.iloc[positions][self.plan.key_field]
        originals = frame.iloc[positions][field]
        # Cents reported where dollars are expected. Nasty precisely because the
        # value stays positive and in-range -- only distribution-aware checks or a
        # cross-field reconciliation will catch it.
        corrupted = originals.astype(float) * 100.0
        frame.iloc[positions, frame.columns.get_loc(field)] = corrupted.to_numpy()
        self._record(
            records, chunk_index, keys, DefectType.UNIT_INCONSISTENCY, field, originals, corrupted
        )
        return frame

    def _apply_referential_orphan(self, frame, rng, count, records, chunk_index):
        picked = self._pick(frame, rng, DefectType.REFERENTIAL_ORPHAN, count)
        if picked is None:
            return frame
        field, positions = picked
        keys = frame.iloc[positions][self.plan.key_field]
        originals = frame.iloc[positions][field]
        corrupted = pd.Series(
            [f"ORPHAN-{rng.integers(10**8, 10**9)}" for _ in positions], index=originals.index
        )
        frame[field] = frame[field].astype(object)
        frame.iloc[positions, frame.columns.get_loc(field)] = corrupted.to_numpy()
        self._record(
            records, chunk_index, keys, DefectType.REFERENTIAL_ORPHAN, field, originals, corrupted
        )
        return frame

    def _apply_type_drift(self, frame, rng, count, records, chunk_index):
        picked = self._pick(frame, rng, DefectType.TYPE_DRIFT, count)
        if picked is None:
            return frame
        field, positions = picked
        keys = frame.iloc[positions][self.plan.key_field]
        originals = frame.iloc[positions][field]
        corrupted = pd.Series(["N/A"] * len(positions), index=originals.index)
        frame[field] = frame[field].astype(object)
        frame.iloc[positions, frame.columns.get_loc(field)] = corrupted.to_numpy()
        self._record(
            records, chunk_index, keys, DefectType.TYPE_DRIFT, field, originals, corrupted
        )
        return frame

    def _apply_temporal_inconsistency(self, frame, rng, count, records, chunk_index):
        if not self.plan.temporal_pairs or count <= 0:
            return frame
        start_field, end_field = self.plan.temporal_pairs[
            rng.integers(0, len(self.plan.temporal_pairs))
        ]
        if start_field not in frame.columns or end_field not in frame.columns:
            return frame
        count = min(count, len(frame))
        positions = rng.choice(len(frame), size=count, replace=False)
        keys = frame.iloc[positions][self.plan.key_field]
        originals = frame.iloc[positions][end_field]
        # End precedes start by up to two days.
        corrupted = frame.iloc[positions][start_field] - pd.to_timedelta(
            rng.integers(1, 2880, size=count), unit="m"
        )
        frame.iloc[positions, frame.columns.get_loc(end_field)] = corrupted.to_numpy()
        self._record(
            records,
            chunk_index,
            keys,
            DefectType.TEMPORAL_INCONSISTENCY,
            end_field,
            originals,
            corrupted,
        )
        return frame

    def _apply_duplicate_record(self, frame, rng, count, records, chunk_index):
        """Append near-duplicates: same business key, cosmetically different.

        Exact copies are trivial to detect with a hash. Real duplicates arrive from
        a retried delivery or a double-submitted form and differ in whitespace or
        capitalisation, so deduplication has to key on business identity rather
        than on row content.
        """
        if count <= 0 or frame.empty:
            return frame
        count = min(count, len(frame))
        positions = rng.choice(len(frame), size=count, replace=False)
        duplicates = frame.iloc[positions].copy()

        text_columns = [
            c
            for c in duplicates.columns
            if duplicates[c].dtype == object and c != self.plan.key_field
        ]
        if text_columns:
            column = text_columns[rng.integers(0, len(text_columns))]
            duplicates[column] = duplicates[column].astype(str).str.strip().str.title()

        keys = duplicates[self.plan.key_field]
        self._record(
            records,
            chunk_index,
            keys,
            DefectType.DUPLICATE_RECORD,
            self.plan.key_field,
            keys,
            keys,
        )
        return pd.concat([frame, duplicates], ignore_index=True)


def injector_for(plan: DefectPlan, config: object) -> DefectInjector:
    """Build an injector whose enabled defect set matches the output format.

    ``TYPE_DRIFT`` is only expressible where the physical type is not fixed by the
    file format, i.e. CSV and JSON. Enabling it for Parquet would either fail the
    write or be silently coerced away, and a defect that cannot exist in the output
    must not appear in the ground-truth manifest — that would depress recall for a
    reason that has nothing to do with the quality layer.
    """
    types = set(DEFAULT_MIX)
    if not getattr(config, "enable_type_drift", False):
        types.discard(DefectType.TYPE_DRIFT)
    return DefectInjector(
        plan, rate=getattr(config, "defect_rate", 0.0), enabled=frozenset(types)
    )


def _break_format(value: object) -> str:
    """Corrupt a value's structure while keeping it a string.

    Must tolerate nulls: defect types are applied independently, so a field nulled
    by ``NULL_VIOLATION`` can legitimately be selected again here. Overlapping
    defects on one record are realistic and are recorded separately in the
    manifest, so the handler coerces rather than refusing.
    """
    if value is None or value is pd.NA or (isinstance(value, float) and pd.isna(value)):
        return "??"
    text = str(value).replace("@", "_at_").replace("-", "").replace(".", "")
    return text[:24] or "??"


def _empty_manifest() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in MANIFEST_COLUMNS})
