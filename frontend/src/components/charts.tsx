/**
 * Chart primitives, so every chart in this console reads as one system.
 *
 * Recharts' defaults are not neutral — dashed gridlines, a white tooltip, a
 * cycling palette, thick bars filling their band. Left alone they make nine
 * modules look like nine products. Everything here is the same handful of
 * decisions applied once:
 *
 *   lines      2px, round cap and join
 *   markers    r >= 4 (8px), with a 2px surface ring so they stay legible crossing
 *   bars       capped at 24px, 4px rounded data-end, square at the baseline
 *   area fill  the series hue at ~10% -- a wash, never a block
 *   grid       hairline, solid, one step off the surface, recessive
 *   gaps       2px of surface between touching marks, never a stroke
 *
 * **Text never wears the series colour.** Values, labels and axis text use ink
 * tokens; identity comes from the coloured mark beside them. `--color-series-4`
 * is a yellow that is fine as a 2px line and illegible as 11px text.
 *
 * **Status colours are not available here.** good/warn/bad mean verified, needs a
 * human, and refused. A chart that spends them on datasets makes a green line and
 * a green badge mean different things on the same screen. Charts that genuinely
 * plot an outcome — grants against refusals — use them *as* status and say so.
 */
import type { ReactNode } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,

  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

// Read from the stylesheet rather than duplicated, so the validated palette has
// exactly one definition. Recharts needs concrete colours, not var() references.
const SERIES = [
  "#3987e5",
  "#d95926",
  "#199e70",
  "#c98500",
  "#d55181",
  "#9085e9",
] as const;

export const STATUS = {
  good: "#34d399",
  warn: "#fbbf24",
  bad: "#f87171",
  suppressed: "#a78bfa",
  neutral: "#4a5468",
} as const;

const SURFACE = "#0d0f14";
const GRID = "#232936";
const AXIS_TEXT = "#6b7688";

/** Colour for series *i*, by position and never cycled past the cap. */
export function seriesColour(index: number): string {
  return SERIES[index] ?? STATUS.neutral;
}

export const SERIES_CAP = SERIES.length;

const axisProps = {
  stroke: GRID,
  tick: { fill: AXIS_TEXT, fontSize: 11 },
  tickLine: false,
  axisLine: false,
} as const;

// ------------------------------------------------------------------ tooltip

/**
 * The hover layer, in the console's own card style.
 *
 * Present by default on every chart here: an HTML chart *is* interactive, and a
 * reader who cannot ask a mark what it is has to guess from the axis.
 */
function ChartTooltip({
  active,
  payload,
  label,
  formatter,
  labelFormatter,
}: {
  active?: boolean;
  payload?: Array<{ name?: string; value?: number | string; color?: string; payload?: Record<string, unknown> }>;
  label?: string | number;
  formatter?: (value: number | string, name: string, row: Record<string, unknown>) => ReactNode;
  labelFormatter?: (label: string | number) => ReactNode;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-md border border-ink-600 bg-ink-850/95 px-3 py-2 text-xs shadow-lg shadow-black/40 backdrop-blur">
      {label !== undefined ? (
        <div className="mb-1.5 font-medium text-ink-100">
          {labelFormatter ? labelFormatter(label) : label}
        </div>
      ) : null}
      <div className="space-y-1">
        {payload.map((entry, i) => (
          <div key={i} className="flex items-center gap-2 whitespace-nowrap">
            {/* Identity is the swatch. The text beside it stays in ink. */}
            <span
              className="size-2 shrink-0 rounded-[2px]"
              style={{ background: entry.color }}
              aria-hidden
            />
            <span className="text-ink-300">{entry.name}</span>
            <span className="tnum ml-auto pl-3 font-medium text-ink-100">
              {formatter && entry.value !== undefined
                ? formatter(entry.value, entry.name ?? "", entry.payload ?? {})
                : entry.value}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

const tooltipCursor = { fill: "#ffffff", fillOpacity: 0.04 };

/**
 * The legend, rendered outside the chart rather than by Recharts.
 *
 * Recharts orders its own legend by when each child registers, which is not the
 * order they paint — it listed "F1, Precision, Recall" above bars running
 * precision, recall, F1. Passing an explicit `payload` is documented to override
 * that and did not. Rather than keep guessing at a component that owns the answer,
 * the legend is just markup here: it renders in the order given, uses ink text
 * beside a coloured swatch, and is one less thing that can disagree with the
 * chart.
 *
 * Always present for two or more series; a single series has none, because the
 * card title already says what is plotted.
 */
function SeriesLegend({
  entries,
}: {
  entries: Array<{ key: string; name: string; colour?: string }>;
}) {
  if (entries.length < 2) return null;
  return (
    <div className="mt-1 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-[11px] text-ink-400">
      {entries.map((entry, i) => (
        <span key={entry.key} className="inline-flex items-center gap-1.5">
          <span
            className="size-2 rounded-full"
            style={{ background: entry.colour ?? seriesColour(i) }}
            aria-hidden
          />
          {entry.name}
        </span>
      ))}
    </div>
  );
}

// ------------------------------------------------------------------ frames

/**
 * `height` is a number, not `100%` inside a sized div.
 *
 * The obvious spelling — a wrapper with a fixed height and
 * `<ResponsiveContainer height="100%">` — measures the parent on an effect, and
 * until that lands the container renders itself `width: 0; height: 0; overflow:
 * visible` and paints the chart absolutely outside its own box. On screen it
 * settles and looks fine; anything that re-measures after mount does not. A
 * full-page screenshot resizes the viewport and re-runs exactly that measurement,
 * which is how this was found: five charts, every mark in the DOM with correct
 * coordinates and a visible fill, and five blank rectangles in the image.
 *
 * Giving the container a definite height means it never has to ask.
 */
export function ChartFrame({
  height = 220,
  children,
}: {
  height?: number;
  children: ReactNode;
}) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      {children as never}
    </ResponsiveContainer>
  );
}

export function ChartEmpty({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-[220px] items-center justify-center px-4 text-center text-sm text-ink-500">
      {children}
    </div>
  );
}

// ------------------------------------------------------------------ charts

export interface Datum {
  label: string;
  value: number;
  colour?: string;
}

/**
 * Horizontal bars: ranked magnitude against a category.
 *
 * Horizontal rather than vertical because the categories here are dataset names,
 * which do not fit under a vertical column without rotating the text — and rotated
 * axis labels are read slower than they are written.
 */
export function RankedBars({
  data,
  height = 220,
  colour = SERIES[0],
  format = (v: number) => v.toLocaleString(),
  domain,
  unit,
}: {
  data: Datum[];
  height?: number;
  colour?: string;
  format?: (value: number) => string;
  domain?: [number, number];
  unit?: string;
}) {
  if (!data.length) return <ChartEmpty>Nothing to plot yet.</ChartEmpty>;
  return (
    <ChartFrame height={height}>
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 16, bottom: 4, left: 4 }}>
        <CartesianGrid stroke={GRID} horizontal={false} />
        <XAxis type="number" domain={domain} {...axisProps} tickFormatter={format} />
        {/* Wide enough for "patient_change · file_drop" on one line. At 128 the
            longer labels wrapped to two lines and collided with their neighbours. */}
        <YAxis type="category" dataKey="label" width={168} {...axisProps} />
        <Tooltip
          cursor={tooltipCursor}
          content={<ChartTooltip formatter={(v) => `${format(Number(v))}${unit ?? ""}`} />}
        />
        {/* 4px rounded data-end, square at the baseline. */}
        <Bar dataKey="value" name="value" radius={[0, 4, 4, 0]} barSize={14} isAnimationActive={false}>
          {data.map((row, i) => (
            <Cell key={i} fill={row.colour ?? colour} />
          ))}
        </Bar>
      </BarChart>
    </ChartFrame>
  );
}

/**
 * A dot plot for values that cluster in a narrow band.
 *
 * Every quality score in this platform sits above 0.95, so eight bars on a 0–1
 * axis are eight identical full-width bars: the colour carries all the meaning and
 * the length carries none. Zooming a *bar* axis is not the fix — a bar encodes
 * magnitude by length from zero, and cutting the baseline makes a 2% difference
 * look like a 90% one.
 *
 * A dot encodes by **position**, so a zoomed axis is legitimate: the reader is
 * comparing where things sit on a scale, not how long they are. The connector is
 * a guide to the label, drawn recessive so it is not read as a bar. The threshold
 * is drawn because a zoomed axis must show what it is zoomed to.
 */
export function DotPlot({
  data,
  height = 240,
  threshold,
  format = (v: number) => v.toFixed(3),
}: {
  data: Datum[];
  height?: number;
  threshold?: number;
  format?: (value: number) => string;
}) {
  if (!data.length) return <ChartEmpty>Nothing to plot yet.</ChartEmpty>;

  const values = data.map((d) => d.value);
  const lowest = Math.min(...values, threshold ?? Infinity);
  const highest = Math.max(...values, threshold ?? -Infinity);
  // Pad the band so no dot sits on the frame. Rounded outward to a readable step
  // rather than to the data, so the axis labels stay clean numbers.
  const pad = Math.max((highest - lowest) * 0.25, 0.005);
  const min = Math.max(0, Math.floor((lowest - pad) * 100) / 100);
  const max = Math.min(1, Math.ceil((highest + pad) * 100) / 100);

  const rowHeight = Math.max(22, (height - 40) / data.length);

  return (
    <div style={{ height }} className="relative w-full overflow-hidden">
      {data.map((row) => {
        const fraction = (row.value - min) / (max - min || 1);
        return (
          <div key={row.label} className="flex items-center gap-2" style={{ height: rowHeight }}>
            <span className="w-32 shrink-0 truncate text-right text-[11px] text-ink-400" title={row.label}>
              {row.label}
            </span>
            <div className="relative h-full flex-1">
              {/* Guide, not a bar: one step off the surface and 1px. */}
              <div className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-ink-700" />
              {threshold !== undefined ? (
                <div
                  className="absolute inset-y-1 w-px bg-ink-500"
                  style={{ left: `${((threshold - min) / (max - min || 1)) * 100}%` }}
                  title={`threshold ${format(threshold)}`}
                />
              ) : null}
              <div
                className="absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-ink-900 transition-transform hover:scale-125"
                style={{ left: `${Math.min(100, Math.max(0, fraction * 100))}%`, background: row.colour ?? SERIES[0] }}
                title={`${row.label} — ${format(row.value)}`}
              />
            </div>
            <span className="tnum w-14 shrink-0 text-right text-[11px] text-ink-200">
              {format(row.value)}
            </span>
          </div>
        );
      })}
      <div className="mt-1 flex items-center gap-2 border-t border-ink-700 pt-1 text-[10px] text-ink-500">
        <span className="w-32 shrink-0" />
        <span className="flex-1">{format(min)}</span>
        <span>
          {threshold !== undefined ? `threshold ${format(threshold)} · ` : ""}
          {format(max)}
        </span>
        <span className="w-14 shrink-0" />
      </div>
    </div>
  );
}

/**
 * Grouped columns: the same measures compared across categories.
 *
 * `stroke={SURFACE}` with a 2px width is the surface gap — the separator is
 * negative space in the background colour, not a border drawn on the mark.
 */
export function GroupedColumns({
  data,
  series,
  height = 240,
  format = (v: number) => v.toFixed(3),
  domain = [0, 1],
}: {
  data: Array<Record<string, string | number>>;
  series: Array<{ key: string; name: string }>;
  height?: number;
  format?: (value: number) => string;
  domain?: [number, number];
}) {
  if (!data.length) return <ChartEmpty>Nothing to plot yet.</ChartEmpty>;
  return (
    <>
    <ChartFrame height={height}>
      {/* A negative left margin pulled the y-axis under its own labels: "1.000"
          rendered as "l.000". The axis gets its declared width and nothing eats
          into it. */}
      <BarChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: 0 }} barGap={2}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="label" {...axisProps} interval={0} />
        <YAxis domain={domain} {...axisProps} tickFormatter={format} width={52} />
        <Tooltip cursor={tooltipCursor} content={<ChartTooltip formatter={(v) => format(Number(v))} />} />
        {series.map((s, i) => (
          <Bar
            key={s.key}
            dataKey={s.key}
            name={s.name}
            fill={seriesColour(i)}
            radius={[4, 4, 0, 0]}
            maxBarSize={24}
            isAnimationActive={false}
          />
        ))}
      </BarChart>
    </ChartFrame>
    <SeriesLegend entries={series} />
    </>
  );
}

/**
 * Stacked columns for a composition that sums to a meaningful whole.
 *
 * Each segment carries a 2px surface-coloured stroke: the gap between touching
 * fills is the separator, one consistent width down the stack.
 */
export function StackedColumns({
  data,
  series,
  height = 240,
  format = (v: number) => v.toLocaleString(),
}: {
  data: Array<Record<string, string | number>>;
  series: Array<{ key: string; name: string; colour?: string }>;
  height?: number;
  format?: (value: number) => string;
}) {
  if (!data.length) return <ChartEmpty>Nothing to plot yet.</ChartEmpty>;
  return (
    <>
      <ChartFrame height={height}>
        <BarChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: 0 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey="label" {...axisProps} interval={0} />
          <YAxis {...axisProps} tickFormatter={format} width={56} />
          <Tooltip cursor={tooltipCursor} content={<ChartTooltip formatter={(v) => format(Number(v))} />} />
          {series.map((s, i) => (
            <Bar
              key={s.key}
              dataKey={s.key}
              name={s.name}
              stackId="a"
              fill={s.colour ?? seriesColour(i)}
              stroke={SURFACE}
              strokeWidth={2}
              maxBarSize={28}
              isAnimationActive={false}
            />
          ))}
        </BarChart>
      </ChartFrame>
      <SeriesLegend entries={series} />
    </>
  );
}

/**
 * A single measure over time, as a 2px line on a 10% wash.
 *
 * The wash is decoration in the strict sense — it carries no value the line does
 * not — and it earns its place only by making a single series legible at a glance
 * in a dense grid. It stays at 10% for that reason.
 */
export function TrendArea({
  data,
  height = 200,
  colour = SERIES[0],
  name,
  format = (v: number) => v.toLocaleString(),
  labelFormatter,
}: {
  data: Array<{ label: string | number; value: number }>;
  height?: number;
  colour?: string;
  name: string;
  format?: (value: number) => string;
  labelFormatter?: (label: string | number) => ReactNode;
}) {
  if (!data.length) return <ChartEmpty>Nothing to plot yet.</ChartEmpty>;
  const gradientId = `wash-${name.replace(/\W/g, "")}`;
  return (
    <ChartFrame height={height}>
      <AreaChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: -14 }}>
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={colour} stopOpacity={0.18} />
            <stop offset="100%" stopColor={colour} stopOpacity={0.01} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="label" {...axisProps} minTickGap={24} />
        <YAxis {...axisProps} tickFormatter={format} width={52} />
        <Tooltip
          cursor={{ stroke: GRID, strokeWidth: 1 }}
          content={<ChartTooltip formatter={(v) => format(Number(v))} labelFormatter={labelFormatter} />}
        />
        <Area
          type="monotone"
          dataKey="value"
          name={name}
          stroke={colour}
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
          fill={`url(#${gradientId})`}
          // r >= 4 is 8px; the 2px surface ring keeps it legible on the wash.
          activeDot={{ r: 4, strokeWidth: 2, stroke: SURFACE }}
          dot={false}
          isAnimationActive={false}
        />
      </AreaChart>
    </ChartFrame>
  );
}

/**
 * A donut for one part-to-whole split, with the total in the hole.
 *
 * Used once, for grants against refusals. A donut is a poor comparator and a good
 * proportion-at-a-glance, which is exactly the job here: the interesting fact is
 * that refusals are a small non-zero slice, not what 15 divided by 347 is.
 */
export function SplitDonut({
  data,
  height = 200,
  centreValue,
  centreLabel,
}: {
  data: Datum[];
  height?: number;
  centreValue: string;
  centreLabel: string;
}) {
  const total = data.reduce((sum, row) => sum + row.value, 0);
  if (!total) return <ChartEmpty>Nothing recorded yet.</ChartEmpty>;
  return (
    <div className="relative">
      <ChartFrame height={height}>
        <PieChart>
          <Tooltip
            content={
              <ChartTooltip
                formatter={(v) => `${Number(v).toLocaleString()} (${((Number(v) / total) * 100).toFixed(1)}%)`}
              />
            }
          />
          <Pie
            data={data}
            dataKey="value"
            nameKey="label"
            innerRadius="62%"
            outerRadius="88%"
            paddingAngle={2}
            stroke={SURFACE}
            strokeWidth={2}
            isAnimationActive={false}
          >
            {data.map((row, i) => (
              <Cell key={i} fill={row.colour ?? seriesColour(i)} />
            ))}
          </Pie>
        </PieChart>
      </ChartFrame>
      <div
        className="pointer-events-none absolute inset-x-0 flex flex-col items-center"
        style={{ top: height / 2 - 34 }}
      >
        <span className="tnum text-2xl font-semibold text-ink-100">{centreValue}</span>
        <span className="text-[11px] text-ink-400">{centreLabel}</span>
      </div>
      <SeriesLegend entries={data.map((row) => ({ key: row.label, name: row.label, colour: row.colour }))} />
    </div>
  );
}
