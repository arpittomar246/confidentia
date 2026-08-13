"""C5 — the enclave placement planner.

Answers the project's research question: where in the pipeline should the
confidentiality boundary be drawn, and what does drawing it there cost?

The package is split so that provenance is visible from the import list.

- ``placement`` derives which stages must be inside from the schema contracts.
  It is a **derivation**: the answer would be the same on real SGX hardware.
- ``profile`` gathers **measured** inputs — row widths from Parquet footers,
  stage durations from the recorded run history, row counts from signed lineage.
- ``model`` turns those into **modelled** enclave costs. Nothing it produces is
  observed, and every result carries that label.
- ``planner`` combines them and compares three placements.

Nothing here is imported at package load: the planner pulls in pyarrow, and the
CLI must stay usable without it.
"""

from __future__ import annotations

__all__ = ["model", "placement", "planner", "profile"]
