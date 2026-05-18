/**
 * @dchub/mcp-helper — CommonJS variant. See index.js for ESM source.
 *
 * Mirror of index.js so older Node + CJS-only setups can require() it:
 *
 *   const { createDCHubFetch } = require('@dchub/mcp-helper');
 *
 * Kept manually in sync rather than auto-built to keep zero deps.
 */

function createDCHubFetch(opts = {}) {
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
    const headers = new Headers(init.headers || {});
    if (apiKey && !headers.has('X-API-Key')) {
      headers.set('X-API-Key', apiKey);
    }
    const firstResp = await _fetch(input, Object.assign({}, init, { headers: headers }));
    if (firstResp.status !== 402) return firstResp;

    let trialKey = firstResp.headers.get('x-trial-key');
    if (!trialKey) {
      try {
        const cloned = firstResp.clone();
        const body = await cloned.json();
        trialKey = body && body.auto_trial_key;
      } catch (_) {}
    }
    if (!trialKey) return firstResp;

    apiKey = trialKey;
    if (verbose) {
      console.log('[@dchub/mcp-helper] captured trial key ' + trialKey.slice(0, 24) + '...');
    }
    if (onTrialKey) {
      try { onTrialKey(trialKey); } catch (_) {}
    }

    headers.set('X-API-Key', trialKey);
    return await _fetch(input, Object.assign({}, init, { headers: headers }));
  };
}

const dchubFetch = createDCHubFetch();

module.exports = { createDCHubFetch: createDCHubFetch, dchubFetch: dchubFetch };
module.exports.default = module.exports;
