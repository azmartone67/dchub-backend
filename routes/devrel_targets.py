"""Phase r32-devrel (2026-05-20) — client-based DevRel target surface.
==========================================================================

We can't email the 15,837 anonymous MCP callers because LLM proxies
strip identity. But we CAN see WHICH platforms (Claude / ChatGPT /
Perplexity / Gemini / Copilot / Cursor) are sending the traffic.
That's a DevRel surface — each platform has community channels,
Discord servers, GPT/agent stores, and Cursor/Copilot extension
catalogs where we can position DC Hub at the agent layer instead
of trying to find the human.

  GET /api/v1/admin/devrel-targets
    Aggregates anonymous MCP signals by mcp_client + tool. Each
    row carries:
      - Platform name + total signal volume
      - Top 3 tools that platform is hitting
      - A pre-drafted DevRel pitch tuned to that platform's surface:
          Claude → Anthropic MCP catalog submission template
          ChatGPT → GPT Store description draft
          Perplexity → Pages content pitch
          Gemini → Gemini Extensions positioning
          Cursor → Cursor extensions marketplace pitch
          Copilot → Microsoft DevHub directory

  GET /devrel-targets
    HTML page rendering the matrix + copy-to-clipboard buttons for
    each draft pitch.

This is the actionable companion to /visitor-intelligence — the
dashboard tells David WHO is visiting; this tells the operator
WHAT to do with that information at the platform level.
"""
import os
import logging
from datetime import datetime
from flask import Blueprint, jsonify, request, Response, render_template_string

logger = logging.getLogger(__name__)
devrel_targets_bp = Blueprint("devrel_targets", __name__)


_INTERNAL_KEYS: set = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY",
           "ADMIN_API_KEY", "ADMIN_SECRET"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key")
            or request.cookies.get("dchub_admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


# Per-platform DevRel surface templates. The operator can paste-and-tune
# directly — each is specific to that platform's submission/listing flow.
PLATFORM_BLUEPRINTS = {
    "claude": {
        "label": "Claude / Anthropic",
        "surface": "Claude MCP catalog + Anthropic community",
        "submission_url": "https://claude.ai/mcp",
        "draft_title": "DC Hub — Data Center Intelligence MCP",
        "draft_pitch": (
            "DC Hub gives Claude real-time data center market intelligence: "
            "20,000+ facilities, ISO grid headroom, M&A transactions, fiber "
            "routes, site scoring. Free dev key with email only. The MCP server "
            "at https://dchub.cloud/mcp is open and stable — your callers "
            "({signal_count} hits in the last 30 days from Claude alone) are "
            "already finding it. Add the formal listing so we can route human "
            "users through the upgrade path."
        ),
        "next_action": "Submit to Claude MCP directory + post to r/ClaudeAI",
    },
    "chatgpt": {
        "label": "ChatGPT / OpenAI",
        "surface": "GPT Store + OpenAI dev forum",
        "submission_url": "https://chat.openai.com/gpts/editor",
        "draft_title": "DC Hub — Data Center Markets GPT",
        "draft_pitch": (
            "Custom GPT spec: builds on DC Hub MCP for real-time data center "
            "intelligence. Use cases: site selection, deal research, capacity "
            "forecasting, ISO grid analysis. {signal_count} ChatGPT-origin "
            "calls / 30d shows the demand is there — a formal GPT makes the "
            "upgrade path clickable inside the chat interface. Free dev key, "
            "no card."
        ),
        "next_action": "Build a 'DC Hub Markets' GPT in the editor, link to /redeem",
    },
    "perplexity": {
        "label": "Perplexity",
        "surface": "Perplexity Pages + Discover feed",
        "submission_url": "https://www.perplexity.ai/hub/pages",
        "draft_title": "Where to put 50MW: a live DCPI ranking",
        "draft_pitch": (
            "Build a Perplexity Page on data center site selection that cites "
            "DC Hub's live DCPI rankings for every major US market. The "
            "{signal_count} Perplexity-origin hits / 30d mean users are "
            "already pulling DC Hub data via search — the Page format lets "
            "them go deeper. Each section links to /pockets/<slug> detail "
            "pages we shipped in r31."
        ),
        "next_action": "Publish 3 Perplexity Pages: PJM pockets, ERCOT pockets, CAISO pockets",
    },
    "gemini": {
        "label": "Gemini / Google",
        "surface": "Gemini Extensions + Google AI Studio",
        "submission_url": "https://ai.google.dev/gemini-api/docs/extensions",
        "draft_title": "DC Hub Gemini Extension",
        "draft_pitch": (
            "Wrap DC Hub MCP as a Gemini Extension. {signal_count} Gemini-"
            "origin calls / 30d shows organic demand without listing. "
            "Extension lets Gemini users invoke DC Hub natively in any chat "
            "without manual setup. Free dev key tier matches Gemini's free "
            "tier so no friction."
        ),
        "next_action": "Submit Gemini Extension manifest pointing at /mcp",
    },
    "copilot": {
        "label": "GitHub Copilot / Microsoft",
        "surface": "VS Code Marketplace + GitHub Apps",
        "submission_url": "https://marketplace.visualstudio.com/manage",
        "draft_title": "DC Hub Copilot Tools",
        "draft_pitch": (
            "Copilot Chat extension that pulls DC Hub MCP tools into VS Code "
            "for developers working on data center infrastructure code "
            "(Terraform, k8s on metal, etc.). {signal_count} Copilot-origin "
            "calls / 30d already; an extension makes it discoverable in "
            "the marketplace."
        ),
        "next_action": "Publish VS Code extension wrapping the DC Hub MCP tools",
    },
    "cursor": {
        "label": "Cursor",
        "surface": "Cursor MCP catalog",
        "submission_url": "https://cursor.sh/mcp",
        "draft_title": "DC Hub MCP for Cursor",
        "draft_pitch": (
            "Cursor agents are pulling DC Hub data ({signal_count} hits / 30d) "
            "without a formal listing. Add to Cursor's MCP catalog so the "
            "configuration is one-click for new users. Free dev key tier "
            "matches Cursor's free tier — zero card friction."
        ),
        "next_action": "Submit DC Hub MCP server card to Cursor catalog",
    },
}


def _classify_client(mcp_client: str, user_agent: str) -> str:
    """Map raw mcp_client / user_agent strings to the platform key in
    PLATFORM_BLUEPRINTS. Fallback to 'unknown' so we don't lose volume."""
    if not mcp_client and not user_agent:
        return "unknown"
    blob = ((mcp_client or "") + " " + (user_agent or "")).lower()
    for key in PLATFORM_BLUEPRINTS:
        if key in blob:
            return key
    if "anthropic" in blob: return "claude"
    if "openai" in blob:    return "chatgpt"
    if "bing" in blob:      return "copilot"
    return "unknown"


def _compute(days: int = 30) -> dict:
    """Aggregate paywall signals by platform-classified mcp_client.
    Returns one row per platform with volume, top tools, draft pitch."""
    out = {
        "as_of": datetime.utcnow().isoformat() + "Z",
        "days":  days,
        "targets": [],
        "total_signals": 0,
        "total_classified": 0,
    }
    db = os.environ.get("DATABASE_URL")
    if not db: return out
    try:
        import psycopg2
        with psycopg2.connect(db, sslmode="require", connect_timeout=8) as c, \
             c.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(NULLIF(mcp_client, ''), '') AS mcp_client,
                       COALESCE(NULLIF(user_agent, ''), '') AS user_agent,
                       tool_requested,
                       COUNT(*) AS signal_count,
                       COUNT(DISTINCT session_id) AS sessions
                  FROM mcp_upgrade_signals
                 WHERE created_at > NOW() - INTERVAL %s
                 GROUP BY 1, 2, 3
            """, (f"{days} days",))
            rows = cur.fetchall()
    except Exception as e:
        out["error"] = str(e)[:200]
        return out

    # Bucket each row into a platform.
    buckets: dict = {}
    total = 0
    classified = 0
    for r in rows:
        mcp_client, ua, tool, count, sessions = r
        plat = _classify_client(mcp_client, ua)
        if plat not in buckets:
            buckets[plat] = {
                "platform": plat,
                "signal_count": 0,
                "sessions": 0,
                "tools": {},
            }
        buckets[plat]["signal_count"] += int(count or 0)
        buckets[plat]["sessions"] += int(sessions or 0)
        if tool:
            buckets[plat]["tools"][tool] = (
                buckets[plat]["tools"].get(tool, 0) + int(count or 0)
            )
        total += int(count or 0)
        if plat != "unknown":
            classified += int(count or 0)

    # Render each bucket with the pitch.
    rendered = []
    for plat, data in buckets.items():
        top_tools = sorted(
            data["tools"].items(), key=lambda kv: -kv[1]
        )[:3]
        blueprint = PLATFORM_BLUEPRINTS.get(plat, {
            "label": "Unknown / unclassified",
            "surface": "—",
            "submission_url": "",
            "draft_title": "",
            "draft_pitch": "",
            "next_action": "Classify the user_agent strings — these aren't matching any known platform.",
        })
        pitch = blueprint["draft_pitch"].format(signal_count=data["signal_count"]) if blueprint["draft_pitch"] else ""
        rendered.append({
            "platform":      plat,
            "label":         blueprint["label"],
            "surface":       blueprint["surface"],
            "submission_url": blueprint["submission_url"],
            "signal_count":  data["signal_count"],
            "sessions":      data["sessions"],
            "top_tools":     [{"tool": t, "hits": h} for t, h in top_tools],
            "draft_title":   blueprint["draft_title"],
            "draft_pitch":   pitch,
            "next_action":   blueprint["next_action"],
        })
    rendered.sort(key=lambda r: -r["signal_count"])

    out["targets"] = rendered
    out["total_signals"] = total
    out["total_classified"] = classified
    out["classification_rate"] = (
        f"{classified / total * 100:.1f}%" if total else "0%"
    )
    return out


@devrel_targets_bp.route("/api/v1/admin/devrel-targets", methods=["GET"])
def devrel_targets_json():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    try:
        days = max(1, min(90, int(request.args.get("days", 30))))
    except (ValueError, TypeError):
        days = 30
    return jsonify(_compute(days)), 200


_DEVREL_HTML = '''<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8">
<title>DevRel Targets · DC Hub</title>
<meta name="robots" content="noindex,nofollow">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a12;--surface:#11121a;--bd:#1f2030;--tx:#fff;--tx2:#9ca3af;
  --indigo:#6366f1;--violet:#a855f7;--green:#10b981;--orange:#f59e0b;
  --mono:'JetBrains Mono','SF Mono',monospace;color-scheme:dark}
*{box-sizing:border-box}body{font-family:'Instrument Sans',-apple-system,sans-serif;
  background:var(--bg);color:var(--tx);margin:0;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1200px;margin:0 auto;padding:2.5rem 1.5rem}
.kicker{font-family:var(--mono);font-size:.78rem;color:#c4b5fd;text-transform:uppercase;letter-spacing:.14em;margin-bottom:.6rem}
h1{margin:0 0 .5rem;font-size:2.2rem;font-weight:800;letter-spacing:-.02em;
  background:linear-gradient(90deg,#fff,#c4b5fd);-webkit-background-clip:text;
  background-clip:text;color:transparent}
.sub{color:var(--tx2);max-width:760px;margin:0 0 2rem}
h2{font-size:.78rem;color:var(--tx2);text-transform:uppercase;letter-spacing:.12em;margin:2.5rem 0 1rem;font-weight:700}
.totals{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin-bottom:2rem}
.totals .stat{background:var(--surface);border:1px solid var(--bd);border-radius:10px;padding:1rem 1.4rem}
.totals .stat .n{font-family:var(--mono);font-size:1.7rem;font-weight:800;line-height:1}
.totals .stat .l{color:var(--tx2);font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;margin-top:.4rem}
.card{background:var(--surface);border:1px solid var(--bd);border-radius:14px;padding:1.75rem 2rem;margin-bottom:1rem}
.card-head{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:1rem;margin-bottom:1rem}
.card-head h3{margin:0;font-size:1.2rem;font-weight:700}
.card-head .vol{font-family:var(--mono);color:#c4b5fd;font-size:.85rem;font-weight:600}
.card-surface{font-size:.85rem;color:var(--tx2);margin-bottom:1rem}
.tools{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1rem}
.tool{background:rgba(99,102,241,.12);border:1px solid rgba(99,102,241,.3);border-radius:6px;padding:.3rem .7rem;font-family:var(--mono);font-size:.78rem}
.tool b{color:#fff;margin-right:.4rem}
.pitch{background:#0a0a12;border:1px solid var(--bd);border-radius:8px;padding:1rem 1.25rem;font-size:.92rem;color:#cbd5e1;line-height:1.55;white-space:pre-wrap;margin-bottom:1rem}
.actions{display:flex;gap:.5rem;flex-wrap:wrap}
.actions a,.actions button{display:inline-block;padding:.55rem 1.1rem;border-radius:6px;font-size:.84rem;font-weight:600;text-decoration:none;cursor:pointer;border:0;font-family:'Instrument Sans',sans-serif}
.actions a.primary{background:linear-gradient(135deg,#6366f1,#a855f7);color:#fff}
.actions button.copy{background:#1f2030;color:#cbd5e1;border:1px solid #2a2d40}
.next{margin-top:.75rem;padding-top:.75rem;border-top:1px solid var(--bd);font-size:.88rem;color:var(--green)}
.next b{color:var(--tx)}
</style></head><body><div class="wrap">
<div class="kicker">DC HUB · DEVREL TARGETS · {{ d.days }}D</div>
<h1>Where to land DC Hub at the platform layer</h1>
<p class="sub">Anonymous LLM-proxy traffic can't be email-outreached. But we know WHICH platform sent each signal — Claude, ChatGPT, Perplexity, Gemini, Copilot, Cursor — and each has a DevRel surface (catalogs, marketplaces, community channels) where we can position DC Hub at the agent layer. Pitches pre-drafted with live signal volume.</p>

<div class="totals">
  <div class="stat"><div class="n">{{ d.total_signals }}</div><div class="l">Total signals</div></div>
  <div class="stat"><div class="n">{{ d.total_classified }}</div><div class="l">Platform-classified</div></div>
  <div class="stat"><div class="n">{{ d.classification_rate }}</div><div class="l">Classification rate</div></div>
  <div class="stat"><div class="n">{{ d.targets|length }}</div><div class="l">Platforms</div></div>
</div>

{% for t in d.targets %}
<div class="card">
  <div class="card-head">
    <h3>{{ t.label }}</h3>
    <span class="vol">{{ t.signal_count }} signals · {{ t.sessions }} sessions</span>
  </div>
  <div class="card-surface"><b>Surface:</b> {{ t.surface }}</div>
  {% if t.top_tools %}
  <div class="tools">
    {% for tt in t.top_tools %}<div class="tool"><b>{{ tt.tool }}</b>{{ tt.hits }} hits</div>{% endfor %}
  </div>
  {% endif %}
  {% if t.draft_pitch %}
  <div class="pitch" id="pitch-{{ t.platform }}">{{ t.draft_pitch }}</div>
  <div class="actions">
    {% if t.submission_url %}<a class="primary" href="{{ t.submission_url }}" target="_blank" rel="noopener">Open {{ t.label }} →</a>{% endif %}
    <button class="copy" onclick="cp('pitch-{{ t.platform }}')">Copy pitch</button>
  </div>
  {% endif %}
  <div class="next"><b>Next action:</b> {{ t.next_action }}</div>
</div>
{% endfor %}

<script>
function cp(id){
  const t = document.getElementById(id).innerText;
  navigator.clipboard.writeText(t).then(()=>{
    event.target.textContent = 'Copied ✓';
    setTimeout(()=>{ event.target.textContent = 'Copy pitch'; }, 1200);
  });
}
</script>
</div></body></html>'''


@devrel_targets_bp.route("/devrel-targets", methods=["GET"])
def devrel_targets_page():
    if not _admin_ok():
        return Response(
            "<!DOCTYPE html><html><body style='font-family:system-ui;"
            "background:#0a0a12;color:#fff;padding:3rem;text-align:center'>"
            "<h1>Login first</h1><p>Visit "
            "<a href='/visitor-intelligence/auth?key=YOUR_KEY' style='color:#6366f1'>"
            "/visitor-intelligence/auth</a> to set the admin cookie."
            "</p></body></html>",
            status=401, mimetype="text/html",
        )
    try:
        days = max(1, min(90, int(request.args.get("days", 30))))
    except (ValueError, TypeError):
        days = 30
    d = _compute(days)
    html = render_template_string(_DEVREL_HTML, d=d)
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp
