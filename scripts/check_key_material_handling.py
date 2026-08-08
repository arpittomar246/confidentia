"""Guard: key material must never reach SparkConf.

Every ``spark.*`` property is rendered in the Spark UI's Environment tab and
returned by ``sc.getConf().toDebugString()``. Spark's default
``spark.redaction.regex`` matches only ``secret|password|token``, so a property
named ``...encryption.key.list`` would be displayed in full — base64 master keys,
in a browser. Key material therefore goes on the Hadoop Configuration after session
creation, which the UI does not display.

Uses the AST rather than a text search, because the modules that explain this rule
necessarily quote the forbidden call in their docstrings, and a grep cannot tell
prose from code.

Run in CI.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Properties that carry secrets and must not be set through SparkConf.
FORBIDDEN_IN_SPARKCONF = {
    "spark.hadoop.parquet.encryption.key.list",
    "spark.hadoop.parquet.encryption.kms.key",
}

# Attribute calls that write into SparkConf.
SPARKCONF_SETTERS = {"config", "set", "setIfMissing"}


def string_args(call: ast.Call) -> list[str]:
    values = [a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    values += [
        kw.value.value
        for kw in call.keywords
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
    ]
    return values


def scan(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [(exc.lineno or 0, f"could not parse: {exc}")]

    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in SPARKCONF_SETTERS:
            continue
        # Hadoop Configuration also exposes .set(); distinguish by the property
        # name, since only spark.* properties reach the UI.
        for value in string_args(node):
            if value in FORBIDDEN_IN_SPARKCONF:
                findings.append(
                    (node.lineno, f"{value!r} set via .{node.func.attr}() — would reach the Spark UI")
                )
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    violations = 0
    for path in sorted((root / "src").rglob("*.py")):
        for lineno, message in scan(path):
            print(f"{path.relative_to(root)}:{lineno}: {message}")
            violations += 1

    if violations:
        print(f"\nkey material handling FAILED ({violations} violation(s))")
        return 1
    print("key material never reaches SparkConf.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
