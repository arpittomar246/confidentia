"""Diagnostic: did anything actually get encrypted on disk?

Arm B of the spike only asserted that data round-tripped, which an unencrypted
write satisfies just as well. This checks the bytes directly:

* Parquet magic -- an encrypted-footer file starts and ends with ``PARE``;
  a normal one uses ``PAR1``.
* Plaintext search -- if a known sensitive value appears verbatim in the file,
  that column is not encrypted, whatever the magic says.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Values written by run_spike.make_dataframe. If these survive into the file,
# the corresponding column is in the clear.
NEEDLES = {
    "patient_name": b"Patient_1234",
    "mrn": b"MRN000001234",
    "diagnosis_code": b"J45.909",
    "note": b"clinical note body",
    "facility_id (expected plaintext)": b"FAC-7",
}


def inspect(directory: Path) -> None:
    files = sorted(directory.rglob("*.parquet"))
    if not files:
        print(f"  {directory.name}: no parquet files")
        return

    target = max(files, key=lambda f: f.stat().st_size)
    blob = target.read_bytes()
    head, tail = blob[:4], blob[-4:]

    if head == b"PARE" and tail == b"PARE":
        magic = "PARE  (encrypted footer)"
    elif head == b"PAR1" and tail == b"PAR1":
        magic = "PAR1  (plaintext footer)"
    else:
        magic = f"{head!r}/{tail!r}  (unrecognised)"

    print(f"\n  {directory.name}")
    print(f"    file   : {target.name}  ({len(blob):,} bytes)")
    print(f"    magic  : {magic}")
    for label, needle in NEEDLES.items():
        found = needle in blob
        verdict = "PLAINTEXT" if found else "not found"
        print(f"    {label:34s} {verdict}")


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/arpit/confidentia-data/spike")
    if not root.exists():
        print(f"No spike output at {root}")
        return 1

    print(f"Inspecting {root}")
    for name in ("a_plaintext", "b_pme", "d_delta_pme", "f_envelope"):
        directory = root / name
        if directory.exists():
            inspect(directory)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
