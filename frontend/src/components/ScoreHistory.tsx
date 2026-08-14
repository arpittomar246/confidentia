/**
 * Overall quality score over time, one line per dataset.
 *
 * The only chart in this console, and it exists because this is the one question
 * the tables genuinely cannot answer: a table shows the current score, and
 * "is this getting worse" needs the shape. Everything else here is a comparison
 * between rows, which a dense table does better than a graphic.
 *
 * The y-axis starts at the threshold rather than at zero. That is normally a way to
 * exaggerate a difference and is deliberate here: every score in this platform sits
 * between 0.8 and 1.0, so a zero-based axis renders six datasets as six identical
 * flat lines at the top of the frame. The threshold line is drawn and labelled so
 * the reader can see where the axis starts and why.
 */
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { Score } from "../api/rows";

/**
 * The categorical palette, not the status one.
 *
 * This list used to be `["#4f8cff", "#34d399", "#fbbf24", "#f87171", …]` — the
 * accent followed by *good*, *warn* and *bad*. Those three are reserved: they mean
 * a verified state, a state needing a human, and a refusal. Spending them on
 * "dataset 2" put a red line and a green line on a chart whose neighbouring table
 * used the same red and green to mean failed and passed, so the same colour meant
 * two things on one screen. The gate threshold below is drawn in `bad` because
 * there it *is* a status.
 */
const SERIES_COLOURS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9"];

export function ScoreHistory({ scores, threshold = 0.95 }: { scores: Score[]; threshold?: number }) {
  const datasets = [...new Set(scores.map((row) => row.dataset))].slice(0, SERIES_COLOURS.length);

  // One point per timestamp, with a column per dataset. Recharts wants rows, and
  // the API returns one row per (dataset, run) -- so this is a pivot, not a
  // calculation: no score is derived, combined or averaged here.
  const byTime = new Map<string, Record<string, number | string>>();
  for (const row of scores) {
    if (row.overall_score === null || !datasets.includes(row.dataset)) continue;
    const key = row.scored_at;
    const point = byTime.get(key) ?? { at: new Date(key).getTime() };
    point[row.dataset] = row.overall_score;
    byTime.set(key, point);
  }
  const data = [...byTime.values()].sort((a, b) => Number(a.at) - Number(b.at));

  if (data.length < 2) {
    return (
      <p className="py-8 text-center text-sm text-ink-500">
        Not enough history to plot — at least two scored runs are needed.
      </p>
    );
  }

  const lowest = Math.min(
    threshold,
    ...scores.filter((row) => row.overall_score !== null).map((row) => row.overall_score as number),
  );

  return (
    <div className="h-64 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
          {/* Solid hairline, not dashed: a dashed grid competes with the data for
              attention and the gate threshold is the only line here that earns a
              dash, because it is the one thing that is not measured data. */}
          <CartesianGrid stroke="#232936" />
          <XAxis
            dataKey="at"
            type="number"
            domain={["dataMin", "dataMax"]}
            scale="time"
            tick={{ fill: "#6b7688", fontSize: 11 }}
            tickFormatter={(value: number) =>
              new Date(value).toLocaleDateString("en-GB", { day: "2-digit", month: "short" })
            }
            stroke="#333b4d"
          />
          <YAxis
            domain={[Math.max(0, Math.floor(lowest * 20 - 1) / 20), 1]}
            tick={{ fill: "#6b7688", fontSize: 11 }}
            tickFormatter={(value: number) => value.toFixed(2)}
            stroke="#333b4d"
          />
          <ReferenceLine
            y={threshold}
            stroke="#f87171"
            strokeDasharray="4 4"
            label={{ value: `gate ${threshold}`, fill: "#f87171", fontSize: 11, position: "insideTopRight" }}
          />
          <Tooltip
            contentStyle={{
              background: "#12151c",
              border: "1px solid #333b4d",
              borderRadius: 6,
              fontSize: 12,
            }}
            // Recharts types both callbacks as taking `ReactNode`/`ValueType`,
            // because a tooltip can be attached to a categorical axis. This one is
            // a time axis carrying numbers, so the narrowing is done here rather
            // than asserted away.
            labelFormatter={(label) =>
              typeof label === "number" ? new Date(label).toLocaleString("en-GB") : String(label)
            }
            formatter={(value) => (typeof value === "number" ? value.toFixed(3) : String(value))}
          />
          <Legend wrapperStyle={{ fontSize: 11, color: "#98a2b3" }} />
          {datasets.map((dataset, index) => (
            <Line
              key={dataset}
              type="monotone"
              dataKey={dataset}
              stroke={SERIES_COLOURS[index % SERIES_COLOURS.length]}
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
              dot={false}
              // r >= 4, with a 2px ring in the surface colour so the marker stays
              // legible where two datasets cross.
              activeDot={{ r: 4, strokeWidth: 2, stroke: "#0d0f14" }}
              // Gaps rather than interpolation: a dataset that was not scored in a
              // run has no score, and joining across the gap would draw a value
              // that was never measured.
              connectNulls={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
