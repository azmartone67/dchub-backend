"""facility_ner_noindex.py — de-index the news-NER facility pages already live.

PR #2490 (2026-08-09) closed the WRITE: news headlines and NER spans can no
longer be ingested as facilities. It did nothing about the pages already
published — 61 live, indexable /facilities/ URLs, 200 + robots="index, follow"
+ self-canonical + present in sitemap-facilities-1.xml:

    /facilities/copilot-07a85c97      <title>Copilot — US Data Center | DC Hub</title>
    /facilities/ferc-ferc-9e0a2b63    <title>FERC — US Data Center | DC Hub</title>
    … GitHub, Intel, Chevron, Waymo, CISA, NTIA, Alphabet, Cloudflare, SpaceX,
      Broadcom, Palantir, "Why OT Security Can", "Texas Batch Zero",
      "Data Center Space Odyssey"

WHY NEITHER SHIPPED PREDICATE REACHES THEM
------------------------------------------
`facility_name_sanity.headline_reject_reason` keys on the name's SHAPE, and
"Copilot" is indistinguishable from a real single-word operator by name alone.
Measured against the 62 slugs this module collects, it catches exactly ONE
("State Pauses Projects Over" → headline-verb:pauses). Widening it to reach
the other 61 was measured and rejected: the collateral is real facilities.

`facility_name_sanity.evidence_reject_reason` DOES separate them — and its
docstring says INGEST-ONLY in bold for precisely the reason that bites here.
Run UNSCOPED over `facilities` it matches 139 rows, and 45 of those are real
OpenStreetMap facilities that merely carry no city/coords in our copy:
'AiNET', 'CoreSite Reston Campus VA2', 'Equinix Secaucus NY6', 'QTS Suwanee'.
De-indexing those is exactly the collateral the slug regex was rejected for.

THE FIX: FENCE THE EVIDENCE TEST BY PROVENANCE
----------------------------------------------
It is safe when — and only when — it is scoped to the rows the NER promoter
wrote. Measured live 2026-08-09 (20,132 `facilities`, 25,024
`discovered_facilities`):

    discovered_facilities WHERE source='news_ner'          →  61 slugs
    facilities WHERE source='discovered_facilities_drain'
               AND <no evidence>                           →  61 slugs
    union                                                  →  62 slugs
    <no evidence> UNSCOPED over `facilities`               → 139 rows ← never

Both prongs are load-bearing; neither is a superset of the other:
  · 'home-rebusinessonline-…-06d30f34' is in the `facilities` prong ONLY. Its
    discovered twin is source='news_extraction' and, carrying no provider,
    hashes to a different slug entirely.
  · 'state-pauses-projects-over-…-03d74fcf' is in the news_ner prong ONLY —
    its `facilities` row has power_mw=50 and so passes the evidence test.
    (That is also the single row headline_reject_reason already caught.)

★ Verified zero collateral: not one of the 62 slugs is shared by any other row
  in either table, so nothing real loses its sitemap entry or its index
  directive. This is the check the 2026-07-28 `_dupe_slugs` correction had to
  learn the hard way — "this slug belongs to a junk row" is not the same claim
  as "no live facility serves this slug".

THE THIRD PRONG: source='news_pipeline' (2026-08-09, PR #2495)
--------------------------------------------------------------
#2490 and #2492 between them covered the NER promoter. They did NOT cover the
OLDER path — `news_facility_extractor`, which writes the article TITLE as the
facility name under source='news_pipeline'. Three of those were still live and
indexable:

    /facilities/how-wisconsin-companies-are-benefitting-from-data-center-
                boom-urban-milwaukee-0d159289
    /facilities/tech-giants-announce-7b-data-center-michigans-first-
                hyperscale-campus-bridge-michigan-aeb3a70d
    /facilities/2026-global-data-center-outlook-jll-ad87ddfe

They are a DIFFERENT root cause from #2492: not the NER promoter, but two gaps
in the name-shape predicate. `headline_reject_reason` catches 26 of the 29
zero-evidence news_pipeline rows and misses exactly these three, because
"announce" is not in _HEADLINE_VERBS (only "announces"/"announced"), and
"Urban Milwaukee" / "Bridge Michigan" / "JLL" are not in _PUBLICATION_WORDS.

★★ WHY THE EVIDENCE CONJUNCT IS LOAD-BEARING HERE AND NOT DECORATIVE
   Unlike source='news_ner' — where provenance ALONE is the whole signal,
   because an NER span is by construction a fragment cut out of an article —
   source='news_pipeline' is a MIXED population. Measured on the live replica
   2026-08-09:

       facilities WHERE source='news_pipeline'                 →  59 rows
         … AND <no evidence>   (headlines)                     →  29 rows
         … AND NOT <no evidence>  (REAL facilities)            →  30 rows

   Those 30 are real, well-evidenced announced facilities that this path did
   its job on — 'Stargate Abilene Phase 1' (Abilene, 1200 MW, 980k sqft),
   'Meta Beaver Dam WI', 'IREN Sweetwater 1', 'NTT Global Data Centers
   Frankfurt' (482 MW), 'Vantage Johor Malaysia'. A prong of
   `source='news_pipeline'` alone would de-index all thirty. So for THIS
   prong the evidence test is not a narrowing convenience, it is the entire
   discriminator, and dropping it is the same class of mistake as running the
   evidence test unscoped.

   Measured result of the prong as written: 29 slugs, all 29 present, zero
   overlap with the existing two prongs (62 → 91).

★ Verified zero collateral, the same check the other two prongs carry: every
  row sharing one of the 29 slugs — in EITHER table — is itself a news
  ingestion row (29 `facilities` + 22 `discovered_facilities`, all
  source='news_pipeline'). Not one real facility serves any of them.

★ NO discovered-side prong, deliberately. 34 `discovered_facilities`
  news_pipeline rows carry a slug this prong does not cover; 27 of them are
  the REAL facilities above and must not be touched, and the other 7
  ('Xcel Energy to power new Google data center in Minnesota - Xcel Energy
  Newsroom', …) have no `facilities` row at all, so they serve no profile
  page — and `headline_reject_reason` already covers them name-side. Adding
  a fourth prong would buy zero pages and put those 27 at risk.

★ The module keeps its `_ner_` name. Its remit is now "published
  news-derived facility pages", NER or otherwise; renaming would churn the
  imports in main.py, routes/facility_profile_page.py and the tests and buy
  nothing.

CONTRACT — the same one every guard before it carries
-----------------------------------------------------
Sitemap EMISSION and robots META only. Slugs are FROZEN (PR #2490,
tests/test_seo_index_hygiene.py). Nothing here renames, redirects or deletes,
and all 91 pages keep serving 200 at their existing URLs.

WHY A CACHED SLUG SET RATHER THAN A ROW-LEVEL TEST
--------------------------------------------------
The serve path (`facility_profile_page._fetch_facility_by_slug`) does not
select `sqft`, `lat`/`lon`, or — for the `facilities` table — `market`. It
therefore CANNOT evaluate the measured predicate on the row in hand; it would
evaluate a looser one, and a looser evidence test is how 'AiNET' gets
de-indexed. So the predicate runs in SQL, once, and callers consult the set.

★ The cache is refreshed ONLY when a caller hands in a cursor. This module
  never opens a connection and never imports main — nothing under tests/ may
  import main.py (CLAUDE.md), and `_render_profile` is behaviour-tested. With
  no cursor ever supplied the set stays empty and every caller degrades to
  exactly the behaviour it had before this module existed.

★ KNOWN, DELIBERATE GAP: an alias URL that resolves through
  `_fetch_facility_by_slug`'s hash8 fallback ('/facilities/<anything>-07a85c97')
  renders the same junk row under a slug that is not in this set, so it keeps
  robots="index, follow". Those URLs are in no sitemap and nothing links them,
  and the alternative — keying on the name — is the thing that cannot work
  here. Same limitation the OSM-junk guard has carried since 2026-08-01.
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

# The zero-evidence predicate, in SQL, EXACTLY as measured on 2026-08-09.
# ★ Do NOT add a column without re-measuring the UNSCOPED count: this predicate
#   is only ever correct in combination with the source filter beside it.
# ★ Deliberately omits evidence_reject_reason's `acreage` / `investment_usd`:
#   neither column exists on `facilities` (verified against the live schema),
#   and this SQL only ever runs against `facilities`.
# ★ No literal '%' anywhere — a lone '%' raises IndexError client-side in
#   psycopg2 before the server sees the statement.
NO_EVIDENCE_SQL = (
    "     COALESCE(city, '')      = ''"
    " AND COALESCE(address, '')   = ''"
    " AND COALESCE(market, '')    = ''"
    " AND latitude IS NULL AND longitude IS NULL"
    " AND lat      IS NULL AND lon       IS NULL"
    " AND COALESCE(power_mw, 0)   = 0"
    " AND COALESCE(sqft, 0)       = 0"
)

# ★★ EVERY prong MUST carry a `source =` filter. A prong without one is the
#    139-row unscoped query, and it de-indexes 45 real OpenStreetMap
#    facilities. tests/test_seo_index_hygiene.py pins this.
SUPPRESSION_QUERIES = (
    # The NER promoter's own rows. Provenance alone is the whole signal here —
    # source='news_ner' IS "an entity span cut out of an article".
    ("news_ner",
     "SELECT canonical_slug FROM discovered_facilities"
     " WHERE source = 'news_ner'"
     "   AND canonical_slug IS NOT NULL AND canonical_slug <> ''"),
    # Their drained twins in the curated table. The drain does not carry the
    # originating source through, so provenance narrows the population and the
    # evidence test picks the NER spans out of it.
    ("drain_no_evidence",
     "SELECT canonical_slug FROM facilities"
     " WHERE source = 'discovered_facilities_drain'"
     "   AND canonical_slug IS NOT NULL AND canonical_slug <> ''"
     "   AND (" + NO_EVIDENCE_SQL + ")"),
    # The OLDER news path — news_facility_extractor writes the article TITLE
    # as the facility name. ★★ The evidence conjunct is the whole
    # discriminator here, not a narrowing convenience: this source is a MIXED
    # population, 29 headlines beside 30 REAL facilities ('Stargate Abilene
    # Phase 1', 'NTT Global Data Centers Frankfurt'). Dropping it de-indexes
    # all thirty. See the docstring's third-prong section.
    ("news_pipeline_no_evidence",
     "SELECT canonical_slug FROM facilities"
     " WHERE source = 'news_pipeline'"
     "   AND canonical_slug IS NOT NULL AND canonical_slug <> ''"
     "   AND (" + NO_EVIDENCE_SQL + ")"),
)

CACHE_TTL_SECONDS = 3600
# After a TOTAL failure the cache keeps its old contents and its old age, so
# the next caller retries immediately. On the serve path that is once per
# facility-page view — two failing statements and two rollbacks each — exactly
# when the DB is already unwell. Back off instead.
FAILED_RETRY_SECONDS = 60

_lock = threading.Lock()
_cache = {'slugs': frozenset(), 'ts': 0.0, 'next_try': 0.0}


def _rollback(cursor):
    """Best-effort rollback of whatever connection `cursor` belongs to.

    psycopg2 poisons a connection after a failed statement — every later read
    on it returns nothing until a rollback, which is how an /agent/index went
    all-zero for months behind an HTTP 200. db_utils.PGCursorWrapper already
    rolls back internally and exposes no `.connection`, so this has to be
    defensive on both shapes rather than assume either.
    """
    for attr in ('connection', '_cur'):
        target = getattr(cursor, attr, None)
        conn = getattr(target, 'connection', None) if attr == '_cur' else target
        if conn is None:
            continue
        try:
            conn.rollback()
            return
        except Exception:
            continue


def load_suppressed_slugs(cursor):
    """Run every provenance prong on `cursor`; return (slugs, prongs_ok).

    Each prong is isolated: a missing table or column costs that prong only,
    never the other one and never the caller's transaction.
    """
    slugs = set()
    prongs_ok = 0
    for label, sql in SUPPRESSION_QUERIES:
        try:
            cursor.execute(sql)
            rows = cursor.fetchall() or []
            found = {r[0] for r in rows if r and r[0]}
            slugs |= found
            prongs_ok += 1
            logger.info("ner-noindex: prong %s → %d slugs", label, len(found))
        except Exception as exc:
            _rollback(cursor)
            logger.warning(
                "ner-noindex: prong %s unavailable (%s) — its pages stay "
                "indexed", label, exc)
    return slugs, prongs_ok


def refresh_suppressed_slugs(cursor, ttl=CACHE_TTL_SECONDS, force=False):
    """Repopulate the cache from `cursor` if it is older than `ttl`.

    Returns the current set either way, so a caller can use the return value
    directly. If EVERY prong failed the previous set is kept rather than
    overwritten with an empty one — a transient DB blip must not silently flip
    61 pages back to index,follow, and it is the sort of failure that reads as
    "the fix shipped inert" three weeks later in GSC.
    """
    now = time.time()
    with _lock:
        if not force:
            if _cache['ts'] and (now - _cache['ts']) < ttl:
                return _cache['slugs']
            if now < _cache['next_try']:
                return _cache['slugs']
    slugs, prongs_ok = load_suppressed_slugs(cursor)
    with _lock:
        if prongs_ok:
            _cache['slugs'] = frozenset(slugs)
            _cache['ts'] = now
            _cache['next_try'] = 0.0
        else:
            _cache['next_try'] = now + FAILED_RETRY_SECONDS
            logger.warning("ner-noindex: every prong failed — keeping the "
                           "previous %d-slug set, retrying in %ds",
                           len(_cache['slugs']), FAILED_RETRY_SECONDS)
        return _cache['slugs']


def suppressed_slugs():
    """The cached set. Never touches the DB — empty until someone refreshes."""
    with _lock:
        return _cache['slugs']


def is_suppressed_slug(slug):
    """True when this canonical slug is a published news-NER span."""
    if not slug:
        return False
    with _lock:
        return slug in _cache['slugs']


def reset_cache():
    """Test hook — drop the cache so a refresh is guaranteed to run."""
    with _lock:
        _cache['slugs'] = frozenset()
        _cache['ts'] = 0.0
        _cache['next_try'] = 0.0
