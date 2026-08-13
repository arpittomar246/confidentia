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
 */
import { Link } from "react-router-dom";

import { useApi } from "../api/hooks";
import type { EvaluationSummary, GateDecision, LineageHead, RunStats, Score } from "../api/rows";
import { Column, DataTable } from "../components/DataTable";
import { Badge, Callout, Card, ErrorState, Grid, Mono, PageHeader, ScoreBar, Spinner, Stat } from "../components/ui";
import { count, digest, ratio, relative, timestamp } from "../lib/format";

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

  const byStatus = stats.data?.by_status ?? [];
  const totalRuns = byStatus.reduce((sum, row) => sum + row.runs, 0);
  const failed = byStatus.find((row) => row.status === "failed")?.runs ?? 0;
  const written = byStatus.reduce((sum, row) => sum + (row.records_written ?? 0), 0);

  const worst = [...(scores.data?.items ?? [])]
    .filter((row) => row.overall_score !== null)
    .sort((a, b) => (a.overall_score ?? 1) - (b.overall_score ?? 1))
    .slice(0, 8);

  return (
    <>
      <PageHeader
        title="Overview"
        subtitle="Everything on this page is read from the platform metadata store. The API computes no numbers of its own."
      />

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

      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card
          title="Lowest quality scores"
          hint="Most recent score per dataset and layer; threshold marked on the bar"
          className="lg:col-span-2"
        >
          {scores.isLoading ? (
            <Spinner />
          ) : scores.error ? (
            <ErrorState error={scores.error} />
          ) : (
            <DataTable
              rows={worst}
              rowKey={(row) => `${row.tenant}/${row.dataset}/${row.layer}`}
              columns={SCORE_COLUMNS}
              empty="No quality run has been scored yet."
            />
          )}
        </Card>

        <div className="space-y-4">
          <Card title="Provenance chain" hint="C3 — signed and Merkle-chained">
            {head.isLoading ? (
              <Spinner />
            ) : head.error ? (
              <ErrorState error={head.error} />
            ) : (
              <div className="space-y-2 text-sm">
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
                  Recording this digest externally is what makes any later edit to an earlier entry
                  detectable.
                </Callout>
                <Link to="/lineage" className="block text-xs text-accent hover:underline">
                  verify the chain →
                </Link>
              </div>
            )}
          </Card>

          <Card
            title="Rule quality"
            hint="Micro-averaged F1, excluding the contaminated Bronze duplicate family"
          >
            {evaluation.isLoading ? (
              <Spinner />
            ) : evaluation.error ? (
              <ErrorState error={evaluation.error} />
            ) : (
              <div className="space-y-1.5 text-sm">
                {(evaluation.data?.items ?? []).map((row) => (
                  <div
                    key={`${row.tenant}/${row.dataset}/${row.layer}`}
                    className="flex items-baseline justify-between gap-3"
                  >
                    <span className="truncate text-ink-300" title={`${row.tenant}/${row.dataset}`}>
                      {row.dataset}
                      <span className="ml-1.5 text-xs text-ink-500">{row.layer}</span>
                    </span>
                    <span className="tnum shrink-0 text-xs text-ink-400">
                      {/* f1_excl, never f1. The raw figure counts every legitimate
                          redelivery at Bronze as a false positive, and putting it on
                          a summary tile is how a measurement artefact becomes a
                          reported result. */}
                      F1 <span className="text-ink-100">{ratio(row.f1_excl)}</span>
                      {row.families_excluded > 0 ? (
                        <span
                          className="ml-1 text-suppressed"
                          title={`raw F1 ${ratio(row.f1)} including the contaminated duplicate_record family`}
                        >
                          *
                        </span>
                      ) : null}
                    </span>
                  </div>
                ))}
                <Link to="/evaluation" className="block pt-1 text-xs text-accent hover:underline">
                  precision, recall and caveats →
                </Link>
              </div>
            )}
          </Card>
        </div>
      </div>

      <Card title="Runs by type" className="mt-4">
        {stats.isLoading ? (
          <Spinner />
        ) : stats.error ? (
          <ErrorState error={stats.error} />
        ) : (
          <DataTable
            rows={stats.data?.by_type ?? []}
            rowKey={(row) => row.run_type}
            empty="No runs recorded."
            columns={[
              { key: "type", header: "Run type", render: (row) => row.run_type },
              { key: "runs", header: "Runs", numeric: true, render: (row) => count(row.runs) },
              {
                key: "failed",
                header: "Failed",
                numeric: true,
                render: (row) =>
                  row.failed > 0 ? <Badge tone="bad">{count(row.failed)}</Badge> : count(0),
              },
            ]}
          />
        )}
      </Card>
    </>
  );
}

const SCORE_COLUMNS: Column<Score>[] = [
  {
    key: "dataset",
    header: "Dataset",
    render: (row) => (
      <span className="text-ink-200">
        {row.dataset}
        <span className="ml-2 text-xs text-ink-500">{row.tenant}</span>
      </span>
    ),
  },
  { key: "layer", header: "Layer", render: (row) => <Badge>{row.layer}</Badge> },
  {
    key: "score",
    header: "Overall",
    render: (row) => <ScoreBar value={row.overall_score} threshold={0.95} />,
  },
  {
    key: "blocked",
    header: "Gate",
    render: (row) =>
      row.promotion_blocked ? <Badge tone="bad">blocked</Badge> : <Badge tone="good">passed</Badge>,
  },
  { key: "scored", header: "Scored", render: (row) => <span className="text-xs text-ink-400">{timestamp(row.scored_at)}</span> },
];
