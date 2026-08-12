#!/usr/bin/env python
"""Every command a DAG invokes must actually exist in the CLI.

The DAGs shell out to `confidentia ...` rather than importing the package
(ADR 0013), which buys isolation and costs a compile-time check: renaming a CLI
command leaves the DAG referring to one that no longer exists, and nothing notices
until a task fails at 03:00 with `No such command`.

This is the seam that check exists for. It parses the DAG files with `ast`,
extracts every `cli("...", "...")` call, and resolves the leading arguments
against the real Typer command tree. No Airflow needed, so it runs in CI in about
a second alongside the other AST guards.

What it deliberately does not check: whether the *options* are valid. Typer would
reject a bad option at runtime anyway, and parsing option arity from the AST would
duplicate Typer's own signature handling badly enough to be a second source of
truth.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DAGS_DIR = REPO_ROOT / "orchestration" / "dags"


def command_tree() -> dict[str, set[str] | None]:
    """The CLI as {group: {subcommands}} plus {command: None} for top-level ones."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from confidentia.cli import app

    def command_name(command: Any) -> str:
        # Typer leaves `name` as None when it is inferred from the function.
        return command.name or command.callback.__name__.replace("_", "-")

    tree: dict[str, set[str] | None] = {}
    for command in app.registered_commands:
        tree[command_name(command)] = None
    for group in app.registered_groups:
        tree[group.name] = {
            command_name(c) for c in group.typer_instance.registered_commands
        }
    return tree


def invocations(path: Path) -> list[tuple[int, list[str]]]:
    """Every `cli(...)` call in a DAG file, as (line, leading string arguments)."""
    found: list[tuple[int, list[str]]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "cli"):
            continue
        # Only the leading constant strings matter; stop at the first argument
        # that is not a literal, since anything after it is data rather than a
        # command path.
        args: list[str] = []
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                args.append(arg.value)
            else:
                break
        if args:
            found.append((node.lineno, args))
    return found


def main() -> int:
    if not DAGS_DIR.is_dir():
        print(f"no DAG directory at {DAGS_DIR}; nothing to check")
        return 0

    tree = command_tree()
    problems: list[str] = []
    checked = 0

    for path in sorted(DAGS_DIR.glob("*.py")):
        for line, args in invocations(path):
            checked += 1
            where = f"{path.relative_to(REPO_ROOT)}:{line}"
            head = args[0]

            if head not in tree:
                problems.append(f"  {where}: no CLI command or group named {head!r}")
                continue

            subcommands = tree[head]
            if subcommands is None:
                # A top-level command such as `runs`; nothing further to resolve.
                continue
            if len(args) < 2:
                problems.append(
                    f"  {where}: {head!r} is a command group and needs a subcommand "
                    f"(one of {sorted(subcommands)})"
                )
                continue
            if args[1] not in subcommands:
                problems.append(
                    f"  {where}: {head!r} has no subcommand {args[1]!r} "
                    f"(has {sorted(subcommands)})"
                )

    if problems:
        print(f"DAG/CLI contract FAILED ({len(problems)} of {checked} invocations)")
        print("\n".join(problems))
        print()
        print("A DAG invokes a command the CLI does not have. Either the command was")
        print("renamed and the DAG was not updated, or the DAG has a typo. Both fail")
        print("only at task runtime, which is why this runs in CI.")
        return 1

    print(f"DAG/CLI contract holds ({checked} invocations across the DAGs, all resolvable).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
