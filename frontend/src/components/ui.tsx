/**
 * Shared presentation primitives.
 *
 * Two of these carry meaning rather than styling and are used deliberately
 * throughout: `Callout` attaches a caveat to the number it qualifies rather than
 * to a footnote, and `Suppressed` renders a blanked mart cell as *suppressed*
 * rather than as absent. Both exist because this console shows disclosure-
 * controlled data, and a UI that flattens those into "no data" undoes the control.
 */
import type { ReactNode } from "react";

import { ApiError } from "../api/client";
import type { Role } from "../auth/AuthContext";

// ---------------------------------------------------------------- containers

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight text-ink-100">{title}</h1>
        {subtitle ? <p className="mt-1 max-w-3xl text-sm text-ink-400">{subtitle}</p> : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </header>
  );
}

export function Card({
  title,
  hint,
  actions,
  children,
  className = "",
}: {
  title?: ReactNode;
  hint?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-lg border border-ink-700 bg-ink-900 shadow-sm shadow-black/20 ${className}`}
    >
      {title ? (
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-ink-700 px-4 py-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-ink-100">{title}</h2>
            {hint ? <p className="mt-0.5 text-xs text-ink-400">{hint}</p> : null}
          </div>
          {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
        </div>
      ) : null}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Grid({ children, cols = 4 }: { children: ReactNode; cols?: 2 | 3 | 4 }) {
  const map = { 2: "sm:grid-cols-2", 3: "sm:grid-cols-2 lg:grid-cols-3", 4: "sm:grid-cols-2 xl:grid-cols-4" };
  return <div className={`grid grid-cols-1 gap-4 ${map[cols]}`}>{children}</div>;
}

// -------------------------------------------------------------------- values

export function Stat({
  label,
  value,
  detail,
  tone = "neutral",
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  tone?: Tone;
}) {
  return (
    // The left rule is the only place a tone shows as a block of colour rather
    // than as text. A tile whose whole surface tinted on a bad value would make a
    // dashboard of four ordinary numbers look like an incident.
    <div className="relative overflow-hidden rounded-lg border border-ink-700 bg-ink-900 px-4 py-3 transition-colors hover:border-ink-600">
      <span className={`absolute inset-y-0 left-0 w-0.5 ${TONE_RULE[tone]}`} aria-hidden />
      <p className="text-xs font-medium tracking-wide text-ink-400 uppercase">{label}</p>
      <p className={`tnum mt-1.5 text-3xl font-semibold tracking-tight ${TONE_TEXT[tone]}`}>{value}</p>
      {detail ? <p className="mt-1 text-xs text-ink-400">{detail}</p> : null}
    </div>
  );
}

export type Tone = "neutral" | "good" | "warn" | "bad" | "info" | "suppressed";

const TONE_TEXT: Record<Tone, string> = {
  neutral: "text-ink-100",
  good: "text-good",
  warn: "text-warn",
  bad: "text-bad",
  info: "text-accent",
  suppressed: "text-suppressed",
};

const TONE_RULE: Record<Tone, string> = {
  neutral: "bg-ink-700",
  good: "bg-good",
  warn: "bg-warn",
  bad: "bg-bad",
  info: "bg-accent",
  suppressed: "bg-suppressed",
};

const TONE_CHIP: Record<Tone, string> = {
  neutral: "border-ink-600 bg-ink-800 text-ink-200",
  good: "border-good/30 bg-good/10 text-good",
  warn: "border-warn/30 bg-warn/10 text-warn",
  bad: "border-bad/30 bg-bad/10 text-bad",
  info: "border-accent/30 bg-accent/10 text-accent",
  suppressed: "border-suppressed/30 bg-suppressed/10 text-suppressed",
};

export function Badge({
  children,
  tone = "neutral",
  title,
}: {
  children: ReactNode;
  tone?: Tone;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-xs font-medium whitespace-nowrap ${TONE_CHIP[tone]}`}
    >
      {children}
    </span>
  );
}

/**
 * A blanked cell in a disclosure-controlled mart.
 *
 * Rendered as its own thing, in its own colour, with an explanation on hover.
 * Showing it as `0` or as an empty cell invites a reader to reconstruct it from a
 * published margin, which is precisely what complementary suppression prevents.
 */
export function Suppressed() {
  return (
    <span
      className="cursor-help text-suppressed"
      title="Suppressed by k-anonymity, not missing. Do not recover it by subtracting published cells from a total."
    >
      suppressed
    </span>
  );
}

/** A score bar. Threshold is drawn, because a score means nothing without it. */
export function ScoreBar({
  value,
  threshold,
  width = 96,
}: {
  value: number | null;
  threshold?: number | null;
  width?: number;
}) {
  if (value === null) return <span className="text-ink-500">—</span>;
  const tone = threshold != null && value < threshold ? "bg-bad" : value < 0.98 ? "bg-warn" : "bg-good";
  return (
    <span className="inline-flex items-center gap-2">
      <span
        className="relative inline-block h-1.5 overflow-hidden rounded-full bg-ink-700"
        style={{ width }}
      >
        <span
          className={`absolute inset-y-0 left-0 rounded-full ${tone}`}
          style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }}
        />
        {threshold != null ? (
          <span
            className="absolute inset-y-0 w-px bg-ink-200"
            style={{ left: `${Math.max(0, Math.min(1, threshold)) * 100}%` }}
            title={`threshold ${threshold.toFixed(3)}`}
          />
        ) : null}
      </span>
      <span className="tnum text-xs text-ink-300">{value.toFixed(3)}</span>
    </span>
  );
}

export function Mono({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span className="font-mono text-xs text-ink-300" title={title}>
      {children}
    </span>
  );
}

// -------------------------------------------------------------------- states

export function Callout({
  tone = "info",
  title,
  children,
}: {
  tone?: Tone;
  title?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className={`rounded-md border px-3 py-2 text-xs ${TONE_CHIP[tone]}`}>
      {title ? <p className="font-semibold">{title}</p> : null}
      <div className={title ? "mt-1 opacity-90" : "opacity-90"}>{children}</div>
    </div>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-8 text-sm text-ink-400" role="status">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-ink-600 border-t-accent" />
      {label}…
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-8 text-center text-sm text-ink-500">{children}</p>;
}

/**
 * The error state, which distinguishes three cases that look alike and are not.
 *
 * A 403 is what the platform is supposed to do to a viewer asking for the audit
 * trail; presenting it as a failure would teach people that the console is broken
 * when it is working. A 401 means the token expired. Anything else is a fault, and
 * only that case shows the request id — which is the thing that finds the log line.
 */
export function ErrorState({ error, needs }: { error: ApiError; needs?: Role }) {
  if (error.isForbidden) {
    return (
      <Callout tone="warn" title="Not available to your role">
        {needs ? (
          <>
            This view requires the <strong>{needs}</strong> role. Your token does not carry it, so
            the API refused the request — which is the access control working, not an error.
          </>
        ) : (
          error.message
        )}
      </Callout>
    );
  }
  if (error.isUnauthenticated) {
    return (
      <Callout tone="warn" title="Your session is no longer valid">
        The API rejected the token. Mint a new one with{" "}
        <code className="font-mono">confidentia api token me --role auditor</code> and sign in again.
      </Callout>
    );
  }
  return (
    <Callout tone="bad" title={`Request failed (${error.status} ${error.code})`}>
      {error.message}
      {error.requestId ? (
        <div className="mt-1 font-mono opacity-70">request id {error.requestId}</div>
      ) : null}
    </Callout>
  );
}

// ------------------------------------------------------------------ controls

export function Select<T extends string>({
  label,
  value,
  options,
  onChange,
  allLabel = "All",
}: {
  label: string;
  value: T | "";
  options: readonly T[];
  onChange: (value: T | "") => void;
  allLabel?: string;
}) {
  return (
    <label className="flex items-center gap-2 text-xs text-ink-400">
      {label}
      <select
        value={value}
        onChange={(event) => onChange(event.target.value as T | "")}
        className="rounded border border-ink-600 bg-ink-800 px-2 py-1 text-xs text-ink-200"
      >
        <option value="">{allLabel}</option>
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </label>
  );
}

export function Button({
  children,
  onClick,
  variant = "ghost",
  type = "button",
  disabled,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "ghost";
  type?: "button" | "submit";
  disabled?: boolean;
}) {
  const styles =
    variant === "primary"
      ? "bg-accent text-ink-950 hover:bg-accent/90 font-semibold"
      : "border border-ink-600 bg-ink-800 text-ink-200 hover:border-ink-500 hover:text-ink-100";
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`rounded px-3 py-1.5 text-xs transition disabled:cursor-not-allowed disabled:opacity-50 ${styles}`}
    >
      {children}
    </button>
  );
}
