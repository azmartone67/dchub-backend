"""facility_headline.py — the rendered identity of a facility page.

WHY THIS MODULE EXISTS (2026-09-07, r-drain-fork)
-------------------------------------------------
`<h1>` and `<title>` on /facilities/<slug> are a pure function of five row
fields — name, provider, city, state, country. Nothing else. That makes them
the only honest test for "are these two URLs the same facility": the slug is
not (it hashes provider|name, and two rows describing one building routinely
disagree about `provider`), and the id is not (the two facility tables use
different id spaces).

routes/facility_dedup_v4.py groups published URLs on exactly this key. It MUST
NOT carry its own copy of the expressions — a detector that scores a mirror of
the renderer instead of the renderer goes green while the pages it is meant to
be looking at drift away from it. So the block was lifted OUT of
routes/facility_profile_page._render_profile verbatim and both call it here.

★ Every expression below is byte-identical to the one it replaced. The
  behaviour is pinned from the OUTSIDE by tests/test_seo_index_hygiene.py,
  which renders whole pages through _render_profile and asserts on the emitted
  title — so an "equivalent" rewrite here fails there.

THE SERP TITLE AND DESCRIPTION (2026-09-10, r-title-facts)
----------------------------------------------------------
compose_title() and compose_description() build the page's <title> and meta
description from the same five fields plus facts only the PAGE knows — power,
status, the DCPI market's ISO and time-to-power, the nearby-generation total.
They are display-only and NOT part of identity_key(); see the section at the
bottom of this module.
"""
from __future__ import annotations

import math
import re as _re
import unicodedata


def brand_already_in_name(provider: str, name: str) -> bool:
    """True when prepending `provider` to `name` would double the brand in the
    SERP title — measured 2026-08-01 as a corpus-wide CTR drag: "DataBank
    DataBank Dallas (DFW2)", "Vantage Data Centers Vantage Berlin II", "Oso
    Grande Technologies, Inc. Oso Grande Technologies". Three cases: provider
    inside name (the old check), name inside provider (legal-suffix operator
    strings), and a shared leading brand word ("Vantage …" vs "Vantage Data
    Centers"). Titles/desc/h1 only — the FROZEN slug is composed elsewhere and
    is never touched here."""
    p = (provider or "").lower().strip()
    n = (name or "").lower().strip()
    if not p or not n:
        return False
    if p in n or n in p:
        return True
    pt = _re.findall(r"[a-z0-9]+", p)
    nt = _re.findall(r"[a-z0-9]+", n)
    # Leading-word brand match. Generic first words are not a brand signal —
    # "Data Foundry" vs a name starting "Data Center …" must still prepend.
    _generic = {"the", "data", "center", "centre", "datacenter", "datacenters",
                "dc", "global"}
    return bool(pt and nt and pt[0] == nt[0] and pt[0] not in _generic)


def facility_headline(name, provider, city, state, country):
    """The page's rendered identity: {disp, loc_short, title, h1, og_title}.

    Callers pass RAW row values; the two `or` defaults that _render_profile
    applies (name -> "Data Center", provider -> "Operator") are applied here so
    a detector reading straight off the DB and the renderer reading off a fetch
    dict cannot disagree about a NULL.
    """
    name = name or "Data Center"
    provider = provider or "Operator"
    city = city or ""
    state = state or ""
    country = country or ""

    # ★★★ r-placeholder-city (2026-09-07) — A NON-PLACE IS NOT A PLACE.
    # `city` arrives straight off the row, and this dataset writes 'Regional'
    # when the upstream gave no city: 727 live rows, ALL source
    # 'competitor_gap:cloudscene'. util/thin_content has known that since
    # 2026-08-14 (real_city / PLACEHOLDER_CITIES) and `is_contentless` counts
    # those rows as having NO city — correctly. This function never asked, so
    # the same row rendered
    #     <title>China Telecom Shanwei Data Center — Regional, CN Data Center
    #     "… is a data center in Regional, CN."
    #     JSON-LD "addressLocality": "Regional"
    # i.e. an asserted location the upstream never gave, which is exactly what
    # tests/test_no_fabricated_facility_fields.py exists to forbid at the
    # WRITER. It leaked at the RENDERER instead.
    # ★ Treated as ABSENT, not rewritten: the city is unknown, and inferring
    #   "Shanwei" from the name would be the fabrication, not the fix.
    # ★ identity_key() rides on this, so the dedup grouping was measured before
    #   the change: 727 affected rows, every key string moves, and the grouping
    #   is IDENTICAL — 4,363 groups before and after, 0 added, 0 removed, 0
    #   keeper changes. See tests/test_thin_content_lanes.py.
    from util.thin_content import is_placeholder_city as _placeholder
    if _placeholder(city):
        city = ""

    loc_short = ", ".join([p for p in (city, state, country) if p])
    # r-geo-facility-title (2026-06-24): rich, entity-bearing title/desc/h1
    # instead of city-only "{name} | DC Hub". Prepend the operator unless the
    # brand is already in the name (substring EITHER way, or shared leading
    # brand word — the plain `provider in name` check shipped SERP titles like
    # "Vantage Data Centers Vantage Berlin II"; see brand_already_in_name).
    op = "" if (not provider or provider == "Operator"
                or brand_already_in_name(provider, name)) else f"{provider} "
    disp = f"{op}{name}".strip()
    # r-title-template (2026-09-09): `{Operator} {Site} · {City} · {Grid} |
    # DC Hub`. The old shape spent its budget on words that carry no query:
    #     Spectrum Charlotte National Data Center — Charlotte, NC, US Data
    #     Center | SERC grid | DC Hub                              (94 chars)
    # Measured live the same day over 10 facility pages, 6 of 10 titles ran
    # past Google's ~60-char SERP cut, and what fell off the end was the TAIL —
    # the grid token and the brand, i.e. the two things the title was extended
    # to carry. Dropping the literal "Data Center" (the <h1>, the description
    # and the JSON-LD all still say it), the ", ST, CC" tail and the word
    # "grid" returns ~27 chars per title.
    # ★ THIS IS A DISPLAY CHANGE ONLY. identity_key() — the grouping key
    #   routes/facility_dedup_v4.py uses to decide "these two URLs render the
    #   same page" — reads `dedup_title` below, which is the PRE-CHANGE string,
    #   byte for byte. Re-keying dedup on a prettier title would have silently
    #   re-grouped 20k pages as a side effect of an SEO edit; the r-placeholder-
    #   city note above is there because that grouping is measured, not assumed.
    legacy_title = (f"{disp} — {loc_short} Data Center | DC Hub" if loc_short
                    else f"{disp} Data Center | DC Hub")
    # r-site-code-title (2026-09-02): operator site-code queries ("interxion
    # mad1", "iad14 data center", "fra28", "htl05", "dus2") sit at pos 6-13
    # with 0 clicks — the code is buried mid-title. When the NAME carries one
    # unambiguous code, lead with "<Operator> <CODE> — <City> Data Center".
    from util.facility_site_code import site_code_headline as _sc_headline
    sc_head = _sc_headline(name, "" if provider == "Operator" else provider,
                           city, state, country)
    h1 = disp
    og_title = f"{disp} — Data Center"
    lead = disp
    if sc_head:
        # sc_head is "<Operator> <CODE> — <City> Data Center". The template
        # wants its LEAD ("<Operator> <CODE>"); the location is re-composed in
        # the template's own separator, so it is not dropped, only moved. If
        # that module ever stops emitting the em-dash, fall back to the whole
        # string rather than mangling it.
        legacy_title = f"{sc_head} | DC Hub"
        lead = sc_head.split(" — ", 1)[0] if " — " in sc_head else sc_head
        h1 = sc_head
        og_title = sc_head
    # ★ THE LOCATION SLOT IS "the most specific place we actually have", not
    #   "city". tests/test_seo_index_hygiene.py holds a FLOOR on this —
    #   test_the_country_survives_when_the_city_is_a_placeholder — because the
    #   fix for the 'Regional' placeholder rows could otherwise be satisfied by
    #   dropping the location ENTIRELY, and the country is real. A first cut of
    #   this template did exactly that and the floor caught it.
    # r-title-facts (2026-09-10): the slot rule now lives in _title_place and
    #   keeps that floor. This is the title WITHOUT the facts only the page
    #   knows (power, status, ISO); compose_title adds them through the same
    #   function, so the two can never disagree about the location.
    title = _assemble_title(lead, op, city, state=state, country=country)
    return {"op": op, "disp": disp, "loc_short": loc_short, "title": title,
            "h1": h1, "og_title": og_title, "dedup_title": legacy_title,
            "lead": lead}


def identity_key(name, provider, city, state, country):
    """The grouping key for "these two URLs render the same page".

    Case- and whitespace-folded (h1, title). Folding is deliberate: "Orange
    Business Services" and "orange business services" are one facility, and a
    detector that treated them as two would leave the duplicate published.
    """
    hl = facility_headline(name, provider, city, state, country)
    # ★ `dedup_title`, NOT `title`. r-title-template (2026-09-09) reshaped the
    # DISPLAYED title; this key must not move with it. facility_dedup_v4 groups
    # on this value and check_sitemap_selfcanon counts the groups, so a change
    # here re-partitions ~20k pages — a dedup decision, never a side effect of
    # an SEO copy edit. dedup_title is the pre-change string byte for byte.
    return (" ".join(hl["h1"].split()).lower(),
            " ".join(hl["dedup_title"].split()).lower())


# ── CANDIDATE, NOT WIRED: a wider identity (2026-09-11, r-identity-wide) ──
#
# ★★★ NOTHING CALLS identity_key_wide(). It is measured and tested, and it is
#   deliberately left unused. Re-pointing routes/facility_dedup_v4.plan_group
#   or scripts/check_sitemap_selfcanon at it RE-PARTITIONS the ~19k published
#   facility URLs and changes which pages get a rel=canonical — a dedup
#   decision that needs its own review, its own measurement and its own apply
#   window. See tests/test_facility_identity_key_wide.py for the measured
#   delta and the collision cases this key must NOT merge.
#
# WHY A WIDER KEY EXISTS AT ALL. identity_key() folds case and whitespace but
# then compares exact strings, so three cosmetic differences hide a true
# duplicate from every consumer:
#
#     "Telehouse Telehouse Frankfurt"  vs  "Telehouse - Frankfurt"
#     "Flexential Atlanta GA"          vs  "Flexential Atlanta, GA"
#     "Global Switch Global Switch Madrid" vs "Global Switch Madrid"
#
# The doubling is not a data error the ingest can simply stop making: it comes
# from a row whose `name` ALREADY carries the operator meeting a `provider`
# that brand_already_in_name() does not suppress (a different legal-entity
# spelling — "Master Internet s.r.o." vs a name starting "Master DC").
#
# MEASURED 2026-09-11 over the live corpus (every /facilities/<slug> in the
# published sitemap, 18,809 URLs, five fields each from the public
# /api/v1/exports/facilities snapshot + /api/v1/facilities/<slug>):
#
#     identity_key       18,701 keys · 102 groups of >=2 · 108 surplus URLs
#     identity_key_wide  18,663 keys · 139 groups of >=2 · 146 surplus URLs
#     38 groups fuse 76 previously-distinct keys, touching 77 URLs
#     all 38 have >=2 members that are self-canonical TODAY (0 already merged)
#     routes.facility_dedup_v4.designators_disagree vetoes 0 of the 38
#
# ★★★ TWO FOLDS THAT LOOK OBVIOUS AND ARE WRONG — both were measured, not
#     reasoned about, and both are pinned by tests:
#
#   1. `[^a-z0-9]` IS NOT "punctuation". It erases every CJK, Cyrillic, Arabic
#      and Bengali name to the empty string, and 36 Chinese-named facilities
#      then land in ONE group. The fold must keep Unicode alphanumerics.
#   2. DE-DOUBLING THE WHOLE STRING EATS THE CITY. "DataBank Atlanta —
#      Atlanta, US …" (city Atlanta) collapses onto "DataBank Atlanta — US …"
#      (NO city), because the repeated token straddles the boundary between
#      the site name and the location slot. Only a LEADING run is an operator
#      prefix, so only a leading run is collapsed.
#
# WHAT IT STILL WILL NOT CATCH (measured, stated rather than hidden): four
# pairs that differ because one row repeats its city into `state` ("Prague,
# Prague" vs "Prague"). Collapsing a repeat there is the boundary-crossing
# fold rule 2 forbids; those need a location normaliser, not a wider name key.

_MAX_BRAND_TOKENS = 4


def _fold_identity(text: str) -> str:
    """Case-, punctuation- and whitespace-folded, Unicode-safe.

    NFKC first so full-width and compatibility forms agree with their ASCII
    spellings ("AirTrunk ２Ａ棟" -> "airtrunk 2a棟"), then every character that
    is not alphanumeric IN ANY SCRIPT becomes a space.

    ★ `ch.isalnum()`, never `[a-z0-9]`: an ASCII class is not a punctuation
      filter, it is a Latin filter. Measured 2026-09-11, it folded 36 Chinese-,
      Japanese-, Russian-, Arabic- and Bengali-named facilities to "" and put
      them in a single group.
    ★ Punctuation becomes a SPACE, not nothing: "1&1" -> "1 1" keeps the token
      boundary the name actually has.
    """
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join("".join(c if c.isalnum() else " "
                            for c in text).split())


def _dedouble_leading(tokens):
    """Collapse ONE immediately-repeated run of up to _MAX_BRAND_TOKENS at the
    START of `tokens` — the operator doubling, and nothing else.

        ["telehouse", "telehouse", "frankfurt"] -> ["telehouse", "frankfurt"]
        ["global", "switch", "global", "switch", "madrid"]
                                     -> ["global", "switch", "madrid"]

    ★★★ LEADING ONLY, AND THAT IS THE WHOLE SAFETY ARGUMENT. A repeat anywhere
      in the string is not evidence of a doubled brand; it is just as likely to
      be the site name meeting the location slot. Measured 2026-09-11: an
      any-position collapse merged "DataBank Atlanta — Atlanta, US Data Center"
      (city Atlanta) with "DataBank Atlanta — US Data Center" (city NULL) by
      eating one "atlanta", i.e. it made a row that names a city equal to a row
      that does not. Four such merges were dropped by this restriction and all
      four were made for the wrong reason.
    ★ ONE run, not a loop to a fixed point: repeated collapsing is how a
      three-token brand quietly turns into a one-token one.
    """
    n = len(tokens)
    for w in range(min(_MAX_BRAND_TOKENS, n // 2), 0, -1):
        if tokens[:w] == tokens[w:2 * w]:
            return tokens[w:]
    return list(tokens)


def identity_key_wide(name, provider, city, state, country):
    """CANDIDATE grouping key — WIDER than identity_key(). NOT WIRED UP.

    Same shape and same two components as identity_key() — (h1, dedup_title) —
    so a consumer could be repointed with no other change, and same inputs, so
    the two can be compared row by row. The difference is the fold: case,
    punctuation and whitespace all collapse, and a doubled leading operator
    token collapses with them.

    ★ It reads `dedup_title`, exactly as identity_key() does. The displayed
      <title> moves with SERP copy edits; a grouping key must not.
    """
    hl = facility_headline(name, provider, city, state, country)
    return (" ".join(_dedouble_leading(_fold_identity(hl["h1"]).split())),
            " ".join(_dedouble_leading(
                _fold_identity(hl["dedup_title"]).split())))


# ── r-title-facts (2026-09-10): the SERP <title> and meta description ─────
#
# ★★★ DISPLAY-ONLY. Nothing below is part of identity_key(). The key reads
#   `h1` and `dedup_title`, which facility_headline() settles BEFORE it calls
#   _assemble_title, and the facts composed here — power, status, the DCPI
#   market's ISO and time-to-power, nearby generation — are not even among the
#   key's inputs. tests/test_facility_title_template.py holds that from the
#   outside, against an independent oracle of the pre-template title.
#
# Why (owner-approved drafts, 2026-09-10, GSC window 2026-08-12 → 09-08): the
# r-title-template shape repeated the city when the name already carries it
# ("Google Council Bluffs Data Center · Council Bluffs"), dropped the state
# that 9 of the top 19 queries for that page contain ("iowa"), and never
# showed MW. Optional facts are admitted in priority order only while the
# title fits Google's ~60-char cut, so the ISO — last in the template — is the
# first to go; it stays in the description, the body and the JSON-LD.

TITLE_BRAND = " | DC Hub"
TITLE_BUDGET = 60                  # Python len(), TITLE_BRAND included
DESCRIPTION_LIMIT = 160
# A plausibility cap, not a unit rule: pipeline rows carry 48,000+ MW, and a
# number in a SERP title reads as a fact about this one building.
MW_PLAUSIBLE_MAX = 5000.0
_US_COUNTRIES = frozenset({"US", "USA", "UNITED STATES",
                           "UNITED STATES OF AMERICA"})
# "operational" is absent ON PURPOSE. People searching "equinix fr5 status",
# "… down", "… issue" want outage news; "Operational" in that title reads as a
# live status claim. The description states the lifecycle in plain words.
# "announced" is absent too (owner review, 2026-09-11): it was 83 of the 97
# phases a 330-page sample would have shown, i.e. a quarter of all facility
# titles, and announced statuses are mostly news-extracted, the likeliest to be
# stale. The approved template names only planned / under construction; the
# description still says "announced data center".
TITLE_PHASES = ("planned", "under construction")
_DESCRIPTION_ADJECTIVES = {
    "operational": "operational",
    "planned": "planned",
    "under construction": "under-construction",
    "announced": "announced",
}
_DESCRIPTION_TAILS = (" Specs, nearby power and peer sites on DC Hub.",
                      " Specs, nearby power & peers.")


def display_mw(power_mw) -> str:
    """The capacity a title or description may print: "350 MW", "2.5 MW", "".

    "" unless `power_mw` parses and 0 < MW <= MW_PLAUSIBLE_MAX. Anything >= 10
    or whole prints with no decimals ("1,200 MW"), a smaller fraction with one
    ("2.5 MW"); a value that would print as "0.0" is not worth printing.
    """
    if isinstance(power_mw, bool):
        return ""
    try:
        p = float(power_mw)
    except (TypeError, ValueError):
        return ""
    if not 0 < p <= MW_PLAUSIBLE_MAX:          # NaN fails this as well
        return ""
    txt = f"{p:,.0f}" if (p >= 10 or p == int(p)) else f"{p:.1f}"
    return "" if txt == "0.0" else f"{txt} MW"


def title_phase(status) -> str:
    """The lifecycle word a title may carry — "planned" or "under
    construction", lower-case — else "". Never "operational" or "announced"
    (TITLE_PHASES)."""
    s = str(status or "").strip().lower()
    return s if s in TITLE_PHASES else ""


def time_to_power_phrase(months) -> str:
    """"~19 months" under 36 months, "~9.8 years" from 36 on; "" when `months`
    is absent or not a finite, non-negative number."""
    if months is None or isinstance(months, bool):
        return ""
    try:
        m = float(months)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(m) or m < 0:
        return ""
    return f"~{m:.0f} months" if m < 36 else f"~{m / 12:.1f} years"


def region_name(state, country) -> str:
    """The spelled region: the US state ("Iowa") on a US row that has one, else
    the country ("Germany", or the raw value when it has no spelling), else the
    raw state, else ""."""
    from location_names import get_country_name, get_state_name
    st = str(state or "").strip()
    cc = str(country or "").strip()
    if st and cc.upper() in _US_COUNTRIES:
        return get_state_name(st, "US") or st
    if cc:
        return get_country_name(cc) or cc
    return st


def _real_city(city) -> str:
    """`city` with a placeholder ('Regional', 'Unknown', …) read as absent —
    the question facility_headline() asks, through the same predicate."""
    from util.thin_content import is_placeholder_city
    city = city or ""
    return "" if is_placeholder_city(city) else city


def _names_words(text, words) -> bool:
    """True when `words` occurs in `text` as whole words, case-insensitively.

    Whole words, not a substring (owner review, 2026-09-11): a substring test
    let "Australian" hide "Australia", "NOCPERU" hide "Peru", "ColoradoColo"
    hide "Colorado" and "DCROUBAIX" hide "Roubaix", so 4 of 330 sampled titles
    lost a real location. The boundary is an ASCII letter or digit rather than
    a regex word character, so names written without spaces (CJK) still match
    the way they did.
    """
    w = str(words or "").strip()
    if not w:
        return False
    return _re.search(r"(?<![A-Za-z0-9])" + _re.escape(w) + r"(?![A-Za-z0-9])",
                      str(text or ""), _re.IGNORECASE) is not None


def _title_place(lead, op, city, state, country):
    """(place, qualifier) for the title's location slot.

    place      MANDATORY, never dropped. With no real city it is the spelled
               region — the FLOOR tests/test_seo_index_hygiene.py holds: the
               country is real and publishes even when the city is a
               placeholder. Otherwise the city, unless the lead already carries
               it ("Google Council Bluffs Data Center"), in which case "".
    qualifier  OPTIONAL. The spelled region, appended to the city or standing
               alone, unless the SITE part of the lead already names it. The
               operator prefix is left out of that check deliberately: a
               provider called "China Telecom" does not put "China" in the
               title, and checking the whole lead drops the country.
    """
    site = lead[len(op):] if (op and lead.startswith(op)) else lead
    region = region_name(state, country)
    region_in_site = _names_words(site, region)
    if not city:
        return region, ""
    qualifier = "" if region_in_site else region
    if not _names_words(lead, city):
        return city, qualifier
    return "", qualifier


def _assemble_title(lead, op, city, state, country, power_mw=None,
                    status=None, iso=None) -> str:
    """`{lead} · {place}[, {region}] · {fact} · {ISO} | DC Hub`.

    `city` must already be placeholder-free. The lead and the place are
    mandatory and kept even past TITLE_BUDGET. The optional parts are admitted
    in PRIORITY order, each only while the whole title stays within budget —
      1. the fact: "200 MW planned", else "200 MW", else "Planned"
      2. the region qualifier
      3. the ISO, a registered label only (never 'UNK')
    — and DISPLAY in template order.
    """
    from util.iso_taxonomy import is_registered_label
    place, qualifier = _title_place(lead, op, city, state, country)
    mw, phase = display_mw(power_mw), title_phase(status)
    iso = str(iso).strip() if is_registered_label(iso) else ""

    def render(fact="", with_qualifier=False, with_iso=False):
        loc = place
        if with_qualifier and qualifier:
            loc = f"{place}, {qualifier}" if place else qualifier
        parts = [lead] + [p for p in (loc, fact, iso if with_iso else "") if p]
        return " · ".join(parts) + TITLE_BRAND

    def fits(text):
        return len(text) <= TITLE_BUDGET

    facts = []
    if mw and phase:
        facts.append(f"{mw} {phase}")
    if mw:
        facts.append(mw)
    if phase:
        facts.append(phase.capitalize())
    fact = next((f for f in facts if fits(render(f))), "")
    with_qualifier = bool(qualifier) and fits(render(fact, True))
    with_iso = bool(iso) and fits(render(fact, with_qualifier, True))
    return render(fact, with_qualifier, with_iso)


def compose_title(name, provider, city, state, country, power_mw=None,
                  status=None, iso=None) -> str:
    """The facility page's <title>. DISPLAY-ONLY — not part of identity_key().

    facility_headline()'s `title` (the same lead, the same location slot) plus
    the facts only the page knows: `power_mw`, `status` and the market's `iso`.
    Called with none of them it returns exactly facility_headline()'s title.
    """
    hl = facility_headline(name, provider, city, state, country)
    return _assemble_title(hl["lead"], hl["op"], _real_city(city),
                           state or "", country or "", power_mw=power_mw,
                           status=status, iso=iso)


def _generation_mw_text(mw) -> str:
    """"1,628" — formatted the way the "Power generation nearby" section prints
    its total — or "" when there is no positive, finite total to cite."""
    if mw is None or isinstance(mw, bool):
        return ""
    try:
        v = float(mw)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(v) or v <= 0:
        return ""
    txt = f"{v:,.0f}"
    return "" if txt == "0" else txt


def compose_description(name, provider, city, state, country, power_mw=None,
                        status=None, iso=None, time_to_power_months=None,
                        nearby_generation_mw=None, radius_km=50,
                        limit=DESCRIPTION_LIMIT) -> str:
    """The meta description (also og:/twitter:description). DISPLAY-ONLY.

    `[{name}: ]{MW} {status} data center in {City, Region}[, operated by
    {operator}][, on the {ISO} grid]. [{time-to-power} to power for new builds
    here. | {N} MW of operating generation within {radius} km.] Specs, nearby
    power and peer sites on DC Hub.`

    * `{name}: ` only when the title shows a SHORTENED lead (a site code —
      "Equinix FR5"), so the snippet still carries the full name;
    * the operator only when it is real and the name does not already contain
      it; the grid only for a registered ISO label;
    * time-to-power comes from the DCPI market. The generation sentence is the
      fallback, and its total must be the one the page's own section prints —
      the caller passes it only when that section rendered;
    * no DCPI verdict word (owner decision: "AVOID" next to "Google Council
      Bluffs" reads as a verdict on Google's building);
    * at most `limit` characters: the optional sentence and the tail are added
      only when they fit, and the tail falls back to a shorter form.
    """
    from util.iso_taxonomy import is_registered_label
    hl = facility_headline(name, provider, city, state, country)
    disp, lead = hl["disp"], hl["lead"]
    city = _real_city(city)
    st = str(state or "").strip()
    region = region_name(st, country)
    if city and region and city.lower() == region.lower():
        # "New York, NY": the spelled region would only repeat the city
        where = f"{city}, {st}" if (st and st.lower() != city.lower()) else city
    else:
        where = ", ".join(p for p in (city, region) if p)
    adjective = _DESCRIPTION_ADJECTIVES.get(str(status or "").strip().lower(), "")
    head = " ".join(p for p in (display_mw(power_mw), adjective, "data center")
                    if p)
    operator = str(provider or "").strip()
    by = (f", operated by {operator}"
          if (operator and operator != "Operator"
              and operator.lower() not in disp.lower()) else "")
    grid = (f", on the {str(iso).strip()} grid"
            if is_registered_label(iso) else "")
    prefix = f"{disp}: " if disp.lower() != lead.lower() else ""
    at = f" in {where}" if where else ""

    def first_sentence(prefix, by, grid):
        h = head if prefix else head[:1].upper() + head[1:]
        return f"{prefix}{h}{at}{by}{grid}."

    text = first_sentence(prefix, by, grid)
    # The first sentence has to fit on its own. A pathological row (a very
    # long name or operator) sheds the operator clause, then the grid clause,
    # then the name prefix — and only then is cut at a word boundary.
    for p_, b_, g_ in ((prefix, "", grid), (prefix, "", ""), ("", "", "")):
        if len(text) <= limit:
            break
        text = first_sentence(p_, b_, g_)
    if len(text) > limit:
        text = text[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"

    ttp = time_to_power_phrase(time_to_power_months)
    gen = _generation_mw_text(nearby_generation_mw)
    if ttp:
        extra = f" {ttp} to power for new builds here."
    elif gen:
        extra = f" {gen} MW of operating generation within {radius_km} km."
    else:
        extra = ""
    if extra and len(text + extra) <= limit:
        text += extra
    for tail in _DESCRIPTION_TAILS:
        if len(text + tail) <= limit:
            text += tail
            break
    return text
