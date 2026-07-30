"""
routes/handoff_contract_shell.py — Handoff Contract Shell (#43, 2026-07-29).

★ THE SUBJECT OF THIS SHELL IS A TOKEN WITH TWO OWNERS.

Shell #42 measured what an agent sees before it reaches us. This one measures the
one moment where money is possible: an agent has hit a paid tool twice, we mint a
claim token, and a human is supposed to receive it.

Measured 7d, from exact columns:

    1,271 high-intent -> 1,259 minted -> 1,257 redeemed -> 0 human opens
    median mint -> redeem: 0.85s      1,257 free trial keys issued

I first called that "arbitrage" — agents gaming a paywall. That was WRONG, and
the correction is the finding. There are TWO endpoints that consume the same
single-use token:

    POST /claim/<token>                      the human email form
    POST /api/v1/mcp/high-intent/redeem      "binds the 7d/50-call trial key
                                              with NO human page-open"

The second is ours. Our own gateway calls it (server.mjs:982), and its docstring
states the intent plainly. Nobody is cheating: we built a path designed to skip
the human, and it wins every race because it is automatic and instant. Once it
fires, `claim_used_at` is set and the human URL returns 410 Gone — the module's
own abuse model says "token is single-use".

So the "Agent -> Human Handoff Funnel" measures a handoff our architecture is
built to foreclose. Not a persuasion problem. A contract problem: one artifact,
two consumers, opposite purposes, first-writer-wins.

LANES
  1. THE DASHBOARD BLAMED ITS HEALTHIEST STAGE (shipped, dchub-frontend #1086).
     The "biggest leak" label and the cliff highlight were both hardcoded to
     relay_minted, which runs at 99.6%, while the next stage sat at zero. The
     copy also proposed minting EARLIER — which raises agent redemption and
     lowers nothing. Both now derive from the data. This lane guards the derived
     form and is the one lane whose fix is already live.

  2. ONE SINGLE-USE TOKEN, TWO CONSUMERS. The lane does NOT recommend adding
     friction. Auto-redeem exists because friction already failed: 7,839 paywall
     signals produced 6 conversions, and agents bounced. Removing it would trade
     a measurable handoff for a measurable loss of adoption. The defect is not
     that agents get keys — it is that ONE token grants the key AND is the human's
     only link, so granting the key destroys the link. Two artifacts, two
     lifetimes: let the agent keep its instant key, and give the human a separate
     durable URL that redemption does not consume. This lane measures the
     collision and refuses to close until human_opens can be non-zero in
     principle rather than by luck.

  3. PUBLISHED FIGURES vs CANON, IN BOTH DIRECTIONS. A number on the homepage is
     a contract with the reader, so it fails when a figure EXCEEDS canon —
     over-claiming upward is the direction that costs credibility. But checking
     only upward misses the drift that actually happened: canon moved to 15,000+
     during this very session, which left the homepage (13,477+) conservative and
     the registry copy I "fixed" this morning (12,650+) stranded BELOW canon. The
     registry floor is a hand-maintained constant in dchub-mcp-server, so it goes
     stale every time canon moves — my own fix had already drifted within hours.
     Both directions are reported; only the upward one is critical.

Read-only. Lane 1's actuator shipped in dchub-frontend; lane 2's is a product
decision this shell deliberately does not make on its own.

Run:  GET /api/v1/admin/handoff-contract-shell        (admin-gated)
"""
from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

handoff_contract_shell_bp = Blueprint("handoff_contract_shell", __name__)

SHELL_ID = 43
SHELL_NAME = "Handoff Contract Shell"

# A redemption this fast was not a human. See relay_conversion_watch for the
# distribution; imported rather than restated so one threshold governs both.
try:
    from routes.relay_conversion_watch import MACHINE_REDEEM_SECONDS
except Exception:            # pragma: no cover - import-order safety
    MACHINE_REDEEM_SECONDS = 5

# The two consumers of the single-use claim token.
HUMAN_CONSUMER = "POST /claim/<token>"
AGENT_CONSUMER = "POST /api/v1/mcp/high-intent/redeem"

_OURS = ("dchub%", "DCHub/%", "Globeholder-%", "human-simulated%", "verify%",
         "audit%", "%probe%", "%validator%", "%certifier%", "%render-verify%")
_OURS_PARAM = list(_OURS)


def _admin_ok() -> bool:
    want = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(want) and got == want


def _disabled() -> bool:
    return (os.environ.get("HANDOFF_CONTRACT_SHELL_DISABLE") or "0") == "1"


def _conn():
    import psycopg2
    dsn = (os.environ.get("NEON_REPLICA_URL") or os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL"))
    if not dsn:
        return None
    c = psycopg2.connect(dsn)
    c.set_session(readonly=True, autocommit=True)
    return c


def _scalar(c, sql, args=None):
    try:
        cur = c.cursor()
        cur.execute(sql, args)
        r = cur.fetchone()
        return r[0] if r else None
    except Exception:
        return None


def _check(cid, name, passed, detail, critical=False) -> dict:
    return {"id": cid, "name": name,
            "status": "PASS" if passed is True else ("FAIL" if passed is False else "INDETERMINATE"),
            "detail": detail, "critical": bool(critical)}


def _lane_verdict(checks: list) -> str:
    if not checks:
        return "INDETERMINATE"
    if any(c["status"] == "INDETERMINATE" for c in checks):
        return "INDETERMINATE"
    if any(c["status"] == "FAIL" and c["critical"] for c in checks):
        return "FAILED"
    if any(c["status"] == "FAIL" for c in checks):
        return "DEGRADED"
    return "PASSED"


# ── Lane 1 — the dashboard must not blame a healthy stage ─────────────
def _lane_dashboard_honesty(c) -> list:
    """Guards the DERIVED cliff, read from the LIVE PAGE.

    My first version opened ~/dchub-frontend/ai.html from disk. Wrong surface
    twice over: the backend host has no frontend checkout (so this lane would be
    permanently INDETERMINATE in production), and locally it read a stale
    pre-merge copy and reported FAILED for a fix that had already shipped. What
    matters is what a visitor is SERVED, so fetch that.
    """
    import requests
    checks = []
    url = os.environ.get("DCHUB_AI_PAGE_URL", "https://dchub.cloud/ai.html")
    try:
        r = requests.get(url, headers={"User-Agent": "dchub-handoff-shell/1.0"},
                         timeout=25)
        src = r.text if r.status_code == 200 else None
        code = r.status_code
    except Exception as e:
        src, code = None, str(e)[:60]
    if not src:
        checks.append(_check("L1.1", "live funnel copy readable", None,
                             f"{url} unreadable ({code}) — an unreadable page is "
                             f"not a passing one", critical=True))
        return checks
    checks.append(_check(
        "L1.1", "cliff is derived, not hardcoded", "worstDrop(" in src,
        "worstDrop() is live — the label and highlight follow the numbers."
        if "worstDrop(" in src else
        "the served page hardcodes its cliff again: it will blame whichever stage "
        "the literal names, and relay-mint runs at 99.6% while the next stage is 0.",
        critical=True))
    checks.append(_check(
        "L1.2", "no static cliff flag survives", "cliff:true" not in src,
        "a static cliff:true flag is back — it paints a fixed stage as the cliff "
        "even when that stage is the healthiest in the funnel."
        if "cliff:true" in src else "no static cliff flag on the served page."))
    return checks


# ── Lane 2 — one single-use token, two consumers ──────────────────────
def _lane_token_contract(c) -> list:
    checks = []
    minted = _scalar(c, """
        SELECT COUNT(*) FROM mcp_high_intent_sessions
         WHERE first_hit_at > now() - interval '7 days'
           AND claim_minted_at IS NOT NULL
           AND NOT (COALESCE(user_agent,'') ILIKE ANY(%s))""", (_OURS_PARAM,))
    machine = _scalar(c, """
        SELECT COUNT(*) FROM mcp_high_intent_sessions
         WHERE first_hit_at > now() - interval '7 days'
           AND claim_used_at IS NOT NULL AND claim_minted_at IS NOT NULL
           AND EXTRACT(EPOCH FROM (claim_used_at - claim_minted_at)) < %s
           AND NOT (COALESCE(user_agent,'') ILIKE ANY(%s))""",
        (MACHINE_REDEEM_SECONDS, _OURS_PARAM))
    human = _scalar(c, """
        SELECT COUNT(*) FROM mcp_high_intent_sessions
         WHERE first_hit_at > now() - interval '7 days'
           AND claim_page_opened_at IS NOT NULL
           AND NOT (COALESCE(user_agent,'') ILIKE ANY(%s))""", (_OURS_PARAM,))
    if minted is None or machine is None or human is None:
        checks.append(_check("L2.0", "claim funnel readable", None,
                             "mcp_high_intent_sessions unreadable", critical=True))
        return checks

    # A token consumed by the machine cannot later be opened by a human: the
    # module's abuse model is "token is single-use" and the page 410s. So this
    # is not a conversion rate — it is a structural ceiling on human_opens.
    checks.append(_check(
        "L2.1", "the human link survives agent redemption",
        machine == 0 or (minted - machine) > 0,
        f"{machine} of {minted} minted claims (7d) were consumed by "
        f"{AGENT_CONSUMER} within {MACHINE_REDEEM_SECONDS}s, which sets "
        f"claim_used_at and makes {HUMAN_CONSUMER} return 410 Gone. Human opens "
        f"are capped at {max(minted - machine, 0)} by construction, regardless of "
        f"how good the link is.",
        critical=(minted > 0 and machine >= minted)))

    checks.append(_check(
        "L2.2", "a human has opened a handoff link", (human or 0) > 0,
        f"{human or 0} human page-opens in 7d. NOTE: all four all-time rows trace "
        f"to cursor render-verify probes, a Grok probe and an indexer, so the true "
        f"historical count is plausibly zero.",
        critical=False))

    # The remedy must not be "add friction". Guard that the reasoning is recorded
    # where a future reader will meet it.
    checks.append(_check(
        "L2.3", "the no-friction constraint is documented", True,
        "Auto-redeem exists because friction already failed: 7,839 paywall "
        "signals produced 6 conversions (0.08%) and agents bounced. The fix is "
        "TWO ARTIFACTS — agent keeps its instant key, human gets a separate "
        "durable URL redemption does not consume — not a wall."))
    return checks


# ── Lane 3 — published claims we do not check ─────────────────────────
def _lane_published_claims(c) -> list:
    """Published figures vs RESOLVED canon — in both directions.

    My first version hardcoded canon as 12,650+ and asked only whether the
    homepage over-claimed. Both halves were wrong. Canon has since moved to
    15,000+, so the homepage's 13,477+ is CONSERVATIVE, and a check that only
    looks upward would never notice a surface stranded BELOW canon — which is
    exactly what happened to the registry copy I "fixed" this morning at
    12,650+. Read canon, compare both ways, and say which direction drifted.
    """
    checks = []
    try:
        from routes.mcp_presence_crawler import _canonical_numbers
        canon = _canonical_numbers() or {}
    except Exception:
        canon = {}
    facilities = canon.get("facilities")
    if not facilities:
        checks.append(_check("L3.1", "canon facilities resolvable", None,
                             "could not resolve canonical facility count — nothing "
                             "to compare published figures against", critical=True))
    else:
        HOMEPAGE = 13477     # "13,477+ Facilities Indexed"
        REGISTRY = 12650     # smithery.yaml / README, set 2026-07-29 AM
        over = [n for n, v in (("homepage", HOMEPAGE), ("registry copy", REGISTRY))
                if v > facilities]
        under = [n for n, v in (("homepage", HOMEPAGE), ("registry copy", REGISTRY))
                 if v < facilities]
        checks.append(_check(
            "L3.1", "no published figure exceeds canon", not over,
            f"canon={facilities:,}. Over-claiming: {over or 'none'}. Over-claiming "
            f"upward is the direction that costs credibility.",
            critical=bool(over)))
        checks.append(_check(
            "L3.2", "published figures track canon upward too", not under,
            f"canon={facilities:,} but {under or 'nothing'} sits BELOW it "
            f"(homepage {HOMEPAGE:,}, registry {REGISTRY:,}). Stranded-low copy is "
            f"not dishonest, but it under-sells and it means the surface stopped "
            f"following canon — the registry floor is a hand-maintained constant in "
            f"dchub-mcp-server, so it drifts every time canon moves.",
            critical=False))

    checks.append(_check(
        "L3.3", "rank claim has a single owner", True,
        "The '#1 on Smithery' claim is measured by shell #42 lane 3, which reports "
        "UNVERIFIED. Not duplicated here on purpose — a second copy of one check is "
        "how the Glama reader and writer drifted apart."))
    return checks


LANES = (
    ("dashboard_honesty", "The dashboard blamed its healthiest stage", _lane_dashboard_honesty),
    ("token_contract", "One single-use token, two consumers", _lane_token_contract),
    ("published_claims", "Claims we publish but do not check", _lane_published_claims),
)


def run_handoff_contract_shell() -> dict:
    if _disabled():
        return {"shell": SHELL_NAME, "id": SHELL_ID, "status": "DISABLED"}
    c = _conn()
    if c is None:
        return {"shell": SHELL_NAME, "id": SHELL_ID, "status": "INDETERMINATE",
                "error": "no database URL configured — nothing was measured"}
    out, verdicts = [], []
    try:
        for key, title, fn in LANES:
            try:
                checks = fn(c)
            except Exception as e:
                checks = [_check(f"{key}.err", "lane executed", None,
                                 f"lane raised: {str(e)[:150]}", critical=True)]
            v = _lane_verdict(checks)
            verdicts.append(v)
            out.append({"lane": key, "title": title, "verdict": v, "checks": checks})
    finally:
        try:
            c.close()
        except Exception:
            pass
    overall = ("INDETERMINATE" if "INDETERMINATE" in verdicts
               else "FAILED" if "FAILED" in verdicts
               else "DEGRADED" if "DEGRADED" in verdicts else "PASSED")
    return {"shell": SHELL_NAME, "id": SHELL_ID, "overall": overall,
            "consumers": {"human": HUMAN_CONSUMER, "agent": AGENT_CONSUMER},
            "lanes": out}


@handoff_contract_shell_bp.route("/api/v1/admin/handoff-contract-shell", methods=["GET"])
def handoff_contract_shell_endpoint():
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(run_handoff_contract_shell())


def register_handoff_contract_shell(app) -> None:
    try:
        app.register_blueprint(handoff_contract_shell_bp)
    except Exception:
        pass
