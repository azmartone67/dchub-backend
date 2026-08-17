"""Onboarding Master Shell (#43) — did the human who just paid us get looked
after, end to end? (2026-07-29)

★WHY THIS EXISTS. "White glove" in this codebase meant ONE thing: registry
listing-copy propagation. Nothing watched the CUSTOMER. Answering "did this new
customer get white-glove treatment?" for alexander@ryex.net (Starter $9, paid
2026-07-29 14:35:08Z) meant opening Stripe and hand-querying four tables. What
that found:

  entitlements  PERFECT — mcp_conversions row, users.plan=starter, an ACTIVE
                api_key and mcp_dev_keys.tier=paid, all inside ONE SECOND.
  comms         BROKEN — FOUR welcome emails in THREE SECONDS (paid:mint, paid,
                starter, founding:cohort_welcome), and NO Stripe receipt.
  activation    UNKNOWN — and still is; see lane 3.

And it was systemic, not one customer: rfontes / landry2 / mykemiller got 4
each, eren / bryanseefeld 3 each.

★THIS SHELL IS A MEASUREMENT SURFACE, NOT A CLAIM OF REPAIR. Read-only. It
exists so the gap between "we provisioned them" and "we looked after them" is
visible on every tick instead of being discovered by hand months later. Several
lanes below are RED ON PURPOSE and name work that is NOT done. Do not read a
green tick as "onboarding is fixed"; read the individual checks.

Lanes:
  1 ENTITLEMENT   money in -> access granted. The one thing that must never fail.
  2 COMMUNICATION exactly ONE welcome, plus a receipt. Currently failing.
  3 ACTIVATION    did they ever USE it. Currently UNMEASURABLE — declared, not
                  faked as zero.
  4 BILLING       refunds reversed, plan recorded, duplicate customers.
  5 HUMAN TOUCH   does a person ever reach a new paying customer. Currently no.

GET /admin/onboarding · /api/v1/admin/onboarding/master-tick
Kill: ONBOARDING_SHELL_DISABLE=1
"""
from __future__ import annotations

import os
import logging

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)
onboarding_master_shell_bp = Blueprint("onboarding_master_shell", __name__)

_LOOKBACK_DAYS = int(os.environ.get("ONBOARDING_SHELL_DAYS", "30"))


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("ONBOARDING_SHELL_DISABLE") or "").strip() == "1"


def _conn():
    """Read replica — this shell only reads."""
    try:
        import psycopg2 as _pg
        url = ((os.environ.get("NEON_REPLICA_URL") or "").strip()
               or (os.environ.get("DATABASE_URL") or "").strip()
               or (os.environ.get("NEON_DATABASE_URL") or "").strip())
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:  # noqa: BLE001
        logger.warning("[onboarding-shell] db connect failed: %s", str(e)[:120])
        return None


def _one_val(cur, sql, args=None):
    """Scalar helper. ★Literal % in `sql` MUST be doubled: execute() with an
    empty tuple still runs %-interpolation, raises, and the caller then reports
    UNMEASURED against a query that works."""
    r = _q(cur, sql, args)
    return (r[0][0] if r and r[0] else None)


def _check(cid: str, name: str, passed, detail: str, critical: bool = False) -> dict:
    return {"id": cid, "name": name, "pass": passed,
            "detail": (detail or "")[:400], "critical": critical}


def _lane_verdict(checks: list):
    decided = [c for c in checks if c["pass"] is not None]
    return all(c["pass"] for c in decided) if decided else None


def _q(cur, sql, args=None):
    try:
        cur.execute(sql, args or ())
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        logger.debug("[onboarding-shell] query failed: %s", str(e)[:140])
        return None


def _payers(cur) -> list:
    rows = _q(cur, """SELECT LOWER(user_email), MIN(created_at)
                        FROM mcp_conversions
                       WHERE created_at >= NOW() - make_interval(days => %s)
                         AND user_email IS NOT NULL
                         AND stripe_customer_id IS NOT NULL
                         AND refunded_at IS NULL
                       GROUP BY 1 ORDER BY 2 DESC LIMIT 100""",
                (_LOOKBACK_DAYS,))
    return rows or []


# ── lane 1 · entitlement: money in -> access granted ──────────────────
def _lane_entitlement(cur, payers) -> list:
    out = []
    if not payers:
        return [_check("ent_any", "recent paying customers to check", None,
                       f"no non-refunded payers in {_LOOKBACK_DAYS}d", critical=True)]
    bad = []
    for email, _ in payers:
        r = _q(cur, """SELECT (SELECT COALESCE(plan,'') FROM users
                                WHERE LOWER(email)=%s LIMIT 1),
                              (SELECT COUNT(*) FROM api_keys
                                WHERE user_id IN (SELECT id FROM users
                                                   WHERE LOWER(email)=%s))""",
               (email, email))
        plan, nkeys = (r[0] if r else ("", 0))
        if not (plan and plan != "free" and int(nkeys or 0) > 0):
            bad.append(email)
    out.append(_check(
        "ent_provisioned", "every payer has a plan AND an active key",
        not bad,
        f"{len(payers)-len(bad)}/{len(payers)} fully provisioned"
        + (f" — MISSING: {', '.join(bad[:4])}" if bad else
           " — money in, access granted, no exceptions"),
        critical=True))
    return out


# ── lane 2 · communication: exactly one welcome, and a receipt ────────
def _lane_communication(cur, payers) -> list:
    out = []
    if not payers:
        return [_check("com_any", "payers to check", None, "no recent payers")]
    counts = {}
    for email, _ in payers:
        r = _q(cur, """SELECT COUNT(*) FROM welcome_email_log
                        WHERE LOWER(email)=%s
                          AND status IN ('sent','sent_via_resend')
                          AND COALESCE(plan,'') NOT LIKE 'receipt%%'""",
               (email,))
        counts[email] = int((r[0][0] if r else 0) or 0)
    flooded = {e: n for e, n in counts.items() if n > 1}
    silent = [e for e, n in counts.items() if n == 0]
    out.append(_check(
        "com_exactly_one", "each payer got EXACTLY ONE welcome email",
        not flooded and not silent,
        (f"{len(flooded)}/{len(counts)} got MORE than one "
         f"({', '.join(f'{e.split(chr(64))[0]}={n}' for e, n in list(flooded.items())[:4])})"
         if flooded else "")
        + (f" · {len(silent)} got NONE" if silent else "")
        or "one welcome each — correct",
        critical=True))
    # ★The flood was never a missing guard — it was check-then-act: every
    # sender read the log, then sent, then wrote, and the write landed AFTER a
    # ~1s network send, so concurrent senders all passed the check before any
    # of them logged (alexander@ryex.net: 4 welcomes in 3 seconds). Fixed
    # 2026-08-17: send_welcome_email_sendgrid claims atomically (one INSERT
    # ... WHERE NOT EXISTS, status='claimed', BEFORE any I/O) and the
    # flask_mcp path stopped writing a second audit row for the same send.
    # Measured on POST-fix sends only — the pre-fix floods are history, and a
    # check red forever on history says nothing (the lane-3b lesson).
    _CLAIM_FIX_TS = "2026-08-17T06:00:00+00:00"
    r = _q(cur, """SELECT COUNT(*) FROM (
                     SELECT lower(email)
                       FROM welcome_email_log
                      WHERE status IN ('sent','sent_via_resend')
                        AND COALESCE(plan,'') NOT LIKE 'receipt%%'
                        AND attempted_at >= %s::timestamptz
                      GROUP BY lower(email)
                     HAVING COUNT(*) > 1
                        AND max(attempted_at) - min(attempted_at)
                            < interval '24 hours') d""", (_CLAIM_FIX_TS,))
    dup_addrs = int((r[0][0] if r else 0) or 0)
    r = _q(cur, """SELECT COUNT(*) FROM welcome_email_log
                    WHERE status IN ('sent','sent_via_resend')
                      AND COALESCE(plan,'') NOT LIKE 'receipt%%'
                      AND attempted_at >= %s::timestamptz""", (_CLAIM_FIX_TS,))
    n_post = int((r[0][0] if r else 0) or 0)
    if n_post == 0:
        out.append(_check(
            "com_senders_guarded",
            "all welcome senders honour the dedupe (atomic claim)",
            None,
            "claim-then-send shipped 2026-08-17; no post-fix welcome sent yet "
            "— verified on the next payer, never on pre-fix history",
            critical=True))
    else:
        out.append(_check(
            "com_senders_guarded",
            "all welcome senders honour the dedupe (atomic claim)",
            dup_addrs == 0,
            f"{dup_addrs} address(es) got >1 welcome within 24h across "
            f"{n_post} post-fix send(s) since 2026-08-17 (receipts excluded)",
            critical=True))
    # Receipts send from the checkout.session.completed webhook since
    # 2026-08-17 (Stripe's own receipt emails stay off; payment links never
    # touch our code, the webhook is the one chokepoint). Logged as
    # plan='receipt:<session>' in welcome_email_log — measured per post-fix
    # payer, never narrated.
    post_payers = [e for e, ts in payers
                   if ts is not None and str(ts) >= "2026-08-17"]
    if not post_payers:
        out.append(_check(
            "com_receipt", "paying customers get a payment receipt",
            None,
            "webhook receipt sender shipped 2026-08-17 "
            "(plan='receipt:<session>' rows); no post-fix payer yet — "
            "measured on the next sale",
            critical=False))
    else:
        no_receipt = []
        for e in post_payers:
            r = _q(cur, """SELECT 1 FROM welcome_email_log
                            WHERE lower(email)=%s AND plan LIKE 'receipt:%%'
                              AND status IN ('sent','sent_via_resend')
                            LIMIT 1""", (e,))
            if not r:
                no_receipt.append(e)
        out.append(_check(
            "com_receipt", "paying customers get a payment receipt",
            not no_receipt,
            (f"{len(no_receipt)}/{len(post_payers)} post-fix payer(s) have no "
             f"receipt row" if no_receipt
             else f"{len(post_payers)}/{len(post_payers)} post-fix payer(s) "
                  f"received a receipt"),
            critical=False))
    return out


# ── lane 3 · activation: did they ever USE what they bought ───────────
def _lane_activation(cur, payers) -> list:
    """★★CORRECTED 2026-07-30 — activation IS measurable for most keys.

    The first cut declared this UNMEASURABLE outright, on the basis that
    `api_keys.key_prefix` is a generic stub ("dchub_dev_") that cannot join to
    `mcp_call_log.api_key`. That was over-generalised from ONE example
    (alexander@ryex.net). Measured across the table:

      · mcp_call_log.api_key holds FULL 59-char keys (dchub_live_08f4fb4d…)
      · MOST api_keys.key_prefix values are DISTINCTIVE 24-char prefixes
        (dchub_developer_jZ6bKqlr) — a LIKE join on those is specific
      · 25 of 146 api_keys match a logged call that way

    So the honest position is per-key, not blanket: measure where the prefix is
    long enough to be specific, and report UNMEASURED only for the short generic
    ones. Declaring the whole metric unmeasurable was the safe error, but it was
    still an error — it retired a question that was answerable.

    ★The >=16 length guard is what keeps this honest in the other direction: a
    short prefix would LIKE-match half the platform and report activation that
    never happened — a flattering false positive, which is worse than the false
    zero because nobody investigates good news.
    """
    out = []
    total = _one_val(cur, """SELECT COUNT(*) FROM api_keys
                              WHERE key_prefix IS NOT NULL""")
    joinable = _one_val(cur, """SELECT COUNT(*) FROM api_keys
                                 WHERE key_prefix IS NOT NULL
                                   AND length(key_prefix) >= 16""")
    active = _one_val(cur, """SELECT COUNT(DISTINCT k.id) FROM api_keys k
                               WHERE k.key_prefix IS NOT NULL
                                 AND length(k.key_prefix) >= 16
                                 AND EXISTS (SELECT 1 FROM mcp_call_log t
                                              WHERE t.api_key LIKE k.key_prefix || '%%')""")
    t, j, a = int(total or 0), int(joinable or 0), int(active or 0)
    out.append(_check(
        "act_link", "customer keys JOIN to call traffic", j > 0,
        f"{j} of {t} api_keys carry a prefix specific enough to join "
        f"(>=16 chars); the rest are short generic stubs and stay UNMEASURED "
        f"rather than being counted as zero", critical=True))
    if j:
        rate = a / j
        out.append(_check(
            "act_used", "keys that CAN be measured were actually used",
            rate >= 0.25,
            f"{a} of {j} joinable key(s) have made at least one call "
            f"({rate*100:.1f}%). This is the churn signal that was reported as "
            f"unmeasurable until 2026-07-30 — it was answerable all along for "
            f"most keys.", critical=True))
    return out


# ── lane 4 · billing integrity ────────────────────────────────────────
def _lane_billing(cur) -> list:
    out = []
    r = _q(cur, """SELECT COUNT(*) FROM information_schema.columns
                    WHERE table_name='mcp_conversions' AND column_name='refunded_at'""")
    has_ref = bool(r and int(r[0][0] or 0) > 0)
    out.append(_check("bil_refunds", "refunds reverse in the ledger", has_ref,
                      "refunded_at present — MRR reads exclude refunded sales"
                      if has_ref else
                      "refunded_at MISSING: refunded revenue counts forever",
                      critical=True))
    r = _q(cur, """SELECT COUNT(*) FROM mcp_conversions
                    WHERE created_at >= NOW() - make_interval(days => %s)
                      AND LOWER(COALESCE(plan_to,'')) IN ('','unknown')""",
           (_LOOKBACK_DAYS,))
    n_unknown = int((r[0][0] if r else 0) or 0)
    out.append(_check(
        "bil_plan_recorded", "every sale records WHICH plan was bought",
        n_unknown == 0,
        f"{n_unknown} conversion(s) in {_LOOKBACK_DAYS}d have plan_to "
        f"unknown/blank — distorts plan-mix reporting (web subscription path)"
        if n_unknown else "all sales carry a plan"))
    out.append(_check(
        "bil_dup_customers", "checkout reuses an existing Stripe customer",
        None,
        "PARTIAL: the dashboard checkout now reuses the customer id, but hosted "
        "PAYMENT LINKS (how most people actually buy) never touch our code — "
        "duplicates remain possible there. gabriel.zuckerman@nlr.gov reached 3 "
        "customer records / 2 subscriptions that way."))
    return out


# ── lane 5 · human touch ──────────────────────────────────────────────
def _lane_human(cur, payers) -> list:
    return [_check(
        "hum_outreach", "a PERSON reaches every new paying customer", False,
        "KNOWN GAP: there is no human white-glove track. What exists is an "
        "automated welcome cascade plus this detector. 'White glove' currently "
        "means monitored, not attended. "
        f"{len(payers)} payer(s) in {_LOOKBACK_DAYS}d received no human contact.",
        critical=False)]


def _run_tick() -> dict:
    out = {"shell": "onboarding", "n": 43, "lookback_days": _LOOKBACK_DAYS,
           "lanes": [], "note": (
               "Read-only. Several checks are RED ON PURPOSE and name work that "
               "is NOT done. A green tick would mean onboarding is attended, "
               "not merely provisioned — read the individual checks.")}
    c = _conn()
    if c is None:
        out["ok"] = False
        out["error"] = "no_database"
        return out
    try:
        with c.cursor() as cur:
            payers = _payers(cur)
            out["payers_checked"] = len(payers)
            lanes = [
                ("1 · entitlement — money in, access granted",
                 _lane_entitlement(cur, payers)),
                ("2 · communication — exactly one welcome, plus a receipt",
                 _lane_communication(cur, payers)),
                ("3 · activation — did they ever use it",
                 _lane_activation(cur, payers)),
                ("4 · billing integrity", _lane_billing(cur)),
                ("5 · human touch", _lane_human(cur, payers)),
            ]
            for label, checks in lanes:
                out["lanes"].append({
                    "lane": label, "checks": checks,
                    "pass": _lane_verdict(checks),
                })
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["error"] = str(e)[:200]
        return out
    finally:
        try:
            c.close()
        except Exception:
            pass
    decided = [ln["pass"] for ln in out["lanes"] if ln["pass"] is not None]
    out["lanes_pass"] = sum(1 for p in decided if p)
    out["lanes_total"] = len(out["lanes"])
    out["ok"] = True
    return out


@onboarding_master_shell_bp.route("/api/v1/admin/onboarding/master-tick",
                                  methods=["GET"])
def onboarding_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    return jsonify(_run_tick())


@onboarding_master_shell_bp.route("/admin/onboarding", methods=["GET"])
@onboarding_master_shell_bp.route("/api/v1/admin/onboarding", methods=["GET"])
def onboarding_dashboard():
    if _disabled():
        return Response("onboarding shell disabled", status=404)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _run_tick()

    def chip(v):
        if v is True:
            return '<span style="color:#22c55e">PASS</span>'
        if v is False:
            return '<span style="color:#ef4444">FAIL</span>'
        return '<span style="color:#eab308">n/a</span>'

    rows = []
    for ln in p.get("lanes", []):
        rows.append(f'<h3>{ln["lane"]} — {chip(ln["pass"])}</h3><ul>')
        for ch in ln["checks"]:
            star = " ★" if ch.get("critical") else ""
            rows.append(f'<li>{chip(ch["pass"])}{star} <b>{ch["name"]}</b><br>'
                        f'<small>{ch["detail"]}</small></li>')
        rows.append("</ul>")
    body = "".join(rows)
    return Response(
        f"<html><body style='font-family:system-ui;background:#0b0b12;"
        f"color:#e6e6f0;padding:24px;max-width:900px'>"
        f"<h1>Onboarding Master Shell #43</h1>"
        f"<p><small>{p.get('note','')}</small></p>"
        f"<p>payers checked (last {p.get('lookback_days')}d): "
        f"<b>{p.get('payers_checked','?')}</b> · lanes passing "
        f"{p.get('lanes_pass','?')}/{p.get('lanes_total','?')}</p>{body}"
        f"</body></html>", mimetype="text/html")
