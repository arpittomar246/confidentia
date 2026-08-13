/**
 * Sign-in: paste a bearer token.
 *
 * There is no login form because there are no credentials to submit. The platform
 * has no user table and no password storage, by decision (ADR 0016) -- a
 * half-built credential store would be the weakest thing in a project whose point
 * is careful cryptography. In `local` auth mode the token comes from the CLI; in
 * `oidc` mode it comes from the identity provider.
 *
 * `/meta` is fetched before sign-in, on purpose. The TEE label and the environment
 * are what the console is connected to, and requiring a token to see them would
 * mean trusting it before it can tell you what it is.
 */
import { useState, type FormEvent } from "react";

import { useMeta } from "../api/hooks";
import type { Meta } from "../api/rows";
import { useAuth } from "../auth/AuthContext";
import { TeeBanner } from "./Layout";
import { Button, Callout } from "./ui";

export function SignIn() {
  const { signIn } = useAuth();
  const { data: meta, isError } = useMeta<Meta>();
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);

  function submit(event: FormEvent) {
    event.preventDefault();
    const result = signIn(value);
    setError(result.ok ? null : result.error);
  }

  return (
    <div className="flex min-h-full flex-col">
      <TeeBanner meta={meta} />
      <div className="flex flex-1 items-center justify-center px-6 py-10">
        <div className="w-full max-w-lg">
          <p className="text-sm font-semibold tracking-[0.18em] text-ink-100">CONFIDENTIA</p>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight text-ink-100">
            Confidential lakehouse console
          </h1>
          <p className="mt-2 text-sm text-ink-400">
            Read-only. Quality, provenance, disclosure control and key release, served from the
            platform metadata store.
          </p>

          {isError ? (
            <div className="mt-6">
              <Callout tone="bad" title="The API is not answering">
                Start it with <code className="font-mono">confidentia api serve</code>, then reload.
              </Callout>
            </div>
          ) : null}

          <form onSubmit={submit} className="mt-6 space-y-3">
            <label className="block text-xs font-medium tracking-wide text-ink-400 uppercase">
              Bearer token
            </label>
            <textarea
              value={value}
              onChange={(event) => setValue(event.target.value)}
              rows={4}
              spellCheck={false}
              autoComplete="off"
              placeholder="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9…"
              className="w-full resize-none rounded border border-ink-600 bg-ink-900 px-3 py-2 font-mono text-xs break-all text-ink-200 placeholder:text-ink-600"
            />
            {error ? <Callout tone="bad">{error}</Callout> : null}
            <div className="flex items-center justify-between gap-4">
              <Button type="submit" variant="primary" disabled={value.trim().length === 0}>
                Sign in
              </Button>
              <span className="text-xs text-ink-500">
                held in memory only — a refresh signs you out
              </span>
            </div>
          </form>

          {meta?.auth_mode === "local" ? (
            <div className="mt-8 rounded-lg border border-ink-700 bg-ink-900 p-4">
              <p className="text-xs font-medium tracking-wide text-ink-400 uppercase">
                Mint a token
              </p>
              <pre className="mt-2 overflow-x-auto font-mono text-xs text-ink-300">
{`confidentia api token me --role viewer
confidentia api token me --role operator
confidentia api token me --role auditor`}
              </pre>
              <p className="mt-2 text-xs text-ink-500">
                Roles form a ladder: viewer &lt; operator &lt; auditor. An auditor sees everything.
              </p>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
