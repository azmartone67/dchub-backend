"""
exclusive_listings.py — Phase GG: Pocket / Coming-Soon listings marketplace.

The user's ask: "create a coming soon, pocket listing concept that allows
for people to get exclusive access to sites that can't be seen by the
public, pro license required."

Three visibility states per listing:
    draft   — owner is composing it; never shown publicly
    pocket  — Pro/Enterprise tier callers only; hidden from free tier
    public  — anyone

Data lives in `exclusive_listings`. Each row is a single site / off-
market opportunity with structured metadata (location, MW, asking,
contact) and a free-form jsonb for everything that doesn't fit a
fixed schema.

Endpoints:
    GET  /api/v1/listings            — feed (tier-gated visibility)
    GET  /api/v1/listings/<id>       — single listing (tier-gated)
    POST /api/v1/admin/listings      — create (admin only)
    PUT  /api/v1/admin/listings/<id> — update / promote draft→pocket→public
    DELETE /api/v1/admin/listings/<id>

Tier resolution:
    The free-tier-gate (free_tier_gate._user_from_api_key) already
    resolves an X-API-Key to a {plan: 'pro' | 'enterprise' | 'founding'}
    user. We reuse that. Falls back to JWT user, then anonymous.
"""
import json
import os
import re
import secrets
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, jsonify, request

exclusive_listings_bp = Blueprint("exclusive_listings", __name__)

ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
             or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()

# Tiers that can see `pocket` listings. Free tier sees only `public` rows.
_POCKET_TIERS = {"pro", "enterprise", "founding"}
_VALID_STATUSES = {"draft", "pocket", "public"}
_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _conn():
    import psycopg2
    return psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)


def _require_admin(fn):
    @wraps(fn)
    def w(*a, **kw):
        provided = (request.headers.get("X-Admin-Key")
                    or request.args.get("admin_key") or "").strip()
        if ADMIN_KEY and provided != ADMIN_KEY:
            return jsonify(error="unauthorized",
                           hint="X-Admin-Key header required"), 401
        return fn(*a, **kw)
    return w


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS exclusive_listings (
    id              BIGSERIAL PRIMARY KEY,
    slug            TEXT UNIQUE NOT NULL,
    title           TEXT NOT NULL,
    summary         TEXT,
    status          TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'pocket', 'public')),
    tier_required   TEXT NOT NULL DEFAULT 'pro'
                        CHECK (tier_required IN ('pro', 'enterprise', 'founding')),
    market          TEXT,
    state           TEXT,
    country         TEXT DEFAULT 'US',
    latitude        REAL,
    longitude       REAL,
    capacity_mw     REAL,
    asking_price    NUMERIC,
    asking_currency TEXT DEFAULT 'USD',
    detail          JSONB,
    contact         JSONB,
    owner_id        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_exclusive_listings_status
    ON exclusive_listings (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_exclusive_listings_market
    ON exclusive_listings (market) WHERE status <> 'draft';
"""


def _ensure_schema():
    if getattr(_ensure_schema, "_done", False):
        return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(_SCHEMA_DDL)
            c.commit()
        _ensure_schema._done = True
    except Exception as e:
        import sys
        print(f"[exclusive_listings] schema init failed: {e}", file=sys.stderr)


try:
    _ensure_schema()
except Exception:
    pass


def _resolve_caller_tier():
    """Best-effort: what tier is this request? Tries X-API-Key against
    mcp_dev_keys (PR #131's resolver), then JWT, then anonymous."""
    try:
        from free_tier_gate import _user_from_api_key
        from db_utils import try_get_db
        api_key = request.headers.get("X-API-Key", "").strip()
        if api_key:
            u = _user_from_api_key(api_key, try_get_db)
            if u and u.get("plan"):
                return u["plan"]
    except Exception:
        pass
    return "free"


def _row_to_dict(row, cols):
    d = {}
    for i, c in enumerate(cols):
        v = row[i]
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        d[c] = v
    return d


def _safe_slug(s):
    return _SLUG_RE.sub("-", (s or "").strip().lower()).strip("-")[:80]


# ─────────────────────────────────────────────────────────────────
# Public-ish: tier-gated read endpoints
# ─────────────────────────────────────────────────────────────────

@exclusive_listings_bp.route("/api/v1/listings", methods=["GET"])
def list_listings():
    """Feed of listings the caller can see.

    Free tier   → only status='public' (and a teaser count of pocket)
    Pro+        → status in ('public', 'pocket')
    ?market=    filter by market slug
    ?state=     filter by state abbr
    ?limit=N    cap (default 50, max 200)
    """
    _ensure_schema()
    tier = _resolve_caller_tier()
    can_pocket = tier in _POCKET_TIERS

    market = (request.args.get("market") or "").strip()
    state = (request.args.get("state") or "").strip().upper()
    try:
        limit = max(1, min(int(request.args.get("limit", "50")), 200))
    except ValueError:
        limit = 50

    visible = ['public']
    if can_pocket:
        visible.append('pocket')

    where = ["status = ANY(%s)"]
    params = [visible]
    if market:
        where.append("LOWER(market) = LOWER(%s)")
        params.append(market)
    if state:
        where.append("UPPER(state) = %s")
        params.append(state)
    where_sql = " AND ".join(where)

    cols = ["id", "slug", "title", "summary", "status", "tier_required",
            "market", "state", "country", "latitude", "longitude",
            "capacity_mw", "asking_price", "asking_currency", "detail",
            "contact", "created_at", "updated_at", "expires_at"]

    items = []
    pocket_count_locked = 0
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                f"""SELECT {', '.join(cols)} FROM exclusive_listings
                       WHERE {where_sql}
                       ORDER BY updated_at DESC LIMIT %s""",
                params + [limit])
            items = [_row_to_dict(r, cols) for r in cur.fetchall()]

            if not can_pocket:
                # Surface teaser count so the free tier knows there's more
                # behind the paywall.
                cur.execute(
                    "SELECT COUNT(*) FROM exclusive_listings WHERE status = 'pocket'")
                pocket_count_locked = int((cur.fetchone() or [0])[0] or 0)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200

    out = {
        "ok": True,
        "count": len(items),
        "caller_tier": tier,
        "can_see_pocket": can_pocket,
        "items": items,
    }
    if not can_pocket and pocket_count_locked:
        out["pocket_locked_count"] = pocket_count_locked
        out["upgrade_for_pocket"] = {
            "tier_required": "pro",
            "url": "https://dchub.cloud/pricing",
            "message": (f"{pocket_count_locked} exclusive off-market "
                        f"listing(s) hidden — upgrade to Pro to view."),
        }
    return jsonify(out), 200


@exclusive_listings_bp.route("/api/v1/listings/<slug_or_id>", methods=["GET"])
def get_listing(slug_or_id):
    _ensure_schema()
    tier = _resolve_caller_tier()
    can_pocket = tier in _POCKET_TIERS

    # Accept either numeric id OR slug.
    by_id = slug_or_id.isdigit()
    cols = ["id", "slug", "title", "summary", "status", "tier_required",
            "market", "state", "country", "latitude", "longitude",
            "capacity_mw", "asking_price", "asking_currency", "detail",
            "contact", "created_at", "updated_at", "expires_at"]
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                f"""SELECT {', '.join(cols)} FROM exclusive_listings
                       WHERE {'id = %s' if by_id else 'slug = %s'}""",
                (int(slug_or_id) if by_id else slug_or_id,))
            row = cur.fetchone()
        if not row:
            return jsonify(ok=False, error="not_found"), 404
        d = _row_to_dict(row, cols)
        if d["status"] == "draft":
            return jsonify(ok=False, error="not_found"), 404
        if d["status"] == "pocket" and not can_pocket:
            return jsonify(
                ok=False, error="upgrade_required", caller_tier=tier,
                tier_required="pro",
                hint=("This listing is pocket-only. Upgrade to Pro to view: "
                      "https://dchub.cloud/pricing"),
                teaser={
                    "title": d["title"], "market": d["market"],
                    "state": d["state"], "capacity_mw": d["capacity_mw"],
                },
            ), 403
        return jsonify(ok=True, listing=d, caller_tier=tier), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200


# ─────────────────────────────────────────────────────────────────
# Admin: create / update / promote / delete
# ─────────────────────────────────────────────────────────────────

@exclusive_listings_bp.route("/api/v1/admin/listings", methods=["POST"])
@_require_admin
def create_listing():
    """Create a new listing (always starts as 'draft' unless caller sets
    a different status). Body: any of the column names; jsonb fields can
    be objects."""
    _ensure_schema()
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify(ok=False, error="title required"), 400

    slug = _safe_slug(body.get("slug") or title)
    if not slug:
        slug = "site-" + secrets.token_urlsafe(6).lower()
    status = (body.get("status") or "draft").lower()
    if status not in _VALID_STATUSES:
        status = "draft"
    tier_required = (body.get("tier_required") or "pro").lower()
    if tier_required not in {"pro", "enterprise", "founding"}:
        tier_required = "pro"

    detail = body.get("detail")
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail)
    contact = body.get("contact")
    if contact is not None and not isinstance(contact, str):
        contact = json.dumps(contact)

    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO exclusive_listings
                       (slug, title, summary, status, tier_required,
                        market, state, country, latitude, longitude,
                        capacity_mw, asking_price, asking_currency,
                        detail, contact, owner_id, expires_at)
                   VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,
                           %s,%s,%s, %s::jsonb, %s::jsonb, %s, %s)
                   ON CONFLICT (slug) DO NOTHING
                   RETURNING id, slug""",
                (slug, title, body.get("summary"), status, tier_required,
                 body.get("market"), body.get("state"),
                 body.get("country", "US"),
                 body.get("latitude"), body.get("longitude"),
                 body.get("capacity_mw"),
                 body.get("asking_price"),
                 body.get("asking_currency", "USD"),
                 detail, contact,
                 body.get("owner_id"), body.get("expires_at")))
            row = cur.fetchone()
            c.commit()
        if not row:
            return jsonify(ok=False, error="slug already exists",
                           slug=slug), 409
        return jsonify(ok=True, id=row[0], slug=row[1], status=status), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200


@exclusive_listings_bp.route("/api/v1/admin/listings/<int:lid>",
                              methods=["PUT", "PATCH"])
@_require_admin
def update_listing(lid):
    """Partial update. Pass any subset of fields. To promote a draft
    to pocket: {"status": "pocket"}. To make pocket public: {"status": "public"}."""
    _ensure_schema()
    body = request.get_json(silent=True) or {}
    SETTABLE = {"title", "summary", "status", "tier_required", "market",
                "state", "country", "latitude", "longitude", "capacity_mw",
                "asking_price", "asking_currency", "owner_id", "expires_at"}
    JSONB_FIELDS = {"detail", "contact"}

    fields = []
    values = []
    for k, v in body.items():
        if k in SETTABLE:
            fields.append(f"{k} = %s")
            values.append(v)
        elif k in JSONB_FIELDS:
            fields.append(f"{k} = %s::jsonb")
            values.append(v if isinstance(v, str) else json.dumps(v))

    if "status" in body and body["status"] not in _VALID_STATUSES:
        return jsonify(ok=False, error="invalid status",
                       allowed=list(_VALID_STATUSES)), 400

    if not fields:
        return jsonify(ok=False, error="no fields to update"), 400

    fields.append("updated_at = NOW()")
    values.append(lid)
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                f"UPDATE exclusive_listings SET {', '.join(fields)} "
                f"WHERE id = %s RETURNING id, slug, status",
                values)
            row = cur.fetchone()
            c.commit()
        if not row:
            return jsonify(ok=False, error="not_found"), 404
        return jsonify(ok=True, id=row[0], slug=row[1], status=row[2]), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200


@exclusive_listings_bp.route("/api/v1/admin/listings/<int:lid>", methods=["DELETE"])
@_require_admin
def delete_listing(lid):
    _ensure_schema()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM exclusive_listings WHERE id = %s", (lid,))
            n = cur.rowcount
            c.commit()
        return jsonify(ok=True, deleted=n), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200


@exclusive_listings_bp.route("/api/v1/listings/health", methods=["GET"])
def listings_health():
    _ensure_schema()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """SELECT status, COUNT(*) FROM exclusive_listings
                    GROUP BY status""")
            by_status = {r[0]: int(r[1]) for r in cur.fetchall()}
        return jsonify(status="ok", by_status=by_status), 200
    except Exception as e:
        return jsonify(status="error", error=str(e)[:200]), 200
