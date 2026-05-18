/**
 * @dchub/mcp-helper — fetch() middleware for DC Hub trial keys (ESM).
 *
 * DC Hub's REST + MCP gates auto-mint working trial keys on every gated
 * request and deliver them via standard HTTP headers (X-Trial-Key,
 * WWW-Authenticate, Link). This package provides a drop-in fetch
 * wrapper so AI agents (Cursor, Cline, Continue, custom JS agents)
 * can grab the key + retry without parsing JSON bodies.
 *
 * Usage:
 *
 *   import { createDCHubFetch } from '@dchub/mcp-helper';
 *
 *   const fetchDC = createDCHubFetch({ verbose: true });
 *   const r = await fetchDC('https://dchub.cloud/api/v1/transactions/export.csv');
 *   // → first call: 402 + X-Trial-Key header
 *   // → wrapper grabs key, retries with X-API-Key
 *   // → second call: 200 with the CSV
 *
 * Or just use the default singleton:
 *
 *   import { dchubFetch } from '@dchub/mcp-helper';
 *   const r = await dchubFetch('https://dchub.cloud/api/v1/dcpi/scores');
 *
 * Persist the key across processes:
 *
 *   import { writeFileSync, readFileSync, existsSync } from 'fs';
 *   const path = process.env.HOME + '/.dchub-trial-key';
 *   const fetchDC = createDCHubFetch({
 *     apiKey: existsSync(path) ? readFileSync(path, 'utf-8').trim() : undefined,
 *     onTrialKey: (k) => writeFileSync(path, k),
 *   });
 *
 * See https://dchub.cloud/.well-known/ai-agents.json
 */

export function createDCHubFetch(opts = {}) {
  let apiKey = opts.apiKey || null;
  const verbose = !!opts.verbose;
  const onTrialKey = opts.onTrialKey || null;
  const _fetch = opts.fetch || globalThis.fetch;
  if (!_fetch) {
    throw new Error(
      '@dchub/mcp-helper: no fetch implementation found. ' +
      'Node 18+ has fetch built in; older Node needs node-fetch passed via opts.fetch.'
    );
  }

  return async function dchubFetch(input, init = {}) {
    // Inject api key if we have one
    const headers = new Headers(init.headers || {});
    if (apiKey && !headers.has('X-API-Key')) {
      headers.set('X-API-Key', apiKey);
    }
    const firstResp = await _fetch(input, { ...init, headers });
    if (firstResp.status !== 402) return firstResp;

    // Try to extract trial key from response header (preferred)
    let trialKey = firstResp.headers.get('x-trial-key');
    if (!trialKey) {
      // Body fallback for MCP gatekeeper's JSON-RPC return
      try {
        const cloned = firstResp.clone();
        const body = await cloned.json();
        trialKey = body && body.auto_trial_key;
      } catch (_) { /* not JSON, give up */ }
    }
    if (!trialKey) return firstResp;  // no key offered → surface 402

    apiKey = trialKey;
    if (verbose) {
      console.log(`[@dchub/mcp-helper] captured trial key ${trialKey.slice(0, 24)}...`);
    }
    if (onTrialKey) {
      try { onTrialKey(trialKey); } catch (_) { /* user callback, ignore */ }
    }

    // Retry with the key
    headers.set('X-API-Key', trialKey);
    return await _fetch(input, { ...init, headers });
  };
}

// Default singleton — convenient for one-liners
export const dchubFetch = createDCHubFetch();

export default { createDCHubFetch, dchubFetch };
