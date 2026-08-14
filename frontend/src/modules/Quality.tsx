/**
 * Data quality: six dimensions, the rule catalogue, failures, freshness, quarantine.
 *
 * Quarantine is the part with a rule attached. The list is operator-level and shows
 * reason codes; the *contents* of a quarantined row are auditor-level, because
 * "redacted" means the sensitive fields were removed, not that the rest is public.
 * This module surfaces that boundary rather than hiding it — an operator sees the
 * row exists and is told what reading it would require.
 */
import { lazy, Suspense, useState } from "react";

// Charting is 380 kB of the bundle for one panel. Split out so the other eight
// modules -- and the sign-in screen -- never download it. It arrives when someone
// opens the Scores tab, which is the only place it is used.
const ScoreHistory = lazy(() =>
  import("../components/ScoreHistory").then((module) => ({ default: module.ScoreHistory })),
);

import { useApi } from "../api/hooks";
import { DIMENSIONS, type Envelope, type Failure, type Freshness, type QuarantineRow, type QuarantineSummary, type Rule, type Score } from "../api/rows";
import { Heatmap } from "../components/charts";
import { Column, DataTable, PageFooter } from "../components/DataTable";
import { Badge, Callout, Card, ErrorState, Mono, PageHeader, ScoreBar, Select, Spinner, type Tone } from "../components/ui";
import { count, duration, percent, ratio, timestamp, titleCase } from "../lib/format";

const TABS = ["Scores", "Rules", "Failures", "Freshness", "Quarantine"] as const;
type Tab = (typeof TABS)[number];

const TENANTS = ["healthcare", "financial"] as const;
const SEVERITY_TONE: Record<string, Tone> = { critical: "bad", major: "warn", minor: "neutral" };

export function Quality() {
  const [tab, setTab] = useState<Tab>("Scores");
  const [tenant, setTenant] = useState<(typeof TENANTS)[number] | "">("");

  return (
    <>
      <PageHeader
        title="Data quality"
        subtitle="Six dimensions, scored per dataset and layer against contract-derived rules. A score is meaningless without its threshold, so the threshold is drawn on every bar."
      />

      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <nav className="flex gap-1 rounded-lg border border-ink-700 bg-ink-900 p-1">
          {TABS.map((name) => (
            <button
              key={name}
              type="button"
              onClick={() => setTab(name)}
              className={`rounded px-3 py-1.5 text-xs transition ${
                tab === name ? "bg-accent/12 font-medium text-accent" : "text-ink-400 hover:text-ink-100"
              }`}
            >
              {name}
            </button>
          ))}
        </nav>
        <Select label="Tenant" value={tenant} options={TENANTS} onChange={setTenant} />
      </div>

      {tab === "Scores" ? <Scores tenant={tenant} /> : null}
      {tab === "Rules" ? <Rules tenant={tenant} /> : null}
      {tab === "Failures" ? <Failures tenant={tenant} /> : null}
      {tab === "Freshness" ? <FreshnessPanel tenant={tenant} /> : null}
      {tab === "Quarantine" ? <Quarantine tenant={tenant} /> : null}
    </>
  );
}

type TenantFilter = (typeof TENANTS)[number] | "";
const withTenant = (tenant: TenantFilter) => (tenant ? { tenant } : {});

function Scores({ tenant }: { tenant: TenantFilter }) {
  const latest = useApi<{ items: Score[] }>("/quality/scores/latest", { query: withTenant(tenant) });
  // The history is paged like everything else; 200 rows is several weeks of runs
  // here and is the point at which the line stops telling you anything new.
  const history = useApi<Envelope<Score>>("/quality/scores", {
    query: { limit: 200, ...withTenant(tenant) },
  });

  return (
    <div className="space-y-4">
    <Card title="Overall score over time" hint="Axis starts near the threshold, not at zero — every score sits between 0.8 and 1.0">
      {history.isLoading ? (
        <Spinner />
      ) : history.error ? (
        <ErrorState error={history.error} needs="viewer" />
      ) : (
        <Suspense fallback={<Spinner label="Loading chart" />}>
          <ScoreHistory scores={history.data?.items ?? []} />
        </Suspense>
      )}
    </Card>

    <Card
      title="Six dimensions, every dataset"
      hint="Where quality is weak, and whether it is weak in one place or everywhere"
    >
      {latest.isLoading ? (
        <Spinner />
      ) : latest.error ? (
        <ErrorState error={latest.error} needs="viewer" />
      ) : (
        <div className="p-3">
          {/* The one shape a table cannot show. Seventy-odd numbers in a grid are
              read one at a time; the question here is about the pattern — is
              `timeliness` weak everywhere, or is one dataset dragging it down. */}
          <Heatmap
            rows={(latest.data?.items ?? []).map((row) => `${row.dataset} · ${row.layer}`)}
            columns={[...DIMENSIONS]}
            cells={(latest.data?.items ?? []).flatMap((row) =>
              DIMENSIONS.map((dimension) => ({
                row: `${row.dataset} · ${row.layer}`,
                column: dimension,
                value: row[dimension],
              })),
            )}
            // Fixed, not fitted to the data: a ramp stretched over 0.99–1.00 would
            // render a 1% spread as the full range of the scale.
            domain={[0.9, 1]}
            threshold={0.95}
          />
        </div>
      )}
    </Card>

    <Card title="Latest score per dataset" hint="Threshold 0.950 — below it the gate blocks promotion">
      {latest.isLoading ? (
        <Spinner />
      ) : latest.error ? (
        <ErrorState error={latest.error} needs="viewer" />
      ) : (
        <DataTable
          rows={latest.data?.items ?? []}
          rowKey={(row) => `${row.tenant}/${row.dataset}/${row.layer}`}
          empty="Nothing scored yet. Run `confidentia quality run`."
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
              key: "overall",
              header: "Overall",
              render: (row) => <ScoreBar value={row.overall_score} threshold={0.95} />,
            },
            ...DIMENSIONS.map((dimension) => ({
              key: dimension,
              header: <span title={titleCase(dimension)}>{dimension.slice(0, 4)}</span>,
              numeric: true,
              render: (row: Score) => <DimensionCell value={row[dimension]} />,
            })),
            { key: "rows", header: "Rows", numeric: true, render: (row) => count(row.rows_total) },
            {
              key: "failing",
              header: "Failing",
              numeric: true,
              render: (row) =>
                row.rows_failing ? (
                  <span className="text-warn">{count(row.rows_failing)}</span>
                ) : (
                  count(0)
                ),
            },
          ]}
        />
      )}
    </Card>
    </div>
  );
}

function DimensionCell({ value }: { value: number | null }) {
  if (value === null) return <span className="text-ink-600">—</span>;
  const tone = value >= 0.99 ? "text-good" : value >= 0.95 ? "text-ink-200" : value >= 0.8 ? "text-warn" : "text-bad";
  return <span className={tone}>{ratio(value)}</span>;
}

function Rules({ tenant }: { tenant: TenantFilter }) {
  const [offset, setOffset] = useState(0);
  const rules = useApi<Envelope<Rule>>("/quality/rules", {
    query: { limit: 50, offset, ...withTenant(tenant) },
  });

  return (
    <Card title="Rule catalogue" hint="Derived from the schema contracts; severity drives the weighted score">
      {rules.isLoading ? (
        <Spinner />
      ) : rules.error ? (
        <ErrorState error={rules.error} needs="viewer" />
      ) : (
        <DataTable
          rows={rules.data?.items ?? []}
          rowKey={(row) => row.rule_id}
          columns={RULE_COLUMNS}
          footer={
            <PageFooter
              returned={rules.data?.page?.returned ?? 0}
              total={rules.data?.total}
              limit={50}
              offset={offset}
              onOffset={setOffset}
            />
          }
        />
      )}
    </Card>
  );
}

const RULE_COLUMNS: Column<Rule>[] = [
  { key: "id", header: "Rule", render: (row) => <Mono>{row.rule_id}</Mono> },
  { key: "dataset", header: "Dataset", render: (row) => row.dataset },
  { key: "dimension", header: "Dimension", render: (row) => <Badge>{row.dimension}</Badge> },
  { key: "field", header: "Field", render: (row) => row.field ?? "—" },
  {
    key: "severity",
    header: "Severity",
    render: (row) => <Badge tone={SEVERITY_TONE[row.severity] ?? "neutral"}>{row.severity}</Badge>,
  },
  {
    key: "description",
    header: "Description",
    render: (row) => <span className="text-xs text-ink-400">{row.description ?? "—"}</span>,
  },
];

function Failures({ tenant }: { tenant: TenantFilter }) {
  const [offset, setOffset] = useState(0);
  const failures = useApi<Envelope<Failure>>("/quality/failures", {
    query: { limit: 25, offset, ...withTenant(tenant) },
  });

  return (
    <Card title="Failing rules" hint="Worst first, by rows failed. Joined to the catalogue so a failure arrives with its meaning.">
      {failures.isLoading ? (
        <Spinner />
      ) : failures.error ? (
        <ErrorState error={failures.error} needs="viewer" />
      ) : (
        <DataTable
          rows={failures.data?.items ?? []}
          rowKey={(row) => String(row.result_id)}
          empty="No rule is currently failing."
          columns={[
            { key: "rule", header: "Rule", render: (row) => <Mono>{row.rule_id}</Mono> },
            {
              key: "dataset",
              header: "Dataset",
              render: (row) => (
                <span>
                  {row.dataset}
                  <span className="ml-2 text-xs text-ink-500">{row.layer}</span>
                </span>
              ),
            },
            { key: "field", header: "Field", render: (row) => row.field ?? "—" },
            {
              key: "severity",
              header: "Severity",
              render: (row) =>
                row.severity ? (
                  <Badge tone={SEVERITY_TONE[row.severity] ?? "neutral"}>{row.severity}</Badge>
                ) : (
                  "—"
                ),
            },
            { key: "failed", header: "Rows failed", numeric: true, render: (row) => count(row.rows_failed) },
            { key: "evaluated", header: "Of", numeric: true, render: (row) => count(row.rows_evaluated) },
            {
              key: "rate",
              header: "Pass rate",
              numeric: true,
              render: (row) => <span className={(row.pass_rate ?? 1) < 0.9 ? "text-bad" : ""}>{percent(row.pass_rate, 2)}</span>,
            },
            { key: "when", header: "Evaluated", render: (row) => <span className="text-xs text-ink-400">{timestamp(row.evaluated_at)}</span> },
          ]}
          footer={
            <PageFooter
              returned={failures.data?.page?.returned ?? 0}
              total={failures.data?.total}
              limit={25}
              offset={offset}
              onOffset={setOffset}
            />
          }
        />
      )}
    </Card>
  );
}

function FreshnessPanel({ tenant }: { tenant: TenantFilter }) {
  const freshness = useApi<{ items: Freshness[] }>("/quality/freshness", { query: withTenant(tenant) });

  return (
    <Card title="Freshness" hint="Lag between the newest event and now, against the SLA">
      {freshness.isLoading ? (
        <Spinner />
      ) : freshness.error ? (
        <ErrorState error={freshness.error} needs="viewer" />
      ) : (
        <DataTable
          rows={freshness.data?.items ?? []}
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
              key: "lag",
              header: "Lag",
              numeric: true,
              render: (row) => duration(row.lag_minutes === null ? null : row.lag_minutes * 60_000),
            },
            { key: "sla", header: "SLA", numeric: true, render: (row) => `${count(row.sla_minutes)} min` },
            {
              key: "state",
              header: "State",
              render: (row) =>
                row.breached ? <Badge tone="bad">breached</Badge> : <Badge tone="good">within SLA</Badge>,
            },
            { key: "event", header: "Newest event", render: (row) => <span className="text-xs text-ink-400">{timestamp(row.max_event_ts)}</span> },
          ]}
        />
      )}
    </Card>
  );
}

function Quarantine({ tenant }: { tenant: TenantFilter }) {
  const [offset, setOffset] = useState(0);
  const [opened, setOpened] = useState<number | null>(null);

  const summary = useApi<{ items: QuarantineSummary[] }>("/quality/quarantine/summary", {
    query: withTenant(tenant),
  });
  const rows = useApi<Envelope<QuarantineRow>>("/quality/quarantine", {
    query: { limit: 25, offset, ...withTenant(tenant) },
  });
  const detail = useApi<QuarantineRow>("/quality/quarantine/{quarantine_id}", {
    params: { quarantine_id: opened ?? 0 },
    enabled: opened !== null,
  });

  return (
    <div className="space-y-4">
      <Callout tone="info" title="Quarantine is not deletion">
        A row that fails a critical rule is written here rather than dropped, so the count below is
        recoverable data, not lost data. Replaying one is a CLI operation — this console cannot
        write.
      </Callout>

      <Card title="By reason" hint="Reason codes only; no record contents">
        {summary.isLoading ? (
          <Spinner />
        ) : summary.error ? (
          <ErrorState error={summary.error} needs="operator" />
        ) : (
          <DataTable
            rows={summary.data?.items ?? []}
            rowKey={(row) => `${row.tenant}/${row.dataset}/${row.reason_code}`}
            columns={[
              { key: "dataset", header: "Dataset", render: (row) => row.dataset },
              { key: "reason", header: "Reason", render: (row) => <Mono>{row.reason_code}</Mono> },
              {
                key: "severity",
                header: "Severity",
                render: (row) =>
                  row.severity ? (
                    <Badge tone={SEVERITY_TONE[row.severity] ?? "neutral"}>{row.severity}</Badge>
                  ) : (
                    "—"
                  ),
              },
              { key: "records", header: "Records", numeric: true, render: (row) => count(row.records) },
              { key: "replayed", header: "Replayed", numeric: true, render: (row) => count(row.replayed) },
              { key: "latest", header: "Latest", render: (row) => <span className="text-xs text-ink-400">{timestamp(row.latest)}</span> },
            ]}
          />
        )}
      </Card>

      <Card title="Quarantined rows">
        {rows.isLoading ? (
          <Spinner />
        ) : rows.error ? (
          <ErrorState error={rows.error} needs="operator" />
        ) : (
          <DataTable
            rows={rows.data?.items ?? []}
            rowKey={(row) => String(row.quarantine_id)}
            columns={[
              { key: "when", header: "Quarantined", render: (row) => <span className="text-xs text-ink-300">{timestamp(row.quarantined_at)}</span> },
              { key: "dataset", header: "Dataset", render: (row) => row.dataset },
              { key: "key", header: "Business key", render: (row) => <Mono>{row.business_key ?? "—"}</Mono> },
              { key: "reason", header: "Reason", render: (row) => <Mono>{row.reason_code}</Mono> },
              { key: "rule", header: "Failed rule", render: (row) => <Mono>{row.failed_rule ?? "—"}</Mono> },
              {
                key: "open",
                header: "",
                render: (row) => (
                  <button
                    type="button"
                    onClick={() => setOpened(row.quarantine_id)}
                    className="text-xs text-accent hover:underline"
                  >
                    contents
                  </button>
                ),
              },
            ]}
            footer={
              <PageFooter
                returned={rows.data?.page?.returned ?? 0}
                total={rows.data?.total}
                limit={25}
                offset={offset}
                onOffset={setOffset}
              />
            }
          />
        )}
      </Card>

      {opened !== null ? (
        <Card
          title={`Quarantined record #${opened}`}
          hint="Auditor-only. Sensitive fields were removed when it was written; the remainder is still a real row."
        >
          {detail.isLoading ? (
            <Spinner />
          ) : detail.error ? (
            <ErrorState error={detail.error} needs="auditor" />
          ) : (
            <pre className="overflow-x-auto rounded border border-ink-700 bg-ink-950 p-3 font-mono text-xs text-ink-300">
              {JSON.stringify(detail.data?.redacted_record ?? {}, null, 2)}
            </pre>
          )}
        </Card>
      ) : null}
    </div>
  );
}
