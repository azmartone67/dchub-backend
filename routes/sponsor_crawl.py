"""Per-AI-engine crawl counts for the surfaces a sponsor block sits on (B5).

WHY THIS IS THE DELIVERABLE. A sponsor on DC Hub is not buying banner
impressions; they are buying presence on the pages AI engines read when they
answer questions about data-center infrastructure. The only party who can prove
an engine actually FETCHED those pages is whoever owns the edge they were
fetched from. No competitor can produce this table about our surfaces, and we
cannot produce it about theirs. It belongs at the front of the report, not in
an appendix.

★ THE 1w1d CAP IS A RETENTION LIMIT, NOT A QUERY-SIZE LIMIT. Measured against
  the live zone 2026-08-28: the API refuses any request for data OLDER than
  1w1d ago —
      "cannot request data older than 1w1d, but your query requests data
       from 4w2d ago"
  so chunking a 30-day window into 7-day slices does NOT work; the old slices
  are simply refused. days=8 is the largest window that returns (verified: 30
  and 14 fail, 8/7/6 succeed). A MONTHLY crawl table therefore cannot be
  produced from a live query at all, which is why snapshot_crawls() below
  accrues daily rows: 30-day coverage has to be accumulated, not asked for.

★ Use `datetime_geq`/`datetime_leq`, not `date_geq` — the latter buckets to
  whole days and loses the boundary slice.

★ A CRAWL IS NOT A READER AND NOT A CITATION. It is evidence the surface was
  fetched by that engine's agent, nothing more. The report says so; do not let
  it be read as impressions.
"""
import json
import logging
import os
import urllib.request
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
_ZONE_ID = "1cb22dda8d50546d6edf0c09a8be5128"      # dchub.cloud
_CHUNK_DAYS = 7          # largest slice that reliably returns
MAX_LOOKBACK_DAYS = 8    # measured hard ceiling; older data is REFUSED, not empty
_SNAPSHOT_TABLE = "sponsor_crawl_daily"

# Substring -> engine. Matched case-insensitively against the UA, first hit
# wins, so ORDER MATTERS: 'oai-searchbot' must be tested before 'bot'.
#
# ★ Googlebot and Bingbot are DELIBERATELY NOT "AI engines" here. Googlebot is
#   search indexing, and our own measurements put Bing traffic overwhelmingly at
#   Bing Webmaster Tools rather than Copilot. Counting them would inflate the
#   headline with crawls that have nothing to do with an AI answer. Google's AI
#   surface is Google-Extended; that one IS counted.
_ENGINE_UA = (
    ("gptbot",             "OpenAI (GPTBot)"),
    ("oai-searchbot",      "OpenAI (SearchBot)"),
    ("chatgpt-user",       "OpenAI (ChatGPT browsing)"),
    ("claudebot",          "Anthropic (ClaudeBot)"),
    ("claude-web",         "Anthropic (Claude-Web)"),
    ("claude-user",        "Anthropic (Claude browsing)"),
    ("anthropic-ai",       "Anthropic (anthropic-ai)"),
    ("perplexitybot",      "Perplexity (PerplexityBot)"),
    ("perplexity-user",    "Perplexity (browsing)"),
    ("google-extended",    "Google (Google-Extended)"),
    ("meta-externalagent", "Meta (external agent)"),
    ("bytespider",         "ByteDance (Bytespider)"),
    ("ccbot",              "Common Crawl (CCBot)"),
    ("amazonbot",          "Amazon (Amazonbot)"),
    ("applebot-extended",  "Apple (Applebot-Extended)"),
    ("cohere-ai",          "Cohere"),
    ("diffbot",            "Diffbot"),
)


def classify_engine(ua):
    """The AI engine behind a user agent, or None if it is not one."""
    low = (ua or "").lower()
    for needle, name in _ENGINE_UA:
        if needle in low:
            return name
    return None


def _token():
    return (os.environ.get("CF_ANALYTICS_READ_TOKEN")
            or os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()


def _query(token, since, until, paths):
    q = """
    query($zone: String!, $since: Time!, $until: Time!, $paths: [String!]) {
      viewer { zones(filter: {zoneTag: $zone}) {
        httpRequestsAdaptiveGroups(
          filter: {datetime_geq: $since, datetime_leq: $until,
                   clientRequestPath_in: $paths}
          limit: 5000
          orderBy: [count_DESC]
        ) { count dimensions { userAgent clientRequestPath } }
      } }
    }"""
    body = json.dumps({"query": q, "variables": {
        "zone": _ZONE_ID,
        "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "until": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "paths": list(paths),
    }}).encode()
    req = urllib.request.Request(_GRAPHQL, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        payload = json.loads(r.read().decode())
    # ★ CF returns HTTP 200 with an `errors` array. Treating 200 as success is
    #   how a permissions failure becomes "no crawlers visited this month".
    if payload.get("errors"):
        raise RuntimeError(str(payload["errors"])[:300])
    zones = ((payload.get("data") or {}).get("viewer") or {}).get("zones") or []
    if not zones:
        raise RuntimeError("no zone in response — token may lack Zone Analytics: Read")
    return zones[0].get("httpRequestsAdaptiveGroups") or []


def engine_crawls(paths, days=30, now=None):
    """Per-engine crawl counts for `paths` over the trailing `days`.

    Returns {"ok", "by_engine", "by_path", "total_ai_crawls", "chunks",
             "window", "limits"}. `ok` is False on ANY chunk failure — a
    partial sum reported as a total under-states an advertiser's reach with no
    sign that anything was missing.
    """
    out = {"ok": False, "by_engine": {}, "by_path": {}, "total_ai_crawls": 0,
           "chunks": 0, "window_days": int(days), "paths": list(paths),
           "limits": []}
    token = _token()
    if not token:
        out["limits"].append("CF_ANALYTICS_READ_TOKEN not set; no crawl data")
        return out

    end = now or datetime.now(timezone.utc)
    # ★ Clamp, and SAY SO. Asking for 30 days does not fail softly here — the
    #   zone refuses the old slices outright — so a caller who is not told it
    #   got 8 days would print a monthly heading over a weekly number.
    requested = int(days)
    days = min(requested, MAX_LOOKBACK_DAYS)
    out["window_days"] = days
    out["requested_days"] = requested
    if days < requested:
        out["limits"].append(
            f"Requested {requested} days; Cloudflare retains only "
            f"{MAX_LOOKBACK_DAYS} days of request-level analytics for this "
            f"zone, so these crawl counts cover {days} days. Longer windows "
            f"must be assembled from accumulated daily snapshots.")
    start = end - timedelta(days=days)
    by_engine, by_path = {}, {}
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=_CHUNK_DAYS), end)
        try:
            rows = _query(token, cursor, chunk_end, paths)
        except Exception as e:
            logger.warning("[sponsor_crawl] chunk %s..%s failed: %s",
                           cursor, chunk_end, e)
            out["limits"].append(
                f"crawl window {cursor:%Y-%m-%d} to {chunk_end:%Y-%m-%d} "
                f"could not be read ({str(e)[:120]}); totals would be partial")
            return out
        for row in rows:
            dims = row.get("dimensions") or {}
            eng = classify_engine(dims.get("userAgent"))
            if not eng:
                continue
            n = int(row.get("count") or 0)
            by_engine[eng] = by_engine.get(eng, 0) + n
            p = dims.get("clientRequestPath") or "?"
            by_path.setdefault(p, {})
            by_path[p][eng] = by_path[p].get(eng, 0) + n
        out["chunks"] += 1
        cursor = chunk_end

    out["by_engine"] = dict(sorted(by_engine.items(), key=lambda kv: -kv[1]))
    out["by_path"] = by_path
    out["total_ai_crawls"] = sum(by_engine.values())
    out["ok"] = True
    out["limits"].extend([
        "A crawl is a FETCH by that engine's agent. It is not a reader, not an "
        "impression, and not a citation.",
        "Googlebot and Bingbot are excluded: Googlebot is search indexing, and "
        "our measurements put Bing traffic overwhelmingly at Bing Webmaster "
        "Tools rather than Copilot. Google's AI agent (Google-Extended) IS "
        "counted.",
        "Engines that do not identify themselves in the user agent cannot be "
        "attributed and are not counted here.",
    ])
    return out


# ── accrual: the only way a 30-day crawl table can ever exist ────────
def ensure_snapshot_table():
    """DDL through the ONE blessed path — a PGCursorWrapper silently swallows
    CREATE TABLE whenever SKIP_DDL is set, and it defaults to '1' on Railway."""
    from db_utils import ddl_cursor
    with ddl_cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS sponsor_crawl_daily (
            day        DATE NOT NULL,
            engine     TEXT NOT NULL,
            path       TEXT NOT NULL,
            crawls     INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (day, engine, path)
        )""")


def snapshot_crawls(paths, conn=None, days=2, now=None) -> dict:
    """Persist the last `days` of per-engine crawl counts, day by day.

    Runs daily. Re-running is safe and CORRECTS rather than accumulates: the
    upsert SETs the count instead of adding to it, so a day re-read after CF
    finished settling replaces the partial figure. That is why the default
    window is 2 days and not 1 — the most recent day is still moving.
    """
    out = {"ok": False, "days_written": 0, "rows": 0, "limits": []}
    token = _token()
    if not token:
        out["limits"].append("CF_ANALYTICS_READ_TOKEN not set")
        return out
    end = now or datetime.now(timezone.utc)
    owned = conn is None
    if owned:
        try:
            from main import get_db
            conn = get_db()
        except Exception as e:
            out["limits"].append(f"database unavailable: {e}")
            return out
    if conn is None:
        out["limits"].append("database unavailable")
        return out
    try:
        ensure_snapshot_table()
    except Exception as e:
        logger.warning("[sponsor_crawl] snapshot DDL failed: %s", e)
    try:
        with conn.cursor() as cur:
            for back in range(min(int(days), MAX_LOOKBACK_DAYS)):
                d_end = end - timedelta(days=back)
                d_start = d_end - timedelta(days=1)
                try:
                    rows = _query(token, d_start, d_end, paths)
                except Exception as e:
                    logger.warning("[sponsor_crawl] snapshot day -%d failed: %s", back, e)
                    out["limits"].append(f"day -{back} unavailable: {str(e)[:100]}")
                    continue
                agg = {}
                for row in rows:
                    dims = row.get("dimensions") or {}
                    eng = classify_engine(dims.get("userAgent"))
                    if not eng:
                        continue
                    key = (eng, dims.get("clientRequestPath") or "?")
                    agg[key] = agg.get(key, 0) + int(row.get("count") or 0)
                for (eng, path), n in agg.items():
                    cur.execute(
                        "INSERT INTO sponsor_crawl_daily (day, engine, path, crawls) "
                        "VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT (day, engine, path) DO UPDATE SET "
                        "  crawls = EXCLUDED.crawls, updated_at = NOW()",
                        (d_start.date(), eng, path, n))
                    out["rows"] += 1
                out["days_written"] += 1
        conn.commit()
        out["ok"] = True
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        logger.warning("[sponsor_crawl] snapshot write failed: %s", e)
        out["limits"].append(f"write failed: {e}")
    finally:
        if owned:
            try: conn.close()
            except Exception: pass
    return out


def crawls_from_snapshots(paths, days=30, conn=None) -> dict:
    """The accumulated crawl table, and HOW MUCH OF THE WINDOW IT COVERS.

    days_covered is counted from distinct days actually present, so a report
    can never print a 30-day heading over 6 days of accrual.
    """
    out = {"ok": False, "by_engine": {}, "window_days": int(days),
           "days_covered": 0, "limits": []}
    owned = conn is None
    if owned:
        try:
            from main import get_read_db
            conn = get_read_db()
        except Exception as e:
            out["limits"].append(f"database unavailable: {e}")
            return out
    if conn is None:
        out["limits"].append("database unavailable")
        return out
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT engine, sum(crawls), count(DISTINCT day) "
                "  FROM sponsor_crawl_daily "
                " WHERE day > (CURRENT_DATE - %s::int) AND path = ANY(%s) "
                " GROUP BY engine ORDER BY 2 DESC",
                (int(days), list(paths)))
            rows = cur.fetchall() or []
            cur.execute(
                "SELECT count(DISTINCT day) FROM sponsor_crawl_daily "
                " WHERE day > (CURRENT_DATE - %s::int) AND path = ANY(%s)",
                (int(days), list(paths)))
            covered = (cur.fetchone() or [0])[0] or 0
        out["by_engine"] = {r[0]: int(r[1] or 0) for r in rows}
        out["total_ai_crawls"] = sum(out["by_engine"].values())
        out["days_covered"] = int(covered)
        out["ok"] = True
        if covered < int(days):
            out["limits"].append(
                f"Daily crawl snapshots cover {covered} of the {int(days)} days "
                f"in this window. Cloudflare retains only {MAX_LOOKBACK_DAYS} "
                f"days of request-level analytics, so earlier days can never be "
                f"backfilled — coverage grows one day at a time from the day "
                f"snapshotting started.")
    except Exception as e:
        logger.warning("[sponsor_crawl] snapshot read failed: %s", e)
        out["limits"].append(f"read failed: {e}")
    finally:
        if owned:
            try: conn.close()
            except Exception: pass
    return out
