"""Site automation (2026-06-06) — closes the "you notice, I fix" loop.

Four automations the founder asked for, to cut the manual back-and-forth:

  #1 Visual sentinel    — a scheduled headless-browser job (visual-sentinel.yml)
                          loads key pages, screenshots them, and flags blank/
                          broken/console-error/empty-looking pages. It POSTs its
                          report here; GET serves the latest + the brain reads it.
                          This is THE gap: API audits never SEE the rendered site.
  #2 Founder briefing   — /api/v1/briefing aggregates "what shipped / what's
                          flagged / what needs your call" from live signals;
                          /briefing renders it. A daily cron (briefing.yml) can
                          email it when DCHUB_BRIEFING_EMAIL is set (opt-in push).
  #3 Deploy integrity   — /api/v1/admin/deploy-integrity smoke-tests the key
                          routes (200 + non-blank) so "why isn't my change live"
                          becomes a self-checking signal. The dual-repo divergence
                          check lives in deploy-integrity.yml (git-level).
  #4 CI auto-triage     — ci-triage.yml fires on a failed GitHub Actions run,
                          classifies it, and POSTs the verdict here; GET serves
                          recent triages so failures arrive pre-diagnosed.

All write endpoints are admin-gated (X-Admin-Key == DCHUB_ADMIN_KEY). Reads are
public so the brain + briefing can consume without a key. Tables are created
lazily; everything fails soft so a misconfigured cron never 500s the site.
"""
import datetime
import json
import os
import urllib.request

from flask import Blueprint, Response, jsonify, request

site_automation_bp = Blueprint("site_automation", __name__)

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY")
              or os.environ.get("ADMIN_API_KEY") or "")
_API = os.environ.get("DCHUB_SELF_BASE", "https://api.dchub.cloud").rstrip("/")


def _admin_ok() -> bool:
    return bool(_ADMIN_KEY) and request.headers.get("X-Admin-Key", "") == _ADMIN_KEY


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    import psycopg2
    return psycopg2.connect(db, sslmode="require", connect_timeout=5)


def _ensure_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS site_sentinel_runs (
            id SERIAL PRIMARY KEY,
            ran_at TIMESTAMPTZ DEFAULT NOW(),
            ok BOOLEAN,
            pages_checked INT,
            issues_count INT,
            report JSONB
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ci_triage_log (
            id SERIAL PRIMARY KEY,
            seen_at TIMESTAMPTZ DEFAULT NOW(),
            workflow TEXT, run_url TEXT, conclusion TEXT,
            classification TEXT, action TEXT, detail JSONB
        )""")


def _self_get(path: str, timeout: float = 8.0, retries: int = 1):
    """Fetch one of our own public endpoints. Retries once (the 1-replica
    backend can momentarily refuse a self-call). Returns {} only after all
    attempts fail — callers must distinguish {} (unavailable) from a real
    empty result, NOT silently treat a failed fetch as 'all good'."""
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(_API + path,
                                         headers={"User-Agent": "dchub-site-automation/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception:
            continue
    return {}


# ════════════════════════════════════════════════════════════════════
# #1  VISUAL SENTINEL
# ════════════════════════════════════════════════════════════════════
@site_automation_bp.route("/api/v1/admin/visual-sentinel", methods=["GET", "POST"])
def visual_sentinel():
    if request.method == "POST":
        if not _admin_ok():
            return jsonify({"ok": False, "error": "admin key required"}), 401
        body = request.get_json(silent=True) or {}
        pages = body.get("pages") or []
        issues = [p for p in pages if not p.get("ok")]
        try:
            c = _conn()
            if c:
                with c, c.cursor() as cur:
                    _ensure_tables(cur)
                    cur.execute(
                        "INSERT INTO site_sentinel_runs (ok, pages_checked, issues_count, report) "
                        "VALUES (%s,%s,%s,%s)",
                        (len(issues) == 0, len(pages), len(issues), json.dumps(body)))
                c.close()
        except Exception as e:
            return jsonify({"ok": False, "stored": False, "error": str(e)[:160]}), 200
        return jsonify({"ok": True, "stored": True,
                        "pages_checked": len(pages), "issues": len(issues)})

    # GET — latest run (public read)
    try:
        c = _conn()
        if not c:
            return jsonify({"ok": True, "available": False, "reason": "no db"})
        with c, c.cursor() as cur:
            _ensure_tables(cur)
            cur.execute("SELECT ran_at, ok, pages_checked, issues_count, report "
                        "FROM site_sentinel_runs ORDER BY ran_at DESC LIMIT 1")
            row = cur.fetchone()
        c.close()
        if not row:
            return jsonify({"ok": True, "available": False, "reason": "no runs yet"})
        rep = row[4] if isinstance(row[4], dict) else (json.loads(row[4]) if row[4] else {})
        issues = [p for p in (rep.get("pages") or []) if not p.get("ok")]
        return jsonify({
            "ok": True, "available": True,
            "ran_at": str(row[0])[:19], "healthy": bool(row[1]),
            "pages_checked": row[2], "issues_count": row[3],
            "issues": issues[:20],
        })
    except Exception as e:
        return jsonify({"ok": True, "available": False, "error": str(e)[:160]})


# ════════════════════════════════════════════════════════════════════
# #2  FOUNDER BRIEFING
# ════════════════════════════════════════════════════════════════════
def _build_briefing() -> dict:
    """Aggregate 'shipped / flagged / needs-your-call' from live signals."""
    media = _self_get("/api/v1/media/aggregate", timeout=12)
    spine = (media.get("live_spine") or {}) if isinstance(media, dict) else {}
    # The audit is the heavy one (runs/aggregates many dims) — give it room + a
    # retry. CRITICAL: if it doesn't load, we must NOT imply "all green".
    audit = _self_get("/api/v1/brain/lifecycle/audit", timeout=22)
    audit_ok = isinstance(audit, dict) and ("findings" in audit or "audits" in audit)
    findings = (audit.get("findings") or []) if audit_ok else []
    sentinel = _self_get("/api/v1/admin/visual-sentinel", timeout=10)
    autocode = _self_get("/api/v1/brain/auto-code", timeout=10)

    shipped = {
        "auto_press_7d": spine.get("auto_press_7d"),
        "mcp_calls_24h": spine.get("mcp_calls_24h"),
        "ai_agents_7d": spine.get("unique_ai_agents_7d"),
        "dcpi_markets": spine.get("dcpi_markets"),
    }
    flagged = [{"dim": f.get("dim"), "summary": f.get("summary")}
               for f in findings if f.get("status") == "weak"]
    if sentinel.get("available") and not sentinel.get("healthy"):
        flagged.append({"dim": "visual_sentinel",
                        "summary": f"{sentinel.get('issues_count')} page(s) look broken/blank — "
                                   + ", ".join(i.get("page", "?") for i in (sentinel.get("issues") or [])[:4])})
    if not audit_ok:
        # Honest: a failed audit fetch is NOT "all green" — say so loudly.
        flagged.append({"dim": "audit_unavailable",
                        "summary": "brain audit didn't load this run — flags UNKNOWN (not necessarily green). "
                                   "Transient self-call miss; reload to refresh."})
    needs_you = []
    recent_actions = autocode.get("recent_actions") or []
    drafts = [a for a in recent_actions if a.get("pr_url") or a.get("recipe")]
    if drafts:
        needs_you.append({"what": f"review {len(drafts)} L22 auto-drafted fixes "
                                  f"(newest: {drafts[0].get('title') or drafts[0].get('recipe')})",
                          "count": len(drafts)})
    # SEO indexing (IndexNow → Bing/Yandex). Read the in-process last-submit
    # status directly so the founder can SEE pings flowing without opening Bing
    # Webmaster Tools. No network call — _LAST is updated on every submit.
    try:
        from routes.indexnow import _LAST as _in_last, KEY_LOCATION as _in_loc
        seo_indexing = {
            "last_submit_at": _in_last.get("at"),
            "last_submitted_count": _in_last.get("submitted"),
            "last_status": _in_last.get("status"),
            "key_location": _in_loc,
        }
    except Exception:
        seo_indexing = {}
    health = "unknown" if not audit_ok else ("attention" if (flagged or needs_you) else "all-green")
    return {
        "audit_loaded": audit_ok,
        "shipped_7d": shipped,
        "flagged": flagged,
        "needs_your_call": needs_you,
        "seo_indexing": seo_indexing,
        "health": health,
    }


@site_automation_bp.route("/api/v1/briefing", methods=["GET"])
def briefing_json():
    b = _build_briefing()
    return Response(json.dumps(b), mimetype="application/json",
                    headers={"Cache-Control": "public, max-age=300",
                             "Access-Control-Allow-Origin": "*"})


@site_automation_bp.route("/briefing", methods=["GET"])
def briefing_html():
    b = _build_briefing()
    def _rows(items, empty):
        if not items:
            return f'<li style="color:#5b6580">{empty}</li>'
        return "".join(f'<li>{(i.get("summary") or i.get("what") or i.get("dim") or "")}</li>'
                       for i in items)
    sh = b.get("shipped_7d", {})
    si = b.get("seo_indexing", {})
    health = b.get("health")
    hcolor = "#22c55e" if health == "all-green" else "#f59e0b"
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DC Hub — Founder Briefing</title>
<style>
body{{margin:0;background:#0A0E1C;color:#E8ECF7;font:16px/1.55 ui-sans-serif,system-ui,sans-serif}}
.wrap{{max-width:760px;margin:0 auto;padding:40px 24px}}
h1{{font-size:28px;margin:0 0 4px}} .as{{color:#8B92A8;font-size:13px;margin-bottom:24px}}
.pill{{display:inline-block;padding:4px 12px;border-radius:99px;font-size:13px;font-weight:700;
  background:{hcolor}22;color:{hcolor};border:1px solid {hcolor}55;margin-bottom:24px}}
.card{{background:#121629;border:1px solid #222842;border-radius:14px;padding:18px 20px;margin:14px 0}}
.card h2{{font-size:15px;margin:0 0 10px;text-transform:uppercase;letter-spacing:.06em;color:#a855f7}}
.stats{{display:flex;gap:26px;flex-wrap:wrap}} .stats b{{font-size:24px;color:#38bdf8}}
.stats span{{color:#8B92A8;font-size:12px;display:block}}
ul{{margin:0;padding-left:18px}} li{{margin:5px 0}}
</style></head><body><div class="wrap">
<h1>DC Hub — Founder Briefing</h1>
<div class="as">Auto-generated · refreshes every load</div>
<span class="pill">● {health}</span>
<div class="card"><h2>Shipped · last 7d</h2><div class="stats">
  <div><b>{sh.get('auto_press_7d','—')}</b><span>auto-press</span></div>
  <div><b>{sh.get('mcp_calls_24h','—')}</b><span>MCP calls/24h</span></div>
  <div><b>{sh.get('ai_agents_7d','—')}</b><span>AI agents/7d</span></div>
  <div><b>{sh.get('dcpi_markets','—')}</b><span>DCPI markets</span></div>
</div></div>
<div class="card"><h2>Search indexing · IndexNow → Bing/Yandex</h2><div class="stats">
  <div><b>{si.get('last_submitted_count','—')}</b><span>URLs · last submit</span></div>
  <div><b>{si.get('last_status','—')}</b><span>last status (202=ok)</span></div>
  <div><b style="font-size:14px">{(si.get('last_submit_at') or '—')[:16]}</b><span>last ping (UTC)</span></div>
</div></div>
<div class="card"><h2>Flagged · the brain wants attention</h2>
  <ul>{_rows(b.get('flagged'), 'Nothing flagged — all audit dims green.')}</ul></div>
<div class="card"><h2>Needs your call</h2>
  <ul>{_rows(b.get('needs_your_call'), 'Nothing waiting on you.')}</ul></div>
</div></body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=300"})


@site_automation_bp.route("/api/v1/admin/briefing/send", methods=["POST"])
def briefing_send():
    """Opt-in daily push. Emails the briefing to DCHUB_BRIEFING_EMAIL (the
    founder's own inbox) — only when that env var is set. Reuses the existing
    mail sender; no-ops safely otherwise. Called by briefing.yml cron."""
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin key required"}), 401
    # Comma-separated → supports a branded inbox (jonathan@dchub.cloud) PLUS a
    # fallback (personal Gmail) so a Proofpoint quarantine of the dchub.cloud one
    # never means you miss the briefing. e.g. "jonathan@dchub.cloud,azmartone@gmail.com".
    raw = os.environ.get("DCHUB_BRIEFING_EMAIL", "").strip()
    recipients = [r.strip() for r in raw.split(",") if r.strip()]
    if not recipients:
        return jsonify({"ok": True, "sent": False, "reason": "DCHUB_BRIEFING_EMAIL not set"})
    b = _build_briefing()
    subject = f"DC Hub briefing — {b.get('health')} — {datetime.datetime.utcnow():%b %d}"
    lines = [f"Health: {b.get('health')}", "", "FLAGGED:"]
    lines += [f"  • {f.get('summary')}" for f in (b.get("flagged") or [])] or ["  (none)"]
    lines += ["", "NEEDS YOUR CALL:"]
    lines += [f"  • {n.get('what')}" for n in (b.get("needs_your_call") or [])] or ["  (none)"]
    _si = b.get("seo_indexing") or {}
    if _si.get("last_submit_at"):
        lines += ["", "SEARCH INDEXING (IndexNow → Bing/Yandex):",
                  f"  • last ping {(_si.get('last_submit_at') or '')[:16]} UTC · "
                  f"{_si.get('last_submitted_count','?')} URLs · status {_si.get('last_status','?')}"]
    lines += ["", "Full view: https://dchub-backend-production.up.railway.app/briefing"]
    text = "\n".join(lines)

    def _send_one(addr):
        try:
            try:
                from routes.cross_post_email import send_plain_email  # type: ignore
                return bool(send_plain_email(addr, subject, text))
            except Exception:
                from main import send_email  # type: ignore
                return bool(send_email(addr, subject, text))
        except Exception:
            return False

    results = {addr: _send_one(addr) for addr in recipients}
    any_sent = any(results.values())
    return jsonify({"ok": True, "sent": any_sent, "recipients": results,
                    "reason": None if any_sent else "no mail sender available / all sends failed"})


# ════════════════════════════════════════════════════════════════════
# #3  DEPLOY INTEGRITY  (live route smoke — the git-divergence check is in CI)
# ════════════════════════════════════════════════════════════════════
@site_automation_bp.route("/api/v1/admin/deploy-integrity", methods=["GET"])
def deploy_integrity():
    """Smoke-test the key public routes: 200 + non-blank. Catches the
    'deploy didn't land / page is blank / stale copy won the race' class
    (e.g., the dc-hub-media 0-byte + stale-index issues) as a live signal."""
    # Public CF Pages routes only. (/briefing is a backend route at
    # api.dchub.cloud, not a dchub.cloud page — it's checked at its own host.)
    routes = ["/", "/land-power-map", "/dc-hub-media/", "/dcpi", "/pricing"]
    results, broken = [], []
    for path in routes:
        try:
            req = urllib.request.Request("https://dchub.cloud" + path,
                                         headers={"User-Agent": "dchub-deploy-integrity/1.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                code = getattr(r, "status", 200)
                body = r.read(60000)
            blank = len(body) < 1500   # a real page is never this small
            ok = (code == 200) and not blank
            entry = {"path": path, "status": code, "bytes": len(body), "ok": ok}
            if not ok:
                broken.append(entry)
            results.append(entry)
        except Exception as e:
            entry = {"path": path, "error": type(e).__name__, "ok": False}
            broken.append(entry); results.append(entry)
    return jsonify({"ok": len(broken) == 0, "checked": len(results),
                    "broken_count": len(broken), "broken": broken, "results": results})


# ════════════════════════════════════════════════════════════════════
# #4  CI AUTO-TRIAGE
# ════════════════════════════════════════════════════════════════════
_CI_CLASSES = [
    ("private-repo checkout (404 get-a-repository)", "Not Found",
     "Cross-repo checkout of a PRIVATE repo with the default token. Make the "
     "checkout continue-on-error + self-skip, or add a read-only PAT secret."),
    ("transient 5xx from a single-replica backend", "5xx",
     "Likely a momentary worker-pool exhaustion, not an outage. Retry the step "
     "2-3x before failing; only sustained 5xx is real."),
    ("dependency / install failure", "npm ERR",
     "Pin the dependency or add a cache; re-run once."),
]


def _classify_ci(text: str) -> dict:
    t = (text or "")
    for label, needle, fix in _CI_CLASSES:
        if needle.lower() in t.lower():
            return {"classification": label, "suggested_fix": fix, "known": True}
    return {"classification": "unrecognized failure", "suggested_fix":
            "No recipe yet — needs a human glance; add a class to _CI_CLASSES once understood.",
            "known": False}


@site_automation_bp.route("/api/v1/admin/ci-triage", methods=["GET", "POST"])
def ci_triage():
    if request.method == "POST":
        if not _admin_ok():
            return jsonify({"ok": False, "error": "admin key required"}), 401
        body = request.get_json(silent=True) or {}
        cls = _classify_ci(body.get("log_excerpt") or body.get("title") or "")
        action = "retried" if "transient" in cls["classification"] else "reported"
        try:
            c = _conn()
            if c:
                with c, c.cursor() as cur:
                    _ensure_tables(cur)
                    cur.execute(
                        "INSERT INTO ci_triage_log (workflow, run_url, conclusion, "
                        "classification, action, detail) VALUES (%s,%s,%s,%s,%s,%s)",
                        (body.get("workflow"), body.get("run_url"), body.get("conclusion"),
                         cls["classification"], action, json.dumps({**body, **cls})))
                c.close()
        except Exception:
            pass
        return jsonify({"ok": True, **cls, "action": action})

    # GET — recent triages
    out = []
    try:
        c = _conn()
        if c:
            with c, c.cursor() as cur:
                _ensure_tables(cur)
                cur.execute("SELECT seen_at, workflow, conclusion, classification, action, run_url "
                            "FROM ci_triage_log ORDER BY seen_at DESC LIMIT 20")
                for r in cur.fetchall():
                    out.append({"seen_at": str(r[0])[:19], "workflow": r[1],
                                "conclusion": r[2], "classification": r[3],
                                "action": r[4], "run_url": r[5]})
            c.close()
    except Exception:
        pass
    return jsonify({"ok": True, "recent": out})


def register_site_automation(app):
    try:
        app.register_blueprint(site_automation_bp)
        app.logger.info("✓ site_automation: visual-sentinel + briefing + deploy-integrity + ci-triage")
    except Exception as e:
        app.logger.warning(f"site_automation registration: {e}")
