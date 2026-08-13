# Submission artefacts

| File | What it is |
|---|---|
| `final-report.md` | The dissertation body — abstract through conclusion, with two appendices. **The source of truth.** |
| `final-report.docx` | The same document in Word, built from the markdown by `make-docx.py` |
| `confidentia-viva.pptx` | 16-slide defence deck, with speaker notes on every slide |
| `viva-preparation.md` | Anticipated questions and answers, including the uncomfortable ones |
| `demo-script.md` | A rehearsed 12-minute live demonstration, with fallbacks |
| `build-deck.js` | Regenerates the deck: `node build-deck.js out.pptx` (needs `npm i pptxgenjs`) |
| `make-docx.py` | Regenerates the Word document: `python3 make-docx.py` (needs `pandoc`) |
| `make-reference.py` | Builds `reference.docx`, the Word styles the report is rendered through |

**Edit the markdown, then rebuild.** `final-report.docx` is a derived artefact and
any change made directly in Word is lost on the next build.

## The Word build

```bash
python3 make-docx.py
```

Four things it does that a bare `pandoc final-report.md -o final-report.docx` does
not, each because the bare version got them wrong:

- **A4, 25 mm margins, 10 mm binding gutter.** Pandoc defaults to US Letter.
- **Page numbers**, centred in the footer.
- **The contents page below the title, not above it.** The title lives in the
  markdown as body text, so pandoc puts the table of contents first. The build lifts
  the title block into document metadata instead. If that block is ever
  restructured, the script stops with an explanation rather than emitting a document
  with two titles.
- **A code style that does not destroy the architecture diagram.** It is 80 columns
  of box-drawing characters; at pandoc's default size it wraps into noise. Code is
  set in 9pt Consolas, which ships with Word and has the glyphs — Courier New, the
  usual substitute, does not. The four triangle glyphs Consolas lacks (`▶ ◀ ▼ ▲`)
  are swapped for ASCII inside fenced blocks at build time, because one
  proportional glyph in a monospace line shifts everything after it.

**Open it and press Ctrl+A then F9.** The contents page is a Word field, empty until
something populates it. Word offers to update on open; if you decline, that keystroke
does it.

> Verified by rendering, not assumed: the diagram's alignment was checked by building
> the same document against a monospace font installed on the build machine, because
> a preview that substitutes a proportional font for Consolas cannot show whether the
> grid holds.

## Two things to know before submitting

**The report has no references section.** Every factual claim in it comes from this
project's own code, measurements and commit history, and each is traceable to a
stage report or an ADR. The literature review and citations are yours to add —
nothing in the document should be attributed to a source it does not name.

**This is not your institute's template.** Headings, margins and the title page are
usually prescribed, and this build has no way to know them. What it produces is a
clean, correctly paginated document to drop that template onto. For LaTeX instead:

```bash
pandoc final-report.md -o final-report.pdf --pdf-engine=xelatex
```

## Rehearse the demo twice

`demo-script.md` is written to be run, not read. Its failure mode is not a broken
platform but a stale terminal: WSL winds the distro down when nothing is attached,
so start the dashboard in a window you keep open.

## The three things the viva turns on

1. **Concede the hardware limitation before being asked.** It is on the title slide
   for that reason.
2. **The central finding is that tokenising identifiers does not shrink the
   boundary.** Quasi-identifiers do, which makes disclosure control load-bearing.
3. **Say "measured" or "modelled" every time you give a number.** It is the
   discipline the whole project rests on, and maintaining it under pressure is what
   makes the rest credible.

A fourth, since ADR 0022: **the landing-zone hole is closed, and the story of closing
it is better than the story of finding it.** It exposed a second defect the planner
had not been looking for, and it forced the violation detector to be rewritten so it
re-derives its answer from the disk instead of repeating a conclusion that had
stopped being true.
