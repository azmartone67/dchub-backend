#!/usr/bin/env python3
"""extractor_cron.py — Standalone announcement-to-signal extractor."""

from __future__ import annotations
import argparse, hashlib, json, logging, os, sys, time
from datetime import datetime
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("extractor_cron")

NEON_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-5")
DEFAULT_BATCH_LIMIT = int(os.environ.get("EXTRACTOR_BATCH_LIMIT", "50"))
REGEX_CONFIDENCE_FLOOR = float(os.environ.get("REGEX_CONFIDENCE_FLOOR", "0.7"))

SOURCE_CONFIDENCE_FLOORS = {
    "datacenterknowledge.com": 0.65,
    "datacenterdynamics.com": 0.65,
    "datacenterfrontier.com": 0.65,
    "dcd": 0.65,
    "datacenterhawk.com": 0.65,
    "_default": 0.65,
}

def _floor_for_source(source):
    if not source: return SOURCE_CONFIDENCE_FLOORS["_default"]
    s = source.lower()
    for key, floor in SOURCE_CONFIDENCE_FLOORS.items():
        if key != "_default" and key in s: return floor
    return SOURCE_CONFIDENCE_FLOORS["_default"]

import re
KNOWN_OPERATORS = [
    "AWS","Amazon Web Services","Microsoft","Azure","Google","Google Cloud",
    "Meta","Facebook","Apple","Oracle","IBM Cloud","Alibaba",
    "CoreWeave","Lambda","Lambda Labs","Crusoe","Together AI","Voltage Park",
    "TensorWave","Nebius","Vast.ai",
    "Equinix","Digital Realty","DigitalBridge","DataBank","QTS","Iron Mountain",
    "CyrusOne","NTT","NTT Data","Cologix","EdgeConneX","Compass Datacenters",
    "Vantage","STACK Infrastructure","Aligned","Switch","Flexential","TierPoint",
    "PowerHouse","DartPoints","Sabey","Skybox Datacenters",
]
_OP_PATTERNS = [(name, re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)) for name in KNOWN_OPERATORS]
_CAPACITY_RE = re.compile(r"(?P<value>\d{1,4}(?:\.\d{1,3})?)\s*(?P<unit>MW|megawatts?|GW|gigawatts?)", re.IGNORECASE)
_MARKET_RE = re.compile(
    r"\b(Ashburn|Loudoun|Reston|Sterling|Manassas|Northern Virginia|N\.?\s*Virginia|"
    r"Phoenix|Mesa|Chandler|Dallas|Plano|Garland|Atlanta|Marietta|"
    r"Chicago|Aurora|Elk Grove|Silicon Valley|San Jose|Santa Clara|"
    r"Columbus|Hillsboro|Portland|Salt Lake City|Reno|Las Vegas|"
    r"Quincy|Moses Lake|Omaha|Council Bluffs|"
    r"New York|New Jersey|Secaucus|Piscataway|"
    r"Dublin|Frankfurt|London|Singapore|Tokyo|Sydney)\b", re.IGNORECASE)
_DEAL_RE = re.compile(
    r"\b(acquir(?:ed|es|ing|ition)|purchas(?:ed|es|ing|e)|merg(?:ed|er|es)|"
    r"buy(?:s|out|ing)|sells?|sold|joint venture|partnership|investment of|"
    r"raised|funding round|Series [A-F]|IPO)\b", re.IGNORECASE)
_DEAL_VALUE_RE = re.compile(r"\$\s?(?P<value>\d{1,4}(?:\.\d{1,3})?)\s*(?P<unit>billion|million|B|M)\b", re.IGNORECASE)

def _regex_extract(text):
    if not text: return None
    operator = None
    for canonical, pat in _OP_PATTERNS:
        if pat.search(text):
            operator = canonical; break
    capacity_mw = None
    for m in _CAPACITY_RE.finditer(text):
        val = float(m.group("value")); unit = m.group("unit").lower()
        mw = val * 1000.0 if unit.startswith("g") else val
        if capacity_mw is None or mw > capacity_mw: capacity_mw = mw
    market_m = _MARKET_RE.search(text); market = market_m.group(0) if market_m else None
    deal_hit = _DEAL_RE.search(text); deal_value_m = _DEAL_VALUE_RE.search(text)
    deal_value_usd = None
    if deal_value_m:
        v = float(deal_value_m.group("value")); u = deal_value_m.group("unit").lower()
        deal_value_usd = v * 1_000_000_000 if u in ("billion","b") else v * 1_000_000
    if operator and capacity_mw and market: confidence = 0.85
    elif operator and capacity_mw: confidence = 0.70
    elif operator and (deal_hit or deal_value_usd): confidence = 0.65
    else: return None
    return {
        "operator": operator, "capacity_mw": capacity_mw, "market": market,
        "status": "announced",
        "deal_type": ("acquisition" if deal_hit else None) if (deal_hit or deal_value_usd) else None,
        "deal_value_usd": deal_value_usd, "confidence": confidence, "source": "regex",
    }

_SYSTEM_PROMPT = """You extract structured data-center signals from news articles.
Return STRICT JSON only — no prose, no markdown fences.

Schema:
{
  "is_dc_news": boolean,
  "operator": string|null,
  "capacity_mw": number|null,
  "market": string|null,
  "status": "announced"|"permitting"|"under_construction"|"operational"|"expansion"|"acquisition"|"closed"|"cancelled"|null,
  "deal_type": "acquisition"|"jv"|"investment"|"sale"|"lease"|null,
  "deal_value_usd": number|null,
  "confidence": number
}

Rules:
- Convert GW to MW (multiply by 1000).
- If the article isn't about a DC build/deal, set is_dc_news=false and confidence=0.95.
- Confidence 0.95 = all four (operator, capacity, market, status) explicitly stated.
- Confidence 0.7 = strong inference required.
"""

def _call_claude(title, text):
    if not ANTHROPIC_API_KEY: return None
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic SDK not installed; pip install anthropic"); return None
    excerpt = (text or "")[:1500]
    user_msg = f"Title: {title or ''}\n\nArticle:\n{excerpt}"
    raw = ""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=400, system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}])
        raw = "".join(b.text for b in resp.content if getattr(b,"type","")=="text").strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"): raw = raw[4:].strip()
        data = json.loads(raw); data["source"] = "claude"; return data
    except json.JSONDecodeError as e:
        logger.warning("Claude returned non-JSON: %s | raw=%r", e, raw[:200]); return None
    except Exception as e:
        logger.warning("Claude call failed: %s", e); return None

def extract(title, text):
    if not text or len(text) < 50: return None
    regex = _regex_extract(text)
    if regex and regex["confidence"] >= REGEX_CONFIDENCE_FLOOR: return regex
    claude = _call_claude(title, text)
    candidates = [s for s in (regex, claude) if s]
    if not candidates: return None
    return max(candidates, key=lambda s: s.get("confidence", 0.0))

def _connect():
    if not NEON_URL: raise RuntimeError("NEON_DATABASE_URL or DATABASE_URL must be set")
    import psycopg2
    conn = psycopg2.connect(NEON_URL, connect_timeout=10)
    conn.autocommit = False
    return conn

def fetch_unprocessed(conn, limit):
    sql = """
        SELECT id, title, COALESCE(content, summary, '') AS body, source, source_url, published_date
        FROM announcements
        WHERE (facility_processed = false OR facility_processed IS NULL)
          AND COALESCE(content, summary, '') <> ''
        ORDER BY discovered_at DESC NULLS LAST
        LIMIT %s
    """
    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (limit,))
        return [dict(r) for r in cur.fetchall()]

def mark_processed(conn, announcement_id, extracted_id=None):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE announcements SET facility_processed = true, "
            "facility_extracted_id = COALESCE(%s, facility_extracted_id) WHERE id = %s",
            (extracted_id, announcement_id))

def insert_capacity(conn, signals, announcement_id):
    sql = """INSERT INTO capacity_pipeline (operator, capacity_mw, market, status,
        source_announcement_id, extraction_confidence, extracted_via, extracted_at, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())
        ON CONFLICT (source_announcement_id) WHERE source_announcement_id IS NOT NULL DO NOTHING RETURNING id"""
    with conn.cursor() as cur:
        cur.execute(sql, (signals.get("operator"), signals.get("capacity_mw"),
            signals.get("market"), signals.get("status") or "announced", announcement_id,
            float(signals.get("confidence", 0.0)), signals.get("source", "regex")))
        return cur.fetchone() is not None

def insert_deal(conn, signals, announcement_id):
    deal_id = "ext_" + hashlib.md5(announcement_id.encode()).hexdigest()[:20]
    sql = """INSERT INTO deals (id, buyer, type, value, market,
        source_announcement_id, extraction_confidence, extracted_via, extracted_at, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())
        ON CONFLICT (source_announcement_id) WHERE source_announcement_id IS NOT NULL DO NOTHING
        RETURNING id"""
    with conn.cursor() as cur:
        cur.execute(sql, (deal_id, signals.get("operator"), signals.get("deal_type"),
            signals.get("deal_value_usd"), signals.get("market"), announcement_id,
            float(signals.get("confidence", 0.0)), signals.get("source", "regex")))
        return cur.fetchone() is not None

def log_run(conn, started, articles, capacity, deals, errors, notes=""):
    duration_ms = int((datetime.utcnow() - started.replace(tzinfo=None)).total_seconds() * 1000) if started else None
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO extractor_runs
            (started_at, finished_at, articles_read, capacity_inserted, deals_inserted, errors, duration_ms, notes)
            VALUES (%s, NOW(), %s, %s, %s, %s, %s, %s)""",
            (started, articles, capacity, deals, errors, duration_ms, notes[:1000] if notes else None))

def process_one(conn, row, dry_run):
    counters = {"capacity_inserted":0,"deals_inserted":0,"errors":0,"skipped_low_conf":0,"skipped_not_dc":0}
    aid = row["id"]; title = row.get("title") or ""; body = row.get("body") or ""
    source = row.get("source") or row.get("source_url") or ""
    try: signals = extract(title, body)
    except Exception as e:
        logger.warning("[%s] extraction error: %s", aid, e); counters["errors"] += 1; return counters
    if not signals:
        if not dry_run: mark_processed(conn, aid); conn.commit()
        return counters
    if signals.get("is_dc_news") is False:
        counters["skipped_not_dc"] += 1
        if not dry_run: mark_processed(conn, aid); conn.commit()
        return counters
    floor = _floor_for_source(source)
    if signals.get("confidence", 0.0) < floor:
        counters["skipped_low_conf"] += 1
        if not dry_run: mark_processed(conn, aid); conn.commit()
        return counters
    if dry_run:
        logger.info("[DRY] %s | conf=%.2f | %s | %s MW | %s", aid, signals["confidence"],
            signals.get("operator"), signals.get("capacity_mw"), signals.get("market"))
        return counters
    try:
        if signals.get("capacity_mw") and signals.get("operator"):
            if insert_capacity(conn, signals, aid): counters["capacity_inserted"] += 1
        if signals.get("deal_type") or signals.get("deal_value_usd"):
            if insert_deal(conn, signals, aid): counters["deals_inserted"] += 1
        mark_processed(conn, aid); conn.commit()
    except Exception as e:
        logger.warning("[%s] insert error: %s", aid, e); conn.rollback(); counters["errors"] += 1
        # Always advance — mark this article processed so we don't loop on it forever
        try:
            mark_processed(conn, aid); conn.commit()
        except Exception as e2:
            logger.warning("[%s] also failed to mark processed: %s", aid, e2); conn.rollback()
    return counters

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH_LIMIT)
    parser.add_argument("--backfill", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not NEON_URL:
        logger.error("NEON_DATABASE_URL or DATABASE_URL must be set"); sys.exit(2)
    started = datetime.utcnow(); conn = _connect()
    total = {"capacity_inserted":0,"deals_inserted":0,"errors":0,"skipped_low_conf":0,"skipped_not_dc":0,"articles_read":0}
    target = args.backfill if args.backfill > 0 else args.limit
    per_batch = min(args.limit, target); processed = 0
    logger.info("Starting | model=%s | target=%d | per_batch=%d | dry_run=%s",
        CLAUDE_MODEL, target, per_batch, args.dry_run)
    while processed < target:
        rows = fetch_unprocessed(conn, per_batch)
        if not rows: logger.info("No unprocessed announcements left."); break
        for row in rows:
            counters = process_one(conn, row, args.dry_run)
            for k, v in counters.items(): total[k] = total.get(k, 0) + v
            total["articles_read"] += 1; processed += 1
            if processed >= target: break
        if not args.backfill: break
    duration = (datetime.utcnow() - started).total_seconds()
    logger.info("Done | read=%d | capacity=%d | deals=%d | low_conf=%d | not_dc=%d | errors=%d | %.1fs",
        total["articles_read"], total["capacity_inserted"], total["deals_inserted"],
        total["skipped_low_conf"], total["skipped_not_dc"], total["errors"], duration)
    try:
        log_run(conn, started, total["articles_read"], total["capacity_inserted"],
            total["deals_inserted"], total["errors"],
            notes=("dry_run" if args.dry_run else "") + f" model={CLAUDE_MODEL}"
                  + f" low_conf={total['skipped_low_conf']} not_dc={total['skipped_not_dc']}")
        conn.commit()
    except Exception as e:
        logger.warning("Failed to log run heartbeat: %s", e); conn.rollback()
    conn.close()

if __name__ == "__main__":
    main()
