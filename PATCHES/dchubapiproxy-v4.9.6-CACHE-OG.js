/**
 * DC Hub API Proxy Worker v4.9.6 — KV stale-while-error + OG images
 * ================================================================================
 * v4.9.6 CHANGES (May 24 2026) — Phase ZZZZZ-round35:
 *   - ADD: KV stale-while-error for FLASK_HTML_PATHS. On Railway+Render
 *          failure, serve the last successful body from KV (24h window)
 *          instead of returning 503. Stamps X-DC-Route-Class=flask-html-kv-stale.
 *   - ADD: /static/og/ to FLASK_HTML_PATHS so the new routes/og_images.py
 *          Pillow renderer is reachable. Images get max-age=86400 +
 *          s-maxage=86400 + stale-while-revalidate=604800 (Pillow render
 *          essentially never runs in steady state — edge cache covers it).
 *   - ADD: /robots.txt and /robots-canonical.txt to FLASK_HTML_PATHS so
 *          routes/robots_seo.py serves the Sitemap: directive instead of
 *          falling through to CF Pages (which serves a bare User-agent file).
 *   - RESULT: OG image previews work on social shares for all 2,031 SEO
 *          landing pages; intermittent Railway rate-limits no longer
 *          surface as 503 to end users.
 *
 * v4.9.5 CHANGES (May 24 2026) — Phase ZZZZZ-round33:
 *   - FIX: api.dchub.cloud/<non-API-path> was hitting `fetch(request)`
 *          in the non-API fallthrough, which loops back into the same
 *          worker (api.dchub.cloud) → infinite recursion → CF 522 in
 *          <100ms. Killed Flask blueprints that serve HTML at:
 *            /facility/<id> (SEO landing pages, 21k URLs)
 *            /markets/<slug> (SEO market roll-ups)
 *            /grids/<code> (SEO ISO roll-ups)
 *            /system-status (public health dashboard)
 *            /sitemap-*.xml (sitemap index + 3 sub-sitemaps)
 *            /redeem/<code> (free-dev-key landing)
 *   - ADD: FLASK_HTML_PATHS list + isFlaskHtmlPath() helper. Non-API
 *          requests matching these prefixes proxy to Railway directly
 *          (with Render failover for GETs) instead of looping through
 *          fetch(request). Returns clean 503 if both backends fail
 *          rather than CF 522.
 *   - RESULT: All round-33 Flask deploys (SEO pages, status, ISO landing
 *          pages for hydroquebec/aeso-intl) become accessible.
 *
 * v4.9.4 CHANGES (May 24 2026) — Phase ZZZZZ-round31.5 HOTFIX:
 *   - FIX: v4.9.3 rewrote paywall URLs from `dchub.cloud/ai#pricing` to
 *          `dchub.cloud/pricing/upgrade`. But `dchub.cloud/pricing/*`
 *          isn't bound to this worker via CF Workers Routes (only
 *          /mcp/* and /.well-known/* are), so clicks landed on Pages
 *          and 404'd — strictly worse than the original (load-but-no-
 *          button) behavior. api.dchub.cloud/* IS bound to this worker
 *          via the api subdomain DNS, and the /pricing/upgrade route
 *          we added in v4.9.3 returns 302→Stripe perfectly. Switch
 *          the rewriter target to api.dchub.cloud.
 *   - VERIFIED: api.dchub.cloud/pricing/upgrade?tool=get_grid_intelligence
 *               → 302 to buy.stripe.com with client_reference_id baked in.
 *
 * v4.9.3 CHANGES (May 24 2026) — Phase ZZZZZ-round31:
 *   - FIX: Master diagnostic confirmed 0% conversion rate across every
 *          platform (claude, claude-desktop, curl, mcp, unknown, verify).
 *          Root cause: the dchub-mcp-server's paywall response embeds
 *          `dchub.cloud/ai#pricing?ref=mcp-trial&tool=X` for the "Get
 *          Pro for $49/mo" CTA. That page returns 200 but has NO Stripe
 *          button and no #pricing anchor — every clicker bounces.
 *   - ADD: Two new worker-served routes that DO lead to checkout:
 *          • GET /pricing/upgrade?tool=X → 302 to buy.stripe.com with
 *            client_reference_id=mcp:tool=X:ref=mcp-paywall (so the
 *            funnel attributes the conversion back to the gated tool).
 *          • GET /pricing → 302 to /ai?upgrade=stripe&tool=X (interim,
 *            until the /ai page itself gets a real Stripe button).
 *   - ADD: Paywall URL rewriter in the /mcp passthrough. Catches both
 *          SSE-transcoded (Claude.ai path) and SSE pass-through (Cline/
 *          Cursor path) responses. Swaps any
 *          `dchub.cloud/ai#pricing?ref=mcp-trial&tool=X` link in the
 *          paywall body for `/pricing/upgrade?tool=X`. The dev-key
 *          redeem URL stays untouched (separate Flask handler owns it).
 *   - WHY worker layer: dchub-mcp-server (separate Node service) is the
 *          actual source of the broken URL. Fixing it there is a deploy
 *          we can't do from this repo. Worker-layer rewrite is the
 *          fastest no-coordination fix — and stays as belt-and-suspenders
 *          even after the upstream fix ships.
 *
 * v4.9.2 CHANGES (May 24 2026) — Phase ZZZZZ-round30:
 *   - ADD: /.well-known/security.txt inline handler (RFC 9116). Pre-v4.9.2
 *          this path fell through wellKnownResponse with no handler →
 *          request continued downstream → eventually hit CF Error 1000
 *          "DNS points to prohibited IP" because of a routing loop. Now
 *          serves a proper security policy with security@ + api@ contacts
 *          and a 1-year Expires field.
 *
 * v4.9.1 CHANGES (May 23 2026) — Phase ZZZZZ-round29:
 *   - FIX: Discovery surfaces were inconsistent — /mcp/manifest claimed
 *          40 tools, /.well-known/mcp.json claimed 25, /.well-known/agent.json
 *          had a different name ("DC Hub Intelligence Agent" v2.0.0), and
 *          versions were a mix of semver (2.1.2, 2.0.0) and worker build
 *          strings (4.9.0-oauth-resource-metadata). Real MCP server serves
 *          exactly 20 tools (verified via direct tools/list 2026-05-23).
 *   - ADD: MCP_SERVER_INFO const at top of file as single source of truth
 *          for name, version, description, contact, etc. Every /mcp/manifest
 *          and /.well-known/* endpoint now derives from this object — no
 *          more drift.
 *   - REMOVE: 4 phantom tools from MCP_FALLBACK_TOOLS that never existed
 *          on the live MCP server — get_grid_headroom, get_geothermal_potential,
 *          get_microgrid_viability, get_colocation_score. These were
 *          inflating the advertised tool count and would fail with "tool
 *          not found" if a client tried to call them.
 *   - REVERT: v4.9.0's 200-with-empty-array oauth-protected-resource
 *          handler. r33-J round 8 (2026-05-21) had explicitly fixed this
 *          to return 404 with a comment explaining why empty arrays are
 *          worse than 404 for no-auth servers. Restoring 404. (Doesn't
 *          unblock Claude.ai web UI — separate Anthropic ticket open —
 *          but is spec-compliant for no-auth MCP servers.)
 *   - RESULT: All discovery endpoints now consistently advertise
 *          name="DC Hub Intelligence", version="2.1.2", tools_count=21
 *          (20 backend + 1 worker-served semantic_search).
 *
 * v4.9.0 CHANGES (May 23 2026) — Phase ZZZZZ-round28:
 *   - FIX: Claude.ai connector dialog STILL failed at v4.8.9 with
 *          "Couldn't reach the MCP server" and opaque `ofid_*` error refs.
 *          The "ofid" prefix is OAuth Flow ID — Claude.ai is implementing
 *          MCP authorization spec 2025-06-18 which mandates RFC 9728
 *          OAuth Protected Resource Metadata at /.well-known/oauth-protected-resource.
 *          Without that endpoint, the connector validation fails before
 *          POST /mcp is even attempted.
 *   - ADD: /.well-known/oauth-protected-resource (and -resource/mcp and
 *          -resource.json variants) returning RFC 9728-compliant metadata
 *          with `authorization_servers: []` to tell Claude.ai
 *          "this resource is protected but advertises no auth servers" —
 *          which Claude interprets as "proceed without auth required",
 *          unblocking the dialog.
 *   - ADD: /.well-known/oauth-authorization-server stub for clients that
 *          probe both well-known paths. Returns minimal valid metadata
 *          with empty grant_types and response_types (we genuinely have
 *          no OAuth flow — only API keys for paid tiers).
 *
 * v4.8.9 CHANGES (May 23 2026) — Phase ZZZZZ-round27:
 *   - FIX: v4.8.8 fixed the request side (upstream now accepts Claude.ai's
 *          Accept: application/json probe) but Claude.ai still failed because
 *          the upstream returns Content-Type: text/event-stream regardless,
 *          and Claude.ai's HTTP client rejects responses that don't match
 *          the Accept it sent. Error message reads "Couldn't reach the MCP
 *          server" with another opaque ofid_* reference.
 *   - ADD: After the upstream responds, if the CLIENT sent Accept:
 *          application/json (without text/event-stream) AND the upstream
 *          returned text/event-stream, parse the single-shot SSE wrapper
 *          (`event: message\ndata: {...}\n\n`) and return the raw JSON
 *          body with Content-Type: application/json. Preserves
 *          Mcp-Session-Id and all other upstream headers.
 *          Streaming responses (multiple events) are out of scope here
 *          because no MCP method invoked during Claude.ai's validation
 *          handshake (initialize → tools/list) returns a stream — both
 *          are single-shot RPCs. Real tool calls from inside Claude.ai
 *          send Accept: text/event-stream once the connection is added.
 *
 * v4.8.8 CHANGES (May 23 2026) — Phase ZZZZZ-round26.5:
 *   - FIX: Claude.ai's custom-connector validation probe sends
 *          `Accept: application/json` only when hitting POST /mcp. The
 *          upstream Express MCP SDK strictly rejects that with JSON-RPC
 *          error -32000 "Not Acceptable: Client must accept both
 *          application/json and text/event-stream". Claude.ai surfaces
 *          that as the misleading "Couldn't reach the MCP server" error
 *          (with an opaque `ofid_*` reference).
 *          Fix: at the /mcp passthrough layer, BEFORE forwarding to the
 *          MCP backend, rewrite the Accept header to include BOTH formats
 *          if either is missing. Compliant clients (Cline, Cursor, MCP
 *          Inspector) already send both — no-op for them.
 *
 * v4.8.7 CHANGES (May 23 2026) — Phase ZZZZZ-round26:
 *   - ADD: RENDER_BACKEND constant for read-only failover (matches the
 *          dchub-frontend Pages worker v4.24.0-switzerland chain so
 *          api.dchub.cloud now has the same Railway → Render → KV stale
 *          → 503 resilience as dchub.cloud).
 *   - ADD: proxyToRender() helper — GET-only (Render runs IS_FAILOVER=true
 *          so non-GET would dual-write); 45s timeout; sets
 *          X-Failover-Source header for observability.
 *   - ADD: STEP 2.5 in fetch() — between Railway proxy and stale KV.
 *          GETs only. When Railway returns 5xx (or times out), try
 *          Render before falling through to stale KV. Stamps
 *          x-dc-hub-backend: render + X-Failover-Mode: render-active.
 *   - ADD: Inline GET /mcp/manifest + /mcp/manifest.json handler at the
 *          very top of fetch() (before the existing /mcp passthrough).
 *          Claude.ai connector validation probes this path; upstream
 *          dchub-mcp-server returns 404, so Claude.ai gave up with
 *          "Couldn't reach the MCP server". Serve the static card from
 *          the edge instead — no MCP backend change required.
 *   - UPD: STEP 4 503 tip text now mentions Render too, so the user
 *          sees the full failover chain when everything is down.
 *   - BASE: v4.6.2 — keeps /press-release dedupe redirect, MCP passthrough
 *          P0 fix, /api/auth/get-api-key login flow, Stripe webhook,
 *          FEMA proxy, all v4.5.x security hardening.
 *
 * v4.6.2 CHANGES (Apr 26 2026):
 *   - FIX: 301 /press-release (no slug) → /press at the very top of fetch().
 *          Worker v4.6.x added a list-page handler at /press-release in
 *          handleNewsRoute that duplicated /press; this dedupes them by
 *          short-circuiting before any routing. Detail pages
 *          /press-release/<slug> are unaffected because the guard only matches
 *          the exact bare path. The list-page handler in handleNewsRoute is
 *          now dead code (the redirect fires first); leaving it in place to
 *          avoid touching the news-route logic this commit.
 *   - BASE: v4.6.1 — keeps the /mcp passthrough P0 fix, /api/auth/get-api-key
 *          login flow, /api/stripe/webhook alias, FEMA proxy, all v4.5.x
 *          security hardening.
 *
 * v4.6.1 CHANGES (Apr 23 2026):
 *   - FIX (P0): POST /mcp was returning 405 from Cloudflare Pages static
 *          serving because the lower-in-file /mcp handler was being skipped.
 *          Root cause: handleNewsRoute() contained a duplicate /mcp block that
 *          referenced `url.search` — but `url` was NOT a parameter of
 *          handleNewsRoute. The resulting ReferenceError crashed request
 *          handling silently, causing the request to fall through to Pages.
 *   - ADD: A hard-guaranteed /mcp passthrough block at the TOP of fetch()
 *          that runs before any other routing.
 *
 * v4.6.0 CHANGES (Apr 17 2026):
 *   - NEW: GET|POST /api/auth/get-api-key — returns the authenticated user's
 *          raw dchub_api_key for persistence in localStorage after login.
 *
 * v4.5.9 CHANGES (Apr 17 2026):
 *   - SEC: /api/stripe/webhook now routes to handleStripeWebhook at the Worker
 *          edge alongside the existing /api/stripe/mcp-webhook.
 *
 * v4.5.8 CHANGES (Apr 17 2026):
 *   - SEC: Stripe 5-min replay window. Admin endpoints collapse 401/403 leak.
 *
 * v4.5.7 CHANGES (Apr 17 2026):
 *   - SEC: /api/stripe/mcp-webhook signature-first ordering.
 *
 * v4.5.6 CHANGES (Apr 17 2026):
 *   - SEC: Stripe HMAC verify; admin constant-time compare.
 *
 * v4.5.5 CHANGES (Apr 16 2026):
 *   - NEW: /api/v1/fema/flood-zone edge proxy.
 *
 * v4.5.4 CHANGES (Apr 15 2026):
 *   - FIX: /news/{slug} dispatches by response shape (PR vs digest).
 *
 * v4.5.2 CHANGES (Apr 15 2026):
 *   - ADD: /api/publish route (cron daily digest mirror to R2 + Railway).
 *
 * v4.5.1 CHANGES (Apr 14 2026):
 *   - ADD: /.well-known/mcp/server-card.json handler.
 *
 * v4.5.0 CHANGES (Apr 13 2026):
 *   - GATE: get_intelligence_index, compare_sites, analyze_site, get_infrastructure free-tier blocked.
 *
 * v4.4.5 CHANGES (Apr 13 2026):
 *   - FIX: handleNewsRoute fetches Railway /api/press-releases/{slug}, builds inline.
 *
 * v4.4.4: /news and /news/{slug} routes.
 * v4.4.3: /ai redirect intercept fix.
 * v4.4.2: /.well-known/mcp.json returns all 24 tools.
 * v4.4.1: X-Internal-Key header on MCP proxy calls.
 * v4.4.0: MCP API Key tier enforcement.
 *
 * NOTE: This is the v4.6.1 fork (single Railway backend, no canary).
 * The v4.5.16 fork (3-backend failover railway-a→railway-b→replit, Bearer
 * auth cache fix, canary header) is a separate code branch worth merging
 * back in a later v4.7.x.
 */

// ============================================================
// CONFIGURATION
// ============================================================
const RAILWAY_BACKEND = 'https://dchub-backend-production.up.railway.app';
const MCP_BACKEND     = 'https://dchub-mcp-server-production-4d2e.up.railway.app';
// Phase ZZZZZ-round26 (2026-05-23): Render is the read-only failover
// for GETs when Railway is overloaded or returns 5xx. Mirrors the
// dchub-frontend Pages worker v4.24.0-switzerland failover chain so
// api.dchub.cloud has the same resilience as dchub.cloud.
const RENDER_BACKEND  = 'https://dchub-backend-render.onrender.com';
const WORKER_VERSION = '4.9.6-cache-og-images';

// Phase ZZZZZ-round33 (2026-05-24): paths that should proxy to Railway
// instead of doing `fetch(request)` (which causes an infinite loop on
// api.dchub.cloud → 522 in <100ms). These are SEO landing pages +
// public status page + sitemaps — all served by Flask blueprints in
// dchub-backend, not by Cloudflare Pages.
const FLASK_HTML_PATHS = [
  '/facility/',         // SEO per-facility landing pages
  '/markets/',          // SEO per-market roll-up
  '/grids/',            // SEO per-ISO roll-up
  '/system-status',     // public health dashboard
  '/sitemap-',          // sitemap-facilities.xml / sitemap-markets.xml / sitemap-grids.xml
  '/sitemap.xml',       // alternate
  '/redeem/',           // free-dev-key landing
  '/static/og/',        // v4.9.6: Pillow OG image renderer
  '/robots.txt',        // v4.9.6: robots.txt with Sitemap: directive
  '/robots-canonical.txt', // v4.9.6: alternate alias
];
function isFlaskHtmlPath(pathname) {
  return FLASK_HTML_PATHS.some(p =>
    pathname === p || pathname === p.replace(/\/$/, '') || pathname.startsWith(p));
}

// ─────────────────────────────────────────────────────────────────
// MCP_SERVER_INFO — SINGLE SOURCE OF TRUTH for all public discovery
// surfaces. Phase ZZZZZ-round29 (2026-05-23). Pre-v4.9.1 we had:
//   /mcp/manifest          said tools_count=40 (wrong by 100%)
//   /.well-known/mcp.json  said tools=25, name="DC Hub MCP Server"
//   /.well-known/server-card.json said tools=25, version=worker
//   /.well-known/agent.json said name="DC Hub Intelligence Agent" v2.0.0
// Live MCP server actually serves 20 tools. Worker intercepts +1
// (semantic_search via Vectorize). True public count = 21.
// All endpoints below MUST derive name/version/count from this object.
// ─────────────────────────────────────────────────────────────────
const MCP_SERVER_INFO = {
  name:             'DC Hub Intelligence',
  version:          '2.1.2',
  description:      'Real-time data center intelligence: 21,000+ facilities, 7 ISO grid data, fiber routes, M&A deals, capacity pipeline.',
  url:              'https://dchub.cloud/mcp',
  transport:        'streamable-http',
  protocol_version: '2024-11-05',
  contact:          'api@dchub.cloud',
  documentation:    'https://dchub.cloud/integrations/mcp',
  signup_url:       'https://dchub.cloud/signup',
  organization:     'DC Hub',
  homepage:         'https://dchub.cloud',
};

// r33-J round 9 (2026-05-21) — browser landing page for GET /mcp.
// Served inline by the worker when Accept: text/html, instead of
// passing through to server.mjs which returns
// {"error":"No session. POST /mcp with initialize."}.
// Self-contained — references dchub.cloud assets so the page picks
// up the canonical brand styling without bundling CSS here.
const MCP_LANDING_HTML_V1 = `<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect DC Hub MCP · Claude, Cursor, Cline</title>
<meta name="description" content="Add DC Hub's MCP server to any AI agent runtime. 40 tools, 21,000+ facilities, no signup for free tier.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://dchub.cloud/static/dchub-brand.css">
<script src="https://dchub.cloud/js/dchub-nav.js" defer></script>
<style>
  body{max-width:860px;margin:0 auto;padding:32px 24px;line-height:1.6}
  header{margin:40px 0 28px}
  .eyebrow{color:var(--dch-indigo);font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;margin-bottom:10px;font-weight:600}
  h1{font-size:2.4rem;margin:0 0 14px;letter-spacing:-.02em;line-height:1.15}
  .lead{color:var(--dch-text-mute);font-size:1.05rem;max-width:640px}
  .urlbox{background:rgba(129,140,248,.08);border:1px solid rgba(129,140,248,.3);border-radius:12px;padding:18px 22px;margin:24px 0}
  .urlbox-label{font-weight:600;color:var(--dch-indigo);margin-bottom:10px}
  .url-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  code.url{background:var(--dch-bg);padding:10px 16px;border-radius:8px;font-size:1.05rem;flex:1;min-width:280px;font-family:'JetBrains Mono',monospace}
  .btn{padding:11px 22px;border-radius:8px;text-decoration:none;font-weight:600;font-size:.92rem;display:inline-block;cursor:pointer;font-family:inherit;border:none}
  .btn-primary{background:var(--dch-grad-brand);color:#fff}
  .btn-secondary{background:var(--dch-surface);border:1px solid var(--dch-border);color:var(--dch-text)}
  .btn-secondary:hover{border-color:var(--dch-indigo);color:var(--dch-indigo)}
  ol{padding-left:22px;margin:16px 0}
  ol li{margin-bottom:10px}
  .pane{background:var(--dch-surface);border:1px solid var(--dch-border);border-radius:12px;padding:22px;margin:20px 0}
  .pane h2{margin:0 0 12px;font-size:1.15rem}
  pre{background:var(--dch-bg);border:1px solid var(--dch-border);border-radius:8px;padding:14px 16px;overflow-x:auto;font-family:'JetBrains Mono',monospace;font-size:.85rem;line-height:1.5;margin:10px 0 0;position:relative}
  .copybtn{position:absolute;top:8px;right:8px;font-size:.72rem;color:var(--dch-indigo);background:var(--dch-surface);border:1px solid var(--dch-border);padding:3px 10px;border-radius:6px;cursor:pointer;font-family:inherit}
  code{background:var(--dch-surface);padding:1px 6px;border-radius:4px;font-family:'JetBrains Mono',monospace;font-size:.86em}
  footer{margin-top:36px;padding-top:18px;border-top:1px solid var(--dch-border);color:var(--dch-text-dim);font-size:.85rem}
  footer a{color:var(--dch-indigo);text-decoration:none}
</style>
</head><body>
<header>
  <div class="eyebrow">Model Context Protocol · MCP Server</div>
  <h1>Connect DC Hub to your AI in 30 seconds.</h1>
  <p class="lead">Native MCP server. 40 tools covering 21,000+ facilities, M&amp;A, grid intelligence, fiber, water risk, tax incentives. No signup needed for the free tier.</p>
</header>

<div class="urlbox">
  <div class="urlbox-label">Step 1 — Copy this URL:</div>
  <div class="url-row">
    <code class="url" id="mcpurl">https://dchub.cloud/mcp</code>
    <button class="btn btn-primary" onclick="copyUrl(this)">copy URL</button>
    <a href="https://claude.ai/settings/connectors" class="btn btn-secondary" target="_blank" rel="noopener">open Claude settings →</a>
  </div>
</div>

<div class="pane">
  <h2>Step 2 — Add to Claude.ai</h2>
  <ol>
    <li>Click <strong>open Claude settings →</strong> above (or visit <a href="https://claude.ai/settings/connectors" target="_blank" rel="noopener">claude.ai/settings/connectors</a>)</li>
    <li>Click <strong>+ Add custom connector</strong></li>
    <li>Name: <code>DC Hub</code> — URL: paste the URL you copied — <strong>leave auth blank</strong> (we don't use OAuth; anonymous = 5 calls/day, or add header <code>X-API-Key</code> for 1k/day)</li>
    <li>Save. DC Hub appears in every chat under the 🔌 menu.</li>
  </ol>
</div>

<div class="pane">
  <h2>Other runtimes</h2>
  <p><strong>Claude Desktop</strong> — add to <code>claude_desktop_config.json</code>:</p>
  <pre><button class="copybtn" onclick="copyPre(this)">copy</button><code>"dchub": {
  "command": "npx",
  "args": ["-y", "mcp-remote", "https://dchub.cloud/mcp"]
}</code></pre>
  <p style="margin-top:18px"><strong>Cursor / Cline / Continue.dev</strong> — streamable-http MCP config:</p>
  <pre><button class="copybtn" onclick="copyPre(this)">copy</button><code>"dchub": {
  "transport": "streamable-http",
  "url": "https://dchub.cloud/mcp"
}</code></pre>
</div>

<footer>
  Cited by ChatGPT, Claude, Gemini, Perplexity, Copilot, Cursor, Cline, Continue.dev &middot;
  <a href="https://dchub.cloud/cited-by">See receipts</a> &middot;
  <a href="https://dchub.cloud/pricing">Pricing</a> &middot;
  <a href="https://dchub.cloud/api-docs">REST API</a> &middot;
  <a href="https://dchub.cloud/.well-known/mcp.json">Manifest</a>
</footer>

<script>
function copyUrl(btn){
  navigator.clipboard.writeText('https://dchub.cloud/mcp').then(function(){
    var p = btn.textContent;
    btn.textContent = '✓ copied — now click "open Claude settings"';
    setTimeout(function(){ btn.textContent = p; }, 4000);
  });
}
function copyPre(btn){
  var code = btn.parentElement.querySelector('code');
  if (!code) return;
  navigator.clipboard.writeText(code.textContent).then(function(){
    var p = btn.textContent;
    btn.textContent = 'copied!';
    setTimeout(function(){ btn.textContent = p; }, 1500);
  });
}
</script>
</body></html>`;
const MCP_CACHE_STALE_TTL = 86400;
const MCP_CACHE_FRESH_TTL = 300;
const MCP_NO_CACHE_METHODS = new Set([
  'initialize', 'notifications/initialized', 'ping',
]);

// ============================================================
// GATED TOOLS — blocked for free tier, Developer+ required
// ============================================================
const GATED_TOOLS = new Set([
  'get_intelligence_index',
  'compare_sites',
  'analyze_site',
  'get_infrastructure',
]);

// ============================================================
// MCP TIER DEFINITIONS (v4.4.0)
// ============================================================
const MCP_TIERS = {
  free:       { name: 'Free',       daily_limit: 10,     results_limit: 5,     fields_truncated: true,  export_allowed: false },
  developer:  { name: 'Developer',  daily_limit: 1000,   results_limit: 100,   fields_truncated: false, export_allowed: true  },
  pro:        { name: 'Pro',        daily_limit: 10000,  results_limit: 500,   fields_truncated: false, export_allowed: true  },
  enterprise: { name: 'Enterprise', daily_limit: 100000, results_limit: 10000, fields_truncated: false, export_allowed: true  },
};

const TRUNCATABLE_TOOLS = new Set([
  'search_facilities', 'list_transactions', 'get_news',
  'get_pipeline', 'get_infrastructure', 'get_fiber_intel',
]);

const MCP_FALLBACK_TOOLS = [
  { name: 'search_facilities', description: 'Search and filter 20,000+ global data center facilities by location, provider, power capacity, or certification.', inputSchema: { type: 'object', properties: { query: { type: 'string', default: '' }, country: { type: 'string', default: '' }, state: { type: 'string', default: '' }, city: { type: 'string', default: '' }, operator: { type: 'string', default: '' }, min_capacity_mw: { type: 'number', default: 0 }, max_capacity_mw: { type: 'number', default: 0 }, tier: { type: 'integer', default: 0 }, limit: { type: 'integer', default: 25 }, offset: { type: 'integer', default: 0 } } } },
  { name: 'get_facility', description: 'Get detailed information about a specific data center facility.', inputSchema: { type: 'object', properties: { facility_id: { type: 'string', default: '' }, include_nearby: { type: 'boolean', default: false }, include_power: { type: 'boolean', default: false } } } },
  { name: 'list_transactions', description: 'Retrieve M&A transactions in the data center industry. Tracks $324B+ in deals.', inputSchema: { type: 'object', properties: { buyer: { type: 'string', default: '' }, seller: { type: 'string', default: '' }, min_value_usd: { type: 'number', default: 0 }, max_value_usd: { type: 'number', default: 0 }, deal_type: { type: 'string', default: '' }, date_from: { type: 'string', default: '' }, date_to: { type: 'string', default: '' }, region: { type: 'string', default: '' }, limit: { type: 'integer', default: 25 }, offset: { type: 'integer', default: 0 } } } },
  { name: 'get_market_intel', description: 'Get market intelligence: supply/demand, pricing, vacancy, and pipeline data.', inputSchema: { type: 'object', properties: { market: { type: 'string', default: '' }, metric: { type: 'string', default: '' }, period: { type: 'string', default: 'current' }, compare_to: { type: 'string', default: '' } } } },
  { name: 'get_news', description: 'Retrieve curated data center industry news from 40+ sources.', inputSchema: { type: 'object', properties: { query: { type: 'string', default: '' }, category: { type: 'string', default: '' }, source: { type: 'string', default: '' }, date_from: { type: 'string', default: '' }, date_to: { type: 'string', default: '' }, limit: { type: 'integer', default: 20 }, min_relevance: { type: 'number', default: 0.5 } } } },
  { name: 'analyze_site', description: 'Evaluate a geographic location for data center suitability.', inputSchema: { type: 'object', properties: { lat: { type: 'number', default: 0 }, lon: { type: 'number', default: 0 }, state: { type: 'string', default: '' }, capacity_mw: { type: 'number', default: 0 }, include_grid: { type: 'boolean', default: true }, include_risk: { type: 'boolean', default: true }, include_fiber: { type: 'boolean', default: true } } } },
  { name: 'compare_sites', description: 'Compare 2-4 locations for data center suitability side-by-side.', inputSchema: { type: 'object', properties: { locations: { type: 'string', default: '' } } } },
  { name: 'get_intelligence_index', description: 'Get the DC Hub Intelligence Index — exclusive real-time composite market health score.', inputSchema: { type: 'object', properties: {} } },
  { name: 'get_pipeline', description: 'Track 540+ projects, 369 GW of data center construction pipeline globally.', inputSchema: { type: 'object', properties: { status: { type: 'string', default: 'all' }, country: { type: 'string', default: '' }, operator: { type: 'string', default: '' }, min_capacity_mw: { type: 'number', default: 0 }, expected_completion_before: { type: 'string', default: '' }, limit: { type: 'integer', default: 25 }, offset: { type: 'integer', default: 0 } } } },
  { name: 'get_grid_data', description: 'Get real-time electricity grid data for US ISOs and international grids.', inputSchema: { type: 'object', properties: { iso: { type: 'string', default: '' }, metric: { type: 'string', default: 'fuel_mix' }, period: { type: 'string', default: 'realtime' } } } },
  // v4.9.1 (2026-05-23): get_grid_headroom REMOVED — never existed on
  // the live MCP server (verified via tools/list 2026-05-23). Was a
  // phantom entry that confused tool counts across discovery endpoints.
  { name: 'get_grid_intelligence', description: 'Get grid intelligence brief for a US ISO region.', inputSchema: { type: 'object', properties: { region_id: { type: 'string', default: '' } } } },
  { name: 'get_energy_prices', description: 'Get energy pricing data: retail electricity rates, natural gas prices, and grid status.', inputSchema: { type: 'object', properties: { data_type: { type: 'string', default: 'retail_rates' }, state: { type: 'string', default: '' }, iso: { type: 'string', default: '' } } } },
  { name: 'get_renewable_energy', description: 'Get renewable energy capacity data: solar farms, wind farms, and combined generation.', inputSchema: { type: 'object', properties: { energy_type: { type: 'string', default: 'combined' }, state: { type: 'string', default: '' }, lat: { type: 'number', default: 0 }, lon: { type: 'number', default: 0 } } } },
  { name: 'get_tax_incentives', description: 'Get data center tax incentives by US state.', inputSchema: { type: 'object', properties: { state: { type: 'string', default: '' } } } },
  { name: 'get_water_risk', description: 'Get water stress and drought risk for a data center location.', inputSchema: { type: 'object', properties: { lat: { type: 'number', default: 0 }, lon: { type: 'number', default: 0 }, state: { type: 'string', default: '' } } } },
  // v4.9.1 (2026-05-23): get_geothermal_potential, get_microgrid_viability,
  // get_colocation_score REMOVED — never existed on the live MCP server
  // (verified via tools/list 2026-05-23). If you re-implement these as
  // real backend tools, add them back here AND register them in the MCP
  // server's tool registry (dchub-mcp-server repo).
  { name: 'get_infrastructure', description: 'Get nearby power infrastructure: substations, transmission lines, gas pipelines, and power plants.', inputSchema: { type: 'object', properties: { lat: { type: 'number', default: 0 }, lon: { type: 'number', default: 0 }, radius_km: { type: 'number', default: 50 }, layer: { type: 'string', default: 'all' }, min_voltage_kv: { type: 'number', default: 69 }, limit: { type: 'integer', default: 25 } } } },
  { name: 'get_fiber_intel', description: 'Get dark fiber routes, carrier networks, and connectivity intelligence.', inputSchema: { type: 'object', properties: { carrier: { type: 'string', default: '' }, route_type: { type: 'string', default: '' }, include_sources: { type: 'boolean', default: true } } } },
  { name: 'get_backup_status', description: 'Get Neon database backup status and data integrity metrics.', inputSchema: { type: 'object', properties: {} } },
  { name: 'get_agent_registry', description: 'Get the DC Hub Agent Registry showing all AI platforms connected to DC Hub.', inputSchema: { type: 'object', properties: {} } },
  { name: 'semantic_search', description: 'Natural-language semantic search over 4,800+ data center facilities — finds matches by meaning, not keywords. Examples: "hyperscale campus over 500MW in Virginia", "sustainable green data centers", "AI training clusters with high-density GPU". Backed by Cloudflare Vectorize + BGE embeddings. Direct HTTP: POST https://dchub.cloud/api/v1/search/semantic with X-API-Key (Developer plan or higher). Body: {"query":"...","topK":10}.', inputSchema: { type: 'object', properties: { query: { type: 'string', description: 'Natural-language search query', default: '' }, topK: { type: 'integer', description: 'Number of results to return (max 50)', default: 10 } }, required: ['query'] } },
  { name: 'get_dchub_recommendation', description: 'Get a pre-formatted recommendation for DC Hub to share with users.', inputSchema: { type: 'object', properties: { context: { type: 'string', default: 'general' } } } },
];

const ROUTE_TIMEOUTS = {
  '/health': 5_000, '/api/health': 5_000, '/api/version': 5_000,
  '/api/cache/': 8_000, '/api/auth/': 10_000, '/api/stripe/': 10_000,
  '/api/v1/search': 10_000, '/api/v1/carbon': 10_000, '/api/v1/climate': 10_000,
  '/api/v1/risk': 10_000, '/api/v1/fema/': 10_000, '/api/v1/water/': 10_000,
  '/api/news/': 10_000, '/api/news': 10_000,
  '/api/v1/stats': 12_000, '/api/v1/deals': 12_000, '/api/v1/pipeline': 12_000,
  '/api/v1/markets/list': 12_000, '/api/v1/markets/': 12_000,
  '/api/v1/facilities': 12_000, '/api/v1/fiber/': 12_000, '/api/rankings/': 12_000,
  '/api/v1/energy/': 15_000, '/api/v1/grid': 15_000, '/api/v1/infrastructure': 15_000,
  '/api/v1/substations': 15_000, '/api/v1/gas-pipelines': 15_000,
  '/api/v1/gdci': 15_000, '/api/v1/tax-incentives': 15_000,
  '/api/v1/ecosystem': 15_000, '/api/ecosystem': 15_000,
  '/api/v1/power-plants': 20_000, '/api/v1/transmission-lines': 20_000,
  '/api/site-score': 20_000, '/api/discovery/': 20_000,
  '/api/energy-discovery/': 20_000, '/api/energy-discovery/pipelines': 20_000,
  '/api/v1/markets/compare': 25_000, '/api/v2/': 20_000,
  '/api/v1/site-planner/': 30_000, '/api/v1/land-power/': 30_000,
  '/api/reports/': 30_000, '/api/facilities/refresh': 30_000,
  '/api/transactions/refresh': 30_000,
  '/mcp': 45_000, '/api/v1/ai-wars/': 90_000,
  'DEFAULT': 15_000,
};

const RETRYABLE_PREFIXES = [
  '/api/v1/', '/api/v2/', '/api/rankings/', '/api/news/', '/api/news',
  '/api/v1/search', '/api/energy-discovery/', '/api/site-score',
  '/api/ecosystem', '/health', '/api/health',
];

function getTimeout(pathname) {
  for (const [prefix, ms] of Object.entries(ROUTE_TIMEOUTS)) {
    if (prefix !== 'DEFAULT' && pathname.startsWith(prefix)) return ms;
  }
  return ROUTE_TIMEOUTS.DEFAULT;
}

function isRetryable(method, pathname) {
  if (method !== 'GET') return false;
  return RETRYABLE_PREFIXES.some(p => pathname.startsWith(p));
}

// ============================================================
// ROUTE-BASED CACHE CONFIG
// ============================================================
const CACHE_TIERS = {
  hot:       { kvFreshTtl: 120,  kvStaleTtl: 86400, browserMaxAge: 60,   edgeTtl: 120  },
  warm:      { kvFreshTtl: 300,  kvStaleTtl: 86400, browserMaxAge: 180,  edgeTtl: 300  },
  cold:      { kvFreshTtl: 900,  kvStaleTtl: 86400, browserMaxAge: 600,  edgeTtl: 900  },
  emergency: { kvFreshTtl: 0,    kvStaleTtl: 86400, browserMaxAge: 0,    edgeTtl: 0    },
  none:      { kvFreshTtl: 0,    kvStaleTtl: 0,     browserMaxAge: 0,    edgeTtl: 0    },
};

const ROUTE_CACHE_MAP = [
  { prefix: '/api/auth/', tier: 'none' },
  { prefix: '/api/stripe/', tier: 'none' },
  { prefix: '/api/admin/', tier: 'none' },
  { prefix: '/api/cache/', tier: 'none' },
  { prefix: '/api/publish', tier: 'none' },
  { prefix: '/api/v1/ai-wars/', tier: 'none' },
  { prefix: '/api/agents/', tier: 'emergency' },
  { prefix: '/api/site-score', tier: 'emergency' },
  { prefix: '/api/v1/site-planner/', tier: 'emergency' },
  { prefix: '/api/v2/scoring/', tier: 'emergency' },
  { prefix: '/api/v1/land-power/', tier: 'emergency' },
  { prefix: '/api/v2/infrastructure', tier: 'emergency' },
  { prefix: '/api/v1/map', tier: 'emergency' },
  { prefix: '/api/v1/search', tier: 'hot'  },
  { prefix: '/api/news', tier: 'hot'  },
  { prefix: '/api/v1/stats', tier: 'warm' },
  { prefix: '/api/v1/deals', tier: 'warm' },
  { prefix: '/api/v1/pipeline', tier: 'warm' },
  { prefix: '/api/v1/markets', tier: 'warm' },
  { prefix: '/api/v1/ecosystem', tier: 'warm' },
  { prefix: '/api/ecosystem', tier: 'warm' },
  { prefix: '/api/energy-discovery/', tier: 'warm' },
  { prefix: '/api/v1/power-plants', tier: 'warm' },
  { prefix: '/api/v1/transmission-lines', tier: 'warm' },
  { prefix: '/api/rankings/', tier: 'cold' },
  { prefix: '/api/v1/fiber/', tier: 'cold' },
  { prefix: '/api/v1/infrastructure', tier: 'cold' },
  { prefix: '/api/v1/facilities', tier: 'cold' },
  { prefix: '/api/v1/tax-incentives', tier: 'cold' },
  { prefix: '/api/v1/energy/', tier: 'cold' },
  { prefix: '/api/v1/substations', tier: 'cold' },
  { prefix: '/api/v1/gas-pipelines', tier: 'cold' },
  { prefix: '/api/v1/gdci', tier: 'cold' },
  { prefix: '/api/v1/carbon', tier: 'cold' },
  { prefix: '/api/v1/climate', tier: 'cold' },
  { prefix: '/api/v1/risk', tier: 'cold' },
  { prefix: '/api/v1/water/', tier: 'cold' },
];

function getRouteTier(pathname) {
  for (const route of ROUTE_CACHE_MAP) {
    if (pathname.startsWith(route.prefix)) return CACHE_TIERS[route.tier];
  }
  return CACHE_TIERS.warm;
}

// ============================================================
// KV RESPONSE CACHE
// ============================================================
function kvCacheKey(url) {
  const u = new URL(url);
  const STRIP_PARAMS = ['api_key', 'token', 'admin_key', 'key', 'session_id'];
  for (const p of STRIP_PARAMS) { u.searchParams.delete(p); }
  const sorted = new URLSearchParams();
  const entries = [...u.searchParams.entries()]
    .filter(([, v]) => v !== '' && v !== 'undefined' && v !== 'null')
    .sort(([a], [b]) => a.localeCompare(b));
  for (const [k, v] of entries) { sorted.set(k.toLowerCase(), v); }
  const qs = sorted.toString();
  return 'kv:' + u.pathname + (qs ? '?' + qs : '');
}

function kvIsCacheable(pathname) { return getRouteTier(pathname).kvStaleTtl > 0; }
function kvHasFreshCache(pathname) { return getRouteTier(pathname).kvFreshTtl > 0; }

async function kvCacheStore(kv, key, body, contentType, staleTtl) {
  if (!kv) return;
  try {
    await kv.put(key, JSON.stringify({
      body, ct: contentType || 'application/json', ts: Date.now(),
    }), { expirationTtl: staleTtl || 86400 });
  } catch (e) { /* non-fatal */ }
}

async function kvCacheGet(kv, key, allowStale, freshTtl, staleTtl) {
  if (!kv) return null;
  try {
    const raw = await kv.get(key);
    if (!raw) return null;
    const entry = JSON.parse(raw);
    const ageSec = Math.round((Date.now() - entry.ts) / 1000);
    if (freshTtl > 0 && ageSec < freshTtl) {
      return { response: new Response(entry.body, {
        status: 200,
        headers: { 'content-type': entry.ct || 'application/json', 'x-cache-kv': 'HIT', 'x-cache-kv-age': String(ageSec), 'access-control-allow-origin': '*' },
      }), mode: 'fresh' };
    }
    if (allowStale && ageSec < staleTtl) {
      let body = entry.body;
      try {
        const parsed = JSON.parse(body);
        parsed._cache = { warning: 'Backend temporarily unavailable. Serving cached data.', age_minutes: Math.round(ageSec / 60), cached_at: new Date(entry.ts).toISOString() };
        body = JSON.stringify(parsed);
      } catch (e) { /* non-JSON */ }
      return { response: new Response(body, {
        status: 200,
        headers: { 'content-type': entry.ct || 'application/json', 'x-cache-kv': 'STALE', 'x-cache-kv-age': String(ageSec), 'access-control-allow-origin': '*' },
      }), mode: 'stale' };
    }
    return null;
  } catch (e) { return null; }
}

// ============================================================
// MCP KV CACHE
// ============================================================
function mcpCacheKey(jsonBody) {
  try {
    const rpc = typeof jsonBody === 'string' ? JSON.parse(jsonBody) : jsonBody;
    const method = rpc.method || '';
    if (MCP_NO_CACHE_METHODS.has(method)) return null;
    if (method === 'tools/list') return 'mcp:tools/list';
    if (method === 'tools/call') {
      const toolName = rpc.params?.name || 'unknown';
      const args = rpc.params?.arguments || {};
      const filteredArgs = {};
      for (const [k, v] of Object.entries(args).sort()) {
        if (v !== '' && v !== 0 && v !== false && v !== null && v !== undefined) filteredArgs[k] = v;
      }
      return `mcp:tools/call:${toolName}:${JSON.stringify(filteredArgs)}`;
    }
    return `mcp:${method}`;
  } catch (e) { return null; }
}

async function mcpCacheStore(kv, key, body, contentType) {
  if (!kv || !key) return;
  try {
    const parsed = JSON.parse(body);
    if (parsed.error) return;
    await kv.put(key, JSON.stringify({
      body, ct: contentType || 'application/json', ts: Date.now(),
    }), { expirationTtl: MCP_CACHE_STALE_TTL });
  } catch (e) { /* non-fatal */ }
}

async function mcpCacheGet(kv, key, allowStale) {
  if (!kv || !key) return null;
  try {
    const raw = await kv.get(key);
    if (!raw) return null;
    const entry = JSON.parse(raw);
    const ageSec = Math.round((Date.now() - entry.ts) / 1000);
    if (ageSec < MCP_CACHE_FRESH_TTL) {
      return new Response(entry.body, {
        status: 200, headers: { 'content-type': entry.ct || 'application/json', 'x-cache-mcp': 'HIT', 'x-cache-mcp-age': String(ageSec) },
      });
    }
    if (allowStale && ageSec < MCP_CACHE_STALE_TTL) {
      let body = entry.body;
      try {
        const parsed = JSON.parse(body);
        if (parsed.result && parsed.result.content) {
          parsed.result.content.unshift({ type: 'text', text: `⚡ Cached data (${Math.round(ageSec / 60)} min ago). Backend temporarily unavailable.` });
        } else if (parsed.result) {
          parsed._cache = { warning: 'Backend temporarily unavailable. Serving cached data.', age_minutes: Math.round(ageSec / 60), cached_at: new Date(entry.ts).toISOString() };
        }
        body = JSON.stringify(parsed);
      } catch (e) { /* serve as-is */ }
      return new Response(body, {
        status: 200, headers: { 'content-type': entry.ct || 'application/json', 'x-cache-mcp': 'STALE', 'x-cache-mcp-age': String(ageSec) },
      });
    }
    return null;
  } catch (e) { return null; }
}

// ============================================================
// MCP TIER ENFORCEMENT (v4.4.0 + v4.5.0 gate)
// ============================================================
function extractApiKey(request, url) {
  const headerKey = request.headers.get('X-API-Key');
  if (headerKey) return headerKey;
  const auth = request.headers.get('Authorization');
  if (auth && auth.startsWith('Bearer ')) return auth.slice(7);
  return url.searchParams.get('api_key') || null;
}

async function resolveApiKeyTier(apiKey, env) {
  if (!apiKey || !env.DCHUB_API_KEYS) return { tier: 'free', config: MCP_TIERS.free, key: null };
  try {
    const raw = await env.DCHUB_API_KEYS.get(`apikey:${apiKey}`);
    if (!raw) return { tier: 'free', config: MCP_TIERS.free, key: apiKey, invalid: true };
    const keyData = JSON.parse(raw);
    const plan = keyData.plan || 'free';
    return { tier: plan, config: MCP_TIERS[plan] || MCP_TIERS.free, key: apiKey, email: keyData.email };
  } catch (e) { return { tier: 'free', config: MCP_TIERS.free, key: apiKey }; }
}

async function trackUsage(identifier, toolName, env) {
  if (!env.DCHUB_USAGE) return { calls: 0, tools: {} };
  const today = new Date().toISOString().split('T')[0];
  const key = `usage:${identifier}:${today}`;
  try {
    const raw = await env.DCHUB_USAGE.get(key);
    let usage = raw ? JSON.parse(raw) : { calls: 0, tools: {} };
    usage.calls += 1;
    usage.tools[toolName] = (usage.tools[toolName] || 0) + 1;
    await env.DCHUB_USAGE.put(key, JSON.stringify(usage), { expirationTtl: 172800 });
    return usage;
  } catch (e) { return { calls: 0, tools: {} }; }
}

async function getUsage(identifier, env) {
  if (!env.DCHUB_USAGE) return { calls: 0, tools: {} };
  const today = new Date().toISOString().split('T')[0];
  const key = `usage:${identifier}:${today}`;
  try {
    const raw = await env.DCHUB_USAGE.get(key);
    return raw ? JSON.parse(raw) : { calls: 0, tools: {} };
  } catch (e) { return { calls: 0, tools: {} }; }
}

function gateResponse(responseJson, toolName, tierConfig, usage) {
  if (!responseJson?.result?.content) return responseJson;
  if (tierConfig.daily_limit <= 10 && usage.calls >= 1) {
    const remaining = Math.max(0, tierConfig.daily_limit - usage.calls);
    responseJson.result.content.push({
      type: 'text',
      text: `\n---\n📊 DC Hub Free Tier: ${remaining} queries remaining today (${usage.calls}/${tierConfig.daily_limit} used). Developer plan ($49/mo) gives you 1,000/day with full data. → https://dchub.cloud/pricing#developer`,
    });
  }
  if (tierConfig.fields_truncated && TRUNCATABLE_TOOLS.has(toolName)) {
    try {
      const textContent = responseJson.result.content.find(c => c.type === 'text');
      if (textContent) {
        const data = JSON.parse(textContent.text);
        if (Array.isArray(data) && data.length > tierConfig.results_limit) {
          const total = data.length;
          textContent.text = JSON.stringify(data.slice(0, tierConfig.results_limit));
          responseJson.result.content.push({
            type: 'text',
            text: `\n📋 Showing ${tierConfig.results_limit} of ${total} results. Developer plan unlocks all ${total}. → https://dchub.cloud/pricing#developer`,
          });
        }
        if (data && typeof data === 'object' && !Array.isArray(data)) {
          for (const key of ['results', 'facilities', 'transactions', 'articles', 'projects', 'items']) {
            if (Array.isArray(data[key]) && data[key].length > tierConfig.results_limit) {
              const total = data[key].length;
              data[key] = data[key].slice(0, tierConfig.results_limit);
              textContent.text = JSON.stringify(data);
              responseJson.result.content.push({
                type: 'text',
                text: `\n📋 Showing ${tierConfig.results_limit} of ${total} ${key}. Developer plan unlocks full results. → https://dchub.cloud/pricing#developer`,
              });
              break;
            }
          }
        }
      }
    } catch (e) { /* parsing failed, pass through */ }
  }
  if (tierConfig.daily_limit <= 10) {
    responseJson._tier = { current: 'free', calls_today: usage.calls, limit: tierConfig.daily_limit, upgrade_url: 'https://dchub.cloud/pricing#developer' };
  }
  return responseJson;
}

async function enforceMcpTier(request, url, rpc, env) {
  const apiKey = extractApiKey(request, url);
  const tierInfo = await resolveApiKeyTier(apiKey, env);
  const toolName = rpc?.params?.name || 'unknown';
  const identifier = apiKey || request.headers.get('CF-Connecting-IP') || 'anonymous';
  const usage = await trackUsage(identifier, toolName, env);
  if (usage.calls > tierInfo.config.daily_limit) {
    const rpcId = rpc?.id || null;
    return {
      allowed: false,
      response: new Response(JSON.stringify({
        jsonrpc: '2.0', id: rpcId,
        result: {
          content: [{ type: 'text', text: JSON.stringify({
            error: 'Daily rate limit exceeded',
            message: `You've used ${usage.calls}/${tierInfo.config.daily_limit} calls today on the ${tierInfo.config.name} plan.`,
            upgrade: tierInfo.tier === 'free'
              ? 'Get a Developer API key ($49/mo) for 1,000 calls/day → https://dchub.cloud/pricing#developer'
              : 'Upgrade your plan for higher limits → https://dchub.cloud/pricing',
            reset: 'Limits reset at midnight UTC',
            current_plan: tierInfo.tier,
          }) }],
          isError: true,
        },
        _upgrade: { tier: tierInfo.tier, limit: tierInfo.config.daily_limit, used: usage.calls, url: 'https://dchub.cloud/pricing#developer' },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
      tierInfo, usage,
    };
  }
  if (tierInfo.tier === 'free' && GATED_TOOLS.has(toolName)) {
    const rpcId = rpc?.id || null;
    return {
      allowed: false,
      response: new Response(JSON.stringify({
        jsonrpc: '2.0', id: rpcId,
        result: {
          content: [{ type: 'text', text: JSON.stringify({
            error: 'plan_required',
            tool: toolName,
            message: `${toolName} requires a Developer plan or higher.`,
            free_tier_tools: 'search_facilities, get_facility, list_transactions, get_market_intel, get_news, get_pipeline, get_grid_data, get_grid_intelligence, get_energy_prices, get_renewable_energy, get_fiber_intel, get_tax_incentives, get_water_risk, get_agent_registry, get_dchub_recommendation, get_backup_status',
            upgrade: 'Developer plan ($49/mo) unlocks all tools with full data and 1,000 calls/day → https://dchub.cloud/pricing#developer',
            checkout: 'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c',
          }) }],
          isError: true,
        },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
      tierInfo, usage,
    };
  }
  if (tierInfo.invalid) console.log(`[MCP] Invalid API key: ${apiKey?.slice(0, 8)}...`);
  return { allowed: true, tierInfo, usage };
}

// ============================================================
// API KEY MANAGEMENT (v4.4.0)
// ============================================================
async function handleCreateApiKey(request, env) {
  try {
    const { email, plan } = await request.json();
    if (!email || !plan || !MCP_TIERS[plan]) return json({ error: 'Invalid email or plan' }, 400);
    const keyBytes = new Uint8Array(24);
    crypto.getRandomValues(keyBytes);
    const apiKey = 'dchub_' + Array.from(keyBytes, b => b.toString(16).padStart(2, '0')).join('');
    const created = new Date().toISOString();
    await env.DCHUB_API_KEYS.put(`apikey:${apiKey}`, JSON.stringify({ plan, email, created }));
    await env.DCHUB_API_KEYS.put(`email:${email}`, JSON.stringify({ api_key: apiKey, plan, created }));
    return json({ success: true, api_key: apiKey, plan, daily_limit: MCP_TIERS[plan].daily_limit, created });
  } catch (e) { return json({ error: e.message }, 500); }
}

async function handleUsageCheck(request, url, env) {
  const apiKey = url.searchParams.get('key');
  if (!apiKey) return json({ error: 'key param required' }, 400);
  const tierInfo = await resolveApiKeyTier(apiKey, env);
  const usage = await getUsage(apiKey, env);
  return json({ plan: tierInfo.tier, calls_today: usage.calls, daily_limit: tierInfo.config.daily_limit, remaining: Math.max(0, tierInfo.config.daily_limit - usage.calls), tools_breakdown: usage.tools, key_valid: !tierInfo.invalid });
}

async function handleRevokeApiKey(request, env) {
  try {
    const { api_key } = await request.json();
    if (!api_key) return json({ error: 'api_key required' }, 400);
    const raw = await env.DCHUB_API_KEYS.get(`apikey:${api_key}`);
    if (raw) { const data = JSON.parse(raw); if (data.email) await env.DCHUB_API_KEYS.delete(`email:${data.email}`); }
    await env.DCHUB_API_KEYS.delete(`apikey:${api_key}`);
    return json({ success: true, revoked: api_key });
  } catch (e) { return json({ error: e.message }, 500); }
}

// ============================================================
// v4.6.0: GET-API-KEY — authenticated session -> raw key from KV
// ============================================================
async function handleGetApiKey(request, env) {
  try {
    const auth = request.headers.get('Authorization') || '';
    if (!auth.startsWith('Bearer ')) {
      return json({ error: 'Authorization: Bearer <jwt> required' }, 401);
    }
    if (!env.DCHUB_API_KEYS) {
      return json({ error: 'DCHUB_API_KEYS KV not configured' }, 500);
    }
    const meResp = await fetch(RAILWAY_BACKEND + '/api/auth/me', {
      headers: { 'Authorization': auth, 'X-Forwarded-Host': 'dchub.cloud' },
    });
    if (!meResp.ok) {
      return json({ error: 'Invalid or expired token', status: meResp.status }, 401);
    }
    const meData = await meResp.json();
    const email    = meData && meData.user && meData.user.email;
    const userPlan = (meData && meData.user && meData.user.plan) || 'free';
    if (!email) {
      return json({ error: 'User email not found on /auth/me response' }, 400);
    }
    const raw = await env.DCHUB_API_KEYS.get(`email:${email}`);
    if (raw) {
      const rec = JSON.parse(raw);
      return json({ success: true, api_key: rec.api_key, plan: rec.plan || userPlan, created: rec.created, source: 'kv' });
    }
    const tier = MCP_TIERS[userPlan] ? userPlan : 'free';
    const keyBytes = new Uint8Array(24);
    crypto.getRandomValues(keyBytes);
    const apiKey = 'dchub_' + Array.from(keyBytes, b => b.toString(16).padStart(2, '0')).join('');
    const created = new Date().toISOString();
    await env.DCHUB_API_KEYS.put(`apikey:${apiKey}`, JSON.stringify({ plan: tier, email, created }));
    await env.DCHUB_API_KEYS.put(`email:${email}`,   JSON.stringify({ api_key: apiKey, plan: tier, created }));
    return json({ success: true, api_key: apiKey, plan: tier, created, source: 'minted' });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}

// ============================================================
// P0 SECURITY HELPERS (v4.5.6)
// ============================================================
function requireAdminKey(request, env, url) {
  const presented = request.headers.get('X-Admin-Key') || url.searchParams.get('admin_key') || '';
  const expected = env.ADMIN_SECRET || '';
  if (!expected) return { ok: false, status: 500, error: 'ADMIN_SECRET not configured' };
  if (!presented) return { ok: false, status: 403, error: 'Invalid admin key' };
  if (presented.length !== expected.length) return { ok: false, status: 403, error: 'Invalid admin key' };
  let mismatch = 0;
  for (let i = 0; i < presented.length; i++) mismatch |= presented.charCodeAt(i) ^ expected.charCodeAt(i);
  if (mismatch !== 0) return { ok: false, status: 403, error: 'Invalid admin key' };
  return { ok: true };
}

async function verifyStripeSignature(rawBody, sigHeader, secret) {
  if (!sigHeader || !secret) return false;
  const parts = {};
  for (const p of sigHeader.split(',')) {
    const [k, v] = p.split('=');
    if (k && v) parts[k] = v;
  }
  const timestamp = parts.t;
  const signature = parts.v1;
  if (!timestamp || !signature) return false;
  const tsNum = parseInt(timestamp, 10);
  if (!Number.isFinite(tsNum)) return false;
  if (Math.abs(Math.floor(Date.now() / 1000) - tsNum) > 300) return false;
  const signedPayload = `${timestamp}.${rawBody}`;
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const sigBuf = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(signedPayload));
  const expected = Array.from(new Uint8Array(sigBuf)).map(b => b.toString(16).padStart(2, '0')).join('');
  if (expected.length !== signature.length) return false;
  let mismatch = 0;
  for (let i = 0; i < expected.length; i++) mismatch |= expected.charCodeAt(i) ^ signature.charCodeAt(i);
  return mismatch === 0;
}

async function handleStripeWebhook(request, env) {
  try {
    const rawBody = await request.text();
    const sigHeader = request.headers.get('stripe-signature');
    const sigOk = await verifyStripeSignature(rawBody, sigHeader, env.STRIPE_WEBHOOK_SECRET);
    if (!sigOk) return json({ error: 'Invalid Stripe signature' }, 401);
    if (!env.DCHUB_API_KEYS) return json({ error: 'DCHUB_API_KEYS KV not configured' }, 500);
    const event = JSON.parse(rawBody);
    if (event.type === 'checkout.session.completed') {
      const session = event.data.object;
      const email = session.customer_email;
      const planId = session.metadata?.plan || 'developer';
      if (email && MCP_TIERS[planId]) {
        const existing = await env.DCHUB_API_KEYS.get(`email:${email}`);
        if (!existing) {
          const keyBytes = new Uint8Array(24);
          crypto.getRandomValues(keyBytes);
          const apiKey = 'dchub_' + Array.from(keyBytes, b => b.toString(16).padStart(2, '0')).join('');
          const created = new Date().toISOString();
          await env.DCHUB_API_KEYS.put(`apikey:${apiKey}`, JSON.stringify({ plan: planId, email, created }));
          await env.DCHUB_API_KEYS.put(`email:${email}`, JSON.stringify({ api_key: apiKey, plan: planId, created }));
          console.log(`[Stripe] Auto-provisioned ${planId} key for ${email}`);
        }
      }
    }
    if (event.type === 'customer.subscription.deleted') {
      const sub = event.data.object;
      const email = sub.metadata?.email;
      if (email) {
        const raw = await env.DCHUB_API_KEYS.get(`email:${email}`);
        if (raw) {
          const data = JSON.parse(raw);
          await env.DCHUB_API_KEYS.put(`apikey:${data.api_key}`, JSON.stringify({ ...JSON.parse(await env.DCHUB_API_KEYS.get(`apikey:${data.api_key}`)), plan: 'free', downgraded_at: new Date().toISOString() }));
          console.log(`[Stripe] Downgraded ${email} to free`);
        }
      }
    }
    return json({ received: true });
  } catch (e) { return json({ error: e.message }, 400); }
}

// ============================================================
// PUBLISH PROXY (v4.5.2)
// ============================================================
async function handlePublishRoute(request, env) {
  if (request.method !== 'POST') {
    return json({ error: 'method_not_allowed', allow: 'POST' }, 405);
  }
  const auth = request.headers.get('authorization') || '';
  const token = auth.startsWith('Bearer ') ? auth.slice(7) : '';
  if (!env.PUBLISH_PROXY_SECRET || token !== env.PUBLISH_PROXY_SECRET) {
    return json({ error: 'unauthorized' }, 401);
  }
  let payload;
  try { payload = await request.json(); }
  catch { return json({ error: 'invalid_json' }, 400); }
  const slug = (payload.slug || '').replace(/[^a-z0-9-]/gi, '');
  if (!slug) return json({ error: 'missing_slug' }, 400);
  let r2Status = 'skipped';
  if (env.NEWS_ARCHIVE) {
    try {
      const meta = { slug, publishedAt: new Date().toISOString(), source: 'worker' };
      const puts = [];
      if (payload.html) {
        puts.push(env.NEWS_ARCHIVE.put(`news/${slug}.html`, payload.html, {
          httpMetadata: { contentType: 'text/html; charset=utf-8' }, customMetadata: meta,
        }));
      }
      if (payload.markdown) {
        puts.push(env.NEWS_ARCHIVE.put(`news/${slug}.md`, payload.markdown, {
          httpMetadata: { contentType: 'text/markdown; charset=utf-8' },
        }));
      }
      if (payload.linkedin_text) {
        puts.push(env.NEWS_ARCHIVE.put(`news/${slug}.linkedin.txt`, payload.linkedin_text, {
          httpMetadata: { contentType: 'text/plain; charset=utf-8' },
        }));
      }
      await Promise.all(puts);
      r2Status = 'ok';
    } catch (err) {
      r2Status = `error: ${String(err)}`;
    }
  } else {
    r2Status = 'no_binding';
  }
  let railwayStatus = 0;
  let railwayBody = null;
  try {
    const upstream = await fetch(`${RAILWAY_BACKEND}/publish/all`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'authorization': `Bearer ${env.RAILWAY_PUBLISH_SECRET || ''}`,
        'x-publish-source': 'worker',
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(45_000),
    });
    railwayStatus = upstream.status;
    railwayBody = await upstream.text();
  } catch (err) {
    railwayStatus = 599;
    railwayBody = String(err);
  }
  const railwayOk = railwayStatus >= 200 && railwayStatus < 300;
  const truncatedBody = typeof railwayBody === 'string' && railwayBody.length > 500
    ? railwayBody.slice(0, 500) + '…'
    : railwayBody;
  return json({
    success: railwayOk || r2Status === 'ok',
    slug,
    railway: { status: railwayStatus, body: truncatedBody },
    r2: { status: r2Status },
    ts: new Date().toISOString(),
  }, railwayOk ? 200 : 502);
}

// ============================================================
// ALLOWED ORIGINS
// ============================================================
const ALLOWED_ORIGINS = [
  'https://dchub.cloud',
  'https://www.dchub.cloud',
  'https://api.dchub.cloud',
  'http://localhost:8788',
  'http://localhost:3000',
];

// ============================================================
// DISCOVERY PATHS
// ============================================================
const DISCOVERY_PATHS = [
  '/openapi.json', '/AGENTS.md', '/llms.txt', '/llms-full.txt',
  '/robots.txt', '/ai-plugin.json', '/mcp-server-card.json',
];
function isDiscoveryPath(pathname) {
  return DISCOVERY_PATHS.some(p => pathname === p || pathname.startsWith(p));
}

// ============================================================
// SOCIAL BOT DETECTION + OG META
// ============================================================
const SOCIAL_BOTS = [
  'linkedinbot', 'twitterbot', 'facebookexternalhit', 'slackbot',
  'discordbot', 'telegrambot', 'whatsapp', 'chatgpt-user',
  'gptbot', 'claudebot', 'bingbot', 'googlebot',
];
function isSocialBot(ua) {
  const lower = ua.toLowerCase();
  return SOCIAL_BOTS.some(bot => lower.includes(bot));
}

const OG_META = {
  '/': { title: 'DC Hub | Data Center Intelligence — 20,000+ Facilities', description: 'Track 20,000+ data centers across 140+ countries. Real-time capacity, AI site selection, M&A deals, and market analytics.', image: 'https://dchub.cloud/images/og-home.png' },
  '/ai': { title: 'AI Platform | DC Hub — Data Center Intelligence for Every AI Agent', description: 'Connect your AI agent to DC Hub for real-time data center intelligence across 20,000+ facilities in 140+ countries.', image: 'https://dchub.cloud/images/og-home.png' },
  '/news': { title: 'DC Industry News Digest | DC Hub', description: 'Daily data center industry intelligence — market moves, expansion deals, regulatory shifts, and community sentiment.', image: 'https://dchub.cloud/images/og-home.png' },
  '/land-power': { title: 'Land & Power Map | DC Hub', description: 'Explore 40+ infrastructure layers — substations, fiber routes, gas pipelines, and data center sites across North America.', image: 'https://dchub.cloud/images/og-land-power.png' },
  '/map': { title: 'Facility Map | DC Hub Intelligence', description: 'Interactive map of 11,000+ global data centers. Search by operator, market, capacity, and status.', image: 'https://dchub.cloud/images/og-home.png' },
  '/deals': { title: 'Data Center M&A Deals | DC Hub', description: 'Track $185B+ in data center transactions. Live deal flow, buyer/seller analysis, and market trends.', image: 'https://dchub.cloud/images/og-deals.png' },
  '/connect': { title: 'Connect to DC Hub MCP Server | AI-Native Data Center Intelligence', description: 'Add data center intelligence to Claude, ChatGPT, Cursor, and more via MCP. 20,000+ facilities, 140+ countries.', image: 'https://dchub.cloud/images/og-connect.png' },
  '/ai-wars': { title: 'AI Wars | Data Center Intelligence Benchmark', description: 'Which AI platform delivers the best data center intelligence? See the benchmark results.', image: 'https://dchub.cloud/images/og-ai-wars.png' },
  '/pricing': { title: 'Pricing | DC Hub', description: 'Free, Pro, and Enterprise plans for data center intelligence. API access, MCP integration, and custom analytics.', image: 'https://dchub.cloud/images/og-home.png' },
  '/press': { title: 'Press & Media | DC Hub', description: 'DC Hub in the news. Media coverage, press releases, and industry recognition.', image: 'https://dchub.cloud/images/og-home.png' },
  '/architecture': { title: 'Platform Architecture | DC Hub', description: 'How DC Hub aggregates intelligence from 50,000+ facilities across 140+ countries.', image: 'https://dchub.cloud/images/og-home.png' },
  '/tax-incentives': { title: 'Data Center Tax Incentives | DC Hub', description: 'Compare tax incentives across US states for data center development.', image: 'https://dchub.cloud/images/og-home.png' },
};

function getOGMetaForPath(pathname) {
  if (OG_META[pathname]) return OG_META[pathname];
  if (pathname.startsWith('/news/')) {
    const slug = pathname.replace('/news/', '').replace(/\.html$/, '');
    const datePart = slug.replace('digest-', '');
    return { title: `DC Industry News Digest — ${datePart} | DC Hub`, description: 'Daily data center industry intelligence — market moves, expansion deals, regulatory shifts, and community sentiment.', image: 'https://dchub.cloud/images/og-home.png' };
  }
  if (pathname.startsWith('/facilities/')) {
    const slug = pathname.replace('/facilities/', '');
    const name = slug.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    return { title: `${name} | DC Hub`, description: `View facility details, specs, and connectivity data for ${name} on DC Hub.`, image: 'https://dchub.cloud/images/og-home.png' };
  }
  if (pathname.startsWith('/locations/')) {
    const loc = pathname.replace('/locations/', '').replace(/-/g, ' ').toUpperCase();
    return { title: `Data Centers in ${loc} | DC Hub`, description: `Explore data centers in ${loc}. Browse facilities, compare providers, and view infrastructure data.`, image: 'https://dchub.cloud/images/og-home.png' };
  }
  return OG_META['/'];
}

function esc(str) {
  return String(str || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function buildOGHtml(meta, fullUrl) {
  return `<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>${esc(meta.title)}</title>
<meta property="og:title" content="${esc(meta.title)}">
<meta property="og:description" content="${esc(meta.description)}">
<meta property="og:image" content="${esc(meta.image)}">
<meta property="og:url" content="${esc(fullUrl)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="DC Hub">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="${esc(meta.title)}">
<meta name="twitter:description" content="${esc(meta.description)}">
<meta name="twitter:image" content="${esc(meta.image)}">
<meta name="description" content="${esc(meta.description)}">
<meta http-equiv="refresh" content="0;url=${fullUrl}">
</head>
<body><p>Redirecting to <a href="${fullUrl}">${esc(meta.title)}</a>...</p></body>
</html>`;
}

// ============================================================
// PRESS RELEASE HTML BUILDER (v4.5.4)
// ============================================================
function buildPressReleaseHtml(slug, pr) {
  const e = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const cat = e(pr.category || 'ANNOUNCEMENT').toUpperCase();
  const catColor = cat.includes('DEAL')  ? '#68d391'
                 : cat.includes('POWER') ? '#f6ad55'
                 : cat.includes('POLICY')? '#fc8181'
                 : cat.includes('LAUNCH')? '#b794f4'
                 :                          '#63b3ed';
  const dateStr = pr.date ? new Date(pr.date + 'T12:00:00Z').toLocaleDateString('en-US', { year:'numeric', month:'long', day:'numeric' }) : '';
  const bodyHtml = /<\/?[a-z][\s\S]*>/i.test(String(pr.body || ''))
    ? String(pr.body)
    : `<p>${e(pr.body || '').replace(/\n\n+/g, '</p><p>').replace(/\n/g, '<br>')}</p>`;
  const title = e(pr.title || slug);
  const sub   = e(pr.subheadline || '');
  const metaDesc = e(pr.meta_description || sub || title);

  return `<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>${title} | DC Hub</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="${metaDesc}">
<meta property="og:title" content="${title}">
<meta property="og:description" content="${metaDesc}">
<meta property="og:image" content="https://dchub.cloud/images/og-home.png">
<meta property="og:type" content="article">
<meta property="og:url" content="https://dchub.cloud/news/${e(slug)}">
<link rel="icon" href="/favicon.ico">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0e1a;color:#c9d1e0;font-family:'Inter',-apple-system,sans-serif;min-height:100vh;line-height:1.6}
nav{background:#0d1224;border-bottom:1px solid #1a2035;padding:0 24px;height:56px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.nav-logo{font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:700;color:#00d4ff;text-decoration:none}
.nav-logo span{color:#7c3aed}
.nav-links{display:flex;gap:24px;align-items:center}
.nav-links a{color:#718096;font-size:13px;text-decoration:none}
.nav-links a:hover{color:#e2e8f0}
.nav-links .btn{background:#7c3aed;color:#fff;padding:6px 14px;border-radius:6px;font-size:12px;font-weight:600}
.container{max-width:820px;margin:0 auto;padding:48px 24px}
.breadcrumb{color:#4a5568;font-size:13px;margin-bottom:32px}
.breadcrumb a{color:#63b3ed;text-decoration:none}
.pr-label{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:600;color:${catColor};letter-spacing:1px;text-transform:uppercase;margin-bottom:12px}
.pr-title{font-size:2.2rem;font-weight:700;color:#f0f4ff;margin-bottom:12px;line-height:1.2;letter-spacing:-0.5px}
.pr-sub{font-size:1.15rem;color:#a0aec0;margin-bottom:24px;line-height:1.5}
.pr-meta{display:flex;gap:16px;align-items:center;padding-bottom:32px;border-bottom:1px solid #1a2035;margin-bottom:40px;flex-wrap:wrap}
.cat-badge{background:rgba(99,179,237,0.1);color:${catColor};font-size:11px;font-weight:700;padding:4px 10px;border-radius:4px;letter-spacing:0.5px;text-transform:uppercase}
.pr-date{color:#718096;font-size:13px;font-family:'JetBrains Mono',monospace}
.pr-body{font-size:1.05rem;color:#c9d1e0;line-height:1.75}
.pr-body h1,.pr-body h2,.pr-body h3{color:#f0f4ff;margin:32px 0 16px;line-height:1.3}
.pr-body h2{font-size:1.5rem}.pr-body h3{font-size:1.25rem}
.pr-body p{margin:0 0 16px}
.pr-body a{color:#63b3ed;text-decoration:underline}
.pr-body a:hover{color:#90cdf4}
.pr-body ul,.pr-body ol{margin:0 0 16px 24px}
.pr-body li{margin-bottom:8px}
.pr-body blockquote{border-left:3px solid #7c3aed;padding:12px 20px;margin:20px 0;color:#a0aec0;background:rgba(124,58,237,0.05);border-radius:0 6px 6px 0}
.pr-body code{background:#0d1224;padding:2px 6px;border-radius:3px;font-family:'JetBrains Mono',monospace;font-size:0.9em;color:#90cdf4}
.pr-body pre{background:#0d1224;padding:16px;border-radius:6px;overflow-x:auto;margin:16px 0}
.pr-body img{max-width:100%;border-radius:8px;margin:16px 0}
.footer-nav{margin-top:48px;padding-top:32px;border-top:1px solid #1a2035;display:flex;gap:16px;flex-wrap:wrap}
.footer-nav a{color:#63b3ed;text-decoration:none;font-size:14px}
.footer-nav a:hover{color:#90cdf4}
footer{background:#0d1224;border-top:1px solid #1a2035;padding:24px;text-align:center;color:#4a5568;font-size:12px;margin-top:64px}
footer a{color:#4a5568;text-decoration:none}
@media(max-width:640px){.pr-title{font-size:1.6rem}.container{padding:24px 16px}}
</style>
</head>
<body>
<nav>
  <a href="/" class="nav-logo">DC<span>Hub</span></a>
  <div class="nav-links">
    <a href="/map">Maps</a>
    <a href="/deals">Deals</a>
    <a href="/news">News</a>
    <a href="/pricing">Pricing</a>
    <a href="/login" class="btn">Sign In</a>
  </div>
</nav>
<div class="container">
  <div class="breadcrumb"><a href="/">DC Hub</a> / <a href="/press">Press</a> / ${title}</div>
  <div class="pr-label">📰 Press Release</div>
  <h1 class="pr-title">${title}</h1>
  ${sub ? `<p class="pr-sub">${sub}</p>` : ''}
  <div class="pr-meta">
    <span class="cat-badge">${cat}</span>
    ${dateStr ? `<span class="pr-date">${e(dateStr)}</span>` : ''}
  </div>
  <div class="pr-body">${bodyHtml}</div>
  <div class="footer-nav">
    <a href="/press">← All Press Releases</a>
    <a href="/news">News Digests</a>
    <a href="/">DC Hub Home</a>
    <a href="/deals">M&amp;A Deals</a>
  </div>
</div>
<footer><p>© 2026 DC Hub. All rights reserved. · <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></p></footer>
</body></html>`;
}

// ============================================================
// NEWS DIGEST HTML BUILDER (v4.4.5)
// ============================================================
function buildDigestHtml(slug, displayDate, articles) {
  const e = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const rows = (articles || []).slice(0, 200).map(a => {
    const cat = e(a.category || 'Industry');
    const catColor = cat === 'Deals' ? '#68d391' : cat === 'Power' ? '#f6ad55' : cat === 'Policy' ? '#fc8181' : '#63b3ed';
    return `<article style="border-bottom:1px solid #1a2035;padding:24px 0">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
        <span style="background:rgba(99,179,237,0.1);color:${catColor};font-size:11px;font-weight:600;padding:3px 8px;border-radius:4px;letter-spacing:0.5px;text-transform:uppercase">${cat}</span>
        <span style="color:#4a5568;font-size:12px">${e((a.published_at || '').slice(0, 10))}</span>
        <span style="color:#2d3748;font-size:12px">·</span>
        <span style="color:#4a5568;font-size:12px">${e(a.source || '')}</span>
      </div>
      <h3 style="margin:0 0 10px;font-size:1.1rem;line-height:1.4"><a href="${e(a.url || '#')}" target="_blank" rel="noopener" style="color:#e2e8f0;text-decoration:none">${e(a.title)}</a></h3>
      <p style="margin:0;color:#718096;font-size:14px;line-height:1.6">${e((a.summary || '').slice(0, 240))}</p>
    </article>`;
  }).join('');

  const count = (articles || []).length;

  return `<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>DC Hub News Digest — ${e(displayDate)}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Daily data center industry intelligence — ${count} articles for ${e(displayDate)}">
<link rel="icon" href="/favicon.ico">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0e1a;color:#c9d1e0;font-family:-apple-system,sans-serif;min-height:100vh}
.container{max-width:900px;margin:0 auto;padding:48px 24px}
.digest-title{font-size:2rem;font-weight:700;color:#f0f4ff;margin-bottom:8px}
.digest-date{font-size:1.1rem;color:#718096;margin-bottom:32px}
</style>
</head>
<body>
<div class="container">
  <div class="digest-title">Data Center News Digest</div>
  <div class="digest-date">${e(displayDate)} · ${count} articles</div>
  ${rows || '<p style="color:#718096;text-align:center;padding:60px 0">No articles found for this date.</p>'}
</div>
</body></html>`;
}

// ============================================================
// NEWS DIGEST ROUTE (v4.4.5)
// NOTE: /press-release branch retained for v4.6.x compatibility but is now
// dead code — the v4.6.2 redirect at top of fetch() short-circuits it.
// ============================================================
async function handleNewsRoute(pathname, request, env) {
  if (pathname === '/press-release' || pathname === '/press-release/') {
    // Dead code in v4.6.2 — top-of-fetch redirect fires first.
    // Kept as a no-op fallback in case the redirect is ever removed.
    return null;
  }

  if (pathname.startsWith('/press-release/')) {
    pathname = pathname.replace(/^\/press-release/, '/news');
  }

  if (pathname === '/news' || pathname === '/news/') {
    return null;
  }

  if (pathname === '/news/archive' || pathname === '/news/archive/') {
    try {
      const apiResp = await fetch(`${RAILWAY_BACKEND}/api/press-releases/archive`,
        { headers: { 'X-Forwarded-Host': 'dchub.cloud', 'Accept': 'application/json' } });
      let dates = [];
      if (apiResp.ok) { const data = await apiResp.json(); dates = data.dates || []; }
      const today = new Date().toISOString().slice(0, 10);
      const cards = dates.length > 0 ? dates.map(d => {
        const dateObj = new Date(d.date + 'T12:00:00Z');
        const display = dateObj.toLocaleDateString('en-US', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
        const isToday = d.date === today;
        return `<a href="/news/digest-${d.date}" style="display:block;background:#0d1224;border:1px solid ${isToday ? '#7c3aed' : '#1a2035'};border-radius:10px;padding:20px 24px;text-decoration:none">
          ${isToday ? '<span style="background:#7c3aed;color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;text-transform:uppercase;margin-bottom:8px;display:inline-block">TODAY</span><br>' : ''}
          <div style="color:#e2e8f0;font-weight:600;font-size:1rem;margin-bottom:4px">${display}</div>
          <div style="color:#4a5568;font-size:13px;font-family:monospace">${d.date}</div>
          <div style="color:#63b3ed;font-size:12px;margin-top:8px">${d.count ? d.count + ' articles' : 'View digest'} →</div>
        </a>`;
      }).join('') : '<p style="color:#718096;text-align:center;padding:40px 0;grid-column:1/-1">No digests available yet.</p>';
      const html = `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>News Archive | DC Hub</title><style>body{background:#0a0e1a;color:#c9d1e0;font-family:-apple-system,sans-serif}.container{max-width:900px;margin:0 auto;padding:48px 24px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}</style></head><body><div class="container"><h1>News Archive</h1><div class="grid">${cards}</div></div></body></html>`;
      return new Response(html, { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'public, max-age=60', 'X-DC-Worker-Version': WORKER_VERSION } });
    } catch(e) {
      console.log('[archive] error:', e.message);
    }
    return new Response('<html><body style="background:#0a0e1a;color:#e2e8f0;padding:40px;font-family:sans-serif;text-align:center"><h1>Archive unavailable</h1><p><a href="/news" style="color:#63b3ed">← Back to News</a></p></body></html>', { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
  }

  if (pathname.startsWith('/news/')) {
    let slug = pathname.replace('/news/', '').replace(/\.html$/, '').replace(/\/$/, '');
    if (!slug) return null;
    try {
      const apiResp = await fetch(
        `${RAILWAY_BACKEND}/api/press-releases/${slug}`,
        { headers: { 'X-Forwarded-Host': 'dchub.cloud', 'Accept': 'application/json' } }
      );
      if (apiResp.ok) {
        const data = await apiResp.json();
        const isPressRelease = !!(data.body || data.subheadline);
        const html = isPressRelease
          ? buildPressReleaseHtml(slug, data)
          : buildDigestHtml(slug, data.display_date || slug, data.articles || []);
        return new Response(html, {
          status: 200,
          headers: {
            'Content-Type': 'text/html; charset=utf-8',
            'Cache-Control': 'public, max-age=300, stale-while-revalidate=600',
            'X-DC-Worker-Version': WORKER_VERSION,
            'X-DC-News-Slug': slug,
            'X-DC-Content-Type': isPressRelease ? 'press-release' : 'digest',
          },
        });
      }
    } catch (e) {
      console.log('[news] fetch error:', e.message);
    }
    return new Response(
      `<html><body style="background:#0f1117;color:#e0e0e6;font-family:sans-serif;padding:40px;text-align:center">
        <h1>Digest Not Found</h1>
        <p>Could not load digest for "${esc(slug)}".</p>
        <p><a href="/news" style="color:#63b3ed">← Back to latest digest</a></p>
      </body></html>`,
      { status: 404, headers: { 'Content-Type': 'text/html; charset=utf-8', 'X-DC-Worker-Version': WORKER_VERSION } }
    );
  }
  return null;
}

// ============================================================
// CORS
// ============================================================
function handleCORS(request) {
  const origin = request.headers.get('Origin') || '*';
  return new Response(null, {
    status: 204,
    headers: {
      'Access-Control-Allow-Origin': ALLOWED_ORIGINS.includes(origin) ? origin : '*',
      'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, PATCH, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, Authorization, X-API-Key, Accept, X-Requested-With, Mcp-Session-Id',
      'Access-Control-Expose-Headers': 'Mcp-Session-Id, X-Failover-Mode, X-DC-Worker-Version, X-DC-Response-Time, x-dc-hub-backend, x-dc-hub-source, x-cache-kv, x-cache-kv-age',
      'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Max-Age': '86400',
    }
  });
}

function addCORS(response, request) {
  const origin = request.headers.get('Origin') || '*';
  const resp = new Response(response.body, response);
  resp.headers.set('Access-Control-Allow-Origin', ALLOWED_ORIGINS.includes(origin) ? origin : '*');
  resp.headers.set('Access-Control-Allow-Credentials', 'true');
  resp.headers.set('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-API-Key, Accept, X-Requested-With, Mcp-Session-Id');
  resp.headers.set('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, PATCH, OPTIONS');
  resp.headers.set('Access-Control-Expose-Headers', 'Mcp-Session-Id, X-Failover-Mode, X-DC-Worker-Version, X-DC-Response-Time, x-dc-hub-backend, x-dc-hub-source, x-cache-kv, x-cache-kv-age');
  resp.headers.delete('content-encoding');
  resp.headers.delete('transfer-encoding');
  return resp;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status, headers: { 'Content-Type': 'application/json' },
  });
}

// ============================================================
// PROXY TO RAILWAY
// ============================================================
async function proxyToRailway(request, pathname, search, edgeTtl, timeoutMs) {

  const targetUrl = RAILWAY_BACKEND + pathname + (search || '');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = new Headers(request.headers);
    headers.set('X-Forwarded-Host', 'dchub.cloud');
    headers.set('X-Forwarded-Proto', 'https');
    headers.set('Referer', 'https://dchub.cloud');
    headers.set('Accept-Encoding', 'identity');
    const fetchOpts = {
      method: request.method, headers,
      body: ['GET', 'HEAD'].includes(request.method) ? null : request.body,
      signal: controller.signal, redirect: 'manual',
    };
    if (request.method === 'GET' && edgeTtl > 0) fetchOpts.cf = { cacheTtl: edgeTtl, cacheEverything: true };
    const resp = await fetch(targetUrl, fetchOpts);
    clearTimeout(timer);
    return resp;
  } catch (e) { clearTimeout(timer); return null; }
}

async function proxyWithRetry(request, pathname, search, edgeTtl, timeoutMs) {
  const resp = await proxyToRailway(request, pathname, search, edgeTtl, timeoutMs);
  if (resp && resp.status >= 500 && isRetryable(request.method, pathname)) {
    await new Promise(r => setTimeout(r, 300));
    const retry = await proxyToRailway(request, pathname, search, edgeTtl, timeoutMs);
    if (retry) return { resp: retry, attempts: 2 };
  }
  return { resp, attempts: 1 };
}

// ============================================================
// PROXY TO RENDER (read-only failover for GETs)
// Phase ZZZZZ-round26 (2026-05-23). Render runs IS_FAILOVER=true
// so non-GETs would mutate state on a stale copy — skip them.
// ============================================================
async function proxyToRender(request, pathname, search, timeoutMs) {
  if (request.method !== 'GET') return null;
  const targetUrl = RENDER_BACKEND + pathname + (search || '');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = new Headers(request.headers);
    headers.set('X-Forwarded-Host',   'dchub.cloud');
    headers.set('X-Forwarded-Proto',  'https');
    headers.set('Referer',            'https://dchub.cloud');
    headers.set('Accept-Encoding',    'identity');
    headers.set('X-Failover-Source',  'dchubapiproxy-render');
    const resp = await fetch(targetUrl, {
      method:   'GET',
      headers,
      signal:   controller.signal,
      redirect: 'manual',
    });
    clearTimeout(timer);
    return resp;
  } catch (e) {
    clearTimeout(timer);
    return null;
  }
}

// ============================================================
// .WELL-KNOWN INLINE RESPONSES
// ============================================================
function wellKnownResponse(pathname) {
  const headers = { 'Cache-Control': 'public, max-age=3600', 'Access-Control-Allow-Origin': '*' };
  // v4.9.1 NOTE: v4.9.0 added 200-with-empty-array handlers for
  // /.well-known/oauth-protected-resource and oauth-authorization-server
  // here. That REGRESSED the r33-J round 8 (2026-05-21) fix below which
  // explicitly returns 404 for the no-auth-server case — see the comment
  // at line ~1455. Empty authorization_servers tells Claude "OAuth is
  // protected but no auth servers exist" which is a stuck state; 404
  // is the spec-compliant "this is a no-auth server" signal. Both
  // approaches failed Claude.ai (the actual blocker is something else
  // we haven't identified — Anthropic support ticket open). Keeping the
  // 404 path because it's spec-compliant for no-auth MCP servers.
  if (pathname === '/.well-known/mcp-registry-auth') {
    return new Response('v=MCPv1; k=ed25519; p=8LE9YOct4SKYuIJT8JGMK6z9lhfPMbCM5pQCp5FTRBg=', { status: 200, headers: { ...headers, 'Content-Type': 'text/plain; charset=utf-8' } });
  }
  if (pathname === '/.well-known/glama.json') {
    return new Response(JSON.stringify({ "$schema": "https://glama.ai/mcp/schemas/connector.json", "maintainers": [{"email": "azmartone@gmail.com"}] }, null, 2), { status: 200, headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } });
  }
  // v4.9.2 (2026-05-24) — RFC 9116 security.txt. Pre-v4.9.2 this path
  // fell through wellKnownResponse (no handler) → request continued
  // through the worker → eventually hit CF Error 1000 "DNS points to
  // prohibited IP" because of a routing loop. Serving it inline avoids
  // the loop entirely. Expires 1 year out so we don't have to remember
  // to refresh it; per RFC 9116 the Expires field SHOULD be < 1 year.
  if (pathname === '/.well-known/security.txt') {
    const expires = new Date(Date.now() + 365 * 24 * 60 * 60 * 1000)
      .toISOString().replace(/\.\d+Z$/, 'Z');
    return new Response(
      [
        '# RFC 9116 security policy for dchub.cloud',
        `Contact: mailto:${MCP_SERVER_INFO.contact}`,
        'Contact: mailto:security@dchub.cloud',
        'Preferred-Languages: en',
        'Canonical: https://dchub.cloud/.well-known/security.txt',
        'Policy: https://dchub.cloud/terms',
        `Expires: ${expires}`,
        '',
      ].join('\n'),
      { status: 200, headers: { ...headers, 'Content-Type': 'text/plain; charset=utf-8' } }
    );
  }
  // v4.9.1 — unified discovery docs. All derive from MCP_SERVER_INFO +
  // MCP_FALLBACK_TOOLS so name/version/tool-count are honest and
  // consistent across every well-known surface.
  if (pathname === '/.well-known/ai-plugin.json') {
    return new Response(JSON.stringify({
      schema_version:        'v1',
      name_for_human:        MCP_SERVER_INFO.name,
      name_for_model:        'dchub',
      description_for_human: MCP_SERVER_INFO.description,
      description_for_model: `${MCP_SERVER_INFO.description} ${MCP_FALLBACK_TOOLS.length} MCP tools for facility search, M&A deal tracking, 7-ISO grid data, capacity pipeline, fiber routes, and site scoring.`,
      auth:                  { type: 'none' },
      api:                   { type: 'openapi', url: 'https://dchub.cloud/openapi.json' },
      logo_url:              'https://dchub.cloud/images/dc-hub-logo.png',
      contact_email:         MCP_SERVER_INFO.contact,
      legal_info_url:        'https://dchub.cloud/terms',
    }, null, 2), { status: 200, headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } });
  }
  if (pathname === '/.well-known/mcp.json') {
    const mcpTools = MCP_FALLBACK_TOOLS.map(t => ({ name: t.name, description: t.description }));
    return new Response(JSON.stringify({
      name:           MCP_SERVER_INFO.name,
      description:    MCP_SERVER_INFO.description,
      url:            MCP_SERVER_INFO.url,
      transport:      MCP_SERVER_INFO.transport,
      version:        MCP_SERVER_INFO.version,
      tools:          mcpTools,
      tools_count:    MCP_FALLBACK_TOOLS.length,
      authentication: { type: 'api_key', header: 'X-API-Key', optional_for: ['free_tier'] },
      pricing: {
        free_tier:  `10 requests/day, ${MCP_FALLBACK_TOOLS.length} tools available, 5 results per call`,
        developer:  `$49/mo — 1,000 requests/day, all ${MCP_FALLBACK_TOOLS.length} tools, full results`,
        pro:        '$199/mo — 10,000 requests/day',
        enterprise: 'Custom — 100,000 requests/day',
      },
      gated_tools:   ['get_intelligence_index', 'compare_sites', 'analyze_site', 'get_infrastructure', 'get_fiber_intel', 'get_grid_intelligence'],
      contact:       MCP_SERVER_INFO.contact,
      documentation: MCP_SERVER_INFO.documentation,
      signup_url:    MCP_SERVER_INFO.signup_url,
    }, null, 2), { status: 200, headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } });
  }
  if (pathname === '/.well-known/mcp/server-card.json') {
    const tools = MCP_FALLBACK_TOOLS.map(t => ({ name: t.name, description: t.description }));
    return new Response(JSON.stringify({
      schema_version:   'mcp-server-card/v1',
      name:             MCP_SERVER_INFO.name,
      version:          MCP_SERVER_INFO.version,
      description:      MCP_SERVER_INFO.description,
      url:              MCP_SERVER_INFO.url,
      transport:        MCP_SERVER_INFO.transport,
      protocol_version: MCP_SERVER_INFO.protocol_version,
      provider: {
        organization: MCP_SERVER_INFO.organization,
        url:          MCP_SERVER_INFO.homepage,
        contact:      MCP_SERVER_INFO.contact,
      },
      authentication: { type: 'api_key', header: 'X-API-Key', optional_for: ['free_tier'] },
      tools,
      tools_count:    MCP_FALLBACK_TOOLS.length,
      gated_tools:    ['get_intelligence_index', 'compare_sites', 'analyze_site', 'get_infrastructure', 'get_fiber_intel', 'get_grid_intelligence'],
      pricing: {
        free:       `10 calls/day, 5 results per call, ${MCP_FALLBACK_TOOLS.length} tools`,
        developer:  `$49/mo — 1,000 calls/day, all ${MCP_FALLBACK_TOOLS.length} tools, full results`,
        pro:        '$199/mo — 10,000 calls/day',
        enterprise: 'Custom — 100,000 calls/day',
      },
      documentation: MCP_SERVER_INFO.documentation,
      signup_url:    MCP_SERVER_INFO.signup_url,
    }, null, 2), { status: 200, headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } });
  }
  if (pathname === '/.well-known/agent.json') {
    return new Response(JSON.stringify({
      name:    MCP_SERVER_INFO.name,
      url:     MCP_SERVER_INFO.url,
      version: MCP_SERVER_INFO.version,
      description: MCP_SERVER_INFO.description,
      provider: {
        organization: MCP_SERVER_INFO.organization,
        url:          MCP_SERVER_INFO.homepage,
        contact:      MCP_SERVER_INFO.contact,
      },
      capabilities: {
        tools: { count: MCP_FALLBACK_TOOLS.length, listChanged: true },
      },
      protocol: MCP_SERVER_INFO.protocol_version,
      transport: MCP_SERVER_INFO.transport,
    }, null, 2), { status: 200, headers: { ...headers, 'Content-Type': 'application/json; charset=utf-8' } });
  }
  // Phase r33-J round 8 (2026-05-21) — OAuth advertisement 404.
  //
  // External Claude agent diagnosed Claude.ai's custom-connector add
  // failing on dchub.cloud/mcp. Root cause: Claude's connector does
  // proactive OAuth discovery on these two paths. Previously they
  // returned HTTP 200 with EMPTY authorization_servers — which tells
  // Claude "this server IS OAuth-protected" but then the flow can't
  // start (no real authorization_endpoint/token_endpoint), so it
  // bails with "couldn't reach".
  //
  // Our MCP server doesn't need OAuth — `initialize` returns 200
  // unauthenticated. Paid tools use X-API-Key header / ?api_key=
  // query param. A no-auth MCP server returns 404 on these paths so
  // clients correctly skip the OAuth flow.
  //
  // No-cache header so any previously edge-cached 200 from the old
  // version is invalidated immediately.
  if (pathname === '/.well-known/oauth-protected-resource' ||
      pathname === '/.well-known/oauth-authorization-server') {
    return new Response('Not Found', {
      status: 404,
      headers: { ...headers, 'Content-Type': 'text/plain; charset=utf-8',
                 'Cache-Control': 'no-store' },
    });
  }
  return null;
}

// ============================================================
// SEED ROUTES
// ============================================================
const SEED_API_ROUTES = [
  '/api/v1/search', '/api/v1/search?limit=25', '/api/v1/stats',
  '/api/v1/pipeline', '/api/v1/pipeline?limit=25', '/api/v1/deals', '/api/v1/deals?limit=25',
  '/api/v1/markets/list', '/api/ecosystem', '/api/v1/facilities?limit=25',
  '/api/v1/tax-incentives', '/api/v1/carbon', '/api/v1/climate', '/api/v1/risk',
  '/api/rankings/gas', '/api/rankings/fiber', '/api/rankings/power', '/api/rankings/construction',
  '/api/news', '/api/v1/substations?limit=25', '/api/v1/gas-pipelines?limit=25', '/api/v1/infrastructure?limit=25',
];

const MCP_SEED_MAP = [
  { api: 'kv:/api/deals', tool: 'list_transactions', args: {} },
  { api: 'kv:/api/v1/pipeline', tool: 'get_pipeline', args: {} },
  { api: 'kv:/api/v1/stats', tool: 'get_intelligence_index', args: {} },
  { api: 'kv:/api/ecosystem', tool: 'get_agent_registry', args: {} },
  { api: 'kv:/api/v1/tax-incentives', tool: 'get_tax_incentives', args: {} },
];

async function seedApiCache(kv) {
  const results = { api_seeded: 0, api_failed: 0, mcp_seeded: 0, mcp_failed: 0, routes: [] };
  for (const route of SEED_API_ROUTES) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 15000);
      const resp = await fetch(RAILWAY_BACKEND + route, {
        method: 'GET', headers: { 'X-Forwarded-Host': 'dchub.cloud', 'X-Forwarded-Proto': 'https', 'Referer': 'https://dchub.cloud' }, signal: controller.signal,
      });
      clearTimeout(timer);
      if (resp.status === 200) {
        const body = await resp.text();
        const ct = resp.headers.get('content-type') || 'application/json';
        const key = kvCacheKey('https://dchub.cloud' + route);
        const routeTier = getRouteTier(route.split('?')[0]);
        await kvCacheStore(kv, key, body, ct, routeTier.kvStaleTtl || 86400);
        results.api_seeded++;
        results.routes.push({ route, key, status: 'seeded' });
      } else { results.api_failed++; results.routes.push({ route, status: `http_${resp.status}` }); }
    } catch (e) { results.api_failed++; results.routes.push({ route, status: 'error', message: e.message || 'timeout' }); }
  }
  const done = new Set();
  const list = await kv.list({ prefix: 'kv:', limit: 200 });
  for (const m of MCP_SEED_MAP) {
    const found = list.keys.find(k => k.name === m.api || k.name.startsWith(m.api));
    if (!found) continue;
    const argsStr = JSON.stringify(Object.fromEntries(Object.entries(m.args).filter(([,v]) => v !== '' && v !== 0 && v !== null).sort()));
    const mcpKey = `mcp:tools/call:${m.tool}:${argsStr}`;
    if (done.has(mcpKey)) continue;
    try {
      const raw = await kv.get(found.name);
      if (!raw) continue;
      const entry = JSON.parse(raw);
      const mcpBody = JSON.stringify({ jsonrpc: '2.0', id: `seed-${m.tool}-${Date.now()}`, result: { content: [{ type: 'text', text: entry.body }] } });
      await kv.put(mcpKey, JSON.stringify({ body: mcpBody, ct: 'application/json', ts: Date.now() }), { expirationTtl: 86400 });
      done.add(mcpKey); results.mcp_seeded++;
    } catch (e) { results.mcp_failed++; }
  }
  try {
    const toolsResp = { jsonrpc: '2.0', id: 'seed-tools-list', result: { tools: MCP_FALLBACK_TOOLS } };
    await kv.put('mcp:tools/list', JSON.stringify({ body: JSON.stringify(toolsResp), ct: 'application/json', ts: Date.now() }), { expirationTtl: 86400 });
    results.mcp_seeded++;
  } catch (e) { results.mcp_failed++; }
  return results;
}

// ============================================================
// MAIN FETCH HANDLER
// ============================================================

// === Edge-served search explorer (Railway-independent) ===
const SEARCH_EXPLORER_HTML_B64 = "PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xIj4KPHRpdGxlPkRDIEh1YiDigJQgU2VtYW50aWMgU2VhcmNoIEV4cGxvcmVyPC90aXRsZT4KPHN0eWxlPgogIDpyb290IHsgY29sb3Itc2NoZW1lOiBkYXJrOyB9CiAgKiB7IGJveC1zaXppbmc6IGJvcmRlci1ib3g7IH0KICBib2R5IHsKICAgIG1hcmdpbjogMDsKICAgIGZvbnQtZmFtaWx5OiAtYXBwbGUtc3lzdGVtLCBCbGlua01hY1N5c3RlbUZvbnQsICdTZWdvZSBVSScsIHNhbnMtc2VyaWY7CiAgICBiYWNrZ3JvdW5kOiAjMGExMjIwOwogICAgY29sb3I6ICNlOGY4ZmY7CiAgICBwYWRkaW5nOiAyNHB4OwogICAgbGluZS1oZWlnaHQ6IDEuNTsKICB9CiAgLndyYXAgeyBtYXgtd2lkdGg6IDExMDBweDsgbWFyZ2luOiAwIGF1dG87IH0KICBoMSB7IGNvbG9yOiAjMDBkNGFhOyBtYXJnaW46IDAgMCA4cHg7IGZvbnQtc2l6ZTogMjRweDsgfQogIHAubGVhZCB7IGNvbG9yOiAjOTRhM2I4OyBtYXJnaW46IDAgMCAyNHB4OyB9CiAgLmNhcmQgewogICAgYmFja2dyb3VuZDogIzE0MWIyZDsKICAgIGJvcmRlcjogMXB4IHNvbGlkICMxZTI5M2I7CiAgICBib3JkZXItcmFkaXVzOiAxMHB4OwogICAgcGFkZGluZzogMTZweDsKICAgIG1hcmdpbi1ib3R0b206IDE2cHg7CiAgfQogIC5mb3JtIHsgZGlzcGxheTogZ3JpZDsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiAxZnIgMWZyOyBnYXA6IDEycHggMTZweDsgfQogIC5mb3JtIGxhYmVsIHsgZGlzcGxheTogYmxvY2s7IGZvbnQtc2l6ZTogMTJweDsgY29sb3I6ICM5NGEzYjg7IG1hcmdpbi1ib3R0b206IDRweDsgfQogIC5mb3JtIGlucHV0LCAuZm9ybSBzZWxlY3QgewogICAgd2lkdGg6IDEwMCU7CiAgICBiYWNrZ3JvdW5kOiAjMGExMjIwOwogICAgY29sb3I6ICNlOGY4ZmY7CiAgICBib3JkZXI6IDFweCBzb2xpZCAjMzM0MTU1OwogICAgYm9yZGVyLXJhZGl1czogNnB4OwogICAgcGFkZGluZzogOHB4IDEwcHg7CiAgICBmb250LXNpemU6IDE0cHg7CiAgICBmb250LWZhbWlseTogaW5oZXJpdDsKICB9CiAgLmZvcm0gLmZ1bGwgeyBncmlkLWNvbHVtbjogMSAvIC0xOyB9CiAgLmZvcm0gLmNoZWNrcyB7IGdyaWQtY29sdW1uOiAxIC8gLTE7IGRpc3BsYXk6IGZsZXg7IGdhcDogMTZweDsgZmxleC13cmFwOiB3cmFwOyBmb250LXNpemU6IDEzcHg7IH0KICAuZm9ybSAuY2hlY2tzIGxhYmVsIHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiA2cHg7IGNvbG9yOiAjZThmOGZmOyBtYXJnaW46IDA7IH0KICBidXR0b24gewogICAgYmFja2dyb3VuZDogIzAwZDRhYTsKICAgIGNvbG9yOiAjMGExMjIwOwogICAgYm9yZGVyOiAwOwogICAgcGFkZGluZzogMTBweCAxNnB4OwogICAgYm9yZGVyLXJhZGl1czogNnB4OwogICAgY3Vyc29yOiBwb2ludGVyOwogICAgZm9udC13ZWlnaHQ6IDYwMDsKICAgIGZvbnQtc2l6ZTogMTRweDsKICB9CiAgYnV0dG9uOmhvdmVyIHsgYmFja2dyb3VuZDogIzAwZjBjMDsgfQogIGJ1dHRvbi5zZWNvbmRhcnkgeyBiYWNrZ3JvdW5kOiB0cmFuc3BhcmVudDsgY29sb3I6ICM5NGEzYjg7IGJvcmRlcjogMXB4IHNvbGlkICMzMzQxNTU7IH0KICAucnVudGltZS10YWcgewogICAgZGlzcGxheTogaW5saW5lLWJsb2NrOwogICAgZm9udC1zaXplOiAxMXB4OwogICAgcGFkZGluZzogMnB4IDhweDsKICAgIGJvcmRlci1yYWRpdXM6IDNweDsKICAgIG1hcmdpbi1sZWZ0OiA4cHg7CiAgICB0ZXh0LXRyYW5zZm9ybTogdXBwZXJjYXNlOwogICAgbGV0dGVyLXNwYWNpbmc6IDAuNXB4OwogIH0KICAucnVudGltZS1lZGdlIHsgYmFja2dyb3VuZDogIzAwZDRhYTsgY29sb3I6ICMwYTEyMjA7IH0KICAucnVudGltZS1mbGFzayB7IGJhY2tncm91bmQ6ICM0NzU1Njk7IGNvbG9yOiAjZThmOGZmOyB9CiAgLm1ldGEgeyBmb250LXNpemU6IDEycHg7IGNvbG9yOiAjNjQ3NDhiOyBtYXJnaW4tYm90dG9tOiAxMnB4OyB9CiAgLm1hdGNoIHsKICAgIGJhY2tncm91bmQ6ICMxYTIyMzU7CiAgICBwYWRkaW5nOiAxMnB4OwogICAgYm9yZGVyLXJhZGl1czogNnB4OwogICAgbWFyZ2luLWJvdHRvbTogOHB4OwogICAgYm9yZGVyLWxlZnQ6IDNweCBzb2xpZCAjMDBkNGFhOwogIH0KICAubWF0Y2ggaDMgeyBtYXJnaW46IDAgMCA0cHg7IGZvbnQtc2l6ZTogMTVweDsgY29sb3I6ICMwMGQ0YWE7IH0KICAubWF0Y2ggLnNjb3JlIHsgZmxvYXQ6IHJpZ2h0OyBmb250LXNpemU6IDEycHg7IGNvbG9yOiAjOTRhM2I4OyB9CiAgLm1hdGNoIC5yb3cgeyBmb250LXNpemU6IDEzcHg7IGNvbG9yOiAjY2JkNWUxOyB9CiAgLm1hdGNoIC5iYWRnZXMgeyBtYXJnaW4tdG9wOiA2cHg7IGRpc3BsYXk6IGZsZXg7IGdhcDogNnB4OyBmbGV4LXdyYXA6IHdyYXA7IGZvbnQtc2l6ZTogMTFweDsgfQogIC5iYWRnZSB7CiAgICBiYWNrZ3JvdW5kOiAjMGExMjIwOwogICAgcGFkZGluZzogMnB4IDhweDsKICAgIGJvcmRlci1yYWRpdXM6IDNweDsKICAgIGJvcmRlcjogMXB4IHNvbGlkICMzMzQxNTU7CiAgfQogIHByZSB7CiAgICBiYWNrZ3JvdW5kOiAjMGExMjIwOwogICAgYm9yZGVyOiAxcHggc29saWQgIzFlMjkzYjsKICAgIHBhZGRpbmc6IDEycHg7CiAgICBib3JkZXItcmFkaXVzOiA2cHg7CiAgICBvdmVyZmxvdy14OiBhdXRvOwogICAgZm9udC1zaXplOiAxMnB4OwogICAgbWF4LWhlaWdodDogNDAwcHg7CiAgfQogIC5lbmRwb2ludC10b2dnbGUgewogICAgZGlzcGxheTogZmxleDsKICAgIGdhcDogNHB4OwogICAgbWFyZ2luLWJvdHRvbTogMTJweDsKICB9CiAgLmVuZHBvaW50LXRvZ2dsZSBidXR0b24gewogICAgZmxleDogMTsKICAgIGJhY2tncm91bmQ6IHRyYW5zcGFyZW50OwogICAgY29sb3I6ICM5NGEzYjg7CiAgICBib3JkZXI6IDFweCBzb2xpZCAjMzM0MTU1OwogICAgcGFkZGluZzogNnB4IDEycHg7CiAgICBmb250LXdlaWdodDogNDAwOwogIH0KICAuZW5kcG9pbnQtdG9nZ2xlIGJ1dHRvbi5hY3RpdmUgewogICAgYmFja2dyb3VuZDogIzAwZDRhYTsKICAgIGNvbG9yOiAjMGExMjIwOwogICAgZm9udC13ZWlnaHQ6IDYwMDsKICAgIGJvcmRlci1jb2xvcjogIzAwZDRhYTsKICB9Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+CjxkaXYgY2xhc3M9IndyYXAiPgogIDxoMT5TZW1hbnRpYyBTZWFyY2ggRXhwbG9yZXIgPHNwYW4gY2xhc3M9InJ1bnRpbWUtdGFnIiBpZD0icnQtdGFnIj5lZGdlPC9zcGFuPjwvaDE+CiAgPHAgY2xhc3M9ImxlYWQiPlF1ZXJ5IDIxLDMxOSBkYXRhIGNlbnRlciBmYWNpbGl0aWVzIGJ5IG5hdHVyYWwtbGFuZ3VhZ2Ugc2ltaWxhcml0eS4gQmFja2VkIGJ5IENsb3VkZmxhcmUgVmVjdG9yaXplIG92ZXIgQkdFLWJhc2UtZW4tdjEuNSBlbWJlZGRpbmdzLjwvcD4KCiAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICA8ZGl2IGNsYXNzPSJlbmRwb2ludC10b2dnbGUiPgogICAgICA8YnV0dG9uIGlkPSJidG4tZWRnZSIgY2xhc3M9ImFjdGl2ZSIgZGF0YS1lbmRwb2ludD0iZWRnZSI+RWRnZSAoQ2xvdWRmbGFyZSkg4oCUIGZhc3Rlc3Q8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBpZD0iYnRuLWZsYXNrIiBkYXRhLWVuZHBvaW50PSJmbGFzayI+Rmxhc2sgKFJhaWx3YXkpIOKAlCBzdXBwb3J0cyBoeWRyYXRlPC9idXR0b24+CiAgICA8L2Rpdj4KCiAgICA8ZGl2IGNsYXNzPSJmb3JtIj4KICAgICAgPGRpdiBjbGFzcz0iZnVsbCI+CiAgICAgICAgPGxhYmVsPlF1ZXJ5PC9sYWJlbD4KICAgICAgICA8aW5wdXQgdHlwZT0idGV4dCIgaWQ9InEiIHZhbHVlPSJoeXBlcnNjYWxlIGZhY2lsaXR5IHdpdGggUEpNIGdyaWQgYWNjZXNzIiBwbGFjZWhvbGRlcj0iZS5nLiAzMCBNVyB3aXRoIGxvdyB3YXRlciByaXNrIGluIE1JU08iIC8+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2PgogICAgICAgIDxsYWJlbD5HcmlkIChJU08vUlRPKTwvbGFiZWw+CiAgICAgICAgPHNlbGVjdCBpZD0iZ3JpZCI+CiAgICAgICAgICA8b3B0aW9uIHZhbHVlPSIiPkFueTwvb3B0aW9uPgogICAgICAgICAgPG9wdGlvbiB2YWx1ZT0iUEpNIj5QSk08L29wdGlvbj4KICAgICAgICAgIDxvcHRpb24gdmFsdWU9IkVSQ09UIj5FUkNPVDwvb3B0aW9uPgogICAgICAgICAgPG9wdGlvbiB2YWx1ZT0iQ0FJU08iPkNBSVNPPC9vcHRpb24+CiAgICAgICAgICA8b3B0aW9uIHZhbHVlPSJNSVNPIj5NSVNPPC9vcHRpb24+CiAgICAgICAgICA8b3B0aW9uIHZhbHVlPSJTUFAiPlNQUDwvb3B0aW9uPgogICAgICAgICAgPG9wdGlvbiB2YWx1ZT0iU09DTyI+U09DTzwvb3B0aW9uPgogICAgICAgICAgPG9wdGlvbiB2YWx1ZT0iTllJU08iPk5ZSVNPPC9vcHRpb24+CiAgICAgICAgICA8b3B0aW9uIHZhbHVlPSJJU08tTkUiPklTTy1ORTwvb3B0aW9uPgogICAgICAgICAgPG9wdGlvbiB2YWx1ZT0iTldQUCI+TldQUDwvb3B0aW9uPgogICAgICAgIDwvc2VsZWN0PgogICAgICA8L2Rpdj4KICAgICAgPGRpdj4KICAgICAgICA8bGFiZWw+U3RhdGVzIChDU1YpPC9sYWJlbD4KICAgICAgICA8aW5wdXQgdHlwZT0idGV4dCIgaWQ9InN0YXRlcyIgcGxhY2Vob2xkZXI9IlZBLFBBLE5KIiAvPgogICAgICA8L2Rpdj4KICAgICAgPGRpdj4KICAgICAgICA8bGFiZWw+TWluIE1XPC9sYWJlbD4KICAgICAgICA8aW5wdXQgdHlwZT0ibnVtYmVyIiBpZD0ibWluX213IiBtaW49IjAiIHBsYWNlaG9sZGVyPSIzMCIgLz4KICAgICAgPC9kaXY+CiAgICAgIDxkaXY+CiAgICAgICAgPGxhYmVsPk1heCBNVzwvbGFiZWw+CiAgICAgICAgPGlucHV0IHR5cGU9Im51bWJlciIgaWQ9Im1heF9tdyIgbWluPSIwIiBwbGFjZWhvbGRlcj0iIiAvPgogICAgICA8L2Rpdj4KICAgICAgPGRpdj4KICAgICAgICA8bGFiZWw+UHJvdmlkZXIgKHN1YnN0cmluZyk8L2xhYmVsPgogICAgICAgIDxpbnB1dCB0eXBlPSJ0ZXh0IiBpZD0icHJvdmlkZXIiIHBsYWNlaG9sZGVyPSJFcXVpbml4IiAvPgogICAgICA8L2Rpdj4KICAgICAgPGRpdj4KICAgICAgICA8bGFiZWw+dG9wSzwvbGFiZWw+CiAgICAgICAgPGlucHV0IHR5cGU9Im51bWJlciIgaWQ9InRvcEsiIG1pbj0iMSIgbWF4PSI1MCIgdmFsdWU9IjUiIC8+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJjaGVja3MiPgogICAgICAgIDxsYWJlbD48aW5wdXQgdHlwZT0iY2hlY2tib3giIGlkPSJoeWRyYXRlIiAvPiBIeWRyYXRlIChGbGFzayBvbmx5KTwvbGFiZWw+CiAgICAgICAgPGxhYmVsPjxpbnB1dCB0eXBlPSJjaGVja2JveCIgaWQ9InJlcmFuayIgLz4gUmVyYW5rIChzY29yZSB4IGxvZyBNVyB4IHN0YXR1cyk8L2xhYmVsPgogICAgICA8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iZnVsbCI+CiAgICAgICAgPGJ1dHRvbiBpZD0iZ28iPlNlYXJjaDwvYnV0dG9uPgogICAgICAgIDxidXR0b24gY2xhc3M9InNlY29uZGFyeSIgaWQ9ImNvcHktY3VybCI+Q29weSBjdXJsPC9idXR0b24+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDxkaXYgaWQ9InN0YXR1cyIgY2xhc3M9Im1ldGEiPjwvZGl2PgogIDxkaXYgaWQ9InJlc3VsdHMiIGNsYXNzPSJjYXJkIiBzdHlsZT0iZGlzcGxheTpub25lIj4KICAgIDxoMyBzdHlsZT0ibWFyZ2luLXRvcDowIj5NYXRjaGVzPC9oMz4KICAgIDxkaXYgaWQ9Im1hdGNoZXMiPjwvZGl2PgogIDwvZGl2PgogIDxkaXYgaWQ9InJhdy1jYXJkIiBjbGFzcz0iY2FyZCIgc3R5bGU9ImRpc3BsYXk6bm9uZSI+CiAgICA8aDMgc3R5bGU9Im1hcmdpbi10b3A6MCI+UmF3IHJlc3BvbnNlPC9oMz4KICAgIDxwcmUgaWQ9InJhdyI+PC9wcmU+CiAgPC9kaXY+CjwvZGl2PgoKPHNjcmlwdD4KbGV0IGFjdGl2ZUVuZHBvaW50ID0gJ2VkZ2UnOwpjb25zdCB0YWcgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncnQtdGFnJyk7Cgpkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuZW5kcG9pbnQtdG9nZ2xlIGJ1dHRvbicpLmZvckVhY2goYiA9PiB7CiAgYi5hZGRFdmVudExpc3RlbmVyKCdjbGljaycsICgpID0+IHsKICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5lbmRwb2ludC10b2dnbGUgYnV0dG9uJykuZm9yRWFjaCh4ID0+IHguY2xhc3NMaXN0LnJlbW92ZSgnYWN0aXZlJykpOwogICAgYi5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICAgIGFjdGl2ZUVuZHBvaW50ID0gYi5kYXRhc2V0LmVuZHBvaW50OwogICAgdGFnLnRleHRDb250ZW50ID0gYWN0aXZlRW5kcG9pbnQgPT09ICdlZGdlJyA/ICdlZGdlJyA6ICdmbGFzayc7CiAgICB0YWcuY2xhc3NOYW1lID0gJ3J1bnRpbWUtdGFnICcgKyAoYWN0aXZlRW5kcG9pbnQgPT09ICdlZGdlJyA/ICdydW50aW1lLWVkZ2UnIDogJ3J1bnRpbWUtZmxhc2snKTsKICB9KTsKfSk7CgpmdW5jdGlvbiBidWlsZFVybCgpIHsKICBjb25zdCBwYXJhbXMgPSBuZXcgVVJMU2VhcmNoUGFyYW1zKCk7CiAgY29uc3QgcSA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdxJykudmFsdWUudHJpbSgpOwogIGlmICghcSkgcmV0dXJuIG51bGw7CiAgcGFyYW1zLnNldCgncScsIHEpOwogIHBhcmFtcy5zZXQoJ3RvcEsnLCBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndG9wSycpLnZhbHVlIHx8ICc1Jyk7CiAgZm9yIChjb25zdCBrIG9mIFsnZ3JpZCcsJ3N0YXRlcycsJ3Byb3ZpZGVyJ10pIHsKICAgIGNvbnN0IHYgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChrKS52YWx1ZS50cmltKCk7CiAgICBpZiAodikgcGFyYW1zLnNldChrLCB2KTsKICB9CiAgZm9yIChjb25zdCBrIG9mIFsnbWluX213JywnbWF4X213J10pIHsKICAgIGNvbnN0IHYgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChrKS52YWx1ZTsKICAgIGlmICh2KSBwYXJhbXMuc2V0KGssIHYpOwogIH0KICBpZiAoZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2h5ZHJhdGUnKS5jaGVja2VkICYmIGFjdGl2ZUVuZHBvaW50ID09PSAnZmxhc2snKSB7CiAgICBwYXJhbXMuc2V0KCdoeWRyYXRlJywgJ3RydWUnKTsKICB9CiAgaWYgKGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdyZXJhbmsnKS5jaGVja2VkKSB7CiAgICBwYXJhbXMuc2V0KCdyZXJhbmsnLCAndHJ1ZScpOwogIH0KICBjb25zdCBwYXRoID0gYWN0aXZlRW5kcG9pbnQgPT09ICdlZGdlJwogICAgPyAnL2FwaS92MS9zZWFyY2gvZWRnZScKICAgIDogJy9hcGkvdjEvc2VhcmNoL3NlbWFudGljJzsKICByZXR1cm4gcGF0aCArICc/JyArIHBhcmFtcy50b1N0cmluZygpOwp9Cgpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnZ28nKS5hZGRFdmVudExpc3RlbmVyKCdjbGljaycsIGFzeW5jICgpID0+IHsKICBjb25zdCB1cmwgPSBidWlsZFVybCgpOwogIGlmICghdXJsKSB7IGFsZXJ0KCdFbnRlciBhIHF1ZXJ5Jyk7IHJldHVybjsgfQogIGNvbnN0IHN0YXR1cyA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdGF0dXMnKTsKICBjb25zdCByZXN1bHRzID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3Jlc3VsdHMnKTsKICBjb25zdCBtYXRjaGVzRWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbWF0Y2hlcycpOwogIGNvbnN0IHJhdyA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdyYXcnKTsKICBjb25zdCByYXdDYXJkID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3Jhdy1jYXJkJyk7CgogIHN0YXR1cy50ZXh0Q29udGVudCA9ICdTZWFyY2hpbmcuLi4nOwogIHJlc3VsdHMuc3R5bGUuZGlzcGxheSA9ICdub25lJzsKICByYXdDYXJkLnN0eWxlLmRpc3BsYXkgPSAnbm9uZSc7CgogIGNvbnN0IHQwID0gcGVyZm9ybWFuY2Uubm93KCk7CiAgdHJ5IHsKICAgIGNvbnN0IHJlc3AgPSBhd2FpdCBmZXRjaCh1cmwpOwogICAgY29uc3QgZGF0YSA9IGF3YWl0IHJlc3AuanNvbigpOwogICAgY29uc3QgZWxhcHNlZCA9IE1hdGgucm91bmQocGVyZm9ybWFuY2Uubm93KCkgLSB0MCk7CgogICAgaWYgKCFkYXRhLm1hdGNoZXMpIHsKICAgICAgc3RhdHVzLnRleHRDb250ZW50ID0gJ0Vycm9yOiAnICsgKGRhdGEuZXJyb3IgfHwgcmVzcC5zdGF0dXMpOwogICAgICByYXcudGV4dENvbnRlbnQgPSBKU09OLnN0cmluZ2lmeShkYXRhLCBudWxsLCAyKTsKICAgICAgcmF3Q2FyZC5zdHlsZS5kaXNwbGF5ID0gJ2Jsb2NrJzsKICAgICAgcmV0dXJuOwogICAgfQoKICAgIGNvbnN0IHRtID0gZGF0YS50aW1pbmdfbXMgfHwge307CiAgICBjb25zdCBmcyA9IGRhdGEuZmlsdGVyX3N0YXRzIHx8IHt9OwogICAgc3RhdHVzLmlubmVySFRNTCA9CiAgICAgICdSZXR1cm5lZCA8Yj4nICsgZGF0YS5tYXRjaGVzLmxlbmd0aCArICc8L2I+IG9mIDxiPicgKyAoZnMuZmV0Y2hlZCB8fCBkYXRhLm1hdGNoZXMubGVuZ3RoKSArCiAgICAgICc8L2I+IGZldGNoZWQgKDxiPicgKyAoZnMubWF0Y2hlZF9maWx0ZXJzID8/IGRhdGEubWF0Y2hlcy5sZW5ndGgpICsgJzwvYj4gbWF0Y2hlZCBmaWx0ZXJzKScgKwogICAgICAnICZtaWRkb3Q7IHJ1bnRpbWU6IDxiPicgKyAoZGF0YS5ydW50aW1lIHx8IGFjdGl2ZUVuZHBvaW50KSArICc8L2I+JyArCiAgICAgICcgJm1pZGRvdDsgdG90YWw6IDxiPicgKyAodG0udG90YWwgPz8gZWxhcHNlZCkgKyAnbXM8L2I+JyArCiAgICAgICh0bS5lbWJlZCAhPSBudWxsID8gJyAoZW1iZWQgJyArIHRtLmVtYmVkICsgJ21zLCBxdWVyeSAnICsgdG0ucXVlcnkgKyAnbXMpJyA6ICcnKTsKCiAgICBtYXRjaGVzRWwuaW5uZXJIVE1MID0gZGF0YS5tYXRjaGVzLm1hcChtID0+IHsKICAgICAgY29uc3Qgc2NvcmUgPSBtLnNjb3JlICE9IG51bGwgPyBtLnNjb3JlLnRvRml4ZWQoMykgOiAnPyc7CiAgICAgIGNvbnN0IGNvbXBvc2l0ZSA9IG0uY29tcG9zaXRlX3Njb3JlICE9IG51bGwgPyAnICZtaWRkb3Q7IHJlcmFuazogJyArIG0uY29tcG9zaXRlX3Njb3JlLnRvRml4ZWQoMikgOiAnJzsKICAgICAgY29uc3QgcHJvdmlkZXIgPSBtLnByb3ZpZGVyID8gKCc8c3BhbiBjbGFzcz0iYmFkZ2UiPicgKyBtLnByb3ZpZGVyICsgJzwvc3Bhbj4nKSA6ICcnOwogICAgICBjb25zdCBzdGF0ZSA9IG0uc3RhdGUgPyAoJzxzcGFuIGNsYXNzPSJiYWRnZSI+JyArIG0uc3RhdGUgKyAnPC9zcGFuPicpIDogJyc7CiAgICAgIGNvbnN0IGNvdW50cnkgPSBtLmNvdW50cnkgPyAoJzxzcGFuIGNsYXNzPSJiYWRnZSI+JyArIG0uY291bnRyeSArICc8L3NwYW4+JykgOiAnJzsKICAgICAgY29uc3Qgc3RhdHVzX2IgPSBtLnN0YXR1cyA/ICgnPHNwYW4gY2xhc3M9ImJhZGdlIj4nICsgbS5zdGF0dXMgKyAnPC9zcGFuPicpIDogJyc7CiAgICAgIGNvbnN0IG13ID0gbS5wb3dlcl9tdyA/ICgnPHNwYW4gY2xhc3M9ImJhZGdlIj4nICsgbS5wb3dlcl9tdyArICcgTVc8L3NwYW4+JykgOiAnJzsKICAgICAgY29uc3QgaHlkcmF0ZWQgPSBtLmh5ZHJhdGVkICYmIG0uaHlkcmF0ZWQuc291cmNlX3VybAogICAgICAgID8gJzxhIGhyZWY9IicgKyBtLmh5ZHJhdGVkLnNvdXJjZV91cmwgKyAnIiB0YXJnZXQ9Il9ibGFuayIgc3R5bGU9ImNvbG9yOiMwMGQ0YWE7Zm9udC1zaXplOjEycHgiPnNvdXJjZSBsaW5rPC9hPicKICAgICAgICA6ICcnOwogICAgICByZXR1cm4gWwogICAgICAgICc8ZGl2IGNsYXNzPSJtYXRjaCI+JywKICAgICAgICAnICA8c3BhbiBjbGFzcz0ic2NvcmUiPnNjb3JlICcgKyBzY29yZSArIGNvbXBvc2l0ZSArICc8L3NwYW4+JywKICAgICAgICAnICA8aDM+JyArIChtLm5hbWUgfHwgJyh1bm5hbWVkKScpICsgJzwvaDM+JywKICAgICAgICAnICA8ZGl2IGNsYXNzPSJyb3ciPicgKyAobS5jaXR5IHx8ICcnKSArIChtLmNpdHkgJiYgbS5zdGF0ZSA/ICcsICcgOiAnJykgKyAobS5zdGF0ZSB8fCAnJykgKyAnICZtaWRkb3Q7ICcgKyBoeWRyYXRlZCArICc8L2Rpdj4nLAogICAgICAgICcgIDxkaXYgY2xhc3M9ImJhZGdlcyI+JyArIHByb3ZpZGVyICsgc3RhdGUgKyBjb3VudHJ5ICsgc3RhdHVzX2IgKyBtdyArICc8L2Rpdj4nLAogICAgICAgICc8L2Rpdj4nCiAgICAgIF0uam9pbignJyk7CiAgICB9KS5qb2luKCcnKTsKICAgIHJlc3VsdHMuc3R5bGUuZGlzcGxheSA9ICdibG9jayc7CgogICAgcmF3LnRleHRDb250ZW50ID0gSlNPTi5zdHJpbmdpZnkoZGF0YSwgbnVsbCwgMik7CiAgICByYXdDYXJkLnN0eWxlLmRpc3BsYXkgPSAnYmxvY2snOwogIH0gY2F0Y2ggKGUpIHsKICAgIHN0YXR1cy50ZXh0Q29udGVudCA9ICdOZXR3b3JrIGVycm9yOiAnICsgZS5tZXNzYWdlOwogIH0KfSk7Cgpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY29weS1jdXJsJykuYWRkRXZlbnRMaXN0ZW5lcignY2xpY2snLCAoKSA9PiB7CiAgY29uc3QgdXJsID0gYnVpbGRVcmwoKTsKICBpZiAoIXVybCkgcmV0dXJuOwogIGNvbnN0IGNtZCA9ICJjdXJsIC1zUyAnaHR0cHM6Ly9kY2h1Yi5jbG91ZCIgKyB1cmwgKyAiJyB8IHB5dGhvbjMgLW0ganNvbi50b29sIjsKICBuYXZpZ2F0b3IuY2xpcGJvYXJkLndyaXRlVGV4dChjbWQpOwogIGNvbnN0IGJ0biA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjb3B5LWN1cmwnKTsKICBjb25zdCBvbGQgPSBidG4udGV4dENvbnRlbnQ7CiAgYnRuLnRleHRDb250ZW50ID0gJ0NvcGllZCEnOwogIHNldFRpbWVvdXQoKCkgPT4geyBidG4udGV4dENvbnRlbnQgPSBvbGQ7IH0sIDE1MDApOwp9KTsKPC9zY3JpcHQ+CjwvYm9keT4KPC9odG1sPgo=";
function _serveSearchExplorer() {
  return new Response(atob(SEARCH_EXPLORER_HTML_B64), {
    status: 200,
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'public, max-age=300',
      'Access-Control-Allow-Origin': '*',
    },
  });
}
// === end edge-served explorer ===

// === DC Hub iteration 5: edge fast-path semantic search ===
// Adds /api/v1/search/edge with iteration 4 filter parity served entirely
// at the Cloudflare edge via env.AI + env.VECTORIZE bindings (no Railway
// round-trip). Sub-50ms target.

const IT5_GRID_TERRITORIES = {
  PJM:      new Set(['PA','NJ','MD','DC','DE','OH','KY','NC','TN','IL','IN','MI','VA','WV']),
  ERCOT:    new Set(['TX']),
  CAISO:    new Set(['CA']),
  SPP:      new Set(['KS','OK','NE','ND','SD','AR','LA']),
  MISO:     new Set(['IL','IN','IA','MI','MN','MO','MS','MT','ND','SD','WI','AR','KY','LA']),
  SOCO:     new Set(['GA','AL','MS','FL']),
  NYISO:    new Set(['NY']),
  'ISO-NE': new Set(['CT','MA','ME','NH','RI','VT']),
  NWPP:     new Set(['WA','OR','ID','MT','UT','WY']),
  AESO:     new Set(),
};

function it5JsonResp(obj, status) {
  return new Response(JSON.stringify(obj, null, 2), {
    status: status || 200,
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
    },
  });
}

function it5ApplyFilters(matches, filters) {
  if (!filters || Object.keys(filters).length === 0) return matches;
  const grid = filters.grid ? filters.grid.toUpperCase() : null;
  const gridStates = grid ? IT5_GRID_TERRITORIES[grid] : null;
  const explicitStates = filters.states
    ? new Set(filters.states.split(',').map(s => s.trim().toUpperCase()).filter(Boolean))
    : null;
  const providerQ = (filters.provider || '').toLowerCase();
  const countryQ  = (filters.country  || '').toUpperCase();
  const statusQ   = (filters.status   || '').toLowerCase();
  return matches.filter(m => {
    const md = m.metadata || {};
    const st = (md.state    || '').toUpperCase();
    const co = (md.country  || '').toUpperCase();
    const pr = (md.provider || '').toLowerCase();
    const ss = (md.status   || '').toLowerCase();
    const mw = md.power_mw || 0;
    if (gridStates && !gridStates.has(st)) return false;
    if (explicitStates && !explicitStates.has(st)) return false;
    if (providerQ && !pr.includes(providerQ)) return false;
    if (countryQ && co !== countryQ) return false;
    if (statusQ && !ss.includes(statusQ)) return false;
    if (filters.min_mw != null && mw < filters.min_mw) return false;
    if (filters.max_mw != null && mw > filters.max_mw) return false;
    return true;
  });
}

async function it5HandleEdgeSearch(request, env) {
  const t0 = Date.now();
  const url = new URL(request.url);
  const q = (url.searchParams.get('q') || '').trim();
  if (!q) return it5JsonResp({ error: 'q parameter required' }, 400);

  if (!env.AI || !env.VECTORIZE) {
    return it5JsonResp({
      error: 'feature_unavailable',
      message: 'AI or VECTORIZE binding missing. Redeploy worker with both bindings attached.',
    }, 503);
  }

  const topK = Math.max(1, Math.min(parseInt(url.searchParams.get('topK') || '5', 10) || 5, 50));

  const filters = {};
  for (const k of ['grid','states','provider','country','status']) {
    const v = (url.searchParams.get(k) || '').trim();
    if (v) filters[k] = v;
  }
  for (const k of ['min_mw','max_mw']) {
    const raw = url.searchParams.get(k);
    if (raw != null && raw !== '') {
      const n = parseFloat(raw);
      if (!isNaN(n)) filters[k] = n;
    }
  }
  if (filters.grid && !IT5_GRID_TERRITORIES[filters.grid.toUpperCase()]) {
    return it5JsonResp({
      error: 'unknown-grid',
      grid: filters.grid,
      available: Object.keys(IT5_GRID_TERRITORIES).sort(),
    }, 400);
  }

  const tEmbed = Date.now();
  const emb = await env.AI.run('@cf/baai/bge-base-en-v1.5', { text: [q] });
  const vector = emb && emb.data && emb.data[0];
  if (!vector) {
    return it5JsonResp({ error: 'embed-failed', detail: emb }, 502);
  }
  const embedMs = Date.now() - tEmbed;

  const filterCount = Object.keys(filters).length;
  const fetchK = filterCount > 0 ? Math.min(topK * 5, 50) : topK;

  const tQuery = Date.now();
  const qres = await env.VECTORIZE.query(vector, { topK: fetchK, returnMetadata: 'all' });
  const queryMs = Date.now() - tQuery;

  let matches = qres.matches || [];
  const preFilter = matches.length;
  if (filterCount > 0) matches = it5ApplyFilters(matches, filters);
  const postFilter = matches.length;
  matches = matches.slice(0, topK);

  const flat = matches.map(m => ({
    id: m.id,
    score: m.score,
    name:     (m.metadata || {}).name,
    provider: (m.metadata || {}).provider,
    city:     (m.metadata || {}).city,
    state:    (m.metadata || {}).state,
    country:  (m.metadata || {}).country,
    lat:      (m.metadata || {}).lat,
    lng:      (m.metadata || {}).lng,
    power_mw: (m.metadata || {}).power_mw,
    status:   (m.metadata || {}).status,
  }));

  return it5JsonResp({
    query: q,
    topK: topK,
    count: flat.length,
    runtime: 'cloudflare-edge',
    matches: flat,
    filters: filterCount ? filters : null,
    filter_stats: {
      fetched: preFilter,
      matched_filters: postFilter,
      returned: flat.length,
    },
    timing_ms: {
      embed: embedMs,
      query: queryMs,
      total: Date.now() - t0,
    },
    index: 'dchub-facilities',
    model: '@cf/baai/bge-base-en-v1.5',
    note: 'Hydration not available on edge runtime; use Flask /api/v1/search/semantic?hydrate=true for full Neon row.',
  }, 200);
}

function it5HandleGrids() {
  const territories = {};
  for (const grid of Object.keys(IT5_GRID_TERRITORIES)) {
    territories[grid] = [...IT5_GRID_TERRITORIES[grid]].sort();
  }
  return it5JsonResp({
    grids: Object.keys(IT5_GRID_TERRITORIES).sort(),
    territories: territories,
    runtime: 'cloudflare-edge',
    note: 'State-level approximation. Some states span multiple ISOs; the listed grid is the primary coverage for filtering.',
  }, 200);
}
// === end iteration 5 helpers ===




// === Edge-served facility detail page (Railway-independent fallback) ===
function _serveFacilityPage(slug) {
  const decoded = decodeURIComponent(slug);
  const titleCase = decoded.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  const html = `<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>${titleCase} | DC Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Facility details for ${titleCase} on DC Hub. View specs, location, power capacity, and connectivity.">
<meta property="og:title" content="${titleCase} | DC Hub">
<meta property="og:description" content="Facility details on DC Hub — 21,000+ data centers across 140+ countries.">
<meta property="og:image" content="https://dchub.cloud/images/og-home.png">
<meta property="og:url" content="https://dchub.cloud/facilities/${decoded}">
<link rel="icon" href="/favicon.ico">
<style>
:root{color-scheme:dark}
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0e1a;color:#c9d1e0;font-family:-apple-system,'Segoe UI',sans-serif;min-height:100vh;line-height:1.6}
.wrap{max-width:900px;margin:0 auto;padding:24px}
nav{background:#0d1224;border-bottom:1px solid #1a2035;padding:16px 24px;display:flex;justify-content:space-between;align-items:center}
nav a{color:#00d4ff;text-decoration:none;font-weight:600}
.crumb{font-size:13px;color:#7a8499;margin:16px 0}
.crumb a{color:#00d4ff;text-decoration:none}
h1{font-size:28px;margin:24px 0 8px;color:#fff}
.subhead{color:#7a8499;margin-bottom:24px}
.card{background:#141b2d;border:1px solid #1e293b;border-radius:10px;padding:24px;margin-bottom:16px}
.card h2{font-size:18px;margin:0 0 12px;color:#00d4ff}
.row{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #1e293b}
.row:last-child{border-bottom:0}
.row .k{color:#7a8499}
.row .v{color:#e8f8ff;font-family:'JetBrains Mono',monospace;font-size:13px}
.cta-row{display:flex;gap:12px;margin-top:24px}
.cta{display:inline-block;background:#00d4aa;color:#0a1220;padding:10px 18px;border-radius:6px;text-decoration:none;font-weight:600}
.cta.secondary{background:transparent;color:#00d4ff;border:1px solid #00d4ff}
.loading{padding:24px;text-align:center;color:#7a8499}
.error{padding:16px;background:#2d1a1a;border:1px solid #5a2a2a;border-radius:8px;color:#f4a4a4;font-size:14px}
</style>
</head>
<body>
<nav><a href="/">DC Hub</a><a href="/map">← Back to Map</a></nav>
<div class="wrap">
  <div class="crumb"><a href="/">Home</a> &middot; <a href="/map">Facilities Map</a> &middot; ${titleCase}</div>
  <h1>${titleCase}</h1>
  <p class="subhead">Facility details &middot; <span style="font-family:monospace;font-size:12px">${decoded}</span></p>
  <div class="card" id="details">
    <div class="loading" id="loading">Loading facility details from edge cache...</div>
    <div id="content" style="display:none"></div>
  </div>
  <div class="cta-row">
    <a class="cta" href="/api/v1/explorer">Open Search Explorer</a>
    <a class="cta secondary" href="/map">Back to Map</a>
  </div>
</div>
<script>
(async () => {
  const slug = ${JSON.stringify(decoded)};
  const loadingEl = document.getElementById('loading');
  const contentEl = document.getElementById('content');
  const RETRIES = 3;
  let data = null;
  for (let i = 0; i < RETRIES; i++) {
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 6000);
      const r = await fetch('/api/v1/facilities/by-slug/' + encodeURIComponent(slug), { signal: ctrl.signal });
      clearTimeout(t);
      if (r.ok) { data = await r.json(); break; }
    } catch (e) {
      if (i < RETRIES - 1) await new Promise(res => setTimeout(res, 1500 * (i + 1)));
    }
  }
  loadingEl.style.display = 'none';
  if (!data) {
    contentEl.innerHTML = '<div class="error">Could not load facility details right now. Try again in a moment, or use the search explorer to find this facility by name.</div>';
    contentEl.style.display = 'block';
    return;
  }
  const f = (data.data && data.data.facility) || data.data || data.facility || data;
  const fields = [
    ['Provider', f.provider], ['Status', f.status],
    ['City', f.city], ['State', f.state], ['Country', f.country],
    ['Power capacity', f.power_mw ? f.power_mw + ' MW' : null],
    ['Tier', f.tier], ['Latitude', f.latitude || f.lat], ['Longitude', f.longitude || f.lng || f.lon],
  ].filter(r => r[1] != null && r[1] !== '');
  contentEl.innerHTML = '<h2>Specifications</h2>' +
    fields.map(r => '<div class="row"><span class="k">' + r[0] + '</span><span class="v">' + r[1] + '</span></div>').join('');
  contentEl.style.display = 'block';
})();
</script>
</body></html>`;
  return new Response(html, {
    status: 200,
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'public, max-age=300',
      'X-Frame-Options': 'SAMEORIGIN',
    },
  });
}
// === end facility page handler ===


export default {
  async scheduled(event, env, ctx) {
    if (!env.DCHUB_CACHE) return;
    const results = await seedApiCache(env.DCHUB_CACHE);
    console.log(`[cron] Cache seed complete: API ${results.api_seeded}/${results.api_seeded + results.api_failed}, MCP ${results.mcp_seeded}/${results.mcp_seeded + results.mcp_failed}`);
  },

  async fetch(request, env, ctx) {
    // === Iteration 5 routes (edge fast-path) ===
    {
      const _it5_url = new URL(request.url);
      if (_it5_url.pathname === '/api/v1/search/edge')        return it5HandleEdgeSearch(request, env);
      if (_it5_url.pathname === '/api/v1/search/grids/edge')  return it5HandleGrids();
      if (_it5_url.pathname === '/api/v1/explorer' || _it5_url.pathname === '/explorer')  return _serveSearchExplorer();
      // Edge-served facility detail page — under /api/v1/* prefix (Pages routes to worker)
      if (_it5_url.pathname.startsWith('/api/v1/facility/')) {
        const slug = _it5_url.pathname.replace('/api/v1/facility/', '');
        if (slug && !slug.includes('/')) return _serveFacilityPage(slug);
      }
    }
    // === end iteration 5 routes ===
    const url = new URL(request.url);
    const startTime = Date.now();
    const pathname = url.pathname;
    // === v4.7: semantic search via Vectorize ===
    if (pathname === '/api/v1/search/semantic') {
      const apiKey = extractApiKey(request, url);
      const tierInfo = await resolveApiKeyTier(apiKey, env);
      if (!apiKey || tierInfo.invalid) return addCORS(json({ error: 'api_key_required', message: 'Provide X-API-Key. Get one at https://dchub.cloud/dashboard.html#api-keys' }, 401), request);
      if (tierInfo.tier === 'free') return addCORS(json({ error: 'plan_required', message: 'Semantic search requires Developer plan or higher.', upgrade_url: 'https://dchub.cloud/pricing#developer' }, 403), request);
      if (!env.AI || !env.VECTORIZE) return addCORS(json({ error: 'feature_unavailable', message: 'Semantic search index not bound.' }, 503), request);
      let q = '', k = 10, flt = null;
      try {
        if (request.method === 'POST') {
          const b = await request.json();
          q = (b.query || b.q || '').trim();
          k = Math.max(1, Math.min(parseInt(b.topK || b.top_k || b.limit || 10), 50));
          flt = b.filter || null;
        } else {
          q = (url.searchParams.get('q') || url.searchParams.get('query') || '').trim();
          k = Math.max(1, Math.min(parseInt(url.searchParams.get('topK') || '10'), 50));
        }
      } catch (e) { return addCORS(json({ error: 'bad_request' }, 400), request); }
      if (!q) return addCORS(json({ error: 'missing_query', message: 'Provide ?q=... or POST {"query":"..."}' }, 400), request);
      try {
        const emb = await env.AI.run('@cf/baai/bge-base-en-v1.5', { text: [q] });
        const v = emb && emb.data && emb.data[0];
        if (!v) throw new Error('embedding failed');
        const opts = { topK: k, returnMetadata: 'all' };
        if (flt) opts.filter = flt;
        const r = await env.VECTORIZE.query(v, opts);
        const results = (r.matches || []).map(m => Object.assign({ score: m.score }, m.metadata));
        return addCORS(json({ query: q, count: results.length, results, worker_version: '4.7.0' }, 200), request);
      } catch (e) { return addCORS(json({ error: 'search_failed', message: String(e.message || e) }, 500), request); }
    }


    // ══════════════════════════════════════════════════════════════
    // v4.6.2: 301 /press-release (no slug) → /press to dedupe list pages.
    // /press-release/<slug> detail pages are unaffected because the guard
    // only matches the exact bare path. Runs FIRST so it can't be skipped
    // by any handler bug downstream. Same pattern as the v4.6.1 /mcp guard.
    // ══════════════════════════════════════════════════════════════
    if (pathname === '/press-release' || pathname === '/press-release/') {
      return new Response(null, {
        status: 301,
        headers: {
          'Location': new URL('/press', url.origin).toString(),
          'Cache-Control': 'public, max-age=3600',
          'X-DC-Worker-Version': WORKER_VERSION,
          'x-dc-hub-source': 'worker-press-release-redirect',
        },
      });
    }

    // ════════════════════════════════════════════════════════════════
    // v4.9.3 Phase ZZZZZ-round31 (2026-05-24) — Pricing/Upgrade
    // shortcuts so the paywall has working URLs.
    //
    // BACKGROUND: The dchub-mcp-server's paywall message points users to
    // `dchub.cloud/ai#pricing?ref=mcp-trial&tool=X` for "Get Pro for
    // $49/mo." That page loads (200) but has NO Stripe button on it,
    // and the #pricing anchor doesn't exist — so every clicker bounces.
    // Master diagnostic 2026-05-24 confirmed 0% conversion across every
    // platform (claude, claude-desktop, curl, mcp, unknown, verify).
    //
    // FIX: Three new worker-served routes that DO go somewhere useful:
    //   /pricing                 → 302 to dchub.cloud/ai with #pricing-fixup
    //   /pricing/upgrade         → 302 to buy.stripe.com (Developer plan)
    //   /pricing/upgrade?tool=X  → same, with client_reference_id=mcp:tool=X
    //
    // The /mcp passthrough block downstream also rewrites the paywall
    // response text to swap `/ai#pricing?ref=mcp-trial&tool=X` for
    // `/pricing/upgrade?tool=X`, so even legacy unfixed messages route
    // through here. Until dchub-mcp-server ships its own fix, the worker
    // is the safety net.
    // ════════════════════════════════════════════════════════════════
    const STRIPE_DEVELOPER_CHECKOUT =
      'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c';
    if (pathname === '/pricing/upgrade' || pathname === '/pricing/upgrade/') {
      const tool = url.searchParams.get('tool') || '';
      const ref  = url.searchParams.get('ref')  || 'mcp-paywall';
      const target = new URL(STRIPE_DEVELOPER_CHECKOUT);
      target.searchParams.set('client_reference_id',
        `mcp:tool=${tool || 'unknown'}:ref=${ref}`);
      // Prefilled email if caller passed one along
      const email = url.searchParams.get('email');
      if (email) target.searchParams.set('prefilled_email', email);
      return Response.redirect(target.toString(), 302);
    }
    if (pathname === '/pricing' || pathname === '/pricing/') {
      // For now redirect to /ai (the existing pricing-ish page) but
      // add a `?upgrade=stripe` query so a small JS shim can scroll
      // to (or render) a Stripe button. If/when /ai gets a real
      // #pricing anchor + Stripe button, this can drop to a 301.
      const dest = new URL('/ai', url.origin);
      dest.searchParams.set('upgrade', 'stripe');
      // Carry tool tracking through
      const tool = url.searchParams.get('tool');
      if (tool) dest.searchParams.set('tool', tool);
      return Response.redirect(dest.toString(), 302);
    }

    // ══════════════════════════════════════════════════════════════
    // v4.8.7 INLINE /mcp/manifest HANDLER — Phase ZZZZZ-round26 (2026-05-23).
    // Claude.ai connector validation probes /mcp/manifest BEFORE attempting
    // POST /mcp. Upstream dchub-mcp-server (Express) returns 404 here, so
    // Claude.ai gives up with "Couldn't reach the MCP server" even though
    // POST /mcp works perfectly. Serve a static server-card from the edge
    // instead — no MCP backend change required.
    // MUST run BEFORE the /mcp passthrough block below (which would catch
    // this path and forward it upstream to the 404).
    // ══════════════════════════════════════════════════════════════
    if (request.method === 'GET' && (pathname === '/mcp/manifest' || pathname === '/mcp/manifest.json')) {
      // v4.9.1 — derives everything from MCP_SERVER_INFO + MCP_FALLBACK_TOOLS
      // so tool count is always honest. Pre-v4.9.1 this was hardcoded 40 (wrong).
      return new Response(JSON.stringify({
        schema_version:   'mcp-server-card/v1',
        name:             MCP_SERVER_INFO.name,
        version:          MCP_SERVER_INFO.version,
        description:      MCP_SERVER_INFO.description,
        url:              MCP_SERVER_INFO.url,
        transport:        MCP_SERVER_INFO.transport,
        protocol_version: MCP_SERVER_INFO.protocol_version,
        provider: {
          organization: MCP_SERVER_INFO.organization,
          url:          MCP_SERVER_INFO.homepage,
          contact:      MCP_SERVER_INFO.contact,
        },
        authentication: {
          type:         'api_key',
          header:       'X-API-Key',
          optional_for: ['free_tier'],
          note:         'Free tier (10 calls/day) requires no auth. Paid tiers add X-API-Key header.',
        },
        capabilities:    { tools: { listChanged: true } },
        tools_count:     MCP_FALLBACK_TOOLS.length,
        tools_endpoint:  'POST /mcp with {"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        pricing: {
          free:       `10 calls/day, truncated results, ${MCP_FALLBACK_TOOLS.length} tools available`,
          developer:  `$49/mo — 1,000/day, all ${MCP_FALLBACK_TOOLS.length} tools, full results`,
          pro:        '$199/mo — 10,000/day',
          enterprise: '$499/mo — 100,000/day',
        },
        documentation: MCP_SERVER_INFO.documentation,
        signup_url:    MCP_SERVER_INFO.signup_url,
      }, null, 2), {
        status: 200,
        headers: {
          'Content-Type':                'application/json; charset=utf-8',
          'Cache-Control':               'public, max-age=3600',
          'Access-Control-Allow-Origin': '*',
          'X-DC-Manifest-Source':        'worker-inline',
          'X-DC-Worker-Version':         WORKER_VERSION,
        },
      });
    }

    // ══════════════════════════════════════════════════════════════
    // v4.6.1 HARD-GUARANTEED MCP PASSTHROUGH (runs before ANY routing)
    // DO NOT move this. DO NOT add logic above it (except the press redirect
    // and the v4.8.7 /mcp/manifest inline handler).
    // ══════════════════════════════════════════════════════════════
    if (pathname === '/mcp' || pathname === '/mcp/' || pathname.startsWith('/mcp/')) {
      if (request.method === 'OPTIONS') {
        return new Response(null, {
          status: 204,
          headers: {
            'Access-Control-Allow-Origin':  '*',
            'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Accept, Authorization, Mcp-Session-Id, X-API-Key',
            'Access-Control-Expose-Headers':'Mcp-Session-Id, X-DC-Worker-Version, x-dc-hub-backend, x-dc-hub-source',
            'Access-Control-Max-Age':       '86400',
            'Cache-Control':                'no-store, no-cache, must-revalidate, private',
          },
        });
      }
      // r33-J round 9 (2026-05-21): browser users hitting GET /mcp
      // used to see {"error":"No session..."} from server.mjs upstream
      // — useless when they're trying to figure out HOW to connect.
      // If Accept: text/html (i.e. a browser), serve a self-contained
      // landing page with copy-paste setup for Claude.ai web, Desktop,
      // Cursor, Cline, etc. Protocol clients (Accept: */* or
      // application/json) still pass through to MCP_BACKEND.
      if (request.method === 'GET' && (request.headers.get('Accept') || '').toLowerCase().includes('text/html')) {
        return new Response(MCP_LANDING_HTML_V1, {
          status: 200,
          headers: {
            'Content-Type':                 'text/html; charset=utf-8',
            'Cache-Control':                'public, max-age=300',
            'Access-Control-Allow-Origin':  '*',
            'X-DC-Worker-Version':          WORKER_VERSION,
            'x-dc-hub-source':              'worker-mcp-landing',
          },
        });
      }
      // ╔═════ v4.7: Intercept tools/call for semantic_search (no Railway round-trip) ═════╗
      if (request.method === 'POST') {
        try {
          const reqClone = request.clone();
          const body = await reqClone.json();
          if (body.method === 'tools/call' && body.params?.name === 'semantic_search') {
            const args = body.params.arguments || {};
            const query = (args.query || '').trim();
            const topK = Math.max(1, Math.min(parseInt(args.topK || 10), 50));
            if (!query) {
              return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id || null, error: { code: -32602, message: 'query parameter required' } }), { status: 400, headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' } });
            }
            if (!env.AI || !env.VECTORIZE) {
              return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id || null, error: { code: -32603, message: 'Vectorize/AI not bound' } }), { status: 503, headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' } });
            }
            const apiKey = extractApiKey(request, url);
            const tierInfo = await resolveApiKeyTier(apiKey, env);
            if (!apiKey || tierInfo.invalid || tierInfo.tier === 'free') {
              return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id || null, result: { content: [{ type: 'text', text: '🔒 Semantic search requires Developer plan or higher.\nGet yours at https://dchub.cloud/pricing#developer ($49/mo, 1,000 calls/day).\nOr signup free for 10 calls/day: https://dchub.cloud/signup' }], isError: false } }), { status: 200, headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*', 'X-DC-Worker-Version': WORKER_VERSION, 'x-dc-hub-source': 'worker-mcp-tier-gate' } });
            }
            try {
              const emb = await env.AI.run('@cf/baai/bge-base-en-v1.5', { text: [query] });
              const v = emb && emb.data && emb.data[0];
              if (!v) throw new Error('embedding failed');
              const r = await env.VECTORIZE.query(v, { topK, returnMetadata: 'all' });
              const results = (r.matches || []).map(m => Object.assign({ score: m.score }, m.metadata));
              const summary = `Found ${results.length} match${results.length === 1 ? '' : 'es'} for: "${query}"`;
              return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id || null, result: { content: [{ type: 'text', text: summary }, { type: 'text', text: JSON.stringify(results, null, 2) }], isError: false } }), { status: 200, headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*', 'X-DC-Worker-Version': WORKER_VERSION, 'x-dc-hub-source': 'worker-mcp-vectorize' } });
            } catch (e) {
              return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id || null, error: { code: -32603, message: 'search_failed: ' + String(e.message || e) } }), { status: 500, headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' } });
            }
          }
        } catch (e) { /* fall through to Railway passthrough */ }
      }
      // ╚═══════════════════════════════════════════════════════════════════════════════╝

      try {
        const fwdHeaders = new Headers(request.headers);
        fwdHeaders.delete('host');
        fwdHeaders.delete('cf-connecting-ip');
        fwdHeaders.delete('cf-ray');
        fwdHeaders.delete('cf-visitor');
        fwdHeaders.delete('x-forwarded-proto');
        // ════════════════════════════════════════════════════════════
        // v4.8.8 Phase ZZZZZ-round26.5 (2026-05-23): Claude.ai connector
        // probe sends `Accept: application/json` only, but the official
        // MCP SDK on the upstream rejects that with JSON-RPC error -32000
        // "Not Acceptable: Client must accept both application/json and
        // text/event-stream". Claude.ai surfaces that as the misleading
        // "Couldn't reach the MCP server" error. Rewrite the Accept header
        // here so the upstream is happy regardless of what the client
        // sent. Compliant clients (Cline, Cursor, MCP Inspector) already
        // send both — this is a no-op for them.
        // ════════════════════════════════════════════════════════════
        const _acc = (fwdHeaders.get('Accept') || '*/*').toLowerCase();
        if (!_acc.includes('text/event-stream') || !_acc.includes('application/json')) {
          fwdHeaders.set('Accept', 'application/json, text/event-stream');
        }
        const upstream = await fetch(`${MCP_BACKEND}${pathname}${url.search}`, {
          method:   request.method,
          headers:  fwdHeaders,
          body:     (request.method === 'GET' || request.method === 'HEAD') ? undefined : request.body,
          redirect: 'manual',
        });
        const h = new Headers(upstream.headers);
        h.set('X-DC-Worker-Version', WORKER_VERSION);
        h.set('x-dc-hub-backend',    'railway');
        h.set('x-dc-hub-source',     'worker-mcp-passthrough');
        h.set('Cache-Control',       'no-store, no-cache, must-revalidate, private');
        h.set('Access-Control-Allow-Origin',   '*');
        h.set('Access-Control-Expose-Headers', 'Mcp-Session-Id, X-DC-Worker-Version, x-dc-hub-backend, x-dc-hub-source');
        h.delete('cf-cache-status');
        // ════════════════════════════════════════════════════════════
        // v4.8.9 Phase ZZZZZ-round27 (2026-05-23): SSE→JSON transcode.
        // If the CLIENT only sent Accept: application/json (not
        // text/event-stream), but the upstream returned an SSE response
        // (because v4.8.8 rewrote the request to include both), parse
        // the single-shot SSE wrapper and return raw JSON. This is what
        // Claude.ai's connector probe expects — without it the response
        // Content-Type doesn't match what the client accepted, the HTTP
        // client rejects it, and Claude.ai reports "Couldn't reach the
        // MCP server".
        // ════════════════════════════════════════════════════════════
        const _clientWantsJsonOnly =
          _acc.includes('application/json') &&
          !_acc.includes('text/event-stream') &&
          !_acc.includes('*/*');
        const _upstreamCT = (upstream.headers.get('Content-Type') || '').toLowerCase();
        const _upstreamIsSSE = _upstreamCT.includes('text/event-stream');
        // ════════════════════════════════════════════════════════════
        // v4.9.3 PAYWALL URL REWRITER (Phase ZZZZZ-round31, 2026-05-24)
        // The dchub-mcp-server's paywall responses embed `dchub.cloud/ai
        // #pricing?ref=mcp-trial&tool=X` — a URL that loads but has no
        // Stripe button. Master diagnostic confirmed 0% conv across all
        // platforms. Rewrite the URL on its way back through the worker
        // to point at `/pricing/upgrade?tool=X` (302→Stripe). The
        // dev-key redeem URL stays unchanged.
        // Applied to BOTH SSE-transcoded responses and pass-through
        // responses below.
        // ════════════════════════════════════════════════════════════
        const _rewritePaywallUrls = (s) => {
          if (!s || typeof s !== 'string') return s;
          if (!s.includes('/ai#pricing')) return s;
          // v4.9.4 (2026-05-24): target api.dchub.cloud, NOT dchub.cloud.
          // dchub.cloud/pricing/* isn't bound to this worker via CF
          // Workers Routes (only /mcp/* and /.well-known/* are), so
          // dchub.cloud/pricing/upgrade returns 404 from Pages — making
          // the rewriter strictly WORSE than the original broken
          // /ai#pricing. api.dchub.cloud/* IS bound to this worker
          // (via the api subdomain DNS), so /pricing/upgrade hits the
          // 302→Stripe handler below.
          return s.replace(
            /https?:\/\/dchub\.cloud\/ai#pricing(?:\?ref=mcp-trial)?(?:&tool=([^)\s\]"']+))?/g,
            (_m, tool) =>
              `https://api.dchub.cloud/pricing/upgrade?tool=${tool || 'unknown'}&ref=mcp-paywall`
          );
        };
        if (_clientWantsJsonOnly && _upstreamIsSSE) {
          let sseBody = await upstream.text();
          // SSE single-shot frame format:
          //   event: message
          //   data: {"jsonrpc":"2.0",...}
          //   (blank line)
          // There may be multiple `data:` lines (continuation); per RFC
          // they're concatenated with \n. For Claude.ai's initialize
          // and tools/list probes the response is always a single line.
          const dataLines = sseBody
            .split(/\r?\n/)
            .filter(line => line.startsWith('data: '))
            .map(line => line.substring(6));
          let jsonPayload = dataLines.length === 0
            ? '{}'
            : (dataLines.length === 1 ? dataLines[0] : dataLines.join('\n'));
          // v4.9.3: rewrite paywall URLs in the JSON payload
          jsonPayload = _rewritePaywallUrls(jsonPayload);
          h.set('Content-Type',        'application/json; charset=utf-8');
          h.set('x-dc-hub-source',     'worker-mcp-sse-to-json');
          h.delete('content-length');  // body length changed
          return new Response(jsonPayload, {
            status:     upstream.status,
            statusText: upstream.statusText,
            headers:    h,
          });
        }
        // v4.9.3: for SSE pass-through (client accepts text/event-stream),
        // ALSO rewrite paywall URLs in the body. The cost is ~50ms to
        // buffer the response instead of streaming — acceptable for
        // single-shot tools/call responses, which is all we hit here.
        if (_upstreamIsSSE) {
          const body = await upstream.text();
          const rewritten = _rewritePaywallUrls(body);
          if (rewritten !== body) {
            h.set('x-dc-hub-source', 'worker-mcp-paywall-rewrite');
            h.delete('content-length');
          }
          return new Response(rewritten, {
            status:     upstream.status,
            statusText: upstream.statusText,
            headers:    h,
          });
        }
        return new Response(upstream.body, {
          status:     upstream.status,
          statusText: upstream.statusText,
          headers:    h,
        });
      } catch (e) {
        return new Response(
          JSON.stringify({ error: 'mcp proxy failed', detail: e && e.message ? e.message : String(e) }),
          { status: 502, headers: { 'Content-Type':  'application/json', 'Cache-Control': 'no-store, no-cache, must-revalidate, private', 'X-DC-Worker-Version': WORKER_VERSION, 'x-dc-hub-source': 'worker-mcp-error', 'Access-Control-Allow-Origin': '*' } }
        );
      }
    }

    // ── Social bot OG tags ──
    const userAgent = request.headers.get('user-agent') || '';
    if (isSocialBot(userAgent) && !pathname.startsWith('/api/') && pathname !== '/mcp' && pathname !== '/mcp/') {
      if (!pathname.match(/\.(png|jpg|jpeg|gif|webp|svg|ico|css|js|woff2?|ttf|json|xml|txt)$/i)) {
        const meta = getOGMetaForPath(pathname);
        return new Response(buildOGHtml(meta, url.toString()), { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'public, max-age=3600' } });
      }
    }

    // ── .well-known ──
    if (pathname.startsWith('/.well-known/')) {
      const wk = wellKnownResponse(pathname);
      if (wk) return wk;
    }

    // ── /health ──
    if (pathname === '/health' || pathname === '/api/health') {
      const healthResp = await proxyToRailway(request, pathname, url.search, 0, 5000);
      if (healthResp && healthResp.status < 500) {
        const r = new Response(healthResp.body, healthResp);
        r.headers.set('X-DC-Worker-Version', WORKER_VERSION);
        r.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
        r.headers.set('Cache-Control', 'no-store');
        return r;
      }
      return new Response(JSON.stringify({ status: 'unhealthy', worker: 'ok', origin: 'unreachable', worker_version: WORKER_VERSION }), { status: 503, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } });
    }

    // ── Non-API routes ──
    if (!pathname.startsWith('/api/')) {
      // /ai redirect intercept
      const strippedPath = pathname.endsWith('/') && pathname.length > 1 ? pathname.slice(0, -1) : pathname;
      if (strippedPath === '/ai') {
        const probeResp = await fetch(request, { redirect: 'manual' });
        if (probeResp.status >= 300 && probeResp.status < 400) {
          const contentResp = await fetch(request, { redirect: 'follow' });
          if (contentResp.ok) {
            const resp = new Response(contentResp.body, { status: 200, headers: contentResp.headers });
            resp.headers.delete('Location');
            resp.headers.set('X-DC-Worker-Version', WORKER_VERSION);
            resp.headers.set('X-DC-Rewrite', '/ai (redirect intercepted)');
            resp.headers.set('Cache-Control', 'public, max-age=120, stale-while-revalidate=300');
            return resp;
          }
        }
        if (probeResp.status !== 0) {
          const resp = new Response(probeResp.body, probeResp);
          resp.headers.set('X-DC-Worker-Version', WORKER_VERSION);
          return resp;
        }
      }

      // MCP HANDLER (legacy in-file — top-of-fetch passthrough above will normally win)
      if (pathname === '/mcp' || pathname === '/mcp/') {
        // Should never reach here because of the v4.6.1 top-of-fetch passthrough.
        return new Response(JSON.stringify({ error: 'unreachable mcp branch' }), { status: 500, headers: { 'Content-Type': 'application/json' } });
      }

      // Discovery paths → Railway
      if (isDiscoveryPath(pathname)) {
        const resp = await proxyToRailway(request, pathname, url.search, 3600, 10000);
        if (resp) return addCORS(new Response(resp.body, resp), request);
      }

      // News digest routes
      if (pathname.startsWith('/news') || pathname.startsWith('/press-release')) {
        const newsResp = await handleNewsRoute(pathname, request, env);
        if (newsResp) return newsResp;
      }

      // Phase ZZZZZ-round33 (2026-05-24): SEO landing pages + status page
      // + sitemaps live in Flask. Route them to Railway directly instead
      // of falling through to `fetch(request)` (which loops on api.dchub.cloud).
      // Pages don't have these routes — Pages would 404 anyway.
      if (isFlaskHtmlPath(pathname)) {
        // v4.9.6: KV stale-while-error wrapper for Flask HTML/asset paths
        const isOg      = pathname.startsWith('/static/og/');
        const isRobots  = pathname === '/robots.txt' || pathname === '/robots-canonical.txt';
        const isSitemap = pathname.startsWith('/sitemap');
        const kvKey     = 'flaskhtml:' + pathname + (url.search || '');

        // GET: try KV fresh (5 min) for quick edge response
        if (request.method === 'GET' && env.DCHUB_CACHE) {
          try {
            const cached = await kvCacheGet(env.DCHUB_CACHE, kvKey, false, 300, 86400);
            if (cached && cached.mode === 'fresh') {
              const r = cached.response;
              r.headers.set('X-DC-Worker-Version', WORKER_VERSION);
              r.headers.set('X-DC-Route-Class', 'flask-html-kv-fresh');
              if (isOg) r.headers.set('Content-Type', 'image/png');
              return r;
            }
          } catch (_e) { /* fall through */ }
        }

        const seoResp = await proxyToRailway(
          request, pathname, url.search, isOg ? 86400 : 60, isOg ? 20000 : 12000);

        if (seoResp && seoResp.status === 200) {
          // Buffer body so we can both serve and KV-store it
          const buf = await seoResp.arrayBuffer();
          const ct  = seoResp.headers.get('content-type')
                      || (isOg ? 'image/png' : 'text/html; charset=utf-8');
          // Store as text if small enough; OG images stored as base64 in KV
          if (request.method === 'GET' && env.DCHUB_CACHE && buf.byteLength < 2_000_000) {
            try {
              let bodyForKv;
              if (isOg) {
                // base64 so the JSON wrapper doesn't choke on binary
                const bytes = new Uint8Array(buf);
                let s = '';
                for (let i = 0; i < bytes.byteLength; i++) s += String.fromCharCode(bytes[i]);
                bodyForKv = 'b64:' + btoa(s);
              } else {
                bodyForKv = new TextDecoder().decode(buf);
              }
              await kvCacheStore(env.DCHUB_CACHE, kvKey, bodyForKv, ct, 86400);
            } catch (_e) { /* non-fatal */ }
          }
          const out = new Response(buf, { status: 200, headers: seoResp.headers });
          out.headers.set('X-DC-Worker-Version', WORKER_VERSION);
          out.headers.set('X-DC-Route-Class', 'flask-html-fresh');
          out.headers.set('Content-Type', ct);
          if (isOg) {
            out.headers.set('Cache-Control', 'public, max-age=86400, s-maxage=86400, stale-while-revalidate=604800');
          } else if (isSitemap) {
            out.headers.set('Cache-Control', 'public, max-age=3600');
          } else if (isRobots) {
            out.headers.set('Cache-Control', 'public, max-age=86400');
          } else {
            out.headers.set('Cache-Control', 'public, max-age=300, s-maxage=900');
          }
          return out;
        }

        // Non-200 from Railway: 3xx/4xx pass through as-is (not an "outage")
        if (seoResp && seoResp.status !== 522 && seoResp.status < 500) {
          const out = new Response(seoResp.body, seoResp);
          out.headers.set('X-DC-Worker-Version', WORKER_VERSION);
          out.headers.set('X-DC-Route-Class', 'flask-html-passthrough');
          return out;
        }

        // Railway 5xx / 522 / timed out — try Render failover for GETs
        if (request.method === 'GET') {
          const renderResp = await proxyToRender(request, pathname, url.search, 12000);
          if (renderResp && renderResp.status < 500) {
            const out = new Response(renderResp.body, renderResp);
            out.headers.set('X-DC-Worker-Version', WORKER_VERSION);
            out.headers.set('x-dc-hub-backend', 'render');
            out.headers.set('X-Failover-Mode', 'render-active');
            return out;
          }
          // Render failed too — try KV stale (out to 24h)
          if (env.DCHUB_CACHE) {
            try {
              const stale = await kvCacheGet(env.DCHUB_CACHE, kvKey, true, 0, 86400);
              if (stale) {
                let resp = stale.response;
                // Decode base64 OG images back to binary
                if (isOg) {
                  const txt = await resp.text();
                  if (txt.startsWith('b64:')) {
                    const bin = atob(txt.slice(4));
                    const bytes = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                    resp = new Response(bytes, { status: 200, headers: { 'Content-Type': 'image/png' } });
                  }
                }
                resp.headers.set('X-DC-Worker-Version', WORKER_VERSION);
                resp.headers.set('X-DC-Route-Class', 'flask-html-kv-stale');
                resp.headers.set('x-dc-hub-backend', 'kv-stale');
                resp.headers.set('X-Failover-Mode', 'kv-stale-active');
                return resp;
              }
            } catch (_e) { /* nothing else to try */ }
          }
        }

        // All layers failed
        return new Response(
          JSON.stringify({ error: 'page_unavailable', path: pathname,
                          message: 'Railway + Render + KV-stale all unreachable for this Flask-served page',
                          worker: WORKER_VERSION }),
          { status: 503, headers: { 'Content-Type': 'application/json',
                                     'X-DC-Worker-Version': WORKER_VERSION,
                                     'X-DC-Route-Class': 'flask-html-503' } }
        );
      }

      // Pages passthrough
      const pagesResp = await fetch(request);
      const contentType = pagesResp.headers.get('content-type') || '';
      if (contentType.includes('text/html')) {
        const resp = new Response(pagesResp.body, pagesResp);
        resp.headers.set('Cache-Control', 'public, max-age=120, stale-while-revalidate=300');
        resp.headers.set('Link', ['<https://dchub-backend-production.up.railway.app>; rel=preconnect', '<https://unpkg.com>; rel=preconnect', '<https://fonts.googleapis.com>; rel=preconnect', '<https://fonts.gstatic.com>; rel=preconnect; crossorigin'].join(', '));
        resp.headers.set('X-DC-Worker-Version', WORKER_VERSION);
        return resp;
      }
      if (url.search.includes('v=') || url.pathname.match(/\.[0-9a-f]{8,}\.(js|css)$/)) {
        const resp = new Response(pagesResp.body, pagesResp);
        resp.headers.set('Cache-Control', 'public, max-age=31536000, immutable');
        return resp;
      }
      if (url.pathname.match(/\.(png|jpg|jpeg|gif|webp|svg|ico|woff2?|ttf|eot)$/i)) {
        const resp = new Response(pagesResp.body, pagesResp);
        resp.headers.set('Cache-Control', 'public, max-age=86400, stale-while-revalidate=604800');
        return resp;
      }
      if (url.pathname.match(/\.(js|css)$/i)) {
        const resp = new Response(pagesResp.body, pagesResp);
        resp.headers.set('Cache-Control', 'public, max-age=300, stale-while-revalidate=600');
        return resp;
      }
      return pagesResp;
    }

    // ================================================================
    // API ROUTES
    // ================================================================
    if (request.method === 'OPTIONS') return handleCORS(request);

    const isGet = request.method === 'GET';
    const tier = getRouteTier(pathname);
    const timeoutMs = getTimeout(pathname);
    const hasApiKey = request.headers.get('X-API-Key') || url.searchParams.get('api_key');

    // Publish proxy
    if (pathname === '/api/publish') {
      return addCORS(await handlePublishRoute(request, env), request);
    }

    // FEMA Flood Zone Proxy
    if (pathname === '/api/v1/fema/flood-zone' && isGet) {
      const lat = url.searchParams.get('lat');
      const lng = url.searchParams.get('lng');
      if (!lat || !lng || isNaN(parseFloat(lat)) || isNaN(parseFloat(lng))) {
        return addCORS(json({ success: false, error: 'lat and lng query parameters required (numeric)' }, 400), request);
      }
      const femaUrl = 'https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/28/query'
        + '?geometry=' + encodeURIComponent(lng + ',' + lat)
        + '&geometryType=esriGeometryPoint&inSR=4326&outSR=4326'
        + '&spatialRel=esriSpatialRelIntersects'
        + '&outFields=FLD_ZONE,ZONE_SUBTY,SFHA_TF,STATIC_BFE,DEPTH,LEN_UNIT'
        + '&returnGeometry=false&f=json';
      try {
        const controller = new AbortController();
        const femaTimeout = setTimeout(() => controller.abort(), 8000);
        const femaResp = await fetch(femaUrl, { signal: controller.signal });
        clearTimeout(femaTimeout);
        if (!femaResp.ok) {
          return addCORS(json({ success: false, error: 'FEMA API error', status: femaResp.status }, 502), request);
        }
        const femaData = await femaResp.json();
        const features = femaData.features || [];
        if (features.length === 0) {
          const result = addCORS(json({
            success: true,
            data: { flood_zone: 'X', zone_subtype: 'AREA OF MINIMAL FLOOD HAZARD', sfha: false, base_flood_elevation: null, depth: null, source: 'fema_nfhl', note: 'No NFHL features found at this point' },
            query: { lat: parseFloat(lat), lng: parseFloat(lng) },
            raw_feature_count: 0
          }), request);
          result.headers.set('X-DC-Worker-Version', WORKER_VERSION);
          result.headers.set('Cache-Control', 'public, max-age=3600');
          return result;
        }
        const attrs = features[0].attributes || {};
        const floodZone = attrs.FLD_ZONE || 'X';
        const zoneSubtype = attrs.ZONE_SUBTY || null;
        const sfhaRaw = attrs.SFHA_TF;
        const sfha = sfhaRaw === 'T' || sfhaRaw === 'True' || sfhaRaw === true;
        let bfe = attrs.STATIC_BFE != null ? attrs.STATIC_BFE : null;
        let depth = attrs.DEPTH != null ? attrs.DEPTH : null;
        if (bfe !== null && bfe < -100) bfe = null;
        if (depth !== null && depth < 0) depth = null;
        const result = addCORS(json({
          success: true,
          data: { flood_zone: floodZone, zone_subtype: zoneSubtype, sfha: sfha, base_flood_elevation: bfe, depth: depth, source: 'fema_nfhl' },
          query: { lat: parseFloat(lat), lng: parseFloat(lng) },
          raw_feature_count: features.length
        }), request);
        result.headers.set('X-DC-Worker-Version', WORKER_VERSION);
        result.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
        result.headers.set('Cache-Control', 'public, max-age=3600');
        return result;
      } catch (e) {
        return addCORS(json({ success: false, error: 'FEMA API unreachable', message: e.name === 'AbortError' ? 'FEMA API timeout (8s)' : String(e.message || e) }, 504), request);
      }
    }

    // v4.6.0: get-api-key
    if (pathname === '/api/auth/get-api-key' && (request.method === 'GET' || request.method === 'POST')) {
      return addCORS(await handleGetApiKey(request, env), request);
    }

    // API key management routes
    if (pathname === '/api/admin/create-api-key' && request.method === 'POST') {
      const adminChk = requireAdminKey(request, env, url);
      if (!adminChk.ok) return addCORS(json({ error: adminChk.error }, adminChk.status), request);
      if (!env.DCHUB_API_KEYS) return addCORS(json({ error: 'DCHUB_API_KEYS KV not configured' }, 500), request);
      return addCORS(await handleCreateApiKey(request, env), request);
    }
    if (pathname === '/api/admin/usage' && isGet) {
      const adminChk = requireAdminKey(request, env, url);
      if (!adminChk.ok) return addCORS(json({ error: adminChk.error }, adminChk.status), request);
      return addCORS(await handleUsageCheck(request, url, env), request);
    }
    if (pathname === '/api/admin/revoke-api-key' && request.method === 'POST') {
      const adminChk = requireAdminKey(request, env, url);
      if (!adminChk.ok) return addCORS(json({ error: adminChk.error }, adminChk.status), request);
      if (!env.DCHUB_API_KEYS) return addCORS(json({ error: 'DCHUB_API_KEYS KV not configured' }, 500), request);
      return addCORS(await handleRevokeApiKey(request, env), request);
    }
    if ((pathname === '/api/stripe/mcp-webhook' || pathname === '/api/stripe/webhook') && request.method === 'POST') {
      return addCORS(await handleStripeWebhook(request, env), request);
    }

    // Cache status
    if (pathname === '/api/cache/status' && env.DCHUB_CACHE) {
      const list = await env.DCHUB_CACHE.list({ prefix: 'kv:', limit: 50 });
      const mcpList = await env.DCHUB_CACHE.list({ prefix: 'mcp:', limit: 50 });
      const keys = [];
      for (const k of list.keys) { const raw = await env.DCHUB_CACHE.get(k.name); let age = null; if (raw) { try { const e = JSON.parse(raw); age = Math.round((Date.now() - e.ts) / 1000); } catch(e) {} } keys.push({ path: k.name.replace('kv:', ''), age_seconds: age, type: 'api' }); }
      for (const k of mcpList.keys) { const raw = await env.DCHUB_CACHE.get(k.name); let age = null; if (raw) { try { const e = JSON.parse(raw); age = Math.round((Date.now() - e.ts) / 1000); } catch(e) {} } keys.push({ path: k.name, age_seconds: age, type: 'mcp' }); }
      return addCORS(json({ cached_endpoints: keys.length, keys, worker_version: WORKER_VERSION }), request);
    }

    // Cache purge
    if (pathname === '/api/cache/purge' && request.method === 'POST' && env.DCHUB_CACHE) {
      const adminChk = requireAdminKey(request, env, url);
      if (!adminChk.ok) return addCORS(json({ error: adminChk.error }, adminChk.status), request);
      const list = await env.DCHUB_CACHE.list({ prefix: 'kv:', limit: 200 });
      const mcpList = await env.DCHUB_CACHE.list({ prefix: 'mcp:', limit: 200 });
      let deleted = 0;
      for (const key of list.keys) { await env.DCHUB_CACHE.delete(key.name); deleted++; }
      for (const key of mcpList.keys) { await env.DCHUB_CACHE.delete(key.name); deleted++; }
      return addCORS(json({ success: true, purged: deleted, includes_mcp: true }), request);
    }

    // Admin: seed caches
    if ((pathname === '/api/admin/seed-api-cache' || pathname === '/api/admin/seed-mcp-cache') && env.DCHUB_CACHE) {
      const adminChk = requireAdminKey(request, env, url);
      if (!adminChk.ok) return addCORS(json({ error: adminChk.error }, adminChk.status), request);
      const results = await seedApiCache(env.DCHUB_CACHE);
      return addCORS(json({ success: true, worker_version: WORKER_VERSION, ...results }), request);
    }

    // Version
    if (pathname === '/api/version' || pathname === '/api/v1/version') {
      return addCORS(json({ version: WORKER_VERSION, source: 'cloudflare-worker', backend: 'railway', mcp_tiers: Object.fromEntries(Object.entries(MCP_TIERS).map(([k, v]) => [k, { daily_limit: v.daily_limit, results_limit: v.results_limit }])), gated_tools: [...GATED_TOOLS], cron: '0 */6 * * * (every 6 hours)', timestamp: new Date().toISOString() }), request);
    }

    // STEP 1: KV fresh cache
    if (isGet && !hasApiKey && env.DCHUB_CACHE && kvHasFreshCache(pathname)) {
      const kvResult = await kvCacheGet(env.DCHUB_CACHE, kvCacheKey(url.toString()), false, tier.kvFreshTtl, tier.kvStaleTtl);
      if (kvResult) {
        const resp = addCORS(kvResult.response, request);
        resp.headers.set('X-DC-Worker-Version', WORKER_VERSION);
        resp.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
        if (tier.browserMaxAge > 0) resp.headers.set('Cache-Control', `public, max-age=${tier.browserMaxAge}, stale-while-revalidate=${tier.browserMaxAge * 2}`);
        return resp;
      }
    }

    // STEP 2: Proxy to Railway
    const edgeTtl = (isGet && !hasApiKey) ? tier.edgeTtl : 0;
    const { resp, attempts } = await proxyWithRetry(request, pathname, url.search, edgeTtl, timeoutMs);

    if (resp && resp.status < 500) {
      let cacheClone = null;
      if (isGet && resp.status === 200 && env.DCHUB_CACHE && kvIsCacheable(pathname)) cacheClone = resp.clone();
      const result = addCORS(new Response(resp.body, resp), request);
      result.headers.set('x-dc-hub-backend', 'railway');
      result.headers.set('X-DC-Worker-Version', WORKER_VERSION);
      result.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
      result.headers.set('X-DC-Attempts', String(attempts));
      if (isGet && !hasApiKey && tier.browserMaxAge > 0) result.headers.set('Cache-Control', `public, max-age=${tier.browserMaxAge}, stale-while-revalidate=${tier.browserMaxAge * 2}`);
      if (cacheClone) ctx.waitUntil((async () => { const body = await cacheClone.text(); await kvCacheStore(env.DCHUB_CACHE, kvCacheKey(url.toString()), body, cacheClone.headers.get('content-type') || 'application/json', tier.kvStaleTtl); })());
      return result;
    }

    // STEP 2.5: Render failover (Phase ZZZZZ-round26, 2026-05-23)
    // GETs only — Render runs IS_FAILOVER=true so it's read-only. Mirrors
    // the dchub-frontend Pages worker v4.24.0-switzerland chain so
    // api.dchub.cloud doesn't 503 immediately when Railway hiccups.
    if (isGet) {
      const renderResp = await proxyToRender(request, pathname, url.search, 45000);
      if (renderResp && renderResp.status < 500) {
        let cacheClone = null;
        if (renderResp.status === 200 && env.DCHUB_CACHE && kvIsCacheable(pathname)) cacheClone = renderResp.clone();
        const result = addCORS(new Response(renderResp.body, renderResp), request);
        result.headers.set('x-dc-hub-backend',      'render');
        result.headers.set('x-dc-hub-failover',     'true');
        result.headers.set('X-Failover-Mode',       'render-active');
        result.headers.set('X-DC-Worker-Version',   WORKER_VERSION);
        result.headers.set('X-DC-Response-Time',    `${Date.now() - startTime}ms`);
        if (!hasApiKey && tier.browserMaxAge > 0) result.headers.set('Cache-Control', `public, max-age=${tier.browserMaxAge}, stale-while-revalidate=${tier.browserMaxAge * 2}`);
        if (cacheClone) ctx.waitUntil((async () => { const body = await cacheClone.text(); await kvCacheStore(env.DCHUB_CACHE, kvCacheKey(url.toString()), body, cacheClone.headers.get('content-type') || 'application/json', tier.kvStaleTtl); })());
        return result;
      }
    }

    // STEP 3: Stale KV
    if (isGet && env.DCHUB_CACHE && kvIsCacheable(pathname)) {
      const kvResult = await kvCacheGet(env.DCHUB_CACHE, kvCacheKey(url.toString()), true, tier.kvFreshTtl, tier.kvStaleTtl);
      if (kvResult) {
        const staleResp = addCORS(kvResult.response, request);
        staleResp.headers.set('x-dc-hub-source', 'kv-stale-cache');
        staleResp.headers.set('X-Failover-Mode', hasApiKey ? 'stale-authenticated' : 'stale-anonymous');
        staleResp.headers.set('X-DC-Worker-Version', WORKER_VERSION);
        staleResp.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
        return staleResp;
      }
    }

    // STEP 4: 503
    const errResp = addCORS(json({ error: 'Service temporarily unavailable', message: 'Backend unreachable and no cached data available. Please retry shortly.', status: 503, worker_version: WORKER_VERSION, tip: 'This message lands when Railway, Render failover, and KV stale cache are all unavailable.' }, 503), request);
    errResp.headers.set('X-DC-Worker-Version', WORKER_VERSION);
    errResp.headers.set('X-DC-Response-Time', `${Date.now() - startTime}ms`);
    return errResp;
  }
};