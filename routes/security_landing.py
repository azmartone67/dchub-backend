"""
security_landing.py — public /security page (compliance + data posture).

Phase ZZZZZ-round47.4 (2026-05-25). Enterprise pitch surface — covers
hosting, data handling, retention, customer obligations. Conservative
copy: no claims we can't back. SOC2/ISO27001 marked as roadmap, not
present.
"""
import datetime
from flask import Blueprint

security_bp = Blueprint("security_landing", __name__)


@security_bp.route("/security", methods=["GET"], strict_slashes=False)
def security():
    today = datetime.datetime.utcnow().strftime("%B %d, %Y")
    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Security & Data Handling | DC Hub</title>
<meta name="description" content="DC Hub security posture: infrastructure, data handling, retention, encryption, and customer obligations.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/security">
<meta property="og:title" content="Security & Data Handling — DC Hub">
<meta property="og:description" content="Infrastructure, encryption, data retention, and customer obligations.">
<style>
 body{{max-width:880px;margin:0 auto;padding:32px 24px;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.6;color:#0f172a}}
 h1{{font-size:2.1rem;margin:.3em 0;letter-spacing:-.02em}}
 h2{{font-size:1.25rem;margin:1.6em 0 .5em;color:#1e293b}}
 h3{{font-size:1.0rem;margin:1.1em 0 .3em;color:#334155}}
 .eyebrow{{color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;font-weight:600}}
 .lead{{color:#475569;font-size:1.05rem;max-width:780px}}
 .pane{{background:#f8fafc;border:1px solid #e2e8f0;padding:18px 22px;border-radius:10px;margin:16px 0}}
 .pane.warn{{background:#fef3c7;border-color:#fbbf24}}
 ul{{padding-left:22px}}
 li{{margin:.3em 0}}
 code{{background:#e0e7ff;color:#3730a3;padding:1px 6px;border-radius:3px;font-family:ui-monospace,monospace;font-size:.88em}}
 .pill{{display:inline-block;padding:2px 8px;border-radius:3px;font-size:.78rem;margin-left:6px;font-weight:600}}
 .pill.live{{background:#dcfce7;color:#15803d}}
 .pill.roadmap{{background:#fef3c7;color:#92400e}}
 .footer{{color:#64748b;font-size:.85rem;margin-top:30px;padding-top:18px;border-top:1px solid #e2e8f0}}
 .footer a{{color:#6366f1;text-decoration:none}}
</style></head><body>
<div class="eyebrow">DC Hub · Security & Data Handling</div>
<h1>Security & Data Posture</h1>
<p class="lead">DC Hub operates as a read-mostly intelligence platform: we ingest public data,
score it, and expose it via API/MCP. The security model below describes what we host, how we
handle the limited customer data we do touch, and what's still on the roadmap.</p>

<div class="pane warn">
<b>Plain English:</b> if you're evaluating us for enterprise use and need a security
questionnaire / DPA / SOC2 letter, email <a href="mailto:security@dchub.cloud">security@dchub.cloud</a>.
SOC2 Type 1 is in progress (target Q3 2026); we'll share the auditor's report when issued.
</div>

<h2>Infrastructure <span class="pill live">live</span></h2>
<ul>
  <li><b>Edge:</b> Cloudflare (Pages + Workers) for TLS termination, DDoS, WAF, bot blocking.</li>
  <li><b>API:</b> Flask + Gunicorn on Railway (US-West region). Single replica with health-checked
       restarts; multi-cloud failover to Render (cold standby).</li>
  <li><b>Database:</b> Neon Postgres. Encrypted at rest (AES-256), TLS in transit, branched
       per-environment.</li>
  <li><b>MCP server:</b> separate Node service on Railway, talks to API over TLS with a service
       token.</li>
  <li><b>Secrets:</b> Railway-managed env vars + Cloudflare Workers KV bindings — never committed.</li>
</ul>

<h2>Customer data we touch</h2>
<p>Three categories:</p>
<h3>1. API key + email (paid users)</h3>
<ul>
  <li>Stored in <code>mcp_dev_keys</code> table; key is hashed at rest, email plaintext.</li>
  <li>Used to: meter quota, send receipts, send winback emails for failed conversions.</li>
  <li>Retention: forever or until user requests deletion. Email
       <a href="mailto:privacy@dchub.cloud">privacy@dchub.cloud</a>.</li>
</ul>

<h3>2. Tool call logs</h3>
<ul>
  <li>Every MCP tool call writes a row in <code>mcp_call_log</code>: tool name, status, duration,
       hashed API key, optional referrer/UA.</li>
  <li>Used for: rate limiting, abuse detection, funnel analytics, transparency dashboard.</li>
  <li>Retention: rolling 90 days for query bodies; aggregate counts retained indefinitely.</li>
</ul>

<h3>3. Visitor IP enrichment</h3>
<ul>
  <li>Anonymous visits to <code>/api/*</code> get IPinfo lookup → company name + ASN.</li>
  <li>Used for: visitor analytics ("which Fortune 500 hit our MCP today?"), bot filtering.</li>
  <li>Stored under hashed-IP key, raw IPs not persisted past the request.</li>
</ul>

<h2>Public data we ingest</h2>
<p>The intelligence we sell is built entirely from public sources: EIA-860, HIFLD, FERC filings,
ISO public dashboards, PeeringDB, OSM, ArcGIS FeatureServers, public news feeds.
Nothing customer-private is incorporated into DCPI or any other surface.</p>

<h2>Audit posture</h2>
<table style="width:100%;border-collapse:collapse;margin:14px 0">
 <thead><tr style="background:#0f172a;color:#fff"><th style="text-align:left;padding:8px 12px;font-size:.82rem">Control</th><th style="text-align:left;padding:8px 12px;font-size:.82rem">Status</th></tr></thead>
 <tbody>
  <tr><td style="padding:10px 12px;border-top:1px solid #e2e8f0">TLS 1.2+ everywhere</td><td style="padding:10px 12px;border-top:1px solid #e2e8f0"><span class="pill live">live</span></td></tr>
  <tr><td style="padding:10px 12px;border-top:1px solid #e2e8f0">Encryption at rest (Neon, R2)</td><td style="padding:10px 12px;border-top:1px solid #e2e8f0"><span class="pill live">live</span></td></tr>
  <tr><td style="padding:10px 12px;border-top:1px solid #e2e8f0">DDoS + WAF (Cloudflare)</td><td style="padding:10px 12px;border-top:1px solid #e2e8f0"><span class="pill live">live</span></td></tr>
  <tr><td style="padding:10px 12px;border-top:1px solid #e2e8f0">CSP + security headers</td><td style="padding:10px 12px;border-top:1px solid #e2e8f0"><span class="pill live">live</span></td></tr>
  <tr><td style="padding:10px 12px;border-top:1px solid #e2e8f0">Rate limiting per-IP + per-tier</td><td style="padding:10px 12px;border-top:1px solid #e2e8f0"><span class="pill live">live</span></td></tr>
  <tr><td style="padding:10px 12px;border-top:1px solid #e2e8f0">Multi-cloud failover (Railway → Render)</td><td style="padding:10px 12px;border-top:1px solid #e2e8f0"><span class="pill live">live</span></td></tr>
  <tr><td style="padding:10px 12px;border-top:1px solid #e2e8f0">SOC2 Type 1</td><td style="padding:10px 12px;border-top:1px solid #e2e8f0"><span class="pill roadmap">Q3 2026</span></td></tr>
  <tr><td style="padding:10px 12px;border-top:1px solid #e2e8f0">SOC2 Type 2</td><td style="padding:10px 12px;border-top:1px solid #e2e8f0"><span class="pill roadmap">2027</span></td></tr>
  <tr><td style="padding:10px 12px;border-top:1px solid #e2e8f0">ISO 27001</td><td style="padding:10px 12px;border-top:1px solid #e2e8f0"><span class="pill roadmap">2027</span></td></tr>
  <tr><td style="padding:10px 12px;border-top:1px solid #e2e8f0">Penetration test (external)</td><td style="padding:10px 12px;border-top:1px solid #e2e8f0"><span class="pill roadmap">Q3 2026</span></td></tr>
 </tbody>
</table>

<h2>Reporting vulnerabilities</h2>
<p>Found something? Email <a href="mailto:security@dchub.cloud">security@dchub.cloud</a> with details.
We aim to acknowledge within 24 hours and fix within 7 days for confirmed issues. No bug bounty
program yet, but we'll credit you publicly (with your permission) on a future
<code>/security/hall-of-fame</code> page.</p>

<p class="footer">
<a href="/">Home</a> · <a href="/architecture">Architecture</a> · <a href="/transparency">Live ops</a>
· <a href="/pricing">Pricing</a> · Updated {today}
</p>
</body></html>"""
    return html, 200, {
        "Content-Type":  "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=900, s-maxage=3600",
        "X-DC-Phase":    "ZZZZZ-round47.4-security",
    }
