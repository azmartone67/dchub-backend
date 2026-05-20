"""Phase FF+25-followup-r23 (2026-05-20) — news entity extraction.
==========================================================================

The user's broader goal tonight: "we need to have better data"
(after finding DCHawk had Calgary facilities we didn't).

The OSM crawler closes the gap with known-public registries. But it
only catches what's already mapped. To LEAD instead of follow, we
need to catch facility announcements the moment they hit the news —
24-72 hours before they appear in DCHawk or DCM.

We already ingest news (news_items table). What we don't do: extract
entity mentions and check whether we know that operator/facility.

THIS MODULE
===========

Two-tier extraction strategy:

  TIER 1 (cheap, always runs): regex-based pattern matching.
    · Capitalized phrases adjacent to "data center"/"data centre" /
      "campus" / "facility" / "MW" / "colocation"
    · Common operator-name patterns ("X Industries", "Y Holdings")
    · Filters out generic noise (cities, vague phrases)

  TIER 2 (LLM, opt-in): Claude Haiku via ANTHROPIC_API_KEY.
    · Takes the news headline + first 500 chars of body
    · Returns structured {facility_names: [], operator_names: []}
    · ~$0.001 per article. ~$30/month for full ingest at current
      volume. Off by default (NEWS_NER_LLM=false).

For each extracted name:
  · LOWER(name) lookup against facilities table
  · If unknown AND looks like a real name (length >= 4, has uppercase,
    not all-numeric), flag as discovery candidate
  · Persist to news_discovered_entities table for the brain to
    surface in the Inspector signal block

ENDPOINTS
=========
  POST /api/v1/admin/news-ner/run           admin: scan recent news
                                              ?days=7 (default 1)
                                              ?dry_run=1
  GET  /api/v1/admin/news-ner/candidates    list unknown entities
                                              (admin-only; might be PII)
  GET  /api/v1/admin/news-ner/status        last-run summary
"""
import os
import re
import time
import json
import logging
import datetime
import hashlib
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
news_ner_bp = Blueprint("news_ner", __name__)


_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


# ── Tunables ─────────────────────────────────────────────────────────
USE_LLM = (os.environ.get("NEWS_NER_LLM", "false")
            .lower() in ("1", "true", "yes"))
LLM_MODEL = (os.environ.get("NEWS_NER_LLM_MODEL") or "").strip()  # falls
                                                                    # back to
                                                                    # voice tier
MAX_PER_RUN = int(os.environ.get("NEWS_NER_MAX", "200"))


# ── Storage ──────────────────────────────────────────────────────────
def _ensure_table():
    c = _get_db()
    if c is None: return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS news_discovered_entities (
                    id              SERIAL PRIMARY KEY,
                    entity_name     TEXT NOT NULL,
                    entity_type     TEXT,
                    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    mention_count   INT NOT NULL DEFAULT 1,
                    sample_news_id  TEXT,
                    sample_headline TEXT,
                    sample_url      TEXT,
                    in_facilities   BOOLEAN DEFAULT FALSE,
                    status          TEXT DEFAULT 'unknown',
                    notes           TEXT
                )
            """)
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ix_news_entity_name "
                "ON news_discovered_entities(LOWER(entity_name))"
            )
        try: c.commit()
        except Exception: pass
    except Exception as e:
        logger.warning(f"[news-ner] table create failed: {e}")
    finally:
        try: c.close()
        except Exception: pass


# ── Regex-based extractor ────────────────────────────────────────────
# Looks for capitalized phrases (1-4 words, Title Case or ALL CAPS) that
# appear within 60 chars of trigger words.
TRIGGERS = (
    "data center", "data centre", "data-center", "datacentre",
    "colocation", "colo facility", "hyperscale", "campus",
    "facility", "data hall", "MW campus", "MW data",
)

# Capitalized-phrase regex (greedy 1-4 words, allows & and -)
_NAME_RE = re.compile(
    r'\b([A-Z][A-Za-z0-9&\-]+(?:\s+[A-Z][A-Za-z0-9&\-]+){0,3})\b'
)

# Phrases that look like operator names but are noise — filter out
NOISE = {
    "data center", "data centre", "datacenter", "datacentre",
    "north america", "south america", "united states", "united kingdom",
    "european union", "asia pacific", "middle east",
    "new york", "san francisco", "los angeles", "las vegas",
    "ai", "ml", "cloud", "edge", "the", "and", "for", "with",
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "january", "february", "march", "april",
    "may", "june", "july", "august", "september", "october",
    "november", "december",
}

# Phase r23-tighten: regex catches Title-Case fragments that aren't
# real entity names. These suffixes give a STRONG signal a phrase is
# actually a company — Inc, Ltd, Holdings, Networks, Digital, etc.
# When the regex finds candidates with no entity-suffix, we require
# them to ALSO match a known-operator pattern (multi-word AllCap or
# CamelCase brand) or we drop them.
ENTITY_SUFFIXES = {
    "Inc", "Inc.", "Ltd", "Ltd.", "LLC", "Corp", "Corp.", "Corporation",
    "Holdings", "Networks", "Communications", "Digital", "Hyperscale",
    "Industries", "Group", "Solutions", "Technologies", "Systems",
    "Realty", "REIT", "Capital", "Partners", "Trust", "Mining",
    "Compute", "Cloud", "Hosting", "Colocation", "DataBank",
    "Edge", "Centers", "Centres",
}

# Generic English words that should NEVER be considered entities
# regardless of capitalization (covers most sentence-fragment noise).
GENERIC_VERBS_NOUNS = {
    "bets", "limits", "front", "debate", "scrutiny", "policy",
    "pushback", "demands", "ground", "alternatives", "rules",
    "report", "operators", "expansion", "resistance", "battery",
    "storage", "boom", "outage", "transforms", "stretch",
    "tightens", "opens", "hit", "seeks", "seek", "meets",
    "resiliency", "permitting", "head-on", "beyond",
    "data centers", "data centres", "ai", "the", "and",
}


def _is_real_entity(phrase: str) -> bool:
    """Heuristic: does this look like an actual operator/facility name
    vs. a sentence fragment? Returns False for obvious noise."""
    p = phrase.strip()
    if not p or len(p) < 4: return False
    lower = p.lower()
    if lower in GENERIC_VERBS_NOUNS: return False
    # If any word is a noisy generic verb/noun, drop it
    tokens = p.split()
    if any(t.lower() in GENERIC_VERBS_NOUNS for t in tokens):
        return False
    # All-caps single word ≥ 3 chars (acronym pattern like TSMC, NTT)
    if len(tokens) == 1 and p.isupper() and len(p) >= 3:
        return True
    # Entity suffix? Strong signal
    if tokens[-1] in ENTITY_SUFFIXES:
        return True
    # 2+ word Title Case (each word starts with caps) → maybe a name
    if (len(tokens) >= 2
            and all(t[0].isupper() and (t[1:].islower() or t[1:].isdigit() or "-" in t)
                    for t in tokens)):
        # But reject if it ends in a verb-y form
        last = tokens[-1].lower()
        if last.endswith(("ing", "ed", "es", "ly", "tion")):
            return False
        return True
    return False


def _extract_names_regex(text: str) -> list[str]:
    if not text: return []
    text_lower = text.lower()
    candidates: set = set()
    # Find each trigger occurrence and grab capitalized phrases within
    # a 60-char window on either side.
    for trigger in TRIGGERS:
        idx = 0
        while True:
            pos = text_lower.find(trigger, idx)
            if pos < 0: break
            window = text[max(0, pos-80):pos + len(trigger) + 80]
            for m in _NAME_RE.finditer(window):
                phrase = m.group(1).strip()
                if (len(phrase) < 4 or len(phrase) > 80
                        or phrase.lower() in NOISE):
                    continue
                if phrase.isdigit():
                    continue
                # Phase r23-tighten: only keep phrases that pass the
                # real-entity heuristic. Drops sentence fragments
                # like "Oklahoma Law Opens New", "Battery Storage
                # Gains Ground", and single-word verbs.
                if not _is_real_entity(phrase):
                    continue
                candidates.add(phrase)
            idx = pos + len(trigger)
    return list(candidates)


# ── Optional LLM extractor ───────────────────────────────────────────
def _extract_names_llm(headline: str, body: str) -> list[str]:
    if not USE_LLM:
        return []
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        return []
    # Use the voice tier (Haiku) by default — cheap + fast for this
    try:
        from routes.brain_models import brain_model_for
        model = LLM_MODEL or brain_model_for("voice")
    except Exception:
        model = LLM_MODEL or "claude-haiku-3-5"
    sample = (headline + "\n\n" + (body or ""))[:1200]
    system = (
        "Extract proper-noun NAMES of data center facilities or "
        "operator companies mentioned in the news snippet. Return ONLY "
        "a JSON list of strings. Skip generic phrases (\"data center\", "
        "\"colocation\"), city names, and common-noun phrases. Each "
        "name should be 2-5 words, properly capitalized. If nothing "
        "qualifies, return []. JSON only, no prose."
    )
    import urllib.request, urllib.error
    payload = json.dumps({
        "model": model,
        "max_tokens": 300,
        "system": system,
        "messages": [{"role": "user", "content": sample}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload, method="POST",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body_resp = json.loads(resp.read().decode("utf-8"))
        text = ""
        for blk in body_resp.get("content", []) or []:
            if blk.get("type") == "text":
                text += blk.get("text", "")
        text = text.strip()
        # Parse JSON list, tolerating prose around it
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end < 0:
            return []
        candidates = json.loads(text[start:end+1])
        return [str(c).strip() for c in candidates
                if isinstance(c, str) and 3 < len(c) < 100]
    except Exception as e:
        logger.warning(f"[news-ner] LLM call failed: {str(e)[:160]}")
        return []


# ── Dedup against facilities ─────────────────────────────────────────
def _already_known(cur, name: str) -> bool:
    """Is this entity already in our facilities or discovered_facilities
    table by name?"""
    try:
        cur.execute(
            "SELECT 1 FROM facilities WHERE LOWER(name) = LOWER(%s) "
            "OR LOWER(COALESCE(provider, '')) = LOWER(%s) LIMIT 1",
            (name, name),
        )
        if cur.fetchone():
            return True
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
    try:
        cur.execute(
            "SELECT 1 FROM discovered_facilities "
            "WHERE LOWER(name) = LOWER(%s) "
            "OR LOWER(COALESCE(provider, '')) = LOWER(%s) LIMIT 1",
            (name, name),
        )
        if cur.fetchone():
            return True
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
    return False


# ── Pipeline ─────────────────────────────────────────────────────────
def _scan(days: int, dry_run: bool) -> dict:
    out = {
        "days": days, "dry_run": dry_run,
        "articles_scanned": 0,
        "candidates_found": 0,
        "candidates_new":   0,
        "candidates_known": 0,
        "examples":         [],
        "started_at":       datetime.datetime.utcnow().isoformat() + "Z",
    }
    _ensure_table()
    c = _get_db()
    if c is None:
        out["error"] = "no_db"
        return out

    # Read recent news. Schema may vary across deploys — try a few
    # known shapes.
    articles: list = []
    try:
        with c.cursor() as cur:
            for sql in (
                "SELECT id, title, body, url FROM news_items "
                "WHERE published_date >= NOW() - INTERVAL %s "
                "ORDER BY published_date DESC LIMIT %s",
                "SELECT id, title, summary AS body, url FROM news_items "
                "WHERE published_at >= NOW() - INTERVAL %s "
                "ORDER BY published_at DESC LIMIT %s",
                "SELECT id, title, NULL AS body, url FROM news "
                "WHERE created_at >= NOW() - INTERVAL %s "
                "ORDER BY created_at DESC LIMIT %s",
            ):
                try:
                    cur.execute(sql, (f"{days} days", MAX_PER_RUN))
                    rows = cur.fetchall()
                    if rows:
                        for r in rows:
                            articles.append({
                                "id":    r[0], "title": r[1],
                                "body":  r[2], "url":   r[3],
                            })
                        break
                except Exception:
                    try: c.rollback()
                    except Exception: pass
                    continue
    finally:
        # Don't close yet; we'll reuse for dedup checks below
        pass

    out["articles_scanned"] = len(articles)

    # Process each article
    candidate_counts: dict = {}
    sample_per_candidate: dict = {}

    for art in articles:
        text = (art.get("title") or "") + "\n" + (art.get("body") or "")
        names = _extract_names_regex(text)
        # Optional LLM enrichment
        if USE_LLM and len(names) < 3:  # only run LLM when regex is thin
            llm_names = _extract_names_llm(
                art.get("title") or "", art.get("body") or ""
            )
            names.extend(llm_names)
        for n in names:
            n = n.strip()
            candidate_counts[n] = candidate_counts.get(n, 0) + 1
            if n not in sample_per_candidate:
                sample_per_candidate[n] = {
                    "news_id":  str(art.get("id") or ""),
                    "headline": (art.get("title") or "")[:200],
                    "url":      art.get("url") or "",
                }

    out["candidates_found"] = len(candidate_counts)

    # Persist + classify
    try:
        with c.cursor() as cur:
            for name, count in candidate_counts.items():
                if dry_run:
                    known = _already_known(cur, name)
                    if known:
                        out["candidates_known"] += 1
                    else:
                        out["candidates_new"] += 1
                    if len(out["examples"]) < 20:
                        out["examples"].append({
                            "name": name, "count": count,
                            "known": known,
                            "sample": sample_per_candidate.get(name, {}),
                        })
                    continue
                known = _already_known(cur, name)
                sample = sample_per_candidate.get(name, {})
                try:
                    cur.execute("""
                        INSERT INTO news_discovered_entities
                          (entity_name, mention_count, sample_news_id,
                           sample_headline, sample_url, in_facilities,
                           status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (LOWER(entity_name)) DO UPDATE SET
                            mention_count = news_discovered_entities.mention_count + EXCLUDED.mention_count,
                            last_seen_at  = NOW(),
                            in_facilities = EXCLUDED.in_facilities
                    """, (
                        name, count,
                        sample.get("news_id"),
                        sample.get("headline"),
                        sample.get("url"),
                        known,
                        ("known" if known else "unknown"),
                    ))
                except Exception as e:
                    try: c.rollback()
                    except Exception: pass
                    logger.info(f"[news-ner] insert failed for {name[:30]}: {str(e)[:120]}")
                    continue
                if known:
                    out["candidates_known"] += 1
                else:
                    out["candidates_new"] += 1
                    if len(out["examples"]) < 20:
                        out["examples"].append({
                            "name": name, "count": count,
                            "headline": sample.get("headline"),
                            "url": sample.get("url"),
                        })
            try: c.commit()
            except Exception: pass
    finally:
        try: c.close()
        except Exception: pass

    out["finished_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    out["ok"] = True
    return out


# ── Endpoints ────────────────────────────────────────────────────────
@news_ner_bp.route("/api/v1/admin/news-ner/run", methods=["POST"])
def ner_run():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    days = int(request.args.get("days") or "1")
    dry_run = (request.args.get("dry_run") or "").lower() in ("1", "true", "yes")
    return jsonify(_scan(days, dry_run))


@news_ner_bp.route("/api/v1/admin/news-ner/candidates", methods=["GET"])
def ner_candidates():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    only_unknown = (request.args.get("unknown_only") or "1").lower() in ("1","true","yes")
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            # r23-tighten: always exclude 'rejected' from the human-
            # facing candidates list. Operators have already said no
            # to these.
            where = ("WHERE in_facilities = FALSE "
                     "AND COALESCE(status, 'unknown') != 'rejected'"
                     if only_unknown
                     else "WHERE COALESCE(status, 'unknown') != 'rejected'")
            cur.execute(f"""
                SELECT entity_name, mention_count, first_seen_at,
                       last_seen_at, sample_headline, sample_url,
                       in_facilities, status
                  FROM news_discovered_entities
                  {where}
                 ORDER BY mention_count DESC, last_seen_at DESC
                 LIMIT 100
            """)
            rows = []
            for r in cur.fetchall():
                rows.append({
                    "name": r[0], "mentions": r[1],
                    "first_seen": str(r[2]) if r[2] else None,
                    "last_seen": str(r[3]) if r[3] else None,
                    "headline": r[4],
                    "url": r[5],
                    "in_facilities": r[6],
                    "status": r[7],
                })
        return jsonify(ok=True, count=len(rows), candidates=rows)
    finally:
        try: c.close()
        except Exception: pass


@news_ner_bp.route("/api/v1/admin/news-ner/reject", methods=["POST"])
def ner_reject():
    """Mark a candidate as 'noise' so it doesn't keep showing up.
    Body: {"name": "Sentence Fragment"} or {"id": 123}.
    Marks status='rejected' — future scans of the same name skip
    adding to the candidates list."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    p = request.get_json(silent=True) or {}
    name = (p.get("name") or "").strip()
    cid = p.get("id")
    if not name and not cid:
        return jsonify(ok=False, error="name_or_id_required"), 400
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            if name:
                cur.execute(
                    "UPDATE news_discovered_entities SET status = 'rejected' "
                    "WHERE LOWER(entity_name) = LOWER(%s) RETURNING id",
                    (name,),
                )
            else:
                cur.execute(
                    "UPDATE news_discovered_entities SET status = 'rejected' "
                    "WHERE id = %s RETURNING id",
                    (cid,),
                )
            r = cur.fetchone()
        try: c.commit()
        except Exception: pass
        return jsonify(ok=True, rejected=bool(r),
                       id=int(r[0]) if r else None)
    finally:
        try: c.close()
        except Exception: pass


@news_ner_bp.route("/api/v1/admin/news-ner/purge-noise", methods=["POST"])
def ner_purge_noise():
    """Retroactively run the _is_real_entity filter over existing
    rows and mark fragments as 'rejected'. Cleans up the noise from
    pre-r23-tighten scans."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    rejected = 0
    kept = 0
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, entity_name FROM news_discovered_entities
                 WHERE status NOT IN ('rejected', 'seeded')
            """)
            rows = cur.fetchall()
            for r in rows:
                name = r[1] or ""
                if not _is_real_entity(name):
                    cur.execute(
                        "UPDATE news_discovered_entities "
                        "SET status = 'rejected' WHERE id = %s",
                        (r[0],),
                    )
                    rejected += 1
                else:
                    kept += 1
        try: c.commit()
        except Exception: pass
        return jsonify(ok=True, scanned=len(rows),
                       rejected=rejected, kept=kept)
    finally:
        try: c.close()
        except Exception: pass


@news_ner_bp.route("/api/v1/admin/news-ner/status", methods=["GET"])
def ner_status():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE in_facilities = FALSE) AS unknown_count,
                    COUNT(*) FILTER (WHERE in_facilities = TRUE)  AS known_count,
                    MAX(last_seen_at) AS last_seen
                  FROM news_discovered_entities
            """)
            r = cur.fetchone() or (0, 0, 0, None)
        return jsonify(
            ok=True,
            llm_enabled=USE_LLM,
            total=int(r[0] or 0),
            unknown=int(r[1] or 0),
            known=int(r[2] or 0),
            last_seen=str(r[3]) if r[3] else None,
        )
    finally:
        try: c.close()
        except Exception: pass


def _smoke():
    logger.info(f"[news-ner] ready · llm_enabled={USE_LLM} · "
                 f"max_per_run={MAX_PER_RUN} · "
                 f"endpoints: /api/v1/admin/news-ner/{{run,candidates,status}}")

_smoke()
