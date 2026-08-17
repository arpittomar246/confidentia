"""Render final-report.md to Word.

`pandoc final-report.md -o final-report.docx` works and produces something you would
not submit: US Letter, no page numbers, the table of contents printed above the
title because the title is body text rather than metadata, and an 80-column ASCII
architecture diagram wrapped into noise by an 11pt code style.

It also keeps the two §6 evaluation tables from breaking across a page, which
bordering the tables made them tall enough to do.

This fixes those five things and nothing else. It does **not** attempt to be the
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
import zipfile
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


# The two evaluation tables in §6 are the ones a reader is sent to most often, and
# bordering the tables made them tall enough to break across a page. Word keeps a
# table whole by keeping every paragraph in it with the one that follows, so this sets
# keepNext on every row but the last — the last is left alone so the table does not
# also drag the paragraph after it around.
#
# Both are listed because holding one whole pushes the other down the page: fixing
# §6.2 alone moved §6.3's three-row table onto a page boundary, which is a worse break
# than the one being repaired.
#
# Identified by the text of the header row rather than by position, so editing the
# report above them cannot silently retarget this at some other table. If a header is
# ever reworded the build fails and says so, which is the point. Deliberately not
# applied to every table — the eleven-row ADR list and the eight-row throughput table
# would leave a half-empty page behind them.
KEEP_TOGETHER = (
    ("Tenant", "Dataset", "Precision", "Recall", "F1"),   # §6.2
    ("Reading", "Precision", "Recall", "F1"),             # §6.3
)


def spans(xml: str, tag: str) -> list[tuple[int, int]]:
    """Locate each top-level `<w:tag>...</w:tag>`, counting depth so nesting is safe."""
    opening = re.compile(rf"<w:{tag}(?:\s[^>]*)?>")
    closing = f"</w:{tag}>"
    found: list[tuple[int, int]] = []
    depth = at = index = 0
    while index < len(xml):
        match = opening.search(xml, index)
        close = xml.find(closing, index)
        if match is None and close == -1:
            break
        if match is not None and (close == -1 or match.start() < close):
            if depth == 0:
                at = match.start()
            depth += 1
            index = match.end()
        else:
            depth -= 1
            index = close + len(closing)
            if depth == 0:
                found.append((at, index))
    return found


def with_keep_next(paragraph: str) -> str:
    """Add `<w:keepNext/>` to one paragraph, respecting the order pPr children take."""
    if "<w:keepNext/>" in paragraph:
        return paragraph
    if "<w:pPr/>" in paragraph:
        return paragraph.replace("<w:pPr/>", "<w:pPr><w:keepNext/></w:pPr>", 1)

    start = paragraph.find("<w:pPr>")
    if start == -1:
        opening = re.match(r"<w:p(?:\s[^>]*)?>", paragraph)
        if opening is None:
            return paragraph
        return (
            paragraph[: opening.end()]
            + "<w:pPr><w:keepNext/></w:pPr>"
            + paragraph[opening.end() :]
        )

    # keepNext follows pStyle and precedes everything else pandoc writes.
    style = re.compile(r"<w:pStyle[^>]*/>").search(paragraph, start)
    end = paragraph.find("</w:pPr>", start)
    at = style.end() if style is not None and style.start() < end else start + len("<w:pPr>")
    return paragraph[:at] + "<w:keepNext/>" + paragraph[at:]


def keep_paragraphs_together(fragment: str) -> str:
    parts: list[str] = []
    last = 0
    for start, end in spans(fragment, "p"):
        parts.append(fragment[last:start])
        parts.append(with_keep_next(fragment[start:end]))
        last = end
    parts.append(fragment[last:])
    return "".join(parts)


def row_text(row: str) -> str:
    """Concatenate a row's visible text.

    The attribute pattern has to demand whitespace before it: `<w:t[^>]*>` happily
    matches `<w:trPr>` and `<w:tc>` as well, which returns a row of raw XML that
    matches no header and fails the build for the wrong reason.
    """
    return "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", row))


def keep_table_together(xml: str, header: tuple[str, ...]) -> str:
    """Stop the table with this header row splitting across a page break."""
    for start, end in spans(xml, "tbl"):
        table = xml[start:end]
        rows = spans(table, "tr")
        if not rows or row_text(table[rows[0][0] : rows[0][1]]) != "".join(header):
            continue

        # Every row but the last, so the table holds together without gluing itself
        # to whatever follows it.
        rebuilt, last = [], 0
        for row_start, row_end in rows[:-1]:
            rebuilt.append(table[last:row_start])
            rebuilt.append(keep_paragraphs_together(table[row_start:row_end]))
            last = row_end
        rebuilt.append(table[last:])
        table = "".join(rebuilt)

        # The sentence introducing the table travels with it; a lead-in stranded at
        # the foot of the previous page is the thing this was meant to prevent.
        head = xml[:start]
        before = spans(head, "p")
        if before:
            lead_start, lead_end = before[-1]
            head = (
                head[:lead_start]
                + with_keep_next(head[lead_start:lead_end])
                + head[lead_end:]
            )

        return head + table + xml[end:]

    raise SystemExit(
        "no table headed "
        + " | ".join(header)
        + " — that header was reworded or the table removed. Update KEEP_TOGETHER "
        "rather than shipping a report whose results tables split across a page."
    )


def apply_pagination(path: Path) -> None:
    """Rewrite document.xml inside the built file, leaving every other part byte-identical."""
    with zipfile.ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
        order = archive.namelist()

    document = parts["word/document.xml"].decode("utf-8")
    for header in KEEP_TOGETHER:
        document = keep_table_together(document, header)
    parts["word/document.xml"] = document.encode("utf-8")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in order:
            archive.writestr(name, parts[name])


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

    apply_pagination(out)

    print(f"wrote {out}")
    print("Open it in Word and press Ctrl+A then F9 to populate the contents page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
