"""Byte-level verification that the lakehouse encryption guarantee actually holds.

Round-trip correctness is not evidence of encryption -- an unencrypted write
round-trips perfectly well, which is how the first spike run produced a false pass.
This checks the bytes on disk instead, and asserts each table against its *expected*
outcome rather than merely printing what it found:

* Parquet magic -- ``PARE`` for an encrypted footer, ``PAR1`` otherwise.
* Plaintext search -- if a known sensitive value appears verbatim anywhere in the
  file, that column is in the clear whatever the magic says.

Two tables are expected to be plaintext and are kept deliberately: ``a_plaintext`` is
the baseline, and ``d_delta_pme`` preserves the trap documented in ADR 0007, where
Delta silently ignores encryption passed as DataFrameWriter options.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

# Values written by the generators in run_spike.py / probes.py. Their presence in a
# file means the corresponding column was never encrypted.
SENSITIVE_NEEDLES = {
    "patient_name": b"Patient_1234",
    "note": b"clinical note body",
    "diagnosis_code": b"J45.909",
}


@dataclass(frozen=True)
class Expectation:
    table: str
    encrypted_footer: bool
    sensitive_visible: bool
    why: str


EXPECTATIONS = (
    Expectation("a_plaintext", False, True, "baseline: no encryption requested"),
    Expectation(
        "d_delta_pme",
        False,
        True,
        "ADR 0007 trap: Delta ignores writer-option encryption, silently",
    ),
    Expectation("h_parquet_pme", True, False, "PME on Parquet"),
    Expectation("g_delta_hadoopconf", True, False, "PME on Delta via Hadoop Configuration"),
    Expectation(
        "f_envelope",
        False,
        False,
        "envelope columns: plaintext footer, ciphertext values",
    ),
)


def largest_parquet(directory: Path) -> Path | None:
    files = [f for f in directory.rglob("*.parquet") if f.is_file()]
    return max(files, key=lambda f: f.stat().st_size) if files else None


def check(root: Path, expectation: Expectation) -> bool | None:
    directory = root / expectation.table
    target = largest_parquet(directory) if directory.exists() else None
    if target is None:
        print(f"  {expectation.table:22s} SKIP   (not present)")
        return None

    blob = target.read_bytes()
    encrypted_footer = blob[:4] == b"PARE" and blob[-4:] == b"PARE"
    visible = [name for name, needle in SENSITIVE_NEEDLES.items() if needle in blob]

    footer_ok = encrypted_footer == expectation.encrypted_footer
    visibility_ok = bool(visible) == expectation.sensitive_visible
    ok = footer_ok and visibility_ok

    magic = "PARE" if encrypted_footer else blob[:4].decode(errors="replace")
    status = "OK" if ok else "MISMATCH"
    print(
        f"  {expectation.table:22s} {status:8s} magic={magic:5s} "
        f"visible={','.join(visible) if visible else 'none':<32s} "
        f"({len(blob):,} B)  -- {expectation.why}"
    )
    if not ok:
        if not footer_ok:
            print(
                f"      expected encrypted_footer={expectation.encrypted_footer}, "
                f"got {encrypted_footer}"
            )
        if not visibility_ok:
            print(
                f"      expected sensitive_visible={expectation.sensitive_visible}, "
                f"got {bool(visible)}"
            )
    return ok


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / "confidentia-data/spike")
    if not root.exists():
        print(f"No spike output at {root}")
        return 1

    print(f"\nByte-level verification of {root}\n")
    results = [check(root, expectation) for expectation in EXPECTATIONS]
    checked = [r for r in results if r is not None]
    failed = [r for r in checked if not r]

    print()
    if failed:
        print(f"  {len(failed)} of {len(checked)} tables did not match expectations")
        return 1
    print(f"  all {len(checked)} present tables match expectations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
