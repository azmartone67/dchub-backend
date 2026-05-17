"""Phase HHHHH (2026-05-16) — external backlink hunter.

TTTT measures AI citations. This measures HUMAN citations — where
DC Hub is mentioned on the open web. Daily polls free public APIs
(HackerNews Algolia, Reddit search) for "dchub.cloud" mentions.

  POST /api/v1/mentions/hunt        admin cron entry
  GET  /api/v1/mentions/recent      public mention feed (last 50)
  GET  /api/v1/mentions/stats       mention count by source / 7d

New brain detector external_mentions_dropoff: fires if 7-day count
drops >40% vs trailing 28-day baseline. Counterpart to TTTT for
external-world signal.
"""

from __future__ import annotations

import os
import datetime
import requests
from flask import Blueprint, jsonify, request


backlink_hunter_bp = Blueprint("backlink_hunter", __name__)


_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


# Search terms to probe for. Include exact-match domain + brand
# variants so we catch indirect mentions too.
_QUERY_TERMS = ["dchub.cloud", "\"DC Hub\" data center"]


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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS external_mentions (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,         -- hackernews|reddit|...
    source_url      TEXT NOT NULL,
    title           TEXT,
    snippet         TEXT,
    author          TEXT,
    posted_at       TIMESTAMPTZ,
    discovered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    points          INT,                    -- HN points, Reddit score
    comment_count   INT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mentions_url
    ON external_mentions(source_url);
CREATE INDEX IF NOT EXISTS ix_mentions_recent
    ON external_mentions(discovered_at DESC);
CREATE INDEX IF NOT EXISTS ix_mentions_source
    ON external_mentions(source, discovered_at DESC);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


def _hunt_hackernews(query: str) -> list[dict]:
    """HN Algolia API — free, no auth, supports query + tag filters."""
    out = []
    try:
        r = requests.get(
            "https://hn.algolia.com/api/v1/search",
            params={"query": query, "tags": "(story,comment)",
                    "hitsPerPage": 25},
            timeout=10,
        )
        if r.status_code != 200: return out
        for hit in (r.json().get("hits") or [])[:25]:
            obj_id = hit.get("objectID")
            title  = hit.get("title") or hit.get("story_title") or ""
            url    = (f"https://news.ycombinator.com/item?id={obj_id}"
                      if obj_id else hit.get("url") or "")
            snippet = (hit.get("comment_text") or hit.get("story_text") or "")[:400]
            out.append({
                "source":      "hackernews",
                "source_url":  url,
                "title":       title[:200],
                "snippet":     snippet,
                "author":      hit.get("author"),
                "posted_at":   hit.get("created_at"),
                "points":      hit.get("points"),
                "comment_count": hit.get("num_comments"),
            })
    except Exception: pass
    return out


def _hunt_reddit(query: str) -> list[dict]:
    """Reddit's public JSON search. Free, no auth needed for search."""
    out = []
    try:
        r = requests.get(
            "https://www.reddit.com/search.json",
            params={"q": query, "limit": 25, "sort": "new"},
            headers={"User-Agent": "DCHub-Mention-Hunter/1.0"},
            timeout=10,
        )
        if r.status_code != 200: return out
        for child in ((r.json().get("data") or {}).get("children") or [])[:25]:
            d = child.get("data") or {}
            out.append({
                "source":      "reddit",
                "source_url":  f"https://reddit.com{d.get('permalink', '')}",
                "title":       (d.get("title") or "")[:200],
                "snippet":     (d.get("selftext") or "")[:400],
                "author":      d.get("author"),
                "posted_at":   datetime.datetime.utcfromtimestamp(
                                  d.get("created_utc", 0)
                              ).isoformat() if d.get("created_utc") else None,
                "points":      d.get("score"),
                "comment_count": d.get("num_comments"),
            })
    except Exception: pass
    return out


def hunt_all() -> dict:
    out: dict = {"sources": {}, "new_mentions": 0,
                  "ran_at": datetime.datetime.utcnow().isoformat() + "Z"}
    all_hits = []
    for q in _QUERY_TERMS:
        for source_fn in (_hunt_hackernews, _hunt_reddit):
            hits = source_fn(q)
            out["sources"][f"{source_fn.__name__}:{q}"] = len(hits)
            all_hits.extend(hits)

    c = _conn()
    if c is None:
        out["error"] = "no_database"; return out
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            for h in all_hits:
                try:
                    cur.execute("""
                        INSERT INTO external_mentions
                          (source, source_url, title, snippet, author,
                           posted_at, points, comment_count)
                        VALUES (%s, %s, %s, %s, %s,
                                CASE WHEN %s ~ '^\\d{4}-' THEN %s::timestamptz ELSE NULL END,
                                %s, %s)
                        ON CONFLICT (source_url) DO NOTHING
                        RETURNING id
                    """, (h["source"], h["source_url"], h.get("title"),
                          h.get("snippet"), h.get("author"),
                          h.get("posted_at") or "", h.get("posted_at") or "",
                          h.get("points"), h.get("comment_count")))
                    if cur.fetchone():
                        out["new_mentions"] += 1
                except Exception: continue
    finally:
        try: c.close()
        except Exception: pass
    out["total_hits"] = len(all_hits)
    return out


@backlink_hunter_bp.route("/api/v1/mentions/hunt", methods=["POST"])
def hunt_endpoint():
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    return jsonify(hunt_all()), 200


@backlink_hunter_bp.route("/api/v1/mentions/recent", methods=["GET"])
def recent_mentions():
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT source, source_url, title, snippet, author,
                       posted_at, discovered_at, points, comment_count
                  FROM external_mentions
                 ORDER BY discovered_at DESC LIMIT 50
            """)
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    out = [{
        "source":         r["source"],
        "url":            r["source_url"],
        "title":          r["title"],
        "snippet":        r["snippet"],
        "author":         r["author"],
        "posted_at":      r["posted_at"].isoformat() if r["posted_at"] else None,
        "discovered_at":  r["discovered_at"].isoformat() if r["discovered_at"] else None,
        "points":         r["points"],
        "comments":       r["comment_count"],
    } for r in rows]
    resp = jsonify(mentions=out, count=len(out))
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@backlink_hunter_bp.route("/api/v1/mentions/stats", methods=["GET"])
def stats_endpoint():
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    out = {"total": 0, "by_source": {},
           "discovered_24h": 0, "discovered_7d": 0,
           "discovered_28d": 0}
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT source, COUNT(*) FROM external_mentions
                     GROUP BY source
                """)
                out["by_source"] = {r[0]: int(r[1] or 0) for r in cur.fetchall()}
                cur.execute("""
                    SELECT COUNT(*),
                           COUNT(*) FILTER (WHERE discovered_at >= NOW() - INTERVAL '24 hours'),
                           COUNT(*) FILTER (WHERE discovered_at >= NOW() - INTERVAL '7 days'),
                           COUNT(*) FILTER (WHERE discovered_at >= NOW() - INTERVAL '28 days')
                      FROM external_mentions
                """)
                r = cur.fetchone() or (0, 0, 0, 0)
                out["total"]          = int(r[0] or 0)
                out["discovered_24h"] = int(r[1] or 0)
                out["discovered_7d"]  = int(r[2] or 0)
                out["discovered_28d"] = int(r[3] or 0)
            except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
