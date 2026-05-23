// PATCH for dchub-mcp-server/server.mjs
// Apply by pasting this block right before `app.listen(PORT, '0.0.0.0', ...)`
// in your dchub-mcp-server repo's server.mjs.
// After commit + push, Railway redeploys; /mcp/manifest → 200.
// ────────────────────────────────────────────────────────────


// ── Phase ZZZZZ-round3 (2026-05-23): /mcp/manifest passthrough ─────────────
// The CF zone-level worker (4.8.5-mcp-landing) proxies ALL /mcp/* to this
// Express server, so /mcp/manifest was returning Express's default 404.
// Flask backend has the canonical manifest at the same path — proxy to it
// so AI agents discovering DC Hub via the manifest get a real response.
//
// Cached 5min in-process to keep Flask cold-call traffic minimal.
let _manifestCache = { body: null, status: 0, contentType: '', at: 0 };
const MANIFEST_TTL_MS = 5 * 60 * 1000;

app.get('/mcp/manifest', async (req, res) => {
  const now = Date.now();
  if (_manifestCache.body && (now - _manifestCache.at) < MANIFEST_TTL_MS) {
    res.status(_manifestCache.status);
    res.setHeader('Content-Type', _manifestCache.contentType);
    res.setHeader('X-DC-Hub-Source', 'mcp-server-cache');
    return res.send(_manifestCache.body);
  }
  try {
    const upstream = await fetch(`${API_BASE}/mcp/manifest`, {
      headers: {
        'Accept': 'application/json',
        'User-Agent': 'dchub-mcp-server/2.1.2 manifest-proxy',
      },
    });
    const body = await upstream.text();
    const contentType = upstream.headers.get('content-type') || 'application/json';
    _manifestCache = { body, status: upstream.status, contentType, at: now };
    res.status(upstream.status);
    res.setHeader('Content-Type', contentType);
    res.setHeader('X-DC-Hub-Source', 'mcp-server-proxy');
    res.send(body);
  } catch (err) {
    console.error('[MCP] /mcp/manifest proxy error:', err);
    res.status(502).json({
      error: 'manifest proxy failed',
      detail: err.message,
      hint: 'Try ' + API_BASE + '/mcp/manifest directly.',
    });
  }
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`DC Hub MCP Server v2.1.2 on port ${PORT}`);
