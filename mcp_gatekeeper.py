"""
DC Hub MCP Gatekeeper — Drop-in auth, rate limiting, tier gating for FastMCP
=============================================================================
Import this in dchub_mcp_server.py. Two functions per tool:

    result = _gate("tool_name", api_key)   # returns error JSON or None
    return _finalize(result, "tool_name", api_key)  # truncates, redacts, adds CTA

The auto-patcher (patch_mcp_server.py) adds these calls automatically.
"""

import os
import time
import json
import secrets
import logging
from enum import IntEnum
from collections import defaultdict
from datetime import datetime
from typing import Optional, Dict, Any, List

logger = logging.getLogger("dchub-mcp-gate")

# ═══════════════════════════════════════════════════════════════
# TIER SYSTEM
# ═══════════════════════════════════════════════════════════════

class Tier(IntEnum):
    # Phase GG (2026-05-15) — Bundle 5A: IDENTIFIED inserted between FREE
    # and DEVELOPER. Free user who's signed up + verified email gets 4x
    # the free limits + access to a few more tools (changes_since, pocket
    # teaser). Drives 72 free users → email signup → identified upgrade.
    # r34 (2026-05-22): STARTER inserted between IDENTIFIED and DEVELOPER —
    # the $9/mo taste-data tier the pricing page sells but the gatekeeper
    # had no place for, so $9 customers were misclassified. Order-preserving:
    # FREE < IDENTIFIED < STARTER < DEVELOPER < PRO < ENTERPRISE.
    FREE = 0
    IDENTIFIED = 1
    STARTER = 2
    DEVELOPER = 3
    PRO = 4
    ENTERPRISE = 5

TIER_NAME = {
    Tier.FREE: "Free",
    Tier.IDENTIFIED: "Identified",
    Tier.STARTER: "Starter",
    Tier.DEVELOPER: "Developer",
    Tier.PRO: "Pro",
    Tier.ENTERPRISE: "Enterprise",
}

# ═══════════════════════════════════════════════════════════════
# RATE LIMITS
# ═══════════════════════════════════════════════════════════════

# Phase EEEEE (2026-05-16): per-request grace metadata holder.
# Flask + WSGI is per-thread, so a thread-local holder is safe for
# stashing the grace/auto-trial metadata from _gate() so the tool
# handler can attach it to the response payload.
import threading as _threading
_grace_local = _threading.local()

def _set_pending_grace_meta(meta: dict) -> None:
    _grace_local.meta = meta

def get_pending_grace_meta() -> dict | None:
    """Tool handlers call this after their response is built to attach
    the grace + auto-trial metadata. Returns None if no grace was used
    for this request. Auto-clears so the next request starts fresh."""
    m = getattr(_grace_local, "meta", None)
    if m is not None:
        _grace_local.meta = None
    return m


LIMITS = {
    # Phase ZZZZ-tease (2026-05-18) — CONVERSION RECALIBRATION.
    # Brain narrative flagged the root cause: 1547 signals → 0 conversions
    # on get_market_intel because the FREE tier was generous enough that
    # users got what they needed without ever signing up. Tightening:
    #   • FREE: 1 row only (TEASER mode) — shows the shape, not the data
    #   • IDENTIFIED: 5 rows (preview-the-locked-content mode) — shows
    #     enough to be useful but with a concrete "X more on Developer"
    #   • DEVELOPER: 100 rows (real working tier)
    #   • PRO: 500 rows (no-friction tier)
    # The cap drops won't kill volume because the auto-trial mint still
    # passes the first 5 anon calls; this only changes the *response shape*
    # so each response makes the upgrade value concrete.
    Tier.FREE:       {"day": 25,     "minute": 3,   "max_rows": 1,    "cooldown": 3.0},
    Tier.IDENTIFIED: {"day": 200,    "minute": 15,  "max_rows": 5,    "cooldown": 1.0},
    # r34: $9/mo Starter — generous taste so people experience the data,
    # but well below Developer so the upgrade still has clear daylight.
    Tier.STARTER:    {"day": 600,    "minute": 30,  "max_rows": 25,   "cooldown": 0.5},
    Tier.DEVELOPER:  {"day": 2000,   "minute": 60,  "max_rows": 100,  "cooldown": 0},
    Tier.PRO:        {"day": 10000,  "minute": 200, "max_rows": 500,  "cooldown": 0},
    Tier.ENTERPRISE: {"day": 100000, "minute": 1000,"max_rows": 10000,"cooldown": 0},
}

# ═══════════════════════════════════════════════════════════════
# TOOL → MINIMUM TIER
# ═══════════════════════════════════════════════════════════════

TOOL_TIER = {
    # FREE — discovery-only warmup tools. Phase XXX (2026-05-16)
    # promoted search_facilities + get_news to IDENTIFIED. Reason:
    # together they were the #1 + #3 most-called tools (11,488 +
    # 7,352 calls in 14d) generating zero email captures. By
    # requiring the free email-only signup to call them, every
    # high-volume hit becomes a marketable lead. Free tier remains
    # generous on the LIST + RECOMMENDATION endpoints below so
    # agents can still warm up DC Hub without auth.
    "get_facility":            Tier.FREE,  # 1 facility = preview, kept free
    "get_dchub_recommendation":Tier.FREE,
    "get_agent_registry":      Tier.FREE,
    # r-cluster-open (2026-07-11): OPEN/adoption-first by design (Gemini
    # partnership spec; endpoint on free_tier_gate's open-exemption list;
    # server.mjs FREE_FULL_TOOLS). Listed EXPLICITLY because the gate lookup
    # defaults an ABSENT tool to Tier.DEVELOPER — despite the Phase-GG comment
    # below saying unlisted tools "default to free" — so absence would
    # silently paywall it if this surface ever serves the tool.
    "cluster_sites_by_latency":Tier.FREE,  # derived math + screening, cheap

    # IDENTIFIED — Phase XXX promotion. These two tools alone
    # represent ~50% of 14-day MCP traffic; gating at email-signup
    # is the highest-leverage conversion move available.
    "search_facilities":       Tier.IDENTIFIED,  # was FREE, 11,488 calls/14d
    "get_news":                Tier.IDENTIFIED,  # was FREE, 7,352 calls/14d

    # IDENTIFIED — Phase MM (2026-05-15) Bundle 9 funnel recovery.
    # Audit: 0.05% gate→pay conversion. Root cause: 7 high-demand tools
    # were gated at Developer ($49/mo). 240+ distinct free users hit
    # these tools daily but bounce at the price wall. Move them to
    # IDENTIFIED (free + email) with strict caps so the friction drops:
    # signup → email-only → instant access (capped) → upgrade later
    # when they want unlimited. Limits: 200 calls/day, 20 rows/call.
    "get_market_intel":        Tier.IDENTIFIED,  # 2,991 signals/30d
    "get_grid_data":           Tier.IDENTIFIED,  # 2,655 signals/30d
    "get_grid_intelligence":   Tier.IDENTIFIED,  # 102 users hit
    "get_fiber_intel":         Tier.IDENTIFIED,  # 101 users hit
    "get_water_risk":          Tier.IDENTIFIED,  # 2,608 signals/30d
    "get_energy_prices":       Tier.IDENTIFIED,  # 2,493 signals/30d
    "get_renewable_energy":    Tier.IDENTIFIED,  # 2,355 signals/30d

    # Phase PP (2026-05-15): demote 9 more tools DEVELOPER → IDENTIFIED to
    # unblock growth. User's report: "ever since we tweaked things [Bundle 9
    # tier moves], growth has stopped." Investigation showed traffic is
    # healthy (38k calls / 7d, 1,245 unique callers) but the DEVELOPER wall
    # was still blocking the most-requested lookup tools — agents could see
    # them in the tools list but couldn't actually pull data without a paid
    # key. Identified-tier (free, email-gated) is the right surface for
    # data LOOKUP tools; the paid moat narrows to the COMPOSITE site
    # analysis tools that synthesize the lookups into a recommendation.
    "list_transactions":       Tier.IDENTIFIED,  # 1,400+ deals M&A history
    "get_pipeline":            Tier.IDENTIFIED,  # 540+ active projects
    "get_infrastructure":      Tier.IDENTIFIED,  # substations/transmission/gas
    "get_grid_headroom":       Tier.IDENTIFIED,  # available MW within 50km
    "get_colocation_score":    Tier.IDENTIFIED,  # DCPI sub-score breakdown
    "get_geothermal_potential":Tier.IDENTIFIED,  # niche but high-signal
    "get_tax_incentives":      Tier.IDENTIFIED,  # state-level abatements
    "get_microgrid_viability": Tier.IDENTIFIED,  # site-level resilience
    "get_intelligence_index":  Tier.IDENTIFIED,  # DCPI scores for 300+ markets

    # DEVELOPER — single-site composite scorer (entry-paid hook)
    "analyze_site":            Tier.DEVELOPER,  # composite lat/lon scorer

    # PRO — Phase DDDD (2026-05-16): compare_sites promoted from
    # DEVELOPER to PRO. Multi-site comparison is the killer broker /
    # buyer workflow worth $199/mo; analyze_site stays DEVELOPER as
    # the entry hook. Plus 3 new PRO L+P tools so the /land-power-map
    # workflow has paid features beyond the free map view.
    "compare_sites":           Tier.PRO,  # was DEVELOPER
    "get_lp_alerts":           Tier.PRO,  # alerts on saved L+P sites
    "save_lp_site":            Tier.PRO,  # persist a candidate site
    "lp_bulk_export":          Tier.PRO,  # bulk CSV/GeoJSON export
    "get_backup_status":       Tier.PRO,

    # ═══════════════════════════════════════════════════════════════
    # Phase GG (2026-05-15) — Bundle 5A: tier the 25 new tools shipped
    # across Bundles 1-4. Without these entries, they default to free +
    # uncapped — meaning anyone can hammer the agent-leverage tools.
    # ═══════════════════════════════════════════════════════════════

    # Bundle 1 — session warm-up (warmup tools are deliberately FREE)
    "get_dchub_index":              Tier.FREE,
    "get_coverage":                 Tier.FREE,

    # Bundle 2 — persona-shaped bundled briefs (1 call = synthesis)
    "get_developer_brief":          Tier.DEVELOPER,
    "get_buyer_brief":              Tier.DEVELOPER,
    "get_investor_brief":           Tier.DEVELOPER,
    "get_policy_brief":             Tier.DEVELOPER,
    "get_market_brief":             Tier.DEVELOPER,  # PR #138

    # Bundle 3 — diff feed (rewards email signup)
    "get_changes_since":            Tier.IDENTIFIED,

    # Phase GG capacity / ISO / listings tools (PR #153)
    "get_site_capacity_report":     Tier.DEVELOPER,
    "get_iso_snapshot":             Tier.DEVELOPER,
    "get_iso_comparison":           Tier.DEVELOPER,
    "get_pocket_listings":          Tier.IDENTIFIED,  # teaser at free, full at identified+
    "get_pocket_listing":           Tier.IDENTIFIED,

    # DCPI MCP tools (PR #152)
    "get_dcpi_scores":              Tier.IDENTIFIED,
    "get_dcpi_market":              Tier.IDENTIFIED,
    "get_dcpi_movers":              Tier.DEVELOPER,
    "get_dcpi_iso":                 Tier.DEVELOPER,

    # Bundle 4 — Brain learning tools (operator-only)
    "get_brain_self_assessment":    Tier.PRO,
    "get_brain_effectiveness":      Tier.PRO,
    "get_brain_outcomes":           Tier.PRO,
    "get_brain_temporal_patterns":  Tier.PRO,
    "get_brain_model_performance":  Tier.PRO,
}

# Fields to REDACT on free tier (show placeholder)
REDACT_FIELDS = {
    "list_transactions": ["value", "notes", "assets"],
    "get_pipeline":      ["investment", "delivery"],
    "search_facilities": ["power_mw"],
    "get_facility":      ["power_mw", "pue"],
}

# ═══════════════════════════════════════════════════════════════
# API KEY → TIER RESOLUTION
# ═══════════════════════════════════════════════════════════════

# In-memory key store. On startup, load from DB or env.
# Format: { "dchub_dev_xxxx": Tier.DEVELOPER, ... }
_key_store: Dict[str, Tier] = {}

def _load_keys_from_env():
    """Load API keys from environment (DCHUB_API_KEYS=key1:dev,key2:pro,...)"""
    raw = os.environ.get("DCHUB_API_KEYS", "")
    if not raw:
        return
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        key, tier_str = pair.rsplit(":", 1)
        tier_map = {"free": Tier.FREE, "identified": Tier.IDENTIFIED,
                     "dev": Tier.DEVELOPER, "developer": Tier.DEVELOPER,
                     "pro": Tier.PRO, "enterprise": Tier.ENTERPRISE, "ent": Tier.ENTERPRISE,
                     "founding": Tier.PRO}  # founding members get Pro
        tier = tier_map.get(tier_str.lower(), Tier.FREE)
        _key_store[key.strip()] = tier
    if _key_store:
        logger.info(f"🔑 Loaded {len(_key_store)} API keys from env")

_load_keys_from_env()


def _load_keys_from_db():
    """Load API keys from Neon. Adapts to your existing api_keys schema:
    key_hash, user_id, is_active, rate_limit_tier, key_prefix
    """
    try:
        import psycopg2, psycopg2.extras
        url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
        if not url:
            return
        conn = psycopg2.connect(url, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Try existing DC Hub schema first (key_hash + rate_limit_tier + users.plan)
            try:
                cur.execute("""
                    SELECT ak.key_prefix, ak.rate_limit_tier, COALESCE(u.plan, 'free') as plan
                    FROM api_keys ak
                    LEFT JOIN users u ON ak.user_id = u.id
                    WHERE ak.is_active = 1 OR ak.is_active = true
                """)
                # r32-sweep (2026-05-20): added 'identified' + 'founding'
                # + 'anonymous'. Pre-fix, a user with plan='identified'
                # in users.plan resolved to Tier.FREE here → MCP gave
                # them free-tier limits and tool access even though
                # they'd signed up with email. Same bug class as
                # api_tier_gating.
                tier_map = {"anonymous": Tier.FREE, "anon": Tier.FREE,
                            "free": Tier.FREE,
                            "identified": Tier.IDENTIFIED,
                            "developer": Tier.DEVELOPER, "dev": Tier.DEVELOPER,
                            "founding": Tier.PRO,    # founding = Pro-equivalent
                            "pro": Tier.PRO,
                            "enterprise": Tier.ENTERPRISE, "ent": Tier.ENTERPRISE,
                            "admin": Tier.ENTERPRISE}
                count = 0
                for row in cur.fetchall():
                    # Map plan/rate_limit_tier to our tier system
                    plan = (row.get("plan") or row.get("rate_limit_tier") or "free").lower()
                    tier = tier_map.get(plan, Tier.FREE)
                    prefix = row.get("key_prefix", "")
                    if prefix:
                        # Store prefix -> tier mapping for prefix-based lookup
                        _key_store[f"prefix:{prefix}"] = tier
                        count += 1
                if count:
                    logger.info(f"🔑 Loaded {count} API key prefixes from DB")
            except Exception as inner_e:
                logger.debug(f"Existing schema query failed: {inner_e}")
                # Fallback: try simple schema
                try:
                    cur.execute("SELECT api_key, tier FROM api_keys WHERE active = true")
                    for row in cur.fetchall():
                        # r32-sweep: parity with the canonical tier_map above.
                        tier_map = {"anonymous": Tier.FREE, "anon": Tier.FREE,
                                    "free": Tier.FREE,
                                    "identified": Tier.IDENTIFIED,
                                    "developer": Tier.DEVELOPER, "dev": Tier.DEVELOPER,
                                    "founding": Tier.PRO,
                                    "pro": Tier.PRO,
                                    "enterprise": Tier.ENTERPRISE, "ent": Tier.ENTERPRISE,
                                    "admin": Tier.ENTERPRISE}
                        _key_store[row["api_key"]] = tier_map.get(row.get("tier", "free"), Tier.FREE)
                    logger.info(f"🔑 Loaded {len(_key_store)} API keys from DB (simple schema)")
                except Exception:
                    pass  # Neither schema works — rely on env + prefix-based resolution
        conn.close()
    except Exception as e:
        logger.warning(f"⚠️ Could not load keys from DB: {e}")


def resolve_tier(api_key: Optional[str]) -> Tier:
    """Resolve API key to tier. No key = Free.
    Checks: in-memory store → prefix match → DB hash lookup (cached).
    """
    if not api_key:
        return Tier.FREE
    # Check in-memory store first
    if api_key in _key_store:
        return _key_store[api_key]
    # Prefix-based resolution (fast path for new-style keys).
    # r78-b (2026-06-09): partner_key_issuer mints keys with LONG-FORM plan
    # names — dchub_developer_*, dchub_enterprise_* — but this resolver
    # only knew the short forms. Long-form fell through to the DB hash
    # lookup, which doesn't match partner keys (stored raw, not hashed),
    # so tier resolved to FREE → site_risk_water gate fired 402 on NLR
    # partner keys. Accept BOTH forms. Long-form match check goes first
    # because "dchub_enterprise_" startswith "dchub_ent" — short check
    # would consume the long-form key incorrectly.
    if api_key.startswith("dchub_enterprise_"): return Tier.ENTERPRISE
    if api_key.startswith("dchub_developer_"):  return Tier.DEVELOPER
    if api_key.startswith("dchub_starter_"):    return Tier.STARTER
    if api_key.startswith("dchub_ent_"):        return Tier.ENTERPRISE
    if api_key.startswith("dchub_pro_"):        return Tier.PRO
    if api_key.startswith("dchub_dev_"):        return Tier.DEVELOPER
    if api_key.startswith("dchub_sta_"):        return Tier.STARTER
    # Phase DDDDD (2026-05-16): auto-mint trial keys (`dch_trial_`)
    # resolve as IDENTIFIED tier. Validation against DB happens lazily
    # on first call; the prefix check here keeps the hot path fast.
    # If the trial is expired or unknown, the per-call check inside
    # the tool handler can re-validate via routes.auto_trial.validate_trial_key.
    if api_key.startswith("dch_trial_"):
        try:
            from routes.auto_trial import validate_trial_key
            ok, _ = validate_trial_key(api_key)
            return Tier.IDENTIFIED if ok else Tier.FREE
        except Exception:
            return Tier.IDENTIFIED  # be lenient — DB issues shouldn't break gate

    # DB hash lookup for old-style keys (dchub_XXXXX without tier prefix)
    if api_key.startswith("dchub_"):
        tier = _resolve_from_db_hash(api_key)
        if tier is not None:
            _key_store[api_key] = tier  # cache it
            return tier

    return Tier.FREE


def _resolve_from_db_hash(api_key: str) -> Optional[Tier]:
    """Look up an old-style key by its SHA-256 hash in the api_keys table.

    PATCH 2026-04-24 (jm): P0 — every Enterprise customer was being silently
    treated as free tier because this query had `(ak.is_active = 1 OR
    ak.is_active = true)`. The api_keys.is_active column is `integer`, and
    PostgreSQL refuses to compare `integer = boolean` — it raises
    `operator does not exist: integer = boolean`, which then got swallowed
    by the `except Exception` below at DEBUG level (invisible in prod logs).
    Result: every call to this function returned None, resolve_tier()
    fell through to Tier.FREE, and every paying customer saw free-tier
    responses in MCP.

    Fix: drop the boolean branch — is_active is always integer in this
    schema — and promote the exception from DEBUG to WARNING so any
    future silent failure surfaces in Railway logs.
    """
    try:
        import hashlib, psycopg2, psycopg2.extras
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
        if not url:
            return None
        conn = psycopg2.connect(url, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # PATCH 2026-07-01 (jm): dual key_hash match. Partner/admin/QA keys
            # (partner_key_issuer, qa_canary_001) store key_hash = the RAW key
            # string, not sha256 — the same convention util/tier_gate.py (r79.1)
            # and /api/v1/me already handle with key_hash IN (hash, raw). The
            # hash-only match here missed all 14 raw-stored active keys, so
            # resolve_tier() fell through to FREE and every route gating via
            # routes.tier_gate._resolve_caller_tier (e.g. hyperscaler-brief)
            # served the free teaser to pro keys that /me/tier resolved as pro.
            cur.execute("""
                SELECT ak.rate_limit_tier, ak.plan, COALESCE(u.plan, 'free') as user_plan
                FROM api_keys ak
                LEFT JOIN users u ON ak.user_id = u.id
                WHERE ak.key_hash IN (%s, %s) AND (ak.is_active = 1 OR ak.is_active IS NULL)
                LIMIT 1
            """, (key_hash, api_key))
            row = cur.fetchone()
        conn.close()
        if row:
            plan = (row.get("plan") or row.get("rate_limit_tier") or row.get("user_plan") or "free").lower()
            # r32-sweep (2026-05-20): added 'identified' — every paying
            # email-signup customer was resolving here to Tier.FREE.
            # Same bug as the loader paths above + the api_tier_gating
            # gap fixed in 4e36c4f9.
            tier_map = {"anonymous": Tier.FREE, "anon": Tier.FREE,
                        "free": Tier.FREE,
                        "identified": Tier.IDENTIFIED,
                        "developer": Tier.DEVELOPER, "dev": Tier.DEVELOPER,
                        "founding": Tier.PRO,
                        "pro": Tier.PRO,
                        "enterprise": Tier.ENTERPRISE, "ent": Tier.ENTERPRISE,
                        "admin": Tier.ENTERPRISE}
            return tier_map.get(plan, Tier.FREE)
    except Exception as e:
        # Promoted from DEBUG → WARNING so silent tier-downgrades surface in logs.
        logger.warning(
            "mcp_gatekeeper._resolve_from_db_hash failed for key prefix %s: %s",
            (api_key or "")[:12], e, exc_info=True
        )
    return None


def generate_key(tier: Tier = Tier.FREE) -> str:
    prefix = {Tier.FREE: "dchub_free_", Tier.DEVELOPER: "dchub_dev_",
              Tier.PRO: "dchub_pro_", Tier.ENTERPRISE: "dchub_ent_"}
    return prefix[tier] + secrets.token_urlsafe(32)


# ═══════════════════════════════════════════════════════════════
# RATE LIMITER (in-memory, single instance)
# ═══════════════════════════════════════════════════════════════

class _RateLimiter:
    def __init__(self):
        self._minute: Dict[str, List[float]] = defaultdict(list)
        self._day: Dict[str, Dict[str, int]] = {}
        self._last: Dict[str, float] = {}

    def _today(self) -> str:
        return datetime.utcnow().strftime("%Y-%m-%d")

    def check(self, key: str, tier: Tier) -> Optional[str]:
        """Returns error message if rate-limited, None if OK.

        Phase ZZZZ-trial-cap (2026-05-18): trial keys get TIGHTER caps
        than full IDENTIFIED tier so agents see the wall + upgrade
        earlier in the trial cycle. Currently 50/day (vs 200) and
        cooldown 1.5s (vs 1s)."""
        now = time.time()
        lim = LIMITS[tier]

        # TRIAL OVERRIDE — keys minted by auto_trial have tighter caps
        # despite resolving to IDENTIFIED tier (they can call IDENTIFIED
        # tools but at FREE-ish volume).
        is_trial = bool(key) and isinstance(key, str) and key.startswith("dch_trial_")
        if is_trial:
            lim = {**lim, "day": 50, "minute": 10, "cooldown": 1.5}

        # r-streak (2026-07-18): progressive return-streak boost — the #1
        # constraint actuator (mature key-reuse 4.9% vs 8% floor). Free-side
        # keys (FREE/IDENTIFIED, incl. trial) that return on distinct days
        # earn a higher DAILY cap: 2+ active days in the trailing 14 = 1.5x,
        # 4+ = 2x, 7+ = 3x (streak cached ~1h per key in return_streak).
        # FAIL-OPEN: any error -> base cap, never block. Paid tiers untouched.
        streak_days = 0
        day_base = lim["day"]  # pre-boost base, for honest streak messaging
        if key and isinstance(key, str) and tier <= Tier.IDENTIFIED:
            try:
                from return_streak import get_streak_days, boosted_cap
                streak_days = get_streak_days(key)
                if streak_days >= 2:
                    lim = {**lim, "day": boosted_cap(day_base, streak_days)}
            except Exception:
                streak_days = 0

        # Cooldown
        if lim["cooldown"] > 0:
            gap = now - self._last.get(key, 0)
            if gap < lim["cooldown"]:
                return f"Rate limited: wait {lim['cooldown'] - gap:.1f}s"

        # Per-minute
        win = self._minute[key]
        win[:] = [t for t in win if t > now - 60]
        if len(win) >= lim["minute"]:
            return (f"Rate limited: {len(win)}/{lim['minute']} calls/min. "
                    f"{'Trial' if is_trial else 'Free'} tier. "
                    f"🤖 Remove the ceiling — {_pack5_cta()} "
                    f"(or {_starter_cta()})")

        # Per-day
        today = self._today()
        if key not in self._day:
            self._day[key] = {}
        dc = self._day[key]
        count = dc.get(today, 0)
        if count >= lim["day"]:
            # r-streak: the wall is the moment to teach the return incentive —
            # current streak + what returning tomorrow unlocks. Fail-open "".
            _streak_note = ""
            if tier <= Tier.IDENTIFIED:
                try:
                    from return_streak import streak_line
                    _streak_note = " 📈 " + streak_line(streak_days, day_base)
                except Exception:
                    _streak_note = ""
            return (f"Rate limited: {count}/{lim['day']} calls today. "
                    f"{'Trial' if is_trial else 'Free'} tier. "
                    f"🤖 No daily ceiling — {_pack5_cta()} "
                    f"(or {_starter_cta()})."
                    f"{_streak_note}")

        # Record
        win.append(now)
        dc[today] = count + 1
        self._last[key] = now
        # Cleanup old days
        for k in list(dc.keys()):
            if k != today: del dc[k]
        return None

    def usage(self, key: str) -> Dict:
        today = self._today()
        return {
            "today": self._day.get(key, {}).get(today, 0),
            "this_minute": len([t for t in self._minute.get(key, []) if t > time.time() - 60]),
        }

_rl = _RateLimiter()


# ═══════════════════════════════════════════════════════════════
# UPGRADE CTAs
# ═══════════════════════════════════════════════════════════════

PRICING_URL = "https://dchub.cloud/pricing"


# ── Canonical CTA fragments (monthly-quota phase 2, 2026-08-06) ──────────
# Price, quota and Stripe link are each READ from their source of truth —
# tier_registry.price, monthly_quota.monthly_quota_for and
# routes/_stripe_links.STRIPE_LINKS — never hardcoded here.
#
# WHY: this file carried its own hardcoded Stripe URLs and they had drifted.
# The rate-limit CTAs below pointed at 14k14og7w7Zz9KJ8i6aZi02 for "$9/mo",
# which is not the canonical starter link in _stripe_links.py; TIER_PRICE
# quoted Pro at $199 after the r-reprice move to $299. That is the exact
# class of drift _stripe_links.py was created to end (the $299-vs-$199
# incident), and a stale payment link on a paywall is a lost sale.
#
# All three helpers are fail-soft: a missing routes/ or a repriced tier
# degrades to the pricing page, never to a wrong number or a dead link.

def _canonical_link(tier_key):
    try:
        from routes._stripe_links import STRIPE_LINKS
        return STRIPE_LINKS.get(tier_key) or PRICING_URL
    except Exception:
        return PRICING_URL


def _canonical_monthly(tier_key):
    """Canonical MONTHLY MCP quota for a tier (mcp_daily x 30)."""
    try:
        from monthly_quota import monthly_quota_for
        return monthly_quota_for(tier_key)
    except Exception:
        return 0


def _canonical_price(tier_key, fallback=""):
    try:
        from tier_registry import price as _tier_price
        v = _tier_price(tier_key)
        return f"${v}/mo" if v else fallback
    except Exception:
        return fallback


def _starter_cta():
    """'$9/mo = 6,000 calls/month → <canonical starter link>'.

    Was hardcoded as '$9/mo=200/day' behind a non-canonical link. Paid
    ceilings are MONTHLY now (monthly_quota.py) — the per-day number was
    never enforced on the /mcp path, so quoting it advertised a limit that
    did not exist and understated what the buyer gets by 30x.
    """
    n = _canonical_monthly("starter")
    qty = f"{n:,} calls/month" if n else "the full starter quota"
    return f"{_canonical_price('starter', '$9/mo')} = {qty} → {_canonical_link('starter')}"


def _pack5_cta():
    """The $10 one-time credit pack — a call PACK, not a period cap."""
    return f"$10 one-time = 1,000 API calls → {_canonical_link('pack5')}"


# Per-tier monthly pricing (for inclusion in CTAs — gives MCP clients
# concrete numbers to relay to the human instead of just "upgrade").
TIER_PRICE = {
    Tier.DEVELOPER:  _canonical_price("developer", "$49/mo"),
    Tier.PRO:        _canonical_price("pro", "$299/mo"),
    Tier.ENTERPRISE: "Contact sales",
}

# ═══════════════════════════════════════════════════════════════
# OPT-IN CTA (marketing consent capture) — ships DARK
# ═══════════════════════════════════════════════════════════════
#
# GOAL: make high-usage FREE users *reachable* for upgrade outreach by
# getting them to OPT IN to marketing — compliantly. The outreach engine's
# high_usage_free segment hard-gates on mcp_dev_keys.metadata->>marketing_opt_in
# = 'true' (routes/upgrade_outreach.py); today ~0 are opted in, so the power
# users on get_grid_intelligence / get_fiber_intel are unreachable.
#
# This is the SOFT, honest, ADDITIVE CTA injected into the paywall response a
# FREE user gets when they hit one of those high-usage paid tools. It does NOT
# send anything — it only points the user at the opt-in REQUEST flow (which
# emails a tokenized CONFIRM link; opt-in is set ONLY on the confirm click —
# the double-opt-in confirm path is a sibling component). Compliance rules
# baked in here:
#   • FLAG-GATED behind OPTIN_CTA_ENABLED (default OFF) — ships dark, tunable
#     without redeploy.
#   • FREE users only (never shown to paid/identified+ — they're already
#     reachable / already customers).
#   • Targeted to the real pool: only the high-usage paid tools below.
#   • SOFT + honest value (free grid-data upgrade guide + early access), easy
#     to ignore, no dark pattern. No pre-checked anything; opt-in happens on a
#     later explicit confirm click, never from showing this CTA.
#   • Additive: returned as a separate structured field + an appended note —
#     it NEVER replaces or mutates the tool's real data payload.

# The high-usage paid tools that define the reachable pool we want to convert.
# Keep this tight so we only prompt where there's a real upgrade-outreach
# audience (184 power users on get_grid_intelligence, 166 on get_fiber_intel).
OPTIN_CTA_TOOLS = frozenset({
    "get_grid_intelligence",
    "get_fiber_intel",
})


def optin_cta_enabled() -> bool:
    """Master flag — ships DARK. Toggle via OPTIN_CTA_ENABLED=true (no
    redeploy). Mirrors the FREE_TIER_GATE_ENABLED env-gate convention."""
    return os.environ.get("OPTIN_CTA_ENABLED", "false").strip().lower() == "true"


def _optin_cta_block(tool_name: str, tier: int,
                     api_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Build the SOFT marketing opt-in CTA for a paywalled FREE-tier response.

    Returns a small structured dict (machine-readable card + a one-line note
    for text-only relay agents), or None when the CTA must NOT show. Pure +
    side-effect-free (no Flask/db/network) so it's safe to call on the hot
    paywall path and trivially unit-testable.

    Shows ONLY when ALL hold:
      • OPTIN_CTA_ENABLED flag is on (ships dark by default), AND
      • the caller is FREE tier (paid/identified+ are already reachable), AND
      • the tool is one of the high-usage paid tools (OPTIN_CTA_TOOLS).

    This NEVER sets marketing_opt_in. It only advertises the opt-in REQUEST
    flow; consent is captured later, on the tokenized confirm click (the
    double-opt-in confirm path is a separate component). No email is sent here.
    """
    if not optin_cta_enabled():
        return None
    # FREE only. Tier is an IntEnum (FREE=0); anyone identified or paying is
    # already reachable / already a customer — don't pester them.
    if tier != Tier.FREE:
        return None
    if tool_name not in OPTIN_CTA_TOOLS:
        return None

    # Opt-in REQUEST endpoint. Hitting it triggers the double-opt-in flow:
    # the server emails a tokenized CONFIRM link, and ONLY the confirm click
    # sets marketing_opt_in=true + logs consent. The CTA carries the source
    # tool (attribution) but no consent is implied by clicking this link.
    from urllib.parse import urlencode
    params = {"source": "paywall_optin_cta", "tool": tool_name}
    if api_key:
        # Lets the request flow pre-resolve the bound email for this key (if
        # any) so the human only confirms — still double-opt-in, no auto-set.
        params["key"] = api_key
    optin_url = f"https://dchub.cloud/api/v1/marketing/opt-in/request?{urlencode(params)}"

    note = (
        "📓 Power user? Get our free grid-data upgrade guide + early access to "
        "new datasets — opt in (we email you a confirm link, unsubscribe "
        "anytime): " + optin_url
    )
    return {
        "optin_url": optin_url,
        "optin_note": note,
        "optin_value": (
            "Free grid-data upgrade guide + early access to new datasets. "
            "Double opt-in (we email a confirm link); one-click unsubscribe "
            "anytime. Easy to ignore — your tool access is unchanged."
        ),
        "optin_double_opt_in": True,
        "optin_source": "paywall_optin_cta",
        "optin_tool": tool_name,
    }


def _optin_recipient_suppressed(api_key: Optional[str]) -> bool:
    """True if the email bound to this api_key is on the suppression list — so a
    previously-unsubscribed user is never RE-shown the opt-in CTA (CAN-SPAM).
    Called ONLY on the narrow CTA-candidate path (flag on + FREE + target tool),
    so the extra query is rare, not on every call. FAIL-SAFE: on any error /
    missing DB we return True (skip the CTA) rather than risk re-prompting."""
    if not api_key:
        return False  # anonymous/no bound email -> nothing to suppress
    try:
        import os as _os
        import psycopg2 as _pg
        from routes.email_suppression import is_suppressed as _is_suppressed
        dsn = _os.environ.get("DATABASE_URL") or _os.environ.get("NEON_DATABASE_URL")
        if not dsn:
            return True
        conn = _pg.connect(dsn, connect_timeout=4)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT email FROM mcp_dev_keys WHERE api_key=%s LIMIT 1",
                            (api_key,))
                row = cur.fetchone()
                email = ((row[0] if row else "") or "").strip()
                if not email:
                    return False  # key has no bound email -> nothing to suppress
                return bool(_is_suppressed(cur, email))
        finally:
            try: conn.close()
            except Exception: pass
    except Exception:
        return True  # fail-safe: skip the CTA on any error


# ═══════════════════════════════════════════════════════════════
# FIRST-CALL DURABLE-KEY SURFACE (ships DARK behind FIRST_CALL_KEY_SURFACE_ENABLED)
# ═══════════════════════════════════════════════════════════════
# GOAL: multi-day MCP retention is ~5% — agents chain calls within ONE session
# (64%) but ~never return on a LATER day. Root cause = CREDENTIAL FRICTION: a
# new/anonymous caller succeeds via inline auto-trial (anon-grace) but the
# durable key never gets SURFACED on the SUCCESS path, so the agent doesn't
# persist it and the human can't come back tomorrow without re-provisioning.
#
# FIX: on the FIRST successful call for a caller that has NO durable/bound key,
# additively surface the caller's OWN auto-trial key (already minted by the
# anon-grace path this same request — reused/extended so it persists ≥7d, and
# 365d once email-bound) + a one-line "save it, reuse across sessions, bind your
# email to keep it" note. We do NOT mint a second key system — we reuse the
# auto-trial + bind paths.
#
# RAILS honored here:
#   (1) ADDITIVE — a separate `_meta.first_call_key_offer` field + one appended
#       line; NEVER replaces/mutates the tool's data payload.
#   (2) FIRST-CALL ONLY — only fires when THIS request's anon-grace minted/reused
#       an auto-trial key for a caller with no durable bound key. Once the caller
#       passes the gate on their own durable/bound key, grace_meta is absent →
#       suppressed (not on every call).
#   (3) DURABLE — the surfaced key is the auto-trial (≥7d unbound, 365d bound);
#       honest expiry + cap come straight from the mint result, never a promise.
#   (4) FLAG-GATED DARK — FIRST_CALL_KEY_SURFACE_ENABLED default OFF.
#   (5) NO LEAK — the key comes ONLY from this request's own grace_meta (minted
#       for this caller) or the api_key the caller themselves passed; never
#       another caller's key, and it's only ever placed in the caller's own
#       response (never logged).


def first_call_key_surface_enabled() -> bool:
    """Master flag — ships DARK. Toggle via FIRST_CALL_KEY_SURFACE_ENABLED=true
    (no redeploy). Mirrors the OPTIN_CTA_ENABLED / FREE_TIER_GATE_ENABLED
    env-gate convention."""
    return os.environ.get(
        "FIRST_CALL_KEY_SURFACE_ENABLED", "false").strip().lower() == "true"


def _first_call_key_surface_block(tool_name: str, tier: int,
                                  api_key: Optional[str],
                                  grace_meta: Optional[Dict[str, Any]]
                                  ) -> Optional[Dict[str, Any]]:
    """Build the additive first-call durable-key surface for a SUCCESSFUL call.

    Pure + side-effect-free (no Flask/db/network) so it's safe on the hot
    success path and trivially unit-testable. Returns a small structured dict
    (machine-readable card + a one-line `note` for text-only relay agents), or
    None when the surface must NOT show.

    Shows ONLY when ALL hold:
      • FIRST_CALL_KEY_SURFACE_ENABLED flag is on (ships dark by default), AND
      • the caller is sub-DEVELOPER (new/free/identified — established paid
        callers already have a durable key + are already customers), AND
      • THIS request's anon-grace minted/reused an auto-trial key for the caller
        (grace_meta carries `auto_trial_key`) — i.e. they had NO durable bound
        key and just got one. This is the FIRST-CALL signal: a caller arriving
        on their own durable/bound key passes the gate WITHOUT grace, so
        grace_meta is absent and the surface is suppressed (not every call).

    NO LEAK: the surfaced key is taken ONLY from grace_meta["auto_trial_key"]
    (minted for THIS caller, this request). We never read another caller's key.
    """
    if not first_call_key_surface_enabled():
        return None
    # Established paid callers already hold a durable key — don't pester them.
    if tier >= Tier.DEVELOPER:
        return None
    if not grace_meta:
        return None
    surfaced_key = (grace_meta.get("auto_trial_key") or "").strip() \
        if isinstance(grace_meta, dict) else ""
    if not surfaced_key:
        return None

    # Honest durability — read the ACTUAL terms off the mint result, never
    # promise more than the DB granted. expires_at / daily cap come from the
    # grace_meta the anon-grace path stashed when it minted/reused the key.
    expires_at = grace_meta.get("auto_trial_expires_at")
    daily_calls = grace_meta.get("auto_trial_daily_calls")

    # The bind path that turns a 7d unbound trial into a 365d, higher-cap key.
    # /api/v1/keys/auto-trial/bind is the lightweight operator-only bind (agent
    # asks its human for an email, no email sent); /redeem is the human 365d
    # conversion. We surface bind as the primary CTA so the agent can act.
    bind_url = "https://dchub.cloud/api/v1/keys/auto-trial/bind"
    redeem_url = "https://dchub.cloud/api/v1/keys/auto-trial/redeem"

    _exp_txt = f", expires {str(expires_at)[:10]}" if expires_at else ""
    _cap_txt = f" ({daily_calls}/day{_exp_txt})" if daily_calls else (
        f"{_exp_txt}" if _exp_txt else "")
    note = (
        f"🔑 Save this DC Hub key and reuse it across sessions — set "
        f"`X-API-Key: {surfaced_key}` in your MCP client config so you don't "
        f"re-provision next time{_cap_txt}. Have your human bind their email "
        f"(POST {bind_url} {{api_key, email}}) to keep it permanently."
    )
    return {
        # Machine-readable card for agents that parse structured output.
        "api_key": surfaced_key,
        "header": "X-API-Key",
        "expires_at": expires_at,
        "daily_calls": daily_calls,
        "bind_url": bind_url,
        "bind_body": {"api_key": surfaced_key, "email": "<your human's email>"},
        "redeem_url": redeem_url,
        "reuse_instruction": (
            "Persist this api_key in your MCP client config and send it as the "
            "X-API-Key header on every future call — including on later days — "
            "so you reuse the same durable key instead of re-provisioning."
        ),
        # One-line note for text-only relay agents that strip structured fields.
        "note": note,
    }


# Per-tool value teasers. Each entry: (one-line "what you'd unlock", optional
# data-shape hint). The CTA includes this so the MCP client can relay
# something concrete — "Atlanta industrial rates ~7-12¢/kWh, paywalled" is
# 100x more compelling than "tool errored, upgrade needed".
#
# Phase JJ (2026-05-13): added to lift conversion from 0.03% gate→pay. The
# previous _cta_gated text said "requires a Developer license" — pure jargon
# that AI clients couldn't turn into actionable text for the human user.
TOOL_TEASER = {
    "get_energy_prices":      "US retail rates span 6–25¢/kWh; this returns the exact ¢/kWh for your state with industrial/commercial/residential breakdown.",
    "get_market_intel":       "supply/demand MW, pricing per kW/mo, vacancy %, and absorption for 60+ global DC markets.",
    "get_grid_data":          "live ISO demand, peak, reserve margin, fuel mix, and interconnection queue for ERCOT/PJM/CAISO/etc.",
    "get_grid_intelligence":  "the same grid telemetry plus DC-specific scoring: interconnection-queue MW, headroom %, and renewable mix.",
    "get_grid_headroom":      "available transmission/substation headroom in MW around a lat/lon, with 50km radius constraint scoring.",
    "get_renewable_energy":   "solar + wind capacity layers with project-level MW, COD, and PPA prices.",
    "get_water_risk":         "WRI Aqueduct water-stress + drought + flood risk scores for any lat/lon, with utility-specific water rate.",
    "get_tax_incentives":     "state-level sales-tax abatements, property-tax exemptions, and incentive program ROI estimates.",
    "get_pipeline":           "540+ active DC projects globally — operator, capacity, status, ETA, preleased %.",
    "get_infrastructure":     "substations, transmission lines, gas pipelines, and power plants within a radius of a lat/lon coordinate (requires lat+lon; no place-name parameter).",
    "get_fiber_intel":        "3,200+ long-haul routes — carriers, route geometries, route classes incl. carrier-advertised dark corridors, IX presence; per-route lit capacity not tracked.",
    "list_transactions":      "1,400+ tracked DC M&A deals — buyer, seller, MW, $/kW, date, region.",
    "analyze_site":           "composite site-score for any lat/lon: power, fiber, water, tax, climate, latency to top markets.",
    "compare_sites":          "PRO multi-site ranker — side-by-side scoring across up to 5 candidate sites with weighted rankings. The killer broker workflow.",
    "get_lp_alerts":          "PRO Land+Power alerts — get notified when DCPI score, capacity, or pricing changes on saved sites.",
    "save_lp_site":           "PRO — save candidate L+P sites to a personal portfolio for tracking, alerts, and bulk export.",
    "lp_bulk_export":         "PRO bulk export — download all your saved L+P sites as CSV or GeoJSON for offline analysis.",
    "get_colocation_score":   "DCPI sub-score breakdown for any market — what's driving the rank.",
    "get_geothermal_potential":"DOE Play Fairway resource scores + estimated MWe for greenfield geothermal.",
    "get_microgrid_viability":"on-site solar/storage/CHP feasibility with NPV across utility-rate scenarios.",
    "get_intelligence_index": "DCPI index for 300+ markets — score, rank, weekly delta, top movers.",
    "get_backup_status":      "live backup/disaster-recovery telemetry for tracked facilities.",

    # Phase GG (2026-05-15) — Bundle 5A: teasers for the 25 new tools.
    # Each teaser is the value-proposition the client should relay when
    # gating fires. Concrete numbers beat jargon every time.
    "get_developer_brief":    "ranked site-selection shortlist with rationale per market — score = excess_power − constraint*0.5 − overshoot*5, +10 for BUILD verdict. One call replaces 6+ generic DCPI/grid lookups.",
    "get_buyer_brief":        "candidate facilities matching size + pocket-listing inventory + transaction comparables. Bundled output saves 4-5 individual calls.",
    "get_investor_brief":     "operator scorecard: footprint, pipeline contribution, M&A history, recent news, peer comparables. Auto-synthesized.",
    "get_policy_brief":       "state-level rollup for policymakers: installed base, pipeline pressure, grid stress, tax-incentive programs, jobs estimate.",
    "get_market_brief":       "one-call site-selection brief: DCPI + grid + power cost + tax incentives + same-ISO comparables.",
    "get_changes_since":      "cross-domain diff feed since a timestamp — new pipeline projects, news, DCPI re-scores, transactions, listings, facilities. Cache the response to skip re-pulls next session.",
    "get_site_capacity_report":"per-facility bundled view: metadata + capacity rollup + pipeline + DCPI verdict + peers + news in one call.",
    "get_iso_snapshot":       "comprehensive ISO snapshot: heartbeat + DCPI rollup + pipeline + facility footprint for any tracked ISO.",
    "get_iso_comparison":     "head-to-head across every tracked ISO ranked by avg excess-power — best opportunities first.",
    "get_pocket_listings":    "off-market data center sites curated by DC Hub — capacity, asking price, direct seller contact. Free tier sees public listings + teaser count of pocket inventory.",
    "get_pocket_listing":     "detailed pocket-listing view with full contact info and entitlement details.",
    "get_dcpi_scores":        "DCPI verdicts (BUILD/CAUTION/AVOID) + 4 numeric scores per market — DC Hub's headline build/avoid signal.",
    "get_dcpi_market":        "full DCPI snapshot for one market — verdict + scores + top risks + opportunities + queue wait.",
    "get_dcpi_movers":        "biggest DCPI movers over a window — emerging BUILD opportunities + newly-flagged AVOID markets with deltas.",
    "get_dcpi_iso":           "DCPI rolled to the ISO level — per-ISO BUILD/CAUTION/AVOID counts + avg scores.",
}


def _cta_gated(tool: str, current: Tier, required: Tier, args: Optional[Dict] = None) -> str:
    """Value-first upgrade message. Designed so MCP clients (Claude
    Desktop, Cursor, etc.) can relay a useful, conversion-friendly
    string to the human — not jargon like "license required".

    Phase JJ (2026-05-13): rewritten. Old text was 35 words of error-
    code framing; new text leads with the *specific value* and the
    *concrete price* so the human sees the deal, not the wall.

    Phase MM (2026-05-15) Bundle 9: branch by gap size. Anonymous→
    IDENTIFIED needs a "sign up free" CTA, NOT a $49/mo pricing link.
    The mismatch was a big driver of the 0.05% conversion rate —
    we were asking $49/mo for the FIRST step when the actual first
    step is just an email.
    """
    teaser = TOOL_TEASER.get(tool, f"premium intelligence from `{tool}`")
    price = TIER_PRICE.get(required, "see pricing")

    # Inline arg-context when we got it — turns "energy prices" into
    # "energy prices for GA" which the AI client can repeat verbatim.
    ctx = ""
    if args:
        # Only echo small, safe scalar args. No dumping nested payloads.
        bits = []
        for k in ("state", "iso", "market", "country", "city"):
            v = args.get(k)
            if v and isinstance(v, (str, int)) and len(str(v)) <= 32:
                bits.append(f"{k}={v}")
        if bits:
            ctx = f" (you asked for {', '.join(bits)})"

    # Bundle 9 — right-sized CTA per gap distance.
    # Phase RR (2026-05-15): lead with the agent-native programmatic path
    # (POST /api/v1/keys/claim → instant free dev key, no browser, no email)
    # since agents can't actually click links. Funnel data showed 12 free
    # keys claimed in 7d via keys/claim and 0 via the redeem URL — agents
    # strongly prefer programmatic. Only mention the human-signup form as
    # a fallback for cases where the agent is relaying to a human user.
    if required == Tier.IDENTIFIED and current == Tier.FREE:
        signup_url = (f"https://dchub.cloud/signup?next=/onboarding"
                      f"&utm_source=mcp&utm_tool={tool}")
        return (f"🔓 **{tool}**{ctx} returns: {teaser}\n\n"
                f"🤖 **AI agents — claim a free dev key in one API call:**\n"
                f"`POST https://dchub.cloud/api/v1/keys/claim` with body "
                f"`{{\"client_name\": \"<your agent name>\", \"email\": \"<optional — "
                f"for usage alerts + new-tool access>\"}}` → returns "
                f"`api_key` instantly (no browser, no card). "
                f"Then retry `{tool}` with header `X-API-Key: <key>`. "
                f"Free tier: 10 calls/day, full data on identified tools.\n\n"
                f"👤 **Human user signup (if you're relaying):** {signup_url} "
                f"— 30 seconds, email only, same 10 calls/day.")

    # Larger jumps (IDENTIFIED→DEV, DEV→PRO, etc.) keep the pricing path
    # Phase ZZZZ-T1-social-proof (2026-05-18): include the Stripe direct
    # link (1-click checkout, bypasses /pricing form-fill friction) and
    # concrete demand-side social proof ("100+ users hit this tool last
    # 30d"). MCP agents relay this verbatim — humans see "100+ users
    # like me already pay for this" rather than a generic upgrade ask.
    stripe_direct = "https://buy.stripe.com/14k14og7w7Zz9KJ8i6aZi02"  # $9/mo dev
    if required == Tier.PRO:
        stripe_direct = "https://buy.stripe.com/00w28o7BqaXLeP31QIaZi04"  # $199/mo pro
    url = (f"{PRICING_URL}?utm_source=mcp&utm_tool={tool}"
           f"{('&utm_term=' + str(args.get('state') or args.get('iso') or args.get('market'))) if args else ''}")
    # Per-tool social proof (real signal counts from /api/v1/mcp/funnel
    # — refreshed quarterly, not live, but defensible).
    _SOCIAL_PROOF = {
        "get_market_intel":      "3,500+ paywall hits / 30d — your peers all want this",
        "get_grid_intelligence": "100+ unique users hit this monthly — likely a Developer-plan user nearby",
        "get_fiber_intel":       "98+ unique users on this tool last 30d",
        "analyze_site":          "20+ teams using this for site selection",
        "compare_sites":         "11+ broker teams using PRO for site bake-offs",
        "list_transactions":     "1,400+ tracked M&A deals — Pitchbook charges $30K/yr for less",
    }
    proof = _SOCIAL_PROOF.get(tool, "")
    proof_line = f"\n📊 {proof}" if proof else ""

    return (f"🔓 **{tool}**{ctx} returns: {teaser}{proof_line}\n\n"
            f"💳 **One-click upgrade:** {stripe_direct} "
            f"({TIER_NAME[required]} · {price})\n"
            f"Or compare plans: {url}\n"
            f"Currently on {TIER_NAME[current]}.")


def _cta_truncated(shown: int, total: int) -> str:
    return (f"📊 Showing {shown} of {total} results (Free tier). "
            f"Upgrade for full access → {PRICING_URL}?utm_source=mcp&utm_medium=truncate")

def _cta_redacted(tool: str) -> str:
    return (f"🔑 Some fields redacted on Free tier. "
            f"Full data with Developer license → {PRICING_URL}?utm_source=mcp&utm_tool={tool}")


# Phase ZZZZ-savings (2026-05-18): per-tool "what you'd save vs the
# alternative." This is the FOMO-concrete claim the user asked for —
# "saves them a ton of time and money and using multiple sites."
# Each entry: (alternative source, hours-saved-per-call, $-equivalent-saved).
# Numbers are deliberately conservative + defensible.
_SAVINGS_CLAIMS = {
    "get_market_intel":      ("Compiling from CBRE/JLL/DCD reports (quarterly PDFs)",
                              2, "$25K/yr CBRE seat = ~$70/report"),
    "get_grid_data":         ("Cross-referencing ISO websites + EIA + manual extraction",
                              1, "$0 direct, ~$150/hr analyst time"),
    "get_grid_intelligence": ("ISO + EIA + interconnection queue manual scrape",
                              2, "~$300/hr senior energy analyst"),
    "get_water_risk":        ("WRI Aqueduct portal + local utility lookups per-site",
                              1, "free data, manual = 1hr per site"),
    "get_energy_prices":     ("EIA state datasets + utility tariff portals",
                              0.5, "EIA monthly = free, current rates often gated"),
    "get_renewable_energy":  ("EIA-860 + state RPS reports + IRA project lookups",
                              2, "all free, but discovery = 2-3hrs"),
    "get_tax_incentives":    ("State commerce dept + property-tax abatement DBs",
                              3, "varies by state, ~$2-5K/site for consultants"),
    "get_pipeline":          ("DCD news scraping + manual project tracking",
                              4, "$25K/yr DC Hawk + ongoing labor"),
    "list_transactions":     ("Pitchbook ($30K/yr) or 451 Research subscription",
                              0.25, "$30K/yr Pitchbook, deal-only data"),
    "get_infrastructure":    ("HIFLD shapefiles + GIS analysis per location",
                              3, "free data, ~$200/hr GIS analyst"),
    "get_fiber_intel":       ("Carrier maps (private) + manual route research",
                              4, "$50K/yr commercial fiber DB"),
    "analyze_site":          ("Multi-vendor site assessment (CBRE + Cushman + JLL)",
                              40, "$15-50K per site assessment"),
    "compare_sites":         ("Brokered site shortlist + bake-off analysis",
                              80, "$30-80K per multi-site comparison"),
    "get_dcpi_scores":       ("CBRE H1/H2 reports + DCHawk quarterlies",
                              3, "$25K/yr DC Hawk seat"),
    "search_facilities":     ("DC Knowledge directory + manual operator websites",
                              1, "free data, ~$150/hr researcher time"),
    "get_news":              ("Google News + DCD + DataCenterDynamics manual filter",
                              0.5, "free, ~$75/hr per news sweep"),
}


def _value_unlock_block(tool_name: str, tier: Tier, max_rows: int,
                         rows_visible: int, rows_total: int) -> dict:
    """Build the upgrade-incentive metadata block attached to every
    response. The goal: make each successful free response include a
    CONCRETE upgrade case (what they'd save, how much, what they're not
    seeing) so even users who didn't hit the gate see the value of paying.
    """
    block: dict = {}
    teaser = TOOL_TEASER.get(tool_name)
    savings = _SAVINGS_CLAIMS.get(tool_name)

    # Tier-aware framing
    if tier == Tier.FREE:
        block["showing_tier"] = "FREE (teaser — 1 row max)"
        block["recommended_tier"] = "DEVELOPER ($49/mo) for full data"
        if rows_total > rows_visible:
            block["rows_hidden"] = rows_total - rows_visible
            block["unlock_hint"] = (f"{rows_total - rows_visible} more rows hidden. "
                                     f"All visible with free signup ({PRICING_URL}?utm_source=mcp&utm_tool={tool_name}).")
    elif tier == Tier.IDENTIFIED:
        block["showing_tier"] = "FREE-IDENTIFIED (5 rows)"
        block["recommended_tier"] = "DEVELOPER ($49/mo) for 100 rows + composite analyses"
        if rows_total > rows_visible:
            block["rows_hidden"] = rows_total - rows_visible
            block["unlock_hint"] = (f"{rows_total - rows_visible} additional rows on Developer plan. "
                                     f"100 rows/call + analyze_site + compare_sites unlocked at $49/mo.")
    elif tier == Tier.DEVELOPER:
        block["showing_tier"] = "DEVELOPER ($49/mo)"
        # Price read from tier_registry — the hardcoded "$199/mo" here was
        # two repricings stale (canonical Pro is $299 since r-reprice).
        block["recommended_tier"] = (
            f"PRO ({_canonical_price('pro', '$299/mo')}) for multi-site + alerts")

    if teaser:
        block["full_value"] = teaser
    if savings:
        alt_source, hrs_saved, dollar_equiv = savings
        block["alternative_source"] = alt_source
        block["hours_saved_vs_manual"] = hrs_saved
        block["dollar_equivalent"] = dollar_equiv
        block["savings_pitch"] = (
            f"This call replaces ~{hrs_saved}h of work from {alt_source}. "
            f"At analyst rates that's ~${int(hrs_saved * 150)} per call; "
            f"Developer plan is {_canonical_price('developer', '$49/mo')} for "
            f"{_canonical_monthly('developer'):,} calls/month.")

    block["upgrade_url"] = (f"{PRICING_URL}?utm_source=mcp&utm_medium=value-unlock"
                            f"&utm_tool={tool_name}")
    return block


# ═══════════════════════════════════════════════════════════════
# MAIN API: _gate() and _finalize()
# ═══════════════════════════════════════════════════════════════

def _gate(tool_name: str, api_key: Optional[str] = None,
          args: Optional[Dict] = None) -> Optional[str]:
    """
    Call at the TOP of every @mcp.tool handler.
    Returns JSON error string if blocked, None if access granted.

    Phase JJ (2026-05-13): now accepts the tool's call args so the
    blocked-response can echo "state=GA" / "market=atlanta" / "iso=PJM"
    back to the caller — gives MCP clients (Claude Desktop, Cursor)
    something concrete to relay to the human, dramatically lifting
    the gate→upgrade conversion rate.

    Tool handlers can opt-in by passing locals() or a kwargs dict;
    calling without args still works (backward compatible).

    Usage:
        block = _gate("list_transactions", api_key)            # legacy
        block = _gate("get_energy_prices", api_key, locals())   # better
        if block: return block
    """
    # NO-LEAK boundary: drain any grace_meta left on this thread by a PRIOR
    # request that set it but never reached _finalize (e.g. a tool handler that
    # raised after the gate passed). _gate runs at the TOP of every gated tool
    # call, so clearing here guarantees the grace _finalize later reads was
    # minted during THIS request only — a stale caller's auto-trial key can
    # never be surfaced to the next caller on a reused thread.
    get_pending_grace_meta()
    tier = resolve_tier(api_key)
    required = TOOL_TIER.get(tool_name, Tier.DEVELOPER)

    # Tier check
    if tier < required:
        # Phase EEEEE (2026-05-16): ANON GRACE MODE — never bounce an
        # anonymous caller off an IDENTIFIED tool for their first 5
        # calls in 24h. Volume recovery move — the XXX tightening cost
        # ~38% of weekly inquiries. Returns None (gate passes) and
        # silently mints a trial key + asks the caller's response to
        # carry it in metadata via _grant_grace_response_meta().
        if tier == Tier.FREE and required == Tier.IDENTIFIED:
            try:
                from routes.anon_grace import grace_remaining, consume_grace
                from routes.auto_trial import mint_trial_for_request
                from flask import request as _flask_req
                if grace_remaining(_flask_req) > 0:
                    # Mint a trial key the agent can use on follow-ups
                    trial = mint_trial_for_request(_flask_req, tool_name)
                    trial_key = trial.get("api_key") if trial.get("ok") else None
                    if consume_grace(_flask_req, tool_name, trial_key):
                        # Stash trial key on a module-level so the tool
                        # handler can attach it to its response metadata.
                        # The handler reads via get_pending_grace_meta().
                        # Stash the HONEST minted terms (daily_calls / expiry
                        # straight off the mint result), NOT an inflated
                        # constant. The first-call key surface reads these to
                        # tell the agent the real cap + expiry — overpromising
                        # (the old hardcoded 200/day) is exactly the trust break
                        # that fed the retention leak when the cap actually bit.
                        _ok = bool(trial.get("ok"))
                        _daily = trial.get("daily_calls") if _ok else None
                        _exp = trial.get("expires_at") if _ok else None
                        _set_pending_grace_meta({
                            "anon_grace_used":              True,
                            "anon_grace_calls_remaining":   grace_remaining(_flask_req),
                            "anon_grace_cap":               5,
                            "auto_trial_key":               trial_key,
                            "auto_trial_daily_calls":       _daily,
                            "auto_trial_expires_at":        _exp,
                            "promotion_note": (
                                "You're using anon grace. After this, a trial key "
                                "auto-mints (or claim a permanent free key now at "
                                "POST /api/v1/keys/claim)."
                            ),
                        })
                        return None  # PASS — gate doesn't fire
            except Exception:
                pass  # any grace failure → fall through to paywall

        # Phase RRR-funnel-transparent-retry (2026-05-18) — Option A from
        # the auto-trial-funnel diagnosis. The funnel observability
        # endpoint revealed: 1,581 paywall signals → only 3 trial keys
        # minted (per-caller 24h dedup) → 1 used → 0 conversions. The
        # mint happens but agents don't extract+retry with the JSON-
        # delivered key. So: for the top-demand READ-ONLY tools, if the
        # caller already has an active trial key from an earlier mint
        # (within 24h, not yet expired), TRANSPARENTLY pass the gate
        # without making them retry. The 200/day cap on trial keys is
        # the natural rate control. Conservative whitelist below — only
        # tools with high concentrated paywall demand + zero side effects.
        # Phase ZZZZ-T1-paywall-visibility (2026-05-18): the previous
        # AUTO_RETRY set transparently passed the gate for 5 hot tools,
        # giving agents free access without ever seeing the paywall.
        # Result: 15,158 signals → 0 conversions across all platforms
        # over 90 days. We're removing all 5 from auto-retry; agents
        # will now SEE the gated response + Stripe-direct CTA + can
        # mint a trial (7d/50-cap) explicitly if they choose.
        _AUTO_RETRY_TOOLS = set()  # explicit empty — no transparent passes
        if (tier == Tier.FREE and required == Tier.IDENTIFIED
                and tool_name in _AUTO_RETRY_TOOLS):
            try:
                from routes.auto_trial import mint_trial_for_request
                from flask import request as _flask_req
                trial = mint_trial_for_request(_flask_req, tool_name)
                if trial.get("ok") and trial.get("reused"):
                    # Caller had a key minted earlier and it's still
                    # valid. Pass the gate transparently — tool returns
                    # data, agent never sees a paywall. Trial-meta will
                    # be attached by finalize() via _set_pending_grace_meta.
                    _set_pending_grace_meta({
                        "auto_trial_active":         True,
                        "auto_trial_key":            trial.get("api_key"),
                        "auto_trial_expires_at":     trial.get("expires_at"),
                        "auto_trial_daily_calls":    200,
                        "promotion_note": (
                            f"You're being transparently authenticated via an "
                            f"active trial key for `{tool_name}`. To track "
                            f"usage + persist to your account, pass header "
                            f"`X-API-Key: {trial.get('api_key')}` on future "
                            f"calls (or POST /api/v1/keys/auto-trial/redeem "
                            f"with the key + your email)."
                        ),
                    })
                    return None  # PASS — tool runs, real data returned
            except Exception:
                pass  # fall through to paywall on any error

        teaser = TOOL_TEASER.get(tool_name)
        price = TIER_PRICE.get(required, "")
        # Phase DDDDD (2026-05-16): if FREE caller hits IDENTIFIED gate,
        # AUTO-MINT a working trial key INLINE so the agent can retry
        # immediately. Removes the "claim a key first" friction step
        # that's been blocking 99.92% of conversions.
        auto_trial = None
        if tier == Tier.FREE and required == Tier.IDENTIFIED:
            try:
                from routes.auto_trial import mint_trial_for_request
                from flask import request as _flask_req
                trial = mint_trial_for_request(_flask_req, tool_name)
                if trial.get("ok"):
                    auto_trial = trial
            except Exception:
                pass
        # Phase RRR-revenue3 (2026-05-18) — direct Stripe Payment Link per
        # required tier. Old upgrade_url just sent users to /pricing,
        # which gates Stripe checkout behind a sign-up wall. With 6,653
        # MCP signals → 1 conversion (0.015%), the friction was killing
        # the funnel. Direct Stripe Payment Link cuts /pricing out and
        # lets agents (or the human they're embedded in) check out with
        # zero account creation. The URLs derive from routes/_stripe_links.py
        # (canon) via _canonical_link — the pre-reprice Pro literal this dict
        # carried until 2026-08-21 sold Pro at the retired price (SH52-103).
        _STRIPE_BUY_NOW = {k: _canonical_link(k)
                           for k in ("starter", "developer", "pro", "enterprise")}
        _required_name = TIER_NAME[required].lower()
        _buy_now_url = _STRIPE_BUY_NOW.get(_required_name)

        # Phase FF+8-funnel (2026-05-19) — THE conversion-killer fix.
        # Mint a pair-code for this api_key and attach it to the Stripe
        # Payment Link as `client_reference_id`. Without this, paid
        # checkouts came back to the webhook with NO attribution and the
        # api_key was never upgraded — 5,410 signals → 6 conversions
        # was largely this bug. Stripe Payment Links pass
        # client_reference_id through to checkout.session.completed,
        # and main.py:8373 already redeems DCM-XXXX → flips tier.
        _ref_code = None
        if api_key and _buy_now_url:
            try:
                from routes.pair_code import get_or_create_code
                _r = get_or_create_code(api_key, tool_name=tool_name)
                if _r and _r.get("code"):
                    _ref_code = _r["code"]
            except Exception:
                pass  # never let attribution breakage block paywall response

        def _attribute(url):
            if not url: return None
            sep = "&" if "?" in url else "?"
            attribution = f"{sep}utm_source=mcp&utm_tool={tool_name}"
            if _ref_code:
                attribution += f"&client_reference_id={_ref_code}"
            return url + attribution

        payload = {
            "success": False,
            "error": "upgrade_required",
            "message": _cta_gated(tool_name, tier, required, args=args),
            "current_tier": TIER_NAME[tier],
            "required_tier": TIER_NAME[required],
            "required_tier_price": price,
            # Machine-readable teaser so MCP clients can render a card
            # instead of just dumping the message. Surfaced separately
            # from the message so structured renderers don't have to
            # parse natural language.
            "teaser": teaser,
            "echo_args": _safe_echo_args(args),
            "upgrade_url": f"{PRICING_URL}?utm_source=mcp&utm_tool={tool_name}",
            # Phase RRR-revenue3: direct Stripe Payment Link — one click
            # to checkout, no /pricing landing, no sign-up wall.
            # Phase FF+8-funnel: now carries pair-code as
            # client_reference_id so the webhook can auto-upgrade the
            # api_key when the user pays. Lifts conversion attribution
            # from "best-effort email match" → deterministic.
            "buy_now_url": _attribute(_buy_now_url),
            "buy_now_price": price,
            "buy_now_note": "Direct Stripe checkout — no signup required",
            # 2026-06-25 (owner): the one-time credit pack is the LEAD option for
            # agents — monthly seats don't fit agent traffic. $10 one-time = 1,000
            # API calls, no subscription. Same attribution path.
            "usage_url": _attribute("https://buy.stripe.com/9B69AU08y2FfbSR55UaZi0i"),
            "usage_price": "$10 one-time = 1,000 API calls",
            "usage_note": ("🤖 $10 one-time = 1,000 API calls, no subscription. "
                           "Best fit for a high-volume agent; we email "
                           "your API key right after checkout."),
            # Phase ZZ+1 (2026-05-15): structured claim-endpoint card so
            # agents that parse the response programmatically (Cursor,
            # Claude Code, Cline) can render a "claim free key" button
            # without parsing natural language. Growth audit identified
            # this as the highest-leverage MCP discovery win — the
            # /api/v1/keys/claim endpoint was buried in the message text
            # and most agents never saw it.
            "claim_endpoint": {
                "method": "POST",
                "url":    "https://dchub.cloud/api/v1/keys/claim",
                "body":   {"client_name": "<your agent name>"},
                "header": {"Content-Type": "application/json"},
                "returns": "{api_key, tier, daily_calls}",
                "note": ("Claim a free dev key instantly (no email, no "
                          "browser). Use the returned api_key as X-API-Key "
                          "header on subsequent calls — lifts daily cap "
                          "to 100, unlocks tier-FREE tools."),
            },
        }
        # Phase FF+16-emailcapture (2026-05-19) — every paywall response
        # now surfaces 3 email-capture paths (notify, auto-trial-w-email,
        # checkout-start). Closes the gap where 99.98% of MCP signals
        # were anonymous and unreachable. The /notify page lets a human
        # opt in to limit-reset notifications; /checkout/start captures
        # email BEFORE Stripe so abandons are still addressable.
        try:
            from routes.email_capture import build_email_capture_urls
            _ec = build_email_capture_urls(
                tool=tool_name, api_key=api_key,
                tier=_required_name, client_reference_id=_ref_code,
            )
            payload["notify_url"] = _ec["notify_url"]
            payload["checkout_start_url"] = _ec["checkout_start_url"]
            payload["auto_trial_with_email_url"] = _ec["auto_trial_with_email_url"]
            # Override buy_now_url to route through checkout/start so we
            # capture email BEFORE the Stripe redirect even when the
            # agent renders only the buy_now CTA.
            if _buy_now_url:
                payload["buy_now_url_direct_stripe"] = payload.get("buy_now_url")
                payload["buy_now_url"] = _ec["checkout_start_url"]
            # Phase FF+16-emailcapture-v2 — also weave the URLs into the
            # natural-language message so text-only relay agents
            # (which strip structured fields when proxying back to the
            # human) still surface them. Without this, MCP servers that
            # only echo `message` to their UI never show the URLs.
            if isinstance(payload.get("message"), str):
                payload["message"] = payload["message"] + (
                    "\n\n💌 Not ready to pay? Drop your email for limit-reset "
                    "notifications: " + _ec["notify_url"] + "\n"
                    "💳 Ready to upgrade? One-click checkout (your API key "
                    "auto-upgrades on payment): " + _ec["checkout_start_url"]
                )
        except Exception:
            pass  # if email_capture fails to import, paywall still works

        # Marketing opt-in soft-CTA (ships DARK behind OPTIN_CTA_ENABLED).
        # Additive only: a structured `optin_cta` card + an appended note —
        # it NEVER replaces or mutates the tool's real data. Shown ONLY to
        # FREE callers hitting a high-usage paid tool (get_grid_intelligence /
        # get_fiber_intel), to target the actual upgrade-outreach pool. Points
        # at the opt-in REQUEST flow (double-opt-in confirm sets consent on the
        # tokenized click — nothing is sent or set here). See _optin_cta_block.
        try:
            _optin = _optin_cta_block(tool_name, tier, api_key)
            # Don't RE-prompt a suppressed (previously-unsubscribed) user — check
            # only when the CTA would actually show (flag on + FREE + target tool),
            # so it adds no query to the common path. Fail-safe: skip on error.
            if _optin and _optin_recipient_suppressed(api_key):
                _optin = None
            if _optin:
                payload["optin_cta"] = _optin
                # Weave the soft ask into the natural-language message too, so
                # text-only relay agents (which strip structured fields) still
                # surface it. Appended AFTER the upgrade paths — easy to ignore.
                if isinstance(payload.get("message"), str):
                    payload["message"] = payload["message"] + "\n\n" + _optin["optin_note"]
        except Exception:
            pass  # opt-in CTA is best-effort; never block the paywall response
        # Phase DDDDD: inject auto-minted trial key INLINE if we got one.
        # This is THE conversion-killer move — agent gets a working key
        # in the same response, retries with X-API-Key header, succeeds.
        # No human signup step required.
        if auto_trial:
            _tk = auto_trial.get("api_key")
            # Use the REAL minted terms. The block used to hardcode an
            # inflated cap/expiry, but the trial is actually
            # TRIAL_DAILY_UNBOUND/day (15) for TRIAL_DAYS (7), 50/day once
            # the operator email is bound. Overpromising broke trust the
            # moment the cap/expiry bit, which is exactly the retention leak.
            _cap = auto_trial.get("daily_calls", 15)
            _days = auto_trial.get("days_remaining") or auto_trial.get("trial_days")
            _exp = auto_trial.get("expires_at")
            payload["auto_trial_key"] = _tk
            payload["auto_trial_expires_at"] = _exp
            payload["auto_trial_daily_calls"] = _cap
            # Machine-readable retry instruction so programmatic agents
            # (Cursor / Cline / Claude Code) retry WITHOUT parsing prose —
            # the documented gap behind "key minted but never used".
            payload["retry_with"] = {
                "header": "X-API-Key",
                "value":  _tk,
                "note":   "Re-send the exact same call with this header to get real data.",
            }
            # Override the message to LEAD with the working key + accurate terms.
            _exp_txt = (f", expires {str(_exp)[:10]}" if _exp
                        else (f", {_days}-day trial" if _days else ""))
            _bind_cta = (auto_trial.get("upgrade_cta") or "").strip()
            payload["message"] = (
                f"✨ Auto-trial key minted for you: `{_tk}` "
                f"({_cap} calls/day{_exp_txt}).\n\n"
                f"**Retry your call with the header `X-API-Key: {_tk}`** — "
                f"it will succeed.\n\n"
                f"Tool returns: {teaser or 'data'}"
                + (f"\n\n{_bind_cta}" if _bind_cta else "")
            )
            # The auto-trial branch fully REPLACED the message above, which
            # would drop the opt-in note woven in earlier. Re-append it (still
            # additive, still gated — payload["optin_cta"] only exists when the
            # flag is on + FREE + high-usage tool) so text-only relay agents
            # keep seeing the soft ask.
            _optin = payload.get("optin_cta")
            if _optin and isinstance(payload.get("message"), str):
                payload["message"] = payload["message"] + "\n\n" + _optin["optin_note"]
            # Suppress the now-redundant claim_endpoint to keep payload tight
            payload.pop("claim_endpoint", None)
        return json.dumps(payload)

    # Rate limit check
    rl_key = api_key or "anon"
    msg = _rl.check(rl_key, tier)
    if msg:
        return json.dumps({
            "success": False,
            "error": "rate_limited",
            "message": msg,
            "current_tier": TIER_NAME[tier],
            "upgrade_url": f"{PRICING_URL}?utm_source=mcp&utm_medium=ratelimit",
            # Phase ZZ+1: same agent-native claim CTA for rate-limit
            # responses. The most common case: anonymous caller hits the
            # FREE-tier daily cap → response now includes the structured
            # path to claim a higher-cap key.
            "claim_endpoint": {
                "method": "POST",
                "url":    "https://dchub.cloud/api/v1/keys/claim",
                "body":   {"client_name": "<your agent name>"},
                "returns": "{api_key, tier, daily_calls}",
                "note": ("Anonymous calls share a 50/day cap. Claim a free "
                          "dev key to lift to 100/day with no email. Verify "
                          "your email at https://dchub.cloud/signup for 200/day."),
            },
        })

    return None  # Access granted


def _safe_echo_args(args: Optional[Dict]) -> Dict:
    """Strip out non-scalar / oversized args so we can safely echo a
    summary of what the caller asked for back to them."""
    if not args or not isinstance(args, dict):
        return {}
    keep = {}
    for k in ("state", "iso", "market", "country", "city", "data_type",
             "lat", "lon", "radius_km", "limit", "slug"):
        v = args.get(k)
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)) and len(str(v)) <= 64:
            keep[k] = v
    return keep


def _finalize(result_json: str, tool_name: str, api_key: Optional[str] = None) -> str:
    """
    Call at the BOTTOM of every @mcp.tool handler, wrapping the return.
    Truncates arrays, redacts premium fields, adds usage/CTA metadata.

    Usage:
        return _finalize(json.dumps(result), "list_transactions", api_key)
    """
    tier = resolve_tier(api_key)

    # Parse
    try:
        data = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        return result_json  # Can't process, return as-is

    if not isinstance(data, dict):
        return result_json

    max_rows = LIMITS[tier]["max_rows"]

    # PATCH 2026-04-24 (jm): P0 — pre-populate `_meta` BEFORE iterating data,
    # and snapshot `data.keys()` into a list so we never mutate the dict mid-
    # iteration. The old code raised `RuntimeError: dictionary changed size
    # during iteration` the first time a response needed truncation, because
    # `data["_meta"] = {}` was being added inside the `for key, val in
    # data.items():` loop. This is what made every search_facilities call
    # with default limit=25 crash for free-tier callers (max_rows=5).
    if "_meta" not in data:
        data["_meta"] = {}

    # ── Truncate arrays ──
    for key in list(data.keys()):
        if key.startswith("_"):
            continue
        val = data[key]
        if isinstance(val, list) and len(val) > max_rows:
            total = len(val)
            data[key] = val[:max_rows]
            data["_meta"]["truncated"] = True
            data["_meta"]["showing"] = max_rows
            data["_meta"]["total_available"] = total
            data["_meta"]["upgrade"] = _cta_truncated(max_rows, total)

    # ── Redact premium fields on free tier ──
    # PATCH 2026-04-24 (jm): snapshot keys with list() for the same dict-
    # iteration safety as the truncation loop above. `_meta` is guaranteed
    # to exist already (populated at the top of _finalize).
    if tier < Tier.DEVELOPER:
        fields = REDACT_FIELDS.get(tool_name, [])
        if fields:
            redacted = False
            for key in list(data.keys()):
                val = data[key]
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, dict):
                            for f in fields:
                                if f in item and item[f] is not None:
                                    item[f] = "🔒 Upgrade to Developer"
                                    redacted = True
            if redacted:
                data["_meta"]["fields_redacted"] = fields
                data["_meta"]["redact_notice"] = _cta_redacted(tool_name)

    # ── Add usage footer ──
    # _meta is guaranteed to exist from the top of _finalize (PATCH 2026-04-24).
    rl_key = api_key or "anon"
    usage = _rl.usage(rl_key)
    lim = LIMITS[tier]
    data["_meta"]["tier"] = TIER_NAME[tier]
    data["_meta"]["usage"] = {
        "calls_today": usage["today"],
        "daily_limit": lim["day"],
        "remaining": max(0, lim["day"] - usage["today"]),
    }
    if tier == Tier.FREE:
        data["_meta"]["upgrade_url"] = f"{PRICING_URL}?utm_source=mcp"

    # Phase ZZZZ-savings (2026-05-18): attach the value_unlock block on
    # EVERY response (not just gated/truncated ones). This is the user's
    # "sell them on dev/pro that saves them time + money + multiple sites"
    # ask. Conservative — only for tiers below PRO + only when we have
    # a savings claim for the tool.
    if tier < Tier.PRO and tool_name in _SAVINGS_CLAIMS:
        rows_visible = 0
        rows_total = 0
        # Sum across all top-level arrays for the visible/total accounting
        for key in data.keys():
            if key.startswith("_"): continue
            val = data[key]
            if isinstance(val, list):
                rows_visible += len(val)
        rows_total = data.get("_meta", {}).get("total_available", rows_visible)
        data["_meta"]["value_unlock"] = _value_unlock_block(
            tool_name, tier, max_rows, rows_visible, rows_total)

    # ── First-call durable-key surface (ships DARK behind
    #    FIRST_CALL_KEY_SURFACE_ENABLED). RETENTION fix: on the FIRST successful
    #    call for a caller with NO durable bound key (i.e. THIS request's
    #    anon-grace minted/reused an auto-trial key for them), additively surface
    #    that OWN key + a "save it, reuse across sessions, bind your email"
    #    note so the agent persists it and the human can return tomorrow without
    #    re-provisioning. Additive (separate _meta field + appended note),
    #    best-effort (never block the response), first-call only (suppressed once
    #    the caller arrives on their own durable/bound key — no grace_meta), and
    #    NO-LEAK (key comes only from this request's own grace_meta).
    #    get_pending_grace_meta() is read-once + auto-clears; nothing else reads
    #    it, so consuming here is the canonical surface point. ALWAYS drain it so
    #    a flag-off request doesn't leak grace state into the next request on
    #    this thread.
    try:
        _grace = get_pending_grace_meta()
        _fck = _first_call_key_surface_block(tool_name, tier, api_key, _grace)
        if _fck:
            data["_meta"]["first_call_key_offer"] = _fck
            # Weave the one-line note into the natural-language message too, so
            # text-only relay agents (which strip structured _meta fields) still
            # surface it. Appended additively — never replaces the data/message.
            _msg = data.get("message")
            if isinstance(_msg, str):
                data["message"] = _msg + "\n\n" + _fck["note"]
            else:
                # No prose message on this tool's payload — expose the note in a
                # dedicated additive field so it's still reachable, without
                # touching any data key.
                data["_meta"]["first_call_key_note"] = _fck["note"]
    except Exception:
        pass  # surface is best-effort; never block the successful response

    return json.dumps(data, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════
# STARLETTE MIDDLEWARE — Extract API key from HTTP headers
# ═══════════════════════════════════════════════════════════════
# The MCP streamable-http transport runs on Starlette/ASGI.
# This middleware extracts x-api-key from HTTP headers and stores
# it in a thread-local so _gate()/_finalize() can access it.

import threading
_request_api_key = threading.local()

def get_current_api_key() -> Optional[str]:
    """Get API key for the current request (set by ASGI middleware)."""
    return getattr(_request_api_key, "key", None)

def set_current_api_key(key: Optional[str]):
    """Set API key for current thread/context."""
    _request_api_key.key = key


class GatekeeperMiddleware:
    """
    ASGI middleware that extracts API key from x-api-key header
    and makes it available via get_current_api_key().

    Add to your Starlette app:
        from mcp_gatekeeper import GatekeeperMiddleware
        app = mcp.streamable_http_app()
        app = GatekeeperMiddleware(app)
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            # Headers are bytes in ASGI
            api_key = headers.get(b"x-api-key", b"").decode("utf-8", errors="ignore")
            if not api_key:
                # Also check query string for ?api_key=xxx
                qs = scope.get("query_string", b"").decode("utf-8", errors="ignore")
                for param in qs.split("&"):
                    if param.startswith("api_key="):
                        api_key = param[8:]
                        break
            set_current_api_key(api_key or None)
        await self.app(scope, receive, send)


# ═══════════════════════════════════════════════════════════════
# CONVENIENCE: gate + finalize using thread-local key
# ═══════════════════════════════════════════════════════════════

def gate(tool_name: str, args: Optional[Dict] = None) -> Optional[str]:
    """Gate check using the API key from the current HTTP request.

    Phase JJ (2026-05-13): optional `args` so each tool handler can pass
    locals() and unlock a far better upgrade message — e.g.
    `gate("get_energy_prices", {"state": state, "iso": iso})` so the
    upgrade CTA returned to free callers can echo their query
    ("you asked for state=GA"). Backwards-compatible: omitting args
    behaves exactly as before.
    """
    return _gate(tool_name, get_current_api_key(), args=args)

def finalize(result_json: str, tool_name: str) -> str:
    """Finalize response using the API key from the current HTTP request."""
    return _finalize(result_json, tool_name, get_current_api_key())


# ═══════════════════════════════════════════════════════════════
# DB TABLE CREATION (run once)
# ═══════════════════════════════════════════════════════════════

def init_db():
    """Verify api_keys table exists and add any missing columns for gatekeeper.
    Does NOT recreate the table — respects your existing schema.
    """
    try:
        import psycopg2
        url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
        if not url:
            logger.info("No DB URL — gatekeeper using env keys + prefix resolution only")
            return
        conn = psycopg2.connect(url, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            # Check table exists
            cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'api_keys')")
            exists = cur.fetchone()[0]
            if not exists:
                logger.info("api_keys table not found — gatekeeper using env keys + prefix resolution")
                conn.close()
                return
            # Add plan column if missing (used by gatekeeper for tier resolution)
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'api_keys' AND column_name = 'plan'
                )
            """)
            has_plan = cur.fetchone()[0]
            if not has_plan:
                cur.execute("ALTER TABLE api_keys ADD COLUMN plan VARCHAR(30) DEFAULT 'free'")
                logger.info("Added 'plan' column to api_keys")
        conn.close()
        logger.info("✅ api_keys table verified")
    except Exception as e:
        logger.warning(f"⚠️ api_keys check: {e}")
