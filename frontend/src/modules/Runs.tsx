/**
 * Pipeline run history.
 *
 * `error_message` is fetched only when a row is opened. The API omits it from the
 * list response on purpose -- a failure message can echo a fragment of the
 * offending record, and the list is what a dashboard polls -- so this module has
 * to make asking for it a deliberate act, which is what the detail panel is.
 */
import { useState } from "react";

import { useApi } from "../api/hooks";
import type { Envelope, Run } from "../api/rows";
import { Column, DataTable, PageFooter } from "../components/DataTable";
import { Badge, Button, Card, ErrorState, Mono, PageHeader, Select, Spinner, type Tone } from "../components/ui";
import { count, duration, percent, timestamp } from "../lib/format";

const TENANTS = ["healthcare", "financial"] as const;
const STATUSES = ["succeeded", "failed", "running", "blocked"] as const;
const LIMIT = 25;

const STATUS_TONE: Record<string, Tone> = {
  succeeded: "good",
  failed: "bad",
  running: "info",
  blocked: "warn",
};

export function Runs() {
  const [tenant, setTenant] = useState<(typeof TENANTS)[number] | "">("");
  const [status, setStatus] = useState<(typeof STATUSES)[number] | "">("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);

  const runs = useApi<Envelope<Run>>("/runs", {
    query: { limit: LIMIT, offset, ...(tenant ? { tenant } : {}), ...(status ? { status } : {}) },
  });

  return (
    <>
      <PageHeader
        title="Pipeline runs"
        subtitle="Every execution recorded by the platform, newest first. Durations and rejection rates come from the run_summary view rather than being computed here."
      />

      <Card
        title="Runs"
        actions={
          <>
            <Select
              label="Tenant"
              value={tenant}
              options={TENANTS}
              onChange={(value) => {
                setTenant(value);
                setOffset(0);
              }}
            />
            <Select
              label="Status"
              value={status}
              options={STATUSES}
              onChange={(value) => {
                setStatus(value);
                setOffset(0);
              }}
            />
          </>
        }
      >
        {runs.isLoading ? (
          <Spinner />
        ) : runs.error ? (
          <ErrorState error={runs.error} needs="viewer" />
        ) : (
          <DataTable
            rows={runs.data?.items ?? []}
            rowKey={(row) => row.run_id}
            columns={columns(setSelected)}
            empty="No run matches these filters."
            footer={
              <PageFooter
                returned={runs.data?.page?.returned ?? 0}
                total={runs.data?.total}
                limit={LIMIT}
                offset={offset}
                onOffset={setOffset}
              />
            }
          />
        )}
      </Card>

      {selected ? <RunDetail runId={selected} onClose={() => setSelected(null)} /> : null}
    </>
  );
}

function columns(onSelect: (id: string) => void): Column<Run>[] {
  return [
    {
      key: "started",
      header: "Started",
      render: (row) => <span className="text-xs text-ink-300">{timestamp(row.started_at)}</span>,
    },
    { key: "type", header: "Type", render: (row) => <Badge>{row.run_type}</Badge> },
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
    {
      key: "status",
      header: "Status",
      render: (row) => (
        <Badge tone={STATUS_TONE[row.status] ?? "neutral"}>{row.status}</Badge>
      ),
    },
    { key: "duration", header: "Duration", numeric: true, render: (row) => duration(row.duration_ms) },
    { key: "read", header: "Read", numeric: true, render: (row) => count(row.records_read) },
    { key: "written", header: "Written", numeric: true, render: (row) => count(row.records_written) },
    {
      key: "rejected",
      header: "Rejected",
      numeric: true,
      render: (row) =>
        row.records_rejected ? (
          <span className="text-warn" title={`${percent(row.rejection_rate)} of rows read`}>
            {count(row.records_rejected)}
          </span>
        ) : (
          count(row.records_rejected)
        ),
    },
    {
      key: "detail",
      header: "",
      render: (row) => (
        <button
          type="button"
          onClick={() => onSelect(row.run_id)}
          className="text-xs text-accent hover:underline"
        >
          details
        </button>
      ),
    },
  ];
}

/**
 * The detail panel, which is the only place `error_message` is requested.
 */
function RunDetail({ runId, onClose }: { runId: string; onClose: () => void }) {
  const run = useApi<Run>("/runs/{run_id}", { params: { run_id: runId } });

  return (
    <div className="mt-4">
      <Card
        title="Run detail"
        hint={<Mono>{runId}</Mono>}
        actions={<Button onClick={onClose}>Close</Button>}
      >
        {run.isLoading ? (
          <Spinner />
        ) : run.error ? (
          <ErrorState error={run.error} />
        ) : run.data ? (
          <dl className="grid grid-cols-2 gap-x-8 gap-y-2 text-sm sm:grid-cols-4">
            <Field label="Tenant" value={run.data.tenant} />
            <Field label="Dataset" value={run.data.dataset} />
            <Field label="Source" value={run.data.source ?? "—"} />
            <Field label="Attempt" value={String(run.data.attempt)} />
            <Field label="Started" value={timestamp(run.data.started_at)} />
            <Field label="Finished" value={timestamp(run.data.finished_at)} />
            <Field label="Duration" value={duration(run.data.duration_ms)} />
            <Field label="Rejection rate" value={percent(run.data.rejection_rate)} />
            {run.data.error_type ? (
              <div className="col-span-full">
                <dt className="text-xs tracking-wide text-ink-500 uppercase">
                  {run.data.error_type}
                </dt>
                <dd className="mt-1 rounded border border-bad/25 bg-bad/8 p-3 font-mono text-xs break-words whitespace-pre-wrap text-bad">
                  {run.data.error_message ?? "no message recorded"}
                </dd>
              </div>
            ) : null}
          </dl>
        ) : null}
      </Card>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs tracking-wide text-ink-500 uppercase">{label}</dt>
      <dd className="tnum mt-0.5 text-ink-200">{value}</dd>
    </div>
  );
}
