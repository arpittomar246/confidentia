/**
 * Verifiable provenance (C3).
 *
 * The verification panel is the one that has to be careful. `GET /lineage/verify`
 * runs the *shallow* check — chain links, ECDSA signatures, and whether the
 * denormalised columns still agree with the signed payload. It does **not** re-read
 * the encrypted lake and recompute Merkle roots; that needs a Spark session and a
 * key release, takes minutes, and is CLI-only by decision (ADR 0016).
 *
 * So a green tick here is rendered with what it did not check written next to it,
 * read from `scope` on the response rather than hard-coded. A badge saying
 * "lineage verified" with no scope is a claim the platform has not made.
 */
import { useState } from "react";

import { useApi } from "../api/hooks";
import type { Envelope, LineageEntry, LineageHead, LineageVerification } from "../api/rows";
import { ChartEmpty, TrendArea } from "../components/charts";
import { DataTable, PageFooter } from "../components/DataTable";
import { Badge, Button, Callout, Card, ErrorState, Grid, Mono, PageHeader, Spinner, Stat } from "../components/ui";
import { count, digest, relative, timestamp } from "../lib/format";

export function Lineage() {
  const [offset, setOffset] = useState(0);
  const [opened, setOpened] = useState<number | null>(null);

  const head = useApi<LineageHead>("/lineage/head");
  const verification = useApi<LineageVerification>("/lineage/verify");
  const latest = useApi<{ items: LineageEntry[] }>("/lineage/latest");
  const entries = useApi<Envelope<LineageEntry>>("/lineage/entries", { query: { limit: 25, offset } });

  // The API returns newest first, which is right for the table below and backwards
  // for a chart: a chain reads forwards.
  const committed = [...(entries.data?.items ?? [])]
    .filter((row) => row.record_count !== null)
    .sort((a, b) => a.seq - b.seq)
    .map((row) => ({ label: row.seq, value: row.record_count ?? 0 }));

  const scope = verification.data?.scope;

  return (
    <>
      <PageHeader
        title="Lineage"
        subtitle="Every recorded table is digested, bound to the code and keys in force, and signed into a hash chain. Each entry commits to the one before it."
      />

      <Grid cols={3}>
        <Stat label="Chain entries" value={count(head.data?.entries ?? null)} detail="each links to its predecessor" />
        <Stat
          label="Head digest"
          value={<span className="font-mono text-base">{digest(head.data?.head?.entry_digest, 14)}</span>}
          detail={`recorded ${relative(head.data?.head?.recorded_at)}`}
        />
        <Stat
          label="Shallow verification"
          value={verification.isLoading ? "…" : verification.data?.ok ? "intact" : "FAILED"}
          tone={verification.data?.ok ? "good" : verification.isLoading ? "neutral" : "bad"}
          detail={`${count(verification.data?.entries_checked ?? null)} entries re-derived`}
        />
      </Grid>

      <Card
        title="Verification"
        hint={verification.data?.summary}
        actions={<Button onClick={() => verification.refetch()}>Re-verify</Button>}
        className="mt-4"
      >
        {verification.isLoading ? (
          <Spinner label="Re-deriving links and signatures" />
        ) : verification.error ? (
          <ErrorState error={verification.error} needs="operator" />
        ) : (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2">
              <Check ok={scope?.chain_links} label="chain links" />
              <Check ok={scope?.signatures} label="signatures" />
              <Check ok={scope?.columns_match_payload} label="columns match payload" />
              <Check ok={scope?.lake_contents_rehashed} label="lake contents re-hashed" />
            </div>

            <Callout tone="warn" title="What this check does not establish">
              {scope?.note ??
                "Deep verification re-reads the encrypted lake and needs a Spark session; run `confidentia lineage verify --deep` for that."}{" "}
              A passing result here means the <em>record</em> of provenance is intact — not that the
              data still matches the roots it committed to.
            </Callout>

            {(verification.data?.failures.length ?? 0) > 0 ? (
              <Callout tone="bad" title="Failures">
                <ul className="space-y-1">
                  {verification.data?.failures.map((failure) => (
                    <li key={`${failure.seq}-${failure.reason}`} className="font-mono">
                      entry #{failure.seq}: {failure.reason} — {failure.detail}
                    </li>
                  ))}
                </ul>
              </Callout>
            ) : null}
          </div>
        )}
      </Card>

      <Card
        title="Records committed per entry"
        hint="Each entry's Merkle root commits to this many records. Newest 25, oldest first."
        className="mt-4"
      >
        {entries.isLoading ? (
          <Spinner />
        ) : entries.error ? (
          <ErrorState error={entries.error} needs="operator" />
        ) : committed.length < 2 ? (
          <ChartEmpty>Not enough entries to plot — at least two are needed.</ChartEmpty>
        ) : (
          <div className="p-3">
            {/* Plotted against sequence, not time: the chain's order *is* the
                sequence, and two entries recorded in the same second are still
                strictly ordered. A time axis would put them on top of each other
                and imply they were interchangeable, which is the one thing a
                hash-chain says they are not. */}
            <TrendArea
              data={committed}
              name="Records"
              labelFormatter={(seq) => `entry #${seq}`}
              height={220}
            />
          </div>
        )}
      </Card>

      <Card title="Latest entry per dataset" className="mt-4">
        {latest.isLoading ? (
          <Spinner />
        ) : latest.error ? (
          <ErrorState error={latest.error} needs="viewer" />
        ) : (
          <DataTable
            rows={latest.data?.items ?? []}
            rowKey={(row) => String(row.seq)}
            columns={[
              { key: "seq", header: "#", numeric: true, render: (row) => row.seq },
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
              { key: "transformation", header: "Transformation", render: (row) => <Mono>{row.transformation}</Mono> },
              { key: "records", header: "Records", numeric: true, render: (row) => count(row.record_count) },
              {
                key: "root",
                header: "Merkle root",
                render: (row) => <Mono title={row.merkle_root}>{digest(row.merkle_root)}</Mono>,
              },
              {
                key: "rooted",
                header: "Attestation",
                render: (row) =>
                  row.hardware_rooted ? (
                    <Badge tone="good">hardware</Badge>
                  ) : (
                    <Badge tone="warn" title="Self-measured and self-signed under the sim backend">
                      simulated
                    </Badge>
                  ),
              },
              { key: "when", header: "Recorded", render: (row) => <span className="text-xs text-ink-400">{timestamp(row.recorded_at)}</span> },
            ]}
          />
        )}
      </Card>

      <Card title="Chain" hint="Newest first. Payloads and signatures are auditor-only." className="mt-4">
        {entries.isLoading ? (
          <Spinner />
        ) : entries.error ? (
          <ErrorState error={entries.error} needs="operator" />
        ) : (
          <DataTable
            rows={entries.data?.items ?? []}
            rowKey={(row) => String(row.seq)}
            columns={[
              { key: "seq", header: "#", numeric: true, render: (row) => row.seq },
              { key: "digest", header: "Entry digest", render: (row) => <Mono title={row.entry_digest}>{digest(row.entry_digest)}</Mono> },
              { key: "previous", header: "Links to", render: (row) => <Mono title={row.previous_digest ?? undefined}>{digest(row.previous_digest)}</Mono> },
              { key: "dataset", header: "Dataset", render: (row) => `${row.tenant}/${row.dataset}` },
              { key: "measurement", header: "Code", render: (row) => <Mono title={row.code_measurement}>{digest(row.code_measurement, 10)}</Mono> },
              { key: "when", header: "Recorded", render: (row) => <span className="text-xs text-ink-400">{timestamp(row.recorded_at)}</span> },
              {
                key: "open",
                header: "",
                render: (row) => (
                  <button type="button" onClick={() => setOpened(row.seq)} className="text-xs text-accent hover:underline">
                    payload
                  </button>
                ),
              },
            ]}
            footer={
              <PageFooter
                returned={entries.data?.page?.returned ?? 0}
                total={entries.data?.total}
                limit={25}
                offset={offset}
                onOffset={setOffset}
              />
            }
          />
        )}
      </Card>

      {opened !== null ? <EntryDetail seq={opened} onClose={() => setOpened(null)} /> : null}
    </>
  );
}

function Check({ ok, label }: { ok: boolean | undefined; label: string }) {
  if (ok === undefined) return null;
  return (
    <Badge tone={ok ? "good" : "warn"} title={ok ? "checked" : "not checked over HTTP"}>
      {ok ? "✓" : "✗"} {label}
    </Badge>
  );
}

/**
 * The auditor view: payload, signature and public key.
 *
 * These three together let a third party recompute the digest and check the
 * signature without trusting this API — which is what makes the lineage claim
 * verifiable rather than merely asserted, so they are shown in full and not
 * truncated.
 */
function EntryDetail({ seq, onClose }: { seq: number; onClose: () => void }) {
  const entry = useApi<LineageEntry>("/lineage/entries/{seq}", { params: { seq } });

  return (
    <Card
      title={`Entry #${seq}`}
      hint="Signed payload, signature and public key — enough to verify this entry offline"
      actions={<Button onClick={onClose}>Close</Button>}
      className="mt-4"
    >
      {entry.isLoading ? (
        <Spinner />
      ) : entry.error ? (
        <ErrorState error={entry.error} needs="auditor" />
      ) : (
        <div className="space-y-3">
          <pre className="max-h-80 overflow-auto rounded border border-ink-700 bg-ink-950 p-3 font-mono text-xs text-ink-300">
            {JSON.stringify(entry.data?.payload ?? {}, null, 2)}
          </pre>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Signature (base64)" value={entry.data?.signature ?? "—"} />
            <Field label="Public key" value={entry.data?.public_key_pem ?? "—"} />
          </div>
        </div>
      )}
    </Card>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs tracking-wide text-ink-500 uppercase">{label}</p>
      <p className="mt-1 rounded border border-ink-700 bg-ink-950 p-2 font-mono text-[11px] break-all text-ink-400">
        {value}
      </p>
    </div>
  );
}
