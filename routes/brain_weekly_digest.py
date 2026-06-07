"""
brain_weekly_digest.py — Strategic-brain Monday digest email (2026-06-06).
==========================================================================

Companion to routes/brain_strategic_planner.py. Once a week (Monday
08:30 UTC via the SCHEDULE harness, fires 30 min after the synthesis at
08:00) this renders an HTML email of THIS WEEK's strategic
recommendations and ships it to the operator(s).

Subject:  DC Hub · Week of <YYYY-MM-DD> · N strategic recommendations + M draft PRs
Body:     - Summary paragraph from the brain
          - 3 strategic gaps (4-week targets)
          - 3 competitor lacks
          - 3 funnel optimizations (with $/lift)
          - 1 wildcard bet
          - Stop-doing nudge
          - Self-critique
          - Links to each draft PR + the dashboard

Why a dedicated module:
  - Lets the operator unsubscribe / change recipient without touching
    the synthesis logic.
  - Idempotent: same week_of email is suppressed if `brain_digest_log`
    already has a row for it (dry-run knob bypasses).
  - Uses the same Resend pattern as digest.py / market_alerts.py /
    lost_conversion_outreach.py.

Env:
  DCHUB_BRAIN_DIGEST_DISABLE=1        kill switch (default off)
  DCHUB_BRAIN_DIGEST_DRY_RUN=1        render but don't send
  DCHUB_BRAIN_DIGEST_TO               comma-separated recipients
                                       (default: azmartone@gmail.com)
  DCHUB_BRAIN_DIGEST_FROM             from address (default
                                       Jonathan Martone <jonathan@dchub.cloud>)
  DCHUB_RESEND_API_KEY                shared with platform Resend
  DCHUB_PUBLIC_BASE                   default https://dchub.cloud

POST  /api/v1/admin/brain/strategic-digest/send   admin: send now
GET   /api/v1/admin/brain/strategic-digest/preview admin: render only
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from typing import Optional

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

brain_weekly_digest_bp = Blueprint("brain_weekly_digest", __name__)


# ─── Config ─────────────────────────────────────────────────────────

_RESEND_KEY = (os.environ.get("DCHUB_RESEND_API_KEY") or "").strip()
_PUBLIC_BASE = (os.environ.get("DCHUB_PUBLIC_BASE")
                or "https://dchub.cloud").rstrip("/")


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _kill_switch_on() -> bool:
    return _truthy(os.environ.get("DCHUB_BRAIN_DIGEST_DISABLE"))


def _dry_run_env() -> bool:
    return _truthy(os.environ.get("DCHUB_BRAIN_DIGEST_DRY_RUN"))


def _recipients() -> list[str]:
    raw = (os.environ.get("DCHUB_BRAIN_DIGEST_TO")
           or "azmartone@gmail.com").strip()
    return [r.strip() for r in raw.split(",") if r.strip()]


def _from_addr() -> str:
    return (os.environ.get("DCHUB_BRAIN_DIGEST_FROM")
            or "Jonathan Martone <jonathan@dchub.cloud>")


def _admin_key() -> str:
    return (os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


def _admin_ok() -> bool:
    expected = _admin_key()
    if not expected:
        return False
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key") or "").strip()
    if not provided:
        return False
    import hmac
    return hmac.compare_digest(provided, expected)


# ─── DB ─────────────────────────────────────────────────────────────

def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _ensure_log_table() -> None:
    """Create the digest log table on demand (idempotent)."""
    c = _get_db()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS brain_digest_log (
                     id          BIGSERIAL PRIMARY KEY,
                     week_of     DATE NOT NULL,
                     kind        TEXT NOT NULL DEFAULT 'strategic',
                     recipients  TEXT,
                     subject     TEXT,
                     sent_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                     resend_ids  JSONB,
                     dry_run     BOOLEAN NOT NULL DEFAULT FALSE
                   )""")
            cur.execute(
                """CREATE INDEX IF NOT EXISTS ix_bdl_week_of
                       ON brain_digest_log(week_of DESC)""")
            cur.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS uq_bdl_week_kind_dry
                       ON brain_digest_log(week_of, kind, dry_run)""")
        try:
            c.commit()
        except Exception:
            pass
    except Exception as e:
        logger.warning("brain_digest_log create failed: %s", e)
    finally:
        try:
            c.close()
        except Exception:
            pass


def _already_sent(week_of: _dt.date, dry_run: bool) -> bool:
    c = _get_db()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM brain_digest_log
                    WHERE week_of=%s AND kind='strategic' AND dry_run=%s
                    LIMIT 1""", (week_of, dry_run))
            return bool(cur.fetchone())
    except Exception:
        return False
    finally:
        try:
            c.close()
        except Exception:
            pass


def _log_send(week_of: _dt.date, recipients: list[str], subject: str,
               resend_ids: list, dry_run: bool) -> None:
    c = _get_db()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO brain_digest_log(
                       week_of, kind, recipients, subject,
                       resend_ids, dry_run)
                   VALUES(%s, 'strategic', %s, %s, %s, %s)
                   ON CONFLICT (week_of, kind, dry_run) DO UPDATE
                     SET recipients=EXCLUDED.recipients,
                         subject=EXCLUDED.subject,
                         resend_ids=EXCLUDED.resend_ids,
                         sent_at=NOW()""",
                (week_of, ",".join(recipients), subject[:200],
                 json.dumps(resend_ids or []), dry_run))
        try:
            c.commit()
        except Exception:
            pass
    except Exception as e:
        logger.warning("brain_digest_log insert failed: %s", e)
    finally:
        try:
            c.close()
        except Exception:
            pass


# ─── HTML rendering ─────────────────────────────────────────────────

def _esc(s: str) -> str:
    import html as _h
    return _h.escape(str(s or ""), quote=True)


def _render_rec_card(r: dict) -> str:
    """Render one strategic recommendation as an HTML card."""
    title = _esc(r.get("title") or "Untitled")
    spec = (r.get("spec_md") or "").strip()
    # Markdown→HTML lite: paragraphs + line breaks
    spec_html = _esc(spec).replace("\n\n", "</p><p>").replace("\n", "<br>")
    if spec_html:
        spec_html = f"<p>{spec_html}</p>"
    dollar = r.get("dollar_lift")
    dollar_pill = (f'<span class="pill" style="background:rgba(34,197,94,.15);'
                   f'color:#22c55e">+${dollar:,.0f}/yr est.</span>'
                   if dollar else "")
    conf = r.get("confidence")
    conf_pill = ""
    if conf is not None:
        label = "high" if conf >= 0.75 else ("medium" if conf >= 0.5 else "low")
        color = ("#22c55e" if label == "high"
                  else ("#f59e0b" if label == "medium" else "#9a9ab0"))
        conf_pill = (f'<span class="pill" style="background:rgba(255,255,255,.06);'
                     f'color:{color}">{label} confidence</span>')
    kind = (r.get("kind") or "").replace("_", " ")
    pr_url = r.get("pr_url") or ""
    pr_block = ""
    if pr_url:
        pr_block = (f'<div style="margin-top:12px"><a href="{_esc(pr_url)}" '
                    f'style="color:#7c5cff;text-decoration:none;'
                    f'font-weight:600">→ Draft PR ready to review</a></div>')
    evid = r.get("evidence_keys") or []
    evid_html = ""
    if evid:
        evid_html = (
            '<div style="margin-top:10px;font-size:12px;color:#9a9ab0">'
            '<b>Evidence cited:</b> '
            + ", ".join(f"<code>{_esc(e)}</code>" for e in evid[:5])
            + '</div>'
        )
    return f'''
<div style="background:#13131f;border:1px solid #23233a;border-radius:8px;
            padding:18px;margin-bottom:14px">
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
    <span class="pill" style="background:rgba(124,92,255,.18);color:#7c5cff;
          padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600">
      {_esc(kind)}
    </span>
    {dollar_pill}
    {conf_pill}
  </div>
  <h3 style="margin:6px 0 8px;color:#e9e9f0;font-size:17px">{title}</h3>
  <div style="color:#c5c5d4;font-size:14px;line-height:1.55">{spec_html}</div>
  {evid_html}
  {pr_block}
</div>'''


def _kind_label(kind: str) -> str:
    return {
        "strategic_gap_4w":    "Strategic gaps (4-week ship target)",
        "competitor_lack":     "Competitor features DC Hub lacks",
        "funnel_optimization": "Funnel optimizations (with $/lift)",
        "wildcard_bet":        "Wildcard bet",
    }.get(kind, kind)


def render_weekly_digest(week_of: Optional[_dt.date] = None) -> dict:
    """Pull this week's recommendations and render the HTML email body
    + plaintext fallback. Returns a dict with subject, html, text,
    rec_count, pr_count."""
    from routes.brain_strategic_planner import (
        _read_recs_for, _week_of_iso,
    )
    week_of = week_of or _week_of_iso()
    recs = _read_recs_for(week_of)

    if not recs:
        return {
            "week_of":   str(week_of),
            "rec_count": 0,
            "pr_count":  0,
            "subject":   (f"DC Hub · Week of {week_of} · "
                          "No strategic recommendations yet"),
            "html":      _empty_state_html(week_of),
            "text":      ("Brain L6 has not produced strategic "
                          "recommendations for the week of "
                          f"{week_of} yet. POST to "
                          "/api/v1/admin/brain/strategic-synthesis/run "
                          "with admin key to seed."),
            "is_empty":  True,
        }

    # The synthesis_meta row holds summary + stop_doing + self_critique
    meta = next((r for r in recs if r["kind"] == "synthesis_meta"), None)
    summary = ""
    stop_doing = ""
    self_crit = ""
    if meta and meta.get("strategy_payload"):
        try:
            mp = meta["strategy_payload"]
            if isinstance(mp, str):
                mp = json.loads(mp)
            summary = (mp.get("summary") or "").strip()
            stop_doing = (mp.get("stop_doing") or "").strip()
            self_crit = (mp.get("self_critique") or "").strip()
        except Exception:
            pass
    if not summary:
        summary = ("The brain's L6 strategic synthesis ran this week. "
                   "Each card below is a recommendation backed by funnel, "
                   "page-health, customer-ask, or competitor evidence.")

    # Group recs by kind, in display order
    kinds_in_order = ["strategic_gap_4w", "competitor_lack",
                      "funnel_optimization", "wildcard_bet"]
    sections_html = []
    for k in kinds_in_order:
        rec_group = [r for r in recs if r["kind"] == k]
        if not rec_group:
            continue
        cards_html = "\n".join(_render_rec_card(r) for r in rec_group)
        sections_html.append(f'''
<h2 style="color:#e9e9f0;font-size:16px;margin:24px 0 12px;
           padding-bottom:6px;border-bottom:1px solid #23233a">
  {_esc(_kind_label(k))}
</h2>
{cards_html}''')

    extras_html = ""
    if stop_doing:
        extras_html += (
            '<div style="background:#1a1410;border:1px solid #f59e0b;'
            'border-radius:8px;padding:14px 16px;margin:18px 0;'
            'color:#f59e0b;font-size:14px"><b>Stop doing:</b> '
            + _esc(stop_doing) + '</div>'
        )
    if self_crit:
        extras_html += (
            '<div style="background:rgba(255,255,255,.04);border:1px solid '
            '#23233a;border-radius:8px;padding:14px 16px;margin:18px 0;'
            'color:#9a9ab0;font-size:13px;font-style:italic">'
            '<b style="font-style:normal;color:#c084fc">'
            'Self-critique from the brain:</b> '
            + _esc(self_crit) + '</div>'
        )

    pr_count = sum(1 for r in recs if r.get("pr_url"))
    rec_count = sum(1 for r in recs if r["kind"] != "synthesis_meta")
    pr_label = (f" + {pr_count} draft PR" + ("s" if pr_count != 1 else "")
                if pr_count else "")
    subject = (f"DC Hub · Week of {week_of} · {rec_count} strategic "
               f"recommendations{pr_label}")

    dashboard_url = (f"{_PUBLIC_BASE}/admin/brain-backlog#strategic")
    latest_url = (f"{_PUBLIC_BASE}/api/v1/brain/strategic-synthesis/latest")

    html = f'''<!doctype html>
<html><body style="margin:0;padding:24px;font-family:ui-sans-serif,
     system-ui,sans-serif;background:#0a0a14;color:#e9e9f0">
  <div style="max-width:680px;margin:0 auto">
    <div style="background:linear-gradient(135deg,#7c5cff,#c084fc);
                color:#fff;border-radius:10px;padding:18px 20px;
                margin-bottom:20px">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:1.5px;
                  opacity:.85;margin-bottom:6px">
        DC Hub · Brain Layer-6 Strategic Synthesis
      </div>
      <div style="font-size:22px;font-weight:700">
        Week of {week_of}
      </div>
      <div style="margin-top:8px;font-size:13px;opacity:.92">
        {rec_count} recommendation{'s' if rec_count != 1 else ''}{pr_label}
      </div>
    </div>

    <div style="color:#c5c5d4;font-size:14px;line-height:1.6;
                margin-bottom:18px">
      {_esc(summary)}
    </div>

    {''.join(sections_html)}

    {extras_html}

    <div style="margin-top:30px;padding-top:18px;border-top:1px solid #23233a;
                font-size:12px;color:#9a9ab0">
      <p>
        <a href="{_esc(dashboard_url)}" style="color:#7c5cff;
           text-decoration:none">→ Brain Backlog dashboard (Strategic tab)</a>
        &nbsp;·&nbsp;
        <a href="{_esc(latest_url)}" style="color:#7c5cff;
           text-decoration:none">→ JSON API</a>
      </p>
      <p style="margin-top:14px">
        Generated by <code>routes/brain_strategic_planner.py</code> +
        <code>routes/brain_weekly_digest.py</code>. To stop receiving
        these emails, set <code>DCHUB_BRAIN_DIGEST_DISABLE=1</code> on
        Railway. To change recipients, set
        <code>DCHUB_BRAIN_DIGEST_TO=...</code>.
      </p>
    </div>
  </div>
</body></html>'''

    # Plaintext fallback (don't trust every client to render HTML)
    text_lines = [
        f"DC Hub Strategic Synthesis - Week of {week_of}",
        "=" * 60,
        "",
        summary,
        "",
    ]
    for k in kinds_in_order:
        group = [r for r in recs if r["kind"] == k]
        if not group:
            continue
        text_lines.append(f"\n## {_kind_label(k)}")
        for r in group:
            text_lines.append(f"\n* {r.get('title', '?')}")
            if r.get("dollar_lift"):
                text_lines.append(
                    f"  Est. lift: ${r['dollar_lift']:,.0f}/yr")
            text_lines.append("  " + (r.get("spec_md") or "")
                              .replace("\n", "\n  "))
            if r.get("pr_url"):
                text_lines.append(f"  PR: {r['pr_url']}")
    if stop_doing:
        text_lines.append(f"\nStop doing: {stop_doing}")
    if self_crit:
        text_lines.append(f"\nSelf-critique: {self_crit}")
    text_lines.append(f"\n\nDashboard: {dashboard_url}")
    text_lines.append(f"JSON: {latest_url}")
    text = "\n".join(text_lines)

    return {
        "week_of":   str(week_of),
        "rec_count": rec_count,
        "pr_count":  pr_count,
        "subject":   subject,
        "html":      html,
        "text":      text,
        "is_empty":  False,
        "recipients_default": _recipients(),
    }


def _empty_state_html(week_of: _dt.date) -> str:
    return f'''<!doctype html>
<html><body style="margin:0;padding:24px;font-family:ui-sans-serif,system-ui;
     background:#0a0a14;color:#e9e9f0">
  <div style="max-width:560px;margin:0 auto">
    <h1 style="background:linear-gradient(90deg,#7c5cff,#c084fc);
               -webkit-background-clip:text;color:transparent;
               font-size:22px">DC Hub Strategic Brain</h1>
    <p>No strategic recommendations were produced for the week of
       {week_of} yet.</p>
    <p>To seed: <code>POST /api/v1/admin/brain/strategic-synthesis/run</code>
       with the admin key.</p>
  </div>
</body></html>'''


# ─── Send ───────────────────────────────────────────────────────────

def send_weekly_digest(week_of: Optional[_dt.date] = None,
                        force: bool = False,
                        dry_run: Optional[bool] = None,
                        recipients: Optional[list[str]] = None) -> dict:
    """Send the weekly digest email. Returns a summary dict (never raises).

    - Idempotency: skips if already sent for this week_of unless
      force=True.
    - dry_run: if True, renders + records but doesn't send.
    """
    _ensure_log_table()

    if _kill_switch_on():
        return {"ok": False, "reason": "kill_switch_on",
                "env": "DCHUB_BRAIN_DIGEST_DISABLE=1"}

    dr = _dry_run_env() if dry_run is None else bool(dry_run)
    if week_of is None:
        from routes.brain_strategic_planner import _week_of_iso
        week_of = _week_of_iso()

    if _already_sent(week_of, dr) and not force:
        return {"ok": True, "skipped": "already_sent_this_week",
                "week_of": str(week_of), "dry_run": dr,
                "hint": "Pass force=1 to override."}

    rendered = render_weekly_digest(week_of)
    if rendered.get("is_empty"):
        return {"ok": False, "reason": "no_recommendations_yet",
                "week_of": str(week_of),
                "hint": ("Run "
                         "POST /api/v1/admin/brain/strategic-synthesis/run "
                         "first.")}

    to_list = recipients or _recipients()
    if not to_list:
        return {"ok": False, "reason": "no_recipients_configured",
                "env": "DCHUB_BRAIN_DIGEST_TO"}

    if dr or not _RESEND_KEY:
        # Render-only path: log it and bail before Resend
        _log_send(week_of, to_list, rendered["subject"], [], dry_run=True)
        return {
            "ok":          True,
            "dry_run":     True,
            "reason":      ("DCHUB_RESEND_API_KEY unset"
                            if not _RESEND_KEY else "dry_run_requested"),
            "week_of":     str(week_of),
            "subject":     rendered["subject"],
            "rec_count":   rendered["rec_count"],
            "pr_count":    rendered["pr_count"],
            "recipients":  to_list,
            "html_chars":  len(rendered["html"]),
            "text_chars":  len(rendered["text"]),
        }

    import requests as _rq
    sent_ok = []
    failed = []
    from_addr = _from_addr()
    for recipient in to_list:
        try:
            rr = _rq.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {_RESEND_KEY}",
                         "Content-Type":  "application/json"},
                json={
                    "from":     from_addr,
                    "to":       [recipient],
                    "reply_to": "jonathan@dchub.cloud",
                    "subject":  rendered["subject"],
                    "html":     rendered["html"],
                    "text":     rendered["text"],
                },
                timeout=20,
            )
            if rr.status_code < 400:
                rd = (rr.json() or {}) if rr.text else {}
                sent_ok.append({"to": recipient,
                                 "resend_id": rd.get("id")})
            else:
                failed.append({"to": recipient,
                                "reason": (f"resend HTTP {rr.status_code}: "
                                            f"{(rr.text or '')[:200]}")})
        except Exception as e:
            failed.append({"to": recipient, "reason": str(e)[:200]})

    if sent_ok:
        _log_send(week_of, to_list, rendered["subject"],
                   sent_ok, dry_run=False)

    return {
        "ok":         bool(sent_ok),
        "week_of":    str(week_of),
        "subject":    rendered["subject"],
        "rec_count":  rendered["rec_count"],
        "pr_count":   rendered["pr_count"],
        "recipients": to_list,
        "sent":       sent_ok,
        "failed":     failed,
        "dry_run":    False,
    }


# ─── HTTP routes ────────────────────────────────────────────────────

@brain_weekly_digest_bp.route(
    "/api/v1/admin/brain/strategic-digest/send", methods=["POST"])
def digest_send():
    """Send the weekly digest. Admin-gated."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    body = request.get_json(silent=True) or {}
    force = _truthy(request.args.get("force")) or _truthy(body.get("force"))
    dr = request.args.get("dry_run") or body.get("dry_run")
    dr_b = None if dr is None else _truthy(dr)
    recs = body.get("recipients") or None
    if isinstance(recs, str):
        recs = [r.strip() for r in recs.split(",") if r.strip()]
    result = send_weekly_digest(force=force, dry_run=dr_b, recipients=recs)
    return jsonify(result), 200


@brain_weekly_digest_bp.route(
    "/api/v1/admin/brain/strategic-digest/preview", methods=["GET"])
def digest_preview():
    """Render the HTML body for this week. Admin-gated (it can expose
    the strategy verbatim, so we don't want to leak it to anon scrapers).
    Returns the HTML so you can eyeball it in a browser."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    rendered = render_weekly_digest()
    if request.args.get("format") == "json":
        return jsonify(
            ok=True,
            week_of=rendered["week_of"],
            subject=rendered["subject"],
            rec_count=rendered["rec_count"],
            pr_count=rendered["pr_count"],
            html_chars=len(rendered["html"]),
            text_chars=len(rendered["text"]),
            recipients_default=_recipients(),
        ), 200
    return Response(rendered["html"], mimetype="text/html")


@brain_weekly_digest_bp.route(
    "/api/v1/admin/brain/strategic-digest/status", methods=["GET"])
def digest_status():
    """Operator-facing health + last-send view."""
    _ensure_log_table()
    c = _get_db()
    last_sends = []
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute(
                    """SELECT week_of, subject, sent_at, dry_run,
                              recipients
                         FROM brain_digest_log
                        WHERE kind='strategic'
                        ORDER BY sent_at DESC
                        LIMIT 10""")
                for row in (cur.fetchall() or []):
                    last_sends.append({
                        "week_of":    str(row[0]),
                        "subject":    row[1],
                        "sent_at":    row[2].isoformat() if row[2] else None,
                        "dry_run":    bool(row[3]),
                        "recipients": row[4],
                    })
        except Exception:
            pass
        finally:
            try:
                c.close()
            except Exception:
                pass
    return jsonify(
        ok=True,
        kill_switch=_kill_switch_on(),
        dry_run_env=_dry_run_env(),
        recipients=_recipients(),
        from_addr=_from_addr(),
        resend_configured=bool(_RESEND_KEY),
        admin_key_set=bool(_admin_key()),
        recent_sends=last_sends,
    ), 200
