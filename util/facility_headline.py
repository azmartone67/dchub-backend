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

    loc_short = ", ".join([p for p in (city, state, country) if p])
    # r-geo-facility-title (2026-06-24): rich, entity-bearing title/desc/h1
    # instead of city-only "{name} | DC Hub". Prepend the operator unless the
    # brand is already in the name (substring EITHER way, or shared leading
    # brand word — the plain `provider in name` check shipped SERP titles like
    # "Vantage Data Centers Vantage Berlin II"; see brand_already_in_name).
    op = "" if (not provider or provider == "Operator"
                or brand_already_in_name(provider, name)) else f"{provider} "
    disp = f"{op}{name}".strip()
    title = (f"{disp} — {loc_short} Data Center | DC Hub" if loc_short
             else f"{disp} Data Center | DC Hub")
    # r-site-code-title (2026-09-02): operator site-code queries ("interxion
    # mad1", "iad14 data center", "fra28", "htl05", "dus2") sit at pos 6-13
    # with 0 clicks — the code is buried mid-title. When the NAME carries one
    # unambiguous code, lead with "<Operator> <CODE> — <City> Data Center".
    from util.facility_site_code import site_code_headline as _sc_headline
    sc_head = _sc_headline(name, "" if provider == "Operator" else provider, city)
    h1 = disp
    og_title = f"{disp} — Data Center"
    if sc_head:
        title = f"{sc_head} | DC Hub"
        h1 = sc_head
        og_title = sc_head
    return {"op": op, "disp": disp, "loc_short": loc_short, "title": title,
            "h1": h1, "og_title": og_title}


def identity_key(name, provider, city, state, country):
    """The grouping key for "these two URLs render the same page".

    Case- and whitespace-folded (h1, title). Folding is deliberate: "Orange
    Business Services" and "orange business services" are one facility, and a
    detector that treated them as two would leave the duplicate published.
    """
    hl = facility_headline(name, provider, city, state, country)
    return (" ".join(hl["h1"].split()).lower(),
            " ".join(hl["title"].split()).lower())
