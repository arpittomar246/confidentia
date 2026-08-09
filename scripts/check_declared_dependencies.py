"""Guard: every third-party import in src/ must be declared in pyproject.toml.

This exists because two dependencies -- `psycopg-pool` and `httpx` -- were
installed by hand into the development environment and never declared. Everything
worked locally and failed in CI, which is the worst version of this bug: the
project appeared to work while being uninstallable by anyone else, including a
future examiner cloning the repository.

Walks the AST of every module under src/, collects top-level import roots, and
checks each against the standard library, the project's own package, and the union
of all declared dependencies and optional extras.

Run in CI.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
PYPROJECT = REPO_ROOT / "pyproject.toml"

FIRST_PARTY = {"confidentia"}

# Import name -> distribution name, where they differ.
IMPORT_TO_DISTRIBUTION = {
    "psycopg_pool": "psycopg-pool",
    "psycopg2": "psycopg2-binary",
    "pyspark": "pyspark",
    "delta": "delta-spark",
    "deltalake": "deltalake",
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "jwt": "pyjwt",
    "faker": "faker",
    "dateutil": "python-dateutil",
    "sklearn": "scikit-learn",
    "cryptography": "cryptography",
    "argon2": "argon2-cffi",
    "multipart": "python-multipart",
    "prometheus_client": "prometheus-client",
    "confluent_kafka": "confluent-kafka",
}


def declared_distributions() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    specs = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        specs.extend(extra)

    names = set()
    for spec in specs:
        # "psycopg[binary]>=3.2" -> "psycopg"
        name = re.split(r"[<>=!\[;\s]", spec, maxsplit=1)[0]
        names.add(name.lower().replace("_", "-"))
    return names


def imported_roots() -> dict[str, set[str]]:
    roots: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots.setdefault(alias.name.split(".")[0], set()).add(
                        str(path.relative_to(REPO_ROOT))
                    )
            elif isinstance(node, ast.ImportFrom):
                # `from . import x` has no module root to resolve.
                if node.level == 0 and node.module:
                    roots.setdefault(node.module.split(".")[0], set()).add(
                        str(path.relative_to(REPO_ROOT))
                    )
    return roots


def main() -> int:
    declared = declared_distributions()
    stdlib = set(sys.stdlib_module_names)
    undeclared: list[tuple[str, str, set[str]]] = []

    for root, files in sorted(imported_roots().items()):
        if root in stdlib or root in FIRST_PARTY or root.startswith("_"):
            continue
        distribution = IMPORT_TO_DISTRIBUTION.get(root, root).lower().replace("_", "-")
        if distribution not in declared:
            undeclared.append((root, distribution, files))

    if undeclared:
        print("Undeclared third-party imports:\n")
        for root, distribution, files in undeclared:
            sample = sorted(files)[:3]
            print(f"  import {root!r} (distribution {distribution!r})")
            for f in sample:
                print(f"      {f}")
        print(
            "\nAdd each to [project.dependencies] or an extra in pyproject.toml. "
            "An undeclared dependency makes the project uninstallable by anyone "
            "who did not happen to pip install it by hand."
        )
        return 1

    print(f"all third-party imports are declared ({len(declared)} distributions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
