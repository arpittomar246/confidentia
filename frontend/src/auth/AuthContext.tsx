/**
 * Who the console thinks you are.
 *
 * **The token lives in memory only.** Not `localStorage`, which any XSS can read
 * and exfiltrate, and not a cookie, because the API sets `allow_credentials: false`
 * and authenticates by header. The cost is that a page refresh logs you out; for a
 * console driven by a token minted with `confidentia api token`, that is a fair
 * trade and not worth engineering around.
 *
 * **The roles decoded here are for layout, not for access control.** This reads the
 * JWT payload without verifying the signature -- it cannot verify it, since in OIDC
 * mode the public key lives at the provider and in local mode the secret is on the
 * server. Every role decision that matters is made by the API, which returns 403.
 * The decoded roles decide which navigation entries are worth showing, and nothing
 * else. An attacker who edits their own token's payload changes their sidebar and
 * still gets 403 from every endpoint.
 */
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

export const ROLES = ["viewer", "operator", "auditor"] as const;
export type Role = (typeof ROLES)[number];

const RANK: Record<Role, number> = { viewer: 1, operator: 2, auditor: 3 };

interface TokenClaims {
  sub?: string;
  roles?: string[] | string;
  exp?: number;
  iss?: string;
}

function decodeClaims(token: string): TokenClaims | null {
  const parts = token.split(".");
  if (parts.length !== 3 || !parts[1]) return null;
  try {
    const base64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), "=");
    return JSON.parse(atob(padded)) as TokenClaims;
  } catch {
    return null;
  }
}

function parseRoles(claims: TokenClaims | null): Role[] {
  const raw = claims?.roles;
  const list = typeof raw === "string" ? raw.split(/\s+/) : (raw ?? []);
  return ROLES.filter((role) => list.includes(role));
}

interface AuthState {
  token: string | null;
  subject: string | null;
  roles: Role[];
  rank: number;
  expiresAt: number | null;
  /** True once the clock has passed `exp`. The API would refuse anyway. */
  expired: boolean;
  may: (required: Role) => boolean;
  signIn: (token: string) => { ok: true } | { ok: false; error: string };
  signOut: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  // Re-rendered on a timer would be tidier; a console session is short enough that
  // reading the clock on render is honest and costs nothing.
  const claims = useMemo(() => (token ? decodeClaims(token) : null), [token]);
  const roles = useMemo(() => parseRoles(claims), [claims]);

  const signIn = useCallback((candidate: string) => {
    const trimmed = candidate.trim();
    const parsed = decodeClaims(trimmed);
    if (!parsed) {
      return { ok: false as const, error: "That does not look like a JWT (expected three dot-separated parts)." };
    }
    if (parseRoles(parsed).length === 0) {
      // Not fatal server-side -- but a token with no role this platform knows about
      // gets 403 from every guarded route, and saying so now beats nine empty panels.
      return {
        ok: false as const,
        error: "The token carries no role this console recognises (viewer, operator or auditor).",
      };
    }
    setToken(trimmed);
    return { ok: true as const };
  }, []);

  const value = useMemo<AuthState>(() => {
    const rank = roles.reduce((highest, role) => Math.max(highest, RANK[role]), 0);
    const expiresAt = claims?.exp ? claims.exp * 1000 : null;
    return {
      token,
      subject: claims?.sub ?? null,
      roles,
      rank,
      expiresAt,
      expired: expiresAt !== null && expiresAt < Date.now(),
      may: (required: Role) => rank >= RANK[required],
      signIn,
      signOut: () => setToken(null),
    };
  }, [token, claims, roles, signIn]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside <AuthProvider>");
  return context;
}
