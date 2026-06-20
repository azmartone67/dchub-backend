"""
routes/customer_portal.py — Admin CUSTOMER PORTAL (2026-06-19). READ-ONLY.

THE PROBLEM THIS SOLVES: the operator is blind on WHO their customers actually
are. Billing (users.plan) and the MCP paywall (mcp_dev_keys.tier) are
nearly-DISJOINT populations — ~32 accounts sit on a paid plan, but only ~5 have a
real paid invoice and only ~18 hold a paid MCP key, leaving ~30 paid-plan accounts
with NO paid key (the activation gap). There is no single admin surface that
reconciles the two and classifies each account into an actionable SEGMENT.

THIS MODULE is ONE browser-openable, admin-gated, strictly READ-ONLY page that:
  · lists every account with plan / real-payer / key tier+tail / 30d+7d MCP usage /
    last-active / subscription_status / MRR contribution, and
  · classifies each into one of FOUR outreach segments using the SAME predicates
    the reconciliation already verified (reused from
    routes.brain_investigator._gather_paid_reconciliation):
       keyless_payer   — pays a real invoice but holds no paid MCP key (transactional
                         "activate the key you're entitled to")
       comped_paid     — on a paid plan with ZERO real invoices (founding/dev grant;
                         marketing convert-before-it-lapses)
       high_usage_free — free plan + heavy MCP usage + marketing opt-in
                         (marketing free->paid)
       at_risk_paid    — real payer whose usage is low/declining (transactional
                         customer-success check-in)
  · per-segment count chips + a filterable table.

SAFETY / SCOPE:
  · STRICTLY READ-ONLY. This module writes NOTHING — no outreach_drafts, no
    grades, no approvals. (The draft-and-approve OUTREACH engine is a SEPARATE
    module; this portal is its read surface only.) The only side effect is an
    optional Set-Cookie that remembers the admin key on a valid open (same UX as
    the brain dashboards).
  · ADMIN-GATED on EVERY endpoint — an internal/admin key from the X-Internal-Key /
    X-Admin-Key header, the ?admin_key= query param, OR the dchub_innov_key cookie.
    Account PII is NEVER exposed to a non-admin. (Mirrors
    routes/brain_innovation_dashboard._admin_ok verbatim.)
  · Served UNDER /api/ so the dchub.cloud CF worker forwards it unconditionally
    (bare subpaths + /brain/* hit CF Error-1000). noindex on the page.
  · The api_key is REDACTED to a short tail for display — the full key never
    leaves the DB.
  · Best-effort: a missing table / offline DB yields an empty payload, never a
    crash; every DB touch is wrapped.

ENDPOINTS (blueprint customer_portal_bp, admin-gated):
  GET /api/v1/portal/accounts  -> {ok, generated_at, counts_by_segment, accounts:[…]}
                                  (LIMIT ~500). READ-ONLY segmented JSON.
  GET /api/v1/portal           -> self-contained admin HTML page that fetches the
                                  JSON and renders a filterable table with per-segment
                                  filter chips + a segment badge per row.

SCHEMA TRAPS honored here:
  · users.created_at is TEXT — never date-filtered here (we only display it).
  · ALL email joins use lower(email)=lower(email) (case drift between the two tables).
  · mcp_call_log has NO email column — usage is attributed
    mcp_call_log.api_key -> mcp_dev_keys.api_key -> mcp_dev_keys.email.
  · marketing_opt_in lives in mcp_dev_keys.metadata->>'marketing_opt_in' ('true').
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

customer_portal_bp = Blueprint("customer_portal", __name__)


# Billing plans that count as "paid plan" (matches brain_investigator._PAID_PLANS).
_PAID_PLANS = ("pro", "founding", "enterprise", "pro_annual", "developer")
_PAID_PLANS_SQL = "('pro','founding','enterprise','pro_annual','developer')"

# high_usage_free threshold: MCP calls in the last 30 days to qualify as "heavy".
_HIGH_USAGE_30D_THRESHOLD = int(
    (os.environ.get("PORTAL_HIGH_USAGE_30D_THRESHOLD") or "50").strip() or "50"
)


# ── Admin gate — MIRROR the brain-dashboard gate (header / ?admin_key= / cookie) ──
def _admin_ok() -> bool:
    """Same gate every admin brain dashboard accepts
    (brain_innovation_dashboard._admin_ok): an internal/admin key from the
    X-Internal-Key / X-Admin-Key header, the ?admin_key= query param (first
    browser visit), OR the dchub_innov_key cookie (remembered after the first
    valid open). No new auth scheme — reuse the codebase pattern. Account PII is
    served ONLY behind this gate."""
    _keys = set()
    for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        _v = os.environ.get(_n)
        if _v:
            _keys.add(_v)
    _sent = (request.headers.get("X-Internal-Key")
             or request.headers.get("X-Admin-Key")
             or request.args.get("admin_key")
             or request.cookies.get("dchub_innov_key") or "").strip()
    return bool(_sent) and _sent in _keys


_ADMIN_ONLY_HTML = (
    "<!doctype html><meta charset=utf-8><title>DC Hub · internal</title>"
    "<meta name='robots' content='noindex,nofollow'>"
    "<body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
    "background:#0a0a0a;color:#9a9a9a;display:flex;align-items:center;"
    "justify-content:center;height:90vh;text-align:center'>"
    "<div><h2 style='color:#e6e6e6;font-weight:300;letter-spacing:-.02em'>"
    "Internal console</h2><p>The DC Hub customer portal is admin-only. "
    "Append <code>?admin_key=…</code>.</p></div>")


# ── DB (direct read-only psycopg2, mirror brain_innovation_dashboard._conn) ──
def _conn():
    """Raw psycopg2 connection. Mirrors brain_innovation_dashboard._conn /
    brain_investigator._conn — the _iso_common contextmanager crashes on
    .cursor(). READ-ONLY use here. Returns None (never raises) on any error."""
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if dsn:
            return _pg.connect(dsn, sslmode="require", connect_timeout=6)
    except Exception as e:
        logger.warning("customer_portal: _conn failed: %s", e)
    return None


def _iso(dt) -> str:
    try:
        return dt.isoformat()
    except Exception:
        return str(dt) if dt is not None else ""


def _key_tail(api_key) -> str:
    """Redact an api_key to a short, non-reversible display tail. The full key
    NEVER leaves the DB. Returns '' when there's no key."""
    s = str(api_key or "")
    if not s:
        return ""
    return "…" + s[-4:] if len(s) > 4 else "…" + s


def _classify_segment(row: dict) -> str:
    """Apply the FOUR segment predicates to one already-gathered account row.

    Reuses the verified reconciliation logic (brain_investigator) so the portal
    and the outreach engine agree on who is who. A row that matches none returns
    '' (unsegmented). Precedence is deliberate: a real payer is checked for the
    keyless-activation gap first, then for the at-risk signal; a comped paid plan
    and a high-usage free plan are mutually exclusive by plan/payer.

    Fields expected on `row`:
      plan (str), real_payer (bool), has_paid_key (bool), opted_in (bool),
      calls_30d (int), calls_7d (int).
    """
    plan = (row.get("plan") or "").strip().lower()
    is_paid_plan = plan in _PAID_PLANS
    real_payer = bool(row.get("real_payer"))
    has_paid_key = bool(row.get("has_paid_key"))
    opted_in = bool(row.get("opted_in"))
    calls_30d = int(row.get("calls_30d") or 0)
    calls_7d = int(row.get("calls_7d") or 0)

    # keyless_payer — pays a real invoice but holds NO paid MCP key. The
    # activation gap. TRANSACTIONAL channel. (Checked first: a keyless payer is
    # the highest-value, consent-free reach.)
    if is_paid_plan and real_payer and not has_paid_key:
        return "keyless_payer"

    # at_risk_paid — real payer WITH a paid key whose recent usage is
    # low/declining (no calls in the last 7d). TRANSACTIONAL (success check-in).
    if is_paid_plan and real_payer and has_paid_key and calls_7d == 0:
        return "at_risk_paid"

    # comped_paid — on a paid plan with ZERO real invoices (founding/dev grant).
    # MARKETING channel (convert comp before it lapses).
    if is_paid_plan and not real_payer:
        return "comped_paid"

    # high_usage_free — free/none plan + heavy MCP usage + marketing opt-in.
    # MARKETING channel. Opt-in is REQUIRED for this segment to be sendable.
    if (not is_paid_plan) and calls_30d >= _HIGH_USAGE_30D_THRESHOLD and opted_in:
        return "high_usage_free"

    return ""


def _gather_accounts(limit: int = 500) -> dict:
    """Build the READ-ONLY account roster, reconciled across billing + the MCP
    paywall + usage + MRR, with a segment classification per row.

    HONEST QUERY DISCIPLINE — one defensive psycopg2 connection; each probe is a
    plain SELECT joined on lower(email)=lower(email); a failing probe degrades the
    affected field rather than blanking the view. Reuses the predicate logic from
    brain_investigator._gather_paid_reconciliation. Returns
    {accounts:[…], counts_by_segment:{…}} — and an empty roster (never a crash)
    on any error / no DB.
    """
    empty = {"accounts": [], "counts_by_segment": {}}
    conn = _conn()
    if conn is None:
        return empty
    try:
        limit = max(1, min(int(limit), 500))
    except Exception:
        limit = 500

    accounts: list[dict] = []
    try:
        with conn.cursor() as cur:
            # ── 1) Base roster from the canonical BILLING table. users.created_at
            #       is TEXT — we only DISPLAY it (no date math), so no cast needed.
            cur.execute(
                "SELECT email, name, COALESCE(plan,'free') AS plan, "
                "       COALESCE(invoices_paid_count,0) AS inv, "
                "       subscription_status, created_at, last_login "
                "FROM users "
                "WHERE email IS NOT NULL AND email <> '' "
                "ORDER BY COALESCE(invoices_paid_count,0) DESC, "
                "         lower(plan) ASC, lower(email) ASC "
                "LIMIT %s",
                (limit,),
            )
            for r in (cur.fetchall() or []):
                email = (r[0] or "").strip()
                accounts.append({
                    "email": email,
                    "name": r[1] or "",
                    "plan": (r[2] or "free"),
                    "real_payer": int(r[3] or 0) > 0,
                    "subscription_status": r[4] or "",
                    "created_at": _iso(r[5]) if not isinstance(r[5], str) else (r[5] or ""),
                    "last_login": _iso(r[6]) if not isinstance(r[6], str) else (r[6] or ""),
                    # filled by the joins below (safe defaults if a probe fails)
                    "key_tier": None,
                    "key_tail": "",
                    "has_paid_key": False,
                    "opted_in": False,
                    "calls_30d": 0,
                    "calls_7d": 0,
                    "last_active": None,
                    "mrr_usd": 0.0,
                    "segment": "",
                    "_api_key": None,  # internal; stripped before return
                })

            by_email = {a["email"].lower(): a for a in accounts if a["email"]}
            if not by_email:
                return {"accounts": accounts, "counts_by_segment": {}}

            # ── 2) MCP KEY join — best active key per email (paid/enterprise
            #       wins), its tier, opt-in flag, and the api_key (for the usage
            #       join + a redacted tail). lower()=lower() per the case-drift trap.
            try:
                cur.execute(
                    "SELECT lower(k.email) AS e, k.api_key, k.tier, "
                    "       (k.metadata->>'marketing_opt_in') AS optin, "
                    "       k.last_used_at, "
                    "       CASE WHEN k.tier IN ('paid','enterprise') THEN 0 ELSE 1 END AS pref "
                    "FROM mcp_dev_keys k "
                    "WHERE k.email IS NOT NULL AND k.email <> '' "
                    "  AND COALESCE(k.status,'active') = 'active' "
                    "  AND lower(k.email) = ANY(%s) "
                    "ORDER BY lower(k.email), pref ASC, k.last_used_at DESC NULLS LAST",
                    (list(by_email.keys()),),
                )
                _seen_key_email = set()
                for kr in (cur.fetchall() or []):
                    e = kr[0]
                    a = by_email.get(e)
                    if a is None:
                        continue
                    is_paid_key = (kr[2] in ("paid", "enterprise"))
                    optin = (str(kr[3] or "").strip().lower() == "true")
                    # opt-in / paid-key flags aggregate across ALL the email's keys
                    if optin:
                        a["opted_in"] = True
                    if is_paid_key:
                        a["has_paid_key"] = True
                    # the DISPLAYED key is the FIRST (best) one per email
                    if e not in _seen_key_email:
                        _seen_key_email.add(e)
                        a["_api_key"] = kr[1]
                        a["key_tier"] = kr[2]
                        a["key_tail"] = _key_tail(kr[1])
                        a["last_active"] = _iso(kr[4]) if kr[4] is not None else None
            except Exception as e:
                logger.warning("customer_portal: mcp_dev_keys join failed: %s", e)
                try: conn.rollback()
                except Exception: pass

            # ── 3) USAGE join — mcp_call_log has NO email column, so we sum calls
            #       per api_key over 30d/7d and attribute via the keys we collected.
            api_keys = [a["_api_key"] for a in accounts if a.get("_api_key")]
            if api_keys:
                try:
                    cur.execute(
                        "SELECT api_key, "
                        "  COUNT(*) FILTER (WHERE timestamp >= NOW() - INTERVAL '30 days') AS c30, "
                        "  COUNT(*) FILTER (WHERE timestamp >= NOW() - INTERVAL '7 days')  AS c7, "
                        "  MAX(timestamp) AS last_call "
                        "FROM mcp_call_log "
                        "WHERE api_key = ANY(%s) "
                        "  AND timestamp >= NOW() - INTERVAL '30 days' "
                        "GROUP BY api_key",
                        (api_keys,),
                    )
                    usage = {}
                    for ur in (cur.fetchall() or []):
                        usage[ur[0]] = (int(ur[1] or 0), int(ur[2] or 0), ur[3])
                    for a in accounts:
                        ak = a.get("_api_key")
                        if ak and ak in usage:
                            c30, c7, last_call = usage[ak]
                            a["calls_30d"] = c30
                            a["calls_7d"] = c7
                            if last_call is not None:
                                # prefer the call-log last-active over the key's
                                a["last_active"] = _iso(last_call)
                except Exception as e:
                    logger.warning("customer_portal: mcp_call_log join failed: %s", e)
                    try: conn.rollback()
                    except Exception: pass

            # ── 4) MRR join — mcp_conversions.mrr_cents per user_email (sum any
            #       attributed subscriptions). lower()=lower() per the case trap.
            try:
                cur.execute(
                    "SELECT lower(user_email) AS e, COALESCE(SUM(mrr_cents),0) AS cents "
                    "FROM mcp_conversions "
                    "WHERE user_email IS NOT NULL AND lower(user_email) = ANY(%s) "
                    "GROUP BY lower(user_email)",
                    (list(by_email.keys()),),
                )
                for mr in (cur.fetchall() or []):
                    a = by_email.get(mr[0])
                    if a is not None:
                        a["mrr_usd"] = round(int(mr[1] or 0) / 100.0, 2)
            except Exception as e:
                logger.warning("customer_portal: mcp_conversions join failed: %s", e)
                try: conn.rollback()
                except Exception: pass

        # ── 5) Classify + redact (strip the internal full api_key) ──
        counts: dict[str, int] = {}
        for a in accounts:
            seg = _classify_segment(a)
            a["segment"] = seg
            if seg:
                counts[seg] = counts.get(seg, 0) + 1
            a.pop("_api_key", None)  # never leave the full key in the payload

        return {"accounts": accounts, "counts_by_segment": counts}
    except Exception as e:
        logger.warning("customer_portal: _gather_accounts failed: %s", e)
        return empty
    finally:
        try: conn.close()
        except Exception: pass


def build_roster(limit: int = 500) -> dict:
    """The READ-ONLY portal payload: reconciled accounts + per-segment counts.
    Best-effort — never raises; an empty roster on any error / no DB."""
    data = _gather_accounts(limit)
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts_by_segment": data.get("counts_by_segment", {}),
        "accounts": data.get("accounts", []),
        "high_usage_30d_threshold": _HIGH_USAGE_30D_THRESHOLD,
        "note": "READ-ONLY admin reconciliation of billing (users.plan) vs the "
                "MCP paywall (mcp_dev_keys.tier) with a segment per account. No "
                "writes; the api_key is redacted to a tail. PII is admin-gated.",
    }


# ════════════════════════════════════════════════════════════════════
#  Endpoints (admin-gated, READ-ONLY)
# ════════════════════════════════════════════════════════════════════
@customer_portal_bp.after_request
def _no_store(resp):
    # These carry account PII + an admin key in the URL — never let CF edge-cache
    # an admin 200 and serve it to anon. (Mirrors the admin brain dashboards.)
    resp.headers["Cache-Control"] = "no-store, private"
    return resp


@customer_portal_bp.route("/api/v1/portal/accounts", methods=["GET"])
def portal_accounts():
    """READ-ONLY reconciled account roster with a segment per row + per-segment
    counts. Admin-gated (PII). Best-effort: empty roster, never a 500.
    Query: ?limit=N (1..500, default 500)."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only — internal customer portal",
                       hint="append ?admin_key= or send X-Admin-Key"), 403
    try:
        limit = int(request.args.get("limit", "500"))
    except Exception:
        limit = 500
    return jsonify(build_roster(limit)), 200


@customer_portal_bp.route("/api/v1/portal", methods=["GET"])
def portal_page():
    """The browser-openable, self-contained admin page. Fetches
    /api/v1/portal/accounts and renders a filterable table with per-segment
    filter chips + a segment badge per row. Admin-gated; carries the same admin
    key the JSON fetch needs. STRICTLY READ-ONLY (no writes from this module)."""
    if not _admin_ok():
        return Response(_ADMIN_ONLY_HTML, mimetype="text/html", status=403)
    resp = Response(_PAGE_HTML, mimetype="text/html")
    # Remember the key so the operator pastes it once (same UX as the brain
    # dashboards). Only (re)set it when PROVIDED this visit via query/header.
    _provided = (request.headers.get("X-Internal-Key")
                 or request.headers.get("X-Admin-Key")
                 or request.args.get("admin_key") or "").strip()
    if _provided:
        resp.set_cookie("dchub_innov_key", _provided, max_age=30 * 24 * 3600,
                        secure=True, samesite="Lax", path="/")
    return resp


# ── Self-contained HTML (inline CSS/JS, no build step) ───────────────
_PAGE_HTML = r"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8">
<title>Customer Portal · DC Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<style>
:root{--bg:#0a0a12;--surface:#11121a;--surface2:#0d0e16;--bd:#1f2030;--tx:#fff;
  --tx2:#9ca3af;--tx3:#6b7280;--indigo:#6366f1;--violet:#a855f7;--green:#10b981;
  --amber:#f59e0b;--red:#ef4444;--blue:#3b82f6;--grad:linear-gradient(135deg,#6366f1,#a855f7);
  --mono:'JetBrains Mono','SF Mono',ui-monospace,monospace;color-scheme:dark}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  background:var(--bg);color:var(--tx);margin:0;line-height:1.5;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1600px;margin:0 auto;padding:2rem 1.25rem}
.kicker{font-family:var(--mono);font-size:.74rem;color:#c4b5fd;text-transform:uppercase;
  letter-spacing:.14em;margin-bottom:.5rem;display:flex;align-items:center;gap:.5rem}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--green);
  box-shadow:0 0 8px var(--green);animation:p 2s ease-in-out infinite}
@keyframes p{0%,100%{opacity:1}50%{opacity:.35}}
h1{margin:0 0 .35rem;font-size:1.9rem;font-weight:800;letter-spacing:-.02em;
  background:linear-gradient(90deg,#fff,#c4b5fd);-webkit-background-clip:text;
  background-clip:text;color:transparent}
.sub{color:var(--tx2);max-width:880px;margin:0 0 1.25rem;font-size:.92rem}
.bar{display:flex;align-items:center;gap:1rem;flex-wrap:wrap;margin-bottom:1.25rem;
  font-size:.8rem;color:var(--tx3);font-family:var(--mono)}
.bar .dot{color:var(--tx2)}
.bar a{color:#c4b5fd;text-decoration:none}
.chips{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1rem}
.chip{font-family:var(--mono);font-size:.76rem;font-weight:600;cursor:pointer;
  border:1px solid var(--bd);background:var(--surface);color:var(--tx2);
  border-radius:99px;padding:.35rem .85rem;transition:all .12s;display:inline-flex;
  align-items:center;gap:.45rem}
.chip:hover{border-color:#3a3d55;color:var(--tx)}
.chip.active{background:#6366f11a;border-color:#6366f1aa;color:#c4b5fd}
.chip .n{font-size:.72rem;background:var(--surface2);border:1px solid var(--bd);
  border-radius:99px;padding:.05rem .45rem;color:var(--tx2)}
.chip.active .n{color:#c4b5fd}
input.search{font-family:inherit;font-size:.85rem;background:var(--surface);
  border:1px solid var(--bd);border-radius:8px;color:var(--tx);padding:.45rem .7rem;
  min-width:220px;margin-bottom:1rem}
input.search::placeholder{color:var(--tx3)}
.tablewrap{overflow-x:auto;border:1px solid var(--bd);border-radius:12px;
  background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:.84rem;min-width:1000px}
th{text-align:left;font-family:var(--mono);font-size:.68rem;text-transform:uppercase;
  letter-spacing:.08em;color:var(--tx3);font-weight:600;padding:.6rem .8rem;
  border-bottom:1px solid var(--bd);white-space:nowrap;position:sticky;top:0;
  background:var(--surface2)}
td{padding:.55rem .8rem;border-bottom:1px solid #16172180;vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:#6366f108}
.em{font-weight:600;color:var(--tx)}
.nm{color:var(--tx3);font-size:.78rem}
.num{font-family:var(--mono);font-size:.8rem}
.muted{color:var(--tx3)}
.badge{display:inline-flex;align-items:center;gap:.3rem;font-family:var(--mono);
  font-size:.68rem;font-weight:700;padding:.16rem .5rem;border-radius:99px;
  border:1px solid;white-space:nowrap}
.seg-keyless_payer{color:var(--green);border-color:#10b98155;background:#10b9811a}
.seg-comped_paid{color:var(--violet);border-color:#a855f755;background:#a855f71a}
.seg-high_usage_free{color:var(--blue);border-color:#3b82f655;background:#3b82f61a}
.seg-at_risk_paid{color:var(--amber);border-color:#f59e0b55;background:#f59e0b1a}
.seg-none{color:var(--tx3);border-color:var(--bd);background:transparent}
.tier{color:#c4b5fd;border-color:#6366f155;background:#6366f11a}
.tier-free{color:var(--tx3);border-color:var(--bd);background:transparent}
.payer-yes{color:var(--green);border-color:#10b98155;background:#10b9811a}
.payer-no{color:var(--tx3);border-color:var(--bd);background:transparent}
.empty{color:var(--tx3);font-size:.9rem;padding:2rem;text-align:center}
.chan{font-family:var(--mono);font-size:.62rem;color:var(--tx3);margin-top:.1rem}
footer{margin-top:2rem;padding-top:1.25rem;border-top:1px solid var(--bd);
  color:var(--tx3);font-size:.8rem;text-align:center}
.legend{font-family:var(--mono);font-size:.7rem;color:var(--tx3);margin:0 0 1rem;
  line-height:1.7}
.legend b{color:var(--tx2)}
</style></head><body><div class="wrap">
<div class="kicker"><span class="pulse"></span>DC HUB · CUSTOMER PORTAL · INTERNAL</div>
<h1>Customer portal</h1>
<p class="sub">READ-ONLY reconciliation of billing (<code>users.plan</code>) against the
MCP paywall (<code>mcp_dev_keys.tier</code>) — two nearly-disjoint populations — with
each account classified into an outreach segment. Nothing here writes: the api_key is
shown as a redacted tail and account data is admin-gated.</p>
<div class="bar">
  <span id="status">Loading…</span>
  <span class="dot">·</span>
  <span id="updated"></span>
  <span class="dot">·</span>
  <span id="total"></span>
  <span class="dot">·</span>
  <a href="#" id="refreshNow">refresh now</a>
</div>
<div class="legend">
  <b>keyless_payer</b> pays a real invoice, no paid MCP key (transactional) &nbsp;·&nbsp;
  <b>comped_paid</b> paid plan, 0 real invoices (marketing) &nbsp;·&nbsp;
  <b>high_usage_free</b> free + heavy usage + opt-in (marketing) &nbsp;·&nbsp;
  <b>at_risk_paid</b> real payer, no 7d usage (transactional)
</div>
<div class="chips" id="chips"></div>
<input class="search" id="search" type="search" placeholder="filter by email / name…" autocomplete="off">
<div class="tablewrap">
  <table>
    <thead><tr>
      <th>Account</th><th>Plan</th><th>Real&nbsp;payer</th><th>MCP&nbsp;key</th>
      <th>Calls&nbsp;30d</th><th>Calls&nbsp;7d</th><th>Last&nbsp;active</th>
      <th>Sub&nbsp;status</th><th>MRR</th><th>Segment</th>
    </tr></thead>
    <tbody id="rows"><tr><td colspan="10" class="empty">Loading…</td></tr></tbody>
  </table>
</div>
<footer>READ-ONLY admin portal · billing × MCP paywall reconciliation ·
users · mcp_dev_keys · mcp_call_log · mcp_conversions · api_key redacted</footer>
</div>
<script>
(function(){
  function getCookie(n){var m=document.cookie.match(new RegExp('(?:^|; )'+n.replace(/([.*+?^${}()|[\]\\])/g,'\\$1')+'=([^;]*)'));return m?decodeURIComponent(m[1]):'';}
  var KEY = new URLSearchParams(location.search).get('admin_key') || getCookie('dchub_innov_key') || '';
  function authq(url){ return KEY ? (url + (url.indexOf('?')<0?'?':'&') + 'admin_key=' + encodeURIComponent(KEY)) : url; }
  function authh(){ var h={}; if(KEY){h['X-Admin-Key']=KEY;} return h; }
  function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];}); }

  var SEGS = [
    {k:'all',          label:'All'},
    {k:'keyless_payer',label:'keyless_payer'},
    {k:'comped_paid',  label:'comped_paid'},
    {k:'high_usage_free',label:'high_usage_free'},
    {k:'at_risk_paid', label:'at_risk_paid'},
    {k:'none',         label:'unsegmented'}
  ];
  var ALL = [], COUNTS = {}, FILTER = 'all', QUERY = '';

  function segBadge(s){
    if(!s) return '<span class="badge seg-none">—</span>';
    return '<span class="badge seg-'+esc(s)+'">'+esc(s)+'</span>';
  }
  function payerBadge(p){ return p ? '<span class="badge payer-yes">✓ paid</span>'
                                   : '<span class="badge payer-no">comp/none</span>'; }
  function keyCell(a){
    if(!a.key_tier) return '<span class="muted">— none</span>';
    var cls = (a.key_tier==='paid'||a.key_tier==='enterprise') ? 'tier' : 'tier-free';
    return '<span class="badge '+cls+'">'+esc(a.key_tier)+'</span> '
         + '<span class="muted num">'+esc(a.key_tail||'')+'</span>';
  }
  function fmtDate(d){ return d ? esc(String(d).slice(0,10)) : '<span class="muted">—</span>'; }
  function mrr(v){ var n=Number(v||0); return n>0 ? '<span class="num" style="color:var(--green)">$'+n.toFixed(0)+'</span>' : '<span class="muted">—</span>'; }

  function renderChips(){
    var el=document.getElementById('chips');
    el.innerHTML = SEGS.map(function(s){
      var n = s.k==='all' ? ALL.length
            : s.k==='none' ? (ALL.filter(function(a){return !a.segment;}).length)
            : (COUNTS[s.k]||0);
      return '<span class="chip'+(FILTER===s.k?' active':'')+'" data-seg="'+s.k+'">'
           + esc(s.label)+' <span class="n">'+n+'</span></span>';
    }).join('');
  }

  function matches(a){
    if(FILTER!=='all'){
      if(FILTER==='none'){ if(a.segment) return false; }
      else if(a.segment!==FILTER) return false;
    }
    if(QUERY){
      var hay=((a.email||'')+' '+(a.name||'')).toLowerCase();
      if(hay.indexOf(QUERY)<0) return false;
    }
    return true;
  }

  function renderRows(){
    var tb=document.getElementById('rows');
    var rows = ALL.filter(matches);
    if(!rows.length){ tb.innerHTML='<tr><td colspan="10" class="empty">No accounts match this filter.</td></tr>'; return; }
    tb.innerHTML = rows.map(function(a){
      return '<tr>'
        + '<td><div class="em">'+esc(a.email)+'</div>'+(a.name?'<div class="nm">'+esc(a.name)+'</div>':'')+'</td>'
        + '<td><span class="badge tier">'+esc(a.plan||'free')+'</span></td>'
        + '<td>'+payerBadge(a.real_payer)+'</td>'
        + '<td>'+keyCell(a)+'</td>'
        + '<td class="num">'+esc(a.calls_30d||0)+'</td>'
        + '<td class="num">'+esc(a.calls_7d||0)+'</td>'
        + '<td class="num">'+fmtDate(a.last_active)+'</td>'
        + '<td class="muted num">'+esc(a.subscription_status||'—')+'</td>'
        + '<td>'+mrr(a.mrr_usd)+'</td>'
        + '<td>'+segBadge(a.segment)+'</td>'
        + '</tr>';
    }).join('');
  }

  function render(){ renderChips(); renderRows(); }

  document.getElementById('chips').addEventListener('click', function(ev){
    var c=ev.target.closest && ev.target.closest('.chip'); if(!c) return;
    FILTER=c.getAttribute('data-seg'); render();
  });
  document.getElementById('search').addEventListener('input', function(ev){
    QUERY=String(ev.target.value||'').trim().toLowerCase(); render();
  });

  function load(){
    document.getElementById('status').textContent='Loading…';
    fetch(authq('/api/v1/portal/accounts'), {headers:authh()})
      .then(function(r){
        if(r.status===403){ document.getElementById('status').textContent='Admin key required — append ?admin_key=…'; throw new Error('403'); }
        return r.json();
      })
      .then(function(d){
        ALL = (d && d.accounts) || [];
        COUNTS = (d && d.counts_by_segment) || {};
        document.getElementById('status').textContent='Live';
        document.getElementById('updated').textContent='as of '+String(d.generated_at||'').slice(0,19);
        document.getElementById('total').textContent=ALL.length+' accounts';
        render();
      })
      .catch(function(){
        var tb=document.getElementById('rows');
        if(tb) tb.innerHTML='<tr><td colspan="10" class="empty">Could not load accounts.</td></tr>';
      });
  }
  document.getElementById('refreshNow').addEventListener('click', function(e){ e.preventDefault(); load(); });
  load();
})();
</script>
</body></html>"""


def register_customer_portal(app) -> None:
    """Idempotent registration helper for main.py. STRICTLY READ-ONLY surface —
    this module writes NOTHING (no tables to bootstrap). Wires the segmented JSON
    endpoint + the browser-openable admin page. Never raises."""
    try:
        app.register_blueprint(customer_portal_bp)
    except Exception as e:
        logger.warning("customer_portal already registered: %s", e)
