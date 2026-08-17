"""Build the pandoc reference document the dissertation is rendered through.

Pandoc's default reference.docx is legible but is not a dissertation: US Letter,
body text with no leading, and a Verbatim style at the same size as body text, which
wraps the 80-column architecture diagram into nonsense. This patches the handful of
styles that decide whether the output reads as a thesis and leaves everything else
at pandoc's defaults — a reference document that redefines every style is a template
nobody can debug later.

What it changes, and why:

- **A4, 25 mm margins, 10 mm gutter.** The institute template will prescribe its
  own; this is a sane default that binds.
- **Body text at 11pt with 1.15 leading.** Pandoc's default has none, which is hard
  to read across a 6.5-inch measure.
- **Code at 9pt Consolas.** The architecture diagram is 80 columns of box-drawing
  characters. At pandoc's 11pt it wraps, and a wrapped ASCII diagram is worse than
  no diagram. Consolas ships with Word and has the box-drawing glyphs; Courier New,
  the usual substitute, does not.
- **Page numbers, centred in the footer.** A printed dissertation without them is
  unusable in a viva.
- **Bordered tables with a bold, shaded header row.** Pandoc's default draws one
  rule under the header and nothing else. That is good typography and it is not
  what a dissertation is marked against; a five-column table of measurements reads
  better on paper with a grid. Done as conditional formatting on the `firstRow` of
  the table style, which every pandoc table opts into via `tblLook`, so no markdown
  changes and no per-table markup.

The patches are applied by locating each style element and rewriting it, rather than
by string replacement against a pretty-printed file whose whitespace is not a
contract. Anything not found is reported rather than silently skipped.

Run: python make_reference.py <out.docx>
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# A4 in twentieths of a point.
PAGE_W, PAGE_H = 11906, 16838
MARGIN = 1418   # 25 mm
GUTTER = 567    # 10 mm on the binding edge

# Consolas ships with Word and has the box-drawing glyphs the architecture diagram
# needs. Courier New, the usual substitute, does not. Declared once so the QA build
# can swap it for a monospace font that exists on this machine and actually prove
# the grid holds — a preview rendered in a substituted proportional font cannot.
CODE_FONTS = '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>'

NORMAL_PROPS = (
    '<w:pPr><w:spacing w:after="120" w:line="264" w:lineRule="auto"/></w:pPr>'
    '<w:rPr><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr>'
)

SOURCE_CODE_STYLE = (
    '<w:style w:type="paragraph" w:customStyle="1" w:styleId="SourceCode">'
    '<w:name w:val="Source Code"/><w:basedOn w:val="Normal"/><w:qFormat/>'
    '<w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/>'
    "<w:contextualSpacing/></w:pPr>"
    "<w:rPr>" + CODE_FONTS + '<w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>'
    "</w:style>"
)

# Table styling. Pandoc's default Table style carries a single rule under the header
# and no other borders. This replaces it with a grid and a bold, shaded header.
#
# The header formatting is conditional formatting on `firstRow` rather than direct
# run properties, because pandoc writes the header cells as plain runs — there is
# nothing to make bold at the document level without rewriting every table. Every
# pandoc table sets `<w:tblLook w:firstRow="1" .../>`, which is what opts each one
# into the block below.
GRID_COLOUR = "808080"    # 0.5pt grid, grey enough not to fight the text
RULE_COLOUR = "333333"    # 1pt rule under the header
HEADER_FILL = "EDEDED"

# Pandoc's default sets top and bottom cell margins to zero. That is fine for an
# unbordered table and cramped once there are lines to touch.
CELL_MARGIN_Y = 60        # 3pt
CELL_MARGIN_X = 108       # pandoc's default, kept


def border(edge: str, size: int, colour: str) -> str:
    return f'<w:{edge} w:val="single" w:sz="{size}" w:space="0" w:color="{colour}"/>'


TABLE_STYLE = (
    '<w:style w:type="table" w:default="1" w:styleId="Table">'
    '<w:name w:val="Table"/><w:basedOn w:val="TableNormal"/><w:qFormat/>'
    "<w:tblPr>"
    '<w:tblInd w:type="dxa" w:w="0"/>'
    "<w:tblBorders>"
    + "".join(
        border(edge, 4, GRID_COLOUR)
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    + "</w:tblBorders>"
    "<w:tblCellMar>"
    f'<w:top w:type="dxa" w:w="{CELL_MARGIN_Y}"/>'
    f'<w:left w:type="dxa" w:w="{CELL_MARGIN_X}"/>'
    f'<w:bottom w:type="dxa" w:w="{CELL_MARGIN_Y}"/>'
    f'<w:right w:type="dxa" w:w="{CELL_MARGIN_X}"/>'
    "</w:tblCellMar>"
    "</w:tblPr>"
    '<w:tblStylePr w:type="firstRow">'
    "<w:rPr><w:b/><w:bCs/></w:rPr>"
    "<w:tcPr>"
    "<w:tcBorders>" + border("bottom", 8, RULE_COLOUR) + "</w:tcBorders>"
    f'<w:shd w:val="clear" w:color="auto" w:fill="{HEADER_FILL}"/>'
    '<w:vAlign w:val="bottom"/>'
    "</w:tcPr>"
    "</w:tblStylePr>"
    "</w:style>"
)

FOOTER_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
    '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    '<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
    '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
    "<w:r><w:t>1</w:t></w:r>"
    '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    "</w:p></w:ftr>"
)

problems: list[str] = []


def find_style(xml: str, style_id: str) -> re.Match[str] | None:
    return re.search(
        r'<w:style [^>]*w:styleId="' + re.escape(style_id) + r'"[^>]*>.*?</w:style>',
        xml,
        re.S,
    )


def patch_styles(xml: str) -> str:
    # Normal: give body text a size and some leading.
    match = find_style(xml, "Normal")
    if match is None:
        problems.append("style Normal not found; body text left at pandoc's default")
    else:
        patched = match.group(0).replace("</w:style>", NORMAL_PROPS + "</w:style>")
        xml = xml[: match.start()] + patched + xml[match.end() :]

    # VerbatimChar: inline code, and — because a run-level style beats the
    # paragraph style it sits in — the runs inside every code block too. Setting
    # only SourceCode below leaves the diagram in the body serif, which is how the
    # first attempt at this document rendered.
    match = find_style(xml, "VerbatimChar")
    if match is None:
        problems.append("style VerbatimChar not found; code size left at pandoc's default")
    else:
        patched = re.sub(r'<w:sz w:val="\d+"\s*/>', '<w:sz w:val="18"/>', match.group(0))
        patched = re.sub(r"<w:rFonts[^/]*/>", CODE_FONTS, patched)
        if "szCs" not in patched:
            patched = patched.replace(
                '<w:sz w:val="18"/>', '<w:sz w:val="18"/><w:szCs w:val="18"/>'
            )
        xml = xml[: match.start()] + patched + xml[match.end() :]

    # SourceCode: pandoc generates this when absent, at a size that wraps the
    # diagram. Declaring it here wins.
    if find_style(xml, "SourceCode") is None:
        xml = xml.replace("</w:styles>", SOURCE_CODE_STYLE + "</w:styles>", 1)

    # Table: replaced wholesale rather than patched. The default is a rule under the
    # header and nothing else, so there is no border block to amend — and leaving the
    # original's firstRow rule in place alongside a new one gives the header two.
    match = find_style(xml, "Table")
    if match is None:
        problems.append("style Table not found; tables left unbordered")
    else:
        xml = xml[: match.start()] + TABLE_STYLE + xml[match.end() :]

    return xml


def patch_document(xml: str) -> str:
    """A4 with a binding gutter, and a footer reference for page numbers."""
    start = xml.find("<w:sectPr")
    end = xml.find("</w:sectPr>", start)
    if start == -1 or end == -1:
        problems.append("no sectPr found; page setup left at pandoc's default")
        return xml
    sect = (
        "<w:sectPr>"
        '<w:footerReference w:type="default" r:id="rIdFooter1"/>'
        f'<w:pgSz w:w="{PAGE_W}" w:h="{PAGE_H}"/>'
        f'<w:pgMar w:top="{MARGIN}" w:right="{MARGIN}" w:bottom="{MARGIN}" '
        f'w:left="{MARGIN}" w:header="708" w:footer="708" w:gutter="{GUTTER}"/>'
        "</w:sectPr>"
    )
    return xml[:start] + sect + xml[end + len("</w:sectPr>") :]


def add_footer(unpacked: Path) -> None:
    """Register footer2.xml: the part, its relationship, and its content type."""
    (unpacked / "word" / "footer_page.xml").write_text(FOOTER_XML, encoding="utf-8")

    rels = unpacked / "word" / "_rels" / "document.xml.rels"
    xml = rels.read_text(encoding="utf-8")
    entry = (
        '<Relationship Id="rIdFooter1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" '
        'Target="footer_page.xml"/>'
    )
    rels.write_text(xml.replace("</Relationships>", entry + "</Relationships>", 1), encoding="utf-8")

    types = unpacked / "[Content_Types].xml"
    xml = types.read_text(encoding="utf-8")
    override = (
        '<Override PartName="/word/footer_page.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'wordprocessingml.footer+xml"/>'
    )
    types.write_text(xml.replace("</Types>", override + "</Types>", 1), encoding="utf-8")


def enable_field_update(unpacked: Path) -> None:
    """Ask Word to refresh fields when the document opens.

    Pandoc writes the table of contents as a TOC *field*, not as text, so a freshly
    generated document shows an empty contents page until something populates it.
    With this flag Word offers to update on open. LibreOffice ignores it, which is
    why the rendered QA copy still shows the heading with nothing under it — that is
    the previewer, not the document.
    """
    settings = unpacked / "word" / "settings.xml"
    if not settings.exists():
        problems.append("no settings.xml; the contents page will need a manual F9")
        return
    xml = settings.read_text(encoding="utf-8")
    if "updateFields" in xml:
        return
    patched = re.sub(r"(<w:settings[^>]*>)", r'\1<w:updateFields w:val="true"/>', xml, count=1)
    if patched == xml:
        problems.append("could not set updateFields; the contents page will need a manual F9")
        return
    settings.write_text(patched, encoding="utf-8")


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "reference.docx").resolve()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        source = work / "default.docx"
        with source.open("wb") as handle:
            subprocess.run(
                ["pandoc", "--print-default-data-file", "reference.docx"],
                stdout=handle,
                check=True,
            )

        unpacked = work / "unpacked"
        with zipfile.ZipFile(source) as archive:
            archive.extractall(unpacked)

        styles = unpacked / "word" / "styles.xml"
        styles.write_text(patch_styles(styles.read_text(encoding="utf-8")), encoding="utf-8")

        add_footer(unpacked)
        enable_field_update(unpacked)
        document = unpacked / "word" / "document.xml"
        document.write_text(patch_document(document.read_text(encoding="utf-8")), encoding="utf-8")

        rebuilt = work / "reference.docx"
        with zipfile.ZipFile(rebuilt, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(unpacked.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(unpacked).as_posix())
        shutil.move(str(rebuilt), out)

    for problem in problems:
        print(f"  WARNING: {problem}")
    print(f"wrote {out}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
