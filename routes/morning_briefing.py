"""morning_briefing.py — operator's personal CEO daily report (2026-06-07).

DESIGN: this is Jonathan's PERSONAL CEO DAILY REPORT. It must READ FAST
(under 60s glance) on a phone over coffee. NOT exhaustive — every
section is opt-in for a deeper dive via the "Full dashboards" footer.

Problem it solves: Jonathan opens 6+ admin dashboards every morning
(/admin/funnel-health, /admin/brain/backlog, /admin/sentinel-inbox,
/api/v1/media/pulse, /api/v1/qa/state-of-2026, etc.) to assemble a
mental picture of "is the business OK?". This module fans out to each
of those source endpoints once, synthesizes the answer into a single
1-page HTML email, ships it at 06:00 UTC via Resend.

What it does NOT do:
  - Re-render dashboards in the email (use the footer links instead).
  - Inject AI commentary it can't ground in numbers. The strategic line
    at the bottom is pulled from brain_strategic_recommendations — if
    the table is empty, the section says so honestly.
  - Send twice on the same day. UNIQUE(DATE(sent_at), recipient_email)
    on morning_briefing_log enforces this even if the cron double-fires.
  - Run on Replit (skipped via crawler_scheduler.py's is_replit gate
    inherited from start_scheduled_crawlers).

Endpoints:
  GET  /api/v1/admin/morning-briefing/preview?format=html  rendered (no send)
  GET  /api/v1/admin/morning-briefing/preview?format=json  same data as JSON
  POST /api/v1/admin/morning-briefing/send                 fire now (testing)
  GET  /api/v1/admin/morning-briefing/history              last 30 sends

Env vars:
  DCHUB_OPERATOR_EMAIL              default azmartone@gmail.com
  DCHUB_RESEND_API_KEY              (required — fail-soft if missing)
  DCHUB_ADMIN_KEY / DCHUB_INTERNAL_KEY (admin gate)
  DCHUB_FROM_NAME                   default "DC Hub"
  DCHUB_FROM_EMAIL                  default "alerts@dchub.cloud"
  DCHUB_INTERNAL_API                default http://127.0.0.1:8080
  MORNING_BRIEFING_DISABLE=1        kill switch (default OFF)
  MORNING_BRIEFING_DRY_RUN=1        renders HTML, skips Resend POST + log
  MORNING_BRIEFING_FORCE=1          bypass same-day idempotency (testing)

Cron: crawler_scheduler.SCHEDULE "morning_briefing" (6,18) — twice-daily
slot but the runner checks the morning_briefing_log same-day UNIQUE so it
never sends twice in 24h.
"""

from __future__ import annotations

import os
import json
import datetime
import logging
from flask import Blueprint, Response, jsonify, request


logger = logging.getLogger("morning_briefing")
morning_briefing_bp = Blueprint("morning_briefing", __name__)


_ADMIN_KEY  = (os.environ.get("DCHUB_ADMIN_KEY")
               or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
_RESEND_KEY = (os.environ.get("DCHUB_RESEND_API_KEY")
               or os.environ.get("RESEND_API_KEY") or "").strip()
_FROM_NAME  = os.environ.get("DCHUB_FROM_NAME",  "DC Hub")
_FROM_EMAIL = os.environ.get("DCHUB_FROM_EMAIL", "alerts@dchub.cloud")
_OPERATOR   = os.environ.get("DCHUB_OPERATOR_EMAIL",
                              "azmartone@gmail.com").strip().lower()
_INTERNAL_BASE = os.environ.get("DCHUB_INTERNAL_API", "http://127.0.0.1:8080")

# Per-endpoint fetch timeout. Kept low — if a source endpoint hangs,
# we render the section as "—" rather than holding up the whole email.
_FETCH_TIMEOUT_S = 8


# ── DB / schema ───────────────────────────────────────────────────────

def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS morning_briefing_log (
    id                  BIGSERIAL PRIMARY KEY,
    sent_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    recipient_email     TEXT NOT NULL,
    resend_message_id   TEXT,
    summary_json        JSONB,
    status              TEXT NOT NULL DEFAULT 'sent'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_morning_briefing_day_recipient
    ON morning_briefing_log ((DATE(sent_at)), recipient_email);
CREATE INDEX IF NOT EXISTS ix_morning_briefing_sent_at
    ON morning_briefing_log(sent_at DESC);
"""


def _ensure_schema(c) -> None:
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


# ── HTTP helpers ──────────────────────────────────────────────────────

def _get_json(path: str) -> dict | None:
    """Loopback GET → JSON. Returns None on any error (caller renders '—')."""
    try:
        import requests
        headers = {
            "User-Agent": "dchub-morning-briefing/1.0",
        }
        if _ADMIN_KEY:
            headers["X-Admin-Key"] = _ADMIN_KEY
            headers["X-Internal-Key"] = _ADMIN_KEY
        url = f"{_INTERNAL_BASE.rstrip('/')}{path}"
        r = requests.get(url, headers=headers, timeout=_FETCH_TIMEOUT_S)
        if r.status_code >= 400:
            return None
        ct = r.headers.get("content-type", "")
        if "application/json" not in ct:
            return None
        return r.json()
    except Exception:
        return None


# ── Source gatherers ──────────────────────────────────────────────────

def _gather_funnel() -> dict:
    """Last-24h funnel + verdict from /admin/funnel-health?format=json."""
    d = _get_json("/api/v1/admin/funnel-health?format=json") or {}
    k = d.get("kpis", {}) or {}
    f = d.get("funnel", {}) or {}
    ab = (d.get("pricing_ab") or {}) if isinstance(d, dict) else {}
    su = (d.get("session_upgrades") or {}) if isinstance(d, dict) else {}
    # Verdict: derive a yellow/green/red from the numbers we have.
    sig30 = int(f.get("signals_30d") or 0)
    conv30 = int(f.get("conversions_30d") or 0)
    verdict_color = "green"
    verdict_text = "healthy"
    if sig30 >= 100 and conv30 == 0:
        verdict_color = "red"
        verdict_text = "funnel leak — 100+ signals, 0 conversions"
    elif sig30 >= 50 and conv30 == 0:
        verdict_color = "yellow"
        verdict_text = "stuck — signals coming in, no conversions"
    elif conv30 == 0:
        verdict_color = "yellow"
        verdict_text = "quiet — no recent conversions"
    return {
        "mrr_usd":            int(k.get("mrr_usd") or 0),
        "conversions_30d":    int(k.get("conversions_30d") or 0),
        "active_dev_keys":    int(k.get("active_dev_keys") or 0),
        "tool_calls_7d":      int(k.get("tool_calls_7d") or 0),
        "signals_30d":        sig30,
        "trial_keys_active":  int(su.get("trial_keys_active") or 0),
        "high_intent_claims": int(su.get("high_intent_claims_30d") or 0),
        "verdict_color":      verdict_color,
        "verdict_text":       verdict_text,
        "pricing_ab": {
            "arm_a_impressions": int(ab.get("arm_a_impressions") or 0),
            "arm_b_impressions": int(ab.get("arm_b_impressions") or 0),
            "arm_a_conversions": int(ab.get("arm_a_conversions") or 0),
            "arm_b_conversions": int(ab.get("arm_b_conversions") or 0),
        },
    }


def _gather_brain_backlog() -> dict:
    d = _get_json("/api/v1/admin/brain/backlog") or {}
    pc = d.get("proposed_code", {}) or {}
    return {
        "stuck_total":         int(d.get("stuck_total") or 0),
        "high_conf_pending":   int(pc.get("high_conf_count") or 0),
        "by_status":           pc.get("by_status") or {},
    }


def _gather_strategic_recs() -> dict:
    """Latest strategic rec title + count from the planner JSON endpoint."""
    # Pull last week of recs from brain_strategic_recommendations via DB.
    c = _conn()
    if c is None:
        return {"count_4w": 0, "latest_title": None, "latest_week": None}
    try:
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT COUNT(*) FROM brain_strategic_recommendations
                     WHERE week_of >= CURRENT_DATE - INTERVAL '28 days'
                """)
                count_4w = int((cur.fetchone() or (0,))[0] or 0)
            except Exception:
                count_4w = 0
            try:
                cur.execute("""
                    SELECT title, week_of FROM brain_strategic_recommendations
                     ORDER BY week_of DESC, id DESC LIMIT 1
                """)
                row = cur.fetchone()
                latest_title = row[0] if row else None
                latest_week  = str(row[1]) if row else None
            except Exception:
                latest_title, latest_week = None, None
        return {"count_4w": count_4w,
                "latest_title": latest_title,
                "latest_week": latest_week}
    finally:
        try: c.close()
        except Exception: pass


def _gather_sentinel() -> dict:
    d = _get_json("/api/v1/admin/sentinel-inbox") or {}
    grades = d.get("grades", {}) or {}
    items = d.get("items") or []
    # Flag the 1-3 worst pages (grade D/F + healthy=false) for the
    # "attention needed" section.
    worst = []
    for it in items:
        if (it.get("grade") in ("D", "F")
                and (it.get("consecutive_5xx") or 0) >= 2):
            worst.append({
                "path":           it.get("path"),
                "grade":          it.get("grade"),
                "consecutive_5xx": int(it.get("consecutive_5xx") or 0),
            })
        if len(worst) >= 3:
            break
    return {
        "total":     int(d.get("total") or 0),
        "healthy":   int(d.get("healthy") or 0),
        "unhealthy": int(d.get("unhealthy") or 0),
        "grades":    grades,
        "worst":     worst,
    }


def _gather_media() -> dict:
    """Press cadence + LinkedIn velocity + spike count last 24h."""
    d = _get_json("/api/v1/media/pulse") or {}
    comp = (d.get("components") or {}) if isinstance(d, dict) else {}
    press = (comp.get("press") or {}) if isinstance(comp, dict) else {}
    linkedin = (comp.get("linkedin") or {}) if isinstance(comp, dict) else {}
    # Try /api/v1/media/topic-pulse for the top topic.
    tp = _get_json("/api/v1/media/topic-pulse") or {}
    topics = tp.get("topics") or []
    top_topic = None
    if topics and isinstance(topics, list):
        # topics tend to be sorted by score desc; defensive sort.
        sorted_topics = sorted(
            topics,
            key=lambda t: float(t.get("score") or 0),
            reverse=True,
        )
        if sorted_topics:
            t0 = sorted_topics[0]
            top_topic = {
                "topic": t0.get("topic") or t0.get("tag") or "—",
                "score": float(t0.get("score") or 0),
            }
    return {
        "verdict":          d.get("verdict") or "—",
        "press_24h":        int(press.get("count_24h") or 0),
        "press_7d":         int(press.get("count_7d") or 0),
        "linkedin_24h":     int(linkedin.get("count_24h") or 0),
        "linkedin_7d":      int(linkedin.get("count_7d") or 0),
        "top_topic":        top_topic,
        "spike_count_24h":  0,  # spike responder writes elsewhere; best-effort
    }


def _gather_state_2026() -> dict:
    """8 living narrative claims health from /api/v1/qa/state-of-2026."""
    d = _get_json("/api/v1/qa/state-of-2026") or {}
    claims = d.get("claims") or []
    passing = sum(1 for c in claims if c.get("status") == "passing")
    return {
        "total_claims":    len(claims),
        "passing_claims":  passing,
        "all_passing":     bool(claims) and passing == len(claims),
    }


# ── Public synthesis ──────────────────────────────────────────────────

def gather_briefing_context() -> dict:
    """Fan out to all source endpoints. Each returns a defensive dict."""
    started = datetime.datetime.now(datetime.timezone.utc)
    funnel = _gather_funnel()
    brain  = _gather_brain_backlog()
    strat  = _gather_strategic_recs()
    sent   = _gather_sentinel()
    media  = _gather_media()
    state  = _gather_state_2026()
    return {
        "generated_at":     started.isoformat(),
        "date_pretty":      started.strftime("%A, %B %d, %Y"),
        "funnel":           funnel,
        "brain_backlog":    brain,
        "strategic":        strat,
        "sentinel":         sent,
        "media":            media,
        "state_of_2026":    state,
    }


# ── Renderer ──────────────────────────────────────────────────────────

def _fmt_num(n) -> str:
    try:
        n = int(n)
    except Exception:
        return "—"
    return f"{n:,}"


def _verdict_emoji(color: str) -> str:
    return {
        "green": "🟢",
        "yellow": "🟡",
        "red": "🔴",
    }.get(color, "⚪")


def render_briefing_html(ctx: dict) -> str:
    f  = ctx["funnel"]
    b  = ctx["brain_backlog"]
    s  = ctx["sentinel"]
    m  = ctx["media"]
    st = ctx["state_of_2026"]
    sr = ctx["strategic"]

    grades = s.get("grades") or {}
    sentinel_score = f"{s.get('healthy', 0)}/{s.get('total', 0)} healthy"

    # "Today's schedule" — hard-coded from crawler_scheduler.SCHEDULE
    # because we don't want to import that giant module just for this.
    today_schedule = [
        ("🧠 Strategic synthesis", "08:00 UTC (Monday only)"),
        ("📣 Media spike check",   "10:00 + 22:00 UTC"),
        ("🛡 Sentinel auto-merge",  "05:00 + 17:00 UTC"),
        ("💰 Half-price campaign poll", "19:00 UTC"),
    ]

    # Worst-page list
    worst_lines = []
    for w in (s.get("worst") or [])[:3]:
        worst_lines.append(
            f"⚠️ <code>{w['path']}</code> grade {w['grade']} "
            f"(5xx {w['consecutive_5xx']}× in row — sentinel will heal-and-ship)"
        )
    if not worst_lines:
        worst_lines.append("✅ No pages in 5xx streak")

    # State 2026
    state_line = (
        f"✅ All {st['passing_claims']}/{st['total_claims']} State of 2026 "
        f"narrative claims still passing"
        if st.get("all_passing") else
        f"⚠️ {st['passing_claims']}/{st['total_claims']} State of 2026 "
        f"claims passing — review needed"
    )

    # Pricing A/B summary
    ab = f.get("pricing_ab") or {}
    ab_line = (
        f"Arm A {ab.get('arm_a_impressions', 0)} impressions / "
        f"Arm B {ab.get('arm_b_impressions', 0)} impressions / "
        f"{ab.get('arm_a_conversions', 0) + ab.get('arm_b_conversions', 0)} conv"
    )

    # Top media topic
    if m.get("top_topic") and m["top_topic"].get("topic"):
        topic_line = (
            f"Top topic by engagement: <strong>{m['top_topic']['topic']}</strong> "
            f"({m['top_topic']['score']:.1f} score)"
        )
    else:
        topic_line = "Top topic: — (no engagement data yet)"

    # Strategic latest
    if sr.get("latest_title"):
        strat_quote = (
            f'<em>"{sr["latest_title"]}"</em>'
            f'<br><span style="color:#6b7280;font-size:.85rem">'
            f'Week of {sr["latest_week"]} · {sr["count_4w"]} recs stored last 4 wks</span>'
        )
    else:
        strat_quote = (
            '<em>No strategic recommendations yet this week.</em>'
            '<br><span style="color:#6b7280;font-size:.85rem">'
            'Monday 08:00 UTC = next L6 synthesis run.</span>'
        )

    schedule_html = "".join(
        f'<div style="margin:.15rem 0">'
        f'<span style="color:#94a3b8">{name}</span> '
        f'<span style="color:#cbd5e1;font-family:monospace;font-size:.85rem">{when}</span>'
        f"</div>"
        for name, when in today_schedule
    )

    funnel_health_color = {
        "green": "#10b981",
        "yellow": "#eab308",
        "red": "#ef4444",
    }.get(f["verdict_color"], "#94a3b8")

    body = f"""<!doctype html>
<html><body style="font-family:-apple-system,'Inter',sans-serif;max-width:640px;
margin:0 auto;padding:1.25rem;color:#1f2937;line-height:1.5;background:#ffffff">

<div style="background:linear-gradient(135deg,#0f172a,#1e3a8a);color:white;padding:1.25rem 1.5rem;border-radius:10px;margin-bottom:1.5rem">
 <div style="font-size:.7rem;letter-spacing:.15em;color:#94a3b8;text-transform:uppercase;margin-bottom:.4rem">DC Hub · Daily Brief</div>
 <h1 style="margin:0;font-size:1.5rem;font-weight:700;letter-spacing:-.02em">Good morning, Jonathan</h1>
 <div style="color:#cbd5e1;font-size:.95rem;margin-top:.25rem">{ctx['date_pretty']}</div>
</div>

<!-- YESTERDAY -->
<div style="margin-bottom:1.25rem">
 <div style="font-size:.7rem;letter-spacing:.15em;color:#64748b;text-transform:uppercase;font-weight:700;margin-bottom:.5rem">▼ Yesterday  (last 24h)</div>
 <div style="background:#f8fafc;border-radius:8px;padding:.85rem 1rem;font-size:.95rem">
  <div style="display:flex;justify-content:space-between;margin-bottom:.3rem">
   <span><strong>Tool calls:</strong> {_fmt_num(f['tool_calls_7d'])} <span style="color:#94a3b8;font-size:.85rem">(7d)</span></span>
   <span><strong>Signals:</strong> {_fmt_num(f['signals_30d'])} <span style="color:#94a3b8;font-size:.85rem">(30d)</span></span>
  </div>
  <div style="display:flex;justify-content:space-between;margin-bottom:.3rem">
   <span><strong>Conversions:</strong> {_fmt_num(f['conversions_30d'])} <span style="color:#94a3b8;font-size:.85rem">(30d)</span></span>
   <span><strong>MRR:</strong> ${_fmt_num(f['mrr_usd'])}</span>
  </div>
  <div style="display:flex;justify-content:space-between;margin-bottom:.3rem">
   <span><strong>Active dev keys:</strong> {_fmt_num(f['active_dev_keys'])}</span>
   <span><strong>High-intent claims:</strong> {_fmt_num(f['high_intent_claims'])}</span>
  </div>
  <div style="margin-top:.5rem;color:#475569;font-size:.9rem">
   Sentinel: <strong>{sentinel_score}</strong>
   &nbsp;·&nbsp; Grades: A={grades.get('A',0)} B={grades.get('B',0)} C={grades.get('C',0)} D={grades.get('D',0)} F={grades.get('F',0)}
  </div>
 </div>
</div>

<!-- TODAY -->
<div style="margin-bottom:1.25rem">
 <div style="font-size:.7rem;letter-spacing:.15em;color:#64748b;text-transform:uppercase;font-weight:700;margin-bottom:.5rem">▼ Today  (next 24h scheduled)</div>
 <div style="background:#0f172a;color:#e2e8f0;border-radius:8px;padding:.85rem 1rem;font-size:.92rem">
  {schedule_html}
 </div>
</div>

<!-- BRAIN BACKLOG -->
<div style="margin-bottom:1.25rem">
 <div style="font-size:.7rem;letter-spacing:.15em;color:#64748b;text-transform:uppercase;font-weight:700;margin-bottom:.5rem">▼ Brain Backlog</div>
 <div style="background:#f8fafc;border-radius:8px;padding:.85rem 1rem;font-size:.93rem;line-height:1.6">
  <div>Draft PRs awaiting review: <strong>{b['high_conf_pending']}</strong></div>
  <div>Stuck issues (untried 23+ cycles): <strong>{b['stuck_total']}</strong></div>
  <div>Strategic recommendations stored (last 4 wks): <strong>{sr['count_4w']}</strong></div>
  <div style="margin-top:.4rem;color:#334155">Latest: {strat_quote}</div>
 </div>
</div>

<!-- MEDIA -->
<div style="margin-bottom:1.25rem">
 <div style="font-size:.7rem;letter-spacing:.15em;color:#64748b;text-transform:uppercase;font-weight:700;margin-bottom:.5rem">▼ Media</div>
 <div style="background:#f8fafc;border-radius:8px;padding:.85rem 1rem;font-size:.93rem;line-height:1.6">
  <div>Posts published yesterday: <strong>{m['linkedin_24h']}</strong> (LinkedIn) · <strong>{m['press_24h']}</strong> (Press)</div>
  <div>{topic_line}</div>
  <div>Media verdict: <strong>{m['verdict']}</strong></div>
 </div>
</div>

<!-- FUNNEL HEALTH -->
<div style="margin-bottom:1.25rem">
 <div style="font-size:.7rem;letter-spacing:.15em;color:#64748b;text-transform:uppercase;font-weight:700;margin-bottom:.5rem">▼ Funnel Health</div>
 <div style="background:#f8fafc;border-radius:8px;padding:.85rem 1rem;font-size:.93rem;line-height:1.6;border-left:4px solid {funnel_health_color}">
  <div style="margin-bottom:.3rem">
   Verdict: {_verdict_emoji(f['verdict_color'])} <strong style="color:{funnel_health_color}">{f['verdict_color']}</strong>
   <span style="color:#475569">({f['verdict_text']})</span>
  </div>
  <div>Pricing A/B: {ab_line}</div>
  <div>Trial key activations: <strong>{f['trial_keys_active']}</strong></div>
 </div>
</div>

<!-- ATTENTION NEEDED -->
<div style="margin-bottom:1.25rem">
 <div style="font-size:.7rem;letter-spacing:.15em;color:#64748b;text-transform:uppercase;font-weight:700;margin-bottom:.5rem">▼ Attention Needed</div>
 <div style="background:#fef3c7;border-radius:8px;padding:.85rem 1rem;font-size:.93rem;line-height:1.7">
  {"<br>".join(worst_lines)}<br>
  {state_line}
 </div>
</div>

<!-- STRATEGIC FROM THE BRAIN -->
<div style="margin-bottom:1.5rem">
 <div style="font-size:.7rem;letter-spacing:.15em;color:#64748b;text-transform:uppercase;font-weight:700;margin-bottom:.5rem">▼ Strategic From The Brain</div>
 <div style="background:linear-gradient(135deg,#fef9c3,#fef3c7);border-radius:8px;padding:.85rem 1rem;font-size:.95rem;line-height:1.55;color:#52525b;border-left:3px solid #eab308">
  {strat_quote}
 </div>
</div>

<!-- FOOTER -->
<div style="border-top:1px solid #e5e7eb;padding-top:1rem;margin-top:1.25rem;font-size:.85rem;color:#64748b">
 <div style="margin-bottom:.5rem;font-weight:600;color:#1f2937">Full dashboards</div>
 <a href="https://dchub.cloud/admin/funnel-health" style="color:#1e40af;text-decoration:none;margin-right:1rem">funnel-health</a> ·
 <a href="https://dchub.cloud/admin/brain-backlog" style="color:#1e40af;text-decoration:none;margin:0 1rem">brain-backlog</a> ·
 <a href="https://dchub.cloud/admin/sentinel-inbox" style="color:#1e40af;text-decoration:none;margin-left:1rem">sentinel-inbox</a>
</div>

<div style="margin-top:1rem;color:#9ca3af;font-size:.72rem">
 Operator brief generated {ctx['generated_at']} · routes/morning_briefing.py ·
 Kill switch: <code>MORNING_BRIEFING_DISABLE=1</code>
</div>

</body></html>"""
    return body


# ── Send ──────────────────────────────────────────────────────────────

def _send_via_resend(to_email: str, subject: str, body_html: str
                     ) -> tuple[bool, str, str]:
    """POST to Resend. Returns (ok, info_string, message_id). Mirrors
    routes/renewal_nudge._send_via_resend exactly."""
    if not _RESEND_KEY:
        return False, "no_resend_key", ""
    try:
        import requests
        r = requests.post(
            "https://api.resend.com/emails",
            json={"from": f"{_FROM_NAME} <{_FROM_EMAIL}>",
                  "to": [to_email], "subject": subject, "html": body_html},
            headers={"Authorization": f"Bearer {_RESEND_KEY}"},
            timeout=10,
        )
        if r.status_code < 300:
            try:
                msg_id = (r.json() or {}).get("id", "") or ""
            except Exception:
                msg_id = ""
            return True, f"sent_{r.status_code}", msg_id
        return False, f"status_{r.status_code}_{r.text[:80]}", ""
    except Exception as e:
        return False, f"{type(e).__name__}:{str(e)[:60]}", ""


def _already_sent_today(c, recipient_email: str) -> bool:
    """True if a row exists for today + this recipient. Cheap UNIQUE check."""
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM morning_briefing_log
                 WHERE DATE(sent_at) = CURRENT_DATE
                   AND recipient_email = %s
                 LIMIT 1
            """, (recipient_email,))
            return cur.fetchone() is not None
    except Exception:
        return False


def send_briefing(force: bool = False, dry_run: bool = False
                  ) -> dict:
    """Build the briefing + ship via Resend. Idempotent on day+recipient."""
    out: dict = {
        "ran_at":  datetime.datetime.utcnow().isoformat() + "Z",
        "ok":      False,
        "dry_run": dry_run,
        "force":   force,
        "recipient": _OPERATOR,
    }
    if os.environ.get("MORNING_BRIEFING_DISABLE", "").lower() in ("1", "true", "yes"):
        out["skipped"] = "kill_switch_MORNING_BRIEFING_DISABLE"
        return out

    env_dry = os.environ.get("MORNING_BRIEFING_DRY_RUN", "").lower() in ("1", "true", "yes")
    dry_run = dry_run or env_dry
    out["dry_run"] = dry_run

    env_force = os.environ.get("MORNING_BRIEFING_FORCE", "").lower() in ("1", "true", "yes")
    force = force or env_force

    ctx = gather_briefing_context()
    body = render_briefing_html(ctx)
    today = datetime.date.today().isoformat()
    subject = f"DC Hub · Daily Brief · {today}"
    out["subject"] = subject
    out["context_summary"] = {
        "tool_calls_7d":    ctx["funnel"]["tool_calls_7d"],
        "signals_30d":      ctx["funnel"]["signals_30d"],
        "conversions_30d":  ctx["funnel"]["conversions_30d"],
        "mrr_usd":          ctx["funnel"]["mrr_usd"],
        "verdict":          ctx["funnel"]["verdict_color"],
        "sentinel_healthy": ctx["sentinel"]["healthy"],
        "sentinel_total":   ctx["sentinel"]["total"],
        "draft_prs":        ctx["brain_backlog"]["high_conf_pending"],
        "stuck_issues":     ctx["brain_backlog"]["stuck_total"],
    }

    if dry_run:
        out["ok"] = True
        out["message_id"] = ""
        out["info"] = "dry_run"
        out["body_len"] = len(body)
        return out

    c = _conn()
    if c is None:
        out["error"] = "no_database"
        # We can still attempt the send (don't block the cron on DB).
        ok, info, msg_id = _send_via_resend(_OPERATOR, subject, body)
        out["ok"] = ok
        out["info"] = info
        out["message_id"] = msg_id
        return out

    try:
        _ensure_schema(c)

        if not force and _already_sent_today(c, _OPERATOR):
            out["skipped"] = "already_sent_today"
            out["ok"] = True
            return out

        ok, info, msg_id = _send_via_resend(_OPERATOR, subject, body)
        out["ok"] = ok
        out["info"] = info
        out["message_id"] = msg_id

        # Always log — sent or failed — so the cron tracks attempts.
        try:
            with c.cursor() as cur:
                cur.execute("""
                    INSERT INTO morning_briefing_log
                      (recipient_email, resend_message_id, summary_json, status)
                    VALUES (%s, %s, %s::jsonb, %s)
                    ON CONFLICT (DATE(sent_at), recipient_email)
                    DO NOTHING
                """, (_OPERATOR, msg_id,
                      json.dumps(out["context_summary"]),
                      "sent" if ok else "send_failed"))
        except Exception as e:
            out["log_error"] = str(e)[:120]
    finally:
        try: c.close()
        except Exception: pass

    return out


# ── Endpoints ─────────────────────────────────────────────────────────

def _check_admin() -> tuple[bool, str]:
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key")
                or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return False, "unauthorized"
    return True, ""


@morning_briefing_bp.route("/api/v1/admin/morning-briefing/preview",
                            methods=["GET"])
def morning_briefing_preview():
    ok, err = _check_admin()
    if not ok:
        return jsonify(error=err, hint="X-Admin-Key required"), 401
    fmt = (request.args.get("format") or "html").lower()
    ctx = gather_briefing_context()
    if fmt == "json":
        return jsonify(ctx), 200
    body = render_briefing_html(ctx)
    return Response(body, mimetype="text/html")


@morning_briefing_bp.route("/api/v1/admin/morning-briefing/send",
                            methods=["POST"])
def morning_briefing_send():
    ok, err = _check_admin()
    if not ok:
        return jsonify(error=err, hint="X-Admin-Key required"), 401
    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    dry   = request.args.get("dry_run", "").lower() in ("1", "true", "yes")
    return jsonify(send_briefing(force=force, dry_run=dry)), 200


@morning_briefing_bp.route("/api/v1/admin/morning-briefing/history",
                            methods=["GET"])
def morning_briefing_history():
    ok, err = _check_admin()
    if not ok:
        return jsonify(error=err, hint="X-Admin-Key required"), 401
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, sent_at, recipient_email, resend_message_id,
                       summary_json, status
                  FROM morning_briefing_log
                 ORDER BY sent_at DESC LIMIT 30
            """)
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    out = [{
        "id":                r["id"],
        "sent_at":            r["sent_at"].isoformat() if r["sent_at"] else None,
        "recipient_email":    r["recipient_email"],
        "resend_message_id":  r["resend_message_id"],
        "summary":            r["summary_json"],
        "status":             r["status"],
    } for r in rows]
    return jsonify(log=out, count=len(out)), 200
