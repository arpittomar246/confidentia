/**
 * Ingestion state: batches, redelivery, and watermarks.
 *
 * `seen_count > 1` is rendered as a *success*, not a warning. A batch whose content
 * digest has been presented several times was ingested once and skipped after that
 * — the exactly-once guarantee doing its job. Showing it as an error would train
 * an operator to investigate the mechanism that is protecting them.
 *
 * `watermark_kind` is shown next to every watermark value because the values are
 * opaque strings of three different kinds — an LSN, a timestamp, a file sequence —
 * and comparing two of different kinds is meaningless.
 */
import { useState } from "react";

import { useApi } from "../api/hooks";
import type { Envelope, IngestBatch, IngestSummary, Watermark } from "../api/rows";
import { ChartEmpty, StackedColumns, seriesColour } from "../components/charts";
import { DataTable, PageFooter } from "../components/DataTable";
import { Badge, Card, ErrorState, Mono, PageHeader, Select, Spinner, type Tone } from "../components/ui";
import { bytes, count, digest, relative, timestamp } from "../lib/format";

const TENANTS = ["healthcare", "financial"] as const;

const STATUS_TONE: Record<string, Tone> = {
  ingested: "good",
  failed: "bad",
  rejected: "bad",
  seen: "neutral",
  promoted: "info",
};

export function Ingestion() {
  const [tenant, setTenant] = useState<(typeof TENANTS)[number] | "">("");
  const [offset, setOffset] = useState(0);
  const query = tenant ? { tenant } : {};

  const summary = useApi<{ items: IngestSummary[] }>("/ingestion/summary", { query });
  const batches = useApi<Envelope<IngestBatch>>("/ingestion/batches", {
    query: { limit: 25, offset, ...query },
  });
  const watermarks = useApi<{ items: Watermark[] }>("/ingestion/watermarks", { query });

  // accepted = batches - redelivered. The registry counts a redelivery against the
  // batch it matched, so the two columns already partition the total.
  const deliveries = [...(summary.data?.items ?? [])]
    .sort((a, b) => b.batches - a.batches)
    .slice(0, 8)
    .map((row) => ({
      label: `${row.dataset} · ${row.source}`,
      accepted: Math.max(0, row.batches - row.redelivered),
      redelivered: row.redelivered,
    }));

  return (
    <>
      <PageHeader
        title="Ingestion"
        subtitle="Content-addressed batches with exactly-once semantics, and the watermark each source has advanced to."
        actions={<Select label="Tenant" value={tenant} options={TENANTS} onChange={setTenant} />}
      />

      <Card
        title="Batches, and how many were redeliveries"
        hint="A redelivery is recognised and skipped — it is the idempotency working, not an error"
      >
        {summary.isLoading ? (
          <Spinner />
        ) : summary.error ? (
          <ErrorState error={summary.error} needs="operator" />
        ) : deliveries.length === 0 ? (
          <ChartEmpty>Nothing has been ingested.</ChartEmpty>
        ) : (
          <div className="p-3">
            {/* Stacked because the two numbers are parts of one total: every batch
                offered was either new or a repeat. A redelivery *rate* that climbs
                is a real upstream signal, and it is only visible as a proportion. */}
            <StackedColumns
              data={deliveries}
              series={[
                { key: "accepted", name: "New content", colour: seriesColour(0) },
                { key: "redelivered", name: "Redelivered, skipped", colour: seriesColour(3) },
              ]}
              height={240}
            />
          </div>
        )}
      </Card>

      <Card title="By source" hint="Ingested batches only" className="mt-4">
        {summary.isLoading ? (
          <Spinner />
        ) : summary.error ? (
          <ErrorState error={summary.error} needs="operator" />
        ) : (
          <DataTable
            rows={summary.data?.items ?? []}
            rowKey={(row) => `${row.tenant}/${row.dataset}/${row.source}`}
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
              { key: "source", header: "Source", render: (row) => <Badge>{row.source}</Badge> },
              { key: "batches", header: "Batches", numeric: true, render: (row) => count(row.batches) },
              {
                key: "redelivered",
                header: "Redelivered",
                numeric: true,
                render: (row) =>
                  row.redelivered > 0 ? (
                    <span className="text-good" title="Recognised by content digest and skipped — exactly-once working">
                      {count(row.redelivered)}
                    </span>
                  ) : (
                    count(0)
                  ),
              },
              { key: "rows", header: "Rows", numeric: true, render: (row) => count(row.rows_ingested) },
              { key: "bytes", header: "Bytes", numeric: true, render: (row) => bytes(row.bytes_ingested) },
              {
                key: "latest",
                header: "Latest",
                render: (row) => <span className="text-xs text-ink-400">{relative(row.latest_ingest)}</span>,
              },
            ]}
          />
        )}
      </Card>

      <Card title="Watermarks" hint="The kind discriminates: an LSN, a timestamp and a file sequence are not comparable" className="mt-4">
        {watermarks.isLoading ? (
          <Spinner />
        ) : watermarks.error ? (
          <ErrorState error={watermarks.error} needs="operator" />
        ) : (
          <DataTable
            rows={watermarks.data?.items ?? []}
            rowKey={(row) => `${row.source}/${row.tenant}/${row.dataset}`}
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
              { key: "source", header: "Source", render: (row) => <Badge>{row.source}</Badge> },
              { key: "kind", header: "Kind", render: (row) => <Badge tone="info">{row.watermark_kind}</Badge> },
              {
                key: "value",
                header: "Value",
                render: (row) => <Mono title={row.watermark_value}>{digest(row.watermark_value, 28)}</Mono>,
              },
              {
                key: "previous",
                header: "Previous",
                render: (row) => (
                  <Mono title={row.previous_value ?? undefined}>{digest(row.previous_value, 20)}</Mono>
                ),
              },
              { key: "updated", header: "Updated", render: (row) => <span className="text-xs text-ink-400">{timestamp(row.updated_at)}</span> },
            ]}
          />
        )}
      </Card>

      <Card title="Batches" className="mt-4">
        {batches.isLoading ? (
          <Spinner />
        ) : batches.error ? (
          <ErrorState error={batches.error} needs="operator" />
        ) : (
          <DataTable
            rows={batches.data?.items ?? []}
            rowKey={(row) => row.batch_digest}
            columns={[
              { key: "seen", header: "First seen", render: (row) => <span className="text-xs text-ink-300">{timestamp(row.first_seen_at)}</span> },
              {
                key: "digest",
                header: "Content digest",
                render: (row) => <Mono title={row.batch_digest}>{digest(row.batch_digest)}</Mono>,
              },
              {
                key: "dataset",
                header: "Dataset",
                render: (row) => (
                  <span>
                    {row.dataset}
                    <span className="ml-2 text-xs text-ink-500">{row.source}</span>
                  </span>
                ),
              },
              {
                key: "status",
                header: "Status",
                render: (row) => <Badge tone={STATUS_TONE[row.status] ?? "neutral"}>{row.status}</Badge>,
              },
              { key: "files", header: "Files", numeric: true, render: (row) => count(row.file_count) },
              { key: "rows", header: "Rows", numeric: true, render: (row) => count(row.row_count) },
              { key: "bytes", header: "Bytes", numeric: true, render: (row) => bytes(row.bytes) },
              {
                key: "seen_count",
                header: "Presented",
                numeric: true,
                render: (row) =>
                  row.seen_count > 1 ? (
                    <Badge tone="good" title="Ingested once; later presentations were skipped">
                      ×{row.seen_count}
                    </Badge>
                  ) : (
                    "×1"
                  ),
              },
            ]}
            footer={
              <PageFooter
                returned={batches.data?.page?.returned ?? 0}
                total={batches.data?.total}
                limit={25}
                offset={offset}
                onOffset={setOffset}
              />
            }
          />
        )}
      </Card>
    </>
  );
}
