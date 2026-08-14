/**
 * The Gold marts — the only place this console shows data rather than metadata.
 *
 * **A blank cell here is usually the disclosure control working.** k-anonymity
 * suppresses cells below the minimum count, and *complementary* suppression blanks
 * additional cells so the suppressed value cannot be recovered by subtracting the
 * published ones from a published total. A UI that renders those as `0`, or as an
 * empty cell that reads as "no data", invites exactly the arithmetic the control
 * exists to prevent — so they are rendered as `suppressed`, in their own colour,
 * with the reason on hover.
 *
 * There is no client-side sort. Mart schemas are inferred at publish time and
 * differ between marts, so the API cannot order them and neither can this module
 * without picking a column that may not exist. Pages are honest about being pages.
 */
import { useState } from "react";

import { useApi } from "../api/hooks";
import type { Envelope, MartListing, MartRow, MartSchema } from "../api/rows";
import { ChartEmpty, StackedColumns, STATUS } from "../components/charts";
import { DataTable, PageFooter } from "../components/DataTable";
import { Badge, Callout, Card, ErrorState, PageHeader, Spinner, Suppressed } from "../components/ui";
import { count, timestamp, titleCase } from "../lib/format";

const LIMIT = 50;

export function Marts() {
  const [selected, setSelected] = useState<string | null>(null);
  const listing = useApi<MartListing>("/marts");

  // Proportions, not counts.
  //
  // On absolute cells this chart was one usable bar and five hairlines:
  // `transactions_daily` has 11,454 cells and `fraud_by_hour` has 120, so a shared
  // axis rendered the small marts as a few pixels — and the case this chart exists
  // to show, `diagnosis_prevalence` with 86 primary suppressions and *zero*
  // complementary ones, was invisible.
  //
  // The question here is compositional — *what fraction of this mart did the
  // control remove* — so the y-axis is the fraction and every mart is comparable.
  // The absolute counts are in the table directly below, unchanged.
  const suppression = (listing.data?.items ?? [])
    .filter((row) => row.disclosure && row.disclosure.total_cells > 0)
    .map((row) => {
      const record = row.disclosure!;
      const total = record.total_cells;
      const published = Math.max(
        0,
        total - record.primary_suppressed - record.complementary_suppressed,
      );
      return {
        label: row.mart.replace(/^mart_/, ""),
        published: published / total,
        primary: record.primary_suppressed / total,
        complementary: record.complementary_suppressed / total,
      };
    })
    .sort((a, b) => b.primary + b.complementary - (a.primary + a.complementary));

  return (
    <>
      <PageHeader
        title="Gold marts"
        subtitle="Disclosure-controlled aggregates published into PostgreSQL. The lake itself is encrypted and cannot be read without a JVM and a key release."
      />

      <Callout tone="suppressed" title="Blank cells are suppression, not missing data">
        Cells below the minimum count are blanked, and complementary suppression blanks more so the
        first cannot be recovered by subtraction. Do not reconstruct them from a total.
      </Callout>

      <Card
        title="What disclosure control removed"
        hint="As a proportion of each mart's cells, so marts of very different sizes are comparable. Absolute counts are in the table below."
        className="mt-4"
      >
        {listing.isLoading ? (
          <Spinner />
        ) : listing.error ? (
          <ErrorState error={listing.error} needs="viewer" />
        ) : suppression.length === 0 ? (
          <ChartEmpty>No mart has been published with a disclosure record.</ChartEmpty>
        ) : (
          <div className="p-3">
            {/* The composition, not the totals. A mart with primary suppressions
                and *zero* complementary ones is the interesting case: the small
                cells are blanked but recoverable by subtracting the published ones
                from a published margin, so the control is running without
                protecting anything. That is visible here as a bar with no purple
                segment, and it is the reason this is a chart rather than a column
                in the table below. */}
            <StackedColumns
              data={suppression}
              series={[
                { key: "published", name: "Published", colour: STATUS.good },
                { key: "primary", name: "Primary suppressed", colour: STATUS.warn },
                { key: "complementary", name: "Complementary", colour: STATUS.suppressed },
              ]}
              format={(v) => `${(v * 100).toFixed(0)}%`}
              height={260}
            />
          </div>
        )}
      </Card>

      <Card title="Published marts" className="mt-4">
        {listing.isLoading ? (
          <Spinner />
        ) : listing.error ? (
          <ErrorState error={listing.error} needs="viewer" />
        ) : (
          <DataTable
            rows={listing.data?.items ?? []}
            rowKey={(row) => row.mart}
            empty="No mart has been published. Run `confidentia gold build`."
            columns={[
              {
                key: "mart",
                header: "Mart",
                render: (row) => (
                  <button
                    type="button"
                    onClick={() => setSelected(row.mart)}
                    className={`text-left hover:underline ${selected === row.mart ? "text-accent" : "text-ink-200"}`}
                  >
                    {titleCase(row.mart.replace(/^mart_/, ""))}
                  </button>
                ),
              },
              { key: "rows", header: "Rows", numeric: true, render: (row) => count(row.rows) },
              {
                key: "k",
                header: "Min cell",
                numeric: true,
                render: (row) => count(row.disclosure?.min_cell_count ?? null),
              },
              {
                key: "primary",
                header: "Primary suppressed",
                numeric: true,
                render: (row) => count(row.disclosure?.primary_suppressed ?? null),
              },
              {
                key: "complementary",
                header: "Complementary",
                numeric: true,
                render: (row) => {
                  const value = row.disclosure?.complementary_suppressed;
                  if (value === undefined || value === null) return "—";
                  // Primary suppression with no complementary suppression usually
                  // means the mart is not actually protected.
                  const risky = value === 0 && (row.disclosure?.primary_suppressed ?? 0) > 0;
                  return risky ? <Badge tone="warn">{count(value)}</Badge> : count(value);
                },
              },
              {
                key: "dp",
                header: "Noise",
                render: (row) =>
                  row.disclosure?.differential_privacy ? (
                    <Badge tone="info">ε {row.disclosure.epsilon ?? "?"}</Badge>
                  ) : (
                    <span className="text-xs text-ink-500">off</span>
                  ),
              },
              {
                key: "published",
                header: "Published",
                render: (row) => (
                  <span className="text-xs text-ink-400">{timestamp(row.disclosure?.published_at)}</span>
                ),
              },
            ]}
          />
        )}
      </Card>

      {selected ? <MartViewer mart={selected} /> : null}
    </>
  );
}

function MartViewer({ mart }: { mart: string }) {
  const [offset, setOffset] = useState(0);
  const schema = useApi<MartSchema>("/marts/{name}/schema", { params: { name: mart } });
  const rows = useApi<Envelope<MartRow>>("/marts/{name}", {
    params: { name: mart },
    query: { limit: LIMIT, offset },
  });

  const columns = (schema.data?.columns ?? []).map((column) => ({
    key: column.column_name,
    header: column.column_name,
    numeric: /int|numeric|double|real|bigint/.test(column.data_type),
    render: (row: MartRow) => (
      <Cell
        value={row[column.column_name] ?? null}
        // Gold writes `_suppressed` and `_suppression_reason` alongside the
        // measures, so whether a NULL is suppression is *recorded*, not inferred.
        // Inferring it labelled every unsuppressed row's empty reason "suppressed",
        // which is the same conflation of "blank" and "withheld" this module exists
        // to prevent -- in the opposite direction.
        suppressed={row._suppressed === true}
        metadata={column.column_name.startsWith("_")}
      />
    ),
  }));

  return (
    <Card
      title={titleCase(mart.replace(/^mart_/, ""))}
      hint="Unordered — mart schemas are inferred at publish time, so no column is guaranteed to exist to sort by"
      className="mt-4"
    >
      {schema.isLoading || rows.isLoading ? (
        <Spinner />
      ) : rows.error ? (
        <ErrorState error={rows.error} needs="viewer" />
      ) : (
        <DataTable
          rows={rows.data?.items ?? []}
          rowKey={(_row, index) => `${mart}-${offset + index}`}
          columns={columns}
          footer={
            <PageFooter
              returned={rows.data?.page?.returned ?? 0}
              total={rows.data?.total}
              limit={LIMIT}
              offset={offset}
              onOffset={setOffset}
            />
          }
        />
      )}
    </Card>
  );
}

function Cell({
  value,
  suppressed,
  metadata,
}: {
  value: string | number | boolean | null;
  suppressed: boolean;
  metadata: boolean;
}) {
  if (value === null) {
    // Three different reasons a cell can be empty, and they must not look alike.
    // On a suppressed row the measures were withheld. On any row, an empty
    // `_suppression_reason` simply does not apply. Anything else is genuinely
    // absent, and saying "suppressed" there would overstate the control.
    if (metadata) return <span className="text-ink-600">—</span>;
    return suppressed ? <Suppressed /> : <span className="text-ink-600">—</span>;
  }
  if (typeof value === "boolean") {
    return <Badge tone={value ? "suppressed" : "neutral"}>{String(value)}</Badge>;
  }
  if (typeof value === "number") return <span className="tnum">{count(value)}</span>;
  // Dates arrive as ISO strings; anything else is a dimension member.
  return /^\d{4}-\d{2}-\d{2}/.test(value) ? (
    <span className="text-ink-300">{value.slice(0, 10)}</span>
  ) : (
    <span>{value}</span>
  );
}
