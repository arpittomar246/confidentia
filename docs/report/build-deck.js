const pptxgen = require("pptxgenjs");

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE";           // 13.33 x 7.5
pres.author = "Arpit Tomar (M25DE2005)";
pres.title = "CONFIDENTIA";

// Palette lifted from the project's own console, so the deck and the artefact
// read as one system.
const C = {
  bgDark: "0D0F14",
  bgDeep: "08090C",
  card: "171B24",
  card2: "1E2430",
  line: "333B4D",
  text: "E8ECF2",
  muted: "98A2B3",
  dim: "6B7688",
  accent: "4F8CFF",
  warn: "FBBF24",
  good: "34D399",
  bad: "F87171",
  purple: "A78BFA",
};

const HEAD = "Cambria";
const BODY = "Calibri";

const DOT = "·";      // middot
const DASH = "—";     // em dash
const ARROW = "→";    // right arrow
const TIMES = "×";    // multiplication sign

// ---------------------------------------------------------------- helpers

function base(slide, { deep = false } = {}) {
  slide.background = { color: deep ? C.bgDeep : C.bgDark };
  return slide;
}

// `chipped` narrows the title so it cannot run under the MEASURED/MODELLED chip
// in the top-right. Rendering caught "Scoring the platform against ground truth"
// passing straight through it.
function title(slide, text, sub, { chipped = false } = {}) {
  slide.addText(text, {
    x: 0.6, y: 0.42, w: chipped ? 10.5 : 12.1, h: 0.75,
    fontFace: HEAD, fontSize: chipped ? 30 : 34, bold: true, color: C.text, margin: 0,
  });
  if (sub) {
    slide.addText(sub, {
      x: 0.6, y: 1.18, w: 12.1, h: 0.4,
      fontFace: BODY, fontSize: 14, color: C.muted, margin: 0,
    });
  }
}

// The visual motif: a MEASURED / MODELLED chip. It is the project's central
// discipline, so it is also the thing that repeats across the deck.
function chip(slide, x, y, label, kind) {
  const colour = kind === "measured" ? C.good : kind === "modelled" ? C.warn : C.accent;
  slide.addShape(pres.ShapeType.roundRect, {
    x, y, w: 1.35, h: 0.3, rectRadius: 0.14,
    fill: { color: colour, transparency: 86 },
    line: { color: colour, width: 1 },
  });
  slide.addText(label, {
    x, y, w: 1.35, h: 0.3, align: "center", valign: "middle",
    fontFace: BODY, fontSize: 9.5, bold: true, color: colour, charSpacing: 1, margin: 0,
  });
}

function card(slide, { x, y, w, h }) {
  slide.addShape(pres.ShapeType.roundRect, {
    x, y, w, h, rectRadius: 0.06,
    fill: { color: C.card }, line: { color: C.line, width: 1 },
  });
}

function stat(slide, x, y, w, value, label, colour = C.accent) {
  // Sized to the string, because "422,297" at 40pt wrapped to two lines in a
  // 2.3in box and collided with its own label.
  const size = value.length >= 7 ? 27 : value.length >= 5 ? 33 : 40;
  slide.addText(value, {
    x, y, w, h: 0.72, fontFace: HEAD, fontSize: size, bold: true,
    color: colour, align: "center", valign: "bottom", margin: 0,
  });
  slide.addText(label, {
    x, y: y + 0.75, w, h: 0.5, fontFace: BODY, fontSize: 11,
    color: C.muted, align: "center", margin: 0,
  });
}

function numberDot(slide, x, y, n, colour) {
  slide.addShape(pres.ShapeType.ellipse, {
    x, y, w: 0.42, h: 0.42,
    fill: { color: colour, transparency: 80 }, line: { color: colour, width: 1.25 },
  });
  slide.addText(n, {
    x, y, w: 0.42, h: 0.42, align: "center", valign: "middle",
    fontFace: BODY, fontSize: 12, bold: true, color: colour, margin: 0,
  });
}

function tableOn(slide, rows, opts) {
  slide.addTable(rows, {
    fontFace: BODY, fontSize: opts.fontSize || 11.5, color: C.text,
    border: { type: "solid", color: C.line, pt: 0.5 },
    fill: { color: C.card },
    ...opts,
  });
}

const th = (t) => ({ text: t, options: { bold: true, color: C.muted, fontSize: 10.5, fill: { color: C.card2 } } });
const td = (t, o = {}) => ({ text: t, options: { color: C.text, ...o } });

// ================================================================= 1. TITLE
{
  const s = base(pres.addSlide(), { deep: true });
  s.addText("CONFIDENTIA", {
    x: 0.9, y: 2.15, w: 11.5, h: 1.0,
    fontFace: HEAD, fontSize: 58, bold: true, color: C.text, charSpacing: 5, margin: 0,
  });
  s.addText("A confidential lakehouse platform", {
    x: 0.9, y: 3.18, w: 11.5, h: 0.5,
    fontFace: HEAD, fontSize: 23, color: C.accent, margin: 0,
  });
  s.addText("Attested, privacy-preserving data engineering on untrusted cloud infrastructure", {
    x: 0.9, y: 3.75, w: 11.5, h: 0.4,
    fontFace: BODY, fontSize: 14, color: C.muted, margin: 0,
  });
  s.addShape(pres.ShapeType.line, {
    x: 0.92, y: 4.5, w: 3.2, h: 0, line: { color: C.line, width: 1 },
  });
  s.addText(`Arpit Tomar   ${DOT}   M25DE2005   ${DOT}   M.Tech Major Project   ${DOT}   IIT Jodhpur`, {
    x: 0.9, y: 4.75, w: 11.5, h: 0.4,
    fontFace: BODY, fontSize: 13, color: C.dim, margin: 0,
  });
  chip(s, 0.9, 5.45, "MODELLED", "modelled");
  s.addText("No confidential-computing hardware. Every enclave figure is labelled.", {
    x: 2.42, y: 5.45, w: 9, h: 0.3, valign: "middle",
    fontFace: BODY, fontSize: 11, italic: true, color: C.warn, margin: 0,
  });
  s.addNotes("Open by naming the limitation before anyone asks. The MODELLED chip appears on every slide carrying an enclave number.");
}

// ============================================================ 2. THE QUESTION
{
  const s = base(pres.addSlide());
  title(s, "The question");

  card(s, { x: 0.6, y: 1.75, w: 12.1, h: 1.65 });
  s.addText(
    `What does it cost, architecturally and quantitatively, to run a full medallion data platform under a hardware-enforced confidentiality boundary ${DASH} and where in that pipeline should the boundary be drawn?`,
    { x: 1.0, y: 1.95, w: 11.3, h: 1.25, fontFace: HEAD, fontSize: 19, italic: true, color: C.text, margin: 0, valign: "middle" }
  );

  s.addText("Most confidential-computing work demonstrates one computation in an enclave.", {
    x: 0.6, y: 3.75, w: 12.1, h: 0.35, fontFace: BODY, fontSize: 14, color: C.muted, margin: 0,
  });

  const items = [
    ["A pipeline is different", "Data moves between layers, is joined, aggregated, quality-checked and published."],
    ["So the question changes", 'Not "can this run in an enclave" but "which of these stages has to".'],
    ["And that is derivable", `From the schema contracts ${DASH} it does not need hardware to answer.`],
  ];
  items.forEach(([h, b], i) => {
    const y = 4.3 + i * 0.85;
    numberDot(s, 0.72, y, String(i + 1), C.accent);
    s.addText(h, { x: 1.35, y: y - 0.04, w: 3.4, h: 0.3, fontFace: BODY, fontSize: 13.5, bold: true, color: C.accent, margin: 0 });
    s.addText(b, { x: 4.85, y: y - 0.04, w: 7.8, h: 0.5, fontFace: BODY, fontSize: 12.5, color: C.text, margin: 0 });
  });
  s.addNotes("The second half of the question is the contribution. Emphasise that placement is a derivation, not a measurement.");
}

// ============================================================= 3. WHAT WAS BUILT
{
  const s = base(pres.addSlide());
  title(s, "What was built", `Two tenants ${DASH} healthcare and financial ${DASH} through a full medallion architecture`);

  const flow = [
    ["SOURCES", `files ${DOT} REST ${DOT} CDC ${DOT} Kafka`, C.dim],
    ["LANDING", "as it arrived, encrypted", C.accent],
    ["BRONZE", "encrypted, raw values", C.accent],
    ["SILVER", `conform ${DOT} dedupe ${DOT} tokenise`, C.accent],
    ["GOLD", `star schema ${DOT} suppression`, C.accent],
    ["SERVING", "aggregates only", C.good],
  ];
  flow.forEach(([h, b, col], i) => {
    const x = 0.6 + i * 2.05;
    card(s, { x, y: 1.85, w: 1.85, h: 1.15 });
    s.addText(h, { x, y: 2.0, w: 1.85, h: 0.3, align: "center", fontFace: BODY, fontSize: 11.5, bold: true, color: col, margin: 0 });
    s.addText(b, { x: x + 0.08, y: 2.32, w: 1.69, h: 0.6, align: "center", fontFace: BODY, fontSize: 8.5, color: C.muted, margin: 0 });
    if (i < flow.length - 1) {
      s.addText(">", { x: x + 1.85, y: 2.15, w: 0.2, h: 0.4, align: "center", fontFace: BODY, fontSize: 13, color: C.line, margin: 0 });
    }
  });

  s.addText(`Landing is encrypted too, since ADR 0022 ${DASH} see slide 11.`, {
    x: 2.3, y: 3.06, w: 4.4, h: 0.3, fontFace: BODY, fontSize: 9.5, italic: true, color: C.good, margin: 0,
  });

  const stats = [
    ["20,777", "lines of Python", C.text],
    ["386", "tests", C.good],
    ["14", "CI jobs", C.good],
    ["422,297", "Bronze rows", C.accent],
    ["19 / 19", "ADRs / stage reports", C.muted],
  ];
  stats.forEach(([v, l, col], i) => stat(s, 0.6 + i * 2.44, 3.85, 2.3, v, l, col));

  s.addText("Every claim is re-checked on each push. The encryption guarantee is proved from the bytes on disk, not from the code.", {
    x: 0.6, y: 5.65, w: 12.1, h: 0.35, fontFace: BODY, fontSize: 12.5, color: C.muted, margin: 0,
  });
  s.addNotes("Do not linger. This slide exists so the examiner knows the scale before the argument starts.");
}

// ============================================================ 4. HONESTY POLICY
{
  const s = base(pres.addSlide());
  title(s, "What is measured, and what is modelled", "The distinction this project is built on");

  card(s, { x: 0.6, y: 1.9, w: 5.9, h: 3.0 });
  chip(s, 0.9, 2.15, "MEASURED", "measured");
  s.addText("Observed on this machine", { x: 2.42, y: 2.15, w: 3.9, h: 0.3, valign: "middle", fontFace: BODY, fontSize: 11.5, color: C.muted, margin: 0 });
  s.addText(
    [
      { text: "Cryptography, storage, Spark throughput", options: { bullet: true, breakLine: true } },
      { text: "Data quality precision and recall", options: { bullet: true, breakLine: true } },
      { text: "Deduplication and disclosure suppression", options: { bullet: true, breakLine: true } },
      { text: "Lineage digesting and tamper detection", options: { bullet: true } },
    ],
    { x: 1.0, y: 2.7, w: 5.2, h: 2.0, fontFace: BODY, fontSize: 12.5, color: C.text, paraSpaceAfter: 8, margin: 0 }
  );

  card(s, { x: 6.8, y: 1.9, w: 5.9, h: 3.0 });
  chip(s, 7.1, 2.15, "MODELLED", "modelled");
  s.addText("Projected, never observed", { x: 8.62, y: 2.15, w: 3.9, h: 0.3, valign: "middle", fontFace: BODY, fontSize: 11.5, color: C.muted, margin: 0 });
  s.addText(
    [
      { text: "Enclave transition costs (ecall / ocall)", options: { bullet: true, breakLine: true } },
      { text: "EPC paging beyond the memory budget", options: { bullet: true, breakLine: true } },
      { text: "Cold-start cost per enclave instance", options: { bullet: true, breakLine: true } },
      { text: "Anything derived from the four above", options: { bullet: true } },
    ],
    { x: 7.2, y: 2.7, w: 5.2, h: 2.0, fontFace: BODY, fontSize: 12.5, color: C.text, paraSpaceAfter: 8, margin: 0 }
  );

  card(s, { x: 0.6, y: 5.15, w: 12.1, h: 1.35 });
  s.addText("Under the simulated backend, the process measures itself and could lie.", {
    x: 0.95, y: 5.32, w: 11.4, h: 0.35, fontFace: BODY, fontSize: 14, bold: true, color: C.warn, margin: 0,
  });
  s.addText("The mechanism is real and exercised: an allowlist, four refusal paths, a full audit. The guarantee is not. Every policy and audit row carries hardware_rooted = false.", {
    x: 0.95, y: 5.72, w: 11.4, h: 0.6, fontFace: BODY, fontSize: 12.5, color: C.muted, margin: 0,
  });
  s.addNotes("Concede this before being asked. It converts the weakness into evidence of judgement.");
}

// ========================================================== 5. CONTRIBUTIONS
{
  const s = base(pres.addSlide());
  title(s, "Five contributions");

  const rows = [
    ["C1", "Crypto-partitioned storage", "Per-column, per-sensitivity keys", "Byte-level check; keyless read refused in a separate process", C.accent],
    ["C2", "Attestation-gated key release", "Four audited refusal paths", "15 refusals recorded across 347 requests", C.accent],
    ["C3", "Verifiable lineage", "Per-record Merkle roots, signed, chained", "Four tamper attacks, each detected for the right reason", C.purple],
    ["C4", "Disclosure control", "k-anonymity + complementary suppression", "8,729 primary + 250 complementary on one mart", C.good],
    ["C5", "Enclave placement planner", "Boundary derived from the contracts", "The result that reframed the architecture", C.warn],
  ];
  rows.forEach(([id, name, what, how, col], i) => {
    const y = 1.75 + i * 1.02;
    card(s, { x: 0.6, y, w: 12.1, h: 0.88 });
    numberDot(s, 0.85, y + 0.23, id, col);
    s.addText(name, { x: 1.45, y: y + 0.12, w: 3.4, h: 0.3, fontFace: BODY, fontSize: 13, bold: true, color: C.text, margin: 0 });
    s.addText(what, { x: 1.45, y: y + 0.44, w: 3.5, h: 0.3, fontFace: BODY, fontSize: 10, color: C.muted, margin: 0 });
    s.addText(how, { x: 5.2, y: y + 0.28, w: 7.3, h: 0.35, valign: "middle", fontFace: BODY, fontSize: 12, color: col, margin: 0 });
  });
  s.addNotes("Right-hand column is how each was verified, not what it claims. Lead with verification.");
}

// ================================================================ 6. C1
{
  const s = base(pres.addSlide());
  title(s, `C1 ${DOT} Crypto-partitioned storage`, "Per-column keys, so predicate pushdown survives encryption", { chipped: true });
  chip(s, 11.35, 0.5, "MEASURED", "measured");

  card(s, { x: 0.6, y: 1.85, w: 5.9, h: 2.35 });
  s.addText("The design", { x: 0.9, y: 2.0, w: 5.3, h: 0.3, fontFace: BODY, fontSize: 13, bold: true, color: C.accent, margin: 0 });
  s.addText(
    [
      { text: "AES-256-GCM key per (tenant, sensitivity class)", options: { bullet: true, breakLine: true } },
      { text: "PUBLIC columns unencrypted, so filters push down", options: { bullet: true, breakLine: true } },
      { text: "MEK to tenant KEK to DEK, derived not stored", options: { bullet: true, breakLine: true } },
      { text: "Tenant isolation is cryptographic, not a filter", options: { bullet: true } },
    ],
    { x: 1.0, y: 2.42, w: 5.2, h: 1.6, fontFace: BODY, fontSize: 12, color: C.text, paraSpaceAfter: 7, margin: 0 }
  );

  card(s, { x: 6.8, y: 1.85, w: 5.9, h: 2.35 });
  s.addText("How it is verified", { x: 7.1, y: 2.0, w: 5.3, h: 0.3, fontFace: BODY, fontSize: 13, bold: true, color: C.good, margin: 0 });
  s.addText(
    [
      { text: "Footer magic read from the bytes: PAR1 vs PARE", options: { bullet: true, breakLine: true } },
      { text: "Raw file grepped for values that must not appear", options: { bullet: true, breakLine: true } },
      { text: "Keyless read attempted in a separate OS process", options: { bullet: true, breakLine: true } },
      { text: "Re-proved by CI on every push", options: { bullet: true } },
    ],
    { x: 7.2, y: 2.42, w: 5.2, h: 1.6, fontFace: BODY, fontSize: 12, color: C.text, paraSpaceAfter: 7, margin: 0 }
  );

  card(s, { x: 0.6, y: 4.45, w: 12.1, h: 1.95 });
  s.addText("Why it is checked at the byte level", { x: 0.95, y: 4.62, w: 11.4, h: 0.3, fontFace: BODY, fontSize: 13, bold: true, color: C.bad, margin: 0 });
  s.addText(
    'The first feasibility spike produced a false pass. The "encrypted" arm asserted only a successful round-trip, which an unencrypted write also satisfies, and the keyless-read test reused the JVM, so Parquet’s key cache served it from memory.',
    { x: 0.95, y: 5.0, w: 11.4, h: 0.6, fontFace: BODY, fontSize: 12.5, color: C.text, margin: 0 }
  );
  s.addText(
    `Separately: Delta silently ignores encryption passed as writer options and writes plaintext ${DASH} no error, no warning. Every Bronze write now asserts PARE afterwards and raises if it is absent.`,
    { x: 0.95, y: 5.65, w: 11.4, h: 0.6, fontFace: BODY, fontSize: 12.5, color: C.muted, margin: 0 }
  );
  s.addNotes("The false pass is the strongest thing on this slide. It explains why every later guarantee is checked at the level the guarantee is made.");
}

// ================================================================ 7. C2
{
  const s = base(pres.addSlide());
  title(s, `C2 ${DOT} Attestation-gated key release`, "A key is released only against evidence that verifies", { chipped: true });
  chip(s, 11.35, 0.5, "MEASURED", "measured");

  stat(s, 0.6, 1.85, 3.0, "347", "key requests recorded", C.accent);
  stat(s, 3.9, 1.85, 3.0, "15", "refused, with a reason", C.bad);
  stat(s, 7.2, 1.85, 3.0, "4", "distinct refusal paths", C.warn);
  stat(s, 10.2, 1.85, 2.5, "6", "measured packages", C.muted);

  const rows = [
    [th("Refusal path"), th("Cause")],
    [td("Unknown measurement"), td("Code not in the attestation allowlist", { color: C.muted })],
    [td("Revoked policy"), td("The measurement was approved, then withdrawn", { color: C.muted })],
    [td("Tenant not permitted"), td("Approved code asking for another tenant's key", { color: C.muted })],
    [td("Stale or replayed evidence"), td("Nonce outside the freshness window", { color: C.muted })],
  ];
  tableOn(s, rows, { x: 0.6, y: 3.6, w: 7.3, colW: [2.7, 4.6], rowH: 0.34, fontSize: 11 });

  card(s, { x: 8.2, y: 3.6, w: 4.5, h: 2.3 });
  s.addText("Editing measured code revokes release", { x: 8.5, y: 3.77, w: 3.9, h: 0.55, fontFace: BODY, fontSize: 12.5, bold: true, color: C.warn, margin: 0 });
  s.addText(
    `crypto ${DOT} pipelines ${DOT} quality ${DOT} lineage ${DOT} evaluation ${DOT} streaming\n\nAny change alters the measurement and stops the pipeline until keys approve runs. That is the mechanism working, and the usual cause of a refusal burst.`,
    { x: 8.5, y: 4.32, w: 3.9, h: 1.45, fontFace: BODY, fontSize: 11, color: C.muted, margin: 0 }
  );
  s.addNotes("If asked what stops the app lying about its measurement: under sim, nothing. Say it plainly, then describe the Azure path.");
}

// ================================================================ 8. C3
{
  const s = base(pres.addSlide());
  title(s, `C3 ${DOT} Verifiable lineage`, "Per-record Merkle roots, ECDSA-signed, hash-chained in PostgreSQL", { chipped: true });
  chip(s, 11.35, 0.5, "MEASURED", "measured");

  s.addText("Granularity was measured before it was chosen", { x: 0.6, y: 1.8, w: 6.0, h: 0.3, fontFace: BODY, fontSize: 13, bold: true, color: C.purple, margin: 0 });
  const g = [
    [th("Granularity"), th("Cost"), th("Answers the claim?")],
    [td("Per run"), td("27 ms"), td("no", { color: C.bad })],
    [td("Per file"), td("8 ms"), td("only by handing over the file", { color: C.warn })],
    [td("Per record", { bold: true }), td("7,330 ms", { bold: true }), td("yes", { color: C.good, bold: true })],
  ];
  tableOn(s, g, { x: 0.6, y: 2.2, w: 6.0, colW: [1.9, 1.5, 2.6], rowH: 0.35, fontSize: 11 });

  s.addText(`~900${TIMES} the cost of per-file, and the only one that can prove a row was included.`, {
    x: 0.6, y: 3.75, w: 6.0, h: 0.5, fontFace: BODY, fontSize: 11.5, italic: true, color: C.muted, margin: 0,
  });

  s.addText("Four tamper attacks, each detected for the correct reason", { x: 6.9, y: 1.8, w: 5.8, h: 0.3, fontFace: BODY, fontSize: 13, bold: true, color: C.purple, margin: 0 });
  const attacks = [
    `Edit a signed payload ${ARROW} payload altered`,
    `Edit a denormalised column ${ARROW} diverges from payload`,
    `Delete a mid-chain entry ${ARROW} broken link`,
    `Edit one row in the encrypted table ${ARROW} root moved`,
  ];
  attacks.forEach((a, i) => {
    const y = 2.25 + i * 0.46;
    s.addShape(pres.ShapeType.ellipse, { x: 6.95, y: y + 0.06, w: 0.13, h: 0.13, fill: { color: C.purple } });
    s.addText(a, { x: 7.25, y, w: 5.45, h: 0.4, fontFace: BODY, fontSize: 11.5, color: C.text, margin: 0 });
  });

  card(s, { x: 0.6, y: 4.55, w: 12.1, h: 1.7 });
  s.addText('"row count unchanged"', { x: 0.95, y: 4.72, w: 3.4, h: 0.3, fontFace: HEAD, fontSize: 15, bold: true, color: C.good, margin: 0 });
  s.addText(
    'The fourth attack edits a single field inside a real encrypted Delta table, then undoes it with time travel. The table is the same size and one value differs, and the Merkle root still moves. The verifier reports which of the four attacks it was, because telling an operator "broken link" when the problem is a forged signature wastes the incident.',
    { x: 0.95, y: 5.12, w: 11.4, h: 0.95, fontFace: BODY, fontSize: 12.5, color: C.text, margin: 0 }
  );
  s.addNotes("Demo this live if there is time. verify_lineage.sh takes about 40 seconds.");
}

// ================================================================ 9. C4
{
  const s = base(pres.addSlide());
  title(s, `C4 ${DOT} Disclosure control`, "k-anonymity at k=5, with complementary suppression", { chipped: true });
  chip(s, 11.35, 0.5, "MEASURED", "measured");

  card(s, { x: 0.6, y: 1.85, w: 12.1, h: 1.05 });
  s.addText("Primary suppression alone is not protection.", { x: 0.95, y: 2.0, w: 11.4, h: 0.3, fontFace: BODY, fontSize: 14, bold: true, color: C.good, margin: 0 });
  s.addText("A suppressed cell is recoverable by subtracting the published cells from a published total. Complementary suppression blanks the additional cells that make that arithmetic impossible.", {
    x: 0.95, y: 2.38, w: 11.4, h: 0.4, fontFace: BODY, fontSize: 12.5, color: C.muted, margin: 0,
  });

  const rows = [
    [th("Published mart"), th("Rows"), th("Primary suppressed"), th("Complementary"), th("")],
    [td("Transactions daily"), td("11,479"), td("8,729"), td("250"), td("protected", { color: C.good })],
    [td("Encounters daily"), td("4,477"), td("1,227"), td("432"), td("protected", { color: C.good })],
    [td("Diagnosis prevalence"), td("263"), td("86"), td("0", { color: C.bad, bold: true }), td("not protected", { color: C.bad, bold: true })],
  ];
  tableOn(s, rows, { x: 0.6, y: 3.2, w: 12.1, colW: [3.5, 1.9, 2.9, 2.1, 1.7], rowH: 0.4, fontSize: 12 });

  card(s, { x: 0.6, y: 5.15, w: 12.1, h: 1.25 });
  s.addText(
    "The third row is a finding, not an omission. A mart with primary suppression and no complementary suppression has usually not been protected. The platform surfaces it in the API and flags it in the console rather than hiding it.",
    { x: 0.95, y: 5.35, w: 11.4, h: 0.85, fontFace: BODY, fontSize: 12.5, color: C.text, margin: 0 }
  );
  s.addNotes("The contribution is not k-anonymity. It is that the suppression is audited per publish and the failure mode is visible.");
}

// ============================================== 10. C5 - THE CENTRAL FINDING
{
  const s = base(pres.addSlide(), { deep: true });
  title(s, `C5 ${DOT} Where the boundary actually falls`, "The rule, applied to the schema contracts by code");

  card(s, { x: 0.6, y: 1.75, w: 12.1, h: 0.85 });
  s.addText(
    "A stage runs inside the enclave if and only if it observes the plaintext of a column classified PII or PHI.",
    { x: 0.95, y: 1.9, w: 11.4, h: 0.55, valign: "middle", fontFace: HEAD, fontSize: 16, italic: true, color: C.warn, margin: 0 }
  );

  s.addText("42", { x: 1.0, y: 2.95, w: 1.5, h: 0.75, align: "center", fontFace: HEAD, fontSize: 44, bold: true, color: C.bad, margin: 0 });
  s.addText("protected columns\nat Bronze", { x: 0.7, y: 3.75, w: 2.1, h: 0.6, align: "center", fontFace: BODY, fontSize: 11, color: C.muted, margin: 0 });

  s.addText(`tokenisation removes 17  ${ARROW}`, { x: 2.95, y: 3.1, w: 3.3, h: 0.4, align: "center", valign: "middle", fontFace: BODY, fontSize: 12, color: C.accent, margin: 0 });

  s.addText("25", { x: 6.35, y: 2.95, w: 1.5, h: 0.75, align: "center", fontFace: HEAD, fontSize: 44, bold: true, color: C.warn, margin: 0 });
  s.addText("quasi-identifiers\nremain after Silver", { x: 6.05, y: 3.75, w: 2.1, h: 0.6, align: "center", fontFace: BODY, fontSize: 11, color: C.muted, margin: 0 });

  s.addShape(pres.ShapeType.roundRect, {
    x: 8.6, y: 2.85, w: 4.1, h: 1.5, rectRadius: 0.06,
    fill: { color: C.bad, transparency: 88 }, line: { color: C.bad, width: 1.25 },
  });
  s.addText("and nothing moves out", { x: 8.75, y: 3.05, w: 3.8, h: 0.35, align: "center", fontFace: BODY, fontSize: 14, bold: true, color: C.bad, margin: 0 });
  s.addText("Every stage touching tenant data stays inside the boundary.", { x: 8.8, y: 3.45, w: 3.7, h: 0.7, align: "center", fontFace: BODY, fontSize: 11, color: C.muted, margin: 0 });

  card(s, { x: 0.6, y: 4.65, w: 12.1, h: 1.75 });
  s.addText("Tokenising direct identifiers is not what shrinks the boundary.", { x: 0.95, y: 4.83, w: 11.4, h: 0.35, fontFace: HEAD, fontSize: 16, bold: true, color: C.text, margin: 0 });
  s.addText(
    `Quasi-identifiers do that work ${DASH} dates of birth, diagnosis codes, geography ${DASH} and they cannot be tokenised without destroying the aggregations they exist for. So what makes Gold's output publishable is disclosure control; what keeps Gold's computation inside is that disclosure control has not been applied yet at that point.`,
    { x: 0.95, y: 5.25, w: 11.4, h: 1.0, fontFace: BODY, fontSize: 12.5, color: C.muted, margin: 0 }
  );
  s.addNotes("This is the slide the viva turns on. Say it slowly. The planner produced this; it was not the expected answer.");
}

// ==================================================== 11. THE FINDING IT EXPOSED
{
  const s = base(pres.addSlide());
  title(s, "A hole in its own architecture", "The planner reported one boundary violation, and the violation was real", { chipped: true });
  chip(s, 11.35, 0.5, "MEASURED", "measured");

  const rows = [
    [th("lake/landing/.../part-00000.parquet"), th("footer magic"), th("state")],
    [td("before  " + DASH + "  ADR 0017 found it"), td("PAR1"), td("unencrypted", { color: C.bad, bold: true })],
    [td("after  " + DASH + "  ADR 0022 closed it"), td("PARE"), td("encrypted", { color: C.good, bold: true })],
  ];
  tableOn(s, rows, { x: 0.6, y: 1.9, w: 12.1, colW: [6.3, 2.6, 3.2], rowH: 0.42, fontSize: 12 });

  card(s, { x: 0.6, y: 3.25, w: 5.9, h: 1.55 });
  s.addText("Confirmed by inspection, not inferred", { x: 0.9, y: 3.42, w: 5.3, h: 0.3, fontFace: BODY, fontSize: 12.5, bold: true, color: C.bad, margin: 0 });
  s.addText("One landing patient file held 19 SSN-shaped and 7 email-shaped strings. 42 protected columns were affected.", {
    x: 0.9, y: 3.8, w: 5.3, h: 0.85, fontFace: BODY, fontSize: 12, color: C.text, margin: 0,
  });

  card(s, { x: 6.8, y: 3.25, w: 5.9, h: 1.55 });
  s.addText("Encrypted under the same keys as Bronze", { x: 7.1, y: 3.42, w: 5.3, h: 0.3, fontFace: BODY, fontSize: 12.5, bold: true, color: C.good, margin: 0 });
  s.addText("24 parts, 369,097 rows, " + TIMES + "1.014 storage. pyarrow writes it, Spark reads it — same wrapping envelope on both sides.", {
    x: 7.1, y: 3.8, w: 5.3, h: 0.85, fontFace: BODY, fontSize: 12, color: C.text, margin: 0,
  });

  card(s, { x: 0.6, y: 5.05, w: 12.1, h: 1.35 });
  s.addText("And closing it exposed a second one, which is worse", { x: 0.95, y: 5.22, w: 11.4, h: 0.32, fontFace: HEAD, fontSize: 14, bold: true, color: C.warn, margin: 0 });
  s.addText(
    "The migration found healthcare/patient_change: 900 rows, no schema contract at all. Never promoted, never classified, never encrypted — not because anything decided it should not be, but because nothing knew it was there. Deleted, not contracted (ADR 0023): its effects already reach the lake as patient_cdc. The ingest ledger keeps the record, marked withdrawn.",
    { x: 0.95, y: 5.6, w: 11.4, h: 0.7, fontFace: BODY, fontSize: 12, color: C.text, margin: 0 }
  );
  s.addNotes("Volunteer both. An examiner who finds one themselves will press much harder than one who is handed it. The line that lands: every check I had built covered the path the data was supposed to take. If pressed on why deleted rather than contracted — a contract is a commitment to a Bronze table, quality rules and a Silver path, for data already in the lake by a better route.");
}

// ================================================== 12. EVALUATION
{
  const s = base(pres.addSlide());
  title(s, "Scoring against ground truth", "The generators record every injected defect, and CI proves no pipeline package can read it", { chipped: true });
  chip(s, 11.35, 0.5, "MEASURED", "measured");

  s.addChart(
    pres.ChartType.bar,
    [
      { name: "Precision", labels: ["patient", "account", "encounter", "transaction", "claim_line"], values: [1.0, 1.0, 0.834, 0.837, 0.795] },
      { name: "Recall", labels: ["patient", "account", "encounter", "transaction", "claim_line"], values: [0.996, 0.983, 1.0, 0.954, 0.955] },
    ],
    {
      x: 0.6, y: 1.85, w: 7.4, h: 3.3,
      barDir: "col", chartColors: [C.accent, C.purple],
      showTitle: false, showLegend: true, legendPos: "t", legendColor: C.muted, legendFontSize: 10,
      showValue: true, dataLabelPosition: "outEnd", dataLabelColor: C.text, dataLabelFontSize: 8,
      dataLabelFormatCode: "0.00",
      valAxisMinVal: 0, valAxisMaxVal: 1.05,
      catAxisLabelColor: C.muted, valAxisLabelColor: C.muted,
      catAxisLabelFontSize: 10, valAxisLabelFontSize: 9,
      valGridLine: { color: C.line, size: 0.5 }, catGridLine: { style: "none" },
      plotArea: { fill: { color: C.bgDark } }, chartArea: { fill: { color: C.bgDark } },
    }
  );

  card(s, { x: 8.3, y: 1.85, w: 4.4, h: 1.55 });
  s.addText("0 uncovered families", { x: 8.6, y: 2.03, w: 3.8, h: 0.32, fontFace: BODY, fontSize: 13, bold: true, color: C.good, margin: 0 });
  s.addText("Down from 5. A defect family the generator injects with no rule to catch it is a permanent false negative. CI now holds this at zero.", {
    x: 8.6, y: 2.42, w: 3.8, h: 0.85, fontFace: BODY, fontSize: 11, color: C.muted, margin: 0,
  });

  card(s, { x: 8.3, y: 3.6, w: 4.4, h: 1.55 });
  s.addText("0.891  vs  0.251", { x: 8.6, y: 3.78, w: 3.8, h: 0.32, fontFace: BODY, fontSize: 13, bold: true, color: C.warn, margin: 0 });
  s.addText("The same dataset. At Bronze a duplicate and a redelivery are identical bytes, so 19,855 legitimate redeliveries count as false positives. Both are returned; the corrected one leads.", {
    x: 8.6, y: 4.17, w: 3.8, h: 0.95, fontFace: BODY, fontSize: 11, color: C.muted, margin: 0,
  });

  card(s, { x: 0.6, y: 5.4, w: 12.1, h: 1.0 });
  s.addText(
    `Recall on patient moved 0.390 ${ARROW} 0.997, and that is not evidence the rules improved. Almost nothing about their method changed; the answer key now describes defects that actually exist. The first run measured the measurement apparatus.`,
    { x: 0.95, y: 5.55, w: 11.4, h: 0.7, fontFace: BODY, fontSize: 12.5, color: C.text, margin: 0 }
  );
  s.addNotes("Do not accept the framing that the rules got 2.5x better. The harness was wrong, not the rules.");
}

// ==================================================== 13. EXPERIMENTS
{
  const s = base(pres.addSlide());
  title(s, "Experiments", "Six of eight measured, each producing a finding beyond its number", { chipped: true });
  chip(s, 11.35, 0.5, "MEASURED", "measured");

  s.addChart(
    pres.ChartType.line,
    [{ name: "Write time (s)", labels: ["4", "16", "90", "360", "720"], values: [2.76, 3.09, 4.70, 10.43, 17.36] }],
    {
      x: 0.6, y: 1.85, w: 6.1, h: 2.9,
      chartColors: [C.warn], lineDataSymbol: "circle", lineDataSymbolSize: 7, lineSize: 2.5,
      showTitle: true, title: `E2 ${DOT} output partitions vs write time`, titleColor: C.text, titleFontSize: 12,
      showLegend: false, showValue: true, dataLabelPosition: "t",
      dataLabelColor: C.muted, dataLabelFontSize: 9,
      // Without this the labels round 2.76 to 3 and the point of the sweep -- that
      // the low end is flat and the high end is not -- disappears.
      dataLabelFormatCode: "0.00",
      catAxisTitle: "output partitions", catAxisTitleColor: C.dim, catAxisTitleFontSize: 9, showCatAxisTitle: true,
      catAxisLabelColor: C.muted, valAxisLabelColor: C.muted,
      catAxisLabelFontSize: 10, valAxisLabelFontSize: 9,
      valGridLine: { color: C.line, size: 0.5 }, catGridLine: { style: "none" },
      plotArea: { fill: { color: C.bgDark } }, chartArea: { fill: { color: C.bgDark } },
    }
  );
  s.addText(`6.3${TIMES} write penalty and 40% storage inflation for identical rows.`, {
    x: 0.6, y: 4.85, w: 6.1, h: 0.35, fontFace: BODY, fontSize: 11.5, italic: true, color: C.muted, margin: 0,
  });

  const findings = [
    ["E4", `Encryption costs ${TIMES}1.042 write, ${TIMES}1.001 storage`, "Below ~1M rows the benchmark inverts: job overhead swamps the signal", C.good],
    ["E1", `Streaming costs ${TIMES}4.27 the wall clock of batch`, "Batch refuses a type mismatch that Kafka's JSON path accepts silently", C.accent],
    ["E8", "Three of four drifts destroy 100% of a column", "All silently. Detection is entirely downstream of Bronze", C.bad],
    ["E3", "Blocked: the incremental arm does not exist", "Silver and Gold are full-refresh; building it is pipeline work", C.dim],
  ];
  findings.forEach(([id, headline, note, col], i) => {
    const y = 1.9 + i * 1.15;
    card(s, { x: 7.0, y, w: 5.7, h: 1.0 });
    s.addText(id, { x: 7.2, y: y + 0.12, w: 0.6, h: 0.3, fontFace: BODY, fontSize: 12, bold: true, color: col, margin: 0 });
    s.addText(headline, { x: 7.8, y: y + 0.1, w: 4.75, h: 0.32, fontFace: BODY, fontSize: 11.5, bold: true, color: C.text, margin: 0 });
    s.addText(note, { x: 7.8, y: y + 0.44, w: 4.75, h: 0.48, fontFace: BODY, fontSize: 10, color: C.muted, margin: 0 });
  });
  s.addNotes("E5-E7 were numbered in the original plan and never specified anywhere in the repository. Say so if asked why six.");
}

// ==================================================== 14. LIMITATIONS
{
  const s = base(pres.addSlide());
  title(s, "Limitations", "Every one of these was found by this project's own checks");

  const lim = [
    ["No TEE hardware", "Enclave figures are modelled. Attestation is a mechanism, not a guarantee.", C.warn],
    ["Deleted data is unlinked, not shredded", "Freed blocks survive until reused. The answer is full-disk encryption.", C.warn],
    ["Schema drift undetected at Bronze", "Three of four drift kinds null 100% of a column silently.", C.bad],
    ["No incremental processing", "Silver and Gold are full-refresh. Blocks experiment E3.", C.muted],
    ["Azure never applied", "terraform validate passes, Checkov 36/0, but plan needs credentials.", C.muted],
    ["Key release proves hardware, not code", "Any Azure confidential VM meeting the claims obtains the key.", C.warn],
  ];
  lim.forEach(([h, b, col], i) => {
    const x = 0.6 + (i % 2) * 6.15;
    const y = 1.85 + Math.floor(i / 2) * 1.35;
    card(s, { x, y, w: 5.95, h: 1.15 });
    s.addShape(pres.ShapeType.ellipse, { x: x + 0.28, y: y + 0.44, w: 0.16, h: 0.16, fill: { color: col } });
    s.addText(h, { x: x + 0.62, y: y + 0.15, w: 5.15, h: 0.32, fontFace: BODY, fontSize: 12.5, bold: true, color: col, margin: 0 });
    s.addText(b, { x: x + 0.62, y: y + 0.5, w: 5.15, h: 0.55, fontFace: BODY, fontSize: 11, color: C.muted, margin: 0 });
  });

  s.addText("A limitation reached before the examiner does is evidence of judgement rather than a hole.", {
    x: 0.6, y: 6.1, w: 12.1, h: 0.35, fontFace: BODY, fontSize: 12.5, italic: true, color: C.dim, margin: 0,
  });
  s.addNotes("The shredding one is what is left of the landing-zone finding after ADR 0022 encrypted it and ADR 0023 resolved the unclassified dataset it exposed. State it as a boundary of the claim rather than a defect: this project does not attempt disk-level erasure and does not pretend to.");
}

// ==================================================== 15. ON HARDWARE
{
  const s = base(pres.addSlide());
  title(s, "What changes on real hardware", "Configuration, plus one Java class");

  const rows = [
    [th("Today"), th("On Azure"), th("What changes")],
    [td("SHA-256 of the source tree"), td("SEV-SNP report signed by AMD"), td("The process can no longer produce its own measurement", { color: C.muted })],
    [td("Verifier in attestation.py"), td("Microsoft Azure Attestation"), td("A third party decides whether evidence is genuine", { color: C.muted })],
    [td("broker.py, four refusal paths"), td("Managed HSM key release policy"), td("Refusal becomes enforceable, not voluntary", { color: C.muted })],
    [td("LocalKmsClient.java"), td("KMS client backed by Managed HSM"), td("The one piece of real work", { color: C.warn })],
  ];
  tableOn(s, rows, { x: 0.6, y: 1.9, w: 12.1, colW: [3.5, 3.9, 4.7], rowH: 0.46, fontSize: 11.5 });

  card(s, { x: 0.6, y: 4.5, w: 5.9, h: 1.9 });
  s.addText("What becomes true", { x: 0.9, y: 4.68, w: 5.3, h: 0.3, fontFace: BODY, fontSize: 13, bold: true, color: C.good, margin: 0 });
  s.addText(
    `Attestation stops being self-issued. The four refusal paths stay exactly as they are and become enforceable. C5's modelled ${TIMES}1.206 becomes a measurement; the placement itself does not change, because it is derived from the contracts.`,
    { x: 0.9, y: 5.08, w: 5.3, h: 1.2, fontFace: BODY, fontSize: 11.5, color: C.text, margin: 0 }
  );

  card(s, { x: 6.8, y: 4.5, w: 5.9, h: 1.9 });
  s.addText("What does not", { x: 7.1, y: 4.68, w: 5.3, h: 0.3, fontFace: BODY, fontSize: 13, bold: true, color: C.bad, margin: 0 });
  s.addText(
    "The policy proves the hardware, not the code. Any Azure confidential VM meeting the claims obtains the key. Pinning the workload needs confidential containers, a materially different deployment.",
    { x: 7.1, y: 5.08, w: 5.3, h: 1.2, fontFace: BODY, fontSize: 11.5, color: C.text, margin: 0 }
  );
  s.addNotes("Nothing in pipelines, quality, evaluation or lineage changes. That is the payoff for treating the boundary as a design parameter from stage 1.");
}

// ==================================================== 16. CONCLUSION
{
  const s = base(pres.addSlide(), { deep: true });
  title(s, "Conclusion");

  card(s, { x: 0.6, y: 1.7, w: 12.1, h: 1.45 });
  s.addText("Where the boundary falls is not where the architecture assumed.", {
    x: 0.95, y: 1.88, w: 11.4, h: 0.35, fontFace: HEAD, fontSize: 17, bold: true, color: C.warn, margin: 0,
  });
  s.addText(
    `Tokenising direct identifiers ${DASH} the intervention the platform spends most effort on ${DASH} moves no stage out of the enclave. Quasi-identifiers do that work, which makes disclosure control load-bearing rather than cosmetic.`,
    { x: 0.95, y: 2.3, w: 11.4, h: 0.7, fontFace: BODY, fontSize: 12.5, color: C.muted, margin: 0 }
  );

  const closing = [
    ["Measured", "Column encryption costs 4.2% of write time and 0.1% of storage.", C.good],
    ["Modelled", `The pipeline inside an enclave: ${TIMES}1.206, a day's work from becoming measured.`, C.warn],
    ["Method", "Build the check before the claim. Six of eight experiments found something beyond their number.", C.accent],
  ];
  closing.forEach(([h, b, col], i) => {
    const y = 3.35 + i * 0.92;
    card(s, { x: 0.6, y, w: 12.1, h: 0.78 });
    s.addText(h, { x: 0.95, y: y + 0.22, w: 1.5, h: 0.35, valign: "middle", fontFace: BODY, fontSize: 12.5, bold: true, color: col, margin: 0 });
    s.addText(b, { x: 2.6, y: y + 0.22, w: 9.75, h: 0.35, valign: "middle", fontFace: BODY, fontSize: 12.5, color: C.text, margin: 0 });
  });

  s.addText(
    "The checks found two holes in the architecture that wrote them.",
    { x: 0.6, y: 6.3, w: 12.1, h: 0.4, align: "center", fontFace: HEAD, fontSize: 14, italic: true, color: C.dim, margin: 0 }
  );
  s.addNotes("Close on the method, not the platform. Every claim has a check behind it, and the checks found things.");
}

pres.writeFile({ fileName: process.argv[2] || "confidentia-viva.pptx" })
  .then((f) => console.log("wrote", f));
