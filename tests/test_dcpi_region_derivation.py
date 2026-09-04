"""DCPI region/country span DERIVATION fence — the manifest cannot restate a map.

/.well-known/ai-agents.json described DCPI as covering

    "300+ markets across the U.S., UK, EU, Japan, Australia, Singapore, and Canada"

— a canon-derived COUNT wired to a HAND-TYPED REGION LIST, in one sentence.
The count floored safely. The list did not, and by 2026-09-03 it named seven
regions against a live universe of 36 countries over 323 scored markets,
omitting Mexico (queretaro, scored 2026-08-07), Brazil, South Africa, India,
South Korea, Taiwan, Hong Kong, Malaysia, Indonesia, Thailand, Vietnam, the
Philippines, New Zealand, thirteen further European countries and the US
territories. Its STRUCTURED twin twenty lines below — dcpi_coverage.
international_markets — was frozen at the 2026-05-25 launch set of 10
countries and 16 markets, and that is the half agents parse.

Same class as the "233 DCPI-scored markets" literal already documented in the
very next field of the same payload: one document, two answers, and the
hardcoded one under-claims.

VERIFIED AGAINST THE LIVE SCORED UNIVERSE (/api/v1/dcpi/scores, 2026-09-03,
at the Railway origin AND the edge — both 323), never against
routes/dcpi.py::_MARKETS_HARDCODED. Most markets arrive from the dynamic
loader and are not in those tuples; barueri and osasco were missed exactly
that way, which routes/dcpi.py:990 records.

No DB and no network: the fences below read the SHIPPED source (AST-extracted,
as tests/test_ai_agents_manifest_canon_bound.py does) and the SHIPPED maps.
"""
from __future__ import annotations

import ast
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, "main.py")

#: The live scored universe as measured 2026-09-03. Rows are (iso, state,
#: slug) triples for every market whose country is NOT decidable from its
#: `state` code alone — i.e. every collision this derivation has to survive.
#: Kept small and named on purpose: it fences the resolver's HARD cases, not
#: a mirror of the whole table.
LIVE_COLLISION_ROWS = [
    # iso,          state, slug,           expected country
    ("POSOCO",      "IN",  "mumbai",       "IN"),          # vs Indiana
    ("POSOCO",      "IN",  "chennai",      "IN"),
    ("ENTSOE-DE",   "DE",  "munich",       "DE"),        # vs Delaware
    ("ENTSOE-DE",   "DE",  "berlin",       "DE"),
    ("PLN",         "ID",  "jakarta",      "ID"),      # vs Idaho
    ("PLN-BATAM",   "ID",  "batam",        "ID"),
    ("AEMO",        "WA",  "perth",        "AU"),      # vs Washington
    ("AEMO",        "QL",  "brisbane",     "AU"),
    ("CENACE",      "MX",  "queretaro",    "MX"),         # added 2026-08-07
    ("KEPCO",       "JP",  "osaka",        "JP"),          # Kansai, NOT Korea
    ("KEPCO-KR",    "KR",  "seoul",        "KR"),
    ("NORDPOOL",    "NO",  "oslo",         "NO"),         # one label, 4 countries
    ("NORDPOOL",    "SE",  "stockholm",    "SE"),
    # ★ Refreshed 2026-09-04. osasco and johannesburg USED to carry no
    #   operator ('UNK'/'') and were resolved by the slug map; upstream
    #   registered ONS and ESKOM, so the OPERATOR settles them now and the
    #   slug map no longer fires for any live row. Fencing the pairs that are
    #   actually live — a fence against a reality that has moved on proves
    #   nothing about the one that exists.
    ("ONS",         "BR",  "osasco",       "BR"),         # Brazil
    ("ONS",         "BR",  "barueri",      "BR"),
    ("ESKOM",       "GP",  "johannesburg", "ZA"),         # Gauteng, not GP-land
    ("ESKOM",       "GP",  "midrand",      "ZA"),
    # ★ THE marquee collision: Colombia's alpha-2 is CO, and so is Colorado's
    #   state code. Nothing but _is_intl_market standing between bogota and
    #   Denver — a guard that reads `state` first puts Bogota in the Rockies.
    ("XM",          "CO",  "bogota",       "CO"),
    ("CEN",         "CL",  "santiago",     "CL"),
    ("ENTSOE-IT",   "IT",  "bologna",      "IT"),         # BO is also Bolivia
    # US territories keep their own ISO-3166 code — correct for a schema.org
    # Place, and folded back into the United States for the COUNTRY count by
    # canonical_stats._COUNTRY_NAME (see the test below).
    ("PREPA",       "PR",  "san-juan",     "PR"),
    ("GPA",         "GU",  "guam",         "GU"),
    ("WAPA",        "VI",  "virgin-islands", "VI"),
]

#: Every grid-operator label carried by a row of the live scored universe,
#: refreshed 2026-09-04, EXCEPT 'NORDPOOL' — the one label that does not name
#: a single country (four members, disambiguated by `state`).
#:
#: ★ 2026-09-04: the 'UNK'/'' sentinels USED to be the other exception. They
#:   are gone — upstream registered ESKOM and ONS, and NO live row now carries
#:   an unregistered operator. The sentinels remain handled in the resolver on
#:   purpose (the next unregistered operator is a matter of time), but there is
#:   no longer a live row to fence them with, and inventing one would fence a
#:   fiction. CEN/ESKOM/ONS/XM joined the live set.
#:
#: ★ THIS is the fence that matters at runtime. A missing COUNTRY is loud —
#:   the country vanishes from the phrase. A missing OPERATOR is silent and
#:   much worse: dropping "PJM" would resolve the single largest block of live
#:   markets to "" and quietly exclude them, while every country in the map
#:   stayed present and a country-only fence stayed green. Found by mutating
#:   exactly that.
LIVE_OPERATOR_LABELS_2026_09_04 = (
    # (operator label, the `state` a live row actually pairs with it)
    ("AEMO", "WA"), ("AESO", "AB"), ("AK", "AK"), ("BCH", "BC"),
    ("CAISO", "CA"), ("CEN", "CL"), ("CENACE", "MX"), ("CLP", "HK"),
    ("EGAT", "TH"), ("EirGrid", "IE"), ("EMA", "SG"),
    ("ENTSOE-AT", "AT"), ("ENTSOE-BE", "BE"), ("ENTSOE-CH", "CH"),
    ("ENTSOE-CZ", "CZ"), ("ENTSOE-DE", "DE"), ("ENTSOE-ES", "ES"),
    ("ENTSOE-FR", "FR"), ("ENTSOE-GR", "GR"), ("ENTSOE-IT", "IT"),
    ("ENTSOE-NL", "NL"), ("ENTSOE-PL", "PL"), ("ENTSOE-PT", "PT"),
    ("ERCOT", "TX"), ("ESKOM", "GP"), ("EVN", "VN"), ("FRCC", "FL"),
    ("GPA", "GU"), ("HECO", "HI"), ("HQ", "QC"), ("IESO", "ON"),
    ("ISONE", "RI"), ("KEPCO", "JP"), ("KEPCO-KR", "KR"), ("MH", "MB"),
    ("MISO", "MI"), ("NGCP", "PH"), ("NGESO", "UK"), ("NYISO", "NY"),
    ("ONS", "BR"), ("PJM", "WV"), ("PLN", "ID"), ("PLN-BATAM", "ID"),
    ("POSOCO", "IN"), ("PREPA", "PR"), ("SERC", "KY"), ("SOCO", "GA"),
    ("SPP", "OK"), ("TAIPOWER", "TW"), ("TEPCO", "JP"), ("TNB", "MY"),
    ("TPM", "NZ"), ("TVA", "TN"), ("WAPA", "VI"), ("WECC", "WY"),
    ("XM", "CO"),
)

#: Countries measured in the live scored universe on 2026-09-04 (US
#: territories folded into the United States, as the count does). The fence is
#: a SUBSET check, never equality — the set grows, and a test that has to be
#: edited every time a market is scored is a test that gets deleted.
LIVE_COUNTRIES_2026_09_04 = 38


def _source() -> str:
    with open(MAIN, encoding="utf-8") as fh:
        return fh.read()


def _handler_source() -> str:
    src = _source()
    start = src.find("if path == '/.well-known/ai-agents.json'")
    assert start != -1, "ai-agents.json handler not found in main.py"
    end = src.find('"provider": {', start)
    assert end != -1 and end > start, "ai-agents handler end marker not found"
    return src[start:end]


def _handler_code() -> str:
    """The handler with whole-line comments removed.

    The sweeps below MUST read code, not prose: the comment that explains why
    "Japan, Australia, Singapore" was removed contains the literal. A fence
    that fails on its own documentation can only be kept green by deleting the
    documentation. Same helper, same reason, as
    tests/test_ai_agents_manifest_canon_bound.py::_handler_code.
    """
    return "\n".join(
        ln for ln in _handler_source().splitlines()
        if not ln.lstrip().startswith("#")
    )


# --------------------------------------------------------------------------
# 1. the resolver: country comes from the OPERATOR, never from `state`
# --------------------------------------------------------------------------

@pytest.mark.parametrize("iso,state,slug,expected", LIVE_COLLISION_ROWS)
def test_country_resolves_from_the_operator_label(iso, state, slug, expected):
    from routes.dcpi import market_country
    got = market_country(state, iso, slug)      # NB (state, iso, slug)
    assert got == expected, (
        f"{slug} (iso={iso!r} state={state!r}) resolved to {got!r}, want "
        f"{expected!r}. These are the two-letter namespace collisions live on "
        f"market_power_scores — keying country off `state` relocates five "
        f"Indian markets to Indiana."
    )


def test_unknown_operator_never_defaults_to_the_us():
    """`.get(iso, "US")` is the shape that hid Brazil and Korea for weeks.

    main.py::_v1_iso_zones defaulted its grid-telemetry country map to "US",
    so every unmapped operator was silently counted as American —
    tests/test_iso_zones_country_map.py exists because of it. A country map
    that guesses under-claims nations and never reports that it did.
    """
    from routes.dcpi import market_country
    for label in ("NEWGRID-2027", "ZZZ", "SOMEISO"):
        # A non-US state code, so the US branch cannot answer for it.
        assert market_country("ZZ", label, "somewhere-new") is None, (
            f"{label!r} resolved to a country. An unknown operator must "
            f"resolve to '' so callers can EXCLUDE and REPORT it."
        )


def test_the_not_a_label_sentinels_do_not_invent_a_country():
    from routes.dcpi import market_country
    # 'UNK' and '' are this codebase's not-a-label sentinels (see
    # is_registered_label). A market carrying one, a non-US state code and no
    # entry in the operator-less map has no derivable country.
    assert market_country("ZZ", "UNK", "unlisted-market") is None
    assert market_country("ZZ", "", "unlisted-market") is None


# --------------------------------------------------------------------------
# 2. every operator's country can reach the phrase
# --------------------------------------------------------------------------

def test_us_territories_fold_into_the_united_states_for_the_count():
    """PR/GU/VI are US territories, not nations.

    _market_country returns them as themselves on purpose ("more precisely
    themselves than US", and schema.org accepts either) — right for a Place,
    wrong for a country COUNT, where it would publish Puerto Rico as a nation
    and put the span 3 above reality. Floors round DOWN.
    """
    from canonical_stats import _COUNTRY_NAME
    for code in ("US", "PR", "GU", "VI", "AS", "MP"):
        assert _COUNTRY_NAME[code] == "United States", (
            f"{code} does not fold into the United States — the DCPI country "
            f"span would over-claim by counting a US territory as a nation")


def test_every_mapped_country_has_a_region():
    """A country with no region contributes nothing to dcpi_regions_phrase().

    That is a SILENT under-claim: adding an operator without a region drops
    its region from the sentence with no error anywhere.
    """
    from canonical_stats import _COUNTRY_NAME, _COUNTRY_REGION, _REGION_ORDER
    known = set(_COUNTRY_NAME.values())
    # ★ NON-VACUITY ANCHOR. Without it, emptying ISO_COUNTRY — the exact
    #   breakage this fence exists to catch — makes `known` empty, `missing`
    #   empty, and the assertion below PASS. A guard whose loop body never
    #   runs is not a guard; see feedback on guards that call the predicate
    #   under test. The floor is the live 2026-09-03 span.
    assert len(known) >= LIVE_COUNTRIES_2026_09_04, (
        f"the operator map spans only {len(known)} countries — this fence "
        f"cannot check what is not there")
    missing = sorted(c for c in known if c not in _COUNTRY_REGION)
    assert not missing, (
        f"{missing} resolve to a country with no continental region. Add them "
        f"to canonical_stats._COUNTRY_REGION or they vanish from the phrase."
    )
    stray = sorted(r for r in _COUNTRY_REGION.values() if r not in _REGION_ORDER)
    assert not stray, (
        f"{stray} are regions _REGION_ORDER never prints, so the phrase would "
        f"silently drop them."
    )


@pytest.mark.parametrize("label,state", LIVE_OPERATOR_LABELS_2026_09_04)
def test_every_live_operator_label_resolves_to_a_country(label, state):
    """A label the live table uses but the resolver does not know yields None,
    and _query_live() EXCLUDES it — silently dropping every market under that
    operator from the span. PJM alone is 60 of 323."""
    from routes.dcpi import market_country
    got = market_country(state, label, "some-market")
    assert got, (
        f"{label!r} is carried by live market_power_scores rows and resolves "
        f"to no country. Those markets drop out of dcpi_countries and out of "
        f"international_markets, with no error anywhere."
    )


def test_the_multi_country_and_operatorless_rows_still_resolve():
    """The shapes deliberately absent from LIVE_OPERATOR_LABELS.

    ★ 2026-09-04: this test used to assert that bologna/barueri/osasco
      resolved from the SLUG map while carrying the 'UNK' sentinel. Upstream
      registered ENTSOE-IT and ONS, so all three now resolve from their
      OPERATOR and the slug entries were removed as dead weight — a slug map
      that shadows a live label path is the second answer this whole change
      set exists to delete. Asserting the old path would fence a reality that
      no longer exists, so what is pinned here is the behaviour that still
      has to hold.
    """
    from routes.dcpi import market_country
    # One label, four countries: the market's own code decides.
    assert market_country("FI", "NORDPOOL", "helsinki") == "FI"
    assert market_country("NO", "NORDPOOL", "oslo") == "NO"
    # The surviving slug override, kept as the fallback for a market that
    # arrives before its operator is registered.
    assert market_country("GP", "", "johannesburg") == "ZA"


def test_an_unregistered_operator_yields_none_and_never_a_guess():
    """★ The resolver must NEVER infer a country from a subnational code.

    This is the property the slug map used to hide. Every code below is a
    real subdivision that collides with a country's ISO-3166 alpha-2: BO is
    Bologna AND Bolivia, SP is Sao Paulo, GP is Gauteng. A resolver that fell
    back to `state` would publish Bolivia, and it would publish it into the
    /dcpi Dataset JSON-LD that AI engines lift verbatim.

    None is the correct answer: the caller omits the country rather than
    asserting a wrong one. An under-claim is recoverable, a confident lie is
    not — the same rule the derived badges follow in README.md.
    """
    from routes.dcpi import market_country
    for state, slug in (("BO", "bologna"), ("SP", "barueri"),
                        ("SP", "osasco"), ("GP", "midrand")):
        for sentinel in ("UNK", ""):
            assert market_country(state, sentinel, slug) is None, (
                f"{slug} resolved a country from state={state!r} with an "
                f"unregistered operator. The resolver guessed — that is how "
                f"Bolivia gets published as an Italian market's country."
            )


def test_the_live_universe_spans_more_countries_than_the_old_list_named():
    """The measured span, held as a FLOOR so the fence survives growth."""
    from canonical_stats import _COUNTRY_NAME
    known = set(_COUNTRY_NAME.values())
    assert len(known) >= LIVE_COUNTRIES_2026_09_04, (
        f"the name map now spans {len(known)} countries, below the "
        f"{LIVE_COUNTRIES_2026_09_04} measured live on 2026-09-04. Countries "
        f"were REMOVED from the map — re-verify against "
        f"/api/v1/dcpi/scores before lowering this floor."
    )
    for c in ("Mexico", "Brazil", "South Africa", "India", "South Korea",
              "Taiwan", "Hong Kong", "Malaysia", "Indonesia", "Thailand",
              "Vietnam", "Philippines", "New Zealand"):
        assert c in known, f"{c} is scored live and unreachable from the map"


# --------------------------------------------------------------------------
# 3. the phrase and the floor
# --------------------------------------------------------------------------

def test_region_phrase_is_derived_from_the_countries_it_is_given():
    """_regions_for() must follow the set, not a literal."""
    from canonical_stats import _regions_for
    assert _regions_for({"United States"}) == ("North America",)
    assert _regions_for({"United States", "Japan"}) == ("North America",
                                                        "Asia-Pacific")
    # Reading order, not insertion order, and only regions actually present.
    assert _regions_for({"Australia", "Brazil", "Germany"}) == (
        "Latin America", "Europe", "Asia-Pacific")
    # An unknown country contributes no region rather than a wrong one.
    assert _regions_for({"Atlantis"}) == ()


def test_pinned_region_floor_is_a_subset_not_a_hand_walked_copy():
    """A floor on a SET is a SUBSET.

    PINNED['public']['dcpi_regions'] is the cold-start fallback. Pinning the
    full live list would recreate the six-cycle hand-walk documented on
    PINNED['public']['facilities'] — and would OVER-claim the moment a region
    left the set. It must name only regions that are in the reading order and
    fewer of them than the live derivation resolves.
    """
    from ai_surface_canon import PINNED
    from canonical_stats import _REGION_ORDER, _COUNTRY_REGION

    pinned = PINNED["public"]["dcpi_regions"]
    named = [r for r in _REGION_ORDER if r in pinned]
    assert named, f"pinned region phrase names no known region: {pinned!r}"
    assert len(named) < len(set(_COUNTRY_REGION.values())), (
        f"the pin names {len(named)} regions and the live map spans "
        f"{len(set(_COUNTRY_REGION.values()))}. The pin must UNDER-claim; "
        f"canonical_stats' derivation is what publishes the full set."
    )


def test_country_floor_rounds_down():
    from ai_surface_canon import PINNED
    from canonical_stats import _FALLBACK, _countries_floor
    assert _countries_floor(LIVE_COUNTRIES_2026_09_04) == "30+"
    assert _FALLBACK["dcpi_countries"] <= LIVE_COUNTRIES_2026_09_04, (
        "the cold-start floor is above the measured live span — the "
        "canonical_floor_above_live_reality failure this module exists to stop"
    )
    assert PINNED["public"]["dcpi_countries"] == _countries_floor(
        _FALLBACK["dcpi_countries"]), (
        "the pin and the floor round the same number two different ways")


# --------------------------------------------------------------------------
# 4. the manifest itself carries no region list
# --------------------------------------------------------------------------

BANNED_IN_HANDLER = (
    # the exact stale list, and each region name it hand-typed
    "Japan, Australia, Singapore",
    "UK, EU,",
    "and Canada.",
)


@pytest.mark.parametrize("literal", BANNED_IN_HANDLER)
def test_handler_carries_no_hardcoded_region_list(literal):
    code = _handler_code()
    assert literal not in code, (
        f"the ai-agents.json handler hand-types {literal!r} again. The region "
        f"span is derived — {{canon_dcpi_regions}} — because a list beside a "
        f"canon-derived count is what went stale here."
    )


def test_description_binds_the_region_span_to_canon():
    code = _handler_code()
    start = code.find('"description": _canon_text(')
    assert start != -1, "ai-agents.json description not found"
    desc = code[start:code.find("),", start)]
    for ph in ("{canon_markets}", "{canon_dcpi_countries}",
               "{canon_dcpi_regions}"):
        assert ph in desc, (
            f"the description no longer binds {ph}. Every span it states must "
            f"come from the canon; the count already did and the regions did "
            f"not, which is how the two disagreed."
        )


def test_international_markets_is_derived_with_the_launch_set_as_fallback():
    code = _handler_code()
    start = code.find('"international_markets"')
    assert start != -1, "dcpi_coverage.international_markets not found"
    block = code[start:start + 3000]
    assert "_live_intl_markets()" in block, (
        "international_markets is a hand-written array again. It froze at the "
        "2026-05-25 launch set (10 countries, 16 markets) against a live 35 "
        "non-US countries, and it is the MACHINE-READABLE half — an agent "
        "asking whether Querétaro is scored parses this and gets no."
    )
    # The launch set survives ONLY as the cold-cache fallback, never alone.
    assert code.find("_live_intl_markets()", start) < code.find('"Frankfurt"', start), (
        "the hardcoded launch set must sit behind the derivation, not in "
        "front of it")


def test_canon_exposes_both_placeholders():
    """The placeholders must exist, or every surface has to hardcode again.

    That is the lesson canonical_stats._PUBLIC_FLOOR_SPECS records for
    `substations`: a value with a snapshot key but no floor spec had no
    {canon_*} route to any surface, so /.well-known/mcp.json hardcoded the
    DB-down seed and served it as a measurement for months.
    """
    from ai_surface_canon import canon_nums
    nums = canon_nums()
    for ph in ("{canon_dcpi_countries}", "{canon_dcpi_regions}"):
        assert ph in nums, f"{ph} is not resolvable by canon_nums()"
        assert nums[ph], f"{ph} resolved empty; the pin should have stood"


def test_main_fallback_canon_nums_offers_the_same_keys():
    """main._canon_nums() is the degraded twin used when ai_surface_canon
    cannot be imported. A key it lacks renders a literal {canon_*} into a
    public manifest."""
    tree = ast.parse(_source())
    ns: dict = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_canon_nums":
            exec(compile(ast.Module([node], []), "<main-helpers>", "exec"), ns)
    assert "_canon_nums" in ns, "main.py no longer defines _canon_nums"
    keys = set(ns["_canon_nums"]())
    for ph in ("{canon_dcpi_countries}", "{canon_dcpi_regions}"):
        assert ph in keys, (
            f"main._canon_nums() cannot resolve {ph}; a degraded import would "
            f"publish the raw placeholder text")


# --------------------------------------------------------------------------
# 5. one operator->country map, not two
# --------------------------------------------------------------------------

def test_canonical_stats_declares_no_operator_country_map():
    """canonical_stats must RESOLVE, never re-declare.

    The first draft of this change added an ISO_COUNTRY dict to
    util/iso_taxonomy.py while routes/dcpi.py already carried
    _ISO_LABEL_COUNTRY — two operator->country maps, which is precisely the
    defect util/iso_taxonomy.py's own docstring opens with ("FOUR divergent
    copies of a state->ISO map"), one column over. The existing
    test_no_second_state_to_iso_map_in_the_tree fence did NOT catch it: it
    matches "XX": "ISOLABEL" pairs, and an operator->country map is the other
    orientation.

    canonical_stats may hold a code->NAME map (presentation) and a
    country->REGION map (presentation). It may not hold a map keyed by grid
    operator — that is a second answer to a question routes/dcpi.py already
    answers.
    """
    import re
    import canonical_stats as cs
    from routes.dcpi import _ISO_LABEL_COUNTRY

    operators = set(_ISO_LABEL_COUNTRY)
    src = open(cs.__file__, encoding="utf-8").read()
    offenders = {}
    for m in re.finditer(r"^(_?[A-Z][A-Z0-9_]*)\s*=\s*\{(.*?)^\}", src, re.S | re.M):
        name, body = m.group(1), m.group(2)
        keys = set(re.findall(r"[\"']([A-Za-z][A-Za-z0-9-]{1,12})[\"']\s*:", body))
        hits = keys & operators
        if len(hits) >= 3:
            offenders[name] = sorted(hits)[:6]
    assert not offenders, (
        f"canonical_stats declares a map keyed by grid operator: {offenders}. "
        f"Import routes.dcpi.market_country instead — a second operator map "
        f"is the drift this whole module exists to end."
    )


def test_the_derivation_binds_to_the_shipped_resolver():
    """...and binds to it by NAME, so the fence above cannot be satisfied by
    inlining the lookup instead of declaring it."""
    import canonical_stats as cs
    src = open(cs.__file__, encoding="utf-8").read()
    assert "from routes.dcpi import market_country" in src, (
        "canonical_stats no longer binds routes/dcpi.py's country resolver. "
        "If it moved, follow it — do not re-derive the map here."
    )


# --------------------------------------------------------------------------
# 6. /dcpi — the flagship page called the whole index American
# --------------------------------------------------------------------------

DCPI_PAGE = os.path.join(REPO, "routes", "dcpi.py")


def _dcpi_source() -> str:
    with open(DCPI_PAGE, encoding="utf-8") as fh:
        return fh.read()


def _dcpi_code() -> str:
    """routes/dcpi.py with comments AND docstrings blanked.

    The sweep below must read CODE, not prose. Written against the raw source
    it failed on dcpi_index_country_count's own docstring, which quotes the
    string it removed — the fence would have been arguing with its own
    documentation, and the only way to keep it green would have been to stop
    naming the bug. Same lesson as
    tests/test_ai_agents_manifest_canon_bound.py::_handler_code, one rung
    further: that handler explains itself in comments, this module in
    docstrings.

    Assigned strings SURVIVE on purpose — DCPI_INDEX_TEMPLATE and the press-kit
    HTML are exactly where the four false claims lived.
    """
    lines = _dcpi_source().splitlines()
    tree = ast.parse("\n".join(lines))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            for i in range(first.lineno - 1, (first.end_lineno or first.lineno)):
                lines[i] = ""
    return "\n".join(ln for ln in lines
                      if not ln.lstrip().startswith(("#", "<!--")))


def test_dcpi_page_does_not_call_the_whole_index_american():
    """The count derived; the geography was typed.

    Live on 2026-09-03 the page rendered "323+ U.S. data center markets" in
    four places — the description meta, og:description, the JSON-LD Dataset
    description and the lede — plus the OG card and the press kit. 255 of 323
    scored markets are US; the other 68 span 35 countries. Comments are
    stripped for the same reason the ai-agents fence strips them: the note
    explaining the fix quotes the string it removed.
    """
    code = _dcpi_code()
    # Non-vacuity: the template that carried the claims must still be in view.
    assert "DCPI_INDEX_TEMPLATE" in code and "{{ total_rows }}" in code, (
        "the comment/docstring stripper ate the page template — this fence "
        "would pass on any source at all")
    for literal in ("U.S. data center markets", "U.S. markets"):
        assert literal not in code, (
            f"routes/dcpi.py asserts {literal!r} again. The index is not "
            f"American — bind the span to dcpi_index_country_count(), or say "
            f"nothing about geography."
        )


def test_country_count_folds_territories_and_never_publishes_zero():
    from routes.dcpi import dcpi_index_country_count as n
    # PR/GU/VI are US territories: 4 Places, 2 countries.
    assert n([{"addressCountry": "US"}, {"addressCountry": "PR"},
              {"addressCountry": "GU"}, {"addressCountry": "BR"}]) == 2
    # Unmeasurable reads as unknown, NEVER as 0 — the reason /dcpi-v2 was
    # retired, and the template drops the clause on None.
    for empty in (None, [], [{}], [{"addressCountry": None}]):
        assert n(empty) is None, f"{empty!r} published a number instead of None"


def test_country_count_reads_the_places_the_page_already_resolved():
    """One resolution pass, so the prose and the JSON-LD on this page cannot
    disagree about how many countries there are."""
    import inspect
    from routes.dcpi import dcpi_index_country_count
    sig = inspect.signature(dcpi_index_country_count)
    assert list(sig.parameters) == ["spatial_coverage"], (
        "dcpi_index_country_count no longer takes the resolved Places. If it "
        "re-derives from rows, the page gains a second country definition — "
        "and _dcpi_index_coverage's isolated-namespace fence will catch the "
        "new globals, which is how this was found the first time."
    )


def test_the_page_binds_the_span_at_the_pre_tier_slice_call_site():
    """r-index-coverage-precap: a tier-capped teaser must never shrink a
    COVERAGE claim. The anon slice is all-US, so binding after the rebind
    would publish "1 country" to the crawlable surface."""
    src = _dcpi_source()
    bind = src.index("total_countries=dcpi_index_country_count(_spatial_cov)")
    spatial = src.index("_spatial_cov = dcpi_index_spatial_coverage(rows)")
    rebind = src.find("rows = ", spatial)
    assert spatial < rebind < bind, (
        "the country span must be computed from the pre-tier-slice row set "
        "(via _spatial_cov), not recomputed at the render call")
