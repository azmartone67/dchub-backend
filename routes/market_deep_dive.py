"""Phase ZZZZ (2026-05-16) — market deep-dive narrative generator.

Closes one of the last big DCHawk/dcByte gaps: per-market narrative
reports. We have DCPI scores (numbers); they have 50-page market
reports (story). This module lets Claude WRITE the story nightly
from our live data.

  POST /api/v1/markets/<slug>/regenerate     admin trigger
  GET  /api/v1/markets/<slug>/deep-dive      JSON deep-dive
  GET  /markets/<slug>/deep-dive             HTML page (schema.org Article)
  POST /api/v1/markets/deep-dive/cron        daily cron — rotates through markets

For each market, Claude is given:
  - DCPI score + rank + recent delta
  - Facility count + total MW
  - Recent M&A deals touching the market
  - Top operators present
…and asked to write a 400-500 word narrative analysis with schema.org
Article markup. Persisted to market_deep_dives table. Daily cron
rotates so all top 100 markets get refreshed at least monthly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import datetime
from flask import Blueprint, Response, jsonify, request, abort, redirect

from util.market_aliases import DCPI_METRO_ALIASES, canonical_slug
from util.dcpi_score_row import PUBLISHED_ONLY
from util.market_entity import SITE as ENTITY_SITE, market_entity
from util.slug_suffix import normalize_periods
from utils.anthropic_helper import anthropic_messages_url
from routes.brain_llm_spend import instrumented_post as _llm_post


logger = logging.getLogger(__name__)
market_deep_dive_bp = Blueprint("market_deep_dive", __name__)

_ADMIN_KEY     = (os.environ.get("DCHUB_ADMIN_KEY")
                  or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
_ANTHROPIC_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


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
CREATE TABLE IF NOT EXISTS market_deep_dives (
    market_slug    TEXT PRIMARY KEY,
    market_name    TEXT NOT NULL,
    narrative_md   TEXT NOT NULL,
    key_stats      JSONB,
    word_count     INT,
    generated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model_used     TEXT
);
CREATE INDEX IF NOT EXISTS ix_mdd_generated ON market_deep_dives(generated_at DESC);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


def _market_name_candidates(name: str) -> list[str]:
    """Deterministic spellings under which this market's facilities may be
    keyed in facilities.city / discovered_facilities.market / .city.

    r-name-variants (2026-08-01): after the fleet-filter fix, 17 of the 19
    zero-count briefs STAYED zero because the market's display name never
    matches any city key — "Dallas–Fort Worth" (en dash), "Cheyenne, WY"
    (state suffix), "Québec City" (accent), "Washington, DC" (city rows say
    "Washington", market rows say "Washington DC"). Variants are derived ONLY
    from the market's own name — deliberately NOT main.MARKET_ALIASES, whose
    city lists carry common-word cities (Aurora, Arlington) that would bleed
    matches in from other states.

    EN dash pairs metro co-anchors ("Dallas–Fort Worth" -> Dallas, Fort
    Worth); the ASCII hyphen joins compound city names (Winston-Salem) and
    must never split.
    """
    import unicodedata
    base: list[str] = []

    def _add(lst, s):
        s = " ".join((s or "").split())
        if s and s.lower() not in {x.lower() for x in lst}:
            lst.append(s)

    _add(base, name)
    if "," in (name or ""):
        _add(base, name.replace(",", ""))          # "Washington, DC" -> "Washington DC"
        _add(base, name.split(",")[0])             # "Cheyenne, WY"   -> "Cheyenne"
    for dash in ("–", "—"):
        if dash in (name or ""):
            _add(base, name.replace(dash, "-"))    # "Dallas-Fort Worth" as stored
            for part in name.split(dash):
                _add(base, part)
    out = list(base)
    for v in base:
        folded = unicodedata.normalize("NFKD", v).encode("ascii", "ignore").decode()
        _add(out, folded)                          # "Québec City" -> "Quebec City"
    return out


def _collision_slugs() -> set:
    """Slugs on EITHER side of a _CITY_MARKET_DISAMBIGUATION city-name
    collision — the bare-city slug and its state-suffixed twin.

    r-twin-bleed (2026-08-03): read from routes.dcpi so there is exactly one
    source of truth. A copied literal here would drift the moment someone adds
    a third twin (the regex-twin class), and the drift would be silent because
    the only symptom is a brief quietly counting the other state again.
    """
    try:
        from routes.dcpi import _CITY_MARKET_DISAMBIGUATION as _dis
    except Exception:
        return set()          # never block a brief on the import
    out = set()
    for (_bare, _st), (_slug, _name) in _dis.items():
        out.add(_bare)
        out.add(_slug)
        # The OTHER side of the collision may live under a hardcoded slug the
        # table never names: bare 'portland' is retired and aliased onto
        # 'portland-or', so without this the Oregon brief keeps counting
        # Maine's 6 facilities (72 vs its true 66). Resolve through the
        # existing alias map rather than hardcoding the twin.
        try:
            from util.market_aliases import canonical_slug as _canon
            _target = _canon(_bare)
            if _target:
                out.add(_target)
        except Exception:
            pass
    return out


_FAC_UNION_SQL = """
        WITH fac_all AS (
          SELECT LOWER(COALESCE(name,''))||'|'||LOWER(COALESCE(provider,'')) AS k,
                 COALESCE(power_mw,0) AS mw, provider
            FROM facilities
           WHERE LOWER(COALESCE(city,'')) = ANY(%(names)s)
             AND (NOT %(qualify)s
                  OR UPPER(TRIM(COALESCE(state,''))) = %(state)s)
          UNION ALL
          SELECT LOWER(COALESCE(name,''))||'|'||LOWER(COALESCE(provider,'')),
                 COALESCE(power_mw,0), provider
            FROM discovered_facilities
           WHERE (LOWER(COALESCE(market,'')) = ANY(%(names)s)
               OR LOWER(COALESCE(city,''))   = ANY(%(names)s))
             AND COALESCE(is_duplicate, 0) = 0
             AND (NOT %(qualify)s
                  OR UPPER(TRIM(COALESCE(state,''))) = %(state)s)
        ), fac AS (
          SELECT k, MAX(mw) AS mw, MIN(provider) AS provider
            FROM fac_all GROUP BY k
        )
    """


def _has_metric(v):
    """True when a metric slot holds a real reading rather than a placeholder
    dash. Module-level so the shell's tiles and the fleet backfill below agree
    on what "missing" means."""
    return v not in (None, '', '—', '-', '?')


def measured_market_facts(cur, name: str, *, slug: str = "",
                          state: str = "") -> dict | None:
    """{'facility_count': N, 'total_mw': X} for a market, read straight from
    the fleet tables — NO market_power_scores row required.

    r-latam-twin (2026-09-03): /markets/<slug>.json sourced its stats from
    market_deep_dives, i.e. from the NARRATIVE. A market with real inventory
    but no brief therefore had no data twin at all: bogota, mexico-city,
    santiago and sao-paulo were 200 as HTML and 404 as .json, while carrying
    40 / 31 / 102 / 55 tracked facilities. The narrative is a publication
    decision; the measurement is not, and the twin is the measurement.

    Same union as _gather_market_facts (one SQL string, hoisted) so the twin
    and the brief can never count a market two ways.

    Returns None when nothing matches, and OMITS a zero rather than reporting
    one: `market_entity` publishes only measures it holds, and a fabricated
    "0 facilities" for a market we merely failed to key on is the #1546 /
    r-nova-zero shape that once published "avoid entering Northern Virginia".
    """
    _names = [c.lower() for c in _market_name_candidates(name)]
    _state = (state or "").strip().upper()
    _args = {
        "names":   _names,
        "qualify": bool(_state) and (slug or "") in _collision_slugs(),
        "state":   _state,
    }
    try:
        cur.execute(_FAC_UNION_SQL +
                    "SELECT COUNT(*), COALESCE(SUM(mw),0) FROM fac", _args)
        row = cur.fetchone()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    out = {"facility_count": int(row[0])}
    _mw = float(row[1] or 0)
    if _mw > 0:
        out["total_mw"] = _mw
    return out


def _unscored_market_facts(cur, slug: str) -> dict | None:
    """Facts for a market the site PUBLISHES but the DCPI does not score.

    r-latam-rotation (2026-09-04): `market_not_found` was the generator's
    answer for bogota, mexico-city, santiago and sao-paulo — four curated,
    sitemapped metros carrying 40 / 31 / 102 / 55 tracked facilities. It is a
    false answer: those markets exist, they are simply absent from
    routes/dcpi.py's `_MARKETS_HARDCODED`, so no market_power_scores row is
    ever written for them and the mps lookup above finds nothing.

    Two consequences, both closed here. `generate_for_market` returned an
    error that reads as "no such market", and — because it writes NOTHING on
    that error — widening cron_rotate to reach these slugs would have pinned
    them at the head of `generated_at NULLS FIRST` forever (r-cron-starvation).
    Resolving them to real, measured facts gives the rotation a market it can
    reach and a definite outcome to record.

    Scoped to CURATED_MARKET_SLUGS, which is exactly the part of the sitemap
    emitted WITHOUT an mps row. The sitemap's other /markets arm
    (US_CITY_MARKET_SQL) INNER JOINs market_power_scores, so a US city slug
    reaching this function has no row and therefore was not sitemapped either
    — `market_not_found` is the honest answer for it. A junk slug likewise
    resolves to None, so r-soft404 does not re-open through the generator.

    `unscored` is the load-bearing key: it is what lets `_brief_guard_reason`
    tell "the DCPI does not cover this market" apart from "dcpi_score is None
    because a join broke". Both used to read `score_none`, and the neutral
    copy seeded for that reason asserts the market has no tracked facilities —
    false for all four of these, and the reason the placeholder text below is
    chosen from the reason rather than being one string.

    NO score is invented. constraint / excess / verdict stay None: a market
    with no DCPI row has no DCPI verdict, and minting one is the shape
    derive_composite_score's `or 0` coercion is guarded against upstream.
    """
    slug = (slug or "").lower().strip()
    if slug not in set(CURATED_MARKET_SLUGS):
        return None
    name = _slug_title(slug)
    out = {
        "slug":       slug, "name": name,
        "state":      None,
        "unscored":   True,
        "dcpi_score": None,
        "constraint": None,
        "excess":     None,
        "verdict":    None,
        "computed":   None,
        "top_operators": [],
        "recent_deals":  [],
    }
    # Same union as the scored path and as the .json twin — one measurement,
    # never a second count of the same market. OMITS a zero rather than
    # reporting one (r-nova-zero): a fabricated "0 facilities" is what once
    # published "avoid entering Northern Virginia".
    measured = None
    try:
        measured = measured_market_facts(cur, name, slug=slug)
    except Exception:
        measured = None
    if measured:
        out.update(measured)
    return out


def _gather_market_facts(cur, slug: str) -> dict | None:
    """Pull live facts for the market from market_power_scores +
    discovered_facilities + deals."""
    # RAG v1 root-cause fix (2026-07-03): the live market_power_scores table
    # has NO `score` column (the DCPI composite is computed on the fly, never
    # stored — the same schema drift market_brief r-fix-3 documents). Selecting
    # it threw, the bare except returned None, and EVERY market came back
    # "market_not_found" — which is why market_deep_dives sat at 0 rows and the
    # daily cron_rotate silently generated nothing. Select only the
    # writer-guaranteed columns.
    try:
        # r-portland-canon (2026-08-02): an EXACT slug match must beat the
        # name-match fallback. Two DIFFERENT markets can share a display
        # name — market_name 'Portland' was both the bare-'portland' row
        # (Portland, ME) and 'portland-or' (Portland, OR) — and ordering on
        # computed_at alone made the resolution depend on which twin the
        # recompute happened to write last. That is how
        # generate_for_market('portland') silently resolved (and upserted)
        # portland-or every night. The name clause stays as a fallback so a
        # metro page whose mps row lives under another slug still resolves.
        # r-market-canon-split (2026-09-05): resolve the alias BEFORE the
        # query and read only PUBLISHED rows. Both halves are load-bearing and
        # they fail differently.
        #
        # Without the alias resolution this asked for the literal URL slug, and
        # the retired twins still HAVE a row — that is why r-twin-unpublish
        # unpublished them rather than deleting them. `northern-virginia` is
        # one, frozen at 2026-07-19, and the exact-slug clause in the old
        # ORDER BY did not merely fail to exclude it, it PREFERRED it. Measured
        # live 2026-09-05: /markets/northern-virginia published "DCPI score
        # 11.7/100" while /dcpi/ashburn — the same market, the published row —
        # published 27.4. Same shape as the r-poe-canon fix in
        # ai_interconnection.py, same two lines of fix.
        #
        # Without PUBLISHED_ONLY the resolution alone is not enough: the
        # name-match fallback below can still land on an unpublished twin
        # (market_name 'Northern Virginia' belongs to the retired row), and
        # `_load_markets_dynamic` can mint an unpublished row under any slug.
        # PUBLISHED_ONLY is the same predicate character-for-character that
        # /dcpi and /api/v1/dcpi/scores serve on, which is the whole point:
        # agreement is not something a second, independently-worded predicate
        # can promise.
        #
        # A twin whose canonical row is missing or unpublished resolves to
        # nothing and falls through to _unscored_market_facts / a 404 — the
        # honest answer, and never back to the frozen row.
        _canon = canonical_slug(slug) or slug
        cur.execute(f"""
            SELECT market_slug, market_name,
                   constraint_score, excess_power_score, verdict,
                   computed_at, time_to_power_months, state
              FROM market_power_scores
             WHERE (LOWER(market_slug) = LOWER(%s)
                    OR LOWER(market_name) = LOWER(REPLACE(%s, '-', ' ')))
               AND {PUBLISHED_ONLY}
             ORDER BY (LOWER(market_slug) = LOWER(%s)) DESC,
                      computed_at DESC LIMIT 1
        """, (_canon, _canon, _canon))
        r = cur.fetchone()
    except Exception:
        # A failed QUERY is not an unscored market — fall through to the
        # unscored resolver here and a transient outage would be recorded as
        # a DCPI coverage state. `market_not_found` stays the honest answer.
        return None
    if not r:
        return _unscored_market_facts(cur, slug)
    out = {
        "slug":       r[0], "name": r[1],
        "state":      r[7],
        "dcpi_score": None,
        "constraint":int(r[2]) if r[2] is not None else None,
        "excess":    int(r[3]) if r[3] is not None else None,
        "verdict":   r[4],
        "computed":  r[5].isoformat() if r[5] else None,
    }
    # r-none-score (2026-08-01): "composite is derived at read time, never
    # stored" used to mean this dict shipped dcpi_score=None into key_stats,
    # and the /markets/<slug> template's stats.get('dcpi_score','?') rendered
    # the stored None as literal "DCPI score None/100" on every page — .get
    # fallbacks only fire on MISSING keys, not null values. Derive the
    # composite here the same way /api/v1/dcpi/scores does. Both components
    # must exist: derive_composite_score coerces None to 0, which would mint
    # a plausible-looking score for a market with no data.
    if r[2] is not None and r[3] is not None:
        try:
            from routes.dcpi import derive_composite_score
            out["dcpi_score"] = derive_composite_score(r[3], r[2], r[6], r[4])
        except Exception:
            pass  # stays None -> _brief_guard_reason keeps it off the page
    # Facilities + MW. r-market-facts (2026-07-06): the old query matched ONLY
    # discovered_facilities.market = market_name — correct for US metros (e.g.
    # 'Northern Virginia' = 9 fac) but returned 0 for city-keyed markets whose
    # facilities live in the `facilities` table by CITY (Amsterdam=108, Paris=89,
    # Frankfurt=85, Atlanta=67). That made Haiku write "zero tracked facilities /
    # 0 MW" for ~140 real markets. Now count by market_name OR city across BOTH
    # tables, deduped by name|provider. Metro markets (city-join=0) still resolve
    # via the disc.market clause, so NoVa-style counts are unchanged.
    # r-nova-zero (2026-08-01): the discovered_facilities arm filtered
    # `merged_at IS NULL AND is_duplicate = 0` — that intersection is the
    # PENDING-REVIEW queue, not the fleet: merge_discovered_v3 stamps
    # merged_at=NOW() on every row it promotes (the #1546 class). As NoVa's
    # rows got promoted the metro count decayed to 0 — no city in `facilities`
    # is named "Northern Virginia", so this arm was its ONLY source — and the
    # writer turned the broken zero into "avoid entering Northern Virginia".
    # Fleet filter is COALESCE(is_duplicate,0)=0 alone. Promoted rows now
    # exist in BOTH tables, so the name|provider dedup must be an explicit
    # GROUP BY k — UNION only collapses twins whose mw happens to match.
    # r-twin-bleed (2026-08-03): %(qualify)s gates a state predicate that is a
    # NO-OP for every market except the two sides of a known city-name
    # collision. A state-suffixed twin's display name comma-strips back to the
    # bare city ("Portland, ME" -> "Portland"), so this name-match join
    # re-merged exactly what the r-portland-canon / r-aurora-canon slug
    # disambiguation had just separated: measured live 2026-08-03,
    # /markets/aurora and /markets/aurora-co served BYTE-IDENTICAL "29
    # facilities, 209 MW", and /markets/portland-me served Oregon's "70
    # facilities, 578 MW" for a ~10-facility Maine market. Regenerating does
    # not help — those rows regenerated AFTER both fixes and were still wrong.
    #
    # Scoped to collision slugs ON PURPOSE; a blanket state qualifier is a
    # WRONG fix twice over: (1) metro-keyed markets match by `market`, not
    # city, and carry mixed member states — qualifying them re-opens the NoVA
    # zero (#1546 / r-nova-zero class); (2) 17 Columbus rows spell the state
    # 'OHIO' rather than 'OH', so a strict equality would silently delete real
    # Amazon New Albany / AWS CMH facilities from that brief.
    # Kept as ONE sql string so both call sites below can never diverge.
    _fac_union = _FAC_UNION_SQL
    _names = [c.lower() for c in _market_name_candidates(out["name"])]
    # Qualify by state ONLY for the two sides of a known city-name collision.
    # A blank/unknown mps state disables it (fail OPEN to today's behaviour —
    # an over-count is recoverable, silently zeroing a brief is the #1546 class
    # that shipped "avoid entering Northern Virginia").
    _state = (out.get("state") or "").strip().upper()
    _fac_args = {
        "names":   _names,
        "qualify": bool(_state) and out["slug"] in _collision_slugs(),
        "state":   _state,
    }
    try:
        cur.execute(_fac_union + "SELECT COUNT(*), COALESCE(SUM(mw),0) FROM fac",
                    _fac_args)
        f = cur.fetchone()
        out["facility_count"] = int(f[0] or 0)
        out["total_mw"]       = float(f[1] or 0)
    except Exception:
        out["facility_count"] = 0
        out["total_mw"]       = 0
    # Top operators (same market-OR-city union)
    try:
        cur.execute(_fac_union + """
            SELECT provider, COUNT(*) AS n FROM fac
             WHERE provider IS NOT NULL
             GROUP BY provider ORDER BY n DESC LIMIT 5
        """, _fac_args)
        out["top_operators"] = [{"name": p[0], "count": int(p[1])} for p in cur.fetchall()]
    except Exception:
        out["top_operators"] = []
    # Recent deals
    try:
        cur.execute("""
            SELECT date, buyer, seller, value, mw
              FROM deals
             WHERE LOWER(COALESCE(market,'')) = LOWER(%s)
                OR LOWER(COALESCE(region,'')) = LOWER(%s)
             ORDER BY date DESC NULLS LAST LIMIT 5
        """, (out["name"], out["name"]))
        out["recent_deals"] = [{
            "date": d[0].isoformat() if hasattr(d[0],"isoformat") else (str(d[0]) if d[0] else None),
            "buyer": d[1], "seller": d[2],
            "value": float(d[3]) if d[3] is not None else None,
            "mw":    float(d[4]) if d[4] is not None else None,
        } for d in cur.fetchall()]
    except Exception:
        out["recent_deals"] = []
    return out


def _retrieve_grounding(facts: dict) -> str:
    """RAG grounding for the deep-dive writer (2026-07-04).

    Before this, the haiku writer saw ONLY the one market's SQL facts —
    no deal color, no news, no peer-market comparisons. Pull top-k
    semantic matches from the brain RAG corpora (routes/brain_rag.py)
    and return a clearly-labeled GROUNDING CONTEXT block. Synergy: the
    resulting richer narratives are themselves embedded into the
    market_narratives corpus, so better grounding compounds downstream.

    FAIL-SOFT: any failure (import, DB, embed provider, per-corpus query)
    returns "" so the composed prompt is byte-identical to the ungrounded
    version — grounding can only ever ADD, never break generation.
    """
    try:
        from routes.brain_rag import retrieve_context
    except Exception:
        return ""
    name = (facts.get("name") or "").strip()
    slug = (facts.get("slug") or "").strip().lower()
    if not name:
        return ""
    # facts carries no state today; include it if a future writer adds one.
    query = " ".join(p for p in (
        name, (facts.get("state") or "").strip(),
        "data center acquisition investment") if p)

    def _pull(k: int, corpus: str) -> list:
        try:
            return retrieve_context(query, k=k, corpus=corpus) or []
        except Exception:
            return []

    deals = _pull(4, "deals")
    news = _pull(4, "news_articles")
    # Peer-market context: the market's OWN narrative chunks
    # (source_id='<slug>#<n>') would dominate top-k for a query containing
    # its own name — over-fetch, then exclude self in Python, keep 3 peers.
    peers = [h for h in _pull(8, "market_narratives")
             if not (slug and str(h.get("source_id") or "").lower().startswith(slug))][:3]

    def _fmt(hits: list) -> str:
        lines = []
        for h in hits:
            txt = " ".join(str(h.get("text") or "").split())[:400]
            if txt:
                lines.append(f"- [{h.get('source_id') or '?'}] {txt}")
        return "\n".join(lines)

    sections = []
    d = _fmt(deals)
    if d:
        sections.append("RELATED DEALS (semantic matches):\n" + d)
    n = _fmt(news)
    if n:
        sections.append("RELATED NEWS (semantic matches):\n" + n)
    p = _fmt(peers)
    if p:
        sections.append("PEER-MARKET CONTEXT (other markets, for comparison only):\n" + p)
    if not sections:
        return ""
    return (
        f"GROUNDING CONTEXT — retrieved from DC Hub's internal knowledge "
        f"base. These are semantic matches and MAY include items that are "
        f"not actually about {name}: weave in ONLY what is clearly relevant "
        f"to {name}, silently ignore the rest, and NEVER invent, derive, or "
        f"repeat numbers from this section — every number you cite must come "
        f"from the live stats above.\n\n" + "\n\n".join(sections)
    )


def _compose_prompt(facts: dict) -> str:
    """Build the writer prompt. Split out of _ask_claude_to_write so tests
    can assert on the exact prompt without mocking the Anthropic call."""
    deals_str = ", ".join(
        f"{d.get('buyer','?')}→{d.get('seller','?')} ({'$'+format(d['value'],',.0f') if d.get('value') else '?'}, {d.get('date') or '?'})"
        for d in (facts.get("recent_deals") or [])[:5]
    ) or "no recent M&A tracked"
    operators_str = ", ".join(
        f"{o['name']} ({o['count']})" for o in (facts.get("top_operators") or [])[:5]
    ) or "operator mix not yet aggregated"
    try:
        grounding = _retrieve_grounding(facts)
    except Exception:
        grounding = ""
    # When grounding is "" this collapses to the original "…\n\n" join, so
    # a retrieval failure yields a prompt byte-identical to the ungrounded one.
    grounding_block = f"\n{grounding}\n" if grounding else ""
    return (
        f"You are writing a 400-word market analysis for data-center "
        f"investors and operators. Be specific, cite the live numbers, "
        f"avoid generic platitudes. Output plain markdown, no preamble.\n\n"
        f"MARKET: {facts['name']}\n"
        f"DCPI: excess-power {facts.get('excess','?')}/100, "
        f"constraint {facts.get('constraint','?')}/100 "
        f"(verdict: {facts.get('verdict','?')})\n"
        f"Tracked facilities: {facts.get('facility_count')} | total MW: {facts.get('total_mw'):,.0f}\n"
        f"Top operators: {operators_str}\n"
        f"Recent M&A: {deals_str}\n"
        f"{grounding_block}"
        f"\nWrite four paragraphs: (1) current state in one sentence, "
        f"then 2-3 specific facts; (2) what the DCPI verdict means for "
        f"buyers; (3) deal flow + operator dynamics; (4) one forward-"
        f"looking sentence. Maximum 500 words. No headings, just paragraphs."
    )


def _ask_claude_to_write(facts: dict) -> tuple[str | None, str | None]:
    if not _ANTHROPIC_KEY:
        return None, "no_anthropic_api_key"
    prompt = _compose_prompt(facts)
    try:
        import requests
        r = _llm_post("market_deep_dive",
            anthropic_messages_url(),
            headers={"x-api-key": _ANTHROPIC_KEY,
                     "User-Agent": "dchub-brain/1.0",
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001",
                  "max_tokens": 1000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=20,
        )
        if r.status_code >= 300:
            return None, f"status_{r.status_code}_{r.text[:100]}"
        text = (r.json().get("content", [{}])[0] or {}).get("text", "").strip()
        return text, None
    except Exception as e:
        return None, f"{type(e).__name__}:{str(e)[:60]}"


def _brief_guard_reason(stats: dict) -> str | None:
    """Reason this market's facts must NOT become (or be served as) an LLM
    brief, or None when they may.

    The 2026-08-01 lesson (the quality-gate-false-claim class): Haiku
    RATIONALIZES broken inputs — a dead join fed it "0 facilities" for
    Northern Virginia and the published brief told buyers to avoid the #1
    market on earth. facilities=0 and score=None are data-bug shapes, not
    analysis inputs. Genuinely-empty markets get neutral copy, never a
    confident verdict built on a zero.
    """
    stats = stats or {}
    # r-latam-rotation (2026-09-04): "the DCPI does not score this market" and
    # "dcpi_score is None because a join broke" are different facts and used to
    # be one reason. Both still refuse the LLM — a brief must never be written
    # over a null score — but only one of them is a DATA BUG, and the neutral
    # copy seeded downstream is chosen from this string. Reported separately so
    # a coverage gap cannot be read as a defect, or a defect as coverage.
    if stats.get("unscored"):
        return "market_unscored"
    if stats.get("dcpi_score") is None:
        return "score_none"
    if not stats.get("facility_count"):
        return "zero_facilities"
    return None


def _guard_placeholder_text(reason: str, facts: dict) -> str:
    """The neutral narrative seeded for a market the guard refuses.

    r-latam-rotation (2026-09-04): there used to be ONE string, and it opens
    "DC Hub does not currently track operational data-center facilities for
    {name}". That is a claim about the fleet, and the guard fires for reasons
    that have nothing to do with the fleet — a market DCPI does not score
    still has its facilities. Seeded for bogota (40 tracked facilities),
    santiago (102), sao-paulo (55) or mexico-city (31) it is simply false, and
    a placeholder is still published copy.

    So the sentence is chosen from the reason, and the zero-facilities claim
    is made ONLY where a zero was actually measured. Every branch stays
    verdict-free and score-free: the guard's whole point is that nothing here
    may read as analysis.
    """
    name = (facts or {}).get("name") or "this market"
    n = (facts or {}).get("facility_count") or 0
    if n:
        fleet = (f"DC Hub tracks {n:,} data-center "
                 f"{'facility' if n == 1 else 'facilities'} in {name}.")
    else:
        fleet = (f"DC Hub does not currently track operational data-center "
                 f"facilities for {name}.")
    if reason == "market_unscored":
        # TRUE for the LatAm four and for any future published market the
        # DCPI universe has not adopted: the fleet reading is real, the score
        # is absent, and the brief waits on the score rather than on the fleet.
        return (f"{fleet} The DC Hub Power Index does not yet score {name}, "
                f"and a market brief is published here only once DCPI "
                f"scoring covers the market — no verdict is written without "
                f"one. Facility-level data for {name} remains available "
                f"across DC Hub's market surfaces.")
    return (f"{fleet} A full market brief is published here only once "
            f"verified facility and power data for the market clears our "
            f"quality checks. Live DCPI scoring and grid data for {name} "
            f"remain available across DC Hub's market surfaces.")


def generate_for_market(slug: str) -> dict:
    """Pull facts + ask Claude + persist. Returns the generated record."""
    out = {"ok": False, "slug": slug}
    # r-portland-canon (2026-08-02): the brief is persisted under the PAGE
    # slug the caller asked for (canonicalized), NEVER under facts["slug"].
    # facts["slug"] is whichever market_power_scores row the resolver landed
    # on — when a name-twin exists (two markets both named 'Portland') that
    # was the OTHER market's slug, so the nightly cron targeting 'portland'
    # rewrote the 'portland-or' row forever and the requested page's brief
    # could never refresh (st.-louis class, 702a7bd0).
    page_slug = (slug or "").lower().strip()
    page_slug = MARKETS_DEEP_DIVE_PAGE_CANON.get(page_slug, page_slug)
    c = _conn()
    if c is None:
        out["error"] = "no_database"; return out
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            facts = _gather_market_facts(cur, slug)
            if not facts:
                out["error"] = "market_not_found"; return out
            # Guard BEFORE the LLM call: never spend tokens writing a brief
            # from guard-shaped facts, and never overwrite a stored narrative
            # with one. Deliberately writes NOTHING on refusal — a transient
            # DB error upstream reads as facility_count=0, and persisting
            # anything here would let an outage overwrite good narratives.
            _guard = _brief_guard_reason(facts)
            if _guard:
                out["error"] = f"brief_guard_{_guard}"
                out["guard"] = True
                # r-cron-starvation (2026-08-02, measured live): a guarded
                # market with NO deep-dive row sits at the head of
                # cron_rotate's `generated_at NULLS FIRST` ordering FOREVER —
                # ~10 genuinely-empty markets ate every daily slot and the
                # rotation generated nothing. Seed a neutral placeholder so
                # the row's generated_at takes the market out of the
                # starvation slot; when facilities (or a score) appear, the
                # market ages back to stalest and the normal upsert replaces
                # the placeholder with a real narrative.
                #
                # r-latam-rotation (2026-09-04): DO NOTHING only DEFERRED the
                # starvation it was written to fix. It sets generated_at once,
                # at first seed, and never again — so a permanently-guarded
                # market's row simply ages, and once it is older than every
                # market the rotation has since refreshed it returns to the
                # head of this same ordering and stays there. Bump the
                # placeholder's generated_at instead, so a market that cannot
                # change goes to the BACK of the queue and comes round on the
                # ordinary monthly cadence, costing a guard check and no
                # tokens.
                #
                # The DO UPDATE is gated on model_used = 'guard-neutral', which
                # keeps the whole of the original guarantee: a row carrying a
                # real narrative is never touched here, so a transient outage
                # that reads as facility_count=0 still cannot overwrite one.
                try:
                    import json as _j
                    neutral = _guard_placeholder_text(_guard, facts)
                    cur.execute("""
                        INSERT INTO market_deep_dives
                          (market_slug, market_name, narrative_md, key_stats,
                           word_count, model_used)
                        VALUES (%s, %s, %s, %s::jsonb, %s, %s)
                        ON CONFLICT (market_slug) DO UPDATE
                          SET market_name  = EXCLUDED.market_name,
                              narrative_md = EXCLUDED.narrative_md,
                              key_stats    = EXCLUDED.key_stats,
                              word_count   = EXCLUDED.word_count,
                              generated_at = NOW()
                        WHERE market_deep_dives.model_used = 'guard-neutral'
                    """, (page_slug, facts["name"], neutral,
                          _j.dumps(facts), len(neutral.split()),
                          "guard-neutral"))
                except Exception:
                    pass
                return out
            narrative, err = _ask_claude_to_write(facts)
            if err:
                out["error"] = err; return out
            wc = len(narrative.split())
            import json as _j
            cur.execute("""
                INSERT INTO market_deep_dives
                  (market_slug, market_name, narrative_md, key_stats,
                   word_count, model_used)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (market_slug) DO UPDATE
                  SET market_name  = EXCLUDED.market_name,
                      narrative_md = EXCLUDED.narrative_md,
                      key_stats    = EXCLUDED.key_stats,
                      word_count   = EXCLUDED.word_count,
                      generated_at = NOW(),
                      model_used   = EXCLUDED.model_used
            """, (page_slug, facts["name"], narrative,
                  _j.dumps(facts), wc, "claude-haiku-4-5"))
            out.update({"ok": True, "market": facts["name"],
                        "word_count": wc,
                        "narrative_preview": narrative[:200] + "…"})
    finally:
        try: c.close()
        except Exception: pass
    return out


# RENDER-PERF (2026-06-01): read_deep_dive() hits Postgres on EVERY per-slug
# deep-dive render (/markets/<slug>, /markets/<slug>/deep-dive, and the JSON
# endpoint), and there is no in-process cache — so each cold render eats a DB
# round-trip while the boot path is already saturating the worker pool. Front it
# with the already-connected, cross-worker Redis helper (redis_cache.py — same
# best-effort pattern report_narrative.py uses) so a warm deep-dive survives a
# gunicorn recycle and is shared across workers/replicas. Deep-dives regenerate
# only on a daily cron, so a short TTL is plenty fresh.
#
# Redis is BEST-EFFORT in front of the live DB read: ANY import/connection/
# serialization/parse error falls through to the unchanged DB query below.
# cache_get/set already swallow their own errors and no-op when REDIS_URL is
# unset. SERIALIZATION TRAP: the row's `generated_at` is a datetime and callers
# do `.isoformat()` / `.strftime()` on it — json round-trips it to a STRING, so
# on a Redis hit we re-hydrate it back to a datetime to keep read_deep_dive()'s
# return shape byte-for-byte identical to the DB path. If re-hydration fails for
# any reason we treat it as a cache MISS (fall through to the DB) rather than
# returning a malformed dict. We store only the JSON-safe projection (ISO string
# for generated_at) — Redis setex governs expiry via _DEEP_DIVE_TTL.
_DEEP_DIVE_TTL = 900  # mirrors the deep-dive Cache-Control: public, max-age=900


def _redis_get_deep_dive(slug: str) -> dict | None:
    """Return a re-hydrated deep-dive row dict from Redis, or None on
    miss / any error (caller then falls through to the live DB query)."""
    try:
        from redis_cache import cache_get
        payload = cache_get(f"deep_dive:{slug}")
        if not isinstance(payload, dict) or not payload:
            return None
        out = dict(payload)
        # Re-hydrate generated_at (stored as ISO string) back to a datetime so
        # downstream .isoformat()/.strftime() calls behave exactly as on the DB
        # path. A None timestamp is preserved as None (callers guard for it).
        ga = out.get("generated_at")
        if ga is None:
            return out
        if isinstance(ga, str):
            out["generated_at"] = datetime.datetime.fromisoformat(ga)
            return out
        # Unexpected type — don't risk a malformed row; force a DB read.
        return None
    except Exception:
        return None


def _redis_set_deep_dive(slug: str, r: dict) -> None:
    """Best-effort write a deep-dive row to Redis with the module TTL. Stores a
    JSON-safe projection (generated_at as ISO string). No-op on any error."""
    try:
        from redis_cache import cache_set
        ga = r.get("generated_at")
        payload = {
            "market_slug": r.get("market_slug"),
            "market_name": r.get("market_name"),
            "narrative_md": r.get("narrative_md"),
            "key_stats": r.get("key_stats"),
            "word_count": r.get("word_count"),
            "generated_at": ga.isoformat() if hasattr(ga, "isoformat") else ga,
            "model_used": r.get("model_used"),
        }
        cache_set(f"deep_dive:{slug}", payload, ttl=_DEEP_DIVE_TTL)
    except Exception:
        pass


# r-market-canon-split (2026-09-05): /markets/<slug> canonical-consolidation
# 301s. DERIVED from util.market_aliases.DCPI_METRO_ALIASES, never re-typed —
# that map is the ONE record of which slug names a market, and this dict used
# to be a second, hand-maintained opinion of the same thing.
#
# ★ THE TWO OPINIONS HAD DRIFTED APART, IN OPPOSITE DIRECTIONS. Measured live
# through the edge, cache-busted 2026-09-05, three of the ten twin pairs
# disagreed with /dcpi about which slug is the market:
#
#     /dcpi/northern-virginia  301 -> /dcpi/ashburn      DCPI 27.4
#     /markets/ashburn         301 -> /markets/northern-virginia   score 11.7
#
#     /dcpi/silicon-valley     301 -> /dcpi/santa-clara   DCPI 42.4
#     /markets/silicon-valley  200  (self-canonical)      score 29.9
#     /markets/santa-clara     200  (self-canonical)      score 42.9
#
#     /dcpi/portland           301 -> /dcpi/portland-or
#     /markets/portland-or     301 -> /markets/portland
#
# Every one of those pages carried <link rel=canonical> to ITSELF and each
# sitemap advertised its own side of the pair, so each surface told a crawler
# the other did not exist. These are the pages AI assistants cite: an agent
# asking about Ashburn got 27.4 or 11.7 depending on where it landed.
#
# The canon is Ashburn / Santa Clara / Portland-OR, per PR #3841 ("Northern
# Virginia is tracked as Ashburn") and per DCPI_METRO_ALIASES itself, which
# both /dcpi/<slug> and /api/v1/dcpi/scores/<slug> already resolve through.
# Deriving rather than re-listing is what makes a fourth divergence
# unrepresentable — same lesson as util/iso_taxonomy.py and
# util/dcpi_score_row.py: a slug map is not the hard part, keeping ONE of it is.
#
# The sitemap builder (main.py city-markets shard, via listable_market_slug)
# skips exactly these keys, because a sitemap must never list a URL that
# redirects.
MARKETS_CANONICAL_REDIRECT = dict(DCPI_METRO_ALIASES)


# ── /markets hub inventory (seo F6, 2026-09-02) ─────────────────────────
# ONE source for "which /markets/<slug> and /pockets/<slug> pages exist",
# shared by main.py's sitemap-markets.xml builder and the /markets hub page
# below, so the hub can never list a page the sitemap does not (or vice
# versa). Before this the hub did not exist: /markets and /markets/ served
# static/market-intelligence.html with <link rel=canonical> →
# /market-intelligence (measured live 2026-09-02 00:40Z), so the 580-URL
# markets shard (250 /markets/, 330 /pockets/) had a "hub" that disclaimed
# itself and 561 of those pages were internal-link dead ends (SH52-092).
#
# ★ EVERY ENTRY IS RUN THROUGH canonical_slug (r-market-canon-split
# 2026-09-05). Three of these were alias slugs — 'northern-virginia',
# 'silicon-valley' and 'portland' — so the sitemap advertised, and this hub
# linked, the exact URLs MARKETS_CANONICAL_REDIRECT now 301s away. Listing a
# redirect in a sitemap is the defect r-portland-canon added
# listable_market_slug to prevent; the curated arm simply never went through
# that filter, because it is emitted unconditionally.
_CURATED_MARKET_SLUGS_RAW = (
    'northern-virginia', 'dallas', 'phoenix', 'atlanta', 'chicago',
    'silicon-valley', 'new-york', 'los-angeles', 'portland', 'seattle',
    'salt-lake-city', 'toronto', 'columbus', 'houston', 'denver',
    'london', 'frankfurt', 'amsterdam', 'paris', 'dublin', 'stockholm',
    'singapore', 'tokyo', 'sydney', 'hong-kong', 'mumbai', 'seoul',
    'jakarta', 'kuala-lumpur', 'bangkok', 'sao-paulo', 'mexico-city',
    'santiago', 'bogota',
)


def _canon_unique(slugs):
    """Canonicalise each slug and drop the duplicates that collapse out.

    Order-preserving: the first spelling of a market keeps that market's
    position, so the hub and the sitemap do not reshuffle when an alias is
    added to DCPI_METRO_ALIASES.
    """
    out, seen = [], set()
    for s in slugs:
        c = canonical_slug(s) or s
        if c not in seen:
            seen.add(c)
            out.append(c)
    return tuple(out)


CURATED_MARKET_SLUGS = _canon_unique(_CURATED_MARKET_SLUGS_RAW)


# r-portland-canon (2026-08-02), rebuilt for r-market-canon-split (2026-09-05):
# canonical market slug → the market_deep_dives ROW KEY its brief is stored
# under. A storage key, never a URL, and the only reason it is not the identity
# map is history: these three markets' briefs were written while the alias was
# the page slug, so the rows are keyed 'northern-virginia', 'silicon-valley'
# and 'portland'. Re-keying them would blank three flagship narratives until
# the rotation caught up, and would pin all three at the head of
# `generated_at NULLS FIRST` in the meantime.
#
# DERIVED from the curated list, so it cannot name a pair the redirect map does
# not: the keys are exactly the curated entries that canonicalised to something
# else. The old hand-written version held one pair ('portland-or': 'portland')
# and its comment forbade adding the other two, because at the time the metro
# slug was the page and the city slug was the alias — that direction is now
# reversed for all three at once.
MARKETS_DEEP_DIVE_PAGE_CANON = {
    canonical_slug(_s): _s
    for _s in _CURATED_MARKET_SLUGS_RAW if canonical_slug(_s)
}

# ★★ 2026-07-28 — THE SITEMAP WAS SUBMITTING 404s. The old criterion (>=3
# facilities for a city+state) was NOT the criterion /markets/<slug> serves
# on: the route keys on market_power_scores slugs; `miami` is a market,
# `miami-fl` is not. INNER JOIN on market_power_scores, so a slug can only
# enter the list if a market page exists for it, and emit the CITY slug (the
# city-state form 301s — a sitemap must never contain a redirect).
US_CITY_MARKET_SQL = """
    SELECT m.market_slug AS slug, MAX(f.first_seen) AS lm
      FROM discovered_facilities f
      JOIN market_power_scores m
        ON m.market_slug = LOWER(REPLACE(TRIM(f.city),' ','-'))
     WHERE f.city IS NOT NULL AND TRIM(f.city) <> ''
       AND COALESCE(f.is_duplicate, 0) = 0
       AND f.country IN ('US','USA','United States')
     GROUP BY m.market_slug
    HAVING COUNT(*) >= 3
"""
# fallback (no first_seen column) — SAME market-existence join, so the
# degraded path can never re-introduce the 404s either
US_CITY_MARKET_SQL_NODATE = """
    SELECT m.market_slug AS slug
      FROM discovered_facilities f
      JOIN market_power_scores m
        ON m.market_slug = LOWER(REPLACE(TRIM(f.city),' ','-'))
     WHERE f.city IS NOT NULL AND TRIM(f.city) <> ''
       AND COALESCE(f.is_duplicate, 0) = 0
       AND f.country IN ('US','USA','United States')
     GROUP BY m.market_slug
    HAVING COUNT(*) >= 3
"""
POCKET_LIST_CEILING = 500


def us_city_market_rows(conn):
    """[(market_slug, lastmod-or-None)] for every DB-backed US city market
    page. Dated query first; on a schema error roll back and run the
    date-less twin (never a dead section)."""
    cur = conn.cursor()
    try:
        cur.execute(US_CITY_MARKET_SQL)
        return [(r[0], r[1]) for r in (cur.fetchall() or [])]
    except Exception:
        try: conn.rollback()
        except Exception: pass
        cur.execute(US_CITY_MARKET_SQL_NODATE)
        return [(r[0], None) for r in (cur.fetchall() or [])]


def listable_market_slug(slug, seen):
    """The slug if /markets/<slug> is a real, indexable, not-yet-listed page,
    else None. Mutates `seen`. Skips: junk ('', '-x', 'x-', no alnum),
    period slugs ('st.-louis' 301s to 'st-louis' — r-period-slug 2026-07-06),
    every MARKETS_CANONICAL_REDIRECT key ('ashburn' 301s to
    'northern-virginia' — r-portland-canon 2026-08-02), and duplicates."""
    s = (slug or '').strip()
    if (len(s) < 3 or s.startswith('-') or s.endswith('-')
            or '.' in s
            or not any(ch.isalnum() for ch in s)
            or s in MARKETS_CANONICAL_REDIRECT
            or s in seen):
        return None
    seen.add(s)
    return s


def _slug_title(slug):
    return _SLUG_TO_MARKET_NAME.get(slug) or slug.replace('-', ' ').title()


def sitemapped_market_slugs(conn=None) -> list:
    """Every /markets/<slug> slug sitemap-markets.xml publishes.

    ★ THE INVARIANT (r-latam-rotation, 2026-09-04): this set must be a SUBSET
    of what cron_rotate can target. It was not. The sitemap emits
    CURATED_MARKET_SLUGS unconditionally, the rotation selects from
    market_power_scores, and four curated metros — bogota, mexico-city,
    santiago, sao-paulo — have no mps row at all (measured 2026-09-03:
    /dcpi/<slug> 404 for all four, 200 for every other curated metro). They
    could never enter the rotation, so their brief could never be written, on
    any cadence, forever. This is the *checker reads a subset of what it
    publishes* class, pointed the other way: the GENERATOR reached a subset of
    what the sitemap published.

    Built from the same three pieces the sitemap and the /markets hub are
    built from — CURATED_MARKET_SLUGS, us_city_market_rows, and
    listable_market_slug — so it cannot drift into a different answer than the
    page inventory. The US arm is fail-soft on its own: a DB blip degrades to
    the curated list rather than emptying the rotation's floor.

    Deliberately NOT the pockets section. /pockets/<slug> is a different page
    family with a different backing route (routes/pockets.py); the invariant is
    about the deep-dive generator and the pages IT backs.
    """
    seen = set(CURATED_MARKET_SLUGS)
    out = list(CURATED_MARKET_SLUGS)
    _own = conn is None
    try:
        c = conn if conn is not None else _conn()
        if c is not None:
            try:
                for slug, _lm in us_city_market_rows(c):
                    s = listable_market_slug(slug, seen)
                    if s:
                        out.append(s)
            finally:
                if _own:
                    try: c.close()
                    except Exception: pass
    except Exception as e:
        logger.warning("sitemapped market slugs: US city arm unavailable: %s", e)
    return out


def markets_hub_inventory():
    """{'metros': [(slug, name)], 'us_markets': [(slug, name)],
        'pockets': [(slug, name, state)]} — the pages sitemap-markets.xml
    lists, from the same queries and the same filters. Each section is
    fail-soft on its own so a DB blip degrades to the curated list."""
    seen = set(CURATED_MARKET_SLUGS)
    metros = [(s, _slug_title(s)) for s in CURATED_MARKET_SLUGS]
    us = []
    try:
        c = _conn()
        if c is not None:
            try:
                for slug, _lm in us_city_market_rows(c):
                    s = listable_market_slug(slug, seen)
                    if s:
                        us.append((s, _slug_title(s)))
            finally:
                try: c.close()
                except Exception: pass
    except Exception as e:
        logger.warning("markets hub: US city market list unavailable: %s", e)
    us.sort()
    pockets = []
    try:
        from routes.pockets import _fetch_pockets
        for p in _fetch_pockets(limit_hint=POCKET_LIST_CEILING):
            slug = p.get("market_slug")
            if not slug or "." in slug:
                continue
            pockets.append((slug, p.get("market_name") or _slug_title(slug),
                            p.get("state") or ""))
    except Exception as e:
        logger.warning("markets hub: pockets list unavailable: %s", e)
    return {"metros": metros, "us_markets": us, "pockets": pockets}


def read_deep_dive(slug: str) -> dict | None:
    # r-portland-canon (2026-08-02): serve the flagship row for either slug
    # form, so /api/v1/markets/portland-or/deep-dive and facility-page splices
    # keyed on the DCPI slug keep resolving after the row moved to 'portland'.
    slug = MARKETS_DEEP_DIVE_PAGE_CANON.get(
        (slug or "").lower().strip(), (slug or "").lower().strip())
    # RENDER-PERF: cross-worker Redis layer in front of the DB (survives a
    # gunicorn recycle). On any miss/error this returns None and we fall through
    # to the unchanged live query below.
    _cached = _redis_get_deep_dive(slug)
    if _cached is not None:
        return _cached

    c = _conn()
    if c is None: return None
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT market_slug, market_name, narrative_md, key_stats,
                       word_count, generated_at, model_used
                  FROM market_deep_dives
                 WHERE market_slug = %s
            """, (slug,))
            r = cur.fetchone()
            if not r: return None
            r = dict(r)
            # Write-through so the next worker/replica skips this DB round-trip.
            _redis_set_deep_dive(slug, r)
            return r
    finally:
        try: c.close()
        except Exception: pass


_US_STATE_ABBREVS = (
    "al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms "
    "mo mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv "
    "wi wy dc"
).split()
_US_STATE_SUFFIXES = (
    "alabama alaska arizona arkansas california colorado connecticut delaware "
    "florida georgia hawaii idaho illinois indiana iowa kansas kentucky "
    "louisiana maine maryland massachusetts michigan minnesota mississippi "
    "missouri montana nebraska nevada new-hampshire new-jersey new-mexico "
    "new-york north-carolina north-dakota ohio oklahoma oregon pennsylvania "
    "rhode-island south-carolina south-dakota tennessee texas utah vermont "
    "virginia washington west-virginia wisconsin wyoming district-of-columbia"
).split()


def _market_slug_without_state(slug_norm):
    """`dallas-texas` -> `dallas`, but ONLY when the shorter slug is a market
    that actually has facilities. Returns None otherwise.

    ★ Guarded two ways so this can never invent a redirect:
      1. the suffix must be a real US state token, not any trailing word
         (`kansas-city` must NOT become `kansas`... which is why the check is
         on the REMAINDER being non-empty AND the full slug ending in
         '-<state>', and why the target is then verified against the DB);
      2. the target must return facilities. No rows -> no redirect -> 404.
    """
    if not slug_norm or "-" not in slug_norm:
        return None
    # ★★ 2026-07-28 second measurement: the sitemap emitted the ABBREVIATED
    # form — miami-fl, goodyear-az, tacoma-wa, charlotte-nc — and the first
    # version of this resolver only stripped FULL state names, so every one of
    # those still 404'd. Sampled 14 sitemap market URLs: 12 were 404s of this
    # exact shape. Abbreviations are checked first because they are the shape
    # Google actually has indexed.
    for st in _US_STATE_ABBREVS:
        if not slug_norm.endswith("-" + st):
            continue
        base = slug_norm[: -(len(st) + 1)]
        if not base:
            return None
        hit = _market_exists(base)
        if hit:
            return base
        return None
    for st in _US_STATE_SUFFIXES:
        if not slug_norm.endswith("-" + st):
            continue
        base = slug_norm[: -(len(st) + 1)]
        if not base:
            return None
        return base if _market_exists(base) else None
    return None


def _market_exists(slug):
    """True only when the market page will actually serve. DB down -> False,
    so an outage degrades to 404 rather than to a guessed redirect."""
    try:
        c = _conn()
        if c is None:
            return False
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM market_power_scores WHERE market_slug = %s "
                    "LIMIT 1", (slug,))
                return bool(cur.fetchone())
        finally:
            try: c.close()
            except Exception: pass
    except Exception:
        return False


def _markets_404_response():
    """Honest 404 for a market with zero facilities, WITH onward links.

    Kept local (not imported from seo_pages) so this module has no new import
    edge; the body mirrors seo_pages._error_page's shape.
    """
    from flask import Response as _Resp
    body = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>DC Hub \u2014 Market not found</title>"
            "<meta name=\"robots\" content=\"noindex,follow\">"
            "<link rel=\"canonical\" href=\"https://dchub.cloud/markets/directory\">"
            "</head><body><h1>Market not found</h1>"
            "<p>That market has no data centers in DC Hub yet.</p>"
            "<p><a href=\"/markets/directory\">Browse all markets</a> \u00b7 "
            "<a href=\"/facilities\">Browse all facilities</a> \u00b7 "
            "<a href=\"/\">DC Hub home</a></p></body></html>")
    return _Resp(body, status=404, mimetype="text/html")


# ── r-entity-json (2026-09-03) — the machine-readable twin ───────────────────
# An agent that crawls /markets/<slug> had no way to get the same facts AS DATA.
# There was no .json twin, no content negotiation, and the API path is a
# different shape it cannot guess from the page URL it already holds.
#
# /markets/<slug>.json did not 404 — it 301'd, which is worse, because it looks
# like it works. market_short_html normalises with slug_norm.replace(".", ""),
# so the request landed on /markets/<slug>json and redirected away.
#
# Werkzeug ranks this rule above "/markets/<slug>" because it carries more
# static text, so the suffix binds here and never reaches that normaliser.
# Pinned by a test — if the ordering ever flips, the twin silently becomes a
# redirect again.
def _twin_facts_without_a_brief(slug: str):
    """(display_name, stats) for a market with no deep-dive row.

    stats is None ONLY for a slug that is not a market at all — that stays a
    404, so this cannot re-open the soft-404 hole r-soft404 closed. A curated,
    sitemapped metro resolves even when the fleet join finds nothing: its page
    serves 200, and a twin that 404s for a page that 200s is the drift the
    entity work exists to remove. It then carries an EMPTY variableMeasured
    rather than an invented number.
    """
    name = _slug_title(slug)
    facts = None
    try:
        c = _conn()
        if c is not None:
            try:
                with c.cursor() as cur:
                    facts = measured_market_facts(cur, name, slug=slug)
            finally:
                try: c.close()
                except Exception: pass
    except Exception:
        facts = None
    if facts:
        return name, facts
    if slug in CURATED_MARKET_SLUGS:
        return name, {}
    return name, None


@market_deep_dive_bp.route("/markets/<slug>.json", methods=["GET"])
def market_entity_json(slug):
    """The market as schema.org Dataset JSON-LD — no auth, no key, plain GET.

    Same builder as the page's embedded block, so the two cannot drift into
    different answers for one market.
    """
    _slug = (slug or "").lower().strip()
    _canon = MARKETS_CANONICAL_REDIRECT.get(_slug, _slug)
    r = read_deep_dive(_slug)
    if r:
        _name  = r.get("market_name") or _slug
        # r-brief-live-score (2026-09-06): the SAME overlay the HTML page uses.
        # If only one of the two went live, /markets/<slug> and
        # /markets/<slug>.json would publish different DCPI scores for one
        # market — a fresh split between two surfaces of the same page, which
        # is the defect this whole family exists to close.
        _stats, _, _ = read_live_stats(_slug, r.get("key_stats") or {})
        _as_of = (r["generated_at"].isoformat()
                  if r.get("generated_at") else None)
    else:
        # r-latam-twin (2026-09-03): a missing NARRATIVE is not a missing
        # market. This 404'd for every sitemapped market whose brief had not
        # been generated — measured live: bogota / mexico-city / santiago /
        # sao-paulo were HTML 200 + .json 404, all four carrying real tracked
        # inventory. They are unreachable by cron_rotate (it targets
        # market_power_scores rows and they have none), so "wait for the
        # nightly" is not a fix; the twin must answer from the measurement.
        # `as_of` stays None on this path ON PURPOSE — market_entity omits
        # dateModified rather than stamping today onto an undated reading.
        _name, _stats = _twin_facts_without_a_brief(_canon)
        _as_of = None
        if _stats is None:
            return jsonify(
                error="unknown_market", slug=slug,
                hint=("No market by that slug. GET /api/v1/markets for the list, or "
                      "call rank_markets — both publish resolvable slugs.")), 404
    body = market_entity(
        _slug, _name, _stats,
        canonical_slug=_canon,
        as_of=_as_of)
    resp = jsonify(body)
    # application/ld+json is the honest type for this payload; agents and
    # validators both accept it, and it tells a crawler the body is structured
    # data rather than an arbitrary API response.
    resp.headers["Content-Type"] = "application/ld+json"
    resp.headers["Cache-Control"] = "public, max-age=900"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    # Point at the HTML twin so a crawler can reconcile the two representations.
    resp.headers["Link"] = f'<{ENTITY_SITE}/markets/{_canon}>; rel="canonical"'
    return resp, 200


@market_deep_dive_bp.route("/api/v1/markets/<slug>/deep-dive", methods=["GET"])
def deep_dive_json(slug):
    r = read_deep_dive(slug)
    if not r:
        return jsonify(error="not_yet_generated",
                       hint="POST /api/v1/markets/<slug>/regenerate to seed"), 404
    resp = jsonify({
        "slug":         r["market_slug"],
        "name":         r["market_name"],
        "narrative_md": r["narrative_md"],
        "key_stats":    r["key_stats"],
        "word_count":   r["word_count"],
        "generated_at": r["generated_at"].isoformat() if r["generated_at"] else None,
        "model":        r["model_used"],
    })
    resp.headers["Cache-Control"] = "public, max-age=900"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@market_deep_dive_bp.route("/api/v1/markets/<slug>/regenerate", methods=["POST"])
def regenerate(slug):
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    return jsonify(generate_for_market(slug)), 200


@market_deep_dive_bp.route("/api/v1/markets/deep-dive/cron", methods=["POST"])
def cron_rotate():
    """Daily cron — picks the 5 stalest markets and regenerates them.
    Over time covers all top 100 markets, every market refreshed
    monthly. Caps Claude API spend automatically."""
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    try: n = max(1, min(15, int(request.args.get("count") or 5)))
    except (ValueError, TypeError): n = 5
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    targets = []
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            # Top markets by DCPI score that are stalest in deep_dives
            # (NULL generated_at sorts first via LEFT JOIN).
            # market_power_scores has no `score` column — the composite is
            # derived at read time (same drift _gather_market_facts documents);
            # rank on writer-guaranteed excess_power_score instead.
            #
            # r-portland-canon (2026-08-02), two rotation fixes:
            # 1. FLAGSHIP METROS: 'northern-virginia' and 'silicon-valley'
            #    are curated + sitemapped /markets pages whose mps rows were
            #    deliberately retired (published=false, r-twin-unpublish —
            #    DCPI canon moved to city vocab). published=true alone made
            #    their briefs unreachable by this rotation FOREVER — refresh
            #    was manual per-slug regen only. Whitelist exactly the
            #    sitemapped metro-canon slugs; do NOT widen to all of
            #    REDUNDANT_TWIN_SLUGS (the others' /markets pages 301 to the
            #    city form, which is already in rotation via published=true).
            # 2. STALENESS must be measured against the PAGE row the
            #    generation actually writes (MARKETS_DEEP_DIVE_PAGE_CANON):
            #    joining mps 'portland-or' to mdd 'portland-or' would read
            #    NULL forever once the brief lives under 'portland', pinning
            #    the slot at the head of NULLS FIRST every day and starving
            #    the rest of the rotation (the guard-starvation class).
            if MARKETS_DEEP_DIVE_PAGE_CANON:
                _canon_case = "CASE " + " ".join(
                    ["WHEN mps.market_slug = %s THEN %s"]
                    * len(MARKETS_DEEP_DIVE_PAGE_CANON)
                ) + " ELSE mps.market_slug END"
                _canon_params = tuple(
                    p for pair in sorted(MARKETS_DEEP_DIVE_PAGE_CANON.items())
                    for p in pair)
            else:
                # CASE with zero WHEN arms is invalid SQL — plain join key.
                _canon_case = "mps.market_slug"
                _canon_params = ()
            # 3. THE ROTATION'S FLOOR IS WHAT THE SITEMAP PUBLISHES
            #    (r-latam-rotation, 2026-09-04). Every arm above still selects
            #    from market_power_scores, so a page the sitemap publishes
            #    whose market has NO mps row was unreachable by this query on
            #    every cadence — not stale, unreachable. Measured 2026-09-03:
            #    bogota, mexico-city, santiago and sao-paulo, all four curated
            #    and sitemapped, all four carrying real tracked inventory (40 /
            #    31 / 102 / 55 facilities), none with a DCPI row. The
            #    published=true arm is also narrower than the sitemap's own US
            #    city query, which joins mps WITHOUT a published predicate — so
            #    an unpublished-but-sitemapped city market had the same hole.
            #    UNION the sitemap's slug list in, deduped against the scored
            #    arm so a slug that has an mps row keeps its excess_power_score
            #    for the tie-break and is never selected twice.
            #
            #    Widening this is only SAFE because generate_for_market now
            #    resolves these markets (_unscored_market_facts) instead of
            #    failing `market_not_found` and writing nothing — that failure
            #    mode is what pins a target at the head of `generated_at NULLS
            #    FIRST` forever, and it is why this widening was deliberately
            #    NOT done when the .json twin was fixed.
            _sitemapped = sitemapped_market_slugs(c)
            cur.execute(f"""
                WITH scored AS (
                    -- r-market-canon-split (2026-09-05): the `OR market_slug =
                    -- ANY(flagship metro whitelist)` arm is gone. It existed
                    -- to re-admit ('northern-virginia', 'silicon-valley') --
                    -- rows that are unpublished precisely BECAUSE they are
                    -- retired twins -- so their sitemapped /markets pages kept
                    -- getting briefs written. Those pages are now 301s to the
                    -- canonical slug, which is published and therefore already
                    -- in this arm, so the whitelist would only re-authorise
                    -- writing a brief for a URL nothing serves. Sitemapped
                    -- markets with NO mps row at all (bogota, mexico-city,
                    -- santiago, sao-paulo) still enter via the UNION ALL below,
                    -- which is the arm r-latam-rotation added for them.
                    SELECT DISTINCT ON (market_slug)
                           market_slug, excess_power_score, computed_at
                      FROM market_power_scores
                     WHERE published = true
                     ORDER BY market_slug, computed_at DESC
                ), mps AS (
                    SELECT market_slug,
                           excess_power_score::double precision AS excess_power_score
                      FROM scored
                    UNION ALL
                    SELECT s, NULL::double precision
                      FROM UNNEST(%s::text[]) AS s
                     WHERE s NOT IN (SELECT market_slug FROM scored)
                )
                SELECT mps.market_slug
                  FROM mps
                  LEFT JOIN market_deep_dives mdd
                    ON mdd.market_slug = {_canon_case}
                 ORDER BY mdd.generated_at NULLS FIRST, mps.excess_power_score DESC NULLS LAST
                 LIMIT %s
            """, (_sitemapped,) + _canon_params + (n,))
            targets = [r[0] for r in cur.fetchall()]
    finally:
        try: c.close()
        except Exception: pass

    results = []
    for slug in targets:
        results.append(generate_for_market(slug))
    generated = sum(1 for r in results if r.get("ok"))
    # Stamp the run watermark BEFORE returning, on every outcome including a
    # run that generated nothing. See the CRON RUN WATERMARK block below for
    # why MAX(generated_at) is not usable for this.
    _record_cron_run(targets, generated)
    return jsonify(generated_count=generated,
                   results=results,
                   ran_at=datetime.datetime.utcnow().isoformat() + "Z"), 200


# ════════════════════════════════════════════════════════════════════
#  CRON RUN WATERMARK (2026-08-19)
#
#  /api/v1/markets/deep-dive/cron is in main.py's _WORKER_PROXY_POST_PATHS but
#  NOT _WORKER_PROXY_SYNC_PATHS, so web relays it to dchub-worker on a 15s read
#  budget and answers 202 once the rotation outlives it — which it does, this
#  being up to 15 sequential Claude calls. A 202 carries no generated_count, so
#  facility-snapshot-daily.yml has to OBSERVE the run landing instead.
#
#  MAX(generated_at) over market_deep_dives is the obvious candidate and is
#  wrong: generate_for_market() writes NOTHING on market_not_found, nothing on
#  an _ask_claude_to_write error (no ANTHROPIC_API_KEY, Anthropic 5xx), and its
#  brief-guard seed is INSERT ... ON CONFLICT DO NOTHING, so a guarded market
#  that already has a placeholder row leaves generated_at untouched. A rotation
#  whose five targets are all guarded is a completed run that moves that
#  watermark not at all — polling it would fail a healthy cron, which is the
#  same false-red #2929 removed from brain-autonomy.
#
#  So this stamps a RUN watermark unconditionally, in brain_state — the shared
#  (state_key, state_value JSONB, updated_at) table routes/
#  brain_data_growth_radar.py and autonomous_brain.py already use. It is a
#  generic key/value state store, not a brain-semantics one; a whole new table
#  for a single timestamp is not worth the DDL.
#
#  DB-backed is the load-bearing part. brain_autonomy_loop._LAST_TICK is a
#  module global (`gunicorn --workers 1`, one copy per service), the ticks run
#  on the worker, and /api/v1/brain/autonomy/status served web's never-written
#  copy until #2929 added it to the proxy allowlist. A row in brain_state is
#  read identically by web and worker, so /deep-dive/status below needs NO
#  allowlist entry and cannot drift that way.
#
#  The timestamp lives INSIDE the JSONB value rather than being read off
#  updated_at: brain_state has two idempotent CREATE TABLE definitions in this
#  repo that disagree on that column (TIMESTAMPTZ vs TIMESTAMP), and whichever
#  ran first is what production has.
# ════════════════════════════════════════════════════════════════════
_CRON_STATE_KEY = "market_deep_dive_last_cron"


def _record_cron_run(targets, generated: int) -> None:
    """Stamp the last-cron-run watermark. Best-effort; NEVER raises.

    Called for every outcome, including a rotation that regenerated nothing —
    the point of the watermark is that the RUN happened, not that it wrote.
    """
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    payload = {
        "last_cron_run": stamp,
        "targets": len(targets or []),
        "generated_count": int(generated or 0),
    }
    c = _conn()
    if c is None:
        return
    try:
        import json as _j
        with c.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS brain_state (
                       id BIGSERIAL PRIMARY KEY,
                       state_key TEXT NOT NULL UNIQUE,
                       state_value JSONB NOT NULL,
                       updated_at TIMESTAMPTZ DEFAULT NOW())"""
            )
            cur.execute(
                """INSERT INTO brain_state (state_key, state_value, updated_at)
                   VALUES (%s, %s, NOW())
                   ON CONFLICT (state_key)
                   DO UPDATE SET state_value = EXCLUDED.state_value,
                                 updated_at = NOW()""",
                (_CRON_STATE_KEY, _j.dumps(payload)),
            )
    except Exception:
        try: c.rollback()
        except Exception: pass
    finally:
        try: c.close()
        except Exception: pass


def _last_cron_run() -> dict:
    """The watermark, or {} when never stamped / unreadable.

    Fail-CLOSED for the caller: an unreadable watermark returns {}, the poller
    sees no advance and reports the run unobserved rather than inventing a
    completion.
    """
    c = _conn()
    if c is None:
        return {}
    try:
        import json as _j
        with c.cursor() as cur:
            cur.execute("SELECT state_value FROM brain_state WHERE state_key=%s",
                        (_CRON_STATE_KEY,))
            row = cur.fetchone()
        val = (row[0] if row else None) or {}
        if isinstance(val, str):
            val = _j.loads(val or "{}")
        return val if isinstance(val, dict) else {}
    except Exception:
        try: c.rollback()
        except Exception: pass
        return {}
    finally:
        try: c.close()
        except Exception: pass


@market_deep_dive_bp.route("/api/v1/markets/deep-dive/status", methods=["GET"])
def cron_status():
    """The rotation WATERMARK — what facility-snapshot-daily.yml polls when its
    POST is answered with a relayed 202 (delegated to dchub-worker, still
    running).

    last_cron_run is stamped at the END of EVERY rotation, including one that
    regenerated nothing: it says the run HAPPENED. latest_generated_at is
    reported alongside it for legibility only — it is NOT the completion
    signal, because a rotation whose targets are all brief-guarded writes no
    row at all (see the block above).

    Reads the DB, so web and dchub-worker return the same value and this path
    deliberately needs no worker-proxy allowlist entry.

    Gated with internal_auth.require_internal_or_admin rather than the
    `if _ADMIN_KEY and provided != _ADMIN_KEY` shape used by cron_rotate above:
    that form skips auth ENTIRELY when the env var is unset, which is exactly
    what a misconfigured process looks like. tests/test_admin_gate_fail_closed
    .py::test_no_new_self_disabling_gates refuses new instances of it, and it
    caught this endpoint when it was first written that way."""
    from internal_auth import require_internal_or_admin
    if not require_internal_or_admin(request):
        return jsonify(error="unauthorized"), 401
    st = _last_cron_run()
    latest = None
    total = None
    c = _conn()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("SELECT MAX(generated_at), COUNT(*) "
                            "FROM market_deep_dives")
                row = cur.fetchone() or (None, None)
                latest = row[0].isoformat() if row[0] else None
                total = int(row[1] or 0)
        except Exception:
            try: c.rollback()
            except Exception: pass
        finally:
            try: c.close()
            except Exception: pass
    return jsonify(ok=True,
                   last_cron_run=st.get("last_cron_run"),
                   targets=st.get("targets"),
                   generated_count=st.get("generated_count"),
                   latest_generated_at=latest,
                   deep_dive_rows=total,
                   note="last_cron_run is stamped at the END of EVERY "
                        "rotation, including one that regenerated nothing. "
                        "latest_generated_at is informational: a rotation "
                        "whose targets are all brief-guarded completes "
                        "without writing a row."), 200


@market_deep_dive_bp.route("/markets/<slug>/deep-dive", methods=["GET"])
def deep_dive_html(slug):
    # r-canon-unify (2026-07-04): /markets/<slug>/deep-dive is in NO sitemap, yet
    # the flagship /markets/<slug> pages used to canonicalize HERE — GSC flagged
    # 569 "alternate" + 150 "duplicate-canonical". Collapse to ONE indexable URL:
    # 301 the deep-dive path to the market page, which now serves the SAME
    # narrative body under a self-canonical https://dchub.cloud/markets/<slug>.
    # (Was: no-cache → 301 to /markets/<slug>; cache → render body here. Now the
    # body lives in _render_deep_dive_body() and this route is a pure 301.)
    slug = (slug or "").lower().strip()
    return redirect(f"/markets/{slug}", code=301)


def _render_neutral_market_page(slug: str, name: str):
    """Neutral 200 served when the stored brief fails _brief_guard_reason.

    No score, no counts, no verdict, no narrative — nothing a broken join
    could have laundered into confident prose. Deliberately NOT a fall-through
    to the market_short_html shell: for non-curated markets that path can end
    in a 404, and a market real enough to have a stored brief must keep a 200.
    Short cache so the page heals minutes after a clean regeneration lands.
    """
    body = f"""<!doctype html><html lang=en>
<head><meta charset=utf-8>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="market-slug" content="{slug}">
<title>{name} Data Center Market · DC Hub</title>
<meta name="description" content="{name} data center market — live facility, power and DCPI data from DC Hub.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/markets/{slug}">
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Instrument Sans',sans-serif;max-width:760px;margin:0 auto;padding:2.5rem 1.25rem;background:#0a0a0f;color:#d4d4d8;line-height:1.75}}
h1{{font-weight:700;letter-spacing:-.02em;margin:0 0 .25rem;font-size:2.1rem;color:#fafafa}}
.sub{{color:#71717a;margin:0 0 1.75rem;font-size:.82rem}}
p{{margin:1.1rem 0;font-size:1.06rem}}
a{{color:#818cf8}}
</style>
</head><body>
<h1>{name}</h1>
<p class="sub">Data Center Market · DC Hub</p>
<p>A refreshed market brief for {name} is being prepared from live DC Hub
data. Analysis is published here only once the market's facility and DCPI
data clear our quality checks — no verdicts get written from incomplete
joins.</p>
<p>Live surfaces for {name} in the meantime:
<a href="/dcpi/{slug}">DCPI score</a> ·
<a href="/facilities">facilities</a> ·
<a href="/markets/directory">all markets</a> ·
<a href="/market-intelligence">market intelligence</a></p>
<div style="margin:26px auto;padding:18px 22px;background:linear-gradient(135deg,rgba(99,102,241,0.14),rgba(168,85,247,0.07));border:1px solid rgba(99,102,241,0.3);border-radius:14px;text-align:center"><a href="/pricing?ref=market-deep-dive&tool={slug}" style="color:#a5b4fc;text-decoration:none;font-weight:600;font-size:15px">DC Hub &mdash; the live infrastructure data layer for AI agents and the people who build data centers. All 19,000+ facilities + live power, grid, fiber &amp; site-selection tools &mdash; <strong>from $49/mo &rarr;</strong></a></div>
<script src="/js/dchub-nav.js" defer></script>
</body></html>"""
    return Response(body, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=300",
                             "X-DC-Page-Source": "market-brief-guard"})


# ── r-crawl-citable (2026-09-03) ─────────────────────────────────────────────
# market_short_html() carries a Place + variableMeasured + Dataset block with a
# CC-BY license — and never reaches it for any market that HAS a deep-dive,
# because the handler returns _render_deep_dive_body() ~200 lines earlier. So
# the markets agents actually land on (Ashburn, Northern Virginia, Dallas …)
# shipped Article markup only. Verified live 2026-09-03 on /markets/ashburn:
#
#   JSON-LD @types  : Article, BreadcrumbList, ListItem, Organization, Place
#   variableMeasured: False    Dataset: False    license: False
#
# Article markup tells a crawler "someone's write-up". Dataset + Observation
# tells it "a citable measurement" — that is the difference between a page being
# read and a number being quoted. The MW was present only in prose.
#
# The basis travels WITH the number. util/facility_count_basis.capacity_basis
# exists because this market's MW reads 5,793 / 11,052 / 12,438 across three
# surfaces — all correct, all different populations. A crawler that takes the
# page figure and an API caller that takes the tool figure must both be able to
# see WHY they differ, or the difference reads as us contradicting ourselves.
# ═══════════════════════════════════════════════════════════════════════
# LIVE SCORE OVERLAY — r-brief-live-score (2026-09-06)
# ═══════════════════════════════════════════════════════════════════════
#
# /markets/<slug> renders a STORED brief. Its key_stats were measured when the
# narrative was written, and `dcpi_score` is the one figure in there that moves
# on a different clock: DCPI recomputes 4x/day, briefs rotate roughly monthly.
# So the page published a dated snapshot of a live number, and an agent citing
# it disagreed with /dcpi for the same market — measured live 2026-09-06,
# BEFORE the flagship set was regenerated:
#
#     /markets/santa-clara   DCPI score 42.4      /dcpi/santa-clara   40.8
#     /markets/los-angeles   DCPI score 44.3      /dcpi/los-angeles   40.6
#
# Regenerating fixed those two for a day. It is a treadmill, not a fix: the
# next recompute re-opens the gap on whichever briefs the rotation has not
# reached. The durable answer is to stop storing that particular number.
#
# ★ WHAT IS OVERLAID, AND WHAT DELIBERATELY IS NOT.
# Only `dcpi_score` and `verdict` — one indexed point-select. facility_count
# and total_mw stay from the snapshot: they cost a two-table union per render,
# and — the real reason — they are what the NARRATIVE talks about. Making them
# live would leave prose reading "303 tracked facilities" beside a tile reading
# something else, which is the same defect one layer in.
#
# ★ THE PROSE STILL CARRIES THE OLD SCORE, so the page SAYS SO. A brief written
# against 42.4 and topped with a live 40.8 is a contradiction unless the page
# discloses it; _live_score_note is that disclosure. Turning the gap into
# stated information is the only honest way to serve both numbers at once.
#
# Fail-soft everywhere: any error leaves the stored stats exactly as they are,
# because a DCPI blip must never blank a page that already has an answer.

#: Below this, the live and stored scores are the same number and there is
#: nothing to disclose. Both are rounded to 1dp before comparison.
_SCORE_DRIFT_EPSILON = 0.05


def live_dcpi_reading(cur, slug):
    """The PUBLISHED DCPI composite for `slug` right now, or None.

    Canonical slug + PUBLISHED_ONLY, the same two lines as _gather_market_facts
    and PR #3841's handle_poe_query — a retired twin's frozen row is exactly
    what this must not read, and it is the row an exact-slug match finds.

    Returns {"dcpi_score", "verdict", "computed_at"} or None. None means "could
    not read", never "no score": every caller falls back to the stored value.
    """
    _canon = canonical_slug(slug) or slug
    try:
        cur.execute(f"""
            SELECT constraint_score, excess_power_score, verdict,
                   time_to_power_months, computed_at
              FROM market_power_scores
             WHERE LOWER(market_slug) = LOWER(%s) AND {PUBLISHED_ONLY}
             LIMIT 1
        """, (_canon,))
        row = cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    constraint, excess, verdict, ttp, computed = row[0], row[1], row[2], row[3], row[4]
    if constraint is None or excess is None:
        # Both components must exist. derive_composite_score coerces None to 0,
        # which mints a plausible-looking score for a market with no data —
        # the same guard _gather_market_facts carries.
        return None
    try:
        from routes.dcpi import derive_composite_score
        score = derive_composite_score(excess, constraint, ttp, verdict)
    except Exception:
        return None
    return {
        "dcpi_score": score,
        "verdict": verdict,
        # r-markets-components (2026-09-06): the three components the composite
        # is BUILT from. This query already selected them — it derived the
        # score and threw them away — so /markets published a single composite
        # while /pockets/<slug> published all four with their bases. Same row,
        # same read, same vintage: carrying them costs nothing and closes the
        # gap between two surfaces of one market.
        "excess_power_score": float(excess),
        "constraint_score": float(constraint),
        "time_to_power_months": float(ttp) if ttp is not None else None,
        "computed_at": computed.isoformat() if hasattr(computed, "isoformat")
                       else (str(computed) if computed else None),
    }


def overlay_live_score(stats, live):
    """`stats` with the live score and verdict in place of the stored ones.

    Pure, and separate from the read so a test can drive every branch without a
    database. Returns (stats, stored_score) — `stored_score` is what the
    narrative was written against, which the caller needs to decide whether
    there is drift worth disclosing.
    """
    stats = dict(stats or {})
    stored = stats.get("dcpi_score")
    if not live:
        return stats, stored
    stats["dcpi_score"] = live["dcpi_score"]
    if live.get("verdict"):
        stats["verdict"] = live["verdict"]
    # r-markets-components (2026-09-06): the components travel with the score
    # under util.market_entity's own key names. Deliberately NOT merged with
    # the stored `excess`/`constraint` that key_stats already carries under
    # different names: those are the snapshot's integers, frozen when the
    # narrative was written, and publishing them beside a live composite would
    # re-create the mixed-vintage problem this whole overlay removed. When
    # there is no live reading they are simply absent — market_entity omits a
    # measure it does not have, which is the honest answer, and never the
    # stale one dressed as current.
    for _k in ("excess_power_score", "constraint_score",
               "time_to_power_months"):
        if live.get(_k) is not None:
            stats[_k] = live[_k]
    # The score's OWN vintage, read by util.market_entity so the ld+json
    # measure states when it was observed rather than inheriting the
    # narrative's date. Both surfaces get it from this one assignment.
    if live.get("computed_at"):
        stats["dcpi_as_of"] = live["computed_at"]
    return stats, stored


def _drifted(stored, live_score):
    """True when the two round to different published numbers."""
    if stored is None or live_score is None:
        return False
    try:
        return abs(round(float(stored), 1) - round(float(live_score), 1)) \
            > _SCORE_DRIFT_EPSILON
    except (TypeError, ValueError):
        return False


def live_score_note(slug, stored, live, gen_at):
    """The sentence a page must carry when its prose predates its headline.

    '' when the two agree, so a page with nothing to disclose says nothing.
    """
    if not live or not _drifted(stored, live.get("dcpi_score")):
        return ""
    when = f" on {gen_at}" if gen_at and gen_at != "?" else ""
    return (f"This analysis was written{when}, when the DC Hub Power Index for "
            f"this market read {float(stored):.1f}. The index is recomputed "
            f"through the day and reads {float(live['dcpi_score']):.1f} now — "
            f"the figure above is the live one, and the narrative below "
            f"describes the market as it stood when it was written.")


def read_live_stats(slug, stats):
    """(stats-with-live-score, stored_score, live) — opens its own connection.

    Wraps the two pure functions above with the one impure step, so both the
    HTML page and the .json twin get the same overlay from one call and cannot
    drift into publishing different scores for one market.
    """
    live = None
    try:
        c = _conn()
        if c is not None:
            try:
                with c.cursor() as cur:
                    live = live_dcpi_reading(cur, slug)
            finally:
                try: c.close()
                except Exception: pass
    except Exception as e:
        logger.warning("live DCPI overlay unavailable for %s: %s", slug, e)
        live = None
    merged, stored = overlay_live_score(stats, live)
    return merged, stored, live


def _market_dataset_ld(slug: str, name: str, stats: dict, gen_at) -> str:
    """The citable half of the deep-dive page's structured data.

    r-one-builder (2026-09-03): this used to build its own Dataset node. That
    made TWO market builders — this one and util.market_entity, which serves
    /markets/<slug>.json — and they drifted immediately: the page attached a
    count basis to `Facilities` while the twin published a bare integer, and the
    twin carried a DCPI measure the page did not. Two structured-data answers
    for one market is the defect, not a detail. #3757 claimed one builder; this
    makes it true.

    Fail-soft: an empty JSON object rather than a broken page render.
    """
    try:
        _canon = MARKETS_CANONICAL_REDIRECT.get(slug, slug)
        return json.dumps(
            market_entity(slug, name, stats, canonical_slug=_canon,
                          as_of=str(gen_at or "") or None),
            ensure_ascii=False)
    except Exception:   # pragma: no cover - structured data never breaks a page
        return "{}"


def _render_deep_dive_body(slug):
    """Render the cached deep-dive narrative as the /markets/<slug> page body.
    Returns a Flask Response (self-canonical to /markets/<slug>), or None when
    no deep-dive is cached (caller then renders the minimal SEO shell)."""
    slug = (slug or "").lower().strip()
    r = read_deep_dive(slug)
    if not r:
        return None
    # r-none-score (2026-08-01): a stored brief whose key_stats say
    # facilities=0 or score=None is a data bug wearing prose — this page
    # published "DCPI score None/100" sitewide and "avoid entering Northern
    # Virginia" off a dead join. Serve neutral copy instead; the page heals
    # to a full brief when a post-guard regeneration lands.
    _guard = _brief_guard_reason(r.get("key_stats") or {})
    if _guard:
        # r-latam-rotation (2026-09-04): the neutral page carries NO measured
        # facts — that is the point of it, for a market whose numbers are
        # suspect. But a guard-neutral PLACEHOLDER is not a suspect brief; it
        # is the marker that no brief was written, and this route runs BEFORE
        # market_short_html's shell (which r-latam-twin just taught to render
        # the fleet reading). Serving the neutral page off a placeholder would
        # therefore have replaced bogota / mexico-city / santiago / sao-paulo's
        # measured facility counts with a blank page the moment the rotation
        # first reached them — a regression caused by fixing the rotation.
        #
        # Curated slugs only: market_short_html is guaranteed to render one
        # (that is what _known_curated gates), so this hands off to a page that
        # exists. For anything else the neutral 200 stays the answer, because
        # the shell path can end in a 404 and a market real enough to hold a
        # stored brief must keep its 200.
        if (r.get("model_used") == "guard-neutral"
                and slug in set(CURATED_MARKET_SLUGS)):
            return None
        return _render_neutral_market_page(slug, r["market_name"])
    try:
        from routes.surface_brain import auto_log
        auto_log("market_deep_dive", "view", target=slug)
    except Exception: pass

    # Convert simple markdown paragraphs to <p>
    paragraphs = "".join(
        f"<p>{p.strip()}</p>" for p in (r["narrative_md"] or "").split("\n\n") if p.strip()
    )
    name = r["market_name"]
    gen_at = r["generated_at"].strftime("%Y-%m-%d") if r["generated_at"] else "?"
    # r-brief-live-score (2026-09-06): the headline DCPI figure comes from the
    # published row at render time, not from the snapshot the narrative was
    # written against. Everything downstream — tiles, meta description, both
    # ld+json blocks — reads `stats`, so overlaying here is the single point
    # that keeps them agreeing with each other AND with /dcpi.
    stats, _stored_score, _live = read_live_stats(slug, r.get("key_stats") or {})
    _score_note = live_score_note(slug, _stored_score, _live, gen_at)
    # Two vintages in one sentence, so the sentence says which is which. A
    # bare "Updated <brief date>" beside a live score dates the wrong figure.
    _live_suffix = (f" (live {str((_live or {}).get('computed_at'))[:10]})"
                    if (_live or {}).get("computed_at") else "")

    # P1-1 (2026-08-28): Product 1's sponsored module on the market template.
    # '' whenever no sponsor is active, which is its state until a row is
    # activated. This route is the one that serves /markets/<slug>
    # (x-dc-hub-served-by: railway-primary).
    try:
        from routes.sponsor_render import sponsor_module_html
        sponsor_html = sponsor_module_html("market_module")
    except Exception as _sp_err:
        logger.warning(f"market_deep_dive sponsor module failed: {_sp_err}")
        sponsor_html = ""

    html = f"""<!doctype html><html lang=en>
<head><meta charset=utf-8>
<meta name="market-slug" content="{slug}">
<title>{name} Market Deep-Dive · DC Hub</title>
<meta name="description" content="{name} data center market analysis. DCPI score {stats.get('dcpi_score','?')}/100{_live_suffix}; {stats.get('facility_count',0)} facilities, {stats.get('total_mw',0):,.0f} MW as of {gen_at}.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://dchub.cloud/markets/{slug}">
<meta property="og:title" content="{name} Market Deep-Dive · DC Hub">
<script type="application/ld+json">{{
 "@context":"https://schema.org","@type":"Article",
 "headline":"{name} Data Center Market Deep-Dive",
 "datePublished":"{r['generated_at'].isoformat() if r['generated_at'] else ''}",
 "author":{{"@type":"Organization","name":"DC Hub","url":"https://dchub.cloud"}},
 "publisher":{{"@type":"Organization","name":"DC Hub","url":"https://dchub.cloud"}},
 "url":"https://dchub.cloud/markets/{slug}",
 "wordCount":{r.get('word_count') or 0},
 "about":{{"@type":"Place","name":"{name}"}},
 "description":"Live data-center market analysis. DCPI score {stats.get('dcpi_score','?')}/100."
}}</script>
<script type="application/ld+json">{_market_dataset_ld(slug, name, stats, gen_at)}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0a0a0f;--surf:#131319;--b:rgba(255,255,255,0.08);--tx:#fafafa;--mut:#a1a1aa;--dim:#71717a;--ind:#818cf8;--grad:linear-gradient(135deg,#6366f1,#a855f7)}}
*{{box-sizing:border-box}}
body{{font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,sans-serif;max-width:760px;margin:0 auto;padding:2.5rem 1.25rem;background:var(--bg);color:#d4d4d8;line-height:1.75;-webkit-font-smoothing:antialiased}}
h1{{font-weight:700;letter-spacing:-.02em;margin:0 0 .25rem;font-size:2.1rem;color:var(--tx)}}
.sub{{color:var(--dim);margin:0 0 1.75rem;font-size:.82rem;font-family:'JetBrains Mono',monospace}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.75rem;margin:1rem 0 2.25rem}}
.drift{{color:var(--mut);font-size:.9rem;background:var(--surf);border:1px solid var(--b);border-left:3px solid var(--ind);border-radius:8px;padding:.85rem 1.05rem;margin:1.25rem 0}}
.stat{{background:var(--surf);border:1px solid var(--b);border-radius:12px;padding:.9rem 1.1rem;font-size:.68rem;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;font-family:'JetBrains Mono',monospace}}
.stat b{{display:block;font-size:1.5rem;color:var(--tx);margin-top:.35rem;letter-spacing:0;text-transform:none}}
p{{margin:1.1rem 0;font-size:1.06rem}}
a{{color:var(--ind)}}
.foot{{color:var(--dim);font-size:.82rem;margin-top:2.5rem;padding-top:1.25rem;border-top:1px solid var(--b);font-family:'JetBrains Mono',monospace}}
.foot a{{color:var(--ind);text-decoration:none}}
.foot a:hover{{text-decoration:underline}}
</style>
</head><body>
<h1>{name}</h1>
<p class="sub">Data Center Market Deep-Dive · {r.get('word_count') or 0} words · generated {gen_at} by Claude haiku from live DC Hub data{' · DCPI live as of ' + _live['computed_at'][:10] if (_live or {}).get('computed_at') else ''}</p>
<div class="stats">
 <div class="stat">DCPI Score<b>{stats.get('dcpi_score','?')}/100</b></div>
 <div class="stat">Facilities<b>{stats.get('facility_count',0):,}</b></div>
 <div class="stat">Total MW<b>{stats.get('total_mw',0):,.0f}</b></div>
 <div class="stat">Verdict<b>{stats.get('verdict','?')}</b></div>
</div>
{('<p class="drift">' + _score_note + '</p>') if _score_note else ''}
{paragraphs}
{sponsor_html}
<div style="max-width:1080px;margin:26px auto;padding:18px 22px;background:linear-gradient(135deg,rgba(99,102,241,0.14),rgba(168,85,247,0.07));border:1px solid rgba(99,102,241,0.3);border-radius:14px;text-align:center"><a href="/pricing?ref=market-deep-dive&tool={slug}" style="color:#a5b4fc;text-decoration:none;font-weight:600;font-size:15px">DC Hub &mdash; the live infrastructure data layer for AI agents and the people who build data centers. All 19,000+ facilities + live power, grid, fiber &amp; site-selection tools &mdash; <strong>from $49/mo &rarr;</strong></a></div>
<p class="foot">JSON: <a href="/api/v1/markets/{slug}/deep-dive" rel="nofollow">/api/v1/markets/{slug}/deep-dive</a> · DCPI: <a href="/dcpi">/dcpi</a> · Operators: <a href="/operators">/operators</a> · Updated nightly</p>
<script src="/js/dchub-nav.js" defer></script>
</body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=1800"})


# Phase ZZZZ-shortform (2026-05-18): top-level /markets/<slug> shell.
# Dashboard QA expects /markets/chicago, /markets/dallas, /markets/northern-virginia
# to return 200. Either renders the cached deep-dive narrative if present,
# or a minimal SEO shell built from MARKET_DATA so the route is always 200.
_SLUG_TO_MARKET_NAME = {
    # r-market-canon-split (2026-09-05): the CANONICAL slug of each twin pair
    # needs its own display name. 'ashburn', 'santa-clara' and 'portland-or'
    # were absent, so once /markets stopped 301ing to the metro form they
    # would have fallen through to the title-cased slug — and, worse, to
    # `_known_curated = False`, which sends a curated market into the
    # soft-404 guard. The alias keys stay: they still name the market for
    # anything that resolves a name before the redirect runs.
    "ashburn":                "Northern Virginia",
    "northern-virginia":      "Northern Virginia",
    "nova":                   "Northern Virginia",
    "santa-clara":            "Silicon Valley",
    "portland-or":            "Portland-Hillsboro",
    "dallas":                 "Dallas-Fort Worth",
    "dallas-fort-worth":      "Dallas-Fort Worth",
    "dfw":                    "Dallas-Fort Worth",
    "chicago":                "Chicago",
    "silicon-valley":         "Silicon Valley",
    "phoenix":                "Phoenix",
    "atlanta":                "Atlanta",
    "new-york":               "New York Metro",
    "new-york-metro":         "New York Metro",
    "nyc":                    "New York Metro",
    "portland":               "Portland-Hillsboro",
    "portland-hillsboro":     "Portland-Hillsboro",
    "los-angeles":            "Los Angeles",
    "la":                     "Los Angeles",
    "seattle":                "Seattle",
    "denver":                 "Denver",
    "miami":                  "Miami",
    "boston":                 "Boston",
    "minneapolis":            "Minneapolis",
    "houston":                "Houston",
    "austin":                 "Austin",
    "salt-lake-city":         "Salt Lake City",
    "columbus":               "Columbus",
    "kansas-city":            "Kansas City",
    "toronto":                "Toronto",
    "montreal":               "Montreal",
    "london":                 "London",
    "frankfurt":              "Frankfurt",
    "amsterdam":              "Amsterdam",
    "paris":                  "Paris",
    "dublin":                 "Dublin",
    "madrid":                 "Madrid",
    "milan":                  "Milan",
    "stockholm":              "Stockholm",
    "warsaw":                 "Warsaw",
    "singapore":              "Singapore",
    "tokyo":                  "Tokyo",
    "sydney":                 "Sydney",
    "hong-kong":              "Hong Kong",
    "seoul":                  "Seoul",
    "mumbai":                 "Mumbai",
    "sao-paulo":              "São Paulo",
}


_HUB_CACHE = {"html": None, "at": 0.0}
_HUB_TTL = 3600


def _render_markets_hub(inv):
    """Self-canonical hub linking every /markets/<slug> and /pockets/<slug>."""
    from facilities_hub import _shell, _ld_breadcrumb, _ld_itemlist, _e, SITE
    metros = inv.get("metros") or []
    us = inv.get("us_markets") or []
    pockets = inv.get("pockets") or []
    n_markets = len(metros) + len(us)

    def _ul(items):
        return '<ul class="grid">' + "".join(items) + "</ul>"

    metro_li = [f'<li><a href="{SITE}/markets/{_e(sl)}">{_e(nm)}</a></li>'
                for sl, nm in metros]
    us_li = [f'<li><a href="{SITE}/markets/{_e(sl)}">{_e(nm)}</a></li>'
             for sl, nm in us]
    pk_li = [f'<li><a href="{SITE}/pockets/{_e(sl)}">{_e(nm)}</a>'
             + (f' <span class="muted">{_e(st)}</span>' if st else '')
             + '</li>'
             for sl, nm, st in pockets]
    body = (
        "<h1>Data Center Markets</h1>"
        f'<p class="muted">{n_markets} market pages and {len(pockets)} '
        "power-pocket rankings, one page per metro. Each market page reads "
        "from the same live tables as the API: facility count and MW in the "
        "market, the DC Hub Power Index (DCPI) score, interconnection-queue "
        "position in its ISO, and the deals that touched it. A "
        "<em>power pocket</em> page ranks the same metro on excess grid "
        "capacity, constraint and time-to-power, so you can read a market "
        "two ways: what is built, and what can still be powered. "
        f'For the cross-market table see <a href="{SITE}/market-intelligence">'
        "Market Intelligence</a>; for the score leaderboard see "
        f'<a href="{SITE}/dcpi">the Power Index</a>; for every metro grouped '
        f'by state see the <a href="{SITE}/markets/directory">markets '
        "directory</a>.</p>"
        f"<h2>Global metros ({len(metros)})</h2>" + _ul(metro_li)
        + f"<h2>US city markets ({len(us)})</h2>"
        + (_ul(us_li) if us_li else '<p class="muted">List temporarily '
           "unavailable — the curated metros above still resolve.</p>")
        + f"<h2>Power pockets ({len(pockets)})</h2>"
        + (_ul(pk_li) if pk_li else '<p class="muted">Pocket rankings are '
           "recomputed daily; check back shortly.</p>")
    )
    entries = ([(nm, f"{SITE}/markets/{sl}") for sl, nm in metros + us]
               + [(nm, f"{SITE}/pockets/{sl}") for sl, nm, _st in pockets])
    ld = [_ld_breadcrumb([("Home", SITE + "/"), ("Markets", SITE + "/markets")]),
          _ld_itemlist("Data center markets", entries)]
    title = f"Data Center Markets — {n_markets} metros, {len(pockets)} power pockets | DC Hub"
    desc = (f"Every data center market DC Hub tracks: {n_markets} metro pages "
            f"with live facility counts, MW, DCPI score and queue position, "
            f"plus {len(pockets)} power-pocket rankings.")
    crumbs = f'<a href="{SITE}/">Home</a> › Markets'
    return _shell(title, desc, f"{SITE}/markets", crumbs, body, ld)


@market_deep_dive_bp.route("/markets", methods=["GET"])
@market_deep_dive_bp.route("/markets/", methods=["GET"])
def markets_hub_page():
    """seo F6 (2026-09-02): the live, SELF-canonical /markets hub. Replaces
    main.py's mapping of /markets → static/market-intelligence.html (whose
    canonical → /market-intelligence). See markets_hub_inventory."""
    import time
    now = time.time()
    html = _HUB_CACHE.get("html")
    if not html or (now - float(_HUB_CACHE.get("at") or 0)) >= _HUB_TTL:
        html = _render_markets_hub(markets_hub_inventory())
        _HUB_CACHE["html"], _HUB_CACHE["at"] = html, now
    resp = Response(html, status=200, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    resp.headers["X-DC-Hub-Source"] = "markets-hub"
    return resp


@market_deep_dive_bp.route("/markets/<slug>", methods=["GET"])
def market_short_html(slug):
    """Top-level /markets/<slug>. Prefers the cached deep-dive narrative;
    falls back to a minimal SEO shell so QA never sees a 404."""
    slug_norm = (slug or "").lower().strip()

    # r-period-slug (2026-07-06): a malformed market_slug with a period
    # (e.g. 'st.-louis' — a soft-404 duplicate of the canonical 'st-louis')
    # must never be its own indexable URL. A period is never valid in a
    # canonical market slug, so strip it and 301 to the '-'-normalized slug,
    # consolidating the duplicate onto the real page. Aligns with the
    # _CANONICAL_REDIRECT canonical-unify pattern just below.
    #
    # r-suffix-301 (2026-09-03): split a file extension off FIRST. The blanket
    # replace(".", "") also ate the period in '<slug>.JSON' / '<slug>.xml' and
    # 301'd to '<slug>json' / '<slug>xml', which 404. A redirect into a 404 is
    # worse than a 404 — it reads as "this moved, follow me". The lowercase
    # '.json' is bound by the market_entity_json twin above and never gets
    # here; every other case does. Rebuild the target WITH the suffix so a
    # '.JSON' request lands on the twin that serves it.
    _norm_slug, _suffix = normalize_periods(slug_norm)
    if _norm_slug and _suffix:
        # Compare against the RAW path, not the lower-cased slug: '.JSON' is
        # exactly the case that escaped the twin's rule (Werkzeug matches its
        # static text case-sensitively), so it must be sent on to the
        # lowercase URL that does serve it rather than answered in place.
        _target = (f"/markets/{_norm_slug}.json" if _suffix == "json"
                   else f"/markets/{_norm_slug}")
        if _target != f"/markets/{slug}":
            return redirect(_target, code=301)
        # Only reachable if Werkzeug ever ranks "/markets/<slug>" above
        # "/markets/<slug>.json". Serve the twin rather than render HTML under
        # a .json URL — the failure the twin's own comment warns of.
        return market_entity_json(_norm_slug)
    if _norm_slug and _norm_slug != slug_norm:
        return redirect(f"/markets/{_norm_slug}", code=301)

    # r43-H (2026-05-28): canonical-consolidation 301s. Several alias slugs
    # point at the same physical market (Ashburn IS the core of Northern
    # Virginia, NoVA == Northern Virginia, DFW == Dallas). Rendering both
    # is duplicate content; redirect the alias to the canonical market page.
    # r-portland-canon (2026-08-02): map lifted to module level
    # (MARKETS_CANONICAL_REDIRECT) and extended with the retired twin slugs
    # (cheyenne-wy, columbus-oh, the-dalles-or, washington, dallas-fort-worth,
    # portland-or/-hillsboro) so each market has ONE indexable /markets URL;
    # the sitemap builder skips exactly these keys.
    if slug_norm in MARKETS_CANONICAL_REDIRECT:
        return redirect(
            f"/markets/{MARKETS_CANONICAL_REDIRECT[slug_norm]}", code=301)

    # If a deep-dive is cached, serve its narrative AS the /markets/<slug> page
    # (self-canonical to /markets/<slug> — r-canon-unify 2026-07-04). The old
    # code returned deep_dive_html(), which now 301s, so render the body directly.
    _dd_body = _render_deep_dive_body(slug_norm)
    if _dd_body is not None:
        return _dd_body

    # Fallback: render minimal shell from MARKET_DATA
    name = _SLUG_TO_MARKET_NAME.get(slug_norm)
    _known_curated = name is not None
    if not name:
        # Try title-cased fallback: "chicago" → "Chicago"
        name = slug_norm.replace("-", " ").title()

    md = {}
    try:
        from market_intelligence_api import MARKET_DATA
        md = MARKET_DATA.get(name, {}) or {}
    except Exception:
        pass

    # SOFT-404 GUARD (r-soft404 2026-07-04): this route used to return a 200 SEO
    # shell for ANY string, which GSC flagged as 222 Soft-404s. If the slug is a
    # curated market, is in MARKET_ALIASES, has a cached deep-dive (checked
    # above), or has ≥1 facility in the DB, it's real → render. Otherwise it's a
    # junk URL → return a real 404 so Google drops it, not an empty 200 shell.
    _in_aliases = False
    try:
        from main import MARKET_ALIASES
        _in_aliases = bool(MARKET_ALIASES.get(slug_norm.replace('-', ' ')))
    except Exception:
        pass
    if not _known_curated and not md and not _in_aliases:
        _fac_ct = 0
        try:
            _c404 = _conn()
            if _c404 is not None:
                try:
                    with _c404.cursor() as _cur404:
                        _cur404.execute(
                            "SELECT COUNT(*) FROM discovered_facilities WHERE market = %s",
                            (name,))
                        _row404 = _cur404.fetchone()
                        _fac_ct = int(_row404[0]) if _row404 and _row404[0] else 0
                finally:
                    try: _c404.close()
                    except Exception: pass
        except Exception:
            _fac_ct = 0
        if _fac_ct == 0:
            # r-markets-404 (2026-07-15): these junk /markets/<slug> URLs are
            # almost all INTERNAL links — facility pages emit href="/markets/<city-state>"
            # for cities with no curated/DB-backed market (Dubai, Ottawa, Casper),
            # so Googlebot crawled ~380 of them into the "Not found (404)" bucket.
            # The prior r-soft404 guard returned a bare 404; instead 302 to the
            # crawlable markets hub so link equity flows and the already-indexed
            # 404s resolve. Still NOT a soft-404 (a real redirect to a real 200
            # page, not an empty 200 shell). 302 (not 301) + short cache so a
            # market self-heals to its own 200 page the moment a facility backfills.
            # ★★ REVERSED 2026-07-28 — see routes/seo_pages.py:_markets_dir_redirect.
            # The comment above claims this is "Still NOT a soft-404". GSC
            # measured 299 Soft 404s. Redirecting every empty market to one hub
            # IS the soft-404 pattern; _fac_ct == 0 means there is nothing to
            # serve, so say 404 and link onward instead of faking a 200.
            # ★★ RESOLVE BEFORE YOU 404 (2026-07-28, second pass).
            # Facility pages link to /markets/{city}-{state} (seo_pages builds
            # `_slug(city + '-' + state)`), but market slugs are METRO/CITY keyed:
            # `dallas` and `dallas-fort-worth` are real, `dallas-texas` is not.
            # The 2026-07-15 code papered over that with a 302 to the hub (a soft
            # 404); my first pass turned it into an honest 404 — which was honest
            # and still wrong, because the site was then hard-404ing its OWN
            # internal links. Try the city-only form first and 301 to it.
            _alt = _market_slug_without_state(slug_norm)
            if _alt:
                _r = redirect("/markets/" + _alt, code=301)
                _r.headers["Cache-Control"] = "public, max-age=3600"
                _r.headers["X-DC-Page-Source"] = "market-state-suffix-301"
                return _r
            _r = _markets_404_response()
            _r.headers["Cache-Control"] = "public, max-age=3600"
            _r.headers["X-DC-Page-Source"] = "market-deepdive-404"
            return _r

    if not md:
        # Still return 200 — the market exists in our universe even if we
        # haven't yet pulled rich data. Better than 404 for SEO + QA.
        md = {"region": "—", "inventory_mw": "—", "vacancy_rate": "—",
              "avg_asking_rate": "—", "num_facilities": "—"}
        # r43-H (2026-05-27): an all-"—" page looks broken (user reported
        # /markets/reno). MARKET_DATA only covers curated major markets, but
        # the live discovered_facilities table has real counts for smaller
        # ones like Reno. Pull facility count + capacity from the DB using
        # the SAME MARKET_ALIASES + US country-guard the authoritative
        # /api/v1/markets/<m> endpoint uses (the country guard is what keeps
        # 'Reno' from matching 'Grenoble' etc. in the count). Research-only
        # metrics (vacancy, asking rate, YoY) stay "—" when we genuinely
        # lack CBRE/JLL coverage for the market.
        try:
            from main import MARKET_ALIASES, RAILWAY_EXCLUSION
            cities = MARKET_ALIASES.get(slug_norm.replace('-', ' '))
            if cities:
                c2 = _conn()
                if c2 is not None:
                    try:
                        conds, params = [], []
                        for city in cities:
                            if len(city) == 2 and city.isupper():
                                conds.append("state = %s"); params.append(city)
                            else:
                                # exact match (+ "City, ST" prefix) — NOT
                                # substring, to avoid reno→Grenoble bleed.
                                conds.append("(LOWER(city) = LOWER(%s) OR city ILIKE %s)")
                                params.append(city); params.append(f"{city},%")
                        where = " OR ".join(conds)
                        guard = ("AND (country='US' OR country='USA' "
                                 "OR country IS NULL OR country='')")
                        with c2.cursor() as cur:
                            # NOTE: the `status ILIKE %s` placeholder sits in
                            # the SELECT (textually BEFORE the WHERE city/state
                            # placeholders), so psycopg2 binds it FIRST — the
                            # construction pattern must lead the params list,
                            # not trail it. (RAILWAY_EXCLUSION uses %% literals,
                            # no placeholders.)
                            cur.execute(f"""
                                SELECT COUNT(*),
                                       COALESCE(SUM(power_mw),0),
                                       COALESCE(SUM(CASE WHEN status ILIKE %s
                                                         THEN power_mw ELSE 0 END),0)
                                  FROM discovered_facilities
                                 WHERE ({where}) {guard} {RAILWAY_EXCLUSION}
                            """, ['%construction%'] + params)
                            row = cur.fetchone()
                        if row and row[0]:
                            md["num_facilities"] = int(row[0])
                            if row[1]:
                                md["inventory_mw"] = round(float(row[1]))
                            if row[2]:
                                md["under_construction_mw"] = round(float(row[2]))
                            md["region"] = "North America"
                    finally:
                        try: c2.close()
                        except Exception: pass
        except Exception:
            pass

        # r-latam-twin (2026-09-03): the backfill above is US-ONLY — it needs a
        # main.MARKET_ALIASES entry AND filters country IN ('US','USA',NULL,''),
        # so every non-US market fell through with num_facilities '—' while the
        # note below told the reader "facility counts and capacity above are
        # live from our infrastructure database". Measured live 2026-09-03:
        # /markets/bogota, /mexico-city, /santiago and /sao-paulo each rendered
        # a single bare em dash over 40 / 31 / 102 / 55 tracked facilities.
        # Counts via the SAME union the brief and the .json twin use, so the
        # three surfaces cannot disagree about one market.
        if not _has_metric(md.get('num_facilities')):
            try:
                _c3 = _conn()
                if _c3 is not None:
                    _mf = None
                    try:
                        with _c3.cursor() as _cur3:
                            _mf = measured_market_facts(_cur3, name,
                                                        slug=slug_norm)
                    finally:
                        try: _c3.close()
                        except Exception: pass
                    if _mf:
                        md['num_facilities'] = _mf['facility_count']
                        if _mf.get('total_mw'):
                            md['inventory_mw'] = round(_mf['total_mw'])
            except Exception:
                pass

    highlights_html = ""
    hl = md.get("highlights") or []
    if hl:
        items = "".join(f"<li>{h}</li>" for h in hl)
        highlights_html = f"<h2>Highlights</h2><ul>{items}</ul>"

    providers_html = ""
    tp = md.get("top_providers") or []
    if tp:
        providers_html = (f"<h2>Top Providers</h2><p>{', '.join(tp)}</p>")

    desc = (f"{name} data center market. {md.get('num_facilities','?')} facilities, "
            f"{md.get('inventory_mw','?')} MW inventory, "
            f"{md.get('vacancy_rate','?')}% vacancy, "
            f"${md.get('avg_asking_rate','?')}/kW/mo asking. Live DC Hub data.")

    # r43-H (2026-05-28): only render metric tiles that have REAL values.
    # Showing bare "—" for research-only metrics (vacancy/asking/YoY) that
    # we don't track for smaller markets made the page look broken (the
    # repeated "Reno is white/no data" reports). Now we show the metrics we
    # have (clean), and a one-line note for the research metrics we don't.
    _has = _has_metric
    _metric_defs = [
        ('Inventory',     md.get('inventory_mw'),         '', ' MW'),
        ('Facilities',    md.get('num_facilities'),       '', ''),
        ('Under Constr.', md.get('under_construction_mw'),'', ' MW'),
        ('Vacancy',       md.get('vacancy_rate'),         '', '%'),
        ('Asking Rate',   md.get('avg_asking_rate'),      '$', '/kW/mo'),
        ('YoY Price',     md.get('yoy_price_change'),     '', '%'),
    ]
    _tiles = [f'<div class="stat">{lab}<b>{pre}{val}{suf}</b></div>'
              for lab, val, pre, suf in _metric_defs if _has(val)]
    _missing = [lab for lab, val, pre, suf in _metric_defs if not _has(val)]
    stats_html = "\n".join(_tiles) or '<div class="stat">Facilities<b>—</b></div>'
    # r-latam-twin (2026-09-03): this note VOUCHED for numbers the page was not
    # showing. It fired on any missing metric — including the facility count it
    # claims is "above" — so a shell whose only tile was a bare em dash still
    # asserted "facility counts and capacity above are live from our
    # infrastructure database". Only claim the fleet clause when a fleet tile
    # actually rendered; the research clause stands on its own.
    _RESEARCH_LABELS = ('Vacancy', 'Asking Rate', 'YoY Price')
    # r-note-precision (2026-09-04): the clause must name the tiles that
    # ACTUALLY rendered. Saying "facility counts and capacity above" on a page
    # showing only a count is the same overclaim, one notch smaller —
    # /markets/bogota shipped exactly that (Facilities 53, no Inventory tile,
    # capacity vouched for anyway) because the gate accepted ANY fleet tile.
    _shown = {lab for lab, val, pre, suf in _metric_defs if _has(val)}
    _has_ct, _has_mw = 'Facilities' in _shown, 'Inventory' in _shown
    if _has_ct and _has_mw:
        _fleet_clause = (' — facility counts and capacity above are live from '
                         'our infrastructure database.')
    elif _has_ct:
        _fleet_clause = (' — the facility count above is live from our '
                         'infrastructure database.')
    elif _has_mw:
        _fleet_clause = (' — the capacity above is live from our '
                         'infrastructure database.')
    else:
        _fleet_clause = '.'
    note_html = ""
    if [lab for lab in _missing if lab in _RESEARCH_LABELS]:
        note_html = (f'<p class="note">Pricing, vacancy & YoY for {name} aren\'t in our '
                     f'CBRE/JLL research coverage yet' + _fleet_clause + '</p>')

    # seo: enrich the market JSON-LD with the load-bearing numbers (total MW,
    # facility count, under-construction MW) as schema.org PropertyValues so AI
    # Overviews + agents ingest the figures as structured data, not just prose.
    # Built via json.dumps (guaranteed-valid + correctly escaped) rather than
    # hand-written JSON in the f-string. No new DB query — reuses `md` already
    # in scope, so the hot /markets/<slug> path stays fast. Only emits metrics
    # that pass _has(); geo/coords live on the richer /dcpi/<slug> Dataset page.
    import json as _ldjson
    _measured = []
    if _has(md.get('inventory_mw')):
        _measured.append({"@type": "PropertyValue", "name": "Total Capacity",
                          "value": md.get('inventory_mw'), "unitText": "MW"})
    if _has(md.get('num_facilities')):
        _measured.append({"@type": "PropertyValue", "name": "Facilities",
                          "value": md.get('num_facilities')})
    if _has(md.get('under_construction_mw')):
        _measured.append({"@type": "PropertyValue", "name": "Under Construction",
                          "value": md.get('under_construction_mw'), "unitText": "MW"})
    _market_ld = {
        "@context": "https://schema.org",
        "@type": "Place",
        "name": name,
        "description": desc,
        "url": f"https://dchub.cloud/markets/{slug_norm}",
        "additionalType": "https://schema.org/Place",
        # r78: Google validates EVERY Dataset entity it finds — nested ones
        # included — and requires name+description. These description-less
        # isPartOf stubs were the GSC "16 invalid Datasets".
        "isPartOf": {"@type": "Dataset", "name": "DC Hub Data Center Market Intelligence",
                     "url": "https://dchub.cloud/markets",
                     "description": ("Live supply, vacancy, pricing, and pipeline "
                                     "intelligence for global data center markets, "
                                     "updated daily by DC Hub."), "license": "https://creativecommons.org/licenses/by/4.0/", "creator": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"}},
    }
    if _has(md.get('region')):
        _market_ld["containedInPlace"] = {"@type": "Place", "name": md.get('region')}
    if _measured:
        _market_ld["additionalProperty"] = _measured
    _market_jsonld = _ldjson.dumps(_market_ld, ensure_ascii=False)

    # r80 SEO INTERNAL-LINK MESH: the 21k /facilities/<slug> pages were a
    # crawl ISLAND — every hub page that should funnel link-equity into them
    # rendered ZERO facility links, so Google left ~21k pages unindexed.
    # Emit the top facilities IN this market as real <a href="/facilities/…">
    # links using the populated `market` column (96.7% of rows) + the
    # canonical slug builder so the URLs match the sitemap exactly.
    fac_links_html = ""
    try:
        from routes.facility_profile_page import _fac_slug as _fslug, _esc as _fesc
        _c3 = _conn()
        if _c3 is not None:
            try:
                with _c3.cursor() as _fcur:
                    _fcur.execute("""
                        SELECT id, name, provider, power_mw
                          FROM discovered_facilities
                         WHERE market = %s AND name IS NOT NULL AND name <> ''
                           AND (country IN ('US','USA','United States')
                                OR country IS NULL OR country='')
                         ORDER BY power_mw DESC NULLS LAST
                         LIMIT 50
                    """, (name,))
                    _frows = _fcur.fetchall() or []
            finally:
                try: _c3.close()
                except Exception: pass
            if _frows:
                _items = "".join(
                    f'<li><a href="/facilities/{_fslug(_rid, _rprov, _rname)}">{_fesc(_rname)}</a>'
                    f'{(" &middot; " + str(round(_rpow)) + " MW") if _rpow else ""}</li>'
                    for _rid, _rname, _rprov, _rpow in _frows)
                fac_links_html = (f'<h2>Data centers in {name}</h2>'
                                  f'<ul class="fac-list">{_items}</ul>')
    except Exception:
        pass

    html = f"""<!doctype html><html lang=en>
<head><meta charset=utf-8>
<meta name="market-slug" content="{slug_norm}">
<title>{name} Data Center Market · DC Hub</title>
<meta name="description" content="{desc}">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/markets/{slug_norm}">
<meta property="og:title" content="{name} Data Center Market · DC Hub">
<meta property="og:description" content="{desc}">
<script type="application/ld+json">{_market_jsonld}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0a0a0f;--surf:#131319;--b:rgba(255,255,255,0.08);--tx:#fafafa;--mut:#a1a1aa;--dim:#71717a;--ind:#818cf8;--grad:linear-gradient(135deg,#6366f1,#a855f7)}}
*{{box-sizing:border-box}}
body{{font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,sans-serif;max-width:760px;margin:0 auto;padding:2.5rem 1.25rem;background:var(--bg);color:#d4d4d8;line-height:1.75;-webkit-font-smoothing:antialiased}}
h1{{margin:0 0 .25rem;font-size:2.1rem;font-weight:700;letter-spacing:-.02em;color:var(--tx)}}
h2{{font-size:1.15rem;font-weight:600;color:var(--tx);margin:2rem 0 .5rem}}
.sub{{color:var(--dim);margin:0 0 1.75rem;font-size:.82rem;font-family:'JetBrains Mono',monospace}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.75rem;margin:1rem 0 2rem}}
.stat{{background:var(--surf);border:1px solid var(--b);border-radius:12px;padding:.9rem 1.1rem;font-size:.68rem;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;font-family:'JetBrains Mono',monospace}}
.stat b{{display:block;font-size:1.5rem;color:var(--tx);margin-top:.35rem;letter-spacing:0;text-transform:none}}
a{{color:var(--ind)}}
.foot{{color:var(--dim);font-size:.82rem;margin-top:2.5rem;padding-top:1.25rem;border-top:1px solid var(--b);font-family:'JetBrains Mono',monospace}}
.foot a{{color:var(--ind);text-decoration:none}}
.foot a:hover{{text-decoration:underline}}
.note{{color:var(--dim);font-size:.82rem;margin:-.25rem 0 1.5rem;line-height:1.55}}
ul{{padding-left:1.25rem}} li{{margin:.3rem 0}}
</style>
</head><body>
<h1>{name}</h1>
<p class="sub">Data Center Market · {md.get('region','—')}</p>
<p class="dc-maplink" style="margin:.4rem 0 .2rem"><a href="/map" style="color:#3b82f6;font-weight:600;text-decoration:none">📍 See {name} data centers on the live facility map →</a></p>
<div class="stats">
{stats_html}
</div>
{note_html}
{providers_html}
{highlights_html}
{fac_links_html}
<div style="max-width:1080px;margin:26px auto;padding:18px 22px;background:linear-gradient(135deg,rgba(99,102,241,0.14),rgba(168,85,247,0.07));border:1px solid rgba(99,102,241,0.3);border-radius:14px;text-align:center"><a href="/pricing?ref=market&tool={slug_norm}" style="color:#a5b4fc;text-decoration:none;font-weight:600;font-size:15px">DC Hub &mdash; the live infrastructure data layer for AI agents and the people who build data centers. All 19,000+ facilities + live power, grid, fiber &amp; site-selection tools &mdash; <strong>from $49/mo &rarr;</strong></a></div>
<p class="foot">JSON: <a href="/api/v1/markets/{name.replace(' ', '%20')}" rel="nofollow">/api/v1/markets/{name}</a> ·
All markets: <a href="/markets">/markets</a></p>
<script src="/js/dchub-nav.js" defer></script>
</body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=900"})
