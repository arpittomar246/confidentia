/**
 * Formatting.
 *
 * The rule running through this file: **a missing value never renders as a
 * number.** `null` from this API means one of several different things and none of
 * them is zero -- an unfinished run, a zero denominator, or a cell blanked by
 * k-anonymity suppression. Every helper here returns an em dash for null and the
 * components decide what to say about it.
 */

const NUMBER = new Intl.NumberFormat("en-GB");
const DATE_TIME = new Intl.DateTimeFormat("en-GB", {
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export const EM_DASH = "—";

export function count(value: number | null | undefined): string {
  return value === null || value === undefined ? EM_DASH : NUMBER.format(value);
}

/** Scores and rates in [0, 1] rendered to three places, because 0.997 vs 1.000 matters. */
export function ratio(value: number | null | undefined, places = 3): string {
  return value === null || value === undefined ? EM_DASH : value.toFixed(places);
}

export function percent(value: number | null | undefined, places = 1): string {
  return value === null || value === undefined ? EM_DASH : `${(value * 100).toFixed(places)}%`;
}

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return EM_DASH;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}

export function bytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return EM_DASH;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 100 || unit === 0 ? Math.round(size) : size.toFixed(1)} ${units[unit]}`;
}

export function timestamp(value: string | null | undefined): string {
  if (!value) return EM_DASH;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : DATE_TIME.format(parsed);
}

export function relative(value: string | null | undefined): string {
  if (!value) return EM_DASH;
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return value;
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86_400)}d ago`;
}

/**
 * Digests are shown truncated with the full value in the title attribute.
 *
 * Never truncated silently in a copyable context: a chain digest that has been
 * shortened and then pasted into a verification command fails in a way that looks
 * like tampering.
 */
export function digest(value: string | null | undefined, length = 12): string {
  if (!value) return EM_DASH;
  return value.length <= length ? value : `${value.slice(0, length)}…`;
}

export function titleCase(value: string): string {
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
