"""routes/activation_desk.py — the ten people who paid and never called.

WHY THIS EXISTS. `brain_escalation_queue` (PR #3310) is the catcher and it
works: measured 2026-09-05 it holds 10 rows, every one `status='open'`,
`calls_at_open=0`, none wrongly auto-activated, and the daily sync has run clean
every day. The gap is one step further down — NOTHING CONSUMES THE QUEUE. No
route, no digest, no surface reads `brain_escalations` except its own module and
its tests. Ten named payers had sat open for 7.3 days with `contacted_at` NULL
on all ten, and zero rows had ever left `open` in the table's life.

Every row carries the same verdict from `customer_white_glove._classify`:

    ESCALATE: nudged 47d ago, still zero calls — automated nudge FAILED.
    Human touch (call / personal note), not another email.

★ SO THIS SURFACE NEVER SENDS. There is no outbound path in this module, by
design. The classifier's finding is that the automated email already failed
9-for-9; a tenth automated email is the thing measured not to work. Drafts are
rendered for a human to edit and send from their own client.

★ ENRICHMENT IS FIRST-PARTY ONLY. Name, company and last_login come from our
own `users` table. Nothing here looks a customer up anywhere else — these are
private individuals and a support queue is not a reason to assemble a profile.

★★★ THE SEGMENT IS THE POINT, AND IT WAS INVISIBLE UNTIL THE JOIN WAS RIGHT.
`saved_lp_sites.user_id` is a KEY hash (`k_<hex>`), not an email, so joining it
against an address returns zero whether or not data exists — an absence of
measurement reading as a measured zero. Joined properly through `users.email`,
the ten split into groups that need OPPOSITE messages:

    LOGGED IN, NEVER CALLED   they came back to the site and still made no API
                              call — gfdickson logged in 2026-08-22 having paid
                              in FEBRUARY, tj@karklins 9 days ago. These people
                              are trying and failing. That is an onboarding
                              defect, and it is the highest-value contact.
    NEVER LOGGED IN           bought a key and never appeared (mgelshteyn,
                              rob@hedmarkholdings). Different problem, different
                              note — possibly a pure-API buyer who needs the
                              key working, not a tour.
    PAID AND VANISHED         last_login == signup: came once, never returned.

Sending all three the same message is what the failed nudge did.

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY) — same gate as integrity_master_shell.
Kill: ACTIVATION_DESK_DISABLE=1

Endpoints:
  GET /api/v1/admin/activation-desk    JSON (rows + drafts + basis)
  GET /admin/activation-desk           HTML desk
"""
from __future__ import annotations

import os
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

activation_desk_bp = Blueprint("activation_desk", __name__)


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("ACTIVATION_DESK_DISABLE") or "").strip() == "1"


def _segment(last_login, created_at, joined_days) -> str:
    """Which of the three shapes is this? Never guessed from the plan or the
    payment — only from whether they came back, which is the thing that
    changes what you would say to them."""
    if last_login is None:
        return "never_logged_in"
    if created_at is not None and last_login <= created_at:
        return "paid_and_vanished"
    return "logged_in_never_called"


_SEG_LABEL = {
    "logged_in_never_called": "logged in, never called — trying and failing",
    "never_logged_in": "never logged in — key bought, never appeared",
    "paid_and_vanished": "came once at signup, never returned",
}


def _draft(row: dict) -> str:
    """A note a human edits and sends. Built ONLY from figures in this payload.

    ★ No invented interest. `saved_searches` and `saved_markets` are empty for
    every one of these accounts, so there is no 'you were looking at X' hook and
    inventing one would be the fabrication this codebase keeps removing. The
    honest hook is the fact itself: you paid, it never ran, what were you after.
    """
    who = (row.get("name") or "").strip().split(" ")[0]
    hi = f"Hi {who}" if who else "Hi"
    plan = row.get("plan") or "paid"
    seg = row.get("segment")
    nudge = row.get("nudge_days")
    nudged_line = (
        f"An automated nudge went out {int(nudge)} days ago and clearly didn't "
        f"land, so this one is from me.\n\n" if nudge else "")

    if seg == "never_logged_in":
        body = (f"you took a {plan} key with DC Hub and it has never made a "
                f"call — and you've never signed in either, so I suspect it "
                f"never got as far as your code.\n\n{nudged_line}"
                f"No pitch. If you tell me what you were trying to query, I'll "
                f"check the key works and send you a request that returns it.")
    elif seg == "paid_and_vanished":
        body = (f"you took a {plan} key with DC Hub, signed in once, and it has "
                f"never made a call.\n\n{nudged_line}"
                f"No pitch — I'd just like to know what you came for. Tell me "
                f"and I'll run it and send back the answer.")
    else:
        body = (f"you took a {plan} key with DC Hub and it has never made a "
                f"call — though you did sign in again recently, so I think you "
                f"tried and something got in the way.\n\n{nudged_line}"
                f"That's a problem at our end, not yours. What were you trying "
                f"to look up? I'll run it myself and send you the answer, and "
                f"fix whatever stopped you.")
    return f"{hi} — {body}"


_SQL = """
    SELECT e.email, e.plan, e.priority, e.reason,
           COALESCE(e.context->>'nudge_days', '')  AS nudge_days,
           COALESCE(e.context->>'joined_days', '') AS joined_days,
           COALESCE(e.context->>'idle_days', '')   AS idle_days,
           ROUND(EXTRACT(EPOCH FROM (NOW() - e.first_seen_at)) / 86400.0, 1)
               AS days_open,
           e.contacted_at IS NOT NULL AS contacted,
           u.name, u.company, u.created_at, u.last_login,
           COALESCE(u.api_calls_total, 0) AS user_calls
      FROM brain_escalations e
      LEFT JOIN users u ON LOWER(u.email) = LOWER(e.email)
     WHERE e.status = 'open'
     ORDER BY e.priority, e.first_seen_at
"""


def _rows(conn):
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '15000'")
    cur.execute(_SQL)
    out = []
    for r in cur.fetchall() or ():
        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        row = {
            "email": r[0], "plan": r[1], "priority": r[2], "reason": r[3],
            "nudge_days": num(r[4]), "joined_days": num(r[5]),
            "idle_days": num(r[6]), "days_open": float(r[7] or 0),
            "contacted": bool(r[8]),
            # ★ `matched_user` is published so an unmatched row is visibly
            #   UNMEASURED rather than silently rendering as "no name, never
            #   logged in" — which would put a real person in the wrong segment.
            "matched_user": r[9] is not None or r[11] is not None,
            "name": r[9], "company": r[10],
            "created_at": str(r[11]) if r[11] else None,
            "last_login": str(r[12]) if r[12] else None,
            "user_calls": int(r[13] or 0),
        }
        row["segment"] = (_segment(r[12], r[11], row["joined_days"])
                          if row["matched_user"] else "unmatched")
        row["segment_label"] = _SEG_LABEL.get(row["segment"],
                                              "no `users` row — segment UNKNOWN")
        row["draft"] = (_draft(row) if row["segment"] != "unmatched" else None)
        out.append(row)
    cur.close()
    return out


def _payload():
    from db_utils import get_read_db
    conn = None
    try:
        conn = get_read_db()
        rows = _rows(conn)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:140]}",
                "rows": None,
                "note": "query failed — the queue is UNREAD, not empty"}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    seg = {}
    for r in rows:
        seg[r["segment"]] = seg.get(r["segment"], 0) + 1
    return {
        "ok": True,
        "open_total": len(rows),
        "by_segment": seg,
        "never_contacted": sum(1 for r in rows if not r["contacted"]),
        "rows": rows,
        "basis": (
            "brain_escalations WHERE status='open', LEFT JOINed to users on "
            "email. Segment is derived from last_login vs created_at only — "
            "never from plan or payment. Drafts are built from the figures in "
            "this payload and nothing else: saved_searches and saved_markets "
            "are empty for every one of these accounts, so there is no prior-"
            "interest hook and inventing one would be fabrication. "
            "THIS ENDPOINT SENDS NOTHING and has no outbound path — the "
            "classifier's own finding is that the automated nudge failed 9/9, "
            "so the next contact has to come from a person."),
    }


@activation_desk_bp.route("/api/v1/admin/activation-desk", methods=["GET"])
def activation_desk_json():
    if _disabled():
        return jsonify({"ok": False, "error": "disabled"}), 503
    if not _admin_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return jsonify(_payload())


@activation_desk_bp.route("/admin/activation-desk", methods=["GET"])
def activation_desk_html():
    if _disabled():
        return Response("disabled\n", status=503)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=\n", status=403)
    d = _payload()
    if not d.get("ok"):
        return Response(
            "<h1>Activation desk</h1><p><strong>Queue UNREAD.</strong> "
            f"{_esc(str(d.get('error')))}</p><p>This is not an empty queue.</p>",
            mimetype="text/html", status=200)

    cards = []
    for r in d["rows"]:
        ident = _esc(r["name"] or r["email"])
        sub = _esc(r["email"]) if r["name"] else ""
        co = f" · {_esc(r['company'])}" if r.get("company") else ""
        ll = _esc(r["last_login"] or "never")
        draft = _esc(r["draft"] or "(no users row — segment unknown, no draft)")
        cards.append(f"""
        <div style="border:1px solid #2a3142;border-radius:8px;padding:14px;margin:0 0 14px;background:#141824">
          <div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap">
            <div><strong style="font-size:1.05em">{ident}</strong>{co}
                 <div style="opacity:.6;font-size:.82em">{sub}</div></div>
            <div style="text-align:right;font-size:.82em;opacity:.85">
              <div>{_esc(r['plan'] or '?')} · open {r['days_open']}d</div>
              <div>last login: {ll} · calls: {r['user_calls']}</div>
            </div>
          </div>
          <div style="margin:9px 0;font-size:.84em;color:#7dd3fc">{_esc(r['segment_label'])}</div>
          <div style="font-size:.8em;opacity:.7;margin-bottom:8px">{_esc(r['reason'] or '')}</div>
          <textarea readonly style="width:100%;height:120px;background:#0d1017;color:#d7dce5;
            border:1px solid #2a3142;border-radius:6px;padding:9px;font:13px/1.5 ui-monospace,monospace"
            >{draft}</textarea>
        </div>""")

    segs = " · ".join(f"{_esc(k)}: {v}" for k, v in sorted(d["by_segment"].items()))
    return Response(f"""<!doctype html><meta charset="utf-8">
<title>Activation desk — {d['open_total']} open</title>
<body style="margin:0;background:#0b0e14;color:#d7dce5;font:14px/1.6 system-ui,sans-serif">
<div style="max-width:860px;margin:0 auto;padding:26px 18px">
  <h1 style="margin:0 0 4px">Activation desk</h1>
  <p style="opacity:.75;margin:0 0 6px">{d['open_total']} open ·
     {d['never_contacted']} never contacted · {segs}</p>
  <p style="opacity:.6;font-size:.85em;margin:0 0 20px">
     Drafts only — <strong>nothing on this page sends anything</strong>.
     The automated nudge already failed 9 of 9; edit and send from your own
     client. Enrichment is first-party (<code>users</code>) only.</p>
  {''.join(cards)}
  <p style="opacity:.5;font-size:.78em;margin-top:26px">{_esc(d['basis'])}</p>
</div></body>""", mimetype="text/html")
