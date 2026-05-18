/**
 * TypeScript declarations for @dchub/mcp-helper.
 */

export interface DCHubFetchOptions {
  /** Pre-existing API key to use; skips the 402 dance. */
  apiKey?: string;
  /** Log a one-line notice when a trial key is captured. Default false. */
  verbose?: boolean;
  /** Callback fired when a new trial key is captured. Use to persist. */
  onTrialKey?: (key: string) => void;
  /** Custom fetch implementation. Defaults to global fetch (Node 18+ / browser). */
  fetch?: typeof fetch;
}

export type DCHubFetch = (
  input: RequestInfo | URL,
  init?: RequestInit
) => Promise<Response>;

export function createDCHubFetch(opts?: DCHubFetchOptions): DCHubFetch;
export const dchubFetch: DCHubFetch;

declare const _default: {
  createDCHubFetch: typeof createDCHubFetch;
  dchubFetch: DCHubFetch;
};
export default _default;
