"""
admin_ai_deals.py - Flask blueprint for /api/v1/ai-deals
Writes to the LIVE `deals` table (the abandoned ai_deals table is no longer
the canonical source). Schema mapping below.

  api field       -> deals column      transform
  -------------------------------------------------
  (auto)          -> id                "AUTO-YYYYMMDD-<6char hex>" deterministic
  target          -> seller            as-is
  acquirer        -> buyer             as-is
  value_usd       -> value             / 1_000_000  (millions)
  announced_date  -> date              .isoformat() (text "YYYY-MM-DD")
  announced_date  -> year              .year (integer)
  deal_type       -> type              as-is (M&A, JV, equity, debt, capex, etc.)
  (derived)       -> deal_category     "transaction" or category from type
  confidence      -> extraction_confidence  as-is (0-1)
  source_url      -> source_url        as-is
  notes           -> notes             as-is
  (auto)          -> status            "active"
  (auto)          -> extracted_via     "deal-ma-tracker"
  (auto)          -> extracted_at      NOW()
  (auto)          -> created_at        NOW() ISO text
  (auto)          -> verified          0 (AI-detected, not yet human verified)
"""

import hashlib
import hmac
import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
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


@admin_ai_deals_bp.route("/restore-marquee-deals", methods=["POST"])
def restore_marquee_deals():
    """r68 RECOVERY: re-insert the 5 marquee deals that the deals-table purge
    over-deleted (the value-gate hadn't deployed). Values are the team's own
    curated figures from main.py's deal-repair block. verified=1 so they survive
    all future purges. Idempotent (ON CONFLICT (id) DO UPDATE). Gate: X-Admin-Key
    OR X-Internal-Key."""
    err = _check_auth()
    if err:
        ok = False
        try:
            from internal_auth import is_valid_internal_key
            ok = is_valid_internal_key(request.headers.get("X-Internal-Key", ""))
        except Exception:
            ok = False
        if not ok:
            return err
    # (id, date, year, buyer, seller, value $M, type, market, notes)
    MARQUEE = [
        ("AUTO-20260130-c61567", "2026-01-30", 2026, "Microsoft", "OpenAI", 13000, "investment", "Global", "Microsoft cumulative OpenAI investment"),
        ("AUTO-20260116-fe900c", "2026-01-16", 2026, "Oracle", "OpenAI (Stargate)", 100000, "jv", "Global", "Oracle Stargate AI infra commitment"),
        ("AUTO-20260206-281981", "2026-02-06", 2026, "Amazon", "Internal (capex)", 100000, "capex", "Global", "Amazon 2025 AI/cloud capex"),
        ("AUTO-20260106-d79e96", "2026-01-06", 2026, "Investor group", "xAI", 6000, "investment", "US", "xAI funding round"),
        ("AUTO-20260126-0897cb", "2026-01-26", 2026, "SoftBank", "OpenAI (Stargate)", None, "investment", "Global", "SoftBank Switch/Stargate infra investment"),
    ]
    restored = []
    try:
        with _conn() as c, c.cursor() as cur:
            for d in MARQUEE:
                cur.execute("""
                    INSERT INTO deals (id, date, year, buyer, seller, value, type, market, notes, verified, source_url, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, 1, '', NOW())
                    ON CONFLICT (id) DO UPDATE SET
                        buyer=EXCLUDED.buyer, seller=EXCLUDED.seller, value=EXCLUDED.value,
                        type=EXCLUDED.type, market=EXCLUDED.market, notes=EXCLUDED.notes,
                        verified=1, date=EXCLUDED.date, year=EXCLUDED.year
                """, d[:9])
                restored.append({"id": d[0], "deal": f"{d[3]} → {d[4]}", "value_m": d[5]})
            c.commit()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200], restored=restored), 500
    return jsonify(ok=True, restored=restored, note="verified=1 — protected from future purges"), 200


@admin_ai_deals_bp.route("/purge-deals-table", methods=["POST"])
def purge_deals_table():
    """r68 (Nico audit #1-3): clean the `deals` table — the /transactions PAGE
    source (separate from ai_deals). SCOPED TO id LIKE 'AUTO-%' so only
    auto-scraped rows are touched; curated/seed deals are never deleted. Removes:
      (a) impossible  — IPO-as-deal (type/notes ~ 'ipo') or self-deal (buyer==seller)
      (b) unsourced   — AUTO row with no source_url (can't verify)
      (c) duplicates  — same buyer+seller+value, keep the newest AUTO row
    dry_run default. Gate: X-Admin-Key (DCHUB_ADMIN_SECRET) OR X-Internal-Key.
    NOTE: the ~6 auto-writers (deal_scraper/extractor_cron/crawler/brain/auto_pilot)
    still need extraction-hardening to stop re-accumulation — flagged follow-up."""
    err = _check_auth()
    if err:
        ok = False
        try:
            from internal_auth import is_valid_internal_key
            ok = is_valid_internal_key(request.headers.get("X-Internal-Key", ""))
        except Exception:
            ok = False
        if not ok:
            return err
    dry = bool((request.get_json(silent=True) or {}).get("dry_run", True))
    # r68: NEVER purge a verified/curated deal, even at an AUTO- id — protects the
    # manually value-corrected marquee deals from over-deletion (lesson learned).
    _AUTO = "id LIKE 'AUTO-%' AND COALESCE(verified::text,'') NOT IN ('1','true','t')"
    _IMPOSSIBLE = (f"{_AUTO} AND ("
                   "LOWER(COALESCE(type,'')) LIKE '%ipo%' OR LOWER(COALESCE(notes,'')) LIKE '%ipo%' "
                   "OR (COALESCE(buyer,'')<>'' AND LOWER(COALESCE(buyer,''))=LOWER(COALESCE(seller,''))))")
    # r68: unsourced AUTO rows that ALSO have no value — pure junk. Value-gated so
    # manually value-corrected marquee deals (Microsoft/OpenAI $13B, Oracle
    # Stargate $100B at AUTO-* ids) are PRESERVED even though they lack a URL.
    _UNSRC = f"{_AUTO} AND COALESCE(source_url,'') = '' AND COALESCE(value,0) = 0"
    _DUP = f"""SELECT id FROM (
          SELECT id, ROW_NUMBER() OVER (
            PARTITION BY LOWER(COALESCE(buyer,'')), LOWER(COALESCE(seller,'')), COALESCE(value,-1)
            ORDER BY date DESC NULLS LAST, id DESC) AS rn
          FROM deals WHERE {_AUTO}) t WHERE rn > 1"""
    out = {"ok": True, "dry_run": dry, "table": "deals", "counts": {}, "deleted": {}}
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM deals"); out["counts"]["total_before"] = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM deals WHERE {_AUTO}"); out["counts"]["auto_rows"] = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM deals WHERE {_IMPOSSIBLE}"); out["counts"]["impossible"] = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM deals WHERE {_UNSRC}"); out["counts"]["unsourced_auto"] = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM ({_DUP}) x"); out["counts"]["duplicates_auto"] = cur.fetchone()[0]
            if not dry:
                cur.execute(f"DELETE FROM deals WHERE {_IMPOSSIBLE}"); out["deleted"]["impossible"] = cur.rowcount
                cur.execute(f"DELETE FROM deals WHERE id IN ({_DUP})"); out["deleted"]["duplicates"] = cur.rowcount
                cur.execute(f"DELETE FROM deals WHERE {_UNSRC}"); out["deleted"]["unsourced"] = cur.rowcount
                c.commit()
                cur.execute("SELECT COUNT(*) FROM deals"); out["counts"]["total_after"] = cur.fetchone()[0]
    except Exception as e:
        out["ok"] = False; out["error"] = str(e)[:200]
        return jsonify(out), 500
    return jsonify(out), 200


@admin_ai_deals_bp.route("/purge-fabricated", methods=["POST"])
def purge_fabricated():
    """r68 (Nico audit #1-2): one-off, dry-run-default cleanup of the ai_deals
    table. The hardened extractor (deal_ingestion_scheduler r68) prevents NEW
    fakes/dups; this clears the EXISTING inflation:
      (a) duplicates  — same buyer+seller+value, keep the newest row only
      (b) land_garbage — deal_type='land_acquisition' (the removed over-broad type)
      (c) unsourced   — no source_url (can't verify → shouldn't publish)
      (d) vague       — no seller (or 'Undisclosed') AND no $ value
    Gate: X-Admin-Key (DCHUB_ADMIN_SECRET) OR X-Internal-Key (dchub-internal-sync-2026).
    Body: {dry_run: true}."""
    err = _check_auth()
    if err:
        ok = False
        try:
            from internal_auth import is_valid_internal_key
            ok = is_valid_internal_key(request.headers.get("X-Internal-Key", ""))
        except Exception:
            ok = False
        if not ok:
            return err
    dry = bool((request.get_json(silent=True) or {}).get("dry_run", True))
    _DUP_IDS = """
        SELECT id FROM (
          SELECT id, ROW_NUMBER() OVER (
            PARTITION BY LOWER(COALESCE(buyer,'')), LOWER(COALESCE(seller,'')),
                         LOWER(COALESCE(deal_value_str,''))
            ORDER BY deal_date DESC NULLS LAST, id DESC) AS rn
          FROM ai_deals) t WHERE rn > 1
    """
    _UNSOURCED = "COALESCE(source_url,'') = ''"
    _LAND = "LOWER(COALESCE(deal_type,'')) = 'land_acquisition'"
    _VAGUE = "(seller IS NULL OR LOWER(seller) = 'undisclosed') AND deal_value_usd IS NULL"
    out = {"ok": True, "dry_run": dry, "counts": {}, "deleted": {}}
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ai_deals"); out["counts"]["total_before"] = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM ({_DUP_IDS}) x"); out["counts"]["duplicates"] = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM ai_deals WHERE {_UNSOURCED}"); out["counts"]["unsourced"] = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM ai_deals WHERE {_LAND}"); out["counts"]["land_garbage"] = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM ai_deals WHERE {_VAGUE}"); out["counts"]["vague"] = cur.fetchone()[0]
            if not dry:
                cur.execute(f"DELETE FROM ai_deals WHERE id IN ({_DUP_IDS})"); out["deleted"]["duplicates"] = cur.rowcount
                cur.execute(f"DELETE FROM ai_deals WHERE {_LAND}"); out["deleted"]["land_garbage"] = cur.rowcount
                cur.execute(f"DELETE FROM ai_deals WHERE {_UNSOURCED}"); out["deleted"]["unsourced"] = cur.rowcount
                cur.execute(f"DELETE FROM ai_deals WHERE {_VAGUE}"); out["deleted"]["vague"] = cur.rowcount
                c.commit()
                cur.execute("SELECT COUNT(*) FROM ai_deals"); out["counts"]["total_after"] = cur.fetchone()[0]
    except Exception as e:
        out["ok"] = False; out["error"] = str(e)[:200]
        return jsonify(out), 500
    return jsonify(out), 200


# ---------------------------------------------------------------------------
# Validation + mapping to deals schema
# ---------------------------------------------------------------------------

VALID_TYPES = {
    "equity", "M&A", "debt", "capex", "JV",
    "AI-contract", "AI-infra", "land", "power-agreement", "other",
    # also accept lowercase variants found in existing data
    "m&a", "jv", "ma", "land_acquisition", "ai_contract", "ai_infra",
    "transaction", "acquisition",
}


def _normalize_type(t: str) -> str:
    """Map flexible inputs to canonical type values."""
    t = t.strip()
    aliases = {
        "ma": "M&A",
        "m&a": "M&A",
        "jv": "JV",
        "land_acquisition": "land",
        "ai_contract": "AI-contract",
        "ai_infra": "AI-infra",
    }
    return aliases.get(t.lower(), t)


def _category_for_type(t: str) -> str:
    """Map specific type to high-level deal_category."""
    t_lower = t.lower()
    if "capex" in t_lower or "infra" in t_lower:
        return "capex"
    if "power" in t_lower:
        return "power agreement"
    if "land" in t_lower:
        return "land acquisition"
    if "contract" in t_lower:
        return "ai contract"
    return "transaction"


def _build_deal_id(buyer: str, seller: str, deal_date: date, value_millions: Optional[float]) -> str:
    """Match the existing format: AUTO-YYYYMMDD-<6char hex>."""
    raw = "|".join([
        buyer.strip().lower(),
        seller.strip().lower(),
        deal_date.isoformat(),
        f"{float(value_millions):.4f}" if value_millions is not None else "null",
    ])
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]
    return f"AUTO-{deal_date.strftime('%Y%m%d')}-{suffix}"


def _validate_and_map(p: dict) -> tuple[Optional[dict], Optional[str]]:
    if not isinstance(p, dict):
        return None, "body must be JSON object"

    buyer  = (p.get("acquirer") or p.get("buyer") or "").strip()
    seller = (p.get("target")   or p.get("seller") or "").strip()
    notes  = (p.get("notes") or p.get("description") or "").strip() or None
    src    = (p.get("source_url") or "").strip() or None
    region = (p.get("region") or "").strip() or None
    market = (p.get("market") or "").strip() or None

    if not buyer or len(buyer) > 500:
        return None, "acquirer/buyer required"
    if not seller or len(seller) > 500:
        return None, "target/seller required"

    deal_type_raw = (p.get("deal_type") or p.get("type") or "").strip()
    if not deal_type_raw:
        return None, "deal_type required"
    deal_type = _normalize_type(deal_type_raw)

    deal_date_in = p.get("announced_date") or p.get("date") or p.get("deal_date")
    if isinstance(deal_date_in, str):
        try:
            deal_date_obj = date.fromisoformat(deal_date_in[:10])
        except ValueError:
            return None, "announced_date must be YYYY-MM-DD"
    elif isinstance(deal_date_in, date):
        deal_date_obj = deal_date_in
    else:
        return None, "announced_date required"

    if deal_date_obj > date.today():
        return None, "announced_date cannot be in the future"

    # Value: input is USD; deals.value is MILLIONS. Convert.
    raw_value = p.get("value_usd") if "value_usd" in p else p.get("value")
    value_millions: Optional[float] = None
    if raw_value is not None:
        try:
            v = float(raw_value)
            # Heuristic: if the number is huge (>= 1e8), assume USD; convert to millions.
            # If small (< 1e6), assume already in millions and leave alone.
            if v >= 1e8:
                value_millions = v / 1_000_000.0
            else:
                value_millions = v
            if value_millions < 0 or value_millions > 1e7:
                return None, "value out of plausible range (millions)"
        except (TypeError, ValueError):
            return None, "value must be numeric"

    confidence = p.get("confidence", 0.85)
    try:
        confidence = float(confidence)
        # Some legacy rows store confidence as 0-100, others 0-1. Normalize to 0-1.
        if confidence > 1.0:
            confidence = confidence / 100.0
        if not (0.0 <= confidence <= 1.0):
            return None, "confidence must be between 0 and 1 (or 0-100)"
    except (TypeError, ValueError):
        return None, "confidence must be numeric"

    deal_id = _build_deal_id(buyer, seller, deal_date_obj, value_millions)
    deal_category = _category_for_type(deal_type)

    return {
        "id": deal_id,
        "date": deal_date_obj.isoformat(),
        "year": deal_date_obj.year,
        "buyer": buyer,
        "seller": seller,
        "value": value_millions,
        "type": deal_type,
        "deal_category": deal_category,
        "region": region,
        "market": market,
        "source_url": src,
        "notes": notes,
        "verified": 0,
        "status": "active",
        "extraction_confidence": confidence,
        "extracted_via": "deal-ma-tracker",
        "extracted_at": datetime.now(timezone.utc),
    }, None


# ---------------------------------------------------------------------------
# POST -- insert (or upsert by id)
# ---------------------------------------------------------------------------

@admin_ai_deals_bp.route("", methods=["POST"])
def insert_deal():
    err = _check_auth()
    if err is not None:
        return err

    payload, msg = _validate_and_map(request.get_json(silent=True) or {})
    if payload is None:
        return jsonify(error=msg), 400

    sql = """
        INSERT INTO deals (
            id, date, year, buyer, seller, value, type, deal_category,
            region, market, source_url, notes, verified, status,
            extraction_confidence, extracted_via, extracted_at, created_at
        )
        VALUES (
            %(id)s, %(date)s, %(year)s, %(buyer)s, %(seller)s, %(value)s,
            %(type)s, %(deal_category)s, %(region)s, %(market)s,
            %(source_url)s, %(notes)s, %(verified)s, %(status)s,
            %(extraction_confidence)s, %(extracted_via)s, %(extracted_at)s,
            NOW()::text
        )
        ON CONFLICT (id) DO UPDATE SET
            extraction_confidence = GREATEST(
                COALESCE(deals.extraction_confidence, 0),
                COALESCE(EXCLUDED.extraction_confidence, 0)
            ),
            notes        = COALESCE(EXCLUDED.notes, deals.notes),
            source_url   = COALESCE(EXCLUDED.source_url, deals.source_url),
            extracted_via = EXCLUDED.extracted_via,
            extracted_at  = EXCLUDED.extracted_at
        RETURNING id, (xmax = 0) AS inserted;
    """
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(sql, payload)
            row = cur.fetchone()
            c.commit()
    except Exception as e:
        return jsonify(error=f"insert failed: {type(e).__name__}: {e}"), 500

    deal_id, was_inserted = row[0], bool(row[1])
    out = dict(payload)
    out["extracted_at"] = out["extracted_at"].isoformat()
    return jsonify(
        id=deal_id,
        status="inserted" if was_inserted else "merged",
        deal=out,
    ), (201 if was_inserted else 200)


# ---------------------------------------------------------------------------
# GET -- list
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
            args["since"] = date.fromisoformat(since).isoformat()
            where = "WHERE date >= %(since)s"
        except ValueError:
            return jsonify(error="since must be YYYY-MM-DD"), 400

    sql = f"""
        SELECT id, date, year, buyer, seller, value, type, deal_category,
               region, market, source_url, notes, verified, status,
               extraction_confidence, extracted_via, extracted_at,
               deal_date, created_at
        FROM deals
        {where}
        ORDER BY date DESC NULLS LAST, value DESC NULLS LAST, id DESC
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
        for ts_field in ("extracted_at", "deal_date"):
            v = r.get(ts_field)
            if isinstance(v, datetime):
                r[ts_field] = v.isoformat()
            elif isinstance(v, date):
                r[ts_field] = v.isoformat()
        if r.get("value") is not None:
            r["value"] = float(r["value"])
            r["value_usd"] = r["value"] * 1_000_000.0  # convenience
        if r.get("extraction_confidence") is not None:
            r["extraction_confidence"] = float(r["extraction_confidence"])

    return jsonify(count=len(rows), deals=rows), 200


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@admin_ai_deals_bp.route("/health", methods=["GET"])
def health():
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*), MAX(date), MAX(extracted_at) FROM deals;")
            count, latest_date, most_recent = cur.fetchone()
        return jsonify(
            status="ok",
            count=int(count or 0),
            latest_date=latest_date if isinstance(latest_date, str) else (latest_date.isoformat() if latest_date else None),
            most_recent_extraction=most_recent.isoformat() if most_recent else None,
            schema="deals",
            target_table="deals",
        ), 200
    except Exception as e:
        return jsonify(error=f"health failed: {type(e).__name__}: {e}"), 500
