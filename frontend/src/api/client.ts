/**
 * Typed fetch over the committed OpenAPI schema.
 *
 * What the schema gives us and what it does not, stated plainly because it shapes
 * everything below. Paths, methods and **query parameters** are fully typed --
 * including enums, so `tenant: "helthcare"` is a compile error. Response bodies
 * are `unknown`.
 *
 * That is not an oversight in the generator. The API's handlers return rows
 * straight from SQL, and adding Pydantic `response_model`s would type them at the
 * cost of a worse failure: `response_model` *filters* the payload, so a column
 * added to a query but not to the model disappears from the response silently.
 * Untyped-and-complete beats typed-and-quietly-truncated for a console whose job
 * is to show what the platform recorded.
 *
 * So row shapes are declared in `rows.ts` as what this client *expects*, and are
 * the one place in the frontend that can drift from the backend. Everything else
 * is checked by the compiler against a schema CI keeps current.
 */
import type { paths } from "./schema";

export type ApiPath = keyof paths;

type GetOperation<P extends ApiPath> = paths[P] extends { get: infer G } ? G : never;

export type QueryParams<P extends ApiPath> =
  GetOperation<P> extends { parameters: { query?: infer Q } } ? Q : never;

export type PathParams<P extends ApiPath> =
  GetOperation<P> extends { parameters: { path?: infer R } } ? R : never;

/** The error envelope every failure uses -- see `api/errors.py`. */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    request_id?: string;
    context?: Record<string, unknown>;
  };
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | undefined;

  constructor(status: number, body: ApiErrorBody | undefined, fallback: string) {
    super(body?.error?.message ?? fallback);
    this.name = "ApiError";
    this.status = status;
    this.code = body?.error?.code ?? "unknown";
    this.requestId = body?.error?.request_id;
  }

  /** A 403 is a normal answer for an under-privileged role, not a fault. */
  get isForbidden(): boolean {
    return this.status === 403;
  }

  get isUnauthenticated(): boolean {
    return this.status === 401;
  }
}

/**
 * Where the API lives.
 *
 * Defaults to the Vite dev proxy at `/api`, which keeps the token same-origin in
 * development. `VITE_API_BASE_URL` overrides it -- that is what the compose
 * frontend service sets.
 */
export const API_BASE: string = import.meta.env.VITE_API_BASE_URL ?? "/api";

function buildUrl(path: string, query?: Record<string, unknown>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    // `undefined` and `null` mean "no filter"; sending them as the strings
    // "undefined"/"null" would filter on a tenant by that name and return nothing.
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const qs = search.toString();
  return `${API_BASE}${path}${qs ? `?${qs}` : ""}`;
}

function interpolate(path: string, params?: Record<string, string | number>): string {
  return path.replace(/\{([^}]+)\}/g, (_match, key: string) => {
    const value = params?.[key];
    if (value === undefined) {
      throw new Error(`missing path parameter ${key} for ${path}`);
    }
    return encodeURIComponent(String(value));
  });
}

export interface RequestOptions<P extends ApiPath> {
  query?: QueryParams<P>;
  params?: PathParams<P>;
  token?: string | null;
  signal?: AbortSignal;
}

export async function apiGet<T, P extends ApiPath = ApiPath>(
  path: P,
  options: RequestOptions<P> = {},
): Promise<T> {
  const url = buildUrl(
    interpolate(path as string, options.params as Record<string, string | number> | undefined),
    options.query as Record<string, unknown> | undefined,
  );

  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.token) headers.Authorization = `Bearer ${options.token}`;

  const response = await fetch(url, {
    headers,
    // The API authenticates by header and sets `allow_credentials: false`.
    // Sending cookies would be silently dropped by CORS and is not how this works.
    credentials: "omit",
    ...(options.signal ? { signal: options.signal } : {}),
  });

  if (!response.ok) {
    let body: ApiErrorBody | undefined;
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      body = undefined;
    }
    throw new ApiError(response.status, body, response.statusText);
  }

  return (await response.json()) as T;
}
