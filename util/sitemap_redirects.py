"""
util/sitemap_redirects.py — 2026-09-12. THE SITEMAP MUST NOT LIST A URL THAT
REDIRECTS.

WHAT WAS MEASURED (live, 2026-09-12)
------------------------------------
Three facility URLs confirmed persistent 301s at BOTH the Cloudflare edge and
the Railway origin, every one of them advertised in a sitemap shard:

    /facilities/flexential-dallas-8594e4e2
        -> /facilities/flexential-flexential-dallas-8594e4e2
    /facilities/airtrunk-india-92231957
        -> /facilities/airtrunk-airtrunk-india-92231957
    /facilities/us-signal-or01-bend-11bc45cd
        -> /facilities/us-signal-us-signal-or01-bend-11bc45cd
           (which then canonicalises onward to a third slug)

A 2,000-URL sample found 2, so ~19 site-wide. In two of the three cases BOTH
members are listed, in different shards.

★★★ THE CAUSE, and it is at the emitter. The frozen slug is
`<provider-slug>-<name-slug>-<hash8>` and hash8 is MD5(provider|name) — the
same for both spellings of a doubled provider prefix. main's emit loop composes
`build_canonical_slug(provider, name)` (the DEDUPED spelling) for a row whose
stored canonical_slug is NULL, but `_fetch_facility_by_slug` answers that URL
by falling through its exact-frozen-slug arms to the hash8 arms, landing on a
DIFFERENT row of the same identity whose frozen slug is the DOUBLED spelling —
and `_twin_redirect_target` case A then 301s to it. The 6,800 doubled frozen
slugs are LEFT ALONE ON PURPOSE (a stable ugly URL beats a pretty one that
moves; see routes/facility_slug_freeze), so the fix cannot be a slug change.

★★★ WHY THIS RESOLVES RATHER THAN PATTERN-MATCHES. The obvious cheap test —
"does another row's frozen slug share this hash8?" — cannot answer it. Whether
the URL redirects depends on WHICH row wins the hash8 arm (ORDER BY power_mw
DESC, id ASC): if the winner is the unfrozen row itself there is no case-A
redirect and the URL is fine. A hash8-collision heuristic would drop those too,
which is the 2026-07-28 "a drop-set cost 21 live pages their sitemap entry"
failure re-run. So this asks the page's OWN resolver.

★ facility_profile_page.served_slugs is that resolver, and it is already what
  every other list emitter uses — facilities_hub, routes/dcpi,
  market_deep_dive, mcp_tier1_tools, routes/indexnow, routes/seo_pages,
  carrier_facility_ingestion. The sitemap was the last emitter still publishing
  unresolved slugs. There is deliberately no second copy of the ladder here:
  tests/test_served_slugs_batch.py and tests/test_served_slugs_sql_parity.py
  pin that resolver against the page statement for statement, and a copy in
  this file would be outside both.

★ DROP, NOT SUBSTITUTE. Emitting the redirect TARGET instead would re-admit
  slugs the sitemap deliberately withheld — junk, contentless, NER, capacity-
  gated, or non-self-canonical — silently widening the artefact past four
  separate policies. The target is emitted by its own row whenever it qualifies
  (measured: 2 of the 3 confirmed pairs already list both members), and a
  redirecting sitemap entry is a GSC "Page with redirect" row, never an indexed
  page, so dropping it loses nothing that existed.

★ SITEMAP EMISSION ONLY — the contract every guard in the builder carries. No
  slug is rewritten, no page moves, and every one of these URLs keeps serving
  its 301 for anyone holding it.
"""
import logging
import os

logger = logging.getLogger(__name__)

#: Refuse a result that wants to drop more than max(this, 2%) of the list. The
#: measured rate is ~19 of ~19,000 (0.1%); 2% is 20x that and still nowhere
#: near a collapse. The absolute floor keeps a SMALL artefact (or a unit
#: fixture) from tripping a percentage that rounds to zero.
_REFUSE_FRACTION = 50          # i.e. len(slugs) // 50 == 2%
_REFUSE_FLOOR = 25


def _disabled() -> bool:
    return (os.environ.get('SITEMAP_REDIRECT_RESOLVE_DISABLE', '')
            .strip().lower() in ('1', 'true', 'yes', 'on'))


def redirecting_slug_set(conn, slugs) -> set:
    """Slugs among `slugs` that /facilities/<slug> does NOT serve at themselves.

    `conn` is a read connection the caller owns and keeps; it is lent to
    served_slugs and never closed here.

    Returns an EMPTY set on failure, on the kill switch, or on an implausibly
    large result. The caller's contract is "empty means emit everything", i.e.
    exactly the artefact built before this guard existed — a resolver that has
    gone wrong must never be able to shrink the sitemap.

    ★ r-slugblockers (2026-09-19), finding 14. EVERY one of those exits used to
    be SILENT, and all four return the same value the happy path returns when
    nothing redirects. "0 slugs redirect" and "I gave up and published the
    redirecting ones" were the same observable event — so the one number that
    decides whether the sitemap quietly degrades, len(out) against the ceiling,
    existed only inside this function for the length of one call.

    That is not theoretical. The refusal is reached by the collision work
    ARMING case-B 301s: disambiguate_slug_collisions collapses a shared slug,
    the keeper's slug_rows falls to 1, _twin_redirect_target stops refusing,
    and twins that used to render 200 start redirecting. Nothing in that chain
    is aware of this ceiling.

    MEASURED LIVE 2026-09-19, sampling the published artefact rather than
    trusting the guard: 400 of the 6,987 slugs in sitemap-facilities-1 and 400
    of the 5,000 /api/v1/map slugs, redirects NOT followed — 800/800 HTTP 200,
    zero redirecting (<0.75%, rule of three). So the guard has NOT tripped in
    production and this is a latent risk, not an incident. What that sweep
    could NOT measure is the headroom: how many slugs it dropped, i.e. how
    close len(out) is to the ceiling, is invisible from outside the process.
    Hence the log lines rather than a threshold change — the number has to
    exist somewhere before anyone can argue about what it should be.
    """
    want = []
    seen = set()
    for s in slugs or ():
        s = str(s or '').strip()
        if s and s not in seen:
            seen.add(s)
            want.append(s)
    if not want:
        return set()
    if _disabled():
        logger.warning(
            "sitemap redirect guard DISABLED by "
            "SITEMAP_REDIRECT_RESOLVE_DISABLE — every redirecting URL among "
            "%d slugs will be published", len(want))
        return set()
    try:
        from routes.facility_profile_page import served_slugs
        served = served_slugs(want, conn=conn)
    except Exception:
        logger.exception(
            "sitemap redirect guard: the resolver raised over %d slugs — "
            "publishing every one of them, redirecting or not", len(want))
        return set()
    if not isinstance(served, dict):
        logger.error(
            "sitemap redirect guard: the resolver returned %s, not a dict, "
            "over %d slugs — publishing every one of them",
            type(served).__name__, len(want))
        return set()
    out = {s for s in want if str(served.get(s) or s) != s}
    ceiling = max(_REFUSE_FLOOR, len(want) // _REFUSE_FRACTION)
    pct = 100.0 * len(out) / len(want)
    if len(out) > ceiling:
        # ★ Loud, and it names both numbers. The safe reading of a big result
        # is "the resolver broke"; the OTHER reading is "the collision work
        # armed this many real 301s", and only the first is a reason to
        # publish them. Nobody could tell which without this line.
        logger.error(
            "sitemap redirect guard REFUSED: %d of %d slugs (%.2f%%) resolve "
            "somewhere else, over the ceiling of %d — publishing ALL of them, "
            "so %d URLs that 301 are back in the sitemap. Either the resolver "
            "is wrong, or collision collapse armed that many twin redirects; "
            "check which before changing _REFUSE_FRACTION.",
            len(out), len(want), pct, ceiling, len(out))
        return set()
    # The headroom, on every build — the number the live sweep could not see.
    logger.info(
        "sitemap redirect guard: dropping %d of %d slugs (%.2f%%), ceiling %d",
        len(out), len(want), pct, ceiling)
    return out
