/**
 * The application shell.
 *
 * The TEE banner is the reason this file has a strong opinion. Under the `sim`
 * backend every confidentiality claim the platform makes is *modelled*: the
 * measurement is a hash of the source tree rather than an SGX MRENCLAVE, and the
 * signing key is on the same disk as the data it signs. A console that renders
 * "attested" without that qualifier turns an honest platform into a dishonest
 * screenshot, so the label from `/meta` sits in the header on every page and is
 * styled as a warning when it says MODELLED.
 *
 * Navigation is filtered by the roles in the token, which is presentation only --
 * the API refuses regardless (see AuthContext). A filtered sidebar is nicer than
 * nine panels that all say 403, and it is not a security boundary.
 */
import { NavLink, Outlet } from "react-router-dom";

import { useMeta } from "../api/hooks";
import type { Meta } from "../api/rows";
import { useAuth, type Role } from "../auth/AuthContext";
import { Badge, Button } from "./ui";

interface Entry {
  to: string;
  label: string;
  needs: Role;
  blurb: string;
}

const NAV: Entry[] = [
  { to: "/", label: "Overview", needs: "viewer", blurb: "Platform state at a glance" },
  { to: "/runs", label: "Pipeline runs", needs: "viewer", blurb: "Every execution and its outcome" },
  { to: "/quality", label: "Data quality", needs: "viewer", blurb: "Six dimensions, rules, failures" },
  { to: "/evaluation", label: "Evaluation", needs: "viewer", blurb: "P/R/F1 against the answer key" },
  { to: "/gate", label: "Promotion gate", needs: "viewer", blurb: "What was blocked, and why" },
  { to: "/marts", label: "Gold marts", needs: "viewer", blurb: "Disclosure-controlled aggregates" },
  { to: "/ingestion", label: "Ingestion", needs: "operator", blurb: "Batches and watermarks" },
  { to: "/lineage", label: "Lineage", needs: "operator", blurb: "Signed, chained provenance" },
  { to: "/audit", label: "Audit", needs: "auditor", blurb: "Key release, policy, disclosure" },
];

export function Layout() {
  const { subject, roles, may, signOut, expiresAt } = useAuth();
  const { data: meta } = useMeta<Meta>();

  return (
    <div className="flex min-h-full">
      <aside className="flex w-60 shrink-0 flex-col border-r border-ink-800 bg-ink-900">
        <div className="border-b border-ink-800 px-4 py-4">
          <p className="text-sm font-semibold tracking-[0.18em] text-ink-100">CONFIDENTIA</p>
          <p className="mt-0.5 text-[11px] text-ink-500">Confidential lakehouse console</p>
        </div>

        <nav className="flex-1 space-y-0.5 overflow-y-auto p-2">
          {NAV.map((entry) => {
            const allowed = may(entry.needs);
            return (
              <NavLink
                key={entry.to}
                to={entry.to}
                end={entry.to === "/"}
                title={allowed ? entry.blurb : `Requires the ${entry.needs} role`}
                className={({ isActive }) =>
                  [
                    "block rounded px-3 py-2 text-sm transition",
                    isActive
                      ? "bg-accent/10 font-medium text-accent"
                      : "text-ink-300 hover:bg-ink-850 hover:text-ink-100",
                    allowed ? "" : "opacity-45",
                  ].join(" ")
                }
              >
                <span className="flex items-center justify-between gap-2">
                  {entry.label}
                  {allowed ? null : (
                    <span className="text-[10px] tracking-wide text-ink-500 uppercase">
                      {entry.needs}
                    </span>
                  )}
                </span>
              </NavLink>
            );
          })}
        </nav>

        <div className="space-y-2 border-t border-ink-800 px-4 py-3 text-xs">
          <div>
            <p className="text-ink-300">{subject ?? "anonymous"}</p>
            <p className="mt-1 flex flex-wrap gap-1">
              {roles.map((role) => (
                <Badge key={role} tone="info">
                  {role}
                </Badge>
              ))}
            </p>
          </div>
          {expiresAt ? (
            <p className="text-ink-500">
              token expires {new Date(expiresAt).toLocaleTimeString("en-GB")}
            </p>
          ) : null}
          <Button onClick={signOut}>Sign out</Button>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <TeeBanner meta={meta} />
        <main className="flex-1 overflow-y-auto px-6 py-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

/**
 * The honesty bar.
 *
 * `hardware_rooted` comes from the same setting the pipeline uses, so the label
 * here and the label in a lineage entry cannot disagree.
 */
export function TeeBanner({ meta }: { meta: Meta | undefined }) {
  if (!meta) return null;
  const modelled = meta.tee.label === "MODELLED";
  return (
    <div
      className={`flex flex-wrap items-center gap-x-4 gap-y-1 border-b px-6 py-2 text-xs ${
        modelled ? "border-warn/25 bg-warn/8 text-warn" : "border-good/25 bg-good/8 text-good"
      }`}
    >
      <span className="font-semibold tracking-wide uppercase">{meta.tee.label}</span>
      <span className="text-ink-300">
        {modelled ? (
          <>
            TEE backend <code className="font-mono">{meta.tee.backend}</code> — enclave behaviour is
            simulated and the code measurement is a hash of the source tree, not a hardware
            MRENCLAVE. Nothing on this console is hardware-attested.
          </>
        ) : (
          <>
            TEE backend <code className="font-mono">{meta.tee.backend}</code> — measurements are
            hardware-rooted.
          </>
        )}
      </span>
      <span className="ml-auto flex items-center gap-3 text-ink-400">
        <span>env {meta.environment}</span>
        <span>auth {meta.auth_mode}</span>
        {meta.read_only ? <Badge tone="neutral">read-only</Badge> : null}
      </span>
    </div>
  );
}
