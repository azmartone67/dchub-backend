"""
hyperscaler_alerts.py — fire alerts when new $1B+ AI capex deals appear.

Phase ZZZZZ-round41 (2026-05-25). The hyperscaler-deals endpoint already
extracts $-figures + MW from news. This module is the WATCHER that
elevates anything ≥ $1B into a typed alert + pushes to the MCP SSE
event ring + writes to hyperscaler_alerts DB table for the dashboard.

Cron-callable. Runs every 5 min (cheap query).

Endpoints:
  GET  /api/v1/hyperscaler-alerts          — recent alerts (last 7d)
  POST /api/v1/hyperscaler-alerts/sweep    — scan + emit new alerts
"""
import os
import datetime
import re
from contextlib import contextmanager

from flask import Blueprint, jsonify

try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

hyperscaler_alerts_bp = Blueprint("hyperscaler_alerts", __name__)

RE_DOLLAR = re.compile(
    r"(?:\$\s?([\d,]+(?:\.\d+)?)\s?(billion|B|trillion|T)\b"
    r"|\b([\d,]+(?:\.\d+)?)\s?(billion|trillion)\s+(?:dollars?|USD))",
    re.I)
MIN_DEAL_USD = 1_000_000_000  # $1B threshold


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


def _ensure_table():
    if not (_pg and _dsn()): return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS hyperscaler_alerts (
                    id            SERIAL PRIMARY KEY,
                    news_id       INT NOT NULL,
                    headline      TEXT NOT NULL,
                    url           TEXT,
                    source        TEXT,
                    value_usd     BIGINT,
                    value_display TEXT,
                    actor         TEXT,
                    published_at  TIMESTAMPTZ,
                    detected_at   TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(news_id)
                );
                CREATE INDEX IF NOT EXISTS ix_hsa_value ON hyperscaler_alerts(value_usd DESC);
                CREATE INDEX IF NOT EXISTS ix_hsa_detected ON hyperscaler_alerts(detected_at DESC);
            """)
            c.commit()
    except Exception:
        pass

_ensure_table()


def _extract_dollar_usd(text):
    if not text: return None
    m = RE_DOLLAR.search(text)
    if not m: return None
    num_str = m.group(1) or m.group(3)
    unit    = (m.group(2) or m.group(4) or "").lower()
    if not num_str: return None
    try: num = float(num_str.replace(",", ""))
    except: return None
    if unit in ("b", "billion"):  return num * 1_000_000_000, f"${num}B"
    if unit in ("t", "trillion"): return num * 1_000_000_000_000, f"${num}T"
    return None


def _classify_actor(text):
    """r43 (2026-05-25): broader actor recognition. Picks the FIRST
    match in the priority order below — hyperscalers > AI labs >
    cloud > GPU vendors > infra investors > "miscellaneous capex".

    SUSE, Google/Blackstone JV, sovereign-AI funds all had to show
    as 'Unknown' until this. Order matters: 'Google and Blackstone'
    should classify as Google, not Blackstone, so Google wins.
    """
    t = (text or "").lower()
    classifications = [
        # AI labs (highest priority — they drive most $1B+ news)
        ("OpenAI",       ["openai", "stargate", "sam altman"]),
        ("Anthropic",    ["anthropic", "claude.ai", "dario amodei"]),
        ("xAI",          ["xai ", "x.ai", "grok ", "musk ai"]),
        ("Mistral",      ["mistral ai", "mistral large"]),
        ("DeepSeek",     ["deepseek"]),
        ("Cohere",       ["cohere ai", "cohere expansion"]),
        # Hyperscalers
        ("Microsoft",    ["microsoft", "azure ", "satya nadella"]),
        ("Google",       ["google ai", "google cloud", "alphabet",
                           "google and ", "deepmind", "gemini"]),
        ("AWS",          ["aws ", "amazon web", "amazon expand",
                           "amazon ai", " bedrock"]),
        ("Meta",         ["meta ai", "facebook ai", "llama "]),
        ("Apple",        ["apple intelligence", "apple ai", "apple build"]),
        # GPU clouds / neoclouds
        ("CoreWeave",    ["coreweave"]),
        ("Lambda",       ["lambda labs", "lambda cloud"]),
        ("Crusoe",       ["crusoe"]),
        ("Vultr",        ["vultr "]),
        ("IREN",         ["iren ", "iris energy"]),
        ("Applied",      ["applied digital"]),
        # Database / enterprise software
        ("Oracle",       ["oracle"]),
        ("SAP",          ["sap "]),
        ("ServiceNow",   ["servicenow"]),
        ("Snowflake",    ["snowflake"]),
        ("Databricks",   ["databricks"]),
        ("SUSE",         ["suse "  , "suse's", "suse l", "suse e", "suse a"]),
        # Chip / hardware
        ("NVIDIA",       ["nvidia", "blackwell", "jensen huang", "h100", "h200"]),
        ("AMD",          ["amd ", "lisa su"]),
        ("Intel",        ["intel ", "intel ai", "gaudi"]),
        ("TSMC",         ["tsmc", "taiwan semi"]),
        # Power / infra investors
        ("Blackstone",   ["blackstone "]),
        ("Brookfield",   ["brookfield"]),
        ("KKR",          ["kkr "]),
        ("Stonepeak",    ["stonepeak"]),
        # Data center operators
        ("Equinix",      ["equinix"]),
        ("Digital Realty", ["digital realty"]),
        ("QTS",          ["qts data"]),
        ("Stack",        ["stack infra"]),
        # Sovereign / state programs
        ("UAE",          ["uae ai", "abu dhabi", "g42 "]),
        ("Saudi",        ["saudi ai", "humain", "pif"]),
        # Generic AI capex (catch-all)
        ("AI Capex",     ["ai data center", "ai infrastructure",
                           "ai cloud", "gpu cluster", "hyperscale"]),
    ]
    for actor, keys in classifications:
        if any(k in t for k in keys): return actor
    return "Unknown"


@hyperscaler_alerts_bp.route("/api/v1/hyperscaler-alerts/sweep", methods=["GET", "POST"])
def sweep():
    """Scan recent news for $1B+ deals, write new ones to alerts table."""
    out = {
        "at": datetime.datetime.utcnow().isoformat() + "Z",
        "new_alerts": 0,
        "scanned": 0,
        "errors": [],
    }
    if not (_pg and _dsn()):
        out["error"] = "no_db"
        return jsonify(out), 200

    # Step 1: discover the news table schema
    table = None
    cols = []
    for candidate in ("news", "news_articles", "dc_news"):
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute(f"SELECT * FROM {candidate} LIMIT 1")
                cols = [d[0] for d in cur.description]
                table = candidate
                break
        except Exception as e:
            out.setdefault("col_attempts", []).append({candidate: type(e).__name__})
            continue
    if not table:
        out["errors"].append("no_news_table_found")
        return jsonify(out), 200
    out["table"] = table
    out["cols"] = cols

    # Step 2: figure out best column names from what's available
    title_col = next((c for c in ("title", "headline") if c in cols), None)
    body_col  = next((c for c in ("summary", "description", "body", "content") if c in cols), None)
    date_col  = next((c for c in ("published_date", "published_at", "created_at", "ts") if c in cols), None)
    url_col   = next((c for c in ("url", "link") if c in cols), None)
    src_col   = next((c for c in ("source", "publisher") if c in cols), None)
    id_col    = next((c for c in ("id", "uuid") if c in cols), None)

    if not (title_col and date_col and id_col):
        out["errors"].append(f"missing_cols: title={title_col} date={date_col} id={id_col}")
        return jsonify(out), 200
    out["mapped"] = {"title": title_col, "body": body_col, "date": date_col,
                     "url": url_col, "src": src_col, "id": id_col}

    # Step 3: query — pull anything mentioning billion/trillion
    body_select = f"COALESCE({body_col}, '')" if body_col else "''"
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT {id_col} AS id,
                       {title_col} AS title,
                       {(src_col or "'unknown'")} AS source,
                       {(url_col or "''")} AS url,
                       {date_col} AS published_at,
                       {body_select} AS body
                FROM {table}
                WHERE LOWER({title_col}) LIKE '%%billion%%'
                   OR LOWER({title_col}) LIKE '%%trillion%%'
                AND {date_col} > NOW() - INTERVAL '30 days'
                ORDER BY {date_col} DESC LIMIT 200
            """)
            rows = cur.fetchall()
    except Exception as e:
        out["errors"].append(f"query_failed: {type(e).__name__}: {str(e)[:120]}")
        return jsonify(out), 200

    out["scanned"] = len(rows)
    new_count = 0
    for r in rows:
        full = (r["title"] or "") + " " + (r.get("body") or "")
        extract = _extract_dollar_usd(full)
        if not extract: continue
        value_usd, display = extract
        if value_usd < MIN_DEAL_USD: continue
        actor = _classify_actor(full)
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute("""
                    INSERT INTO hyperscaler_alerts
                      (news_id, headline, url, source, value_usd, value_display, actor, published_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (news_id) DO NOTHING
                    RETURNING id
                """, (r["id"], r["title"][:500], r.get("url"), r.get("source"),
                       int(value_usd), display, actor, r.get("published_at")))
                if cur.fetchone():
                    new_count += 1
                c.commit()
        except Exception as e:
            out["errors"].append(f"insert_failed_news_{r['id']}: {type(e).__name__}")

    out["new_alerts"] = new_count
    return jsonify(out), 200



    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Try news table with various column names
            for col_summary in ("summary", "description", "''"):
                try:
                    # r41.1: simplified LIKE filter (regex with backslashes in
                    # psycopg2 f-string was failing silently). Catches "$10B",
                    # "$10 billion", "10 billion", "$1.5B" etc.
                    # r46.5: use raw f-string to avoid SyntaxWarning on \$.
                    # The $ is a literal char in SQL LIKE — no escape needed —
                    # but earlier rounds left \$ in place and Python warned on
                    # every boot. rf""" silences the warning.
                    cur.execute(rf"""
                        SELECT id, title, source, url, published_date,
                               COALESCE({col_summary}, '') AS body
                        FROM news
                        WHERE (LOWER(title) LIKE '%%billion%%'
                            OR LOWER(title) LIKE '%%trillion%%'
                            OR LOWER(title) LIKE '%% b %%'
                            OR LOWER(title) LIKE '%%$%%b%%'
                            OR LOWER(title) LIKE '%%$%%m%%')
                          AND published_date > CURRENT_DATE - INTERVAL '14 days'
                        ORDER BY published_date DESC LIMIT 200
                    """)
                    rows = cur.fetchall()
                    break
                except Exception:
                    continue
            else:
                out["errors"].append("no_news_table")
                return jsonify(out), 200

        out["scanned"] = len(rows)
        new_count = 0
        for r in rows:
            full = (r["title"] or "") + " " + (r.get("body") or "")
            extract = _extract_dollar_usd(full)
            if not extract: continue
            value_usd, display = extract
            if value_usd < MIN_DEAL_USD: continue
            actor = _classify_actor(full)
            try:
                with _conn() as c, c.cursor() as cur:
                    cur.execute("""
                        INSERT INTO hyperscaler_alerts
                          (news_id, headline, url, source, value_usd, value_display, actor, published_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (news_id) DO NOTHING
                        RETURNING id
                    """, (r["id"], r["title"][:500], r.get("url"), r.get("source"),
                           int(value_usd), display, actor, r.get("published_date")))
                    if cur.fetchone():
                        new_count += 1
                    c.commit()
            except Exception as e:
                out["errors"].append(f"insert_failed_for_news_id_{r['id']}: {type(e).__name__}")

        out["new_alerts"] = new_count
    except Exception as e:
        out["errors"].append(f"sweep_failed: {type(e).__name__}: {str(e)[:120]}")

    return jsonify(out), 200


@hyperscaler_alerts_bp.route("/api/v1/hyperscaler-alerts", methods=["GET"])
def list_alerts():
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 200
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, headline, url, source, value_usd, value_display, actor,
                       published_at, detected_at
                FROM hyperscaler_alerts
                ORDER BY detected_at DESC LIMIT 50
            """)
            rows = cur.fetchall()
            for r in rows:
                for k, v in list(r.items()):
                    if isinstance(v, datetime.datetime):
                        r[k] = v.isoformat()
        return jsonify({
            "count": len(rows),
            "alerts": rows,
            "computed_at": datetime.datetime.utcnow().isoformat() + "Z",
            "threshold_usd": MIN_DEAL_USD,
        }), 200
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}", "detail": str(e)[:200]}), 500
