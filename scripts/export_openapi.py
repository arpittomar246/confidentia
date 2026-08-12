#!/usr/bin/env python3
"""Write the OpenAPI schema to docs/api/openapi.json, or check it is current.

The schema is committed so that Stage 11 can generate a typed client from it
without standing up the API, and so that a change to a response shape appears in a
diff rather than being discovered by the dashboard at runtime.

``--check`` is what CI runs. It regenerates the schema in memory and compares; a
mismatch means someone changed a route and did not re-export, which is precisely
the drift the committed file exists to prevent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = REPO_ROOT / "docs" / "api" / "openapi.json"


def render() -> str:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from confidentia.api.app import create_app

    # sort_keys so the file is stable across runs: FastAPI builds the schema from
    # dict iteration over routes, and an unstable key order would make every export
    # a diff.
    return json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the file is stale")
    args = parser.parse_args()

    current = render()
    if args.check:
        if not TARGET.exists():
            print(f"FAIL {TARGET.relative_to(REPO_ROOT)} does not exist")
            print("     run: python scripts/export_openapi.py")
            return 1
        if TARGET.read_text(encoding="utf-8") != current:
            print(f"FAIL {TARGET.relative_to(REPO_ROOT)} is out of date")
            print("     run: python scripts/export_openapi.py")
            return 1
        paths = len(json.loads(current)["paths"])
        print(f"ok   openapi.json matches the application ({paths} paths)")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(current, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(REPO_ROOT)} ({len(json.loads(current)['paths'])} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
