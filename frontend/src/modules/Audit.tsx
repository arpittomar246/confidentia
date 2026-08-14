/**
 * The audit trails: key release, attestation policy, disclosure control, restatement.
 *
 * **Refusals are the point.** An audit log that only shows grants cannot answer the
 * question it exists for, so refused key requests are surfaced first and the filter
 * defaults to showing both. The four refusal paths — unknown measurement, revoked
 * policy, tenant not permitted, stale or replayed evidence — are what make
 * "fail closed" a demonstrated property rather than a claim.
 *
 * `key_ids` are identifiers of the form `tenant/sensitivity/version`. No key
 * material is stored in this table and none reaches this console; the API is not
 * in the attested boundary and never calls the broker.
 */
import { useState } from "react";

import { useApi } from "../api/hooks";
import type {
  AttestationPolicy,
  DisclosureRecord,
  Envelope,
  KeyRelease,
  KeyReleaseSummary,
  Restatement,
} from "../api/rows";
import { RankedBars, STATUS } from "../components/charts";
import { DataTable, PageFooter } from "../components/DataTable";
import { Badge, Callout, Card, ErrorState, Grid, Mono, PageHeader, Select, Spinner, Stat } from "../components/ui";
import { count, digest, percent, timestamp } from "../lib/format";

const GRANTED = ["granted", "refused"] as const;

export function Audit() {
  const [filter, setFilter] = useState<(typeof GRANTED)[number] | "">("");
  const [offset, setOffset] = useState(0);

  const grantedQuery = filter === "" ? {} : { granted: filter === "granted" };

  const summary = useApi<{ items: KeyReleaseSummary[] }>("/audit/key-releases/summary");
  const releases = useApi<Envelope<KeyRelease>>("/audit/key-releases", {
    query: { limit: 25, offset, ...grantedQuery },
  });
  const policy = useApi<{ items: AttestationPolicy[] }>("/audit/attestation-policy");
  const disclosure = useApi<Envelope<DisclosureRecord>>("/audit/disclosure", { query: { limit: 25 } });
  const restatements = useApi<Envelope<Restatement>>("/audit/restatements", { query: { limit: 15 } });

  const totals = (summary.data?.items ?? []).reduce(
    (acc, row) => {
      acc.total += row.requests;
      if (!row.granted) acc.refused += row.requests;
      return acc;
    },
    { total: 0, refused: 0 },
  );

  // Refusals summed per reason across tenants. The summary is per (tenant, granted,
  // reason), so a reason that fired for both tenants appears twice; grouping is a
  // sum of returned counts, not a recomputation of anything.
  const refusalsByReason = Object.entries(
    (summary.data?.items ?? [])
      .filter((row) => !row.granted)
      .reduce<Record<string, number>>((acc, row) => {
        const reason = row.reason ?? "unrecorded";
        acc[reason] = (acc[reason] ?? 0) + row.requests;
        return acc;
      }, {}),
  )
    .map(([label, value]) => ({ label, value }))
    .sort((a, b) => b.value - a.value);

  return (
    <>
      <PageHeader
        title="Audit"
        subtitle="Key release and its refusals, the attestation policy that decides them, and what disclosure control removed before publishing."
      />

      <Grid cols={3}>
        <Stat label="Key requests" value={count(totals.total)} detail="grants and refusals" />
        <Stat
          label="Refused"
          value={count(totals.refused)}
          tone={totals.refused > 0 ? "info" : "neutral"}
          detail={totals.total > 0 ? percent(totals.refused / totals.total) : "—"}
        />
        <Stat
          label="Policy entries"
          value={count(policy.data?.items.length ?? null)}
          detail="approved code measurements"
        />
      </Grid>

      <div className="mt-4">
        <Callout tone="info" title="A refusal is the mechanism working">
          The broker releases nothing unless an attestation verifies against an approved, unrevoked
          measurement for that tenant. A refused request means the pipeline stopped rather than
          falling back to plaintext.
        </Callout>
      </div>

      <Card
        title="Why the broker refused"
        hint="Every refusal, by the reason recorded at the time"
        className="mt-4"
      >
        {summary.isLoading ? (
          <Spinner />
        ) : summary.error ? (
          <ErrorState error={summary.error} needs="auditor" />
        ) : refusalsByReason.length === 0 ? (
          <Callout tone="warn">
            No refusal has been recorded. A gate that has never refused anything is
            indistinguishable from no gate, so this panel being empty is not good news.
          </Callout>
        ) : (
          <div className="p-3">
            {/* The refusals are the point of C2, and a table sorts them by tenant
                rather than by how often each path fired. Which reason dominates is
                the operational question — an allowlist that only ever rejects
                unknown measurements is behaving differently from one rejecting
                stale evidence. */}
            <RankedBars
              data={refusalsByReason}
              colour={STATUS.bad}
              height={Math.max(140, refusalsByReason.length * 34)}
            />
          </div>
        )}
      </Card>

      <Card title="Requests by outcome" className="mt-4">
        {summary.isLoading ? (
          <Spinner />
        ) : summary.error ? (
          <ErrorState error={summary.error} needs="auditor" />
        ) : (
          <DataTable
            rows={summary.data?.items ?? []}
            rowKey={(row, index) => `${row.tenant}-${row.reason ?? "none"}-${index}`}
            columns={[
              { key: "tenant", header: "Tenant", render: (row) => row.tenant },
              {
                key: "outcome",
                header: "Outcome",
                render: (row) =>
                  row.granted ? <Badge tone="good">granted</Badge> : <Badge tone="bad">refused</Badge>,
              },
              { key: "reason", header: "Reason", render: (row) => <span className="text-xs text-ink-400">{row.reason ?? "—"}</span> },
              {
                key: "backend",
                header: "Backend",
                render: (row) => (
                  <Badge tone={row.hardware_rooted ? "good" : "warn"}>
                    {row.tee_backend}
                    {row.hardware_rooted ? "" : " (simulated)"}
                  </Badge>
                ),
              },
              { key: "requests", header: "Requests", numeric: true, render: (row) => count(row.requests) },
              { key: "latest", header: "Latest", render: (row) => <span className="text-xs text-ink-400">{timestamp(row.latest)}</span> },
            ]}
          />
        )}
      </Card>

      <Card
        title="Key release audit"
        actions={
          <Select
            label="Outcome"
            value={filter}
            options={GRANTED}
            onChange={(value) => {
              setFilter(value);
              setOffset(0);
            }}
          />
        }
        className="mt-4"
      >
        {releases.isLoading ? (
          <Spinner />
        ) : releases.error ? (
          <ErrorState error={releases.error} needs="auditor" />
        ) : (
          <DataTable
            rows={releases.data?.items ?? []}
            rowKey={(row) => String(row.audit_id)}
            columns={[
              { key: "when", header: "Requested", render: (row) => <span className="text-xs text-ink-300">{timestamp(row.requested_at)}</span> },
              { key: "tenant", header: "Tenant", render: (row) => row.tenant },
              {
                key: "measurement",
                header: "Measurement",
                render: (row) => <Mono title={row.measurement}>{digest(row.measurement, 14)}</Mono>,
              },
              {
                key: "outcome",
                header: "Outcome",
                render: (row) =>
                  row.granted ? <Badge tone="good">granted</Badge> : <Badge tone="bad">refused</Badge>,
              },
              { key: "reason", header: "Reason", render: (row) => <span className="text-xs text-ink-400">{row.reason ?? "—"}</span> },
              {
                key: "keys",
                header: "Key ids",
                render: (row) => (
                  <span className="flex flex-wrap gap-1">
                    {(row.key_ids ?? []).map((id) => (
                      <Mono key={id}>{id}</Mono>
                    ))}
                    {(row.key_ids ?? []).length === 0 ? <span className="text-ink-600">none</span> : null}
                  </span>
                ),
              },
            ]}
            footer={
              <PageFooter
                returned={releases.data?.page?.returned ?? 0}
                total={releases.data?.total}
                limit={25}
                offset={offset}
                onOffset={setOffset}
              />
            }
          />
        )}
      </Card>

      <Card
        title="Attestation policy"
        hint="Which code measurements may hold which tenants' keys. Revoking one stops the pipeline."
        className="mt-4"
      >
        {policy.isLoading ? (
          <Spinner />
        ) : policy.error ? (
          <ErrorState error={policy.error} needs="auditor" />
        ) : (
          <DataTable
            rows={policy.data?.items ?? []}
            rowKey={(row) => row.measurement}
            columns={[
              {
                key: "measurement",
                header: "Measurement",
                render: (row) => <Mono title={row.measurement}>{digest(row.measurement, 16)}</Mono>,
              },
              { key: "description", header: "Description", render: (row) => <span className="text-xs text-ink-400">{row.description ?? "—"}</span> },
              {
                key: "tenants",
                header: "Tenants",
                render: (row) => (
                  <span className="flex flex-wrap gap-1">
                    {(row.tenants ?? []).map((tenant) => (
                      <Badge key={tenant}>{tenant}</Badge>
                    ))}
                  </span>
                ),
              },
              {
                key: "rooted",
                header: "Rooted in",
                render: (row) =>
                  row.hardware_rooted ? (
                    <Badge tone="good">hardware</Badge>
                  ) : (
                    <Badge tone="warn" title="A hash of the source tree, not an SGX MRENCLAVE">
                      source hash
                    </Badge>
                  ),
              },
              {
                key: "state",
                header: "State",
                render: (row) =>
                  row.revoked_at ? (
                    <Badge tone="bad" title={timestamp(row.revoked_at)}>
                      revoked
                    </Badge>
                  ) : (
                    <Badge tone="good">active</Badge>
                  ),
              },
              { key: "approved", header: "Approved", render: (row) => <span className="text-xs text-ink-400">{timestamp(row.approved_at)}</span> },
            ]}
          />
        )}
      </Card>

      <Card
        title="Disclosure control"
        hint="What each publish suppressed. Complementary suppression is what stops a small cell being recovered by subtraction."
        className="mt-4"
      >
        {disclosure.isLoading ? (
          <Spinner />
        ) : disclosure.error ? (
          <ErrorState error={disclosure.error} needs="operator" />
        ) : (
          <DataTable
            rows={disclosure.data?.items ?? []}
            rowKey={(row) => String(row.disclosure_id)}
            columns={[
              { key: "when", header: "Published", render: (row) => <span className="text-xs text-ink-300">{timestamp(row.published_at)}</span> },
              { key: "mart", header: "Mart", render: (row) => row.mart ?? "—" },
              { key: "k", header: "Min cell", numeric: true, render: (row) => count(row.min_cell_count) },
              { key: "cells", header: "Cells", numeric: true, render: (row) => count(row.total_cells) },
              { key: "primary", header: "Primary", numeric: true, render: (row) => count(row.primary_suppressed) },
              {
                key: "complementary",
                header: "Complementary",
                numeric: true,
                render: (row) =>
                  row.complementary_suppressed === 0 && row.primary_suppressed > 0 ? (
                    <Badge tone="warn" title="Primary suppression alone leaves cells recoverable from a margin">
                      0
                    </Badge>
                  ) : (
                    count(row.complementary_suppressed)
                  ),
              },
              { key: "rows", header: "Rows published", numeric: true, render: (row) => count(row.rows_published) },
            ]}
          />
        )}
      </Card>

      <Card title="Restatements" hint="Marts recomputed after late-arriving data" className="mt-4">
        {restatements.isLoading ? (
          <Spinner />
        ) : restatements.error ? (
          <ErrorState error={restatements.error} needs="operator" />
        ) : (
          <DataTable
            rows={restatements.data?.items ?? []}
            rowKey={(row) => String(row.restatement_id)}
            empty="No mart has needed restating."
            columns={[
              { key: "when", header: "Restated", render: (row) => <span className="text-xs text-ink-300">{timestamp(row.restated_at)}</span> },
              { key: "mart", header: "Mart", render: (row) => row.mart },
              { key: "partitions", header: "Partitions", numeric: true, render: (row) => count(row.partitions_restated) },
              { key: "lag", header: "Lag (days)", numeric: true, render: (row) => count(row.lag_days) },
              { key: "reason", header: "Trigger", render: (row) => <span className="text-xs text-ink-400">{row.trigger_reason ?? "—"}</span> },
            ]}
          />
        )}
      </Card>
    </>
  );
}
