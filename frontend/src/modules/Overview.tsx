/**
 * Overview: the state of the platform in one screen.
 *
 * The only polling module. Everything else refreshes on navigation, because there
 * is no push channel and interval-polling nine modules would put more load on the
 * API than the pipeline does.
 *
 * Two tiles here are normally boring and exist for the moment they are not.
 * "Uncovered families" should read 0 — Stage 7c closed the last coverage gap, and
 * a defect family with no rule is a permanent false negative that would otherwise
 * be discovered as a depressed recall number months later. "Chain" shows the head
 * digest, which commits to every entry before it.
 *
 * **Every chart plots a column the API returned.** Nothing here is derived beyond
 * summing, sorting and pivoting rows into the shape Recharts wants — the API
 * computes no numbers of its own and neither does this page. Where a figure has a
 * caveat, the caveat travels with it rather than living in a footnote: `f1_excl`
 * is charted and the raw value sits in the tooltip, because a chart is exactly
 * where a measurement artefact would quietly become a reported result.
 */
import { Link } from "react-router-dom";

import { useApi } from "../api/hooks";
import type {
  EvaluationSummary,
  GateDecision,
  IngestSummary,
  KeyReleaseSummary,
  LineageHead,
  RunStats,
  Score,
} from "../api/rows";
import {
  ChartEmpty,
  DotPlot,
  GroupedColumns,
  RankedBars,
  SplitDonut,
  StackedColumns,
  STATUS,
  SERIES_CAP,
  seriesColour,
} from "../components/charts";
import {
  Callout,
  Card,
  ErrorState,
  Grid,
  Mono,
  PageHeader,
  Spinner,
  Stat,
} from "../components/ui";
import { count, digest, relative } from "../lib/format";

const POLL_MS = 30_000;

export function Overview() {
  const stats = useApi<RunStats>("/runs/stats", { refetchInterval: POLL_MS });
  const scores = useApi<{ items: Score[] }>("/quality/scores/latest", { refetchInterval: POLL_MS });
  const blocked = useApi<{ items: GateDecision[]; blocked_count: number }>("/gate/blocked", {
    refetchInterval: POLL_MS,
  });
  const coverage = useApi<{ items: unknown[]; uncovered_families: number }>("/evaluation/coverage");
  const head = useApi<LineageHead>("/lineage/head");
  const evaluation = useApi<{ items: EvaluationSummary[] }>("/evaluation/summary");
  const releases = useApi<{ items: KeyReleaseSummary[] }>("/audit/key-releases/summary");
  const ingestion = useApi<{ items: IngestSummary[] }>("/ingestion/summary");

  const byStatus = stats.data?.by_status ?? [];
  const totalRuns = byStatus.reduce((sum, row) => sum + row.runs, 0);
  const failed = byStatus.find((row) => row.status === "failed")?.runs ?? 0;
  const written = byStatus.reduce((sum, row) => sum + (row.records_written ?? 0), 0);

  // Lowest first: a quality panel exists to surface what is worst, and sorting the
  // other way would make the chart a list of things nobody needs to look at.
  //
  // The label carries the layer as well as the dataset. Scores are per (dataset,
  // layer), so labelling by dataset alone produced two rows both reading
  // "encounter" with different values and no way to tell which was which.
  const worstScores = [...(scores.data?.items ?? [])]
    .filter((row) => row.overall_score !== null)
    .sort((a, b) => (a.overall_score ?? 1) - (b.overall_score ?? 1))
    .slice(0, 8)
    .map((row) => ({
      label: `${row.dataset} · ${row.layer}`,
      value: row.overall_score ?? 0,
      // Status colour used *as* status: this dot is red because the gate blocked
      // the promotion, not because it is the fourth series.
      colour: row.promotion_blocked ? STATUS.bad : row.overall_score! >= 0.95 ? STATUS.good : STATUS.warn,
    }));

  // succeeded = runs - failed. Arithmetic on two columns the endpoint returned,
  // which is the most this page does to anything.
  const runsByType = (stats.data?.by_type ?? []).map((row) => ({
    label: row.run_type,
    succeeded: row.runs - row.failed,
    failed: row.failed,
  }));

  const releaseRows = releases.data?.items ?? [];
  const granted = releaseRows.filter((r) => r.granted).reduce((s, r) => s + r.requests, 0);
  const refused = releaseRows.filter((r) => !r.granted).reduce((s, r) => s + r.requests, 0);

  // Refusals are the interesting slice. A gate that has never refused anything is
  // indistinguishable from no gate, so the donut exists to show the split is not
  // zero — not to help anyone divide 15 by 347.
  const releaseSplit = [
    { label: "granted", value: granted, colour: STATUS.good },
    { label: "refused", value: refused, colour: STATUS.bad },
  ].filter((row) => row.value > 0);

  const evaluationRows = (evaluation.data?.items ?? []).slice(0, SERIES_CAP);
  const evaluationChart = evaluationRows.map((row) => ({
    label: row.dataset,
    precision: row.precision_excl ?? 0,
    recall: row.recall_excl ?? 0,
    f1: row.f1_excl ?? 0,
  }));

  // Labelled by dataset *and* source: the summary is per (dataset, source), so a
  // dataset ingested by two collectors produced two rows both reading "encounter".
  //
  // Nothing is filtered out. `patient_change` appears here because the ingest
  // ledger says it was ingested, which is true — its landing data was later
  // withdrawn (ADR 0023) and the ledger keeps that as metadata the API does not
  // yet expose. Dropping the row would make this console quietly disagree with
  // the ledger it claims to read.
  const ingestChart = [...(ingestion.data?.items ?? [])]
    .filter((row) => (row.rows_ingested ?? 0) > 0)
    .sort((a, b) => (b.rows_ingested ?? 0) - (a.rows_ingested ?? 0))
    .slice(0, 8)
    .map((row, i) => ({
      label: `${row.dataset} · ${row.source}`,
      value: row.rows_ingested ?? 0,
      colour: seriesColour(i % SERIES_CAP),
    }));

  return (
    <>
      <PageHeader
        title="Overview"
        subtitle="Everything on this page is read from the platform metadata store. The API computes no numbers of its own."
      />

      <div className="rise">
        <Grid>
          <Stat
            label="Pipeline runs"
            value={stats.isLoading ? "…" : count(totalRuns)}
            detail={`${count(failed)} failed`}
            tone={failed > 0 ? "warn" : "neutral"}
          />
          <Stat
            label="Records written"
            value={stats.isLoading ? "…" : count(written)}
            detail="across every layer"
          />
          <Stat
            label="Blocked by the gate"
            value={blocked.isLoading ? "…" : count(blocked.data?.blocked_count ?? 0)}
            detail={<Link to="/gate" className="text-accent hover:underline">what and why →</Link>}
            tone={(blocked.data?.blocked_count ?? 0) > 0 ? "bad" : "good"}
          />
          <Stat
            label="Uncovered defect families"
            value={coverage.isLoading ? "…" : count(coverage.data?.uncovered_families ?? 0)}
            detail="families the generator injects with no rule"
            tone={(coverage.data?.uncovered_families ?? 0) > 0 ? "bad" : "good"}
          />
        </Grid>
      </div>

      <div className="rise mt-4 grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card
          title="Lowest quality scores"
          hint="Most recent score per dataset and layer. Dot colour is the gate's verdict; the axis is zoomed to the band the scores occupy."
          className="lg:col-span-2"
        >
          {scores.isLoading ? (
            <Spinner />
          ) : scores.error ? (
            <ErrorState error={scores.error} />
          ) : (
            <div className="p-3">
              <DotPlot data={worstScores} threshold={0.95} height={248} />
              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-ink-700 px-1 pt-2 text-[11px] text-ink-400">
                <LegendKey colour={STATUS.good} label="at or above threshold" />
                <LegendKey colour={STATUS.warn} label="below 0.95" />
                <LegendKey colour={STATUS.bad} label="promotion blocked" />
                <Link to="/quality" className="ml-auto text-accent hover:underline">
                  every rule and failure →
                </Link>
              </div>
            </div>
          )}
        </Card>

        <Card
          title="Key release"
          hint="C2 — every request, granted or refused"
          actions={
            <Link to="/audit" className="text-xs text-accent hover:underline">
              audit →
            </Link>
          }
        >
          {releases.isLoading ? (
            <Spinner />
          ) : releases.error ? (
            <ErrorState error={releases.error} />
          ) : (
            <div className="p-3">
              <SplitDonut
                data={releaseSplit}
                centreValue={count(granted + refused)}
                centreLabel="requests"
                height={196}
              />
              <Callout tone={refused > 0 ? "info" : "warn"}>
                {refused > 0
                  ? `${count(refused)} refusals. A gate that has never refused anything is indistinguishable from no gate.`
                  : "No refusal has been recorded, so nothing here demonstrates the gate works."}
              </Callout>
            </div>
          )}
        </Card>
      </div>

      <div className="rise mt-4 grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card
          title="Rule quality"
          hint="Precision, recall and F1 per dataset, excluding the contaminated Bronze duplicate family"
          className="lg:col-span-2"
        >
          {evaluation.isLoading ? (
            <Spinner />
          ) : evaluation.error ? (
            <ErrorState error={evaluation.error} />
          ) : evaluationChart.length === 0 ? (
            <ChartEmpty>No evaluation has been run.</ChartEmpty>
          ) : (
            <div className="p-3">
              <GroupedColumns
                data={evaluationChart}
                series={[
                  { key: "precision", name: "Precision" },
                  { key: "recall", name: "Recall" },
                  { key: "f1", name: "F1" },
                ]}
                height={240}
              />
              {/* The caveat rides with the number it qualifies. Charting the raw
                  micro-average instead would put a known measurement artefact on
                  the front page as a result. */}
              {evaluationRows.some((row) => row.families_excluded > 0) ? (
                <Callout tone="info">
                  Plotted excluding <Mono>duplicate_record</Mono> at Bronze, where a
                  legitimate redelivery is byte-identical to a duplicate and counts as
                  a false positive.{" "}
                  <Link to="/evaluation" className="text-accent hover:underline">
                    both figures, side by side →
                  </Link>
                </Callout>
              ) : null}
            </div>
          )}
        </Card>

        <Card title="Provenance chain" hint="C3 — signed and Merkle-chained">
          {head.isLoading ? (
            <Spinner />
          ) : head.error ? (
            <ErrorState error={head.error} />
          ) : (
            <div className="space-y-2 p-4 text-sm">
              <div className="flex items-baseline justify-between">
                <span className="text-ink-400">entries</span>
                <span className="tnum text-ink-100">{count(head.data?.entries ?? 0)}</span>
              </div>
              <div className="flex items-baseline justify-between gap-3">
                <span className="text-ink-400">head</span>
                <Mono title={head.data?.head?.entry_digest ?? undefined}>
                  {digest(head.data?.head?.entry_digest, 16)}
                </Mono>
              </div>
              <div className="flex items-baseline justify-between">
                <span className="text-ink-400">recorded</span>
                <span className="text-ink-300">{relative(head.data?.head?.recorded_at)}</span>
              </div>
              <Callout tone="info">
                Recording this digest externally is what makes any later edit to an earlier
                entry detectable.
              </Callout>
              <Link to="/lineage" className="block text-xs text-accent hover:underline">
                verify the chain →
              </Link>
            </div>
          )}
        </Card>
      </div>

      <div className="rise mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="Rows ingested" hint="Per dataset, across every collector">
          {ingestion.isLoading ? (
            <Spinner />
          ) : ingestion.error ? (
            <ErrorState error={ingestion.error} needs="operator" />
          ) : (
            <div className="p-3">
              <RankedBars data={ingestChart} height={240} />
            </div>
          )}
        </Card>

        <Card title="Runs by type" hint="Failures shown against the total that ran">
          {stats.isLoading ? (
            <Spinner />
          ) : stats.error ? (
            <ErrorState error={stats.error} />
          ) : (
            <div className="p-3">
              {/* Stacked, not one bar coloured by whether anything failed. Every
                  run type here has had a failure, so that version was a wall of a
                  single colour with a legend key nothing used. The composition is
                  the point: how many failed against how many ran. */}
              <StackedColumns
                data={runsByType}
                series={[
                  { key: "succeeded", name: "Succeeded", colour: STATUS.good },
                  { key: "failed", name: "Failed", colour: STATUS.bad },
                ]}
                height={248}
              />
              <div className="mt-2 flex items-center border-t border-ink-700 px-1 pt-2 text-[11px]">
                <Link to="/runs" className="ml-auto text-accent hover:underline">
                  every run →
                </Link>
              </div>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}

/** A swatch beside ink-coloured text: identity without colouring the label. */
function LegendKey({ colour, label }: { colour: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="size-2 rounded-[2px]" style={{ background: colour }} aria-hidden />
      {label}
    </span>
  );
}

