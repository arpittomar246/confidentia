/// <reference types="vite/client" />

/**
 * The one environment variable this console reads.
 *
 * Declared rather than left to `vite/client`'s permissive index signature so that a
 * typo in a variable name is a compile error instead of `undefined` at runtime --
 * which, for an API base URL, means every request silently going to the dev proxy.
 */
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
