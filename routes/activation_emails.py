"""routes/activation_emails.py — paid-customer ACTIVATION emails (2026-09-02).

★ WHY (QA sweep 2026-09-02, finding 5:9 — "paid keys are not used → early
cancels"). At 00:31Z on 2026-09-02, /api/v1/admin/customer-lookup showed
17/17 paying customers with api_keys.calls_total = 0 and last_used_at NULL.
Stripe showed the two fastest cancellations ($49 after 4 days, $9 after 6
days) — both buyers never made a call. The welcome path is healthy (it
delivers a key) but it delivers a KEY, not a first RESULT. The only other
activation instrument, upgrade_nudger, last fired 2026-06-12 (2 QA rows) and
/api/v1/nudges/log counted 0.

Two steps, each sent at most ONCE per customer, ever:

  day1_first_query   >= 20h after the paid account was created: the customer's
                     REAL Claude connector URL (the dch_live_ key the welcome
                     path minted) plus ONE concrete, paste-able tool call that
                     is PAID-ONLY (mcp_upgrade_gate.PAID_ONLY_TOOLS) — so the
                     first thing they run is something the free tier cannot.
  day3_no_usage      >= 3d after creation AND still zero usage on every key
                     (api_keys.calls_total = 0 for all REST keys, and no
                     mcp_dev_keys.last_used_at for the email): a short
                     founder-voice "reply and I'll run your first query"
                     nudge. Skipped forever once any usage exists.

★ SHIPS DARK. Customer sends were deliberately disarmed in this repo before
(the nudger, the drips); a new sender that arms itself on merge is exactly the
class of change that has cost this owner money. `ACTIVATION_EMAILS_ENABLED`
must be EXACTLY the string "1" for a single email to leave; unset, "true",
"yes", "on" all mean OFF. The disarmed sweep still computes and REPORTS its
candidates (would_send) so the owner can read what one flip would do.
tools/kill_switch_probe.py registers the switch with intent OFF and checks
that the running process agrees (stats.enabled) and that nothing was sent.

★ IDEMPOTENT BY LEDGER, not by clock. activation_email_ledger has
UNIQUE (customer_id, step); a row is RESERVED with INSERT … ON CONFLICT DO
NOTHING before the send and the send happens only when that insert landed
(rowcount = 1). A repeat heartbeat, a second replica, or a restart mid-sweep
therefore cannot send twice. The reservation is updated with the outcome
after the send. tests/test_activation_emails.py mutation-proves both the
ON CONFLICT clause and the UNIQUE constraint.

Routes (admin, X-Admin-Key header ONLY — fail-closed when the env is unset):
  POST /api/v1/admin/activation-emails/run     the sweep (cron_heartbeat)
  GET  /api/v1/admin/activation-emails/stats   enabled + sends in window
                                               (read by kill_switch_probe)

No module-scope side effects beyond the Blueprint. No DDL at import.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
activation_emails_bp = Blueprint("activation_emails", __name__)

ENV_SWITCH = "ACTIVATION_EMAILS_ENABLED"
STEP_DAY1 = "day1_first_query"
STEP_DAY3 = "day3_no_usage"
STEPS = (STEP_DAY1, STEP_DAY3)

DAY1_AFTER_HOURS = 20          # "day 1": not before the welcome has landed
DAY3_AFTER_HOURS = 72
LOOKBACK_DAYS = 14             # never mail an account older than this
MAX_SENDS_PER_RUN = 25         # a runaway candidate query cannot mass-mail

PAID_PLANS = ("developer", "pro", "paid", "enterprise", "founding")

_FROM_EMAIL = "jonathan@dchub.cloud"
_FROM_NAME = "Jonathan at DC Hub"
_CONNECT_BASE = "https://dchub.cloud/mcp"

# ★ The pre-filled first query. BOTH tools are in mcp_upgrade_gate.
# PAID_ONLY_TOOLS (asserted by the test, so a gate change cannot leave this
# email recommending a tool the free tier already answers). The analyze_site
# arguments are the tool's own documented example (a Phoenix parcel, 100 MW).
FIRST_QUERY_TOOL = "analyze_site"
FIRST_QUERY_ARGS = {"lat": 33.45, "lon": -112.07, "capacity_mw": 100, "state": "AZ"}
SECOND_QUERY_TOOL = "get_grid_intelligence"
SECOND_QUERY_ARGS = {"iso": "ERCOT"}


def enabled(env=None) -> bool:
    """True ONLY when ACTIVATION_EMAILS_ENABLED is exactly "1"."""
    env = os.environ if env is None else env
    return (env.get(ENV_SWITCH) or "") == "1"


def _admin_ok() -> bool:
    """Fail-CLOSED: no configured key => nobody is admin (header only)."""
    sent = (request.headers.get("X-Admin-Key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(expected) and bool(sent) and sent == expected


def _conn():
    """RAW psycopg2, autocommit (db_utils' SKIP_DDL would no-op the DDL)."""
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        return None
    try:
        import psycopg2
        c = psycopg2.connect(dsn, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception as e:  # noqa: BLE001
        logger.warning("[activation] connect failed: %s", str(e)[:120])
        return None


# ── ledger ──────────────────────────────────────────────────────────────────

LEDGER_DDL = (
    "CREATE TABLE IF NOT EXISTS activation_email_ledger ("
    " id BIGSERIAL PRIMARY KEY,"
    " customer_id TEXT NOT NULL,"
    " step TEXT NOT NULL,"
    " email TEXT,"
    " reserved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
    " sent_at TIMESTAMPTZ,"
    " status TEXT NOT NULL DEFAULT 'reserved',"
    " delivery_info TEXT,"
    " UNIQUE (customer_id, step))"
)

# ★ The idempotency key. ON CONFLICT (customer_id, step) DO NOTHING is in the
# SAME string literal as the INSERT (regression_lint requires that) and is
# what makes a repeat sweep a no-op: rowcount 0 => somebody already holds
# this (customer, step) => do not send.
CLAIM_SQL = (  # ONE literal: regression_lint reads INSERT..ON CONFLICT per string
    "INSERT INTO activation_email_ledger (customer_id, step, email) VALUES (%s, %s, %s) ON CONFLICT (customer_id, step) DO NOTHING"
)


def ensure_ledger(cur) -> None:
    cur.execute(LEDGER_DDL)


def claim(cur, customer_id: str, step: str, email: str) -> bool:
    """Reserve (customer_id, step). True iff THIS call reserved it."""
    cur.execute(CLAIM_SQL, (str(customer_id), step, (email or "").lower()))
    return int(getattr(cur, "rowcount", 0) or 0) == 1


def record_outcome(cur, customer_id: str, step: str, ok: bool, info: str,
                   now=None) -> None:
    """Stamp the send outcome. `now` is the sweep's clock, NOT wall time.

    r-clock-injection (2026-09-04): this stamped sent_at with
    _dt.datetime.now() while run_sweep threaded an injected `now` through
    everything else and its docstring promised "Pure with respect to time
    (now)". The ledger therefore recorded a DIFFERENT instant than the sweep
    that wrote it, and read_stats' window is computed from the caller's clock —
    so a window query could count a send that the same clock says is outside
    it. test_stats_publish_the_process_view_and_window_sends encoded the
    correct behaviour and passed only while wall time happened to sit inside
    the window: its fixed NOW (2026-09-02 06:00Z) put the boundary at
    2026-09-04 04:00Z, and the suite went red at exactly that instant and
    stayed red. A time bomb, not a flake.
    """
    cur.execute(
        "UPDATE activation_email_ledger SET status = %s, sent_at = %s, "
        "delivery_info = %s WHERE customer_id = %s AND step = %s",
        ("sent" if ok else "failed",
         (now or _dt.datetime.now(_dt.timezone.utc)) if ok else None,
         (info or "")[:200], str(customer_id), step))


# ── candidates ──────────────────────────────────────────────────────────────

# Paid = the honest filter (stripe_customer_id NOT NULL) on a paid plan,
# created inside the lookback. One row per customer with the aggregate usage
# of EVERY key that belongs to them: REST keys by user_id, MCP keys by email.
CANDIDATES_SQL = (
    "SELECT u.id::text AS customer_id, lower(u.email) AS email, u.plan, "
    "       u.created_at, "
    "       COALESCE((SELECT SUM(COALESCE(k.calls_total, 0)) FROM api_keys k "
    "                  WHERE k.user_id::text = u.id::text), 0) AS rest_calls, "
    "       (SELECT MAX(m.last_used_at) FROM mcp_dev_keys m "
    "         WHERE lower(m.email) = lower(u.email)) AS mcp_last_used_at, "
    "       (SELECT m.api_key FROM mcp_dev_keys m "
    "         WHERE lower(m.email) = lower(u.email) AND m.status = 'active' "
    "         ORDER BY m.created_at DESC LIMIT 1) AS mcp_key "
    "  FROM users u "
    " WHERE u.stripe_customer_id IS NOT NULL "
    "   AND lower(COALESCE(u.plan, '')) IN %s "
    "   AND u.created_at >= NOW() - (%s * INTERVAL '1 day') "
    "   AND COALESCE(u.email, '') <> '' "
    " ORDER BY u.created_at"
)


def fetch_candidates(cur, lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    cur.execute(CANDIDATES_SQL, (tuple(PAID_PLANS), int(lookback_days)))
    out = []
    for r in cur.fetchall() or []:
        if isinstance(r, dict):
            d = dict(r)
        else:
            d = {"customer_id": r[0], "email": r[1], "plan": r[2], "created_at": r[3],
                 "rest_calls": r[4], "mcp_last_used_at": r[5], "mcp_key": r[6]}
        out.append(d)
    return out


def has_usage(c: dict) -> bool:
    return int(c.get("rest_calls") or 0) > 0 or c.get("mcp_last_used_at") is not None


def _age_hours(created_at, now: _dt.datetime) -> float | None:
    if created_at is None:
        return None
    if getattr(created_at, "tzinfo", None) is None:
        created_at = created_at.replace(tzinfo=_dt.timezone.utc)
    return (now - created_at).total_seconds() / 3600.0


def due_steps(c: dict, now: _dt.datetime) -> list[tuple[str, str | None]]:
    """(step, skip_reason) per step. skip_reason None => send it.

    Pure. Ordering is fixed so a customer who is both >=20h and >=72h old
    gets day-1 first (they are independent ledger rows; day-3 is withheld
    whenever ANY usage exists)."""
    age = _age_hours(c.get("created_at"), now)
    out = []
    if age is None:
        return [(STEP_DAY1, "no_created_at"), (STEP_DAY3, "no_created_at")]
    if age >= DAY1_AFTER_HOURS:
        out.append((STEP_DAY1, None if c.get("mcp_key") else "no_mcp_key"))
    if age >= DAY3_AFTER_HOURS:
        out.append((STEP_DAY3, "has_usage" if has_usage(c) else None))
    return out


# ── the two emails ──────────────────────────────────────────────────────────

def connect_url(mcp_key: str) -> str:
    return f"{_CONNECT_BASE}?api_key={mcp_key}"


def _json_args(args: dict) -> str:
    import json
    return json.dumps(args, separators=(", ", ": "))


def render_day1(email: str, mcp_key: str) -> tuple[str, str]:
    url = connect_url(mcp_key)
    q1 = f"{FIRST_QUERY_TOOL} {_json_args(FIRST_QUERY_ARGS)}"
    q2 = f"{SECOND_QUERY_TOOL} {_json_args(SECOND_QUERY_ARGS)}"
    subject = "Your first DC Hub query, pre-filled (60 seconds)"
    html = f"""<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:600px;margin:0 auto;color:#1f2937;line-height:1.55">
<p>Hi &mdash; Jonathan here, the person who built DC Hub.</p>
<p>Your key is live, but a key is not a result. Here is the whole first run, ready to paste.</p>
<p><strong>1. Connect (once).</strong> Claude.ai &rarr; Settings &rarr; Connectors &rarr; Add custom connector, paste:</p>
<div style="background:#1a1a2e;color:#00d4ff;padding:14px 16px;border-radius:8px;font-family:monospace;font-size:13px;word-break:break-all">{url}</div>
<p style="font-size:13px;color:#6a6a7a">Claude Desktop / Cursor / Cline instead: send <code>X-API-Key: {mcp_key}</code>.</p>
<p><strong>2. Ask this, verbatim</strong> (it is a paid-only tool &mdash; the free tier cannot answer it):</p>
<div style="background:#f3f4f6;padding:14px 16px;border-radius:8px;font-family:monospace;font-size:13px">Use the DC Hub tool <code>{q1}</code> and tell me the overall score, the weakest factor, and how far the nearest substation is.</div>
<p>Then try: <code style="font-size:13px">{q2}</code> &mdash; live grid headroom and queue depth for one ISO.</p>
<p>If either returns anything other than a scored answer, reply to this email and I will run it for you and send the output back.</p>
<p style="color:#6a6a7a;font-size:13px">Sent once, on your first day. Full tool list: <a href="https://dchub.cloud/mcp" style="color:#0b5cad">dchub.cloud/mcp</a></p>
</div>"""
    return subject, html


def render_day3(email: str) -> tuple[str, str]:
    subject = "Want me to run your first DC Hub query for you?"
    html = """<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:600px;margin:0 auto;color:#1f2937;line-height:1.55">
<p>Hi &mdash; Jonathan again.</p>
<p>Three days in, your DC Hub key has not made a call yet. That is usually a connector that did not stick, not a lack of questions.</p>
<p>Reply with one sentence about the site, market or ISO you are looking at and I will run the query on your account and email you the scored output &mdash; or, if the connector is the problem, a working URL.</p>
<p style="color:#6a6a7a;font-size:13px">Sent once. If you would rather not hear from me about activation, reply "no" and that is the end of it.</p>
</div>"""
    return subject, html


def render(step: str, c: dict) -> tuple[str, str]:
    if step == STEP_DAY1:
        return render_day1(c.get("email") or "", c.get("mcp_key") or "")
    return render_day3(c.get("email") or "")


# ── sender ──────────────────────────────────────────────────────────────────

def send_via_resend(to_email: str, subject: str, html: str) -> tuple[bool, str]:
    """Transactional send (onboarding of a paying customer). Founder from/
    reply-to so a reply reaches a human, as the welcome path does."""
    key = (os.environ.get("DCHUB_RESEND_API_KEY")
           or os.environ.get("RESEND_API_KEY") or "").strip()
    if not key:
        return False, "no_resend_key"
    try:
        import requests
        r = requests.post(
            "https://api.resend.com/emails",
            json={"from": f"{_FROM_NAME} <{_FROM_EMAIL}>", "to": [to_email],
                  "reply_to": _FROM_EMAIL, "subject": subject, "html": html},
            headers={"Authorization": f"Bearer {key}"}, timeout=15)
        ok = 200 <= int(r.status_code) < 300
        return ok, (f"sent_{r.status_code}" if ok
                    else f"status_{r.status_code}_{(r.text or '')[:80]}")
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}:{str(e)[:60]}"


# ── the sweep ───────────────────────────────────────────────────────────────

def run_sweep(conn, sender=None, now=None, armed: bool | None = None,
              max_sends: int = MAX_SENDS_PER_RUN) -> dict:
    """One pass. Pure with respect to time (now) and the wire (sender).

    armed=False (the default when the env switch is not exactly "1") reads
    candidates and reports would_send, and writes NOTHING — not even a ledger
    reservation, so arming later sends exactly what the dry run listed."""
    armed = enabled() if armed is None else bool(armed)
    sender = sender or send_via_resend
    now = now or _dt.datetime.now(_dt.timezone.utc)
    out = {"ok": True, "enabled": armed, "switch": ENV_SWITCH,
           "candidates": 0, "would_send": [], "sent": 0, "skipped": 0,
           "already_sent": 0, "errors": 0, "sends": [], "skips": []}
    if conn is None:
        out.update(ok=False, error="no_db")
        return out
    with conn.cursor() as cur:
        if armed:
            ensure_ledger(cur)
        cands = fetch_candidates(cur)
        out["candidates"] = len(cands)
        for c in cands:
            for step, why in due_steps(c, now):
                if why:
                    out["skipped"] += 1
                    out["skips"].append({"customer_id": c["customer_id"], "step": step,
                                         "reason": why})
                    continue
                if not armed:
                    out["would_send"].append({"customer_id": c["customer_id"],
                                              "email": c["email"], "step": step})
                    continue
                if out["sent"] >= max_sends:
                    out["skipped"] += 1
                    out["skips"].append({"customer_id": c["customer_id"], "step": step,
                                         "reason": "max_sends_per_run"})
                    continue
                try:
                    if not claim(cur, c["customer_id"], step, c["email"]):
                        out["already_sent"] += 1
                        continue
                    subject, html = render(step, c)
                    ok, info = sender(c["email"], subject, html)
                    record_outcome(cur, c["customer_id"], step, ok, info,
                                   now=now)
                    if ok:
                        out["sent"] += 1
                        out["sends"].append({"customer_id": c["customer_id"],
                                             "email": c["email"], "step": step})
                    else:
                        out["errors"] += 1
                        out["skips"].append({"customer_id": c["customer_id"], "step": step,
                                             "reason": "send_failed:" + str(info)[:60]})
                except Exception as e:  # noqa: BLE001
                    out["errors"] += 1
                    out["skips"].append({"customer_id": c.get("customer_id"), "step": step,
                                         "reason": f"{type(e).__name__}:{str(e)[:60]}"})
    if not armed:
        out["note"] = (f"DISARMED: {ENV_SWITCH} is not exactly '1'. Nothing was sent and "
                       "nothing was written; would_send lists what arming would send.")
    return out


def read_stats(conn, now=None, window_hours: int = 2) -> dict:
    """What the kill-switch probe reads: the running process's own view of the
    switch plus sends in the window. A missing table is 0 sends (the ledger
    is created on the first ARMED sweep, so absent == never armed)."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    out = {"ok": True, "enabled": enabled(), "switch": ENV_SWITCH,
           "window_hours": int(window_hours), "sent_in_window": 0,
           "sent_total": 0, "by_step": {}, "last_sent_at": None, "ledger_exists": False}
    if conn is None:
        out.update(ok=False, error="no_db")
        return out
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.activation_email_ledger') IS NOT NULL")
            row = cur.fetchone()
            out["ledger_exists"] = bool(row and row[0])
            if not out["ledger_exists"]:
                return out
            cur.execute(
                "SELECT step, COUNT(*) FILTER (WHERE status = 'sent'), "
                "       COUNT(*) FILTER (WHERE status = 'sent' AND sent_at >= %s), "
                "       MAX(sent_at) FILTER (WHERE status = 'sent') "
                "  FROM activation_email_ledger GROUP BY step",
                (now - _dt.timedelta(hours=int(window_hours)),))
            last = None
            for step, total, win, mx in cur.fetchall() or []:
                out["by_step"][step] = {"sent_total": int(total or 0),
                                        "sent_in_window": int(win or 0)}
                out["sent_total"] += int(total or 0)
                out["sent_in_window"] += int(win or 0)
                if mx is not None and (last is None or mx > last):
                    last = mx
            out["last_sent_at"] = last.isoformat() if hasattr(last, "isoformat") else last
    except Exception as e:  # noqa: BLE001
        out.update(ok=False, error=str(e)[:120])
    return out


# ── routes ──────────────────────────────────────────────────────────────────

@activation_emails_bp.route("/api/v1/admin/activation-emails/run", methods=["POST"])
def activation_emails_run():
    if not _admin_ok():
        return jsonify(error="unauthorized", hint="X-Admin-Key header required"), 401
    c = _conn()
    try:
        out = run_sweep(c)
    finally:
        try:
            if c is not None:
                c.close()
        except Exception:
            pass
    return jsonify(out), (200 if out.get("ok") else 503)


@activation_emails_bp.route("/api/v1/admin/activation-emails/stats", methods=["GET"])
def activation_emails_stats():
    if not _admin_ok():
        return jsonify(error="unauthorized", hint="X-Admin-Key header required"), 401
    try:
        hours = max(1, min(int(request.args.get("hours", 2)), 168))
    except Exception:
        hours = 2
    c = _conn()
    try:
        out = read_stats(c, window_hours=hours)
    finally:
        try:
            if c is not None:
                c.close()
        except Exception:
            pass
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp, (200 if out.get("ok") else 503)
