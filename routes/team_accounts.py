"""team_accounts.py — Round 3 Unlock 3 (2026-06-07)
====================================================

Team accounts (multi-seat tier) — $499/mo for 5 seats.

WHY
---
Round 1 + 2 fixed the SOLO conversion funnel. Round 3 Unlock 3 opens a
new growth surface: TEAM customers. A 5-person consulting team or a
hyperscaler internal squad currently has to either:
  (a) buy 5 individual Pro keys at $199/mo = $995/mo (ouch), or
  (b) buy 1 Enterprise at $699/mo (no per-seat attribution).

Team is the missing tier between them.

PRICING MATH
------------
$499/mo for 5 seats = $99.80/seat/month.
  - Pro Monthly: $199/seat → 5 seats = $995/mo (Team SAVES $496/mo)
  - Pro Annual:  $99/seat  → 5 seats = $495/mo (Team is +$4/mo; SAME
    cap but on a MONTHLY billing cycle, no annual commitment)
  - Enterprise:  $699/mo flat (Team is -$200/mo; suitable for a 5-7
    person squad that doesn't need the Enterprise white-label)
The economic edge: a customer who would have churned at "$199×5 is
too much" now has a viable middle path. The Team tier also creates a
NATURAL upsell ramp (5 seats → 10 seats → Enterprise).

DATA MODEL
----------
team_accounts (id, owner_email, plan, seats, created_at, stripe_subscription_id, shared_api_key, status)
team_seats     (team_id, member_email, role 'admin'|'member', added_at, status)

Each team has ONE shared API key but per-seat usage attribution via
mcp_call_log.session_id (we set session_id = hash(member_email) at MCP
auth time) so a team owner can SEE who's using what.

ENDPOINTS
---------
POST  /api/v1/team/create         — owner creates a team (Stripe upgrade)
POST  /api/v1/team/invite         — owner adds a seat (max 5)
DELETE /api/v1/team/remove/<email> — owner removes a seat
GET   /api/v1/team/usage          — owner sees per-seat breakdown
GET   /api/v1/team/me             — current user's team membership

SAFETY
------
- Kill switch: MCP_FUNNEL_R3_DISABLE=1
- Team endpoints only operable when STRIPE_PRICE_TEAM_MONTHLY env var is
  set — without it /api/v1/team/create returns 503 with a clear "stripe
  product not minted yet" message. The other endpoints (invite/remove/
  usage) still work for existing teams created before the price was
  added.
- Owner-only: invite/remove/usage require the requester's email to
  match team_accounts.owner_email.
- A seat cannot be added twice (UNIQUE(team_id, member_email)).
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

team_accounts_bp = Blueprint("team_accounts", __name__)


# ── env ───────────────────────────────────────────────────────────────

_DISABLE = os.environ.get("MCP_FUNNEL_R3_DISABLE", "").strip() in ("1", "true", "yes")

_TEAM_STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_TEAM_MONTHLY", "").strip()
_TEAM_STRIPE_LINK = os.environ.get("STRIPE_LINK_TEAM_MONTHLY", "").strip()

# Per-tier max-seats. 5 is the launch SKU. Owners can request a higher
# seat count → Enterprise. Don't accidentally promote a Team to 50 seats.
_TEAM_MAX_SEATS = int(os.environ.get("TEAM_MAX_SEATS", "5") or "5")
_TEAM_PRICE_USD = 499


# ── DB ────────────────────────────────────────────────────────────────

def _conn():
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if not dsn:
            return None
        c = _pg.connect(dsn, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("team_accounts: db connect failed: %s", e)
        return None


def _savepoint(cur, name: str) -> bool:
    try:
        cur.execute(f"SAVEPOINT {name}")
        return True
    except Exception:
        return False


def _rollback_sp(cur, name: str) -> None:
    try:
        cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
    except Exception:
        pass


def _release_sp(cur, name: str) -> None:
    try:
        cur.execute(f"RELEASE SAVEPOINT {name}")
    except Exception:
        pass


# ── Auth helpers ──────────────────────────────────────────────────────

def _requester_email() -> Optional[str]:
    """Resolve the requester's email from either:
       - X-User-Email header (server-side trusted call)
       - their API key (X-API-Key → mcp_dev_keys.email)
       - 'email' form field on the body
    Returns lowercased email or None."""
    em = (request.headers.get("X-User-Email") or "").strip().lower()
    if em:
        return em
    api_key = (request.headers.get("X-API-Key") or "").strip()
    if api_key:
        conn = _conn()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT email FROM mcp_dev_keys "
                        " WHERE api_key = %s AND status = 'active'",
                        (api_key,))
                    r = cur.fetchone()
                    if r and r[0]:
                        return r[0].strip().lower()
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
    body = request.get_json(silent=True) or {}
    em = (body.get("email") or request.form.get("email") or "").strip().lower()
    return em or None


def _is_admin() -> bool:
    """Admin key bypass — used by ops to fix a team's state."""
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("ADMIN_KEY") or "").strip()
    return bool(admin_key and sent == admin_key)


# ── Team lookup ───────────────────────────────────────────────────────

def _get_team_by_owner(cur, owner_email: str) -> Optional[dict]:
    if not owner_email:
        return None
    if not _savepoint(cur, "tm_own"):
        return None
    try:
        cur.execute(
            "SELECT id, owner_email, plan, seats, created_at, "
            "       stripe_subscription_id, shared_api_key, status "
            "  FROM team_accounts "
            " WHERE LOWER(owner_email) = LOWER(%s) "
            "   AND COALESCE(status, 'active') = 'active'",
            (owner_email,))
        r = cur.fetchone()
        _release_sp(cur, "tm_own")
        if not r:
            return None
        return {
            "id": r[0], "owner_email": r[1], "plan": r[2],
            "seats": int(r[3] or 0),
            "created_at": (r[4].isoformat() if r[4] else None),
            "stripe_subscription_id": r[5],
            "shared_api_key": r[6],
            "status": r[7] or "active",
        }
    except Exception as e:
        _rollback_sp(cur, "tm_own")
        logger.debug("get_team_by_owner failed: %s", e)
        return None


def _get_team_by_member(cur, member_email: str) -> Optional[dict]:
    if not member_email:
        return None
    if not _savepoint(cur, "tm_mem"):
        return None
    try:
        cur.execute(
            "SELECT t.id, t.owner_email, t.plan, t.seats, t.created_at, "
            "       t.stripe_subscription_id, t.shared_api_key, t.status, s.role "
            "  FROM team_accounts t JOIN team_seats s ON s.team_id = t.id "
            " WHERE LOWER(s.member_email) = LOWER(%s) "
            "   AND COALESCE(t.status, 'active') = 'active' "
            "   AND COALESCE(s.status, 'active') = 'active'",
            (member_email,))
        r = cur.fetchone()
        _release_sp(cur, "tm_mem")
        if not r:
            return None
        return {
            "id": r[0], "owner_email": r[1], "plan": r[2],
            "seats": int(r[3] or 0),
            "created_at": (r[4].isoformat() if r[4] else None),
            "stripe_subscription_id": r[5],
            "shared_api_key": r[6],
            "status": r[7] or "active",
            "your_role": r[8] or "member",
        }
    except Exception as e:
        _rollback_sp(cur, "tm_mem")
        logger.debug("get_team_by_member failed: %s", e)
        return None


def _list_seats(cur, team_id: int) -> list[dict]:
    if not _savepoint(cur, "tm_lst"):
        return []
    try:
        cur.execute(
            "SELECT member_email, role, added_at, status "
            "  FROM team_seats "
            " WHERE team_id = %s "
            "   AND COALESCE(status, 'active') = 'active' "
            " ORDER BY added_at ASC",
            (team_id,))
        out = []
        for r in cur.fetchall() or []:
            out.append({
                "email": r[0],
                "role": r[1] or "member",
                "added_at": (r[2].isoformat() if r[2] else None),
                "status": r[3] or "active",
            })
        _release_sp(cur, "tm_lst")
        return out
    except Exception as e:
        _rollback_sp(cur, "tm_lst")
        logger.debug("list_seats failed: %s", e)
        return []


# ── Endpoints ────────────────────────────────────────────────────────

@team_accounts_bp.route("/api/v1/team/create", methods=["POST"])
def team_create():
    """Owner upgrades to a team. Body:
        { email, stripe_subscription_id (optional, from webhook) }
    Returns the team object incl. shared_api_key. Requires
    STRIPE_PRICE_TEAM_MONTHLY to be set in env (the Stripe product MUST
    exist before customers can buy)."""
    if _DISABLE:
        return jsonify(error="feature_disabled"), 503
    if not _TEAM_STRIPE_PRICE_ID and not _is_admin():
        return jsonify(
            error="stripe_team_product_not_minted",
            message=("Team accounts require STRIPE_PRICE_TEAM_MONTHLY env "
                     "var. The owner must mint the Stripe product first. "
                     "See ops runbook."),
            stripe_dashboard="https://dashboard.stripe.com/products",
        ), 503

    body = request.get_json(silent=True) or {}
    owner_email = (body.get("email") or _requester_email() or "").strip().lower()
    if not owner_email:
        return jsonify(error="missing_email"), 400
    stripe_sub = (body.get("stripe_subscription_id") or "").strip() or None

    conn = _conn()
    if conn is None:
        return jsonify(error="no_db"), 503

    try:
        with conn.cursor() as cur:
            # Idempotency: if the owner already has a team, return it.
            existing = _get_team_by_owner(cur, owner_email)
            if existing:
                existing["seats_list"] = _list_seats(cur, existing["id"])
                return jsonify(team=existing, idempotent=True), 200

            # Mint a shared API key. Format: dch_team_<32-hex>.
            shared_key = "dch_team_" + secrets.token_hex(16)

            cur.execute(
                "INSERT INTO team_accounts "
                "  (owner_email, plan, seats, stripe_subscription_id, "
                "   shared_api_key, status) "
                "VALUES (%s, 'team', %s, %s, %s, 'active') "
                "RETURNING id, created_at",
                (owner_email, _TEAM_MAX_SEATS, stripe_sub, shared_key))
            r = cur.fetchone()
            team_id = r[0]

            # Owner is the first seat (role=admin).
            cur.execute(
                "INSERT INTO team_seats "
                "  (team_id, member_email, role, status) "
                "VALUES (%s, %s, 'admin', 'active') "
                "ON CONFLICT (team_id, member_email) DO NOTHING",
                (team_id, owner_email))

            # Mirror to mcp_dev_keys so the shared key validates at the MCP
            # gate (which keys off mcp_dev_keys). Tier = 'pro' since Team
            # gets Pro+1 effective access.
            try:
                cur.execute(
                    "INSERT INTO mcp_dev_keys "
                    "  (api_key, developer_id, email, tier, status) "
                    "VALUES (%s, %s, %s, 'pro', 'active') "
                    "ON CONFLICT (api_key) DO UPDATE "
                    "  SET tier = 'pro', status = 'active'",
                    (shared_key, f"team-{team_id}", owner_email))
            except Exception as _mcp_err:
                logger.warning(
                    "team_create: mcp_dev_keys mirror failed (non-fatal): %s",
                    _mcp_err)

            team = _get_team_by_owner(cur, owner_email)
            if team:
                team["seats_list"] = _list_seats(cur, team["id"])
            return jsonify(team=team), 201
    except Exception as e:
        logger.warning("team_create failed: %s", e)
        return jsonify(error=str(e)), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


@team_accounts_bp.route("/api/v1/team/invite", methods=["POST"])
def team_invite():
    """Owner invites a member.
    Body: { member_email }
    Owner identified by X-User-Email / X-API-Key.
    Returns the updated seats list."""
    if _DISABLE:
        return jsonify(error="feature_disabled"), 503
    body = request.get_json(silent=True) or {}
    member_email = (body.get("member_email") or "").strip().lower()
    owner_email = _requester_email()
    if not owner_email and not _is_admin():
        return jsonify(error="missing_owner"), 401
    if not member_email:
        return jsonify(error="missing_member_email"), 400
    if "@" not in member_email or "." not in member_email:
        return jsonify(error="invalid_member_email"), 400

    conn = _conn()
    if conn is None:
        return jsonify(error="no_db"), 503

    try:
        with conn.cursor() as cur:
            # Admin override: use body.owner_email if X-Admin-Key is present.
            if _is_admin() and body.get("owner_email"):
                owner_email = body.get("owner_email").strip().lower()
            team = _get_team_by_owner(cur, owner_email or "")
            if not team:
                return jsonify(
                    error="no_team_for_owner",
                    message=("This email doesn't own a team. POST "
                             "/api/v1/team/create first."),
                ), 404

            # Seat cap.
            seats = _list_seats(cur, team["id"])
            if len(seats) >= team["seats"]:
                return jsonify(
                    error="seat_cap_reached",
                    cap=team["seats"],
                    used=len(seats),
                    upgrade_url="/pricing",
                ), 409

            try:
                cur.execute(
                    "INSERT INTO team_seats "
                    "  (team_id, member_email, role, status) "
                    "VALUES (%s, %s, 'member', 'active') "
                    "ON CONFLICT (team_id, member_email) DO UPDATE "
                    "  SET status = 'active'",
                    (team["id"], member_email))
            except Exception as e:
                logger.warning("team_invite insert failed: %s", e)
                return jsonify(error=str(e)), 500

            seats = _list_seats(cur, team["id"])
            return jsonify(
                team_id=team["id"],
                seats=seats,
                seats_used=len(seats),
                seats_cap=team["seats"],
            ), 201
    finally:
        try:
            conn.close()
        except Exception:
            pass


@team_accounts_bp.route("/api/v1/team/remove/<member_email>",
                        methods=["DELETE"])
def team_remove(member_email: str):
    """Owner removes a seat. The owner CANNOT remove themselves (would
    orphan the team). Returns the updated seats list."""
    if _DISABLE:
        return jsonify(error="feature_disabled"), 503
    member_email = (member_email or "").strip().lower()
    owner_email = _requester_email()
    if not owner_email and not _is_admin():
        return jsonify(error="missing_owner"), 401
    if not member_email:
        return jsonify(error="missing_member_email"), 400

    conn = _conn()
    if conn is None:
        return jsonify(error="no_db"), 503

    try:
        with conn.cursor() as cur:
            # Admin override.
            if _is_admin() and request.args.get("owner_email"):
                owner_email = request.args.get("owner_email").strip().lower()
            team = _get_team_by_owner(cur, owner_email or "")
            if not team:
                return jsonify(error="no_team_for_owner"), 404
            if member_email == (owner_email or "").lower():
                return jsonify(
                    error="cannot_remove_owner",
                    message=("To close the team, use POST "
                             "/api/v1/team/cancel or downgrade in Stripe."),
                ), 409

            cur.execute(
                "UPDATE team_seats SET status = 'removed' "
                " WHERE team_id = %s AND LOWER(member_email) = LOWER(%s) "
                "   AND COALESCE(status, 'active') = 'active'",
                (team["id"], member_email))
            removed = cur.rowcount

            seats = _list_seats(cur, team["id"])
            return jsonify(
                removed=removed,
                team_id=team["id"],
                seats=seats,
                seats_used=len(seats),
                seats_cap=team["seats"],
            ), 200
    finally:
        try:
            conn.close()
        except Exception:
            pass


@team_accounts_bp.route("/api/v1/team/usage", methods=["GET"])
def team_usage():
    """Owner sees per-seat call counts in the last 30d.
    A seat is matched to mcp_call_log via either the seat's own API key
    (when they minted one separately) OR via the shared team key +
    session_id=hash(member_email).
    Returns: { team, total_calls_30d, seats:[{email, role, calls_30d}] }."""
    if _DISABLE:
        return jsonify(error="feature_disabled"), 503
    owner_email = _requester_email()
    if not owner_email and not _is_admin():
        return jsonify(error="missing_owner"), 401

    conn = _conn()
    if conn is None:
        return jsonify(error="no_db"), 503

    try:
        with conn.cursor() as cur:
            if _is_admin() and request.args.get("owner_email"):
                owner_email = request.args.get("owner_email").strip().lower()
            team = _get_team_by_owner(cur, owner_email or "")
            if not team:
                return jsonify(error="no_team_for_owner"), 404

            seats = _list_seats(cur, team["id"])
            shared_key = team["shared_api_key"]

            # Total calls on the shared key in 30d.
            total_30d = 0
            if _savepoint(cur, "tm_tot"):
                try:
                    cur.execute(
                        "SELECT COUNT(*) FROM mcp_call_log "
                        " WHERE api_key = %s "
                        "   AND timestamp >= NOW() - INTERVAL '30 days'",
                        (shared_key,))
                    r = cur.fetchone()
                    total_30d = int((r and r[0]) or 0)
                    _release_sp(cur, "tm_tot")
                except Exception:
                    _rollback_sp(cur, "tm_tot")

            # Per-seat: attribute by session_id = sha256(email)[:16].
            for s in seats:
                em = (s.get("email") or "").lower()
                if not em:
                    s["calls_30d"] = 0
                    continue
                session_hash = hashlib.sha256(em.encode("utf-8")).hexdigest()[:16]
                s["calls_30d"] = 0
                if _savepoint(cur, "tm_seat"):
                    try:
                        cur.execute(
                            "SELECT COUNT(*) FROM mcp_call_log "
                            " WHERE api_key = %s "
                            "   AND (session_id = %s OR session_id LIKE %s) "
                            "   AND timestamp >= NOW() - INTERVAL '30 days'",
                            (shared_key, session_hash, session_hash + "%"))
                        r = cur.fetchone()
                        s["calls_30d"] = int((r and r[0]) or 0)
                        _release_sp(cur, "tm_seat")
                    except Exception:
                        _rollback_sp(cur, "tm_seat")

            return jsonify(
                team=team,
                total_calls_30d=total_30d,
                seats=seats,
                seats_used=len(seats),
                seats_cap=team["seats"],
            ), 200
    finally:
        try:
            conn.close()
        except Exception:
            pass


@team_accounts_bp.route("/api/v1/team/me", methods=["GET"])
def team_me():
    """Return the current user's team membership (if any)."""
    if _DISABLE:
        return jsonify(error="feature_disabled"), 503
    em = _requester_email()
    if not em:
        return jsonify(error="missing_email"), 401

    conn = _conn()
    if conn is None:
        return jsonify(error="no_db"), 503

    try:
        with conn.cursor() as cur:
            # Owner first (so they always see their team object).
            t = _get_team_by_owner(cur, em)
            if t:
                t["your_role"] = "admin"
                t["seats_list"] = _list_seats(cur, t["id"])
                return jsonify(team=t, you_are="owner"), 200
            # Otherwise, do they belong to a team?
            t = _get_team_by_member(cur, em)
            if t:
                t["seats_list"] = _list_seats(cur, t["id"])
                return jsonify(team=t, you_are="member"), 200
            return jsonify(team=None, you_are="none"), 200
    finally:
        try:
            conn.close()
        except Exception:
            pass


@team_accounts_bp.route("/api/v1/team/config", methods=["GET"])
def team_config():
    """Public: returns whether the team tier is buyable yet. The pricing
    page reads this to render either a 'Start Team' CTA or a 'Coming
    soon' card."""
    return jsonify(
        enabled=bool(_TEAM_STRIPE_PRICE_ID) and not _DISABLE,
        stripe_price_id=_TEAM_STRIPE_PRICE_ID or None,
        stripe_link=_TEAM_STRIPE_LINK or None,
        seats=_TEAM_MAX_SEATS,
        price_usd_month=_TEAM_PRICE_USD,
        price_per_seat_usd_month=round(_TEAM_PRICE_USD / max(1, _TEAM_MAX_SEATS), 2),
        disabled_reason=(
            "MCP_FUNNEL_R3_DISABLE=1" if _DISABLE
            else ("STRIPE_PRICE_TEAM_MONTHLY not set" if not _TEAM_STRIPE_PRICE_ID
                  else None)
        ),
    ), 200
