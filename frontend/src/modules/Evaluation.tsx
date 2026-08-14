/**
 * How good the quality rules actually are, measured against the ground-truth
 * answer key the generator wrote.
 *
 * Three honesty requirements live in this module and each has a visible
 * consequence:
 *
 * 1. `rule_exists = false` is **not** a bad score. It means the generator injects
 *    that defect family and no rule covers it, so recall is 0 for a reason that has
 *    nothing to do with rule quality. Those rows get their own state and are
 *    excluded from the chart, never quietly averaged in.
 * 2. The `caveats` array from the API is rendered next to the numbers it qualifies,
 *    not as a footnote. At Bronze a duplicate and a legitimate redelivery are the
 *    same bytes, so precision for `duplicate_record` measures the layer's blindness
 *    rather than the rule's accuracy.
 * 3. The summary is the API's micro-average, fetched, not recomputed here.
 *    Averaging per-family F1 in the browser would weight a family with three
 *    defects the same as one with four hundred.
 */
import { useState } from "react";

import { useApi } from "../api/hooks";
import type { Evaluation as EvaluationRow, EvaluationCaveat, EvaluationSummary } from "../api/rows";
import { GroupedColumns, SERIES_CAP } from "../components/charts";
import { Column, DataTable } from "../components/DataTable";
import { Badge, Callout, Card, ErrorState, Grid, PageHeader, Select, Spinner, Stat } from "../components/ui";
import { count, ratio, timestamp } from "../lib/format";

const TENANTS = ["healthcare", "financial"] as const;

interface LatestResponse {
  items: EvaluationRow[];
  caveats: EvaluationCaveat[];
}

export function Evaluation() {
  const [tenant, setTenant] = useState<(typeof TENANTS)[number] | "">("");
  const query = tenant ? { tenant } : {};

  const latest = useApi<LatestResponse>("/evaluation/latest", { query });
  const summary = useApi<{ items: EvaluationSummary[]; method: string }>("/evaluation/summary", { query });
  const coverage = useApi<{ items: EvaluationRow[]; uncovered_families: number }>("/evaluation/coverage");

  const rows = latest.data?.items ?? [];
  const covered = rows.filter((row) => row.rule_exists);
  const uncovered = rows.filter((row) => !row.rule_exists);

  return (
    <>
      <PageHeader
        title="Evaluation"
        subtitle="Precision, recall and F1 of the quality rules against the injected-defect answer key. These measure the rules, not the data."
        actions={<Select label="Tenant" value={tenant} options={TENANTS} onChange={setTenant} />}
      />

      <Grid cols={3}>
        <Stat
          label="Families evaluated"
          value={count(rows.length)}
          detail={`${count(covered.length)} with a rule`}
        />
        <Stat
          label="Families with no rule"
          value={count(coverage.data?.uncovered_families ?? uncovered.length)}
          tone={(coverage.data?.uncovered_families ?? uncovered.length) > 0 ? "bad" : "good"}
          detail="a permanent false negative while it lasts"
        />
        <Stat
          label="Datasets"
          value={count(summary.data?.items.length ?? 0)}
          detail={summary.data?.method ?? "micro-average over defect families"}
        />
      </Grid>

      {(latest.data?.caveats ?? []).map((caveat) => (
        <div key={caveat.note} className="mt-4">
          <Callout
            tone="warn"
            title={`Caveat — ${caveat.applies_to.family ?? "all"} at ${caveat.applies_to.layer ?? "all layers"}`}
          >
            {caveat.note}
          </Callout>
        </div>
      ))}

      <Card
        title="Raw against corrected"
        hint="F1 per dataset, both ways. The gap is the contaminated Bronze duplicate family, not a change in the rules."
        className="mt-4"
      >
        {summary.isLoading ? (
          <Spinner />
        ) : summary.error ? (
          <ErrorState error={summary.error} needs="viewer" />
        ) : (
          <div className="p-3">
            {/* Both figures, side by side, because the page that shows only one of
                them is the page that misleads. Where a dataset has no excluded
                family the two bars are identical, which is itself the useful
                signal: the correction applies to Bronze duplicates and nowhere
                else. */}
            <GroupedColumns
              data={(summary.data?.items ?? []).slice(0, SERIES_CAP).map((row) => ({
                label: `${row.dataset} · ${row.layer}`,
                raw: row.f1 ?? 0,
                corrected: row.f1_excl ?? 0,
              }))}
              series={[
                { key: "raw", name: "F1 as measured" },
                { key: "corrected", name: "F1 excluding duplicate_record" },
              ]}
              height={260}
            />
          </div>
        )}
      </Card>

      <Card title="Per dataset" hint="Micro-averaged by the API from the confusion-matrix counts" className="mt-4">
        {summary.isLoading ? (
          <Spinner />
        ) : summary.error ? (
          <ErrorState error={summary.error} needs="viewer" />
        ) : (
          <DataTable
            rows={summary.data?.items ?? []}
            rowKey={(row) => `${row.tenant}/${row.dataset}/${row.layer}`}
            columns={[
              {
                key: "dataset",
                header: "Dataset",
                render: (row) => (
                  <span>
                    {row.dataset}
                    <span className="ml-2 text-xs text-ink-500">{row.tenant}</span>
                  </span>
                ),
              },
              { key: "layer", header: "Layer", render: (row) => <Badge>{row.layer}</Badge> },
              {
                key: "precision",
                header: <span title="Excluding the contaminated Bronze duplicate family">Precision*</span>,
                numeric: true,
                render: (row) => ratio(row.precision_excl),
              },
              {
                key: "recall",
                header: <span title="Excluding the contaminated Bronze duplicate family">Recall*</span>,
                numeric: true,
                render: (row) => ratio(row.recall_excl),
              },
              {
                key: "f1",
                header: <span title="Excluding the contaminated Bronze duplicate family">F1*</span>,
                numeric: true,
                render: (row) => <strong className="text-ink-100">{ratio(row.f1_excl)}</strong>,
              },
              {
                // Shown next to the corrected figure rather than replaced by it. The
                // raw number is what the harness measured and hiding it would be a
                // different kind of dishonesty from publishing it unqualified.
                key: "raw",
                header: <span title="Every family, including the contaminated one">F1 raw</span>,
                numeric: true,
                render: (row) =>
                  row.families_excluded > 0 ? (
                    <span className="text-ink-500" title={`${row.families_excluded} family excluded above`}>
                      {ratio(row.f1)}
                    </span>
                  ) : (
                    <span className="text-ink-600">same</span>
                  ),
              },
              { key: "tp", header: "TP", numeric: true, render: (row) => count(row.tp) },
              { key: "fp", header: "FP", numeric: true, render: (row) => count(row.fp) },
              { key: "fn", header: "FN", numeric: true, render: (row) => count(row.fn) },
              {
                key: "uncovered",
                header: "Uncovered",
                numeric: true,
                render: (row) =>
                  row.families_uncovered > 0 ? (
                    <Badge tone="bad">{row.families_uncovered}</Badge>
                  ) : (
                    <span className="text-ink-500">0</span>
                  ),
              },
            ]}
          />
        )}
      </Card>

      <Card
        title="Per defect family"
        hint="Ordered worst first. A family with no rule is shown as uncovered rather than as a low score."
        className="mt-4"
      >
        {latest.isLoading ? (
          <Spinner />
        ) : latest.error ? (
          <ErrorState error={latest.error} needs="viewer" />
        ) : (
          <DataTable rows={rows} rowKey={(row, i) => `${row.dataset}/${row.family}/${i}`} columns={FAMILY_COLUMNS} />
        )}
      </Card>
    </>
  );
}

const FAMILY_COLUMNS: Column<EvaluationRow>[] = [
  {
    key: "family",
    header: "Defect family",
    render: (row) => (
      <span>
        {row.family}
        <span className="ml-2 text-xs text-ink-500">
          {row.dataset} · {row.layer}
        </span>
      </span>
    ),
  },
  {
    key: "state",
    header: "State",
    render: (row) =>
      row.rule_exists ? (
        <Badge tone="neutral">covered</Badge>
      ) : (
        <Badge tone="bad" title="The generator injects this defect and no rule detects it.">
          no rule
        </Badge>
      ),
  },
  {
    key: "precision",
    header: "Precision",
    numeric: true,
    render: (row) => (row.rule_exists ? ratio(row.precision) : <span className="text-ink-600">n/a</span>),
  },
  {
    key: "recall",
    header: "Recall",
    numeric: true,
    render: (row) => (row.rule_exists ? ratio(row.recall) : <span className="text-ink-600">n/a</span>),
  },
  {
    key: "f1",
    header: "F1",
    numeric: true,
    render: (row) => (row.rule_exists ? ratio(row.f1) : <span className="text-ink-600">n/a</span>),
  },
  { key: "truth", header: "In answer key", numeric: true, render: (row) => count(row.truth_claims) },
  { key: "detected", header: "Detected", numeric: true, render: (row) => count(row.detected_claims) },
  {
    key: "evaluated",
    header: "Evaluated",
    render: (row) => <span className="text-xs text-ink-400">{timestamp(row.evaluated_at)}</span>,
  },
];
