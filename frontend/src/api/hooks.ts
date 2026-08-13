/**
 * Query hooks. One place decides retry policy and how a 403 is treated.
 *
 * A 403 is a *normal answer* for an under-privileged role, so it is never retried
 * and never surfaced as an error state -- the module renders an explanation of
 * which role it needs. Retrying a 401 or 403 would hammer the API with a token
 * that will never work.
 */
import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { useAuth } from "../auth/AuthContext";
import { ApiError, apiGet, type ApiPath, type PathParams, type QueryParams } from "./client";

interface Options<P extends ApiPath> {
  query?: QueryParams<P>;
  params?: PathParams<P>;
  /** Milliseconds. Only the Overview module polls; everything else refreshes on demand. */
  refetchInterval?: number;
  enabled?: boolean;
}

export function useApi<T, P extends ApiPath = ApiPath>(
  path: P,
  options: Options<P> = {},
): UseQueryResult<T, ApiError> {
  const { token } = useAuth();
  return useQuery<T, ApiError>({
    // The token is part of the key: switching role must not serve the previous
    // role's cached payload, which would show an auditor's data to a viewer.
    queryKey: [path, options.params, options.query, token],
    queryFn: ({ signal }) =>
      apiGet<T, P>(path, {
        token,
        signal,
        ...(options.query ? { query: options.query } : {}),
        ...(options.params ? { params: options.params } : {}),
      }),
    enabled: options.enabled !== false && Boolean(token),
    ...(options.refetchInterval ? { refetchInterval: options.refetchInterval } : {}),
    retry: (attempt, error) => {
      if (error.isForbidden || error.isUnauthenticated) return false;
      return attempt < 2;
    },
    staleTime: 15_000,
  });
}

/**
 * `/meta` without a token.
 *
 * Separate from `useApi` because it must work before sign-in: the TEE label and
 * the environment are what the sign-in screen shows, and gating them behind a
 * token would mean the console cannot say what it is connected to until you have
 * already trusted it.
 */
export function useMeta<T>(): UseQueryResult<T, ApiError> {
  return useQuery<T, ApiError>({
    queryKey: ["/meta"],
    queryFn: ({ signal }) => apiGet<T>("/meta", { signal }),
    staleTime: 5 * 60_000,
    retry: 1,
  });
}
