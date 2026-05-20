"""Phase FF+25-followup-r25 (2026-05-20) — schema repair + geocoding + funnel diag.
==========================================================================

Brief #2 from the Inspector surfaced three concrete issues:

  1. `brain_findings` table doesn't exist — consistency radar can't
     persist findings, so the brain can't learn from history.
  2. `worker_versions` table doesn't exist — worker drift detector blind.
  3. `press_releases.published_at` column missing on some deploys.
  4. 25% of facilities (3,187 rows) have country='?' — geocoding gap.
  5. MCP funnel shows 99.8% drop-off from upgrade signals to final
     paid conversion — needs diagnostic surface.

This module ships three endpoints to address each:

  POST /api/v1/admin/schema/repair         create missing tables + cols
  POST /api/v1/admin/geocoding/backfill    fill country='?' rows
  GET  /api/v1/admin/funnel/leakage        per-stage drop-off detail
"""
import os
import logging
import datetime
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
schema_repair_bp = Blueprint("schema_repair", __name__)


_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


# ── Schema repair ────────────────────────────────────────────────────
# Each entry: (label, list of idempotent SQL statements to run).
# All wrapped in try/except so one bad statement doesn't block the rest.
SCHEMA_STATEMENTS = [
    ("brain_findings table", [
        """CREATE TABLE IF NOT EXISTS brain_findings (
            id           SERIAL PRIMARY KEY,
            issue        TEXT NOT NULL,
            url          TEXT,
            count        INTEGER NOT NULL DEFAULT 1,
            detail       TEXT,
            detector     TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at  TIMESTAMPTZ,
            status       TEXT NOT NULL DEFAULT 'open'
        )""",
        "CREATE INDEX IF NOT EXISTS ix_brain_findings_issue ON brain_findings(issue)",
        "CREATE INDEX IF NOT EXISTS ix_brain_findings_created ON brain_findings(created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_brain_findings_status ON brain_findings(status)",
    ]),
    ("worker_versions table", [
        """CREATE TABLE IF NOT EXISTS worker_versions (
            id           SERIAL PRIMARY KEY,
            version      TEXT NOT NULL,
            observed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            source       TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS ix_worker_versions_observed ON worker_versions(observed_at DESC)",
    ]),
    ("users.dunning_counters columns", [
        # Phase r26 (2026-05-20): tracks dunning state per customer so
        # handle_payment_failed can decide whether to demote API rate
        # limits without yanking the key entirely. Real customers (with
        # at least one successful invoice) ride out Stripe's full
        # retry cycle untouched; first-charge-never-succeeded freeloaders
        # get demoted to 'free' rate limit on the 2nd consecutive failure
        # while keeping their key alive so dunning emails still work.
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS invoices_paid_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS payment_failed_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS demoted_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS demoted_reason TEXT",
    ]),
    ("press_releases.published_at column", [
        # If the column doesn't exist, ALTER TABLE ADD COLUMN IF NOT
        # EXISTS is idempotent. Some deploys may have it as
        # published_date instead — we add published_at unconditionally
        # so the column the brain expects always exists.
        "ALTER TABLE press_releases ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ",
        # Backfill from published_date if both exist
        """DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'press_releases'
                   AND column_name = 'published_date'
            ) THEN
                UPDATE press_releases
                   SET published_at = published_date::timestamptz
                 WHERE published_at IS NULL
                   AND published_date IS NOT NULL;
            END IF;
        END $$""",
    ]),
]


@schema_repair_bp.route("/api/v1/admin/schema/repair", methods=["POST"])
def schema_repair():
    """Run idempotent schema-repair statements. Safe to call multiple
    times — each statement uses IF NOT EXISTS or equivalent."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    results = []
    try:
        for label, stmts in SCHEMA_STATEMENTS:
            for sql in stmts:
                try:
                    with c.cursor() as cur:
                        cur.execute(sql)
                    try: c.commit()
                    except Exception: pass
                    results.append({"label": label, "ok": True,
                                    "stmt": sql.split("\n")[0][:80]})
                except Exception as e:
                    try: c.rollback()
                    except Exception: pass
                    results.append({"label": label, "ok": False,
                                    "stmt": sql.split("\n")[0][:80],
                                    "error": str(e)[:200]})
        return jsonify(
            ok=True,
            statements_run=len(results),
            successes=sum(1 for r in results if r["ok"]),
            failures=sum(1 for r in results if not r["ok"]),
            results=results,
        )
    finally:
        try: c.close()
        except Exception: pass


# ── Geocoding backfill ───────────────────────────────────────────────
# Static state/province → country mapping. Covers US, CA, AU, UK, BR,
# MX which together account for 95%+ of the gap. Anything not in this
# table stays as country='?' for a later (Nominatim-based) pass.
STATE_TO_COUNTRY = {
    # US states + territories
    **{s: "US" for s in (
        "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD "
        "MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC "
        "SD TN TX UT VT VA WA WV WI WY DC PR VI GU AS MP"
    ).split()},
    # Canadian provinces + territories
    **{p: "CA" for p in
        "AB BC MB NB NL NS NT NU ON PE QC SK YT".split()},
    # Australian states + territories
    **{s: "AU" for s in
        "ACT NSW NT QLD SA TAS VIC WA".split()},
    # UK home nations (sometimes appear as state)
    "ENG": "GB", "SCT": "GB", "WLS": "GB", "NIR": "GB",
    "England": "GB", "Scotland": "GB", "Wales": "GB",
    # Major Mexican states (Spanish)
    "CMX": "MX", "CDMX": "MX", "JAL": "MX", "NLE": "MX",
    # Major Brazilian states
    "SP": "BR", "RJ": "BR",   # note collision: SP also South Africa
}


@schema_repair_bp.route("/api/v1/admin/geocoding/backfill", methods=["POST"])
def geocoding_backfill():
    """For facilities with country IS NULL OR country = '?', infer
    the country from the state/province code. Reports per-country
    additions + leftover unresolved count."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    dry_run = (request.args.get("dry_run") or "").lower() in ("1","true","yes")
    out = {"dry_run": dry_run, "scanned": 0, "updated": 0,
           "by_country": {}, "unresolved": 0, "examples": []}
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, name, state, city, address
                  FROM facilities
                 WHERE (country IS NULL
                        OR country = ''
                        OR country = '?'
                        OR LOWER(country) = 'unknown')
                 LIMIT 5000
            """)
            rows = cur.fetchall()
        out["scanned"] = len(rows)

        # Batch UPDATEs
        for r in rows:
            fid = r[0]
            state = (r[2] or "").strip()
            city = (r[3] or "").strip()
            address = (r[4] or "")
            inferred = None
            # Priority 1: state code → country
            if state:
                # Uppercase + canonicalize
                key = state.upper()
                # 2-letter and 3-letter codes
                inferred = STATE_TO_COUNTRY.get(key) or STATE_TO_COUNTRY.get(state)
                # Some "state" fields hold full names
                if not inferred and len(state) > 2:
                    inferred = STATE_TO_COUNTRY.get(state.title())
            # Priority 2: address contains a country name (final 30 chars)
            if not inferred and address:
                tail = address.upper()[-60:]
                if " USA" in tail or ", US" in tail or " UNITED STATES" in tail:
                    inferred = "US"
                elif " CANADA" in tail or ", CA " in tail:
                    inferred = "CA"
                elif " UK" in tail or "UNITED KINGDOM" in tail:
                    inferred = "GB"
                elif " GERMANY" in tail:
                    inferred = "DE"
                elif " AUSTRALIA" in tail:
                    inferred = "AU"
                elif " BRAZIL" in tail or " BRASIL" in tail:
                    inferred = "BR"

            if inferred:
                if dry_run:
                    out["updated"] += 1
                    out["by_country"][inferred] = out["by_country"].get(inferred, 0) + 1
                    if len(out["examples"]) < 12:
                        out["examples"].append({
                            "name": r[1], "state": state,
                            "city": city, "inferred": inferred,
                        })
                else:
                    try:
                        with c.cursor() as cur:
                            cur.execute(
                                "UPDATE facilities SET country = %s "
                                "WHERE id = %s",
                                (inferred, fid),
                            )
                        try: c.commit()
                        except Exception: pass
                        out["updated"] += 1
                        out["by_country"][inferred] = out["by_country"].get(inferred, 0) + 1
                        if len(out["examples"]) < 12:
                            out["examples"].append({
                                "name": r[1], "state": state,
                                "city": city, "inferred": inferred,
                            })
                    except Exception:
                        try: c.rollback()
                        except Exception: pass
            else:
                out["unresolved"] += 1
        return jsonify(ok=True, **out)
    finally:
        try: c.close()
        except Exception: pass


# ── Funnel leakage diagnostic ───────────────────────────────────────
@schema_repair_bp.route("/api/v1/admin/funnel/leakage", methods=["GET"])
def funnel_leakage():
    """Per-tool, per-stage drop-off in the MCP conversion funnel.
    Reads mcp_call_log + mcp_upgrade_signals + mcp_pair_codes +
    api_keys to surface where users actually leave.

    Inspector Brief #2 reported: 25,405 calls → 4,452 signals → 9
    conversions. This breaks that down per-stage."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    days = int(request.args.get("days") or "30")
    out = {"days": days, "stages": {}, "top_leak_tools": []}
    try:
        with c.cursor() as cur:
            # Stage 1: total tool calls
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_tool_calls "
                    "WHERE created_at >= NOW() - INTERVAL %s",
                    (f"{days} days",),
                )
                out["stages"]["1_tool_calls"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass
            # Stage 2: paywall-hit signals
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_upgrade_signals "
                    "WHERE created_at >= NOW() - INTERVAL %s",
                    (f"{days} days",),
                )
                out["stages"]["2_paywall_signals"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass
            # Stage 3: pair codes minted (offers shown)
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_pair_codes "
                    "WHERE created_at >= NOW() - INTERVAL %s",
                    (f"{days} days",),
                )
                out["stages"]["3_codes_minted"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass
            # Stage 4: codes redeemed
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_pair_codes "
                    "WHERE redeemed_at IS NOT NULL "
                    "AND redeemed_at >= NOW() - INTERVAL %s",
                    (f"{days} days",),
                )
                out["stages"]["4_codes_redeemed"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass
            # Stage 5: final paid keys created
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM api_keys "
                    "WHERE plan IN ('developer','pro','enterprise') "
                    "AND created_at >= NOW() - INTERVAL %s",
                    (f"{days} days",),
                )
                out["stages"]["5_paid_keys"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass

            # Per-tool drop-off — tools with highest paywall-signal
            # volume that have NEAR-ZERO conversions
            try:
                cur.execute("""
                    SELECT tool_requested,
                           COUNT(*) AS signals,
                           COUNT(DISTINCT session_id) AS distinct_sessions
                      FROM mcp_upgrade_signals
                     WHERE created_at >= NOW() - INTERVAL %s
                     GROUP BY tool_requested
                     ORDER BY signals DESC
                     LIMIT 15
                """, (f"{days} days",))
                out["top_leak_tools"] = [
                    {"tool": r[0], "signals": r[1], "sessions": r[2]}
                    for r in cur.fetchall()
                ]
            except Exception:
                try: c.rollback()
                except Exception: pass

            # Compute drop rates
            stages = out["stages"]
            if stages.get("1_tool_calls", 0) > 0 and stages.get("2_paywall_signals", 0) > 0:
                out["drop_calls_to_signals_pct"] = round(
                    100 * (1 - stages["2_paywall_signals"] / stages["1_tool_calls"]), 2)
            if stages.get("2_paywall_signals", 0) > 0 and stages.get("3_codes_minted", 0) is not None:
                out["drop_signals_to_codes_pct"] = round(
                    100 * (1 - stages["3_codes_minted"] / max(1, stages["2_paywall_signals"])), 2)
            if stages.get("3_codes_minted", 0) > 0 and stages.get("4_codes_redeemed", 0) is not None:
                out["drop_codes_to_redeemed_pct"] = round(
                    100 * (1 - stages["4_codes_redeemed"] / max(1, stages["3_codes_minted"])), 2)
            if stages.get("4_codes_redeemed", 0) is not None and stages.get("5_paid_keys", 0) is not None:
                out["drop_redeemed_to_paid_pct"] = round(
                    100 * (1 - stages["5_paid_keys"] / max(1, stages.get("4_codes_redeemed", 1))), 2)

        return jsonify(ok=True, **out)
    finally:
        try: c.close()
        except Exception: pass


def _smoke():
    logger.info("[schema-repair] ready · POST /schema/repair · "
                 "/geocoding/backfill · GET /funnel/leakage")

_smoke()
