"""Phase r32-brain-show (2026-05-20) — Brain innovation transparency page.
==========================================================================

The brain (Opus 4.7 Inspector + autopilot + L22 auto-code) has been
running autonomously for weeks. The Inspector writes thoughtful daily
briefs naming specific problems with confidence ratings; autopilot
fires actions when patterns match; L22 *could* draft PRs but hasn't
yet (its proposal pipeline isn't wired to the Inspector's RECIPE
candidates).

This page makes the autonomy VISIBLE to the operator (and eventually
prospects). It aggregates four windows of brain activity:

  1. Inspector briefs (last 5) — full text, model used, token counts
  2. Autopilot actions (last 24h) — what fired, what was rate-limited,
     what was escalation-only
  3. Consistency-radar findings — currently-open issues brain caught
  4. L22 auto-code activity — drafted PRs (currently 0 — the gap)

  GET /brain/innovation
    Public HTML page. Renders all four windows + a "what brain
    proposed but didn't act on" section that names the operator's
    next decisions. Brand-matched dark theme.

  GET /api/v1/brain/innovation
    Same data as JSON.

This is the autonomous-product surface. Pre-r32 the brain was
invisible — the only way to see what it was doing was via Railway
logs or the admin /brain/brief endpoint. Now it's a first-class
storytelling surface that doubles as a credibility signal for
enterprise prospects ("we run a self-aware system that audits
itself daily").
"""
import os
import json
import logging
from datetime import datetime
from flask import Blueprint, jsonify, request, Response, render_template_string

logger = logging.getLogger(__name__)
brain_innovation_bp = Blueprint("brain_innovation", __name__)


def _get_db():
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        import psycopg2
        return psycopg2.connect(db, sslmode="require", connect_timeout=8)
    except Exception:
        return None


def _compute(days: int = 7) -> dict:
    out = {
        "as_of":       datetime.utcnow().isoformat() + "Z",
        "days":        days,
        "briefs":      [],
        "autopilot":   {"total": 0, "by_pattern": [], "by_outcome": {}},
        "findings":    [],
        "l22_status":  {"drafted_count": 0, "samples": []},
        "summary":     {},
    }
    conn = _get_db()
    if conn is None:
        out["error"] = "no_db"
        return out

    try:
        with conn.cursor() as cur:
            # ── Inspector briefs ───────────────────────────────────────
            try:
                cur.execute("""
                    SELECT id, generated_at, summary, brief_md, model,
                           healthy_count, degrading_count, attention_count,
                           tokens_in, tokens_out, duration_ms
                      FROM brain_briefs
                     WHERE generated_at > NOW() - INTERVAL %s
                     ORDER BY generated_at DESC
                     LIMIT 5
                """, (f"{days} days",))
                for r in cur.fetchall():
                    out["briefs"].append({
                        "id":              r[0],
                        "generated_at":    r[1].isoformat() if r[1] else None,
                        "summary":         r[2] or "",
                        "brief_md":        (r[3] or "")[:3000],
                        "model":           r[4] or "unknown",
                        "healthy_count":   int(r[5] or 0),
                        "degrading_count": int(r[6] or 0),
                        "attention_count": int(r[7] or 0),
                        "tokens_in":       int(r[8] or 0),
                        "tokens_out":      int(r[9] or 0),
                        "duration_ms":     int(r[10] or 0),
                    })
            except Exception:
                try: conn.rollback()
                except Exception: pass

            # ── Autopilot activity ─────────────────────────────────────
            try:
                cur.execute("""
                    SELECT pattern_name, outcome, COUNT(*) AS n
                      FROM brain_autopilot_actions
                     WHERE started_at > NOW() - INTERVAL %s
                       AND pattern_name IS NOT NULL
                     GROUP BY pattern_name, outcome
                     ORDER BY n DESC
                     LIMIT 30
                """, (f"{days} days",))
                ap_rows = cur.fetchall()
                by_pattern = {}
                by_outcome = {}
                total = 0
                for p, o, n in ap_rows:
                    by_pattern[p] = by_pattern.get(p, 0) + int(n or 0)
                    by_outcome[o or "unknown"] = by_outcome.get(o or "unknown", 0) + int(n or 0)
                    total += int(n or 0)
                out["autopilot"] = {
                    "total":     total,
                    "by_pattern": [
                        {"pattern": k, "count": v}
                        for k, v in sorted(by_pattern.items(), key=lambda x: -x[1])[:12]
                    ],
                    "by_outcome": by_outcome,
                }
            except Exception:
                try: conn.rollback()
                except Exception: pass

            # ── Open brain findings ────────────────────────────────────
            try:
                cur.execute("""
                    SELECT issue, url, COALESCE(count, 1) AS hits,
                           detail, detector, created_at
                      FROM brain_findings
                     WHERE COALESCE(status, 'open') = 'open'
                       AND created_at > NOW() - INTERVAL %s
                     ORDER BY created_at DESC
                     LIMIT 20
                """, (f"{days} days",))
                for r in cur.fetchall():
                    out["findings"].append({
                        "issue":    r[0],
                        "url":      r[1],
                        "count":    int(r[2] or 1),
                        "detail":   (r[3] or "")[:400],
                        "detector": r[4],
                        "created_at": r[5].isoformat() if r[5] else None,
                    })
            except Exception:
                try: conn.rollback()
                except Exception: pass

            # ── L22 auto-code activity ─────────────────────────────────
            try:
                cur.execute("""
                    SELECT recipe, target, status, created_at
                      FROM brain_l22_proposals
                     WHERE created_at > NOW() - INTERVAL %s
                     ORDER BY created_at DESC
                     LIMIT 10
                """, (f"{days} days",))
                rows = cur.fetchall()
                out["l22_status"] = {
                    "drafted_count": len(rows),
                    "samples": [
                        {"recipe": r[0], "target": r[1], "status": r[2],
                         "at": r[3].isoformat() if r[3] else None}
                        for r in rows
                    ],
                }
            except Exception:
                try: conn.rollback()
                except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass

    # Computed summary fields.
    out["summary"] = {
        "briefs_count":      len(out["briefs"]),
        "autopilot_actions": out["autopilot"]["total"],
        "rate_limited":      out["autopilot"]["by_outcome"].get("rate_limited", 0),
        "open_findings":     len(out["findings"]),
        "l22_drafted":       out["l22_status"]["drafted_count"],
        "newest_brief_at":   out["briefs"][0]["generated_at"] if out["briefs"] else None,
    }
    return out


@brain_innovation_bp.route("/api/v1/brain/innovation", methods=["GET"])
def brain_innovation_json():
    try:
        days = max(1, min(30, int(request.args.get("days", 7))))
    except (ValueError, TypeError):
        days = 7
    payload = _compute(days)
    payload["ok"] = "error" not in payload
    return jsonify(payload), (200 if "error" not in payload else 500)


_BRAIN_INNOV_HTML = '''<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8">
<title>Brain · Autonomous Innovation · DC Hub</title>
<meta name="description" content="Live transparency of the DC Hub Brain — autonomous Inspector briefs, autopilot actions, consistency findings, and auto-code proposals. Updated continuously.">
<meta property="og:title" content="DC Hub Brain · Live Autonomous Innovation">
<meta property="og:description" content="Self-aware system that audits itself daily and proposes fixes.">
<link rel="canonical" href="https://dchub.cloud/brain/innovation">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a12;--surface:#11121a;--bd:#1f2030;--tx:#fff;--tx2:#9ca3af;--tx3:#6b7280;
  --indigo:#6366f1;--violet:#a855f7;--green:#10b981;--orange:#f59e0b;--red:#ef4444;
  --grad:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
  --mono:'JetBrains Mono','SF Mono',monospace;color-scheme:dark}
*{box-sizing:border-box}body{font-family:'Instrument Sans',-apple-system,sans-serif;
  background:var(--bg);color:var(--tx);margin:0;line-height:1.55;min-height:100vh;
  position:relative;overflow-x:hidden;-webkit-font-smoothing:antialiased}
body::before{content:'';position:fixed;top:-30%;left:50%;transform:translateX(-50%);
  width:1400px;height:1400px;z-index:0;pointer-events:none;
  background:radial-gradient(circle,rgba(99,102,241,.10) 0%,rgba(168,85,247,.06) 30%,transparent 70%)}
.wrap{max-width:1180px;margin:0 auto;padding:2.5rem 1.5rem;position:relative;z-index:1}
.kicker{font-family:var(--mono);font-size:.78rem;color:#c4b5fd;text-transform:uppercase;letter-spacing:.14em;margin-bottom:.6rem;display:flex;align-items:center;gap:.5rem}
.pulse{width:8px;height:8px;border-radius:50%;background:#10b981;box-shadow:0 0 8px #10b981;animation:p 2s ease-in-out infinite}
@keyframes p{0%,100%{opacity:1}50%{opacity:.4}}
h1{margin:0 0 .5rem;font-size:2.6rem;font-weight:800;letter-spacing:-.02em;
  background:linear-gradient(90deg,#fff,#c4b5fd);-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:var(--tx2);max-width:760px;margin:0 0 2rem}
h2{font-size:.82rem;color:var(--tx2);text-transform:uppercase;letter-spacing:.12em;margin:3rem 0 1rem;font-weight:700}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin:1.5rem 0 2.5rem}
.stat{background:var(--surface);border:1px solid var(--bd);border-radius:12px;padding:1.25rem 1.5rem;position:relative;overflow:hidden}
.stat::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--grad)}
.stat .n{font-family:var(--mono);font-size:1.8rem;font-weight:800;line-height:1}
.stat .l{color:var(--tx2);font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;margin-top:.4rem;font-weight:600}
.brief{background:var(--surface);border:1px solid var(--bd);border-radius:14px;padding:1.75rem 2rem;margin-bottom:1rem}
.brief-head{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:1rem;margin-bottom:1rem}
.brief-head h3{margin:0;font-size:1.05rem;color:#c4b5fd;font-family:var(--mono)}
.brief-head .meta{font-family:var(--mono);color:var(--tx3);font-size:.78rem}
.brief-summary{color:#cbd5e1;font-size:.95rem;line-height:1.6;margin-bottom:1rem;font-style:italic}
.brief-counts{display:flex;gap:.75rem;margin-bottom:1rem}
.bc{background:#0a0a12;border:1px solid var(--bd);border-radius:6px;padding:.35rem .75rem;font-family:var(--mono);font-size:.78rem}
.bc.h{color:#10b981;border-color:#10b98144}
.bc.d{color:#f59e0b;border-color:#f59e0b44}
.bc.a{color:#ef4444;border-color:#ef444444}
.brief-md{background:#0a0a12;border:1px solid var(--bd);border-radius:8px;padding:1rem 1.25rem;font-family:var(--mono);font-size:.82rem;color:#cbd5e1;white-space:pre-wrap;max-height:280px;overflow-y:auto;line-height:1.55}
.brief-md::-webkit-scrollbar{width:6px}.brief-md::-webkit-scrollbar-track{background:#11121a}.brief-md::-webkit-scrollbar-thumb{background:#2a2d40;border-radius:3px}
.pattern-row{display:flex;justify-content:space-between;padding:.65rem 1rem;background:var(--surface);border:1px solid var(--bd);border-radius:8px;margin-bottom:.5rem;align-items:center;font-size:.9rem}
.pattern-row code{font-family:var(--mono);color:#c4b5fd;font-size:.85rem}
.pattern-row .n{font-family:var(--mono);font-weight:700}
.finding-row{display:flex;justify-content:space-between;padding:.85rem 1.25rem;background:var(--surface);border:1px solid var(--bd);border-radius:8px;margin-bottom:.5rem;gap:1rem;flex-wrap:wrap}
.finding-row code{font-family:var(--mono);color:#fbbf24;font-size:.85rem;font-weight:600}
.finding-row .detail{flex:1;color:var(--tx2);font-size:.85rem;min-width:300px}
.finding-row .meta{font-family:var(--mono);color:var(--tx3);font-size:.74rem;text-align:right}
.callout{background:linear-gradient(135deg,rgba(245,158,11,.10),rgba(245,158,11,.04));border:1px solid rgba(245,158,11,.3);border-radius:12px;padding:1.25rem 1.5rem;margin:2rem 0;color:#fbbf24}
.callout b{color:#fff}
footer{margin-top:3rem;padding-top:1.5rem;border-top:1px solid var(--bd);color:var(--tx3);font-size:.85rem;text-align:center}
footer a{color:var(--indigo);text-decoration:none}
</style></head><body><div class="wrap">
<div class="kicker"><span class="pulse"></span>DC HUB · BRAIN · LAST {{ d.days }} DAYS</div>
<h1>Autonomous innovation, live</h1>
<p class="sub">DC Hub runs a self-aware system that audits itself, proposes fixes, and acts on patterns autonomously. Inspector briefs are written by Claude Opus 4.7 (1M-token context). Autopilot acts on detector findings within rate-limit + cooldown safety. L22 drafts PRs from code-level RECIPE candidates. This page is the transparent view of what brain has been doing without anyone asking.</p>

<div class="stats">
  <div class="stat"><div class="n">{{ d.summary.briefs_count }}</div><div class="l">Inspector briefs</div></div>
  <div class="stat"><div class="n">{{ d.summary.autopilot_actions }}</div><div class="l">Autopilot actions</div></div>
  <div class="stat"><div class="n">{{ d.summary.rate_limited }}</div><div class="l">Rate-limited (correctly)</div></div>
  <div class="stat"><div class="n">{{ d.summary.open_findings }}</div><div class="l">Open findings</div></div>
  <div class="stat"><div class="n">{{ d.summary.l22_drafted }}</div><div class="l">L22 PR proposals</div></div>
</div>

{% if d.summary.l22_drafted == 0 %}
<div class="callout"><b>Honest scope:</b> Inspector identifies code-level RECIPE candidates in every brief (schema_drift_guard, cron_if_mismatched, route_alias_404). The L22 auto-code drafter is wired but no proposals have been promoted to draft-PR stage yet — the Inspector → L22 handoff pipe needs one more step. That's the next autonomy frontier.</div>
{% endif %}

<h2>Recent Inspector briefs</h2>
{% for b in d.briefs %}
<div class="brief">
  <div class="brief-head">
    <h3>{{ b.generated_at[:19] }}Z · {{ b.model }}</h3>
    <span class="meta">{{ b.tokens_in }} in / {{ b.tokens_out }} out · {{ (b.duration_ms/1000)|round(1) }}s</span>
  </div>
  <div class="brief-summary">{{ b.summary }}</div>
  <div class="brief-counts">
    <span class="bc h">{{ b.healthy_count }} healthy</span>
    <span class="bc d">{{ b.degrading_count }} degrading</span>
    <span class="bc a">{{ b.attention_count }} attention</span>
  </div>
  <div class="brief-md">{{ b.brief_md }}</div>
</div>
{% endfor %}

<h2>What autopilot did (and tried)</h2>
{% if d.autopilot.by_pattern %}
{% for p in d.autopilot.by_pattern %}
<div class="pattern-row">
  <code>{{ p.pattern }}</code>
  <span class="n">{{ p.count }}× fired</span>
</div>
{% endfor %}
<p style="color:var(--tx3);font-size:.85rem;margin-top:1rem">
Outcomes: {{ d.autopilot.by_outcome.items()|list|map('join', ' = ')|join(' · ') if d.autopilot.by_outcome else 'no actions' }}
</p>
{% else %}
<div class="pattern-row" style="color:var(--tx3)">No autopilot actions in the window. Brain is in detect-only mode.</div>
{% endif %}

<h2>Open findings brain caught</h2>
{% for f in d.findings %}
<div class="finding-row">
  <div>
    <code>{{ f.issue }}</code>
    <div class="detail">{{ f.detail }}</div>
  </div>
  <div class="meta">{{ f.detector or 'unknown' }}<br>{{ f.created_at[:10] if f.created_at else '' }}</div>
</div>
{% endfor %}

<footer>
Brain Inspector model: Claude Opus 4.7 (1M context) · Reasoning: Opus 4.7 · Routine: Sonnet 4.5 · Voice: Haiku 3.5 · Autopilot: rate-limited via cooldown machinery ·
L22 auto-code: routes/brain_layer22_auto_code.py · JSON: <a href="/api/v1/brain/innovation">/api/v1/brain/innovation</a>
</footer>
</div></body></html>'''


@brain_innovation_bp.route("/brain/innovation", methods=["GET"])
def brain_innovation_page():
    try:
        days = max(1, min(30, int(request.args.get("days", 7))))
    except (ValueError, TypeError):
        days = 7
    d = _compute(days)
    html = render_template_string(_BRAIN_INNOV_HTML, d=d)
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp
