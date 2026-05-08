"""
admin_ai_deals.py - Flask blueprint for /api/v1/ai-deals
Maps deal-ma-tracker's API contract to the EXISTING ai_deals table schema:

  api field       -> column            type / notes
  -------------------------------------------------
  title           -> (not stored; merged into description if provided)
  target          -> seller            (NOT NULL)
  acquirer        -> buyer             (NOT NULL)
  value_usd       -> deal_value_usd    (numeric, nullable)
  (derived)       -> deal_value_str    ("$5.2B" display)
  deal_type       -> deal_type         (varchar(50))
  confidence      -> confidence        (real, 0-1)
  announced_date  -> deal_date         (date NOT NULL)
  source_url      -> source_url        (text)
  notes           -> description       (text)
  (auto-sha256)   -> deal_hash         (varchar(64) NOT NULL, dedupe key)
  (auto)          -> ai_detected       (TRUE)
  (auto)          -> status            ('active')
  (auto)          -> ingestion_batch   ('deal-ma-tracker-YYYY-MM-DD')
  (auto)          -> created_at, updated_at
"""

import hashlib
import hmac
import os
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Optional

import psycopg2 as _pg
from flask import Blueprint, jsonify, request


admin_ai_deals_bp = Blueprint("admin_ai_deals", __name__, url_prefix="/api/v1/ai-deals")


def _dsn() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    dsn = _dsn()
    if not dsn:
        raise RuntimeError("No DATABASE_URL / NEON_DATABASE_URL env var set")
    c = _pg.connect(dsn)
    try:
        yield c
    finally:
        c.close()


# Defensive: ensure unique index on deal_hash exists so ON CONFLICT works.
# Idempotent — safe on every call but cached after first success.
INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS ix_ai_deals_deal_hash_unique
    ON ai_deals (deal_hash);
"""


def _ensure_index() -> None:
    if getattr(_ensure_index, "_done", False):
        return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(INDEX_SQL)
            c.commit()
    except Exception:
        # If we can't create the index (perms, etc), let the route handler
        # surface the underlying error -- don't crash startup.
        pass
    _ensure_index._done = True  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _check_auth() -> Optional[tuple]:
    expected = os.environ.get("DCHUB_ADMIN_SECRET", "dchub-admin-secret-2026")
    presented = ""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        presented = auth[len("Bearer "):]
    elif request.headers.get("X-Admin-Key"):
        presented = request.headers["X-Admin-Key"]
    if not presented or not hmac.compare_digest(presented, expected):
        return jsonify(error="unauthorized"), 401
    return None


# ---------------------------------------------------------------------------
# Validation + field mapping
# ---------------------------------------------------------------------------

VALID_TYPES = {
    "equity", "M&A", "debt", "capex", "JV",
    "AI-contract", "AI-infra", "land", "power-agreement", "other",
}


def _format_deal_value_str(v: Optional[float]) -> Optional[str]:
    """5_200_000_000 -> '$5.2B', 500_000_000 -> '$500M', 1_500_000 -> '$1.5M'."""
    if v is None:
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n >= 1_000_000_000:
        return f"${n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n / 1_000:.0f}K"
    return f"${n:.0f}"


def _build_deal_hash(buyer: str, seller: str, deal_date: date, value_usd: Optional[float]) -> str:
    raw = "|".join([
        buyer.strip().lower(),
        seller.strip().lower(),
        deal_date.isoformat(),
        f"{float(value_usd):.2f}" if value_usd is not None else "null",
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_and_map(p: dict) -> tuple[Optional[dict], Optional[str]]:
    """Validate API payload and return a dict matching the existing column names."""
    if not isinstance(p, dict):
        return None, "body must be JSON object"

    # API contract field aliases — accept either api-style or column-style names
    target  = (p.get("target")  or p.get("seller")   or "").strip()
    buyer   = (p.get("acquirer") or p.get("buyer")   or "").strip()
    title   = (p.get("title")   or "").strip()
    notes   = (p.get("notes")   or p.get("description") or "").strip()
    src     = (p.get("source_url") or "").strip() or None
    src_name= (p.get("source_name") or "").strip() or None

    if not target or len(target) > 255:
        return None, "target/seller required, <= 255 chars"
    if not buyer or len(buyer) > 255:
        return None, "acquirer/buyer required, <= 255 chars"

    deal_type = (p.get("deal_type") or "").strip()
    if deal_type not in VALID_TYPES:
        return None, f"deal_type must be one of {sorted(VALID_TYPES)}"
    if len(deal_type) > 50:
        return None, "deal_type too long"

    deal_date = p.get("announced_date") or p.get("deal_date")
    if isinstance(deal_date, str):
        try:
            deal_date = date.fromisoformat(deal_date)
        except ValueError:
            return None, "announced_date must be YYYY-MM-DD"
    elif not isinstance(deal_date, date):
        return None, "announced_date required (YYYY-MM-DD)"
    if deal_date > date.today():
        return None, "announced_date cannot be in the future"

    value_usd = p.get("value_usd") if "value_usd" in p else p.get("deal_value_usd")
    if value_usd is not None:
        try:
            value_usd = float(value_usd)
            if value_usd < 0 or value_usd > 1e13:
                return None, "value_usd out of plausible range"
        except (TypeError, ValueError):
            return None, "value_usd must be numeric"

    confidence = p.get("confidence", 0.5)
    try:
        confidence = float(confidence)
        if not (0.0 <= confidence <= 1.0):
            return None, "confidence must be between 0 and 1"
    except (TypeError, ValueError):
        return None, "confidence must be numeric"

    region = (p.get("region") or "").strip() or None
    market = (p.get("market") or "").strip() or None
    if region and len(region) > 100:
        region = region[:100]
    if market and len(market) > 255:
        market = market[:255]

    # Build description: prefer explicit notes; fall back to title; else None
    description = notes or (title or None)

    deal_hash = _build_deal_hash(buyer, seller=target, deal_date=deal_date, value_usd=value_usd)
    deal_value_str = _format_deal_value_str(value_usd)
    ingestion_batch = f"deal-ma-tracker-{date.today().isoformat()}"

    return {
        "deal_hash": deal_hash,
        "buyer": buyer,
        "seller": target,
        "deal_value_usd": value_usd,
        "deal_value_str": deal_value_str,
        "deal_type": deal_type,
        "confidence": confidence,
        "deal_date": deal_date,
        "region": region,
        "market": market,
        "source_url": src,
        "source_name": src_name,
        "description": description,
        "ai_detected": True,
        "status": "active",
        "ingestion_batch": ingestion_batch,
    }, None


# ---------------------------------------------------------------------------
# POST -- insert (or upsert by deal_hash)
# ---------------------------------------------------------------------------

@admin_ai_deals_bp.route("", methods=["POST"])
def insert_deal():
    err = _check_auth()
    if err is not None:
        return err

    _ensure_index()

    payload, msg = _validate_and_map(request.get_json(silent=True) or {})
    if payload is None:
        return jsonify(error=msg), 400

    sql = """
        INSERT INTO ai_deals (
            deal_hash, buyer, seller,
            deal_value_usd, deal_value_str, deal_type, confidence,
            deal_date, region, market,
            source_url, source_name, description,
            ai_detected, status, ingestion_batch,
            created_at, updated_at
        )
        VALUES (
            %(deal_hash)s, %(buyer)s, %(seller)s,
            %(deal_value_usd)s, %(deal_value_str)s, %(deal_type)s, %(confidence)s,
            %(deal_date)s, %(region)s, %(market)s,
            %(source_url)s, %(source_name)s, %(description)s,
            %(ai_detected)s, %(status)s, %(ingestion_batch)s,
            NOW(), NOW()
        )
        ON CONFLICT (deal_hash) DO UPDATE SET
            confidence       = GREATEST(ai_deals.confidence, EXCLUDED.confidence),
            description      = COALESCE(EXCLUDED.description, ai_deals.description),
            source_url       = COALESCE(EXCLUDED.source_url, ai_deals.source_url),
            source_name      = COALESCE(EXCLUDED.source_name, ai_deals.source_name),
            ingestion_batch  = EXCLUDED.ingestion_batch,
            updated_at       = NOW()
        RETURNING id, (xmax = 0) AS inserted, deal_hash;
    """
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(sql, payload)
            row = cur.fetchone()
            c.commit()
    except Exception as e:
        return jsonify(error=f"insert failed: {type(e).__name__}: {e}"), 500

    deal_id, was_inserted, deal_hash = row[0], bool(row[1]), row[2]
    return jsonify(
        id=deal_id,
        deal_hash=deal_hash,
        status="inserted" if was_inserted else "merged",
        deal={
            **{k: v for k, v in payload.items() if k != "deal_date"},
            "deal_date": payload["deal_date"].isoformat(),
        },
    ), (201 if was_inserted else 200)


# ---------------------------------------------------------------------------
# GET -- list (returns existing schema verbatim)
# ---------------------------------------------------------------------------

@admin_ai_deals_bp.route("", methods=["GET"])
def list_deals():
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 500))
    except ValueError:
        return jsonify(error="limit must be int"), 400

    since = request.args.get("since")
    args: dict = {"limit": limit}
    where = ""
    if since:
        try:
            args["since"] = date.fromisoformat(since)
            where = "WHERE deal_date >= %(since)s"
        except ValueError:
            return jsonify(error="since must be YYYY-MM-DD"), 400

    sql = f"""
        SELECT id, deal_hash, buyer, seller,
               deal_value_usd, deal_value_str, deal_type, confidence,
               deal_date, region, market,
               source_url, source_name, description,
               ai_detected, status, ingestion_batch,
               created_at, updated_at
        FROM ai_deals
        {where}
        ORDER BY deal_date DESC, deal_value_usd DESC NULLS LAST, id DESC
        LIMIT %(limit)s;
    """
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(sql, args)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        return jsonify(error=f"list failed: {type(e).__name__}: {e}"), 500

    for r in rows:
        if isinstance(r.get("deal_date"), date):
            r["deal_date"] = r["deal_date"].isoformat()
        if isinstance(r.get("created_at"), datetime):
            r["created_at"] = r["created_at"].isoformat()
        if isinstance(r.get("updated_at"), datetime):
            r["updated_at"] = r["updated_at"].isoformat()
        if r.get("deal_value_usd") is not None:
            r["deal_value_usd"] = float(r["deal_value_usd"])
        if r.get("confidence") is not None:
            r["confidence"] = float(r["confidence"])

    return jsonify(count=len(rows), deals=rows), 200


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@admin_ai_deals_bp.route("/health", methods=["GET"])
def health():
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*), MAX(deal_date) FROM ai_deals;")
            count, latest = cur.fetchone()
        return jsonify(
            status="ok",
            count=int(count or 0),
            latest_deal_date=latest.isoformat() if latest else None,
            schema="existing",
        ), 200
    except Exception as e:
        return jsonify(error=f"health failed: {type(e).__name__}: {e}"), 500
