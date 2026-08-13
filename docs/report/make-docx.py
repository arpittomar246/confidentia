"""Render final-report.md to Word.

`pandoc final-report.md -o final-report.docx` works and produces something you would
not submit: US Letter, no page numbers, the table of contents printed above the
title because the title is body text rather than metadata, and an 80-column ASCII
architecture diagram wrapped into noise by an 11pt code style.

This fixes those four things and nothing else. It does **not** attempt to be the
institute's template — headings, margins and the title page are prescribed and this
script has no way to know them. What it produces is a clean, correctly paginated
document to drop that template onto.

The markdown stays the source of truth and is never modified. The title block is
lifted out of it into pandoc metadata at build time, which is what moves the
contents page below the title where it belongs. If that block is ever restructured
this script fails loudly rather than quietly emitting a document with two titles.

Run: python make-docx.py [out.docx]
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "final-report.md"


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Lift the title block out of the markdown into pandoc metadata.

    Expects, in order: an H1 title, a bold subtitle, a byline, and then the body
    beginning at the first '## ' heading. Everything between is carried through
    unchanged — the note on citations lives there and must survive.
    """
    lines = text.splitlines()

    def require(pattern: str, description: str) -> tuple[int, str]:
        for index, line in enumerate(lines):
            match = re.match(pattern, line)
            if match:
                return index, match.group(1).strip()
        raise SystemExit(
            f"final-report.md no longer starts with {description}. Update this script "
            "rather than letting it emit a document with two titles."
        )

    title_at, title = require(r"^#\s+(.*)$", "an H1 title")
    subtitle_at, subtitle = require(r"^\*\*(.+)\*\*\s*$", "a bold subtitle line")
    byline_at, byline = require(r"^(M\.Tech .*)$", "an 'M.Tech ...' byline")

    body_at = next(
        (i for i, line in enumerate(lines) if line.startswith("## ")),
        None,
    )
    if body_at is None:
        raise SystemExit("final-report.md has no '## ' section; nothing to render.")

    carried = [
        line
        for index, line in enumerate(lines[:body_at])
        if index not in {title_at, subtitle_at, byline_at} and line.strip() not in {"---", ""}
    ]
    body = "\n".join(carried) + "\n\n" + "\n".join(lines[body_at:])

    return {"title": title, "subtitle": subtitle, "author": byline}, body


# Glyphs the architecture diagram uses that Consolas does not contain. Word falls
# back to a proportional font for each one, and a single proportional glyph in a
# monospace grid shifts every character after it on that line — which is why the
# diagram arrives skewed even though nothing wrapped. Mapped to ASCII, which is
# guaranteed to be the same width as everything around it.
#
# Only applied inside fenced code blocks, and only in the Word build: the markdown
# keeps the better-looking glyphs for terminals and for GitHub.
CONSOLAS_SAFE = {
    "▶": ">",   # black right-pointing triangle
    "◀": "<",
    "▼": "v",
    "▲": "^",
}


def monospace_safe(body: str) -> str:
    lines = body.splitlines()
    inside = False
    for index, line in enumerate(lines):
        if line.startswith("```"):
            inside = not inside
            continue
        if inside:
            for bad, good in CONSOLAS_SAFE.items():
                line = line.replace(bad, good)
            lines[index] = line
    return "\n".join(lines) + "\n"


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "final-report.docx").resolve()
    reference = HERE / "reference.docx"

    if not reference.exists():
        print(f"building {reference.name}")
        subprocess.run(
            [sys.executable, str(HERE / "make-reference.py"), str(reference)], check=True
        )

    meta, body = split_front_matter(SOURCE.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "report.md"
        front = "\n".join(
            [
                "---",
                f'title: "{meta["title"]}"',
                f'subtitle: "{meta["subtitle"]}"',
                f'author: "{meta["author"]}"',
                "---",
                "",
            ]
        )
        staged.write_text(front + monospace_safe(body), encoding="utf-8")

        subprocess.run(
            [
                "pandoc",
                str(staged),
                "--from=gfm",
                "--to=docx",
                f"--reference-doc={reference}",
                "--toc",
                "--toc-depth=3",
                f"--output={out}",
            ],
            check=True,
        )

    print(f"wrote {out}")
    print("Open it in Word and press Ctrl+A then F9 to populate the contents page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
