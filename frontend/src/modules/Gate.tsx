/**
 * The promotion gate.
 *
 * "Currently blocked" is a definition — the latest decision per dataset, not
 * overridden — and it lives in the `promotion_blocked_now` view. This module reads
 * that view through the API rather than deriving the state from the decision
 * history, which is the same rule the API follows for the same reason: a second
 * copy of the definition is how the console and the CLI come to disagree.
 *
 * Overrides get their own panel. A gate nobody can override is a gate that gets
 * switched off the first time it is wrong at an inconvenient moment, so overriding
 * is allowed and recorded — and "how often is it overridden" needs to be a question
 * with a visible answer, or the recording is decorative.
 */
import { useState } from "react";

import { useApi } from "../api/hooks";
import type { Envelope, GateDecision } from "../api/rows";
import { Column, DataTable, PageFooter } from "../components/DataTable";
import { Badge, Callout, Card, ErrorState, PageHeader, ScoreBar, Select, Spinner } from "../components/ui";
import { timestamp } from "../lib/format";

const TENANTS = ["healthcare", "financial"] as const;

export function Gate() {
  const [tenant, setTenant] = useState<(typeof TENANTS)[number] | "">("");
  const [offset, setOffset] = useState(0);
  const query = tenant ? { tenant } : {};

  const blocked = useApi<{ items: GateDecision[]; blocked_count: number }>("/gate/blocked", { query });
  const decisions = useApi<Envelope<GateDecision>>("/gate/decisions", {
    query: { limit: 25, offset, ...query },
  });
  const overrides = useApi<Envelope<GateDecision>>("/gate/overrides", { query: { limit: 10 } });

  return (
    <>
      <PageHeader
        title="Promotion gate"
        subtitle="Silver is not promoted to Gold below the quality threshold. This is the enforcement point, not a report about one."
        actions={<Select label="Tenant" value={tenant} options={TENANTS} onChange={setTenant} />}
      />

      <Card
        title="Blocked now"
        hint="From the promotion_blocked_now view — the latest decision per dataset, excluding overrides"
      >
        {blocked.isLoading ? (
          <Spinner />
        ) : blocked.error ? (
          <ErrorState error={blocked.error} needs="viewer" />
        ) : (blocked.data?.items.length ?? 0) === 0 ? (
          <Callout tone="good" title="Nothing is blocked">
            Every dataset's most recent score is at or above its threshold.
          </Callout>
        ) : (
          <DataTable
            rows={blocked.data?.items ?? []}
            rowKey={(row) => `${row.tenant}/${row.dataset}/${row.layer}`}
            columns={BLOCKED_COLUMNS}
          />
        )}
      </Card>

      <Card title="Decision history" className="mt-4">
        {decisions.isLoading ? (
          <Spinner />
        ) : decisions.error ? (
          <ErrorState error={decisions.error} needs="operator" />
        ) : (
          <DataTable
            rows={decisions.data?.items ?? []}
            rowKey={(row) => String(row.gate_id)}
            columns={[
              { key: "when", header: "Decided", render: (row) => <span className="text-xs text-ink-300">{timestamp(row.decided_at)}</span> },
              {
                key: "dataset",
                header: "Dataset",
                render: (row) => (
                  <span>
                    {row.dataset}
                    <span className="ml-2 text-xs text-ink-500">{row.tenant} · {row.layer}</span>
                  </span>
                ),
              },
              { key: "score", header: "Score", render: (row) => <ScoreBar value={row.overall_score} threshold={row.threshold} /> },
              {
                key: "decision",
                header: "Decision",
                render: (row) => (
                  <Badge tone={row.blocked ? "bad" : "good"}>{row.decision}</Badge>
                ),
              },
              {
                key: "overridden",
                header: "Override",
                render: (row) => (row.overridden ? <Badge tone="warn">overridden</Badge> : "—"),
              },
              { key: "reason", header: "Reason", render: (row) => <span className="text-xs text-ink-400">{row.reason ?? "—"}</span> },
            ]}
            footer={
              <PageFooter
                returned={decisions.data?.page?.returned ?? 0}
                total={decisions.data?.total}
                limit={25}
                offset={offset}
                onOffset={setOffset}
              />
            }
          />
        )}
      </Card>

      <Card
        title="Overrides"
        hint="Recorded on purpose: an unoverridable gate is a gate that gets disabled instead"
        className="mt-4"
      >
        {overrides.isLoading ? (
          <Spinner />
        ) : overrides.error ? (
          <ErrorState error={overrides.error} needs="operator" />
        ) : (
          <DataTable
            rows={overrides.data?.items ?? []}
            rowKey={(row) => String(row.gate_id)}
            empty="The gate has never been overridden."
            columns={[
              { key: "when", header: "Decided", render: (row) => <span className="text-xs text-ink-300">{timestamp(row.decided_at)}</span> },
              { key: "dataset", header: "Dataset", render: (row) => `${row.tenant}/${row.dataset}` },
              { key: "score", header: "Score", render: (row) => <ScoreBar value={row.overall_score} threshold={row.threshold} /> },
              { key: "reason", header: "Reason", render: (row) => <span className="text-xs text-ink-400">{row.reason ?? "—"}</span> },
            ]}
          />
        )}
      </Card>
    </>
  );
}

const BLOCKED_COLUMNS: Column<GateDecision>[] = [
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
  { key: "score", header: "Score vs threshold", render: (row) => <ScoreBar value={row.overall_score} threshold={row.threshold} width={140} /> },
  { key: "decision", header: "Decision", render: (row) => <Badge tone="bad">{row.decision}</Badge> },
  { key: "reason", header: "Reason", render: (row) => <span className="text-xs text-ink-400">{row.reason ?? "—"}</span> },
  { key: "when", header: "Decided", render: (row) => <span className="text-xs text-ink-400">{timestamp(row.decided_at)}</span> },
];
