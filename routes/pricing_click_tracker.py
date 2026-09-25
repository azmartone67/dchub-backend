"""
pricing_click_tracker.py — GET /go/p/<plan>, the /pricing page's checkout hop.

2026-09-21, frontend#1534. /pricing is a static page. It cannot mint the HMAC
token /go/c verifies (routes/checkout_click_tracker.py), so its buttons linked
straight to buy.stripe.com and a press was never a server fact: the in-page
click_upgrade beacon races the cross-origin navigation. This is the unsigned
sibling of /go/c: stamp the plan, then 302 to the same Payment Link.

    GET /go/p/<plan>?ref=<page attribution id>
        -> stamp pricing_checkout_clicks, 302 to STRIPE_LINKS[plan]
           (client_reference_id=<ref> when the page sent one)

A module of its own, like routes/partnership_click_tracker.py, because the
/go/c writer is read as THE relayed-checkout writer: the human_acted v7 lane
derives the table it counts from that module's one INSERT target.

`/go/*` already reaches the backend: it is in the frontend `_routes.json`
include and in the _worker.js backend-proxy prefix list.

FAIL-OPEN, as on /go/c: a database error stamps nothing and still redirects.
"""
from __future__ import annotations

import logging
import os
import re

from flask import Blueprint, request, redirect

from routes._stripe_links import STRIPE_LINKS
from routes._swallowed_writes import note_swallowed_write
from routes.checkout_click_tracker import _PRICING_URL, _REF_OK, _conn, _dsn, _pg

logger = logging.getLogger(__name__)

pricing_click_bp = Blueprint("pricing_click", __name__)

# ★ Its OWN table, never mcp_checkout_clicks. Every reader of that table
#   (handoff_definition, ops_activation, mcp_high_intent_claim, human_acted)
#   counts a link an AGENT relayed. A website visitor pressing a button is not
#   one, and a row here would inflate the MCP handoff lane with a cohort that
#   never saw an agent. The per-path funnel reads both tables side by side.
# ★ The plan is an allowlist key, so this is no more an open redirect than /go/c.
#   Only the plans /pricing sells: never starter (not on /pricing).
# ★ `ref` is the page's own attribution id, passed through to Stripe as
#   client_reference_id exactly as the page used to append it. It is unsigned, so
#   it is held to the same charset as a /go/c ref before it touches the Location.
COLD_PLANS = ("metered", "developer", "pro")

# ★ r-pro-trial-web (2026-09-24, owner): the Pro 7-day trial Payment Link, sold
#   on /pricing only (web; MCP surfaces untouched until after 10-01). Deliberately
#   NOT in COLD_PLANS / STRIPE_LINKS:
#     · handoff_definition.CLICK_TO_PAY_PLANS is pinned equal to COLD_PLANS, so a
#       trial there would enter the click-to-pay lanes the 10-01 readout uses
#       (schema_repair files an unlisted plan under cold_go_p "other");
#     · STRIPE_LINKS feeds the checkout-integrity lanes that compare a link's
#       charge with its label, and a trial charges $0 at checkout.
#   Clicks are still stamped in pricing_checkout_clicks, as plan 'pro_trial'.
#   DCHUB_PRO_TRIAL_LINK overrides the URL (must be a buy.stripe.com link); any
#   other value ("off") disables it without a frontend deploy — the button then
#   lands on /pricing.
PRO_TRIAL_PLAN = "pro_trial"
_PRO_TRIAL_DEFAULT = "https://buy.stripe.com/cNieVeg7w93DcWV9maaZi0q"
_STRIPE_LINK_RE = re.compile(r"^https://buy\.stripe\.com/[A-Za-z0-9_]{8,64}$")  # test-mode links are test_…


def pro_trial_link():
    """The trial Payment Link, or None when disabled / misconfigured. Any value
    that is not a buy.stripe.com link ("off" included) disables it."""
    v = (os.environ.get("DCHUB_PRO_TRIAL_LINK") or "").strip() or _PRO_TRIAL_DEFAULT
    return v if _STRIPE_LINK_RE.match(v) else None

_PRICING_CLICKS_READY = [False]


def _ensure_pricing_table() -> bool:
    """Create pricing_checkout_clicks once, at import. Never raises.

    A new table, so nothing waits on a lock another table holds, and it runs on
    this module's own connection (the pooled get_db() cursor skips DDL).
    """
    if not (_pg and _dsn()):
        return False
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pricing_checkout_clicks (
                    id          SERIAL PRIMARY KEY,
                    clicked_at  TIMESTAMPTZ DEFAULT NOW(),
                    plan        TEXT,
                    ref         TEXT,
                    known_plan  BOOLEAN DEFAULT TRUE,
                    ip          TEXT,
                    user_agent  TEXT,
                    referrer    TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_pcc_ts  ON pricing_checkout_clicks(clicked_at DESC);
                CREATE INDEX IF NOT EXISTS ix_pcc_ref ON pricing_checkout_clicks(ref);
            """)
        _PRICING_CLICKS_READY[0] = True
    except Exception:
        pass
    return _PRICING_CLICKS_READY[0]


_ensure_pricing_table()


def _log_pricing_click(plan: str, ref: str, known_plan: bool) -> None:
    try:
        with _conn() as c, c.cursor() as cur:
            ip = request.headers.get("CF-Connecting-IP") or request.remote_addr or ""
            cur.execute(
                # Append-only event log keyed by SERIAL: ON CONFLICT DO NOTHING
                # satisfies the insert lint and never fires. Two presses are two
                # clicks, as on /go/c.
                """INSERT INTO pricing_checkout_clicks
                     (plan, ref, known_plan, ip, user_agent, referrer)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (plan[:40], ref[:200] or None, bool(known_plan), ip[:80],
                 (request.headers.get("User-Agent", "") or "")[:300],
                 (request.headers.get("Referer", "") or "")[:300]),
            )
    except Exception:
        note_swallowed_write("pricing_checkout_clicks",
                             where="checkout_click_tracker._log_pricing_click")


@pricing_click_bp.route("/go/p/<plan>", methods=["GET"])
def pricing_click(plan):
    """Stamp a /pricing button press, then 302 to that plan's Payment Link."""
    plan = (plan or "").strip().lower()
    ref = (request.args.get("ref") or "").strip()
    if ref and not _REF_OK.match(ref):
        ref = ""
    if plan in COLD_PLANS:
        target = STRIPE_LINKS.get(plan)
    elif plan == PRO_TRIAL_PLAN:
        target = pro_trial_link()
    else:
        target = None
    if not target:
        # Not a plan /pricing sells: land on the page rather than guess a
        # checkout. Stamped known_plan=false so a broken button shows up.
        _log_pricing_click(plan or "unknown", ref, False)
        return redirect(_PRICING_URL, code=302)
    if ref:
        sep = "&" if "?" in target else "?"
        target = target + sep + "client_reference_id=" + ref
    _log_pricing_click(plan, ref, True)
    return redirect(target, code=302)
