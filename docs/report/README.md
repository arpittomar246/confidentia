# Submission artefacts

| File | What it is |
|---|---|
| `final-report.md` | The dissertation body — abstract through conclusion, with two appendices |
| `confidentia-viva.pptx` | 16-slide defence deck, with speaker notes on every slide |
| `viva-preparation.md` | Anticipated questions and answers, including the uncomfortable ones |
| `demo-script.md` | A rehearsed 12-minute live demonstration, with fallbacks |
| `build-deck.js` | Regenerates the deck: `node build-deck.js out.pptx` (needs `npm i pptxgenjs`) |

## Two things to know before submitting

**The report has no references section.** Every factual claim in it comes from this
project's own code, measurements and commit history, and each is traceable to a
stage report or an ADR. The literature review and citations are yours to add —
nothing in the document should be attributed to a source it does not name.

**The report is Markdown.** If your department requires Word or LaTeX, it converts
cleanly:

```bash
pandoc final-report.md -o final-report.docx
pandoc final-report.md -o final-report.pdf --pdf-engine=xelatex
```

Check the institute template first — headings, margins and the title page are
usually prescribed, and pandoc will not know about them.

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
