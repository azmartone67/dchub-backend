"""Free-tier upgrade nudge — automated outreach to ACTIVE free-key users.

Owner request 2026-06-12 ("need an automated outreach email that goes to free
users to advise them for upgrades"). Modeled on campaign_halfprice_annual's
preview/fire/log shape: NOTHING sends without an explicit admin fire call.

Audience (honest sizing at build time: ~2-6 people — only 6 of 40 free keys
have an email at all; this compounds as identity capture improves):
  mcp_dev_keys tier='free' with a real email, >= min_calls in the window,
  not nudged within the cooldown (default 45d).

The unlock that makes this email different from generic marketing: each
recipient gets a KEY-BOUND one-click checkout — we mint their pair-code and
bake it into the Stripe URL as client_reference_id, so the webhook's existing
DCM- branch flips THEIR key the moment payment completes. No signup, no key
swap. (Same one-hop flow the /upgrade paywall redirect now uses.)

Endpoints (all admin: X-Admin-Key header or ?admin_key=):
  GET  /api/v1/admin/upgrade-nudge/preview   — candidates + rendered sample
  POST /api/v1/admin/upgrade-nudge/fire      — ?confirm=SEND to go live
  GET  /api/v1/admin/upgrade-nudge/log       — send history
Cron: intentionally NOT scheduled until the owner approves the preview copy.
"""
import os
import json
import hashlib
from datetime import datetime, timezone

import requests
from flask import Blueprint, request, jsonify
from routes._swallowed_writes import note_swallowed_write

free_upgrade_nudge_bp = Blueprint("free_upgrade_nudge", __name__)

_FROM_NAME  = os.environ.get("DCHUB_FROM_NAME",  "DC Hub")
_FROM_EMAIL = os.environ.get("DCHUB_FROM_EMAIL", "alerts@dchub.cloud")
_RESEND_KEY = (os.environ.get("DCHUB_RESEND_API_KEY")
               or os.environ.get("RESEND_API_KEY") or "").strip()


def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY", "")
                or os.environ.get("DCHUB_INTERNAL_KEY", ""))
    got = (request.headers.get("X-Admin-Key", "")
           or request.args.get("admin_key", ""))
    return bool(expected) and got == expected


def _conn():
    from db_utils import get_db
    return get_db()


def _ensure_schema() -> None:
    """Create upgrade_nudge_log via db_utils.ddl_cursor — never safe_db.

    2026-08-04: this used `safe_db()`, whose PGCursorWrapper SKIPS DDL when
    SKIP_DDL is set — and it defaults to '1' (db_utils.py:13) and is unset in
    prod. So the CREATE below has never run, and the failure was worse than a
    missing log: the candidate query at ~line 145 filters on `NOT EXISTS
    (SELECT 1 FROM upgrade_nudge_log ...)`, which raises against a table that
    does not exist. A dead nudge table means ZERO candidates, not just an
    unrecorded send.

    The note left here in June — "the missing table 500'd the preview while
    this except hid the reason" — diagnosed the symptom and added a print. The
    CREATE still never ran, because the wrapper was eating it. Caught by
    scripts/check_ddl_through_pool.py.

    The hand-rolled psycopg2 block this replaced was the 26th copy of the same
    escape hatch in this tree; `ddl_cursor` is now the one blessed way, so the
    next module does not have to rediscover it.
    """
    try:
        from db_utils import ddl_cursor
        with ddl_cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS upgrade_nudge_log (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    api_key_hash TEXT,
                    pair_code TEXT,
                    calls_window INT,
                    top_tools TEXT,
                    mode TEXT,
                    resend_message_id TEXT,
                    sent_at TIMESTAMPTZ DEFAULT NOW()
                )""")
    except Exception as _se:
        # never swallow silently — the first deploy did, and the missing table
        # 500'd the preview while this except hid the reason
        print(f"[upgrade-nudge] schema ensure failed: {_se}", flush=True)


def _key_hash(k: str) -> str:
    return hashlib.sha256((k or "").encode()).hexdigest()[:16]


# Phase 3 — CAN-SPAM suppression + one-click unsubscribe helpers. Guarded so
# a missing/broken module degrades gracefully and never blocks a send.
def _suppression():
    """Return (is_suppressed, unsub_link, list_unsubscribe_headers) or (None,)*3."""
    try:
        from routes.email_suppression import (
            is_suppressed, unsub_link, list_unsubscribe_headers)
        return is_suppressed, unsub_link, list_unsubscribe_headers
    except Exception:
        try:
            from email_suppression import (
                is_suppressed, unsub_link, list_unsubscribe_headers)
            return is_suppressed, unsub_link, list_unsubscribe_headers
        except Exception:
            return None, None, None


def _is_suppressed_email(email: str) -> bool:
    """True only if the suppression list positively contains `email`.

    Opens a short-lived safe_db cursor and unwraps to the raw psycopg2 cursor
    (db_utils PGCursorWrapper trap) before handing it to is_suppressed.
    """
    is_sup, _ul, _lh = _suppression()
    if not is_sup:
        return False
    try:
        from db_utils import safe_db
        with safe_db() as c:
            cur = c.cursor()
            raw = getattr(cur, "_cur", cur)
            return bool(is_sup(raw, email))
    except Exception:
        return False


def _candidates(min_calls: int, days: int, cooldown_days: int,
                include_internal: bool = False) -> list[dict]:
    out: list[dict] = []
    # First live preview surfaced ONLY our own QA inboxes (qa-deep-scan@,
    # final-test-*, audit-*, smoke+final@) — exclude internal/test addresses
    # by default; ?include_internal=1 keeps them reachable for live-send tests.
    internal_filter = "" if include_internal else """
               AND k.email NOT ILIKE '%%@dchub.cloud'
               AND k.email NOT ILIKE 'qa-%%'
               AND k.email NOT ILIKE 'audit-%%'
               AND k.email NOT ILIKE 'smoke%%'
               AND k.email NOT ILIKE '%%test%%'
               AND k.email NOT ILIKE '%%demo%%'"""
    from db_utils import safe_db
    with safe_db() as c:
        cur = c.cursor()
        cur.execute(f"""
            SELECT k.email, k.api_key, COUNT(l.id) AS calls
              FROM mcp_dev_keys k
              JOIN mcp_call_log l ON l.api_key = k.api_key
             WHERE k.tier = 'free'
               AND COALESCE(k.email,'') <> ''{internal_filter}
               AND k.metadata->>'marketing_opt_in' = 'true'
               AND NOT EXISTS (SELECT 1 FROM email_suppression s
                                WHERE lower(s.email) = lower(k.email))
               AND l.timestamp > NOW() - INTERVAL '{int(days)} days'
               AND NOT EXISTS (
                   SELECT 1 FROM upgrade_nudge_log n
                    WHERE n.email = k.email
                      AND n.sent_at > NOW() - INTERVAL '{int(cooldown_days)} days'
                      AND n.mode = 'live')
             GROUP BY k.email, k.api_key
            HAVING COUNT(l.id) >= %s
             ORDER BY calls DESC
             LIMIT 50
        """, (min_calls,))
        rows = cur.fetchall()
        for email, api_key, calls in rows:
            cand = {"email": email, "api_key": api_key,
                    "api_key_hash": _key_hash(api_key),
                    "calls_window": int(calls), "top_tools": []}
            # Top tools are personalization sugar — never fail the run on a
            # schema mismatch here.
            try:
                cur.execute("""
                    SELECT tool, COUNT(*) FROM mcp_call_log
                     WHERE api_key = %s
                       AND timestamp > NOW() - INTERVAL '30 days'
                     GROUP BY tool ORDER BY 2 DESC LIMIT 3""", (api_key,))
                cand["top_tools"] = [r[0] for r in cur.fetchall() if r and r[0]]
            except Exception:
                try:
                    c.rollback()
                except Exception:
                    pass
            out.append(cand)
    return out


def _checkout_links(api_key: str, ref: str) -> dict:
    """Key-bound Developer link (their key auto-upgrades on payment) +
    attribution-only metered link."""
    from routes._stripe_links import STRIPE_LINKS
    links = {"developer": None, "metered": None, "pair_code": None}
    dev = STRIPE_LINKS.get("developer")
    met = STRIPE_LINKS.get("metered")
    code = None
    try:
        from routes.pair_code import get_or_create_code
        minted = get_or_create_code(api_key, tool_name="upgrade_nudge_email")
        code = (minted or {}).get("code")
    except Exception:
        code = None
    if dev:
        sep = "&" if "?" in dev else "?"
        links["developer"] = (f"{dev}{sep}client_reference_id={code}"
                              if code else
                              f"{dev}{sep}client_reference_id=nudge:{ref}")
    if met:
        sep = "&" if "?" in met else "?"
        links["metered"] = f"{met}{sep}client_reference_id=nudge-metered:{ref}"
    links["pair_code"] = code
    return links


def _render(cand: dict, links: dict) -> tuple[str, str]:
    calls = cand["calls_window"]
    tools = cand.get("top_tools") or []
    tools_clause = (
        f" — mostly <b>{tools[0]}</b>" + (f" and <b>{tools[1]}</b>" if len(tools) > 1 else "")
        if tools else "")
    _cw = f"{calls:,} call" + ("s" if calls != 1 else "")
    subject = f"Your DC Hub key made {_cw} this month — here's the full-data unlock"
    dev_link = links.get("developer") or "https://dchub.cloud/pricing"
    met_link = links.get("metered") or "https://dchub.cloud/pricing"

    # Real tokenized one-click unsubscribe (replaces bare "Reply STOP").
    _is_sup, _unsub_link, _lh = _suppression()
    unsub_url = None
    if _unsub_link:
        try:
            unsub_url = _unsub_link(cand.get("email"))
        except Exception:
            unsub_url = None
    if unsub_url:
        unsub_footer = (f'<a href="{unsub_url}" style="color:#999">Unsubscribe</a> '
                        f'and we\'ll never send another upgrade note. '
                        f'Either way we won\'t send this more than once every 45 days.')
    else:
        unsub_footer = ("Reply STOP and we'll never send another upgrade note. "
                        "Either way we won't send this more than once every 45 days.")
    body = f"""
<div style="font-family:-apple-system,Segoe UI,Inter,sans-serif;max-width:560px;margin:0 auto;color:#1a1a2e">
  <p>Hi,</p>
  <p>Your DC Hub API key made <b>{_cw} in the last 30 days</b>{tools_clause}.
     You're on the <b>free tier</b>: 10 calls/day on paid tools and preview-sized results —
     so your agent is reasoning from a fraction of the data on every gated call.</p>
  <p><b>One click unlocks your existing key</b> (no signup, no key swap — payment
     completes and the key you're already using flips to Developer instantly):</p>
  <p style="margin:18px 0">
    <a href="{dev_link}"
       style="background:#6366f1;color:#fff;padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:700">
       Upgrade this key — Developer · $49/mo</a></p>
  <p style="font-size:14px;color:#444">Developer = 500 MCP calls/day, full result sets,
     grid intelligence + fiber intel ungated. Prefer no subscription?
     <a href="{met_link}">$10 one-time = 1,000 API calls</a>.</p>
  <p style="font-size:13px;color:#666">Questions? Just reply — this inbox is read by a human.
     Full plans: <a href="https://dchub.cloud/pricing">dchub.cloud/pricing</a></p>
  <p style="font-size:12px;color:#999">You're receiving this because your API key has been
     actively calling DC Hub. {unsub_footer}</p>
</div>"""
    return subject, body


def _send_resend(to_email: str, subject: str, html: str,
                 unsub_headers: dict | None = None) -> tuple[bool, str]:
    if not _RESEND_KEY:
        return False, "no_resend_key"
    try:
        _json = {"from": f"{_FROM_NAME} <{_FROM_EMAIL}>",
                 "to": [to_email], "subject": subject, "html": html}
        # RFC 2369 + RFC 8058 one-click unsubscribe headers (Resend "headers").
        if unsub_headers:
            _json["headers"] = {str(k): str(v) for k, v in unsub_headers.items()}
        r = requests.post(
            "https://api.resend.com/emails",
            json=_json,
            headers={"Authorization": f"Bearer {_RESEND_KEY}",
                     "User-Agent": "DCHub-UpgradeNudge/1.0 (+https://dchub.cloud)"},
            timeout=15)
        if 200 <= r.status_code < 300:
            return True, (r.json() or {}).get("id", "")
        return False, f"http_{r.status_code}:{r.text[:120]}"
    except Exception as e:
        return False, str(e)[:150]


@free_upgrade_nudge_bp.route("/api/v1/admin/upgrade-nudge/preview", methods=["GET"], strict_slashes=False)
def nudge_preview():
    if not _admin_ok():
        return jsonify({"error": "admin auth required"}), 403
    _ensure_schema()
    min_calls = int(request.args.get("min_calls", "5"))
    days = int(request.args.get("days", "30"))
    cooldown = int(request.args.get("cooldown_days", "45"))
    include_internal = (request.args.get("include_internal") or "") in ("1", "true")
    cands = _candidates(min_calls, days, cooldown, include_internal)
    sample = None
    if cands:
        links = _checkout_links(cands[0]["api_key"], cands[0]["api_key_hash"])
        subj, html = _render(cands[0], links)
        sample = {"to": cands[0]["email"], "subject": subj, "html": html,
                  "pair_code": links.get("pair_code")}
    return jsonify({
        "mode": "preview — nothing sent",
        "audience_note": ("only free keys WITH an email qualify; most free keys "
                          "are anonymous claim-keys — identity capture grows this list"),
        "criteria": {"tier": "free", "min_calls": min_calls, "days": days,
                     "cooldown_days": cooldown},
        "candidates": [{k: v for k, v in c.items() if k != "api_key"} for c in cands],
        "would_send": len(cands),
        "sample_email": sample,
        "resend_configured": bool(_RESEND_KEY),
        "fire_with": "POST /api/v1/admin/upgrade-nudge/fire?confirm=SEND",
    })


@free_upgrade_nudge_bp.route("/api/v1/admin/upgrade-nudge/fire", methods=["POST"], strict_slashes=False)
def nudge_fire():
    if not _admin_ok():
        return jsonify({"error": "admin auth required"}), 403
    _ensure_schema()
    live = (request.args.get("confirm") or "") == "SEND"
    min_calls = int(request.args.get("min_calls", "5"))
    days = int(request.args.get("days", "30"))
    cooldown = int(request.args.get("cooldown_days", "45"))
    cap = int(request.args.get("cap", "25"))
    include_internal = (request.args.get("include_internal") or "") in ("1", "true")
    cands = _candidates(min_calls, days, cooldown, include_internal)[:cap]
    results = []
    from db_utils import safe_db
    _is_sup, _ulink, _lheaders = _suppression()
    for cand in cands:
        # Phase 3 — honor CAN-SPAM suppression list (opted-out addresses).
        if _is_suppressed_email(cand["email"]):
            results.append({"to": cand["email"], "ok": False,
                            "detail": "skipped_suppressed",
                            "pair_code": None})
            continue
        links = _checkout_links(cand["api_key"], cand["api_key_hash"])
        subj, html = _render(cand, links)
        # RFC 2369 + RFC 8058 one-click unsubscribe headers.
        unsub_headers = None
        if _lheaders:
            try:
                unsub_headers = _lheaders(cand["email"])
            except Exception:
                unsub_headers = None
        if live:
            ok, detail = _send_resend(cand["email"], subj, html,
                                      unsub_headers=unsub_headers)
        else:
            ok, detail = True, "dry_run"
        results.append({"to": cand["email"], "ok": ok, "detail": detail,
                        "pair_code": links.get("pair_code")})
        try:
            with safe_db() as c:
                cur = c.cursor()
                cur.execute("""
                    INSERT INTO upgrade_nudge_log
                        (email, api_key_hash, pair_code, calls_window,
                         top_tools, mode, resend_message_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (cand["email"], cand["api_key_hash"], links.get("pair_code"),
                     cand["calls_window"], json.dumps(cand.get("top_tools") or []),
                     "live" if live else "dry_run",
                     detail if (live and ok) else None))
                c.commit()
        except Exception:
            note_swallowed_write("upgrade_nudge_log", where="free_upgrade_nudge.nudge_fire")
            pass
    return jsonify({"mode": "live" if live else "dry_run",
                    "sent": sum(1 for r in results if r["ok"]),
                    "results": results})


@free_upgrade_nudge_bp.route("/api/v1/admin/upgrade-nudge/log", methods=["GET"], strict_slashes=False)
def nudge_log():
    if not _admin_ok():
        return jsonify({"error": "admin auth required"}), 403
    _ensure_schema()
    out = []
    try:
        from db_utils import safe_db
        with safe_db() as c:
            cur = c.cursor()
            cur.execute("""
                SELECT email, pair_code, calls_window, mode, sent_at
                  FROM upgrade_nudge_log ORDER BY sent_at DESC LIMIT 100""")
            for r in cur.fetchall():
                out.append({"email": r[0], "pair_code": r[1],
                            "calls_window": r[2], "mode": r[3],
                            "sent_at": r[4].isoformat() if r[4] else None})
    except Exception:
        pass
    return jsonify({"log": out})
