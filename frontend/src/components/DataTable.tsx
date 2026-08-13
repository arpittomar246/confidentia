/**
 * A dense table for platform rows.
 *
 * Deliberately not a data grid. Nothing here sorts client-side: every list comes
 * from the API already ordered by the column that makes sense for it (newest run,
 * worst failure, largest quarantine reason), and a sortable header over a *paged*
 * response sorts fifty rows out of thirty thousand while looking like it sorted
 * everything. That is a worse lie than no sorting at all.
 */
import type { ReactNode } from "react";

import { Empty } from "./ui";

export interface Column<T> {
  key: string;
  header: ReactNode;
  render: (row: T) => ReactNode;
  /** Right-aligned for quantities, so magnitudes line up down the column. */
  numeric?: boolean;
  width?: string;
}

export function DataTable<T>({
  rows,
  columns,
  rowKey,
  empty = "Nothing recorded yet.",
  footer,
}: {
  rows: T[];
  columns: Column<T>[];
  rowKey: (row: T, index: number) => string;
  empty?: ReactNode;
  footer?: ReactNode;
}) {
  if (rows.length === 0) return <Empty>{empty}</Empty>;

  return (
    <div className="-mx-4 -mb-4 overflow-x-auto">
      <table className="w-full min-w-max text-left text-sm">
        <thead>
          <tr className="border-b border-ink-700 text-xs tracking-wide text-ink-400 uppercase">
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                style={column.width ? { width: column.width } : undefined}
                className={`px-4 py-2 font-medium whitespace-nowrap ${column.numeric ? "text-right" : ""}`}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-ink-800">
          {rows.map((row, index) => (
            <tr key={rowKey(row, index)} className="transition-colors hover:bg-ink-850">
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={`px-4 py-2 align-top ${column.numeric ? "tnum text-right" : ""}`}
                >
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {footer ? (
        <div className="border-t border-ink-700 px-4 py-2 text-xs text-ink-400">{footer}</div>
      ) : null}
    </div>
  );
}

/**
 * The paging footer.
 *
 * Prints "showing N of M" whenever the API returned a total, because a table
 * showing 50 of 30,951 rows and a table showing all 50 look identical otherwise.
 */
export function PageFooter({
  returned,
  total,
  limit,
  offset,
  onOffset,
}: {
  returned: number;
  total?: number | undefined;
  limit: number;
  offset: number;
  onOffset: (offset: number) => void;
}) {
  const hasMore = total === undefined ? returned === limit : offset + returned < total;
  return (
    <div className="flex items-center justify-between gap-4">
      <span>
        showing {offset + 1}–{offset + returned}
        {total === undefined ? "" : ` of ${total.toLocaleString("en-GB")}`}
      </span>
      <span className="flex gap-2">
        <button
          type="button"
          disabled={offset === 0}
          onClick={() => onOffset(Math.max(0, offset - limit))}
          className="rounded border border-ink-600 px-2 py-0.5 disabled:opacity-40"
        >
          Previous
        </button>
        <button
          type="button"
          disabled={!hasMore}
          onClick={() => onOffset(offset + limit)}
          className="rounded border border-ink-600 px-2 py-0.5 disabled:opacity-40"
        >
          Next
        </button>
      </span>
    </div>
  );
}
