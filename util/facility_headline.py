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
"""
from __future__ import annotations

import re as _re


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
    title = f"{disp} · {city} | DC Hub" if city else f"{disp} | DC Hub"
    # r-site-code-title (2026-09-02): operator site-code queries ("interxion
    # mad1", "iad14 data center", "fra28", "htl05", "dus2") sit at pos 6-13
    # with 0 clicks — the code is buried mid-title. When the NAME carries one
    # unambiguous code, lead with "<Operator> <CODE> — <City> Data Center".
    from util.facility_site_code import site_code_headline as _sc_headline
    sc_head = _sc_headline(name, "" if provider == "Operator" else provider,
                           city, state, country)
    h1 = disp
    og_title = f"{disp} — Data Center"
    if sc_head:
        # sc_head is "<Operator> <CODE> — <City> Data Center". The template
        # wants its LEAD ("<Operator> <CODE>"); the city is re-appended in the
        # template's own separator, so it is not dropped, only moved. If that
        # module ever stops emitting the em-dash, fall back to the whole string
        # rather than mangling it.
        legacy_title = f"{sc_head} | DC Hub"
        lead = sc_head.split(" — ", 1)[0] if " — " in sc_head else sc_head
        title = f"{lead} · {city} | DC Hub" if city else f"{lead} | DC Hub"
        h1 = sc_head
        og_title = sc_head
    return {"op": op, "disp": disp, "loc_short": loc_short, "title": title,
            "h1": h1, "og_title": og_title, "dedup_title": legacy_title}


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
