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
from utils.anthropic_helper import cached_system
import os
from internal_auth import accepted_internal_keys
import re
import time
import json
import logging
import datetime
import hashlib
from flask import Blueprint, jsonify, request
from utils.anthropic_helper import anthropic_messages_url
from utc_clock import utc_iso_z, utc_now

logger = logging.getLogger(__name__)
news_ner_bp = Blueprint("news_ner", __name__)


_INTERNAL_KEYS = accepted_internal_keys()
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

# r-entityres (2026-07-04): minimum LOCAL cosine (0-1, computed over Cohere
# embeddings fetched in ONE _embed call — NEVER the cross-encoder rerank
# scores retrieve_context returns, whose absolute values sit at 0.05-0.3
# even for good hits) for a promote-time candidate to resolve to an
# EXISTING discovered_facilities row instead of creating a duplicate.
try:
    NEWS_ENTITY_DUP_COSINE = float(
        os.environ.get("NEWS_ENTITY_DUP_COSINE", "0.90"))
except Exception:
    NEWS_ENTITY_DUP_COSINE = 0.90


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
    # ★ SINGLE-TOKEN PROPER NOUN (r-ner-singletoken 2026-07-27). Without this
    # branch every one-word mixed-case name fell through to the "2+ word Title
    # Case" test below, failed `len(tokens) >= 2`, and returned False — so
    # _is_real_entity rejected 287 of 470 live entities INCLUDING Google,
    # Apple, Amazon Web Services, Huawei, Vodafone, Comcast, CoreWeave,
    # ByteDance, AirTrunk, Orange and Telus. 48 of them resolve to a real
    # facility or provider. That made /api/v1/admin/news-ner/purge-noise — which
    # applies this predicate DESTRUCTIVELY (status='rejected') over every row —
    # a loaded gun; it had never been run, which is the only reason the entity
    # table survived intact.
    #
    # Deliberately permissive: a false ACCEPT costs one advisory candidate row
    # that a human triages, a false REJECT silently deletes Google from the
    # graph. These rows are candidates, never auto-promoted, so the asymmetry
    # is the whole argument. It does admit common nouns ("Debate", "Front");
    # separating those from "Orange" needs a dictionary or an LLM pass, and is
    # NOT solved here.
    # Intercaps counts: "nLighten" and "euNetworks" are real operators whose
    # first letter is lowercase, so testing p[0].isupper() alone loses them.
    if (len(tokens) == 1 and len(p) >= 4
            and any(ch.isupper() for ch in p) and not p.isupper()):
        return True
    # Entity suffix? Strong signal
    if tokens[-1] in ENTITY_SUFFIXES:
        return True
    # 2+ word name-shaped tokens → maybe a name.
    # ★ The original test demanded t[0].isupper() AND t[1:].islower(), which
    # rejected every acronym-bearing and lowercase-initial operator we carry:
    # "Colt DCS", "Pure DC", "SK Telecom", "LG Uplus", "nLighten",
    # "euNetworks". A token is name-shaped if it contains ANY capital (covers
    # Title Case, ALLCAPS acronyms and intercaps) or is a bare number.
    if (len(tokens) >= 2
            and all(any(ch.isupper() for ch in t) or t.isdigit() for t in tokens)):
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
        # r-fix (2026-06-06): claude-haiku-3-5 is RETIRED → 404 on every call
        # (broke entity extraction + ~contributed to the AI Gateway error rate).
        model = LLM_MODEL or "claude-haiku-4-5"
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
        "system": cached_system(system),
        "messages": [{"role": "user", "content": sample}],
    }).encode()
    req = urllib.request.Request(
        anthropic_messages_url(),
        data=payload, method="POST",
        headers={
            "x-api-key": key,
            "User-Agent": "dchub-brain/1.0",
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
# Words that are legitimate facility-name PREFIXES but meaningless as an
# entity — a prefix match on these claims half the fleet. Kept tight: every
# addition costs real resolutions, so only genuinely ambiguous infra nouns.
_GENERIC_PREFIX_STOP = {
    "power", "data", "center", "centre", "energy", "grid", "cloud", "network",
    "digital", "global", "tech", "technology", "systems", "solutions", "group",
    "holdings", "international", "national", "american", "new", "future", "key",
}


# ── the resolver's blind spot, measured once, imported twice ──────────────
# Entities we did NOT resolve that DO prefix-match a facility name at a token
# boundary. Two master shells need this number — #36 lane 5a (es_blindspot,
# critical) and brain-autonomy's news_entity_reresolve trigger — and both had
# RESTATED the query. It lives here now, beside the stoplist they already
# import, for the reason that comment already gives: a restated query drifts.
#
# ★ 2026-08-23 — the stated form could never return. Its predicate was
#       lower(f.name) LIKE lower(trim(e.entity_name)) || ' ' || chr(37)
# whose pattern is computed from the OUTER row, so the planner cannot turn it
# into an index qual: a Nested Loop Semi Join with a bare Join Filter over
# 1,903 x 22,162 rows (cost 380,622) that hit the 15s statement_timeout EVERY
# time. Both shells swallowed the error and reported the check UNMEASURED, so
# the actuator could never fire and /admin/brain-autonomy took ~17s at the
# origin — a 502 at the CF edge. Measured, not reasoned: 15,060ms -> 55ms.
#
# The fast form brackets the SAME strings as a byte RANGE, which the existing
# idx_facilities_name_lower btree serves as a real Index Cond:
#       name >= entity || ' '   AND   name < entity || '!'
# exact because ' ' is chr(32) and '!' is chr(33), so the half-open interval
# holds every string beginning with "<entity> " and nothing else. The original
# LIKE is KEPT as a recheck — it is the semantic definition, and on the handful
# of rows the index returns it is free (55ms with it, 63ms without).
#
# ★ The range is only exact under BYTE ordering. Under a linguistic collation
# it could UNDER-include, and es_blindspot is critical=True — a silently low 0
# would read as "no blind spot" and go GREEN. So the collation is witnessed and
# an unwitnessed database yields None (UNMEASURED), never a number. Fails
# closed, the same shape as ai_surface_canon.canon_is_live().
_BYTE_ORDERED_COLLATIONS = ("C", "C.UTF-8", "C.utf8", "POSIX", "ucs_basic")
_COLLATION_BYTE_ORDERED = None   # None = not witnessed yet; cached on success


def _collation_is_byte_ordered(cur):
    """True/False once witnessed, None if the database would not say."""
    global _COLLATION_BYTE_ORDERED
    if _COLLATION_BYTE_ORDERED is not None:
        return _COLLATION_BYTE_ORDERED
    try:
        cur.execute("SELECT datcollate FROM pg_database"
                    " WHERE datname = current_database()")
        r = cur.fetchone()
    except Exception as e:
        logger.debug("[entity-blindspot] collation witness failed: %s",
                     str(e)[:140])
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return None
    if not r or r[0] is None:
        return None
    _COLLATION_BYTE_ORDERED = str(r[0]) in _BYTE_ORDERED_COLLATIONS
    return _COLLATION_BYTE_ORDERED


def entity_blindspot_count(cur_or_conn):
    """Unresolved entities that DO prefix-match a facility name at a token
    boundary. Returns an int, or None when it could not be measured — never a
    number the caller should not trust. Both callers already treat None as
    UNMEASURED and refuse to act on it.

    ★ Takes a cursor OR a connection, because the two shells that call it
    disagree: #36's own _row() takes a CONNECTION and opens the cursor
    itself, brain-autonomy's takes a CURSOR. Handing this function the wrong
    one raised inside the fail-soft except and returned a silent None —
    forever, and green-looking, which is the exact failure this whole
    measurement exists to stop. Discriminated on .cursor(), which psycopg2
    connections have and cursors do not."""
    if hasattr(cur_or_conn, "cursor"):
        with cur_or_conn.cursor() as _cur:
            return _entity_blindspot_count(_cur)
    return _entity_blindspot_count(cur_or_conn)


def _entity_blindspot_count(cur):
    if _collation_is_byte_ordered(cur) is not True:
        return None
    stop_sql = ", ".join("'" + w.replace("'", "''") + "'"
                         for w in sorted(_GENERIC_PREFIX_STOP)) or "''"
    try:
        cur.execute("SELECT count(*) FROM news_discovered_entities e"
                    " WHERE e.in_facilities = FALSE"
                    "   AND length(trim(e.entity_name)) >= 4"
                    "   AND lower(trim(e.entity_name)) NOT IN (" + stop_sql + ")"
                    "   AND EXISTS (SELECT 1 FROM facilities f"
                    "               WHERE lower(f.name) >="
                    "                     lower(trim(e.entity_name)) || ' '"
                    "                 AND lower(f.name) <"
                    "                     lower(trim(e.entity_name)) || '!'"
                    "                 AND lower(f.name) LIKE"
                    "                     lower(trim(e.entity_name)) || ' '"
                    "                     || chr(37))")
        r = cur.fetchone()
    except Exception as e:
        logger.debug("[entity-blindspot] count failed: %s", str(e)[:140])
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return None
    return None if not r or r[0] is None else int(r[0])


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

    # ★ TOKEN-BOUNDARY PREFIX MATCH (r-ner-prefix 2026-07-27) — the blind spot
    # exact matching could never see. Our facility NAMES carry the site, not
    # just the operator: "Azure Korea Central (Seoul)", "Crown Castle
    # Baltimore", "Alibaba Cloud Zhangbei DC". So an entity that IS in the
    # graph 55 times ("Azure") matched nothing at all. This one predicate
    # resolves 16 further entities — Azure(55 facilities), Crown Castle(22),
    # Alibaba(13), Microsoft Azure(12), Facebook(11), Stargate(11), XLSMART,
    # Matrix, Dell, Synergy, Cisco, Samsung, Dish, Grok, Lightstorm, Flex —
    # every one verified correct by hand.
    #
    # GUARDS, because a prefix match is exactly how false positives get in:
    #   · trailing space in the pattern = a real token boundary, so "Dell"
    #     matches "Dell 350 Holger Way" but never "Dellwood ..."
    #   · len >= 4 and a generic-infra stoplist, or "Power" would claim
    #     "power generator for government-linked data center ..."
    #   · DECOY CONTROL: the same query run over entity_name || 'x' returns 0
    #     matches, so these are not coincidental prefixes (the lesson from the
    #     carrier join — a count that a decoy also scores is not evidence).
    # The % lives in the PARAMETER, never in the SQL string, so the empty-tuple
    # %-substitution trap does not apply.
    n = (name or "").strip()
    if len(n) >= 4 and n.lower() not in _GENERIC_PREFIX_STOP:
        try:
            cur.execute(
                "SELECT 1 FROM facilities WHERE LOWER(name) LIKE %s LIMIT 1",
                (n.lower() + " %",),
            )
            if cur.fetchone():
                return True
        except Exception:
            try: cur.connection.rollback()
            except Exception: pass
    return False


def _reresolve_unmatched(c, cap: int = 200) -> dict:
    """Re-run _already_known over entities still marked in_facilities=FALSE.

    in_facilities is stamped at INSERT and refreshed only when the entity is
    re-mentioned (ON CONFLICT ... in_facilities = EXCLUDED.in_facilities), so
    an entity whose matching facility arrives LATER stays FALSE until the news
    happens to mention it again. That structural lag is Shell #36 lane 5a's
    "resolver blind spot" — it regrew 0 → 9 between 2026-07-27 and 08-16.
    This pass closes the lag on the scan cadence with the SAME matcher the
    scan uses, so the lane and the code cannot drift.

    Only ever flips FALSE→TRUE — ground truth from the facilities tables may
    never be overruled here. A row that now resolves while status='rejected'
    is moved to 'known': lane 5b's invariant is that nothing may be both
    resolved and rejected. 'promoted' is left as-is (the promote paths stamp
    in_facilities=TRUE themselves; an older path missed it — id 350).
    """
    out = {"checked": 0, "resolved": 0}
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT id, entity_name FROM news_discovered_entities"
                " WHERE in_facilities = FALSE"
                # id DESC is a TIEBREAKER, not decoration: a scan pass stamps
                # many rows with the same last_seen_at, and without it this
                # 300-row window and the pre-image squasher_action_classes
                # takes of it (for the rollback) can be different row sets.
                " ORDER BY last_seen_at DESC NULLS LAST, id DESC LIMIT %s", (cap,))
            rows = cur.fetchall()
            for eid, name in rows:
                out["checked"] += 1
                if _already_known(cur, name or ""):
                    cur.execute(
                        "UPDATE news_discovered_entities"
                        " SET in_facilities = TRUE,"
                        "     status = CASE WHEN COALESCE(status,'')"
                        "                        IN ('', 'unknown', 'rejected')"
                        "                   THEN 'known' ELSE status END"
                        " WHERE id = %s", (eid,))
                    out["resolved"] += 1
        try: c.commit()
        except Exception: pass
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        out["error"] = str(e)[:200]
    return out


# ── Semantic entity resolution (r-entityres 2026-07-04) ──────────────
# ROOT CAUSE of facility-count inflation: _already_known() is EXACT
# LOWER(name)= equality, so "QTS Atlanta DC-1" vs "QTS Atlanta Data
# Center 1" both pass "unknown" and BOTH become discovered_facilities
# rows. Fix: when exact match fails, do a semantic check against the
# discovered_facilities RAG corpus BEFORE creating a new row. Resolve
# to the existing row only when (a) local embedding cosine >= threshold
# AND (b) the location POSITIVELY agrees (same city OR same state, both
# sides present). High name similarity with a differing/unknown location
# still CREATES — a false merge is worse than a duplicate. Every failure
# path degrades to the exact current behavior (create).

def _cos_sim(a, b):
    """Plain local cosine over two equal-length vectors. None on failure."""
    try:
        num = sum(float(x) * float(y) for x, y in zip(a, b))
        da = sum(float(x) * float(x) for x in a) ** 0.5
        db = sum(float(y) * float(y) for y in b) ** 0.5
        if da <= 0 or db <= 0:
            return None
        return num / (da * db)
    except Exception:
        return None


def _loc_agrees(new_city, new_state, ex_city, ex_state) -> bool:
    """POSITIVE location agreement only: same city (both present) or same
    state (both present). Missing location on either side → False, so the
    caller creates rather than risking a false merge."""
    nc = (new_city or "").strip().lower()
    ns = (new_state or "").strip().lower()
    ec = (ex_city or "").strip().lower()
    es = (ex_state or "").strip().lower()
    if nc and ec and nc == ec:
        return True
    if ns and es and ns == es:
        return True
    return False


def _entity_query_text(name, city, state) -> str:
    bits = [str(b).strip() for b in (city, state) if b and str(b).strip()]
    return (name or "") + ((" — " + ", ".join(bits)) if bits else "")


# ── Designator guard (r-entityres fix 2026-07-04) ────────────────────
# Embeddings CANNOT reliably distinguish digit/designator-level name
# differences: "QTS Atlanta DC-1" vs "QTS Atlanta DC-2" (same city)
# clears cosine 0.90+ yet is two different buildings. So cosine alone
# must never decide. We extract normalized designator tokens (unit
# numbers, DC-N, IAD8-style letter codes, roman numerals I–X, Phase N,
# Building X) from both names and allow the merge ONLY when the token
# sets are identical or BOTH empty:
#   · both have designators and they differ → NEVER merge ("DC-1" vs
#     "DC-2"), regardless of cosine.
#   · only ONE side has a designator → refuse too ("QTS Atlanta" vs
#     "QTS Atlanta DC-2" may be campus vs building — conservative).
# Normalization makes true synonyms compare equal: "DC-1" == "DC1" ==
# "Data Center 1" (DC prefix IS "data center"), "Phase II" == "Phase 2",
# "01" == "1". Non-DC letter codes stay opaque (IAD8 != IAD9 != 8).
# False-positive extraction only skews CONSERVATIVE (a refused merge =
# one duplicate row, recoverable; a false merge silently loses a
# facility).

_ROMAN_UNITS = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5,
                "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}
_ROMAN_ALT = "VIII|VII|VI|IX|IV|V|X|III|II|I"

_RE_DESIG_PHASE = re.compile(
    r"\bPHASE[\s\-]*([0-9]+|" + _ROMAN_ALT + r")\b")
_RE_DESIG_BLDG = re.compile(r"\bBUILDING[\s\-]*([A-Z0-9]{1,4})\b")
# Letter code: 1-4 uppercase letters + digits, optional hyphen
# (DC-1, DC11, IAD8, ATL1, V1). Single-letter included so "Vantage V1"
# carries a designator.
_RE_DESIG_CODE = re.compile(r"\b([A-Z]{1,4})-?([0-9]+)\b")
_RE_DESIG_ROMAN = re.compile(r"\b(" + _ROMAN_ALT + r")\b")
_RE_DESIG_DIGITS = re.compile(r"\b([0-9]+)\b")


def _norm_unit(tok: str) -> str:
    """Normalize one unit token: roman → arabic, strip leading zeros."""
    tok = (tok or "").strip().upper()
    if tok in _ROMAN_UNITS:
        return str(_ROMAN_UNITS[tok])
    if tok.isdigit():
        return str(int(tok))  # "01" == "1"
    return tok


def _designator_tokens(name: str) -> frozenset:
    """Extract the set of normalized designator tokens from a facility
    name. Empty set = no designators. Each regex class consumes its
    matched span before the next runs, so "DC-1" never double-counts
    its digit as a standalone "1"."""
    s = " " + (name or "").upper() + " "
    out = set()

    def _eat(rx, fmt):
        nonlocal s

        def _repl(m):
            tok = fmt(m)
            if tok:
                out.add(tok)
            return " "

        s = rx.sub(_repl, s)

    _eat(_RE_DESIG_PHASE, lambda m: "PHASE" + _norm_unit(m.group(1)))
    _eat(_RE_DESIG_BLDG, lambda m: "BLDG" + _norm_unit(m.group(1)))

    def _code(m):
        letters, digits = m.group(1), m.group(2)
        if letters == "DC":
            # "DC" literally means "data center": DC-1 == DC1 ==
            # "Data Center 1" (whose "1" lands via the digit class).
            return _norm_unit(digits)
        return letters + _norm_unit(digits)

    _eat(_RE_DESIG_CODE, _code)
    _eat(_RE_DESIG_ROMAN, lambda m: _norm_unit(m.group(1)))
    _eat(_RE_DESIG_DIGITS, lambda m: _norm_unit(m.group(1)))
    return frozenset(out)


def _designators_compatible(name_a: str, name_b: str) -> bool:
    """True only when both names carry the SAME designator set (possibly
    both empty). Any asymmetry or difference → merge is forbidden."""
    return _designator_tokens(name_a) == _designator_tokens(name_b)


def _resolve_existing_semantic(facility: dict):
    """Semantic duplicate check for a facility about to be CREATED.

    Returns {"id", "name", "cosine"} of the existing discovered_facilities
    row to resolve to, or None (= create, exactly as before). Recipe per
    the RAG similarity rule: retrieve_context() only NOMINATES candidates
    (its scores are cross-encoder rerank values and are NEVER thresholded);
    the actual similarity is local cosine over embeddings from ONE _embed
    call (input_type=search_document for both sides — symmetric compare).
    A DESIGNATOR GUARD runs before cosine can win: candidates whose
    designator token set differs from the new name's (see
    _designator_tokens) are excluded outright — "QTS Atlanta DC-1" never
    merges into "QTS Atlanta DC-2" no matter the cosine.
    Fail-soft: ANY import/RAG/embed/DB failure returns None."""
    try:
        from routes.brain_rag import retrieve_context, _embed
    except Exception:
        return None

    name = (facility.get("name") or "").strip()
    if not name:
        return None
    query = _entity_query_text(name, facility.get("city"),
                               facility.get("state"))

    try:
        hits = retrieve_context(query, k=5, corpus="discovered_facilities") or []
    except Exception:
        return None
    ids = []
    for h in hits:
        try:
            sid = str(h.get("source_id") or "").strip()
        except Exception:
            continue
        if sid.isdigit():
            ids.append(int(sid))
    if not ids:
        return None

    # Fetch candidate name/city/state by id — we compare canonical
    # "name — city, state" texts, not whatever prose shape the corpus
    # embedded (which also carries provider/market noise).
    rows = []
    c = _get_db()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT id, name, city, state FROM discovered_facilities "
                "WHERE id = ANY(%s) AND COALESCE(is_duplicate, 0) = 0",
                (ids,))
            for r in cur.fetchall():
                if r[1]:
                    rows.append({"id": r[0], "name": r[1],
                                 "city": r[2], "state": r[3]})
    except Exception:
        try: c.rollback()
        except Exception: pass
        return None
    finally:
        try: c.close()
        except Exception: pass
    if not rows:
        return None

    cand_texts = [_entity_query_text(r["name"], r["city"], r["state"])
                  for r in rows]
    try:
        vecs = _embed([query] + cand_texts, input_type="search_document")
    except Exception:
        return None
    if not vecs or len(vecs) != len(cand_texts) + 1:
        return None

    qv = vecs[0]
    new_toks = _designator_tokens(name)
    best = None
    for r, v in zip(rows, vecs[1:]):
        cos = _cos_sim(qv, v)
        if cos is None:
            continue
        # DESIGNATOR GUARD (r-entityres fix): embeddings cannot tell
        # "DC-1" from "DC-2" — sibling buildings in the same campus
        # exceed any usable cosine threshold. If designator token sets
        # differ (including one-sided), this candidate can NEVER be a
        # merge target, regardless of cosine. Log the near-miss when it
        # would otherwise have cleared the threshold.
        ex_toks = _designator_tokens(r["name"])
        if new_toks != ex_toks:
            if cos >= NEWS_ENTITY_DUP_COSINE:
                logger.info(
                    f"[news-ner entityres] designator guard REFUSED "
                    f"merge: new='{name}' {sorted(new_toks) or '[]'} vs "
                    f"existing #{r['id']} '{r['name']}' "
                    f"{sorted(ex_toks) or '[]'} cosine={round(cos, 4)}")
            continue
        if best is None or cos > best[1]:
            best = (r, cos)
    if best is None:
        return None

    r, cos = best
    if cos < NEWS_ENTITY_DUP_COSINE:
        return None
    if not _loc_agrees(facility.get("city"), facility.get("state"),
                       r["city"], r["state"]):
        # Similar name, but no positive location agreement → could be a
        # genuinely different site with a similar name. CREATE anyway.
        logger.info(
            f"[news-ner entityres] near-duplicate name NOT merged "
            f"(location differs/unknown): new='{name}' vs "
            f"existing #{r['id']} '{r['name']}' cosine={round(cos, 4)}")
        return None
    return {"id": r["id"], "name": r["name"], "cosine": round(cos, 4)}


# ── Pipeline ─────────────────────────────────────────────────────────
def _scan(days: int, dry_run: bool) -> dict:
    out = {
        "days": days, "dry_run": dry_run,
        "articles_scanned": 0,
        "candidates_found": 0,
        "candidates_new":   0,
        "candidates_known": 0,
        "examples":         [],
        "started_at":       utc_iso_z(),
    }
    _ensure_table()
    c = _get_db()
    if c is None:
        out["error"] = "no_db"
        return out

    # Read recent news. Schema may vary across deploys — try a few known
    # shapes, LIVE STORE FIRST. r-ner-live-store (2026-08-17): the scan read
    # only `news` — a table whose sole writer is the manual
    # /api/admin/trigger-sync?type=news endpoint nothing schedules — while the
    # automated pipeline (news_engine, NEWS_VIA_CRON) writes `news_articles`.
    # Both `news_items` shapes have NEVER matched (relation does not exist in
    # prod), so the feed starved whenever nobody hand-fired the aggregator and
    # the deadman board flapped error/no_new_data for a week. `news_articles.
    # published_at` is TEXT in prod: compare against an ISO date-string cutoff
    # (lexicographic >= 'YYYY-MM-DD' orders chronologically for ISO-prefixed
    # values; NULL published_at rows drop out of the window, by design).
    articles: list = []
    shape_errors: list = []
    _iso_cutoff = (datetime.datetime.now(datetime.timezone.utc)
                   - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        with c.cursor() as cur:
            for label, sql, params in (
                ("news_articles",
                 "SELECT id, title, summary AS body, url FROM news_articles "
                 "WHERE published_at >= %s "
                 "ORDER BY published_at DESC LIMIT %s",
                 (_iso_cutoff, MAX_PER_RUN)),
                ("news_items/published_date",
                 "SELECT id, title, body, url FROM news_items "
                 "WHERE published_date >= NOW() - INTERVAL %s "
                 "ORDER BY published_date DESC LIMIT %s",
                 (f"{days} days", MAX_PER_RUN)),
                ("news_items/published_at",
                 "SELECT id, title, summary AS body, url FROM news_items "
                 "WHERE published_at >= NOW() - INTERVAL %s "
                 "ORDER BY published_at DESC LIMIT %s",
                 (f"{days} days", MAX_PER_RUN)),
                ("news",
                 "SELECT id, title, NULL AS body, url FROM news "
                 "WHERE created_at >= NOW() - INTERVAL %s "
                 "ORDER BY created_at DESC LIMIT %s",
                 (f"{days} days", MAX_PER_RUN)),
            ):
                try:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
                    if rows:
                        for r in rows:
                            articles.append({
                                "id":    r[0], "title": r[1],
                                "body":  r[2], "url":   r[3],
                            })
                        out["article_source"] = label
                        break
                except Exception as e:
                    # A silently-skipped shape is how two dead SQLs survived
                    # indefinitely — keep the swallow (fallback chain must not
                    # abort the scan) but RECORD what was swallowed.
                    shape_errors.append("%s: %s: %s" % (
                        label, type(e).__name__, str(e)[:80]))
                    try: c.rollback()
                    except Exception: pass
                    continue
    finally:
        # LEAK FIX: release the pooled conn before the (potentially long) LLM
        # enrichment loop below so it isn't held idle across external calls and
        # tripped by FORCED RECLAIM. We re-acquire a fresh conn for persist.
        try: c.close()
        except Exception: pass
        c = None

    out["articles_scanned"] = len(articles)
    if shape_errors:
        # Every shape that errored, even on a successful scan — a dead shape
        # must be visible the run it dies, not when the last fallback starves.
        out["shape_errors"] = shape_errors

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

    # Persist + classify. Re-acquire a fresh pooled conn — the read conn was
    # released before the LLM loop so it wasn't held idle across external calls.
    c = _get_db()
    if c is None:
        out["error"] = "no_db_persist"
        out["finished_at"] = utc_iso_z()
        return out
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
        if not dry_run:
            # Close the resolver's structural lag every scan (Shell #36 lane
            # 5a): entities inserted before their facility existed never
            # re-check unless re-mentioned. Bounded, FALSE→TRUE only.
            out["reresolve"] = _reresolve_unmatched(c, cap=200)
    finally:
        try: c.close()
        except Exception: pass

    out["finished_at"] = utc_iso_z()
    out["ok"] = True
    return out


# ── Promotion: NER candidate → discovered_facilities ─────────────────
# Item 2c (2026-06-13). The NER scan above only fills news_discovered_entities
# (a name-level candidate queue). This step closes the loop: high-confidence
# candidates whose sample headline reads like a real facility announcement
# are turned into proper discovered_facilities rows via the well-tested
# news_facility_extractor.insert_discovered_facility(). Conservative gates
# (min mention_count, real-entity filter, announcement-shaped headline,
# not already known/rejected) keep regex noise out of the facility table.

PROMOTE_MIN_MENTIONS = int(os.environ.get("NEWS_NER_PROMOTE_MIN_MENTIONS", "2"))
PROMOTE_MAX_PER_RUN = int(os.environ.get("NEWS_NER_PROMOTE_MAX", "50"))


def _promote_candidates(min_mentions: int = None,
                        limit: int = None,
                        dry_run: bool = False) -> dict:
    """Promote high-confidence NER candidates into discovered_facilities.

    Returns a summary dict. Idempotent: promoted rows are flipped to
    in_facilities=TRUE / status='promoted' so a re-run won't double-insert,
    and insert_discovered_facility() itself dedups on source_url."""
    if min_mentions is None:
        min_mentions = PROMOTE_MIN_MENTIONS
    if limit is None:
        limit = PROMOTE_MAX_PER_RUN

    out = {
        "ok": True,
        "dry_run": dry_run,
        "min_mentions": min_mentions,
        "considered": 0,
        "promoted": 0,
        "resolved_existing": 0,
        "skipped_not_announcement": 0,
        "skipped_not_a_facility": 0,
        "skipped_no_url": 0,
        "errors": 0,
        "examples": [],
        "started_at": utc_iso_z(),
    }

    try:
        from news_facility_extractor import (
            extract_facility_from_article,
            insert_discovered_facility,
            is_facility_announcement,
        )
    except Exception as e:
        out["ok"] = False
        out["error"] = f"news_facility_extractor import failed: {e}"
        return out

    c = _get_db()
    if c is None:
        out["ok"] = False
        out["error"] = "no_db"
        return out

    candidates = []
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, entity_name, mention_count, sample_headline,
                       sample_url
                  FROM news_discovered_entities
                 WHERE in_facilities = FALSE
                   AND COALESCE(status, 'unknown') NOT IN ('rejected', 'promoted')
                   AND mention_count >= %s
                 ORDER BY mention_count DESC, last_seen_at DESC
                 LIMIT %s
            """, (min_mentions, limit))
            for r in cur.fetchall():
                candidates.append({
                    "id": r[0], "name": r[1], "mentions": r[2],
                    "headline": r[3] or "", "url": r[4] or "",
                })
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        out["ok"] = False
        out["error"] = f"candidate query failed: {e}"
        try: c.close()
        except Exception: pass
        return out

    out["considered"] = len(candidates)

    try:
        for cand in candidates:
            name = cand["name"]
            headline = cand["headline"]
            url = cand["url"]
            if not url:
                out["skipped_no_url"] += 1
                continue
            # Only promote candidates whose headline reads like a real
            # facility announcement (groundbreaking / MW / acres / new DC).
            if not (is_facility_announcement(name, headline)
                    or is_facility_announcement(headline, name)
                    or _is_real_entity(name)):
                out["skipped_not_announcement"] += 1
                continue

            facility = extract_facility_from_article(
                headline or name, headline, url, "news_ner")
            if not facility:
                # Build a minimal record keyed on the entity name when the
                # headline alone doesn't trip the extractor's heuristics
                # but the entity is a real, repeatedly-mentioned operator.
                if not _is_real_entity(name) or cand["mentions"] < (min_mentions + 1):
                    out["skipped_not_announcement"] += 1
                    continue
                facility = {
                    "name": name[:200], "provider": name[:200],
                    "city": None, "state": None, "country": "US",
                    "latitude": None, "longitude": None,
                    "power_mw": None, "sqft": None,
                    "status": "Announced",
                    "source": "news_ner",
                    "source_url": url,
                    "confidence_score": 0.62,
                    "discovered_at": utc_now().strftime("%Y-%m-%d"),
                    "notes": f"Promoted from news NER candidate "
                             f"({cand['mentions']} mentions): {headline[:160]}",
                    "investment_usd": None, "acreage": None,
                }
            else:
                facility["name"] = (name or facility["name"])[:200]
                # r86g: extract_facility_from_article always sets provider=None so
                # setdefault was a no-op (key exists) -> NULL provider. Use the
                # entity name as provider when none was extracted.
                facility["provider"] = facility.get("provider") or name[:200]
                facility["source"] = "news_ner"
                facility["notes"] = (
                    f"Promoted from news NER candidate "
                    f"({cand['mentions']} mentions). "
                    f"{facility.get('notes') or ''}")[:400]

            # r-headline-reject (2026-08-09). The gate above accepts anything
            # _is_real_entity() likes, and that predicate's own docstring
            # justifies its permissiveness with "these rows are candidates,
            # never auto-promoted" — a sentence this function (Item 2c,
            # 2026-06-13) made false. The result: 61 NER spans published as
            # live indexable facility pages — Copilot, GitHub, FERC, CISA,
            # Chevron, "Why OT Security Can", "Texas Batch Zero" — plus the
            # headline family ("Stack breaks ground on second Tokyo data
            # center"). insert_discovered_facility() now refuses these too;
            # checking here as well lets us mark the candidate 'rejected'
            # instead of the misleading 'promoted' the old flip applied to
            # every attempt, successful or not.
            try:
                from util.facility_name_sanity import facility_reject_reason
                _reject = facility_reject_reason(facility)
            except Exception:
                _reject = None
            if _reject:
                out["skipped_not_a_facility"] += 1
                logger.info("[news-ner promote] REJECTED %r (%s)",
                            (facility.get("name") or "")[:80], _reject)
                if not dry_run:
                    try:
                        with c.cursor() as cur:
                            cur.execute(
                                "UPDATE news_discovered_entities "
                                "SET status = 'rejected', last_seen_at = NOW(), "
                                "notes = %s WHERE id = %s",
                                (f"not a facility ({_reject})", cand["id"]))
                        c.commit()
                    except Exception:
                        try: c.rollback()
                        except Exception: pass
                continue

            # r-entityres (2026-07-04): exact match already failed (that's
            # why this candidate is here) — semantic check BEFORE creating
            # a possibly-duplicate discovered_facilities row. Fail-soft:
            # resolver returns None on any failure → create as before.
            resolved = _resolve_existing_semantic(facility)
            if resolved:
                logger.info(
                    f"[news-ner entityres] RESOLVED to existing facility "
                    f"#{resolved['id']} '{resolved['name']}' — new candidate "
                    f"'{facility['name']}' NOT created "
                    f"(cosine={resolved['cosine']})")
                out["resolved_existing"] += 1
                if len(out["examples"]) < 20:
                    out["examples"].append({
                        "name": facility["name"],
                        "resolved_to_id": resolved["id"],
                        "resolved_to_name": resolved["name"],
                        "cosine": resolved["cosine"],
                        "mentions": cand["mentions"],
                        "url": url,
                    })
                if dry_run:
                    continue
                # Flip the NER candidate so it isn't re-considered.
                try:
                    with c.cursor() as cur:
                        cur.execute(
                            "UPDATE news_discovered_entities "
                            "SET in_facilities = TRUE, "
                            "status = 'resolved_existing', "
                            "last_seen_at = NOW(), notes = %s "
                            "WHERE id = %s",
                            (f"semantic-resolved to discovered_facilities "
                             f"#{resolved['id']} ({resolved['name'][:120]}) "
                             f"cosine={resolved['cosine']}",
                             cand["id"]))
                    c.commit()
                except Exception:
                    try: c.rollback()
                    except Exception: pass
                continue

            if dry_run:
                out["promoted"] += 1
                if len(out["examples"]) < 20:
                    out["examples"].append({
                        "name": facility["name"],
                        "status": facility.get("status"),
                        "mentions": cand["mentions"],
                        "url": url,
                    })
                continue

            try:
                new_id = insert_discovered_facility(c, facility)
            except Exception as e:
                out["errors"] += 1
                logger.info(f"[news-ner promote] insert failed for "
                            f"{name[:30]}: {str(e)[:120]}")
                continue

            # Flip the candidate so it won't be re-promoted, whether or not
            # the insert was a fresh row (None = dup source_url already there).
            try:
                with c.cursor() as cur:
                    cur.execute(
                        "UPDATE news_discovered_entities "
                        "SET in_facilities = TRUE, status = 'promoted', "
                        "last_seen_at = NOW() WHERE id = %s",
                        (cand["id"],))
                c.commit()
            except Exception:
                try: c.rollback()
                except Exception: pass

            if new_id:
                out["promoted"] += 1
                if len(out["examples"]) < 20:
                    out["examples"].append({
                        "name": facility["name"],
                        "id": new_id,
                        "mentions": cand["mentions"],
                        "url": url,
                    })
    finally:
        try: c.close()
        except Exception: pass

    out["finished_at"] = utc_iso_z()
    return out


# ── Endpoints ────────────────────────────────────────────────────────
@news_ner_bp.route("/api/v1/admin/news-ner/run", methods=["POST"])
def ner_run():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    days = int(request.args.get("days") or "1")
    dry_run = (request.args.get("dry_run") or "").lower() in ("1", "true", "yes")
    # Item 2c: optionally chain promotion after the scan in one call so the
    # cron can do scan→promote with a single POST. ?promote=1
    result = _scan(days, dry_run)
    if (request.args.get("promote") or "").lower() in ("1", "true", "yes"):
        result["promotion"] = _promote_candidates(dry_run=dry_run)
    return jsonify(result)


@news_ner_bp.route("/api/v1/admin/news-ner/re-resolve", methods=["POST"])
def ner_reresolve():
    """On-demand run of the FALSE→TRUE re-resolve pass (also runs per scan)."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    cap = int(request.args.get("cap") or "500")
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        res = _reresolve_unmatched(c, cap=cap)
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=("error" not in res), **res)


@news_ner_bp.route("/api/v1/admin/news-ner/promote", methods=["POST"])
def ner_promote():
    """Promote high-confidence NER candidates → discovered_facilities.

    Gate: X-Admin-Key. Query:
      ?min_mentions=N   override mention threshold (default env-driven, 2)
      ?limit=N          max candidates to promote this run (default 50)
      ?dry_run=1        evaluate + count but DO NOT insert/flip.
    """
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    mm = request.args.get("min_mentions")
    lim = request.args.get("limit")
    dry_run = (request.args.get("dry_run") or "").lower() in ("1", "true", "yes")
    return jsonify(_promote_candidates(
        min_mentions=int(mm) if mm else None,
        limit=int(lim) if lim else None,
        dry_run=dry_run))


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
            # ★ NEVER re-classify something that resolves to a real facility
            # or provider (r-ner-purge-guard 2026-07-27). This endpoint applies
            # a HEURISTIC destructively across every row, and that heuristic
            # has been wrong before — until 07-27 it rejected every
            # single-token mixed-case name, i.e. Google, Apple, Huawei,
            # Vodafone, CoreWeave. `in_facilities` is ground truth from the
            # facilities table; a guess must never overrule it. This guard is
            # what makes the predicate safe to keep improving, and it is the
            # invariant Shell #36 lane 5b asserts (in_facilities AND
            # status='rejected' must be 0).
            cur.execute("""
                SELECT id, entity_name FROM news_discovered_entities
                 WHERE status NOT IN ('rejected', 'seeded')
                   AND COALESCE(in_facilities, FALSE) = FALSE
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
