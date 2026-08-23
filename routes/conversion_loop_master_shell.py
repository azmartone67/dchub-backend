"""
conversion_loop_master_shell.py — Agent→paid conversion-loop master shell (2026-07-04).
=========================================================================================

WHY
---
DC Hub's whole thesis is agent→paid with NO human reconnect. That loop is two
moves, both deployed 2026-06-24:

  · MOVE #2 — AUTO-REDEEM: at a high-intent moment (a repeat Pro-tool siting
              workflow) the gateway auto-redeems the signed claim_token and binds
              a working 7d/50-call trial key FOR THE AGENT, inline — no human
              page-open. Measured by claims_redeemed_30d / claims_with_key_30d on
              /api/v1/mcp/high-intent/stats (was claims_used_30d until 2026-07-27,
              when that key was repointed at the human claim_page_opened_at
              instrument — see r-used-is-human).
  · MOVE #3 — KEY-BOUND UPGRADE: paying flips the agent's OWN key in place
              ($9 tier-flip via a DCM- pair-code, or $5/$10 pack credits) — no key
              swap, no copy-paste. Measured by keys_by_tier.paid / conversions_30d
              on /api/v1/mcp/funnel.

The 2026-07-03 pivot silently dropped the gateway's auto-redeem call (its evidence
argued only against RELAYING A LINK, not against binding a key inline), which
froze claims_used at 2 and left claim_to_paid at 0. The 2026-07-04 fix restored
auto-redeem in server.mjs (buildHighIntentClaimBlock) + bound the upgrade link to
the just-redeemed key so a payment attaches to THAT key. This shell exists so the
loop can never silently break again: it probes both moves every tick, scores loop
health, and surfaces the single next action.

WHAT
----
POST /api/v1/admin/conversion-loop/master-tick runs three tiers:
  · TIER 1 — MEASURE:  live probes — /high-intent/stats (Move #2 KPIs),
                       /funnel (Move #3 keys-by-tier + conversions), and a
                       liveness probe of POST /high-intent/redeem (must be 400
                       missing_token, NOT 404 — 404 = gateway route gone).
  · TIER 2 — SCORE:    classify Move #2 (firing / may-not-be-firing) and Move #3
                       (converted / not-yet) against the 2026-06-24 deploy
                       baselines; loop_healthy + loop_confirmed_e2e; a ranked
                       next-action worklist (code-actionable vs owner-gated).
  · TIER 3 — PERSIST:  one snapshot row per tick (conversion_loop_snapshots), so
                       "two days running" confirmation is a query, not a memory.
GET /api/v1/admin/conversion-loop/state returns the latest snapshot + trend.

MEASURES-AND-REPORTS ONLY. The only write is its own snapshot table (direct
cursor execute + commit — db_utils safe_db SILENTLY SKIPS DDL). It never mints,
redeems, or mutates production data.

Auth: X-Admin-Key (DCHUB_ADMIN_KEY / DCHUB_INTERNAL_KEY). Fail-closed.
Kill switch: CONVERSION_LOOP_MASTER_DISABLED=1.
"""
from __future__ import annotations

import hmac
import json
import os
import time
from datetime import datetime, timezone

import requests
from flask import Blueprint, jsonify, request

conversion_loop_master_shell_bp = Blueprint("conversion_loop_master_shell", __name__)

# Stats / funnel / redeem are all served on the public origin.
_PUBLIC = os.environ.get("DCHUB_PUBLIC_BASE", "https://dchub.cloud")

# ── 2026-06-24 deploy baselines (the day Moves #2 + #3 shipped) ───────
_BASELINE = {
    "claims_minted_30d": 25,
    # r-used-is-human (2026-07-27): this 2 was measured against the OLD
    # claims_used_30d (any-channel redeem), so it is the claims_redeemed_30d
    # baseline. claims_used_30d now means "human opened the claim page" and has
    # its own baseline — claim_page_opened_at had fired 0x all-time as of
    # 2026-06-25, so ANY nonzero human open is genuine movement.
    "claims_redeemed_30d": 2,
    "claims_used_30d": 0,
    "claim_to_paid_30d": 0,
    "paid_keys": 22,
    "conversions_30d": 8,
}


# ── auth (mirrors agent_onboarding_master_shell) ──────────────────────
def _admin_key() -> str | None:
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    expected = (_admin_key() or "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, expected)


def _disabled() -> bool:
    return str(os.environ.get("CONVERSION_LOOP_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


def _close(conn) -> None:
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


# ── tiny HTTP helpers (requests — house rule: no urllib on Railway) ───
_UA = {"User-Agent": "dchub-conversion-loop-shell"}


def _get_json(path: str, timeout: float = 8.0) -> dict:
    url = _PUBLIC.rstrip("/") + path
    try:
        r = requests.get(url, headers=_UA, timeout=timeout)
        if r.status_code >= 400:
            return {"ok": False, "status": r.status_code, "error": (r.text or "")[:400]}
        return {"ok": True, "status": r.status_code, "json": r.json()}
    except Exception as e:
        return {"ok": False, "status": None, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def _probe_redeem_status(timeout: float = 8.0) -> int | None:
    """POST /high-intent/redeem with an empty body — a LIVE gateway route returns
    400 (missing_token); 404 means the route was removed from the deploy."""
    url = _PUBLIC.rstrip("/") + "/api/v1/mcp/high-intent/redeem"
    try:
        r = requests.post(url, data=b"{}",
                          headers={"Content-Type": "application/json", **_UA},
                          timeout=timeout)
        return r.status_code
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════
# TIER 1 — MEASURE
# ══════════════════════════════════════════════════════════════════════
def tier1_measure() -> dict:
    stats = _get_json("/api/v1/mcp/high-intent/stats")
    funnel = _get_json("/api/v1/mcp/funnel")
    redeem_status = _probe_redeem_status()

    s = stats.get("json") or {} if stats.get("ok") else {}
    f = funnel.get("json") or {} if funnel.get("ok") else {}
    kbt = (f.get("keys_by_tier") or {}) if isinstance(f.get("keys_by_tier"), dict) else {}

    return {
        "stats_ok": bool(stats.get("ok")),
        "funnel_ok": bool(funnel.get("ok")),
        "redeem_route_status": redeem_status,          # 400 = alive, 404 = route gone
        # Move #2
        "claims_minted_30d": int(s.get("claims_minted_30d") or 0),
        "claims_used_30d": int(s.get("claims_used_30d") or 0),
        # r-used-is-human (2026-07-27): Move #2 asks "is the gateway auto-redeem
        # firing", which is the ANY-CHANNEL number. That used to be claims_used_30d;
        # it is now claims_redeemed_30d (claims_used_30d was repointed at the human
        # claim_page_opened_at instrument). Fall back to the old key so this shell
        # keeps scoring correctly against a backend that has not deployed the
        # rename yet — `or 0` alone would read a real 0 as "auto-redeem dead".
        "claims_redeemed_30d": int(
            (s.get("claims_redeemed_30d")
             if s.get("claims_redeemed_30d") is not None
             else s.get("claims_used_30d")) or 0),
        "claims_with_key_30d": int(s.get("claims_with_key_30d") or 0),
        "claim_email_captured_30d": int(s.get("claim_email_captured_30d") or 0),
        "claim_to_paid_30d": int(s.get("claim_to_paid_30d") or 0),
        "high_intent_sessions_30d": int(s.get("high_intent_sessions_30d") or 0),
        # Move #3
        "paid_keys": int(kbt.get("paid") or 0),
        "enterprise_keys": int(kbt.get("enterprise") or 0),
        "identified_keys": int(kbt.get("identified") or 0),
        "free_keys": int(kbt.get("free") or 0),
        "conversions_30d": int(f.get("conversions_30d") or 0),
        "raw_errors": {k: v.get("error") for k, v in
                       {"stats": stats, "funnel": funnel}.items() if not v.get("ok")},
    }


# ══════════════════════════════════════════════════════════════════════
# TIER 2 — SCORE
# ══════════════════════════════════════════════════════════════════════
def tier2_score(m: dict) -> dict:
    b = _BASELINE
    worklist = []

    # ── MOVE #2 — auto-redeem firing? ────────────────────────────────
    minted_grew = m["claims_minted_30d"] > b["claims_minted_30d"]
    # r-used-is-human (2026-07-27): compare the ANY-CHANNEL redeem count, not the
    # (now human-only) claims_used_30d — Move #2 is a gateway-health question.
    used_climbed = m["claims_redeemed_30d"] > b["claims_redeemed_30d"]
    got_key = m["claims_with_key_30d"] > b["claims_redeemed_30d"]  # working key delivered
    route_alive = m["redeem_route_status"] == 400

    if used_climbed or got_key:
        move2_status = "firing"
        move2_note = (f"claims_redeemed_30d={m['claims_redeemed_30d']} / "
                      f"claims_with_key_30d={m['claims_with_key_30d']} — above baseline "
                      f"({b['claims_redeemed_30d']}); the gateway is binding keys for "
                      f"agents. Humans who actually opened a claim page: "
                      f"claims_used_30d={m['claims_used_30d']}.")
    elif not route_alive:
        move2_status = "route_broken"
        move2_note = (f"POST /high-intent/redeem returned {m['redeem_route_status']} "
                      "(expected 400 missing_token) — the gateway redeem route is GONE. "
                      "Check the dchub-mcp-server deploy.")
        worklist.append({"move": 2, "owner_gated": False, "priority": 100,
                         "action": "Redeploy dchub-mcp-server — /high-intent/redeem is not 400."})
    elif minted_grew:
        move2_status = "not_firing"
        move2_note = (f"claims_minted_30d={m['claims_minted_30d']} grew but "
                      f"claims_redeemed_30d={m['claims_redeemed_30d']} is stuck at "
                      "baseline — claims mint but never redeem. Verify server.mjs "
                      "buildHighIntentClaimBlock calls _autoRedeemClaim and "
                      "DCHUB_AUTO_REDEEM_DISABLE is unset.")
        worklist.append({"move": 2, "owner_gated": False, "priority": 90,
                         "action": "Confirm auto-redeem is wired in server.mjs + "
                                   "DCHUB_AUTO_REDEEM_DISABLE unset; grep gateway logs "
                                   "for '[auto-redeem]' errors."})
    else:
        move2_status = "quiet"
        move2_note = ("No high-intent claims minted above baseline this window — "
                      "not broken, just no qualifying repeat-siting cohort yet.")

    # ── MOVE #3 — key-bound upgrade converted? ───────────────────────
    # ★2026-08-23: this was `paid_grew or conv_grew`, and the two are not the
    # same KIND of number. `paid_keys` is a CUMULATIVE STOCK measured against
    # a frozen baseline, so it can only ever ratchet upward — once it passed
    # 22 the OR pinned move3 to "converted" permanently, whatever sales did.
    # Measured live 2026-08-23: paid_keys 44 (> baseline 22) while
    # conversions_30d had FALLEN 8 -> 6, and the note still asserted "a
    # wall→key-bound upgrade has converted". Because move2 is likewise
    # permanently above its own baseline (684 redeems vs 2), the composite
    # read loop_score 90/100 loop_healthy=True with claim_to_paid_30d=0 —
    # a score that structurally could not fall.
    # A conversion is a FLOW. Only the flow can evidence one. The stock is
    # still reported, beside it, as context — never as the trigger.
    paid_grew = m["paid_keys"] > b["paid_keys"]
    conv_grew = m["conversions_30d"] > b["conversions_30d"]
    if m["conversions_30d"] < b["conversions_30d"]:
        conv_dir = f"FELL {b['conversions_30d']} -> {m['conversions_30d']}"
    elif conv_grew:
        conv_dir = f"grew {b['conversions_30d']} -> {m['conversions_30d']}"
    else:
        conv_dir = f"flat at {m['conversions_30d']}"
    stock_note = (f"paid_keys={m['paid_keys']} (baseline {b['paid_keys']}, "
                  "cumulative — this number cannot fall)")
    if conv_grew:
        move3_status = "converted"
        move3_note = (f"conversions_30d {conv_dir} — a wall→key-bound upgrade "
                      f"has converted. {stock_note}.")
    elif paid_grew:
        # The old "converted" branch, named for what it actually is.
        move3_status = "stock_only"
        move3_note = (f"{stock_note} grew, but conversions_30d {conv_dir}. A "
                      "cumulative key count rising is NOT evidence of a "
                      "conversion in this window — the flow is the metric this "
                      "move exists to move.")
        worklist.append({"move": 3, "owner_gated": True, "priority": 60,
                         "action": f"Paid keys grew while conversions_30d {conv_dir} — "
                                   "confirm the new keys are real sales and not "
                                   "comp/seed/NLR grants, then re-baseline."})
    else:
        move3_status = "not_yet"
        move3_note = (f"{stock_note}, conversions_30d {conv_dir} "
                      "— no key-bound upgrade above baseline yet.")
        worklist.append({"move": 3, "owner_gated": True, "priority": 40,
                         "action": "No key-bound upgrade yet — ensure high_intent_upgrade_url "
                                   "(key-bound) is surfaced and the DCM- webhook flips the key."})

    # ── end-to-end confirmation ──────────────────────────────────────
    claim_to_paid = m["claim_to_paid_30d"]
    loop_healthy = (move2_status == "firing") and (move3_status == "converted")
    loop_confirmed_e2e = claim_to_paid > 0
    if claim_to_paid > 0:
        e2e_note = (f"claim_to_paid_30d={claim_to_paid} — a REAL paid conversion has flowed "
                    "through the high-intent funnel end-to-end.")
    else:
        e2e_note = ("claim_to_paid_30d=0 — Move #3 gains (if any) came via the direct wall "
                    "path, not yet through the high-intent claim→key→pay chain.")

    # score: 50 for a firing Move #2, 40 for a converting Move #3, +10 for e2e.
    # ★2026-08-23: "stock_only" scores 0 here, exactly like "not_yet". It is a
    # more INFORMATIVE status, not a partially-successful one — the flow did
    # not move, so the move did not land.
    score = (50 if move2_status == "firing" else 0) \
        + (40 if move3_status == "converted" else 0) \
        + (10 if loop_confirmed_e2e else 0)

    worklist.sort(key=lambda x: -x["priority"])
    return {
        "loop_score": score,
        "loop_healthy": loop_healthy,
        "loop_confirmed_e2e": loop_confirmed_e2e,
        "move2": {"status": move2_status, "note": move2_note},
        "move3": {"status": move3_status, "note": move3_note,
                   "basis": {
                       "flow_metric": "conversions_30d",
                       "flow_value": m["conversions_30d"],
                       "flow_baseline": b["conversions_30d"],
                       "stock_metric": "paid_keys",
                       "stock_value": m["paid_keys"],
                       "stock_baseline": b["paid_keys"],
                       "note": ("only the FLOW can evidence a conversion; the "
                                "stock is cumulative and cannot fall"),
                   }},
        "e2e": {"claim_to_paid_30d": claim_to_paid, "note": e2e_note},
        "worklist": worklist,
        "baseline": b,
    }


# ══════════════════════════════════════════════════════════════════════
# TIER 3 — PERSIST (direct cursor DDL + INSERT; read-only elsewhere)
# ══════════════════════════════════════════════════════════════════════
def _persist(scored: dict, measures: dict) -> bool:
    conn = None
    try:
        from main import get_pg_connection
    except Exception:
        return False
    try:
        conn = get_pg_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversion_loop_snapshots (
                id             BIGSERIAL PRIMARY KEY,
                loop_score     REAL,
                loop_healthy   BOOLEAN,
                move2_status   TEXT,
                move3_status   TEXT,
                claim_to_paid  INTEGER,
                measures_json  JSONB,
                scored_json    JSONB,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute(
            # Append-only snapshot log (one row per tick, BIGSERIAL PK, no
            # natural conflict key). Whitelisted in scripts/regression_lint.py
            # alongside the other *_snapshots history tables.
            "INSERT INTO conversion_loop_snapshots "
            "(loop_score, loop_healthy, move2_status, move3_status, claim_to_paid, "
            " measures_json, scored_json) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (scored.get("loop_score"), scored.get("loop_healthy"),
             (scored.get("move2") or {}).get("status"),
             (scored.get("move3") or {}).get("status"),
             (scored.get("e2e") or {}).get("claim_to_paid_30d"),
             json.dumps(measures or {}), json.dumps(scored or {})),
        )
        conn.commit()
        return True
    except Exception:
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        return False
    finally:
        _close(conn)


def _two_days_running(loop_healthy_now: bool) -> bool:
    """True when this tick AND the most recent PRIOR tick from a different calendar
    day both report loop_healthy — the 'two days running → routine can stop' gate."""
    if not loop_healthy_now:
        return False
    conn = None
    try:
        from main import get_pg_connection
        conn = get_pg_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT loop_healthy FROM conversion_loop_snapshots "
            "WHERE created_at < NOW() - INTERVAL '12 hours' "
            "ORDER BY id DESC LIMIT 1")
        r = cur.fetchone()
        return bool(r and r[0])
    except Exception:
        return False
    finally:
        _close(conn)


# ── endpoints ─────────────────────────────────────────────────────────
@conversion_loop_master_shell_bp.route(
    "/api/v1/admin/conversion-loop/master-tick", methods=["POST", "GET"])
def master_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="CONVERSION_LOOP_MASTER_DISABLED"), 200
    started = time.time()
    measures = tier1_measure()
    scored = tier2_score(measures)
    persisted = _persist(scored, measures)
    scored["confirmed_two_days_running"] = _two_days_running(scored.get("loop_healthy"))
    return jsonify(
        ok=True,
        ms=int((time.time() - started) * 1000),
        loop_score=scored.get("loop_score"),
        loop_healthy=scored.get("loop_healthy"),
        loop_confirmed_e2e=scored.get("loop_confirmed_e2e"),
        confirmed_two_days_running=scored.get("confirmed_two_days_running"),
        move2=scored.get("move2"),
        move3=scored.get("move3"),
        e2e=scored.get("e2e"),
        worklist=scored.get("worklist"),
        measures=measures,
        persisted=persisted,
        generated_at=datetime.now(timezone.utc).isoformat(),
    ), 200


@conversion_loop_master_shell_bp.route(
    "/api/v1/admin/conversion-loop/state", methods=["GET"])
def state():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    conn = None
    try:
        from main import get_pg_connection
    except Exception:
        return jsonify(error="db_unavailable"), 503
    try:
        conn = get_pg_connection()
        cur = conn.cursor()
        try:
            cur.execute("SET LOCAL statement_timeout = 8000")
        except Exception:
            pass
        latest = None
        try:
            cur.execute(
                "SELECT id, loop_score, loop_healthy, move2_status, move3_status, "
                "claim_to_paid, scored_json, created_at "
                "FROM conversion_loop_snapshots ORDER BY id DESC LIMIT 1")
            r = cur.fetchone()
            if r:
                latest = {"id": r[0], "loop_score": r[1], "loop_healthy": r[2],
                          "move2_status": r[3], "move3_status": r[4],
                          "claim_to_paid": r[5], "scored": r[6], "created_at": str(r[7])}
        except Exception:
            pass
        trend = []
        try:
            cur.execute(
                "SELECT loop_score, loop_healthy, claim_to_paid, created_at "
                "FROM conversion_loop_snapshots ORDER BY id DESC LIMIT 30")
            trend = [{"loop_score": row[0], "loop_healthy": row[1],
                      "claim_to_paid": row[2], "created_at": str(row[3])}
                     for row in (cur.fetchall() or [])]
            trend.reverse()
        except Exception:
            pass
        return jsonify(latest=latest, trend=trend, count=len(trend)), 200
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {str(e)[:120]}"), 500
    finally:
        _close(conn)
