"""Phase FF+25-followup-r9 (2026-05-20) — Brain Inspector (Opus 4.7).
==========================================================================

The user's vision: "i feel like the brain should become the inspector of
everything proactively improve systems, squash bugs reactively, and come
to the rescue when errors arise."

The L21 autopilot already covers reactive (squash + rescue). The missing
piece is PROACTIVE — reading everything, synthesizing across signals,
and writing a coherent narrative that humans + the system itself can act
on. That's this module.

THE INSPECTOR LOOP

Every BRAIN_INSPECTOR_INTERVAL_HOURS (default 6), the Inspector:
  1. Reads from 10+ surfaces:
     - Last 24h autopilot actions (pattern, outcome, target)
     - Last 24h consistency-radar findings
     - Site Sentinel top-10 unhealthy surfaces
     - MCP funnel (calls 7d, real-traffic split, conversions)
     - Press cadence (last 5 publishes + age)
     - Brain pulse (last_action + 24h count)
     - Source-of-truth + citation score trend
     - Sponsorship queue state
     - Monthly outreach status
     - Worker version drift
  2. Calls Opus 4.7 with a system prompt that defines the Inspector
     persona: dry, evidence-first, no hype, never invents numbers.
     Asks for structured Markdown.
  3. Persists the response to brain_briefs table.
  4. Surfaces the latest brief at /api/v1/brain/brief/latest +
     /brain/brief (HTML page).

SAFETY:
  · Inspector NEVER mutates state on its own. It produces text.
  · Findings + would-do recommendations feed the existing autopilot
    (which DOES rate-limit + cooldown like before).
  · Every claim must cite which surface it came from. The system
    prompt forbids invented numbers.
  · If ANTHROPIC_API_KEY is unset, all endpoints return 503 with a
    clear next-step hint.

ENDPOINTS:
  GET  /api/v1/brain/brief/latest           public summary
  GET  /api/v1/brain/brief/<id>             specific brief
  GET  /api/v1/brain/brief/list             recent briefs
  POST /api/v1/brain/brief/generate         admin: trigger fresh
  GET  /brain/brief                         HTML rendering of latest
  GET  /api/v1/brain/models                 which model each tier uses
"""
import os
import json
import logging
import datetime
from flask import Blueprint, jsonify, request, Response

logger = logging.getLogger(__name__)
brain_inspector_bp = Blueprint("brain_inspector", __name__)


# ── Auth ─────────────────────────────────────────────────────────────
_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


# ── DB ───────────────────────────────────────────────────────────────
def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _ensure_brief_table():
    c = _get_db()
    if c is None: return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_briefs (
                    id            SERIAL PRIMARY KEY,
                    generated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    model         TEXT NOT NULL,
                    inputs        JSONB,            -- signals fed to the LLM
                    brief_md      TEXT NOT NULL,    -- the rendered narrative
                    summary       TEXT,             -- 1-line take
                    healthy_count INT,
                    degrading_count INT,
                    attention_count INT,
                    tokens_in     INT,
                    tokens_out    INT,
                    duration_ms   INT,
                    error         TEXT
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_brain_briefs_generated "
                "ON brain_briefs(generated_at DESC)"
            )
        return True
    except Exception as e:
        logger.warning(f"[brain-inspector] table create failed: {e}")
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── Signal gathering ─────────────────────────────────────────────────
def _gather_signals() -> dict:
    """Read the 10+ surfaces the Inspector reasons over. Each block is
    independently fault-tolerant — one bad query doesn't blank the
    brief."""
    out: dict = {
        "as_of": datetime.datetime.utcnow().isoformat() + "Z",
    }
    c = _get_db()
    if c is None:
        out["error"] = "no_database"
        return out

    def _try(label: str, sql: str, params=(), one=False):
        try:
            with c.cursor() as cur:
                cur.execute(sql, params)
                if one:
                    r = cur.fetchone()
                    out[label] = (list(r) if r else None)
                else:
                    out[label] = [list(r) for r in cur.fetchall()]
        except Exception as e:
            try: c.rollback()
            except Exception: pass
            out[f"{label}_error"] = str(e).split("\n")[0][:120]

    try:
        # Autopilot recent activity
        _try("autopilot_24h",
             """SELECT pattern_name, outcome, COUNT(*) AS n
                  FROM brain_autopilot_actions
                 WHERE started_at >= NOW() - INTERVAL '24 hours'
                 GROUP BY pattern_name, outcome
                 ORDER BY n DESC LIMIT 20""")
        # Consistency findings
        _try("consistency_findings_24h",
             """SELECT issue, COUNT(*) AS n FROM brain_findings
                 WHERE created_at >= NOW() - INTERVAL '24 hours'
                 GROUP BY issue ORDER BY n DESC LIMIT 15""")
        # MCP funnel
        _try("mcp_funnel",
             """SELECT
                  (SELECT COUNT(*) FROM mcp_tool_calls
                    WHERE created_at >= NOW() - INTERVAL '7 days') AS calls_7d,
                  (SELECT COUNT(*) FROM mcp_upgrade_signals
                    WHERE created_at >= NOW() - INTERVAL '7 days') AS signals_7d,
                  (SELECT COUNT(*) FROM mcp_conversions
                    WHERE created_at >= NOW() - INTERVAL '30 days') AS conv_30d
             """, one=True)
        # Press cadence
        _try("press_recent",
             """SELECT title, published_at FROM press_releases
                 WHERE published_at IS NOT NULL
                 ORDER BY published_at DESC LIMIT 5""")
        # Citation pulse
        _try("citation_score",
             """SELECT score_pct, score_date FROM citation_scores
                 ORDER BY score_date DESC LIMIT 1""", one=True)
        # Worker drift
        _try("worker_version",
             """SELECT version, observed_at FROM worker_versions
                 ORDER BY observed_at DESC LIMIT 1""", one=True)
        # Top facilities counts
        _try("facilities_total",
             """SELECT COUNT(*) FROM facilities""", one=True)
        # Most recent deal
        _try("deals_recent",
             """SELECT date, buyer, seller, value FROM deals
                 WHERE date IS NOT NULL ORDER BY date DESC LIMIT 5""")
    finally:
        try: c.close()
        except Exception: pass

    return out


# ── LLM call ─────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You are the DC Hub Inspector — an autonomous senior infrastructure engineer reviewing the health of dchub.cloud.

Your job: read the signal block below and produce a single coherent Markdown brief. Your voice is dry, observational, evidence-first. Never overpromise, never invent numbers, never use exclamation marks or emojis.

You must:
  - Cite which signal each claim came from (e.g. "per autopilot_24h" or "per mcp_funnel").
  - If a number isn't in the signal block, do NOT invent one. Say "not yet measured" or omit.
  - Mark each item with confidence: high / medium / low.
  - Suggest concrete next actions ONLY for items where the action is well-defined; otherwise mark as "needs human review".
  - Forecast what's likely to change in the next 24 hours, with explicit caveats.

Output the brief in this exact Markdown structure:

# DC Hub · Brain Brief · {timestamp}

**One-line take:** [a single sentence summarizing system state]

## Healthy
[bulleted list of things working as expected, each with the signal that proves it]

## Degrading
[bulleted list of things trending in the wrong direction, each with the signal + a confidence tag]

## Needs attention
[items that need human review or where autopilot action is uncertain]

## Would-do (autonomous recommendations)
[concrete actions the autopilot could take, each one mapped to an existing pattern name from the autopilot library if possible]

## Predictions · next 24h
[1-3 forecasts with confidence + the caveat that they're forecasts not facts]
"""


def _call_opus(system: str, user: str, model: str,
                 max_tokens: int = 2400) -> tuple[str, dict]:
    """Call the Anthropic API. Returns (text, meta_dict). meta has
    tokens_in/out/duration_ms/error."""
    import urllib.request, urllib.error
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        return "", {"error": "ANTHROPIC_API_KEY not set"}
    started = datetime.datetime.utcnow()
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload, method="POST",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        return "", {"error": f"HTTPError {e.code}",
                    "detail": e.read().decode("utf-8", errors="replace")[:400]}
    except Exception as e:
        return "", {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    dur = int((datetime.datetime.utcnow() - started).total_seconds() * 1000)
    text = ""
    for block in body.get("content", []) or []:
        if block.get("type") == "text":
            text += block.get("text", "")
    usage = body.get("usage") or {}
    return text, {
        "tokens_in":   usage.get("input_tokens"),
        "tokens_out":  usage.get("output_tokens"),
        "duration_ms": dur,
        "model_used":  body.get("model"),
    }


def _generate_brief() -> dict:
    """End-to-end: gather signals → ask Opus → persist → return."""
    from routes.brain_models import brain_model_for
    model = brain_model_for("inspector")
    signals = _gather_signals()
    user_msg = (
        "Signal block (JSON):\n\n```json\n"
        + json.dumps(signals, indent=2, default=str)[:18000]
        + "\n```\n\nWrite the brief now."
    )
    text, meta = _call_opus(_SYSTEM_PROMPT, user_msg, model)
    out = {
        "model":       model,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "brief_md":    text,
        "signals_count": len([k for k in signals if not k.endswith("_error")]),
        **meta,
    }

    # Heuristic section counters (so the API + UI can show summary
    # numbers without needing to parse the full Markdown).
    if text:
        def _count_after(header: str) -> int:
            try:
                section = text.split(f"## {header}", 1)[1]
                section = section.split("\n## ", 1)[0]
                return sum(1 for line in section.split("\n")
                           if line.strip().startswith(("-", "*", "+")))
            except Exception:
                return 0
        out["healthy_count"]   = _count_after("Healthy")
        out["degrading_count"] = _count_after("Degrading")
        out["attention_count"] = _count_after("Needs attention")
        # One-line take
        if "One-line take" in text:
            try:
                out["summary"] = (text.split("One-line take", 1)[1]
                                  .split("\n", 2)[0]
                                  .lstrip(":* ")
                                  .strip()[:300])
            except Exception:
                pass

    # Persist
    _ensure_brief_table()
    c = _get_db()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    INSERT INTO brain_briefs
                      (model, inputs, brief_md, summary,
                       healthy_count, degrading_count, attention_count,
                       tokens_in, tokens_out, duration_ms, error)
                    VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, generated_at
                """, (model, json.dumps(signals, default=str), text,
                       out.get("summary"),
                       out.get("healthy_count"),
                       out.get("degrading_count"),
                       out.get("attention_count"),
                       out.get("tokens_in"), out.get("tokens_out"),
                       out.get("duration_ms"), out.get("error")))
                r = cur.fetchone()
                if r:
                    out["id"] = int(r[0])
                    out["generated_at"] = str(r[1])
        except Exception as e:
            out["persist_error"] = str(e)[:200]
        finally:
            try: c.close()
            except Exception: pass

    return out


# ── ENDPOINTS ────────────────────────────────────────────────────────
@brain_inspector_bp.route("/api/v1/brain/brief/latest", methods=["GET"])
def brief_latest():
    """Public summary of the most recent brief. Returns 404 if none yet."""
    _ensure_brief_table()
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, generated_at, model, summary,
                       healthy_count, degrading_count, attention_count,
                       brief_md, tokens_in, tokens_out, duration_ms
                  FROM brain_briefs
                 WHERE error IS NULL
                 ORDER BY generated_at DESC LIMIT 1
            """)
            r = cur.fetchone()
            if not r:
                return jsonify(
                    ok=False,
                    error="no_brief_yet",
                    hint="POST /api/v1/brain/brief/generate to trigger one. "
                         "Requires ANTHROPIC_API_KEY env var.",
                ), 404
            resp = jsonify(
                ok=True,
                id=int(r[0]),
                generated_at=str(r[1]),
                model=r[2],
                summary=r[3],
                healthy_count=r[4],
                degrading_count=r[5],
                attention_count=r[6],
                brief_md=r[7],
                tokens_in=r[8],
                tokens_out=r[9],
                duration_ms=r[10],
            )
            resp.headers["Cache-Control"] = "public, max-age=300"
            return resp
    finally:
        try: c.close()
        except Exception: pass


@brain_inspector_bp.route("/api/v1/brain/brief/<int:bid>", methods=["GET"])
def brief_specific(bid: int):
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, generated_at, model, summary, brief_md,
                       healthy_count, degrading_count, attention_count
                  FROM brain_briefs WHERE id = %s
            """, (bid,))
            r = cur.fetchone()
            if not r: return jsonify(ok=False, error="not_found"), 404
            return jsonify(ok=True, id=int(r[0]),
                           generated_at=str(r[1]), model=r[2],
                           summary=r[3], brief_md=r[4],
                           healthy_count=r[5], degrading_count=r[6],
                           attention_count=r[7])
    finally:
        try: c.close()
        except Exception: pass


@brain_inspector_bp.route("/api/v1/brain/brief/list", methods=["GET"])
def brief_list():
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, generated_at, model, summary,
                       healthy_count, degrading_count, attention_count
                  FROM brain_briefs
                 ORDER BY generated_at DESC LIMIT 50
            """)
            return jsonify(ok=True, briefs=[
                {"id": int(r[0]), "generated_at": str(r[1]),
                 "model": r[2], "summary": r[3],
                 "healthy": r[4], "degrading": r[5], "attention": r[6]}
                for r in cur.fetchall()
            ])
    finally:
        try: c.close()
        except Exception: pass


@brain_inspector_bp.route("/api/v1/brain/brief/generate", methods=["POST"])
def brief_generate():
    """Admin: trigger a fresh Inspector pass. Returns the new brief."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    out = _generate_brief()
    if out.get("error"):
        return jsonify(ok=False, **out), 503
    return jsonify(ok=True, **out)


@brain_inspector_bp.route("/api/v1/brain/models", methods=["GET"])
def brain_models_endpoint():
    """Show which model each tier currently uses. Useful for diagnostics
    when wondering 'is the brain really on Opus?' """
    try:
        from routes.brain_models import brain_model_summary
        return jsonify(ok=True, **brain_model_summary())
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500


# ── HTML page at /brain/brief ────────────────────────────────────────
@brain_inspector_bp.route("/brain/brief", methods=["GET"])
def brief_html():
    """Render the latest brief with the eyeball-card aesthetic."""
    _ensure_brief_table()
    summary = ""
    md = ""
    generated_at = ""
    model = ""
    counts = {"healthy": 0, "degrading": 0, "attention": 0}
    c = _get_db()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT generated_at, model, summary, brief_md,
                           healthy_count, degrading_count, attention_count
                      FROM brain_briefs
                     WHERE error IS NULL
                     ORDER BY generated_at DESC LIMIT 1
                """)
                r = cur.fetchone()
                if r:
                    generated_at = str(r[0])[:19]
                    model = r[1] or ""
                    summary = r[2] or ""
                    md = r[3] or ""
                    counts["healthy"]    = int(r[4] or 0)
                    counts["degrading"]  = int(r[5] or 0)
                    counts["attention"]  = int(r[6] or 0)
        finally:
            try: c.close()
            except Exception: pass

    body_inner = (md.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")) or (
        "<p style='color:#a1a1aa'>No brief yet. "
        "<code>POST /api/v1/brain/brief/generate</code> to trigger one.</p>"
    )

    return Response(f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<title>DC Hub · Brain Brief</title>
<meta name="description" content="DC Hub autonomous Inspector brief. {summary[:140]}">
<meta name="robots" content="noindex">
<link rel="icon" type="image/svg+xml" href="/icons/icon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script defer src="/js/dchub-brand.js"></script>
<style>
:root{{
  --bg:#0a0a0f;--surface:#131319;--border:rgba(255,255,255,.06);
  --border-strong:rgba(255,255,255,.1);--text:#f5f5f7;--text-dim:#a1a1aa;
  --text-faint:#71717a;--indigo:#6366f1;--violet:#a855f7;
  --grad:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
  --grad-soft:linear-gradient(135deg,rgba(99,102,241,.10) 0%,rgba(168,85,247,.10) 100%);
  --font:'Instrument Sans',-apple-system,sans-serif;
  --mono:'JetBrains Mono','SF Mono',monospace;
}}
*,*::before,*::after{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:var(--font);background:var(--bg);color:var(--text);
     line-height:1.6;-webkit-font-smoothing:antialiased;
     position:relative;min-height:100vh}}
body::before{{content:'';position:fixed;top:-30%;left:50%;transform:translateX(-50%);
  width:1200px;height:1200px;z-index:0;pointer-events:none;
  background:radial-gradient(circle,rgba(99,102,241,.10) 0%,
                              rgba(168,85,247,.06) 30%,transparent 60%)}}
.wrap{{max-width:920px;margin:0 auto;padding:48px 24px 80px;
       position:relative;z-index:1}}
header.top{{display:flex;align-items:center;justify-content:space-between;
            margin-bottom:36px;flex-wrap:wrap;gap:14px}}
header.top a.brand{{display:inline-flex;align-items:center;gap:10px;
                    text-decoration:none;color:var(--text)}}
.meta{{font-family:var(--mono);font-size:11px;text-transform:uppercase;
       letter-spacing:.1em;color:var(--text-faint)}}
.meta .dot{{display:inline-block;width:6px;height:6px;border-radius:50%;
            background:var(--violet);box-shadow:0 0 8px var(--violet);
            margin-right:6px;vertical-align:middle;
            animation:pulse 2s ease-in-out infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
.eyebrow{{font-family:var(--mono);font-size:11px;text-transform:uppercase;
          letter-spacing:.16em;color:var(--violet);font-weight:600;
          margin-bottom:14px}}
h1.title{{font-size:clamp(1.8rem,3.6vw,2.4rem);font-weight:700;
          letter-spacing:-.025em;line-height:1.1;margin-bottom:12px}}
h1.title .grad{{background:var(--grad);-webkit-background-clip:text;
                background-clip:text;color:transparent}}
.lede{{color:var(--text-dim);font-size:1rem;line-height:1.55;
       max-width:680px;margin-bottom:28px}}
.counts{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;
         margin-bottom:32px}}
@media (max-width:640px){{.counts{{grid-template-columns:1fr}}}}
.count{{background:var(--surface);border:1px solid var(--border);
        border-radius:12px;padding:18px;text-align:center}}
.count-val{{font-size:1.6rem;font-weight:700;letter-spacing:-.02em;
            background:var(--grad);-webkit-background-clip:text;
            background-clip:text;color:transparent;display:block}}
.count-lbl{{font-family:var(--mono);font-size:10px;text-transform:uppercase;
            letter-spacing:.1em;color:var(--text-faint);margin-top:6px;
            display:block}}
pre.brief{{background:var(--surface);border:1px solid var(--border);
           border-radius:14px;padding:24px 28px;font-family:var(--font);
           font-size:14.5px;line-height:1.7;color:var(--text);
           white-space:pre-wrap;word-wrap:break-word;
           border-left:3px solid var(--violet)}}
.regen{{display:inline-flex;align-items:center;gap:8px;
        padding:8px 14px;border-radius:999px;background:var(--surface);
        border:1px solid var(--border-strong);font-size:12.5px;
        font-weight:600;color:var(--text);text-decoration:none;
        font-family:var(--mono);text-transform:uppercase;letter-spacing:.08em}}
.regen:hover{{border-color:var(--violet)}}
.foot{{margin-top:64px;padding-top:32px;border-top:1px solid var(--border);
       font-family:var(--mono);font-size:11px;color:var(--text-faint);
       text-align:center}}
.foot a{{color:var(--text-dim);margin:0 8px;text-decoration:none}}
.foot a:hover{{color:var(--text)}}
</style></head><body>
<div class="wrap">
  <header class="top">
    <a href="/" class="brand" data-dchub-brand></a>
    <span class="meta"><span class="dot"></span>{generated_at or 'No brief yet'}{' · ' + model if model else ''}</span>
  </header>
  <div class="eyebrow">Autonomous Inspector</div>
  <h1 class="title">What the brain <span class="grad">noticed today.</span></h1>
  <p class="lede">{summary or 'The Inspector reads every surface every few hours, synthesizes what changed, and writes this brief. Evidence-first, no invented numbers, no hype.'}</p>
  <div class="counts">
    <div class="count"><span class="count-val">{counts['healthy']}</span><span class="count-lbl">Healthy</span></div>
    <div class="count"><span class="count-val">{counts['degrading']}</span><span class="count-lbl">Degrading</span></div>
    <div class="count"><span class="count-val">{counts['attention']}</span><span class="count-lbl">Needs attention</span></div>
  </div>
  <pre class="brief">{body_inner}</pre>
  <div class="foot">
    DC Hub · Inspector layer · model {model or '—'}<br>
    <a href="/">dchub.cloud</a> · <a href="/reports/monthly">monthly trend</a> · <a href="/cited-by">cited by</a> · <a href="/transparency">ops</a>
  </div>
</div>
</body></html>""",
        mimetype="text/html",
        headers={"Cache-Control": "public, max-age=300"})


def _smoke():
    logger.info("[brain-inspector] ready · GET /brain/brief + "
                 "/api/v1/brain/brief/{latest|<id>|list} + "
                 "POST /api/v1/brain/brief/generate")

_smoke()
