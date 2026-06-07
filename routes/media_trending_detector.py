"""
media_trending_detector.py — Trending topic detector for DC Hub Media (2026-06-07,
Round 3).

What it does
============
Twice-daily scan of three signal sources to detect what data-center topics are
"trending now," then biases the next 24h of our topic_mix toward whatever is
spiking. The intuition: when DCD + DCF + The Register all write about ERCOT
capacity in the same morning, our LinkedIn cadence should reflect that — not
keep grinding through last week's stale themes.

Signal sources
--------------
1. RSS — already pulled by news_engine.py into the `news` Postgres table. We
   query that table directly (avoid re-fetching feeds we already store) and
   count keyword hits in the last 24h vs the prior 7-day baseline.
2. Reddit — anon JSON API on r/datacenter, r/sysadmin/?q=data+center, and
   r/ArtificialIntelligence with a DC keyword filter. Counts new submissions
   in 24h vs the trailing 7d average (or skipped if Reddit returns 4xx/5xx).
3. LinkedIn — public trending is gated behind LI Premium / Marketing Sales
   API; without an entitled token we surface the slot as
   "needs_li_premium": true so the operator sees it explicitly rather than
   silently returning zeros. If LINKEDIN_TRENDING_TOKEN is set, we attempt
   the /rest/socialMetadata/trending endpoint.

Scoring
-------
For each topic in TOPIC_LIBRARY (taken from media_topic_tuner so we share
ontology), velocity_score = (24h_count / max(1, prior_7d_avg)) × engagement_signal.
Engagement_signal is a per-topic weight (mature topics like dcpi_verdict get
a small boost because anything trending in our specialty domain is more
actionable than a generic AI-capex headline).

Persistence
-----------
Each run writes one row per top-5 topic into `media_trending_topics`
(topic, score, source, detected_at). Same-day same-source rows are upserted
on UNIQUE(topic, source, detected_on::date) so the second 18:00 UTC run of
the day just overwrites the 06:00 row.

Surfacing
---------
GET  /api/v1/admin/media/trending          — JSON of the last run's top 5
GET  /admin/media-trending                 — minimal HTML dashboard
POST /api/v1/admin/media/trending/run-now  — manual trigger
GET  /api/v1/media/trending/top            — public-safe top 3 (no scores,
                                              just labels — for the
                                              homepage "what's trending" tile)

Topic-mix bias
--------------
When a topic shows velocity_score ≥ TRENDING_BIAS_FLOOR (default 2.0, i.e.
2x baseline), the tuner's next run reads the trending row and BUMPS that
topic's weight by +TRENDING_BIAS_WEIGHT (default 0.10) — capped by the
existing 35% per-topic ceiling. This is read-only from the tuner's
perspective; we never overwrite the tuner's own weight.

Schedule
--------
06:00 + 18:00 UTC. Slot picked so that 06:00 lands just after the 06:00
news crawler finishes (which writes the rows we read) and 18:00 lands just
after the 18:00 news crawler.

Safety
------
- MEDIA_R3_DISABLE=1                kills R3 globally (trending +
                                    newsletter A/B + thread generator).
- TRENDING_DETECTOR_DISABLE=1       per-module kill switch.
- Read-only: the detector NEVER writes to social_media_posts /
  linkedin_posts / media_topic_mix directly. It only writes its own
  media_trending_topics rows + surfaces a card on /admin/media-mix.
- Reddit / LinkedIn calls have 8s timeouts and ALWAYS fall back to RSS
  if they fail.
- No Claude calls — pure keyword counting. Free, fast, deterministic.

Tables
------
media_trending_topics
  (id BIGSERIAL, topic TEXT, name TEXT, source TEXT,
   score NUMERIC, count_24h INT, baseline_7d_avg NUMERIC,
   sample_titles JSONB, detected_at TIMESTAMPTZ,
   UNIQUE(topic, source, (detected_at::date)))
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
import math
import datetime
from typing import Any, Iterable

from flask import Blueprint, jsonify, request


media_trending_detector_bp = Blueprint("media_trending_detector", __name__)


# ── Config ──────────────────────────────────────────────────────────────
TRENDING_BIAS_FLOOR    = float(os.environ.get("TRENDING_BIAS_FLOOR")  or "2.0")
TRENDING_BIAS_WEIGHT   = float(os.environ.get("TRENDING_BIAS_WEIGHT") or "0.10")
TOP_N                  = int(  os.environ.get("TRENDING_TOP_N")       or "5")
RSS_LOOKBACK_DAYS      = 7
RSS_RECENT_WINDOW_HRS  = 24
REDDIT_SUBS            = ["datacenter", "sysadmin", "ArtificialIntelligence",
                          "selfhosted", "homelab"]
REDDIT_TIMEOUT_S       = 8
LINKEDIN_TIMEOUT_S     = 8


# ── Auth / plumbing ─────────────────────────────────────────────────────
def _db_conn():
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        return psycopg2.connect(url, connect_timeout=5) if url else None
    except Exception:
        return None


def _admin_or_cron_authorized() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or request.args.get("key") or "")
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    if expected and provided == expected:
        return True
    cron_hdr = request.headers.get("X-Internal-Cron", "")
    cron_env = os.environ.get("DCHUB_CRON_SECRET", "")
    return bool(cron_env) and cron_hdr == cron_env


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"[trending] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _r3_disabled() -> bool:
    v = (os.environ.get("MEDIA_R3_DISABLE") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _module_disabled() -> bool:
    if _r3_disabled():
        return True
    v = (os.environ.get("TRENDING_DETECTOR_DISABLE") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


# ── Topic library — shared with media_topic_tuner ──────────────────────
def _load_topic_library() -> list[dict]:
    """Import shared TOPIC_LIBRARY from media_topic_tuner so we never drift.
    Fail-soft to a minimal fallback if the import breaks (shouldn't happen
    in prod since both ship in the same deploy)."""
    try:
        from routes.media_topic_tuner import TOPIC_LIBRARY  # type: ignore
        return list(TOPIC_LIBRARY)
    except Exception as e:
        _log(f"shared TOPIC_LIBRARY import failed, using fallback: {e}")
        # Tiny fallback so a partial deploy doesn't crash the cron.
        return [
            {"topic": "ai_capex",        "name": "AI Capex",
             "patterns": [r"AI\s+capex", r"GPU\s+(buildout|cluster)"]},
            {"topic": "grid_alert",      "name": "Grid / ISO",
             "patterns": [r"\bERCOT\b", r"\bPJM\b", r"\bgrid\b"]},
            {"topic": "hyperscaler_deal","name": "Hyperscaler Deal",
             "patterns": [r"\b(AWS|Microsoft|Google|Meta|Oracle)\b"]},
            {"topic": "facility_news",   "name": "New Facility",
             "patterns": [r"\bgroundbreak", r"campus.*announce"]},
            {"topic": "energy_pricing",  "name": "Energy / LMP",
             "patterns": [r"\bLMP\b", r"\$/MWh"]},
        ]


# Per-topic engagement signal weights. Specialty DC-Hub topics get a small
# multiplier so a 2x ERCOT spike beats a 2x generic-AI spike (we'd rather
# write about something we own than chase noise).
_ENGAGEMENT_SIGNAL = {
    "dcpi_verdict":   1.5,
    "verdict_shift":  1.5,
    "grid_alert":     1.3,
    "hyperscaler_deal": 1.2,
    "ai_capex":       1.1,
    "ma_transaction": 1.1,
    "market_brief":   1.2,
    "facility_news":  1.1,
    "fiber_route":    1.0,
    "energy_pricing": 1.0,
    "water_risk":     0.9,
    "renewable_energy": 0.9,
    "industry_pulse": 0.9,
    "ai_citation":    0.8,
}


# ── Schema ──────────────────────────────────────────────────────────────
def init_trending_tables() -> bool:
    conn = _db_conn()
    if conn is None:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_trending_topics (
                    id              BIGSERIAL PRIMARY KEY,
                    topic           TEXT NOT NULL,
                    name            TEXT,
                    source          TEXT NOT NULL,
                    score           NUMERIC NOT NULL DEFAULT 0,
                    count_24h       INTEGER NOT NULL DEFAULT 0,
                    baseline_7d_avg NUMERIC NOT NULL DEFAULT 0,
                    sample_titles   JSONB DEFAULT '[]'::jsonb,
                    detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            # Same-day same-source same-topic rows upsert via a partial unique
            # index keyed on the date-truncated timestamp.
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS media_trending_topics_uq
                    ON media_trending_topics(topic, source, ((detected_at AT TIME ZONE 'UTC')::date))
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS media_trending_topics_score_idx
                    ON media_trending_topics(detected_at DESC, score DESC)
            """)
        return True
    except Exception as e:
        _log(f"schema bootstrap failed: {e}")
        return False
    finally:
        try: conn.close()
        except Exception: pass


try:
    _SCHEMA_OK = init_trending_tables()
except Exception:
    _SCHEMA_OK = False


# ── Source 1: RSS via news_engine's `news` Postgres table ──────────────
def _rss_topic_counts(library: list[dict]) -> list[dict]:
    """For each topic, count news rows last 24h vs the 7d baseline average.
    `news` table written by news_engine.fetch_all_feeds(). Fail-soft."""
    conn = _db_conn()
    if conn is None:
        return []
    out: list[dict] = []
    try:
        with conn, conn.cursor() as cur:
            # Probe which news table we have. news_engine.py writes to `news`
            # but older deploys used `news_articles` — handle both.
            table_name = None
            for candidate in ("news", "news_articles"):
                try:
                    cur.execute(
                        "SELECT to_regclass(%s) IS NOT NULL", (f"public.{candidate}",))
                    row = cur.fetchone()
                    if row and row[0]:
                        table_name = candidate
                        break
                except Exception:
                    continue
            if not table_name:
                _log("no news table found; rss source contributes 0")
                return []
            # Probe column names — `news` uses (title, summary, published_at);
            # `news_articles` may use (headline, body, created_at).
            title_col = "title"
            body_col = "summary"
            date_col = "published_at"
            for candidate, fallbacks in (
                ("title",       ["title", "headline", "name"]),
                ("summary",     ["summary", "body", "description", "content"]),
                ("published_at",["published_at", "created_at", "fetched_at"]),
            ):
                for fc in fallbacks:
                    try:
                        cur.execute(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_name=%s AND column_name=%s LIMIT 1",
                            (table_name, fc))
                        if cur.fetchone():
                            if candidate == "title":      title_col = fc
                            if candidate == "summary":    body_col = fc
                            if candidate == "published_at": date_col = fc
                            break
                    except Exception:
                        continue
            for entry in library:
                topic = entry["topic"]
                name = entry["name"]
                # Build a regex union of patterns. Postgres' SIMILAR TO and
                # ~ both accept POSIX; we use `~*` (case-insensitive). To
                # keep the query simple we concatenate title+body and run
                # one regex per row, scoring all patterns together.
                pat_union = "|".join(f"({p})" for p in entry["patterns"])
                if not pat_union:
                    continue
                try:
                    cur.execute(f"""
                        SELECT
                          SUM(CASE WHEN {date_col} >= NOW() - INTERVAL '24 hours'
                                   THEN 1 ELSE 0 END) AS c24,
                          SUM(CASE WHEN {date_col} <  NOW() - INTERVAL '24 hours'
                                    AND {date_col} >= NOW() - INTERVAL '%s days'
                                   THEN 1 ELSE 0 END) AS c_prior
                          FROM {table_name}
                         WHERE (COALESCE({title_col},'') || ' ' ||
                                COALESCE({body_col},'')) ~* %s
                           AND {date_col} >= NOW() - INTERVAL '%s days'
                    """, (RSS_LOOKBACK_DAYS, pat_union,
                          RSS_LOOKBACK_DAYS + 1))
                    row = cur.fetchone() or (0, 0)
                    c24 = int(row[0] or 0)
                    prior = int(row[1] or 0)
                    baseline = (prior / float(RSS_LOOKBACK_DAYS - 1)) if (RSS_LOOKBACK_DAYS - 1) > 0 else 0.0
                    # Pull up to 3 sample titles for the surface card
                    cur.execute(f"""
                        SELECT {title_col} FROM {table_name}
                         WHERE (COALESCE({title_col},'') || ' ' ||
                                COALESCE({body_col},'')) ~* %s
                           AND {date_col} >= NOW() - INTERVAL '24 hours'
                         ORDER BY {date_col} DESC
                         LIMIT 3
                    """, (pat_union,))
                    samples = [str(r[0] or "")[:160] for r in (cur.fetchall() or [])]
                    out.append({
                        "topic": topic,
                        "name": name,
                        "source": "rss",
                        "count_24h": c24,
                        "baseline_7d_avg": round(baseline, 2),
                        "samples": samples,
                    })
                except Exception as e:
                    _log(f"rss query failed for {topic}: {e}")
                    continue
    except Exception as e:
        _log(f"rss topic_counts failed: {e}")
    finally:
        try: conn.close()
        except Exception: pass
    return out


# ── Source 2: Reddit anon JSON ─────────────────────────────────────────
_DC_TERMS = re.compile(
    r"(data\s*center|datacenter|colocation|hyperscale|nvidia\s+h\d+|"
    r"\bDCPI\b|\b(ERCOT|PJM|MISO|CAISO|SPP|NYISO)\b|"
    r"\b(AWS|Azure|GCP|Meta|Oracle)\b.*(region|capex|deal)|"
    r"\bMW\b|\bGW\b|interconnection)",
    re.IGNORECASE)


def _reddit_24h_titles() -> list[str]:
    """Pull the recent post titles from our subreddit list, DC-filtered.
    Returns up to 100 titles. Fail-soft (returns [] on any error)."""
    titles: list[str] = []
    try:
        import urllib.request
        for sub in REDDIT_SUBS[:5]:
            try:
                url = f"https://www.reddit.com/r/{sub}/new.json?limit=50"
                req = urllib.request.Request(url, headers={
                    "User-Agent": "dchub-trending-detector/1.0 (+https://dchub.cloud)"
                })
                with urllib.request.urlopen(req, timeout=REDDIT_TIMEOUT_S) as r:
                    data = json.loads(r.read().decode("utf-8", errors="replace"))
                kids = (data or {}).get("data", {}).get("children", [])
                cutoff = time.time() - (RSS_RECENT_WINDOW_HRS * 3600)
                for k in kids:
                    d = (k or {}).get("data") or {}
                    title = str(d.get("title") or "")
                    created = float(d.get("created_utc") or 0)
                    if not title or created < cutoff:
                        continue
                    if d.get("subreddit", "").lower() == "datacenter":
                        # Whole subreddit is on-topic; no keyword filter
                        titles.append(title)
                    elif _DC_TERMS.search(title):
                        titles.append(title)
                time.sleep(0.5)  # be a good citizen
            except Exception as e:
                _log(f"reddit /r/{sub} failed: {e}")
                continue
    except Exception as e:
        _log(f"reddit fetch failed: {e}")
    return titles[:200]


def _reddit_topic_counts(library: list[dict],
                          titles: list[str]) -> list[dict]:
    """Score reddit titles per topic. Baseline = same titles × prior 7d
    is not available from /new (only last ~24h); we approximate baseline
    by dividing total topic-hit volume by topic count (i.e. fair share)
    and use the deviation from fair share as our velocity proxy."""
    if not titles:
        return []
    out: list[dict] = []
    total_hits = 0
    per_topic: dict[str, int] = {}
    per_samples: dict[str, list[str]] = {}
    for entry in library:
        topic = entry["topic"]
        regex = "|".join(f"({p})" for p in entry["patterns"])
        if not regex:
            continue
        rx = re.compile(regex, re.IGNORECASE)
        per_topic[topic] = 0
        per_samples[topic] = []
        for t in titles:
            if rx.search(t):
                per_topic[topic] += 1
                if len(per_samples[topic]) < 3:
                    per_samples[topic].append(t[:160])
        total_hits += per_topic[topic]
    fair_share = (total_hits / max(1, len([e for e in library if e["patterns"]])))
    for entry in library:
        topic = entry["topic"]
        if topic not in per_topic:
            continue
        c24 = per_topic[topic]
        baseline = max(1.0, fair_share)
        out.append({
            "topic": topic,
            "name": entry["name"],
            "source": "reddit",
            "count_24h": c24,
            "baseline_7d_avg": round(baseline, 2),
            "samples": per_samples.get(topic, []),
        })
    return out


# ── Source 3: LinkedIn ─────────────────────────────────────────────────
def _linkedin_trending(library: list[dict]) -> dict:
    """LinkedIn public-trending requires the Marketing Sales API + an
    entitled token, which our standard org token doesn't have. We attempt
    if LINKEDIN_TRENDING_TOKEN is set; otherwise surface
    needs_li_premium=True so the operator sees the slot but knows why
    it's empty."""
    token = (os.environ.get("LINKEDIN_TRENDING_TOKEN") or "").strip()
    if not token:
        return {"needs_li_premium": True, "rows": []}
    # Best-effort: try /rest/trending — endpoint is under flux at LI; we
    # fail-soft to needs_li_premium on any non-200 so the dashboard never
    # blows up. Reserved for the day LI exposes a stable endpoint.
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.linkedin.com/rest/socialMetadata/trending",
            headers={
                "Authorization": f"Bearer {token}",
                "LinkedIn-Version": "202601",
                "X-Restli-Protocol-Version": "2.0.0",
            })
        with urllib.request.urlopen(req, timeout=LINKEDIN_TIMEOUT_S) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        # Shape is hypothetical until LI documents it; we just look for
        # any string fields that match our topic patterns.
        text_dump = json.dumps(data)[:50_000]
        rows: list[dict] = []
        for entry in library:
            regex = "|".join(f"({p})" for p in entry["patterns"])
            if not regex:
                continue
            hits = len(re.findall(regex, text_dump, re.IGNORECASE))
            if hits == 0:
                continue
            rows.append({
                "topic": entry["topic"],
                "name": entry["name"],
                "source": "linkedin",
                "count_24h": hits,
                "baseline_7d_avg": 1.0,  # no historical baseline available
                "samples": [],
            })
        return {"needs_li_premium": False, "rows": rows}
    except Exception as e:
        _log(f"linkedin trending fetch failed (token set): {e}")
        return {"needs_li_premium": True, "rows": []}


# ── Scoring + persistence ──────────────────────────────────────────────
def _score(c24: int, baseline: float, signal: float) -> float:
    """velocity_score = (24h / max(1, baseline)) × engagement_signal."""
    velocity = (float(c24) / max(1.0, float(baseline)))
    return velocity * float(signal)


def _merge_topics(*lists: Iterable[dict]) -> list[dict]:
    """Sum counts + average baselines across sources, then score once.
    Per-source rows are still persisted separately so the dashboard can
    show "rss=4 / reddit=2" without re-querying."""
    bucket: dict[str, dict] = {}
    for source_list in lists:
        for r in source_list or []:
            t = r["topic"]
            cur = bucket.setdefault(t, {
                "topic": t,
                "name": r.get("name") or t,
                "count_24h": 0,
                "baseline_7d_avg": 0.0,
                "samples": [],
                "_sources": set(),
            })
            cur["count_24h"] += int(r.get("count_24h") or 0)
            cur["baseline_7d_avg"] = max(cur["baseline_7d_avg"],
                                          float(r.get("baseline_7d_avg") or 0))
            cur["_sources"].add(r.get("source") or "unknown")
            for s in (r.get("samples") or []):
                if len(cur["samples"]) < 5 and s not in cur["samples"]:
                    cur["samples"].append(s)
    out: list[dict] = []
    for t, row in bucket.items():
        signal = _ENGAGEMENT_SIGNAL.get(t, 1.0)
        row["score"] = round(_score(row["count_24h"],
                                     row["baseline_7d_avg"],
                                     signal), 3)
        row["sources"] = sorted(row.pop("_sources"))
        out.append(row)
    out.sort(key=lambda x: (-x["score"], -x["count_24h"]))
    return out


def _persist_rows(rows: list[dict], per_source_rows: list[dict]) -> int:
    """Write merged top-N + every per-source row. UPSERT keyed on
    (topic, source, detected_at::date) so re-runs on the same day overwrite
    the same row idempotently."""
    if not rows and not per_source_rows:
        return 0
    conn = _db_conn()
    if conn is None:
        return 0
    n = 0
    try:
        with conn, conn.cursor() as cur:
            today = datetime.date.today().isoformat()
            # Persist top-N merged rows under source='merged' for the public
            # surface; persist per-source rows for diagnostic
            for r in rows[:TOP_N]:
                try:
                    cur.execute("""
                        INSERT INTO media_trending_topics
                            (topic, name, source, score, count_24h,
                             baseline_7d_avg, sample_titles)
                        VALUES (%s, %s, 'merged', %s, %s, %s, %s::jsonb)
                        ON CONFLICT (topic, source, ((detected_at AT TIME ZONE 'UTC')::date)) DO UPDATE SET
                          score = EXCLUDED.score,
                          count_24h = EXCLUDED.count_24h,
                          baseline_7d_avg = EXCLUDED.baseline_7d_avg,
                          sample_titles = EXCLUDED.sample_titles,
                          name = EXCLUDED.name
                    """, (r["topic"], r.get("name"), r["score"],
                          r["count_24h"], r["baseline_7d_avg"],
                          json.dumps(r.get("samples", []))))
                    n += 1
                except Exception as e:
                    _log(f"merged upsert failed for {r['topic']}: {e}")
                    continue
            for r in per_source_rows:
                try:
                    signal = _ENGAGEMENT_SIGNAL.get(r["topic"], 1.0)
                    score = round(_score(r["count_24h"],
                                          r["baseline_7d_avg"], signal), 3)
                    cur.execute("""
                        INSERT INTO media_trending_topics
                            (topic, name, source, score, count_24h,
                             baseline_7d_avg, sample_titles)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (topic, source, ((detected_at AT TIME ZONE 'UTC')::date)) DO UPDATE SET
                          score = EXCLUDED.score,
                          count_24h = EXCLUDED.count_24h,
                          baseline_7d_avg = EXCLUDED.baseline_7d_avg,
                          sample_titles = EXCLUDED.sample_titles,
                          name = EXCLUDED.name
                    """, (r["topic"], r.get("name"), r["source"], score,
                          r["count_24h"], r["baseline_7d_avg"],
                          json.dumps(r.get("samples", []))))
                    n += 1
                except Exception:
                    continue
    except Exception as e:
        _log(f"persist failed: {e}")
    finally:
        try: conn.close()
        except Exception: pass
    return n


def detect_trending() -> dict:
    """Run all three sources, merge, persist top-5, return the dict."""
    if _module_disabled():
        return {"ok": False, "reason": "disabled", "rows": []}
    library = _load_topic_library()
    started = time.time()
    rss_rows    = _rss_topic_counts(library)
    reddit_titles = _reddit_24h_titles()
    reddit_rows = _reddit_topic_counts(library, reddit_titles)
    li_data     = _linkedin_trending(library)
    merged      = _merge_topics(rss_rows, reddit_rows, li_data.get("rows") or [])
    written     = _persist_rows(merged, rss_rows + reddit_rows + (li_data.get("rows") or []))
    return {
        "ok": True,
        "ran_for_ms": int((time.time() - started) * 1000),
        "rss_topics": len(rss_rows),
        "reddit_titles": len(reddit_titles),
        "reddit_topics": len(reddit_rows),
        "linkedin_topics": len(li_data.get("rows") or []),
        "linkedin_needs_premium": bool(li_data.get("needs_li_premium")),
        "top": merged[:TOP_N],
        "rows_written": written,
        "detected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def latest_trending(top_n: int = TOP_N) -> list[dict]:
    """Read back the most recent run's top-N merged rows. Used by both the
    dashboard card and the public homepage tile."""
    conn = _db_conn()
    if conn is None:
        return []
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT topic, name, score, count_24h, baseline_7d_avg,
                       sample_titles, detected_at
                  FROM media_trending_topics
                 WHERE source = 'merged'
                   AND detected_at >= NOW() - INTERVAL '2 days'
                 ORDER BY detected_at DESC, score DESC
                 LIMIT %s
            """, (int(top_n) * 3,))  # take some headroom for dedup
            seen: set[str] = set()
            out: list[dict] = []
            for r in cur.fetchall() or []:
                topic = str(r[0] or "")
                if topic in seen:
                    continue
                seen.add(topic)
                samples = r[5] or []
                if isinstance(samples, str):
                    try: samples = json.loads(samples)
                    except Exception: samples = []
                out.append({
                    "topic": topic,
                    "name": r[1] or topic,
                    "score": float(r[2] or 0),
                    "count_24h": int(r[3] or 0),
                    "baseline_7d_avg": float(r[4] or 0),
                    "samples": samples or [],
                    "detected_at": r[6].isoformat() if r[6] else None,
                })
                if len(out) >= int(top_n):
                    break
            return out
    except Exception as e:
        _log(f"latest_trending read failed: {e}")
        return []
    finally:
        try: conn.close()
        except Exception: pass


def trending_bias_map() -> dict[str, float]:
    """Return {topic: bias_weight} where bias_weight > 0 for topics whose
    most recent merged score is above TRENDING_BIAS_FLOOR. The tuner reads
    this and ADDS bias to its computed weight (capped by the 35% ceiling
    that the tuner already enforces)."""
    out: dict[str, float] = {}
    for r in latest_trending(TOP_N):
        if float(r.get("score") or 0) >= TRENDING_BIAS_FLOOR:
            out[r["topic"]] = TRENDING_BIAS_WEIGHT
    return out


# ── HTTP endpoints ──────────────────────────────────────────────────────
@media_trending_detector_bp.route("/api/v1/admin/media/trending", methods=["GET"])
def http_trending_json():
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    rows = latest_trending(TOP_N)
    return jsonify({
        "ok": True,
        "top_n": TOP_N,
        "rows": rows,
        "bias_floor": TRENDING_BIAS_FLOOR,
        "bias_weight": TRENDING_BIAS_WEIGHT,
        "disabled": _module_disabled(),
        "r3_kill": _r3_disabled(),
    })


@media_trending_detector_bp.route("/api/v1/admin/media/trending/run-now",
                                    methods=["POST"])
def http_trending_run_now():
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    res = detect_trending()
    return jsonify(res)


@media_trending_detector_bp.route("/api/v1/media/trending/top", methods=["GET"])
def http_trending_public():
    """Public-safe top 3 — no scores, no sample article titles (those leak
    publisher info). Just the topic label + a coarse "hot" badge. Cached
    5min via Cache-Control."""
    rows = latest_trending(3)
    safe = [{
        "topic": r["topic"],
        "name": r["name"],
        "hot": float(r.get("score") or 0) >= TRENDING_BIAS_FLOOR,
    } for r in rows]
    resp = jsonify({"ok": True, "rows": safe})
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@media_trending_detector_bp.route("/admin/media-trending", methods=["GET"])
def http_trending_dashboard():
    """Minimal HTML — same look as /admin/media-mix. Operators can hit this
    directly without going through the topic-mix surface."""
    if not _admin_or_cron_authorized():
        return ("Unauthorized — pass ?key=<DCHUB_ADMIN_KEY> or X-Admin-Key.",
                401, {"Content-Type": "text/plain"})
    rows = latest_trending(TOP_N)
    is_disabled = _module_disabled()
    li_premium_warned = False
    # Check the last per-source linkedin row to decide whether to warn
    try:
        conn = _db_conn()
        if conn is not None:
            with conn, conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM media_trending_topics
                     WHERE source='linkedin'
                       AND detected_at >= NOW() - INTERVAL '24 hours'
                """)
                row = cur.fetchone() or (0,)
                if int(row[0] or 0) == 0:
                    li_premium_warned = True
            conn.close()
    except Exception:
        pass
    rows_html = "".join(
        f"<tr><td><b>{(r.get('name') or r['topic'])}</b><br>"
        f"<span class='muted' style='font-size:11px'>{r['topic']}</span></td>"
        f"<td class='num'>{r['count_24h']}</td>"
        f"<td class='num'>{r['baseline_7d_avg']:.2f}</td>"
        f"<td class='num'><b>{r['score']:.2f}</b></td>"
        f"<td>" + "".join(
            f"<div class='muted' style='font-size:11px'>· {s}</div>"
            for s in (r.get('samples') or [])[:3]
        ) + "</td></tr>"
        for r in rows
    ) or "<tr><td colspan=5 class='muted'>No trending rows yet — POST /api/v1/admin/media/trending/run-now</td></tr>"
    status_pill = ("<span class='pill' style='background:#3a0e0e;color:#ff8a8a'>DISABLED</span>"
                   if is_disabled else
                   "<span class='pill'>LIVE</span>")
    li_warn = ("<p class='muted' style='color:#ffd166'>"
               "LinkedIn trending source returned no rows in 24h — "
               "set <code>LINKEDIN_TRENDING_TOKEN</code> with a "
               "Marketing Sales API token, or accept that the slot is "
               "Reddit+RSS-only.</p>") if li_premium_warned else ""
    return (f"""<!doctype html><html><head><meta charset="utf-8">
<title>Trending · DC Hub Admin</title>
<meta name="robots" content="noindex,nofollow">
<style>
body{{font:14px/1.5 -apple-system,system-ui,sans-serif;background:#0a0d12;color:#e7ecf3;
     max-width:1100px;margin:24px auto;padding:0 16px}}
h1{{font-size:22px;margin:0 0 4px}}
.muted{{color:#6b7785}}
.pill{{display:inline-block;padding:2px 10px;border-radius:14px;
       background:#1d2630;color:#9ec5fe;font-size:11px}}
table{{width:100%;border-collapse:collapse;margin-bottom:16px}}
th{{text-align:left;padding:6px 8px;border-bottom:1px solid #20283a;
     color:#97a3b6;font-weight:500;font-size:12px}}
td{{padding:7px 8px;border-bottom:1px solid #161c28;vertical-align:top}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
code{{background:#131a25;padding:1px 6px;border-radius:4px}}
</style></head><body>
<h1>Trending Now {status_pill}</h1>
<p class='muted'>Twice-daily (06:00 + 18:00 UTC) detector across RSS, Reddit,
LinkedIn. Top {TOP_N} topics bias the next 24h of topic_mix when score
>= {TRENDING_BIAS_FLOOR:.1f}x baseline (+{TRENDING_BIAS_WEIGHT:.2f} weight).</p>
{li_warn}
<h2 style='font-size:16px;color:#9ec5fe'>Top {TOP_N} Trending</h2>
<table><thead><tr><th>Topic</th><th>24h</th><th>7d avg</th><th>Score</th>
<th>Sample headlines</th></tr></thead><tbody>{rows_html}</tbody></table>
<p class='muted' style='margin-top:24px;font-size:13px'>
<code>POST /api/v1/admin/media/trending/run-now</code> ·
<code>GET /api/v1/admin/media/trending</code> ·
<code>GET /api/v1/media/trending/top</code> (public top 3)
</p></body></html>""", 200, {"Content-Type": "text/html; charset=utf-8",
                              "Cache-Control": "no-store"})


def render_trending_card_html() -> str:
    """Drop-in HTML fragment to embed on /admin/media-mix as the
    "Trending Now" card. Kept as a function so the topic_tuner can
    render it without circular import (we import this here, not the
    other way around)."""
    rows = latest_trending(TOP_N)
    if not rows:
        return ("<h2>Trending Now</h2>"
                "<p class='muted'>No trending data yet. Cron fires at 06:00 + 18:00 UTC, "
                "or trigger manually with <code>POST /api/v1/admin/media/trending/run-now</code>.</p>")
    rows_html = "".join(
        f"<tr><td><b>{r.get('name') or r['topic']}</b></td>"
        f"<td class='num'>{r['count_24h']}</td>"
        f"<td class='num'>{r['baseline_7d_avg']:.2f}</td>"
        f"<td class='num'><b>{r['score']:.2f}x</b></td></tr>"
        for r in rows
    )
    return (f"<h2>Trending Now (Round 3 · last run "
            f"{rows[0].get('detected_at', '')[:16].replace('T', ' ')})</h2>"
            f"<table><thead><tr><th>Topic</th><th>24h</th>"
            f"<th>7d avg</th><th>Score</th></tr></thead>"
            f"<tbody>{rows_html}</tbody></table>")


# ── Cron entry point ───────────────────────────────────────────────────
def _run_media_trending_detect():
    """Called twice daily by crawler_scheduler.py (06/18 UTC)."""
    if _module_disabled():
        _log("skipped — disabled")
        return
    try:
        res = detect_trending()
        _log(f"detect ran_for_ms={res.get('ran_for_ms')} "
             f"rss={res.get('rss_topics')} reddit={res.get('reddit_topics')} "
             f"li={res.get('linkedin_topics')} top={len(res.get('top') or [])} "
             f"written={res.get('rows_written')}")
    except Exception as e:
        _log(f"detect cron failed: {e}")


__all__ = [
    "media_trending_detector_bp",
    "detect_trending",
    "latest_trending",
    "trending_bias_map",
    "render_trending_card_html",
    "_run_media_trending_detect",
    "init_trending_tables",
]
