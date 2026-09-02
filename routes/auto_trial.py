"""Phase DDDDD (2026-05-16) — auto-mint trial keys to kill paywall friction.

User diagnosis: 7,839 paywall signals over 7 days → 6 conversions
over 30 days. That's **0.08%** conversion. 100+ distinct users
hammering `get_grid_intelligence` + `get_fiber_intel` and bouncing
off the paywall instead of claiming a key.

Root cause: the paywall response TELLS agents to POST to
/api/v1/keys/claim, but most agents either don't parse the JSON,
or relay the natural-language message to a human who walks away.

The fix: **mint a working IDENTIFIED-tier trial key INLINE in the
paywall response.** Agent gets the key in the same response,
retries with X-API-Key header, succeeds. Conversion happens
WITHOUT a human signup step.

Auto-trial keys:
  - Prefix: `dch_trial_`
  - Resolved by mcp_gatekeeper as IDENTIFIED tier
  - 200 calls/day cap (same as IDENTIFIED) — abuse risk is bounded
  - 30-day expiry; agent can convert to permanent via
    POST /api/v1/keys/auto-trial/redeem {email}
  - Tracked in auto_trial_keys table for funnel attribution

  POST /api/v1/keys/auto-mint              admin or called inline by gatekeeper
  POST /api/v1/keys/auto-trial/redeem      bind trial key to email (one-click conv)
  GET  /api/v1/keys/auto-trial/stats       public funnel metrics

Brain detector check_auto_trial_conversion_rate fires if <20% of
trial keys → real signups within 7 days. Tracks the fix's impact.
"""

from __future__ import annotations

import os
import secrets
import datetime
import hashlib
from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

import logging
log = logging.getLogger("auto_trial")


auto_trial_bp = Blueprint("auto_trial", __name__)


_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


# Phase ZZZZ-trial-tighten (2026-05-18): trial config in one place.
# Brain narrative + funnel showed: 15,104 paywall signals → 0 conversions
# because the trial gave 200 calls/day for 30 days — long + generous
# enough that agents never had to upgrade.
# v2: 7-day expiry + 50 calls/day. Forces renewal/upgrade decision quicker.
TRIAL_DAYS         = 7
TRIAL_DAILY_CALLS  = 50
# 2026-06-08 conversion lever (founder-set: Conservative). Unbound trials (no
# operator/signed-up email) get a small daily taste; binding the operator's email
# unlocks the full TRIAL_DAILY_CALLS. Diagnosis: 894 trials minted → 143 activated
# → 0 IDENTIFIED, because a full week of 50/day with no email meant agents never
# had to involve their human (= no lead = nothing to convert). 15/day is generous
# enough to protect the "agents reach for DC Hub" moat while making heavy
# power-users (the real leads) bind. Tune via env without a deploy.
TRIAL_DAILY_UNBOUND = int(os.environ.get("TRIAL_DAILY_CALLS_UNBOUND", "15"))
# 2026-06-28 forcing function — EMAIL-GATE THE TAIL (founder-set). After this many
# CUMULATIVE calls, an UNBOUND trial must bind an operator/signed-up email to keep
# full-data access. The first N calls stay free + frictionless (protects the "agents
# reach for DC Hub" taste); past N the call drops to FREE tier → the agent sees the
# paywall + bind CTA. Bound trials (signed_up_email/operator_email) are unaffected.
# WHY: the soft-nudge lever (DCHUB_HIGH_INTENT_THRESHOLD) was already maxed at 1 and
# produced 0 binds / 0 upgrades over 1,885 trials (329 active, 131 power-users) —
# the free taste never ran out, so nothing forced a bind. This is the only lever
# with teeth. Tunable WITHOUT a deploy via TRIAL_FREE_CALLS_UNBOUND; set very high
# (e.g. 99999) to effectively disable the gate and revert to pure soft-nudge.
TRIAL_FREE_CALLS_UNBOUND = int(os.environ.get("TRIAL_FREE_CALLS_UNBOUND", "10"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS auto_trial_keys (
    api_key          TEXT PRIMARY KEY,
    minted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at       TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '7 days'),
    minted_for_tool  TEXT,
    request_ip_hash  TEXT,
    request_ua       TEXT,
    last_used_at     TIMESTAMPTZ,
    call_count       INT NOT NULL DEFAULT 0,
    daily_count      INT NOT NULL DEFAULT 0,
    daily_date       DATE,
    signed_up_email  TEXT,
    operator_email   TEXT,
    operator_name    TEXT,
    client_name      TEXT,
    upgraded_tier    TEXT,
    notes            TEXT
);
CREATE INDEX IF NOT EXISTS ix_auto_trial_ip ON auto_trial_keys(request_ip_hash);
CREATE INDEX IF NOT EXISTS ix_auto_trial_signedup ON auto_trial_keys(signed_up_email)
    WHERE signed_up_email IS NOT NULL;
"""


# ── r-ddl-once (2026-08-31) ──────────────────────────────────────────
# WHY THIS FLAG EXISTS — a measured production stall, not a theory.
#
# _ensure_schema was called on EVERY request through four paths, and it issues
# one CREATE TABLE IF NOT EXISTS plus five ALTER TABLE ... ADD COLUMN IF NOT
# EXISTS. All of those are no-ops after the first run, but a no-op ALTER still
# REQUESTS ACCESS EXCLUSIVE. Observed live at 2026-08-31 09:5x UTC:
#
#   pid 29441  COPY public.fiber_kmz_routes_old_0822 ...      549s   (the dump)
#   pid 30160  ALTER TABLE auto_trial_keys ADD COLUMN ...     268s   waiting
#   pid 30760  CREATE TABLE IF NOT EXISTS auto_trial_keys       7s   waiting on 30160
#   -> 17 of 20 active backends blocked
#
# The chain: a pg_dump holds ACCESS SHARE on every table for the whole dump.
# A no-op ALTER then queues for ACCESS EXCLUSIVE behind it — and in PostgreSQL
# a PENDING exclusive request blocks every request that arrives after it. So
# ordinary reads of auto_trial_keys, which would have coexisted with the dump
# perfectly happily, stacked up behind our own pointless DDL. The backup made
# the trial-minting path unavailable, and the DDL is the only reason it could.
#
# Running it once per process keeps the schema guarantee (a fresh worker still
# ensures the table exists) and removes the per-request lock request entirely.
# On failure the flag is NOT set, so a genuinely missing schema is retried
# rather than silently skipped forever.
#
# Same class as #3366 ("a no-op ALTER still takes ACCESS EXCLUSIVE — stop
# running it per request"), which fixed one site; this is another.
# DCHUB_AUTO_TRIAL_DDL_ALWAYS=1 restores the old per-call behaviour.
_SCHEMA_READY = False


def _ensure_schema(c):
    global _SCHEMA_READY
    if _SCHEMA_READY and not str(
            os.environ.get("DCHUB_AUTO_TRIAL_DDL_ALWAYS", "")).strip().lower() \
            in ("1", "true", "yes", "on"):
        return
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
            # r71-conv bridge: idempotent column adds for pre-existing tables
            # (CREATE TABLE IF NOT EXISTS won't add columns to an existing one).
            # operator_email is the human-conversion bridge handle captured AT
            # mint time — the missing top-of-human-funnel (agents don't pay).
            cur.execute("ALTER TABLE auto_trial_keys ADD COLUMN IF NOT EXISTS operator_email TEXT")
            cur.execute("ALTER TABLE auto_trial_keys ADD COLUMN IF NOT EXISTS operator_name TEXT")
            cur.execute("ALTER TABLE auto_trial_keys ADD COLUMN IF NOT EXISTS client_name TEXT")
            # 2026-06-08 conversion lever: per-day counter for the tiered cap
            # (unbound trials get a small taste; binding the operator email unlocks
            # the full daily allowance — the bridge that makes agents capture a lead).
            cur.execute("ALTER TABLE auto_trial_keys ADD COLUMN IF NOT EXISTS daily_count INT NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE auto_trial_keys ADD COLUMN IF NOT EXISTS daily_date DATE")
        # Only on a CLEAN pass. A failure leaves the flag false so the next
        # request retries — a half-applied schema must not be latched as done.
        _SCHEMA_READY = True
    except Exception:
        try: c.rollback()
        except Exception: pass


def mint_trial_for_request(req=None, tool_name: str = "", client_name: str = "",
                           operator_email: str = "", operator_name: str = "") -> dict:
    """Called by mcp_gatekeeper when an anonymous user hits an
    IDENTIFIED-tier gate. Returns {api_key, expires_at, cap, ...}.

    r71-conv: now accepts client_name (the MCP client UA, e.g. Cursor/Cline)
    and an optional operator_email/operator_name captured AT MINT TIME. This
    fixes the with-email endpoint (which always TypeError'd passing client_name)
    and creates the human-conversion bridge handle — agents don't pay, humans
    do, so we capture the operator at the moment of maximum intent.

    Reuses an existing trial key for the SAME (ip_hash, ua) within
    AUTO_TRIAL_REUSE_DAYS (default 30, but capped by the key's expires_at —
    so effectively ~TRIAL_DAYS=7d for unbound trials; the full 30d only bites
    once a key is email-redeemed to 365d) instead of minting a new one —
    prevents N-keys-per-user AND lets a returning agent re-bind its durable
    key across sessions."""
    req = req or request
    ip = (req.headers.get("CF-Connecting-IP")
          or req.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or req.remote_addr or "?")
    ua = (req.headers.get("User-Agent") or "")[:200]
    # r88h P2: don't mint throwaway trial keys to crawlers/bots (Googlebot,
    # meta-externalagent, etc.) — they never reuse the key and inflated the
    # mint→activate "drop" (see the crawler-mint note at ~line 537). Bots still
    # get anon-grace data; they just stop padding the funnel denominator with
    # keys no human is behind.
    _ua_l = ua.lower()
    if any(_b in _ua_l for _b in (
            "googlebot", "bingbot", "meta-externalagent", "facebookexternalhit",
            "duckduckbot", "yandexbot", "baiduspider", "ahrefsbot", "semrushbot",
            "petalbot", "bytespider", "applebot", "amazonbot", "gptbot", "ccbot",
            "claudebot", "google-extended", "perplexitybot", "dotbot", "mj12bot",
            "crawler", "spider")):
        return {"ok": False, "reason": "bot_skip", "bot": True}
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16]

    c = _conn()
    if c is None:
        return {"error": "no_database", "ok": False}
    # Reuse window, hoisted so BOTH the gated-identity probe and the legacy
    # (ip_hash, ua) reuse share it. _reuse_days is int()-built → f-string safe.
    _reuse_days = max(1, int(os.environ.get("AUTO_TRIAL_REUSE_DAYS", "30") or 30))
    # Deferred so the mirror never runs on the caller's connection mid-
    # transaction: set in the bind branches below, fired in the finally once
    # this function's DB work is done — the same ordering the two bind
    # ENDPOINTS use (they call it after closing their connection).
    _mirror_after = None
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            # ── 2026-08-31 (funnel audit, leak #1b: ROTATING-IP RE-MINT) ────
            # The two probes below both key on request_ip_hash, and the legacy
            # one also on request_ua. The 2026-06-19 note on that probe already
            # names the case they cannot cover: "Web hosts on ROTATING egress
            # IPs still won't match — email-bind is their durable path." That
            # cohort is not hypothetical, it is the majority of the funnel.
            #
            # Measured 2026-08-31 over mcp_high_intent_sessions: 5,205 mint rows
            # resolve to only 289 DISTINCT keys across 14 DISTINCT user agents,
            # and ONE user agent — the literal string "node" — accounts for 263
            # of those keys and 4,412 of the rows, with 0 email binds, ever.
            # A generic UA on rotating egress presents a brand-new (ip_hash, ua)
            # on every call, so both probes miss, a fresh key is minted, the
            # unbound counter resets, and the bind gate can never be reached.
            # That is the whole 289-keys / 14-emails funnel in one mechanism.
            #
            # The agent is not anonymous, though: it is holding a key it was
            # issued moments ago. That key is the ONE identifier that survives
            # IP rotation, and nothing here was reading it. Probe it FIRST.
            #
            # Deliberately narrow, so this cannot cost a real signup:
            #   * fires ONLY when the caller presents a key that is already ours
            #     and still live. A genuinely new agent presents none and falls
            #     through to the existing behaviour, untouched.
            #   * returns the SAME key rather than an error — an agent mid-task
            #     keeps working. This closes a re-mint, it does not close access.
            #   * binds operator_email on the spot if the agent finally supplied
            #     one, exactly as the ip_hash probe below does.
            #   * FAIL-OPEN: any exception falls through to the probes below.
            # Kill switch: AUTO_TRIAL_KEY_PROBE=0.
            _presented = ""
            try:
                if str(os.environ.get("AUTO_TRIAL_KEY_PROBE", "")).strip().lower() \
                        not in ("0", "false", "no", "off"):
                    _presented = (req.headers.get("X-API-Key")
                                  or req.headers.get("X-Api-Key") or "")
                    if not _presented:
                        _auth = req.headers.get("Authorization") or ""
                        if _auth.lower().startswith("bearer "):
                            _presented = _auth[7:]
                    _presented = (_presented or "").strip()[:128]
                    # Only OUR key shapes. Never look up an arbitrary string.
                    if not (_presented.startswith("dch_trial_")
                            or _presented.startswith("dch_live_")):
                        _presented = ""
            except Exception:
                _presented = ""

            if _presented:
                try:
                    cur.execute("""
                        SELECT api_key, expires_at, call_count,
                               (signed_up_email IS NOT NULL
                                OR operator_email IS NOT NULL) AS is_bound
                          FROM auto_trial_keys
                         WHERE api_key = %s AND expires_at > NOW()
                         LIMIT 1
                    """, (_presented,))
                    k = cur.fetchone()
                    if k:
                        _k_bound = bool(k[3])
                        if operator_email and not _k_bound:
                            try:
                                cur.execute(
                                    "UPDATE auto_trial_keys SET "
                                    "operator_email = COALESCE(operator_email, %s), "
                                    "operator_name  = COALESCE(operator_name, %s), "
                                    "client_name    = COALESCE(client_name, %s) "
                                    "WHERE api_key = %s",
                                    (operator_email.strip().lower(),
                                     (operator_name or None),
                                     (client_name[:80] or None) if client_name else None,
                                     k[0]))
                                # ★★★ 2026-09-02: MINT-TIME BINDS MUST MIRROR TOO.
                                # An operator email captured here is a bind like any
                                # other, so the same r88h unlock applies: the Stripe
                                # webhook lifts mcp_dev_keys.tier, and with no row there
                                # is nothing to lift — the agent pays and its own key
                                # stays free. The two bind ENDPOINTS mirrored; these three
                                # mint-time sites never did. Found by the structural guard
                                # in #3577, which held this as a strict xfail.
                                # Idempotent (ON CONFLICT DO UPDATE) and internally
                                # fail-soft, so a repeat bind is harmless.
                                _mirror_after = (k[0], operator_email)
                                _k_bound = True
                            except Exception:
                                note_swallowed_write(
                                    "auto_trial_keys",
                                    where="auto_trial.mint_trial_for_request.key_probe")

                        import datetime as _dt2
                        _days_left = None
                        if k[1]:
                            _d = k[1] - _dt2.datetime.now(_dt2.timezone.utc)
                            _days_left = max(0, int(_d.total_seconds() / 86400))
                        _used = int(k[2] or 0)
                        _gated = (not _k_bound) and _used >= TRIAL_FREE_CALLS_UNBOUND

                        out = {
                            "ok": True,
                            "api_key": k[0],
                            "expires_at": k[1].isoformat() if k[1] else None,
                            "tier": "IDENTIFIED",
                            "daily_calls": (TRIAL_DAILY_CALLS if _k_bound
                                            else TRIAL_DAILY_UNBOUND),
                            "daily_calls_when_email_bound": TRIAL_DAILY_CALLS,
                            "trial_days": TRIAL_DAYS,
                            "days_remaining": _days_left,
                            "reused": True,
                            "reuse_basis": "presented_key",
                        }
                        if _gated:
                            out.update({
                                "bind_required": True,
                                "gate": "bind_email_required",
                                "free_calls_unbound": TRIAL_FREE_CALLS_UNBOUND,
                                "bind_endpoint":
                                    "POST /api/v1/keys/auto-trial/bind {api_key, email}",
                                "upgrade_cta": (
                                    f"🔒 You are already holding this key and it has "
                                    f"used its {TRIAL_FREE_CALLS_UNBOUND} free unbound "
                                    f"calls. Re-minting returns the SAME key, not a "
                                    f"fresh allowance. Bind your operator's email "
                                    f"(free) to restore {TRIAL_DAILY_CALLS}/day: POST "
                                    f"/api/v1/keys/auto-trial/bind {{api_key, email}}."),
                                "operator_action": (
                                    f"ACTION FOR YOUR HUMAN OPERATOR: this key used its "
                                    f"{TRIAL_FREE_CALLS_UNBOUND} free unbound calls. "
                                    f"Ask your human for their work email — never "
                                    f"invent one — then bind it via POST "
                                    f"/api/v1/keys/auto-trial/bind {{api_key, email}} "
                                    f"to unlock {TRIAL_DAILY_CALLS}/day free."),
                                "instructions": (
                                    "Use api_key in X-API-Key header. Unbound free "
                                    "calls are exhausted for THIS key — binding an "
                                    "email is the free way to continue."),
                            })
                        elif _k_bound:
                            out["upgrade_cta"] = (
                                f"✅ Operator email bound — {TRIAL_DAILY_CALLS}/day "
                                f"on this key.")
                        else:
                            out["instructions"] = (
                                f"Use api_key in X-API-Key header. "
                                f"{TRIAL_DAILY_UNBOUND} unbound calls/day; bind an "
                                f"operator email for {TRIAL_DAILY_CALLS}/day.")
                        return out
                except Exception:
                    # FAIL-OPEN — fall through to the ip_hash probes below.
                    pass

            # ── 2026-07-10 (funnel audit, leak #1: RE-MINT ESCAPE) ──────────
            # The (ip_hash, ua) reuse below misses a UA change, so an identity
            # gated at TRIAL_FREE_CALLS_UNBOUND cumulative calls could re-mint
            # a fresh trial with a reset counter. If this IP holds a GATED
            # unbound live trial under ANY UA, hand back that SAME key. If the
            # agent finally supplied an operator email on THIS call, bind it —
            # that's the conversion we wanted — and the gate lifts. FAIL-OPEN.
            try:
                cur.execute(f"""
                    SELECT api_key, expires_at FROM auto_trial_keys
                     WHERE request_ip_hash = %s
                       AND signed_up_email IS NULL AND operator_email IS NULL
                       AND COALESCE(call_count, 0) >= %s
                       AND minted_at >= NOW() - INTERVAL '{_reuse_days} days'
                       AND expires_at > NOW()
                     ORDER BY minted_at DESC LIMIT 1
                """, (ip_hash, TRIAL_FREE_CALLS_UNBOUND))
                g = cur.fetchone()
                if g:
                    _bound_now = False
                    if operator_email:
                        try:
                            cur.execute(
                                "UPDATE auto_trial_keys SET "
                                "operator_email = COALESCE(operator_email, %s), "
                                "operator_name  = COALESCE(operator_name, %s), "
                                "client_name    = COALESCE(client_name, %s) "
                                "WHERE api_key = %s",
                                (operator_email.strip().lower(), (operator_name or None),
                                 (client_name[:80] or None) if client_name else None, g[0]))
                            # mirror the mint-time bind — see the note above
                            _mirror_after = (g[0], operator_email)
                            _bound_now = True
                        except Exception:
                            note_swallowed_write("auto_trial_keys", where="auto_trial.mint_trial_for_request")
                            pass
                    import datetime as _dt
                    days_left = None
                    if g[1]:
                        delta = g[1] - _dt.datetime.now(_dt.timezone.utc)
                        days_left = max(0, int(delta.total_seconds() / 86400))
                    out = {
                        "ok":          True,
                        "api_key":     g[0],
                        "expires_at":  g[1].isoformat() if g[1] else None,
                        "tier":        "IDENTIFIED",
                        "daily_calls": (TRIAL_DAILY_CALLS if _bound_now
                                        else TRIAL_DAILY_UNBOUND),
                        "daily_calls_when_email_bound": TRIAL_DAILY_CALLS,
                        "trial_days":  TRIAL_DAYS,
                        "days_remaining": days_left,
                        "reused":      True,
                    }
                    if not _bound_now:
                        out.update({
                            "bind_required": True,
                            "gate": "bind_email_required",
                            "free_calls_unbound": TRIAL_FREE_CALLS_UNBOUND,
                            "upgrade_cta": (
                                f"🔒 This identity already used its "
                                f"{TRIAL_FREE_CALLS_UNBOUND} free unbound calls — "
                                f"re-minting returns the SAME key, not a fresh "
                                f"allowance. Bind your operator's email (free) to "
                                f"restore {TRIAL_DAILY_CALLS}/day: POST "
                                f"/api/v1/keys/auto-trial/bind {{api_key, email}}."),
                            "operator_action": (
                                f"ACTION FOR YOUR HUMAN OPERATOR: this key used its "
                                f"{TRIAL_FREE_CALLS_UNBOUND} free unbound calls. "
                                f"Reply with your work email and your agent binds it "
                                f"via POST /api/v1/keys/auto-trial/bind "
                                f"{{api_key, email}} to unlock {TRIAL_DAILY_CALLS}/day."),
                            "instructions": (
                                f"Use api_key in X-API-Key header. Unbound free calls "
                                f"are exhausted for this identity — bind your "
                                f"operator's email to keep the free tier "
                                f"({TRIAL_DAILY_CALLS}/day)."),
                        })
                    else:
                        out["upgrade_cta"] = (
                            f"✅ Operator email bound — {TRIAL_DAILY_CALLS}/day "
                            f"unlocked on this key.")
                        out["instructions"] = (
                            f"Use api_key in X-API-Key header. "
                            f"{TRIAL_DAILY_CALLS} calls/day.")
                    return out
            except Exception:
                note_swallowed_write("auto_trial_keys", where="auto_trial.mint_trial_for_request")
                pass

            # Check for existing recent trial key for this caller
            try:
                # r-retention (2026-06-19): widen the same-(ip_hash,ua) reuse
                # window 24h -> AUTO_TRIAL_REUSE_DAYS (default 30, capped by
                # expires_at → effectively ~7d for unbound keys, full 30d once
                # email-redeemed) so a returning agent on a STABLE IP re-binds the SAME
                # durable key across sessions instead of minting a fresh throwaway
                # each visit. That is what lets call_count / last_used_at accumulate
                # and makes the key-reuse retention metric meaningful (avg 0.67
                # calls/key today = re-mint churn, not real one-shot use). Web hosts
                # on ROTATING egress IPs still won't match — email-bind is their
                # durable path. _reuse_days is int()-built (hoisted above), so
                # the f-string is injection-safe.
                cur.execute(f"""
                    SELECT api_key, expires_at FROM auto_trial_keys
                     WHERE request_ip_hash = %s
                       AND request_ua = %s
                       AND minted_at >= NOW() - INTERVAL '{_reuse_days} days'
                       AND expires_at > NOW()
                     ORDER BY minted_at DESC LIMIT 1
                """, (ip_hash, ua))
                r = cur.fetchone()
                if r:
                    # r71-conv: backfill the operator handle onto the reused key
                    # if the agent supplied one this call (capture at first chance).
                    if operator_email:
                        try:
                            cur.execute(
                                "UPDATE auto_trial_keys SET "
                                "operator_email = COALESCE(operator_email, %s), "
                                "operator_name  = COALESCE(operator_name, %s), "
                                "client_name    = COALESCE(client_name, %s) "
                                "WHERE api_key = %s",
                                (operator_email.strip().lower(), (operator_name or None),
                                 (client_name[:80] or None) if client_name else None, r[0]))
                            # mirror the backfilled bind — see the note above
                            _mirror_after = (r[0], operator_email)
                        except Exception:
                            note_swallowed_write("auto_trial_keys", where="auto_trial.mint_trial_for_request")
                            pass
                    # Compute days_remaining for the countdown CTA
                    import datetime as _dt
                    days_left = None
                    if r[1]:
                        delta = r[1] - _dt.datetime.now(_dt.timezone.utc)
                        days_left = max(0, int(delta.total_seconds() / 86400))
                    return {
                        "ok":          True,
                        "api_key":     r[0],
                        "expires_at":  r[1].isoformat() if r[1] else None,
                        "tier":        "IDENTIFIED",
                        "daily_calls": (TRIAL_DAILY_CALLS if operator_email
                                        else TRIAL_DAILY_UNBOUND),
                        "daily_calls_when_email_bound": TRIAL_DAILY_CALLS,
                        "trial_days":  TRIAL_DAYS,
                        "days_remaining": days_left,
                        "reused":      True,
                        "upgrade_cta": (
                            f"⏳ {days_left}d left on this trial ({TRIAL_DAILY_UNBOUND}/day). "
                            f"Bind your operator's email — POST /api/v1/keys/auto-trial/bind "
                            f"{{api_key, email}} — for a PERMANENT key, {TRIAL_DAILY_CALLS}/day, "
                            f"limit alerts, and first access to new tools."
                        ),
                        "operator_action": (
                            f"ACTION FOR YOUR HUMAN OPERATOR: this key is capped at "
                            f"{TRIAL_DAILY_UNBOUND}/day until you add your work email "
                            f"— reply with it to unlock {TRIAL_DAILY_CALLS}/day "
                            f"(agent binds via POST /api/v1/keys/auto-trial/bind "
                            f"{{api_key, email}})."
                        ),
                        "instructions":(f"Use api_key in X-API-Key header. "
                                         f"{TRIAL_DAILY_UNBOUND} calls/day; bind your "
                                         f"operator email to unlock {TRIAL_DAILY_CALLS}/day."),
                    }
            except Exception:
                note_swallowed_write("auto_trial_keys", where="auto_trial.mint_trial_for_request")
                pass

            # ── 2026-07-10 (leak #1, part 2): CARRY THE COUNTER FORWARD. Even
            # when no live gated trial exists (expired, or the identity's usage
            # sits on a dch_live_ claim key — the other mint door), a fresh
            # unbound mint from a GATED identity inherits the cumulative count,
            # so it is born gated and binding stays cheaper than re-minting.
            # Seeded ONLY when the identity already crossed the gate (partial
            # counts are NOT carried — that would pollute the activated-stage
            # funnel stat, which reads call_count > 0). notes stamps the seed
            # provenance so stats can exclude these later. FAIL-OPEN → 0.
            _carry = 0
            if not operator_email:
                try:
                    cur.execute(f"""
                        SELECT COALESCE(MAX(COALESCE(call_count, 0)), 0)
                          FROM auto_trial_keys
                         WHERE request_ip_hash = %s
                           AND signed_up_email IS NULL AND operator_email IS NULL
                           AND minted_at >= NOW() - INTERVAL '{_reuse_days} days'
                    """, (ip_hash,))
                    _carry = int((cur.fetchone() or [0])[0] or 0)
                    cur.execute(f"""
                        SELECT COALESCE(MAX(COALESCE((metadata->>'validate_calls')::int, 0)), 0)
                          FROM mcp_dev_keys
                         WHERE metadata->>'source' = 'claim_api'
                           AND metadata->>'ip' = %s
                           AND (email IS NULL OR email = '')
                           AND created_at >= NOW() - INTERVAL '{_reuse_days} days'
                    """, (ip,))
                    _carry = max(_carry, int((cur.fetchone() or [0])[0] or 0))
                except Exception:
                    _carry = 0
            _seed = _carry if _carry >= TRIAL_FREE_CALLS_UNBOUND else 0

            # Mint a new trial key
            api_key = "dch_trial_" + secrets.token_urlsafe(24).replace("_", "x").replace("-", "x")[:32]
            try:
                cur.execute(f"""
                    INSERT INTO auto_trial_keys
                      (api_key, minted_for_tool, request_ip_hash, request_ua,
                       expires_at, client_name, operator_email, operator_name,
                       call_count, notes)
                    VALUES (%s, %s, %s, %s, NOW() + INTERVAL '{TRIAL_DAYS} days', %s, %s, %s, %s, %s)
                    ON CONFLICT (api_key) DO NOTHING
                    RETURNING expires_at
                """, (api_key, tool_name[:40] or None, ip_hash, ua,
                      (client_name[:80] or None) if client_name else None,
                      (operator_email.strip().lower() or None) if operator_email else None,
                      (operator_name[:120] or None) if operator_name else None,
                      _seed,
                      (f"gate_carry:{_seed} (cumulative unbound usage carried "
                       f"from this identity — re-minting does not reset the "
                       f"bind gate)") if _seed else None))
                r = cur.fetchone()
                expires = r[0].isoformat() if r and r[0] else None
            except Exception:
                note_swallowed_write("auto_trial_keys", where="auto_trial.mint_trial_for_request")
                return {"error": "mint_failed", "ok": False}
    finally:
        try: c.close()
        except Exception: pass
        # ★ AFTER the connection closes, and inside `finally` so it still fires
        # on the several `return`s inside the try body above — the block after
        # the finally (where the CRM capture sits) never sees those paths.
        if _mirror_after:
            _mirror_trial_to_mcp_dev_keys(*_mirror_after)

    # r74 (2026-06-07): emit CRM reverse-ETL capture on first mint (not reuse).
    # Only fires when an operator email is bound at mint time. Fail-soft.
    try:
        if operator_email:
            from routes.crm_reverse_etl import capture_event as _crm_capture
            _crm_capture("trial_key_activated", {
                "email": operator_email,
                "tool": tool_name, "mcp_client": client_name,
            })
    except Exception:
        pass

    # Born-gated mint (identity carried past the gate): overwrite the cheery
    # fresh-mint CTAs with the honest bind-first ask so the agent isn't told
    # it has N free calls it doesn't have.
    if _seed:
        return {
            "ok":          True,
            "api_key":     api_key,
            "expires_at":  expires,
            "tier":        "IDENTIFIED",
            "daily_calls": TRIAL_DAILY_UNBOUND,
            "daily_calls_when_email_bound": TRIAL_DAILY_CALLS,
            "trial_days":  TRIAL_DAYS,
            "days_remaining": TRIAL_DAYS,
            "reused":      False,
            "bind_required": True,
            "gate": "bind_email_required",
            "free_calls_unbound": TRIAL_FREE_CALLS_UNBOUND,
            "upgrade_cta": (
                f"🔒 This identity already used its {TRIAL_FREE_CALLS_UNBOUND} "
                f"free unbound calls and the counter carries onto this key — "
                f"re-minting does not reset it. Bind your operator's email "
                f"(free) to restore {TRIAL_DAILY_CALLS}/day: POST "
                f"/api/v1/keys/auto-trial/bind {{api_key, email}}."),
            "operator_action": (
                f"ACTION FOR YOUR HUMAN OPERATOR: this key's identity used its "
                f"{TRIAL_FREE_CALLS_UNBOUND} free unbound calls. Reply with your "
                f"work email and your agent binds it via POST "
                f"/api/v1/keys/auto-trial/bind {{api_key, email}} to unlock "
                f"{TRIAL_DAILY_CALLS}/day."),
            "instructions": (
                f"Use api_key in X-API-Key header. Unbound free calls are "
                f"exhausted for this identity — bind your operator's email to "
                f"keep the free tier ({TRIAL_DAILY_CALLS}/day)."),
        }

    return {
        "ok":          True,
        "api_key":     api_key,
        "expires_at":  expires,
        "tier":        "IDENTIFIED",
        "daily_calls": TRIAL_DAILY_UNBOUND,
        "daily_calls_when_email_bound": TRIAL_DAILY_CALLS,
        "trial_days":  TRIAL_DAYS,
        "days_remaining": TRIAL_DAYS,
        "reused":      False,
        "upgrade_cta": (
            f"⏳ This trial key expires in {TRIAL_DAYS} days and is capped at "
            f"{TRIAL_DAILY_UNBOUND}/day. Bind your operator's email — POST "
            f"/api/v1/keys/auto-trial/bind {{api_key, email}} — to get: a "
            f"PERMANENT key (no re-minting), {TRIAL_DAILY_CALLS}/day, an alert "
            f"before you hit limits, and first access to new tools. Agents can't "
            f"pay — the email is the only way the upgrade path reaches your human."
        ),
        "value_props": [
            "permanent key — stop re-minting every 7 days",
            f"{TRIAL_DAILY_CALLS}/day instead of {TRIAL_DAILY_UNBOUND}/day",
            "usage alerts before you hit the cap",
            "first access to new DC Hub tools + data layers",
        ],
        "operator_action": (
            f"ACTION FOR YOUR HUMAN OPERATOR: this key is capped at "
            f"{TRIAL_DAILY_UNBOUND} calls/day until you add your work email. Reply "
            f"with it and your agent unlocks {TRIAL_DAILY_CALLS}/day via POST "
            f"/api/v1/keys/auto-trial/bind {{api_key, email}}."
        ),
        "instructions":(f"Use api_key in X-API-Key header. {TRIAL_DAILY_UNBOUND} "
                         f"calls/day free; bind your operator's email to unlock "
                         f"{TRIAL_DAILY_CALLS}/day for {TRIAL_DAYS} days: POST "
                         f"/api/v1/keys/auto-trial/bind {{api_key, email}} (or send "
                         f"your human to https://dchub.cloud/redeem to convert to a "
                         f"365-day IDENTIFIED key)."),
    }


def is_trial_key(api_key: str) -> bool:
    """Cheap shape check. mcp_gatekeeper.resolve_tier delegates to this."""
    return bool(api_key) and api_key.startswith("dch_trial_")


def validate_trial_key(api_key: str) -> tuple[bool, str]:
    """Returns (valid, reason). Validates against DB + expiry."""
    if not is_trial_key(api_key):
        return False, "not_trial_prefix"
    c = _conn()
    if c is None: return False, "no_database"
    try:
        with c.cursor() as cur:
            try:
                # Tiered daily cap: unbound trials get TRIAL_DAILY_UNBOUND/day;
                # binding the operator (or signed-up) email unlocks the full
                # TRIAL_DAILY_CALLS. This is the conversion lever — hitting the
                # unbound cap is the moment the agent asks its human for an email.
                cur.execute("""
                    SELECT expires_at, signed_up_email, operator_email,
                           COALESCE(daily_count, 0), daily_date,
                           COALESCE(call_count, 0)
                      FROM auto_trial_keys WHERE api_key = %s
                """, (api_key,))
                r = cur.fetchone()
                if not r: return False, "unknown_trial_key"
                now = datetime.datetime.now(datetime.timezone.utc)
                if r[0] and r[0] < now:
                    return False, "expired"
                bound = bool(r[1]) or bool(r[2])
                # EMAIL-GATE THE TAIL: first TRIAL_FREE_CALLS_UNBOUND cumulative calls
                # are free; past that an UNBOUND trial must bind email. Dropping to FREE
                # (not a hard error) means the agent still gets the paywall + bind CTA —
                # the forcing function that turns active trials into captured leads.
                if not bound and int(r[5] or 0) >= TRIAL_FREE_CALLS_UNBOUND:
                    return False, "bind_email_required"
                cap   = TRIAL_DAILY_CALLS if bound else TRIAL_DAILY_UNBOUND
                today = now.date()
                used_today = r[3] if r[4] == today else 0
                if used_today >= cap:
                    return False, ("daily_cap_unbound" if not bound else "daily_cap")
                try:
                    cur.execute("""
                        UPDATE auto_trial_keys
                           SET last_used_at = NOW(),
                               call_count   = call_count + 1,
                               daily_count  = CASE WHEN daily_date = %s
                                                   THEN daily_count + 1 ELSE 1 END,
                               daily_date   = %s
                         WHERE api_key = %s
                    """, (today, today, api_key))
                except Exception:
                    note_swallowed_write("auto_trial_keys", where="auto_trial.validate_trial_key")
                    pass
                return True, "ok"
            except Exception:
                # FAIL-OPEN: daily_* not migrated yet OR a transient error → fall
                # back to the legacy expiry-only path so a caller is never wrongly
                # blocked. (mint/redeem run _ensure_schema constantly, so the
                # columns exist within seconds of deploy; this covers the gap.)
                try:
                    cur.execute("SELECT expires_at FROM auto_trial_keys WHERE api_key = %s", (api_key,))
                    r = cur.fetchone()
                    if not r: return False, "unknown_trial_key"
                    if r[0] and r[0] < datetime.datetime.now(datetime.timezone.utc):
                        return False, "expired"
                    cur.execute("UPDATE auto_trial_keys SET last_used_at = NOW(), "
                                "call_count = call_count + 1 WHERE api_key = %s", (api_key,))
                    return True, "ok"
                except Exception:
                    note_swallowed_write("auto_trial_keys", where="auto_trial.validate_trial_key")
                    return False, "validation_failed"
    finally:
        try: c.close()
        except Exception: pass


@auto_trial_bp.route("/api/v1/keys/auto-mint", methods=["POST"])
def auto_mint_endpoint():
    """Direct callable for testing or alt clients. Same as the
    inline mint in mcp_gatekeeper.

    2026-06-10 (conversion): also read an optional email/name from the JSON
    body and forward it. mint_trial_for_request already honors operator_email
    (→ auto_trial_keys.operator_email, the 15→50/day cap bump, and CRM
    trial_key_activated capture) — the endpoint just wasn't forwarding it, so
    every /auto-mint key was anonymous and un-nurturable (the dead free→paid
    funnel). No email in the body → unchanged anonymous mint."""
    d = request.get_json(silent=True) or {}
    tool = (request.args.get("tool") or d.get("tool") or "").strip()
    return jsonify(mint_trial_for_request(
        request, tool,
        client_name=(d.get("client_name") or "").strip(),
        operator_email=(d.get("operator_email") or d.get("email") or "").strip(),
        operator_name=(d.get("operator_name") or "").strip(),
    )), 200


def _bind_receipt_armed() -> bool:
    """Customer-facing send — DISARMED by default, per the house pattern.

    Every customer-facing send path in this codebase ships dry-run behind its own
    arm flag (ACTIVATION_NUDGE_ARM, CUSTOMER_WHITE_GLOVE_ACT, the agent-digest
    DRY_RUN default) precisely so nothing can spam by accident. Unarmed, this logs
    the intended recipient and sends NOTHING.
    """
    return (os.environ.get("DCHUB_BIND_RECEIPT_ARM") or "").strip() == "1"


def _ensure_bind_receipt_log(c) -> None:
    """One receipt per key, ever — direct DDL (safe_db SKIPs DDL)."""
    try:
        with c.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS bind_receipt_log ("
                        " api_key TEXT PRIMARY KEY,"
                        " email TEXT NOT NULL,"
                        " sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                        " armed BOOLEAN NOT NULL DEFAULT FALSE,"
                        " delivered BOOLEAN)")
    except Exception as e:
        note_swallowed_write("bind_receipt_log ddl", e)


def _bind_receipt_html(api_key: str, email: str, name: str) -> str:
    import html as _h
    upgrade = f"https://dchub.cloud/upgrade?key={_h.escape(api_key)}"
    who = f" {_h.escape(name)}" if name else ""
    return (
        "<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        "max-width:520px;color:#111\">"
        f"<p>Hi{who},</p>"
        "<p>Your AI agent just bound this address to a DC Hub trial key, so the "
        f"key keeps working across sessions — <b>{TRIAL_DAILY_CALLS} calls/day</b>, free.</p>"
        f"<p style=\"margin:22px 0\"><a href=\"{upgrade}\" "
        "style=\"background:#4F46E5;color:#fff;padding:11px 20px;border-radius:8px;"
        "text-decoration:none;font-weight:600\">Unlock full data on this key</a></p>"
        f"<p style=\"font-size:13px;color:#555\">That link upgrades <i>this exact key</i> "
        "— your agent keeps working, no copy/paste, nothing to reconfigure.</p>"
        "<p style=\"font-size:12px;color:#777;border-top:1px solid #eee;padding-top:12px;"
        "margin-top:22px\">You received this once because your agent supplied this "
        "address to keep its DC Hub key alive. It is a one-time receipt, not a "
        "subscription — we will not add you to any list. Reply STOP and we will "
        f"suppress {_h.escape(email)} permanently.</p></div>")


def _send_bind_receipt(api_key: str, email: str, name: str) -> dict:
    """Transactional receipt at bind time. THE point: put a payment surface in
    front of a HUMAN at the moment of intent.

    ★WHY TRANSACTIONAL AND NOT MARKETING: bind_email's published consent copy is
    transactional-only (recovery + receipts, explicitly NO digest/marketing), and
    `marketing_opt_in` defaults FALSE with a stamped lawful_basis/purpose. So this
    must NOT flip marketing_opt_in and must NOT go through
    send_marketing_email() — a one-time receipt is inside the existing basis; a
    list subscription is not.
    ★Delegates to main._resend_email rather than POSTing Resend directly, so the
    ratcheting choke-point guard (tests/test_marketing_chokepoint.py, which FAILS
    any NEW file containing the literal Resend send URL) stays green. That URL is
    deliberately NOT written out here: the guard regexes file CONTENT, so naming it
    even in a comment would trip it exactly like a real sender would.
    ★Suppression is still honoured (fail-open) even though it is transactional.
    """
    out = {"armed": _bind_receipt_armed(), "to": email, "sent": False}
    c = _conn()
    if c is None:
        out["skipped"] = "no_database"
        return out
    try:
        _ensure_bind_receipt_log(c)
        # Idempotent: claim the slot first, so a retry or a re-bind cannot re-mail.
        with c.cursor() as cur:
            cur.execute("INSERT INTO bind_receipt_log (api_key, email, armed) "
                        "VALUES (%s,%s,%s) ON CONFLICT (api_key) DO NOTHING",
                        (api_key, email, out["armed"]))
            if cur.rowcount == 0:
                out["skipped"] = "already_sent_for_this_key"
                return out
        try:
            from routes.email_suppression import is_suppressed
            if is_suppressed(email):
                out["skipped"] = "suppressed"
                return out
        except Exception:
            pass                      # fail-open, matching the existing contract
        if not out["armed"]:
            out["skipped"] = ("not_armed — set DCHUB_BIND_RECEIPT_ARM=1 to send. "
                              "Recipient logged, nothing mailed.")
            log.info("[bind-receipt] DRY-RUN would email %s for key %s",
                     email, api_key[:12])
            return out
        try:
            from main import _resend_email
            ok = bool(_resend_email(
                email, "Your DC Hub key is live — unlock full data",
                _bind_receipt_html(api_key, email, name),
                from_email="hello@dchub.cloud", from_name="DC Hub"))
        except Exception as e:
            log.warning("[bind-receipt] send failed: %s", str(e)[:160])
            ok = False
        out["sent"] = ok
        with c.cursor() as cur:
            cur.execute("UPDATE bind_receipt_log SET delivered=%s WHERE api_key=%s",
                        (ok, api_key))
        return out
    except Exception as e:
        note_swallowed_write("bind receipt", e)
        out["skipped"] = str(e)[:160]
        return out
    finally:
        try:
            c.close()
        except Exception:
            pass


def _mirror_trial_to_mcp_dev_keys(api_key: str, email: str):
    """r88h: mirror a bound trial key into mcp_dev_keys (tier='free') so a LATER
    Stripe payment by this email flips THIS exact key's tier. The MCP gate reads
    mcp_dev_keys.tier and validate_key (flask_mcp_endpoints.py:218) picks up the
    row the instant the webhook lifts it to 'paid' — turning the documented
    identified→paid 100% leak into a hands-free unlock of the agent's OWN key.
    Own connection + fully fail-soft: must NEVER affect the bind."""
    if not api_key or not email:
        return
    try:
        c = _conn()
        if c is None:
            return
        try:
            with c.cursor() as cur:
                # api_key is the PRIMARY KEY; developer_id is NOT NULL (use the key
                # itself as a stable id). The tier CHECK only allows
                # free/paid/enterprise, so seed 'free' and let the Stripe webhook
                # lift it to 'paid'. Verified end-to-end before ship.
                cur.execute(
                    "INSERT INTO mcp_dev_keys (api_key, developer_id, email, tier, status) "
                    "VALUES (%s, %s, %s, 'free', 'active') "
                    "ON CONFLICT (api_key) DO UPDATE "
                    "   SET email = EXCLUDED.email, status = 'active'",
                    (api_key, api_key, email))
            c.commit()
        finally:
            try: c.close()
            except Exception: pass
    except Exception:
        note_swallowed_write("mcp_dev_keys", where="auto_trial._mirror_trial_to_mcp_dev_keys")
        pass


@auto_trial_bp.route("/api/v1/keys/auto-trial/redeem", methods=["POST"])
def redeem_endpoint():
    """Bind a trial key to an email — converts the trial into a
    permanent IDENTIFIED-tier account. One-click conversion path."""
    d = request.get_json(silent=True) or {}
    api_key = (d.get("api_key") or "").strip()
    email   = (d.get("email") or "").strip().lower()
    if not is_trial_key(api_key):
        return jsonify(error="not_a_trial_key"), 400
    if "@" not in email or len(email) > 200:
        return jsonify(error="valid_email_required"), 400
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute("""
                UPDATE auto_trial_keys
                   SET signed_up_email = %s,
                       expires_at = NOW() + INTERVAL '365 days'
                 WHERE api_key = %s
                   AND (signed_up_email IS NULL OR signed_up_email = %s)
                RETURNING expires_at
            """, (email, api_key, email))
            r = cur.fetchone()
            if not r:
                return jsonify(error="key_not_found_or_already_bound"), 404
    finally:
        try: c.close()
        except Exception: pass
    _mirror_trial_to_mcp_dev_keys(api_key, email)
    return jsonify(ok=True, api_key=api_key, email=email,
                   tier="IDENTIFIED", daily_calls=200,
                   expires_at=r[0].isoformat() if r[0] else None,
                   message=(f"Trial key bound to {email}. You now have "
                            f"IDENTIFIED tier (200 calls/day) for 365 days. "
                            f"To upgrade to DEVELOPER ($49/mo, 500 calls/day): "
                            f"https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c"
                            f"?prefilled_email={email}")), 200


@auto_trial_bp.route("/api/v1/keys/auto-trial/bind", methods=["POST"])
def bind_operator_endpoint():
    """r71-conv — the HUMAN-CONVERSION BRIDGE capture. Lighter than /redeem:
    binds an operator_email (the human behind the agent) to a trial key at the
    moment of demand, WITHOUT converting the trial. This is the agent-satisfiable
    step the mint CTA points at — most coding agents (Cursor/Cline/Continue) will
    ask their user for an email when told it keeps the key working. It gives the
    out-of-band operator digest someone to reach. Does NOT send any email."""
    d = request.get_json(silent=True) or {}
    api_key = (d.get("api_key") or "").strip()
    email   = (d.get("email") or "").strip().lower()
    name    = (d.get("name") or d.get("operator_name") or "").strip()[:120]
    if not is_trial_key(api_key):
        return jsonify(error="not_a_trial_key"), 400
    if "@" not in email or "." not in email.split("@")[-1] or len(email) > 200:
        return jsonify(error="valid_email_required"), 400
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute("""
                UPDATE auto_trial_keys
                   SET operator_email = %s,
                       operator_name  = COALESCE(NULLIF(%s,''), operator_name)
                 WHERE api_key = %s
                RETURNING expires_at
            """, (email, name, api_key))
            r = cur.fetchone()
            if not r:
                return jsonify(error="key_not_found"), 404
    finally:
        try: c.close()
        except Exception: pass
    _mirror_trial_to_mcp_dev_keys(api_key, email)
    # ★2026-07-28: bind_email captured 6 addresses and had NEVER mailed one of them.
    # This endpoint's own docstring said "Does NOT send any email" while its success
    # message promised the human "a usage summary + 1-click upgrade link" — a promise
    # nothing kept. The only thing that would have mailed them (agent_winback_digest)
    # draws its audience from mcp_dev_keys.marketing_opt_in='true', which bind never
    # sets, so the audience was 0 and agent_digest_log had 0 rows: zero emails ever
    # delivered to a bound operator. Verified separately that transport is healthy
    # (4 dchub senders delivered 18 threads in 3 days), so this was a missing wire,
    # not broken infrastructure. Now it sends ONE transactional receipt, at the
    # moment of intent, with the 1-click upgrade URL for THIS key.
    receipt = _send_bind_receipt(api_key, email, name)
    _promise = ("They'll get a one-time receipt with a 1-click upgrade link. "
                if receipt.get("sent") else "")
    return jsonify(
        ok=True, api_key=api_key, operator_email=email, bound=True,
        upgrade_url=f"https://dchub.cloud/upgrade?key={api_key}",
        receipt=receipt,
        message=(f"Operator {email} bound to this trial key. {_promise}"
                 f"To upgrade now, open "
                 f"https://dchub.cloud/upgrade?key={api_key} (one click upgrades "
                 f"THIS exact key — no copy/paste). Or convert to a 365-day "
                 f"IDENTIFIED key: POST /api/v1/keys/auto-trial/redeem "
                 f"{{api_key, email}}.")), 200


@auto_trial_bp.route("/api/v1/keys/auto-trial/stats", methods=["GET"])
def stats_endpoint():
    """Public funnel metrics for the inline auto-mint flow.

    r62-conv (2026-06-01): rebuilt as an honest 4-stage funnel after the
    success analysis found 143 distinct agents calling the paid grid/fiber
    tools but only 2 paid keys. The old endpoint hid the single most
    important stage — did the agent actually RE-USE the minted key
    (proving inline-mint works)? — and reported `trials_upgraded` off a
    column (`upgraded_tier`) that NOTHING ever writes, so it was
    structurally always 0 (same bug class as the per-platform conv-rate
    that never coincides). Paid conversion is now computed by JOINing the
    bound email to a real paid tier in mcp_dev_keys.

        Stage 1  minted     — trial key issued in a paywall response
        Stage 2  activated  — agent reconnected & USED the key (call_count>0)
        Stage 3  identified — bound an email via /auto-trial/redeem
        Stage 4  paid       — that email later holds a paid/enterprise key

    Each stage's drop_pct shows where the conversion engine leaks.
    """
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    out = {
        "trials_minted_total":      0,
        "trials_minted_7d":         0,
        "trials_activated":         0,   # reconnected & used the key (NEW)
        "trials_signed_up":         0,   # bound an email
        "trials_paid":              0,   # email → real paid/enterprise key (NEW: honest)
        "activated_rate_pct":       0.0,
        "signed_up_rate_pct":       0.0,
        "paid_rate_pct":            0.0,
        "active_unique_callers_7d": 0,
    }
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            try:
                # r78: "identified" now counts EITHER email column — the
                # operator-bind path (/auto-trial/bind, and /keys/identify
                # fallthrough) writes operator_email, not signed_up_email,
                # so partial wins were invisible and stage 3 read 0 even
                # when binds happened. callers_7d now counts keys actually
                # USED in the window — the old mint-time IP-hash count was
                # dominated by crawler mints (meta-externalagent, Googlebot)
                # and measured crawler IP diversity, not engaged callers.
                cur.execute("""
                    SELECT COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE minted_at >= NOW() - INTERVAL '7 days') AS minted_7d,
                           COUNT(*) FILTER (WHERE COALESCE(call_count,0) > 0
                                              OR last_used_at IS NOT NULL) AS activated,
                           COUNT(*) FILTER (WHERE signed_up_email IS NOT NULL
                                              OR operator_email IS NOT NULL) AS signed_up,
                           COUNT(DISTINCT request_ip_hash) FILTER (WHERE last_used_at >= NOW() - INTERVAL '7 days') AS callers_7d,
                           COUNT(*) FILTER (WHERE operator_email IS NOT NULL) AS operator_bound
                      FROM auto_trial_keys
                """)
                r = cur.fetchone() or (0, 0, 0, 0, 0, 0)
                total, m7d, act, su, callers = (int(r[0] or 0), int(r[1] or 0),
                                                 int(r[2] or 0), int(r[3] or 0),
                                                 int(r[4] or 0))
                out["trials_minted_total"]      = total
                out["trials_minted_7d"]         = m7d
                out["trials_activated"]         = act
                out["trials_signed_up"]         = su
                out["trials_operator_bound"]    = int(r[5] or 0)
                out["active_unique_callers_7d"] = callers
                out["activated_rate_pct"] = round(100.0 * act / max(1, total), 2)
                out["signed_up_rate_pct"] = round(100.0 * su  / max(1, total), 2)
            except Exception: pass

            # Honest paid conversion: a bound trial email that later holds a
            # real paid/enterprise key. JOIN to mcp_dev_keys by email. Wrapped
            # defensively — if the table/columns differ on this deploy we
            # report 0 paid rather than 500 the whole endpoint.
            try:
                # r78: match on either bound email (operator OR signed-up).
                cur.execute("""
                    SELECT COUNT(DISTINCT COALESCE(t.signed_up_email, t.operator_email))
                      FROM auto_trial_keys t
                      JOIN mcp_dev_keys k
                        ON lower(k.email) = lower(COALESCE(t.signed_up_email, t.operator_email))
                     WHERE COALESCE(t.signed_up_email, t.operator_email) IS NOT NULL
                       AND k.tier IN ('paid','enterprise')
                """)
                paid = int((cur.fetchone() or [0])[0] or 0)
                out["trials_paid"]     = paid
                out["paid_rate_pct"]   = round(100.0 * paid / max(1, out["trials_minted_total"]), 2)
            except Exception:
                try: c.rollback()
                except Exception: pass
    finally:
        try: c.close()
        except Exception: pass

    # Staged funnel + biggest-leak, so the reader sees WHERE it breaks.
    m  = out["trials_minted_total"]
    def _drop(a, b):
        return None if a == 0 else round(100.0 * (1 - (b / a)), 1)
    out["funnel"] = {
        "1_minted":     m,
        "2_activated":  out["trials_activated"],
        "3_identified": out["trials_signed_up"],
        "4_paid":       out["trials_paid"],
    }
    out["drop_rates"] = {
        "1_minted_to_2_activated":     _drop(m,                       out["trials_activated"]),
        "2_activated_to_3_identified": _drop(out["trials_activated"], out["trials_signed_up"]),
        "3_identified_to_4_paid":      _drop(out["trials_signed_up"], out["trials_paid"]),
    }
    _leak_stage, _leak_drop = None, -1.0
    for stg, dr in out["drop_rates"].items():
        if dr is not None and dr > _leak_drop:
            _leak_drop, _leak_stage = dr, stg
    out["biggest_leak"] = ({"stage": _leak_stage, "drop_pct": _leak_drop}
                           if _leak_stage else None)
    out["legend"] = {
        "1_minted":     "trial key issued inline in a paywall response",
        "2_activated":  "agent reconnected and USED the key (call_count>0) — proves inline-mint works",
        "3_identified": "bound an email via /auto-trial/redeem, /auto-trial/bind, or /keys/identify (operator OR signed-up email)",
        "4_paid":       "that email later holds a real paid/enterprise key (JOIN mcp_dev_keys)",
    }
    out["generated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
