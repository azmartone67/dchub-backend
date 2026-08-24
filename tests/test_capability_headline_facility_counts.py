"""tests/test_capability_headline_facility_counts.py — every announceable
headline quotes BUILDINGS, never the row pile.

THE BUG: brain_capability_radar's REGISTRY headlines read `tracked`
(COUNT(*) FROM discovered_facilities = raw source ROWS, ~1.4x buildings) and
called the result "facilities". Two surfaces published it:

  • 2026-08-17  data_milestone / facility_coverage posted "26,000 data-center
                facilities are now live in DC Hub's index, spanning 179
                countries" — the post routes/claim_breaker.py's rows_ne_buildings
                class was subsequently written to stop.
  • 2026-08-22  the reserved 16:00 capability slot (cap_agent_memory) was refused
    and 08-23   by that class, twice: "26,347/26,387 facilities exceeds live
                distinct buildings (18621)". The gate was right; the composer
                was not. FOUR MORE cards carried the same string and would have
                refused in turn — weekly_ledger is next in the rotation once
                agent_memory is publish-blocked, so the class was not
                self-healing, only self-limiting.

THE FIX this pins: `_canonical_stats()` publishes `distinct` — byte-for-byte
the query media_fact_check_guard.check_facility_count_claims measures against —
and every "N facilities" claim reads it. The row pile appears only as "source
records".

WHY A FENCE AND NOT JUST THE EDIT: the gate reads the FINAL composed text, so it
catches this only at 16:00, in production, as a refusal. This runs the REAL gate
over every rendered headline at commit time, and its `_milestone` default is
adversarial (the row pile), so a NEW milestone source that quotes rows as
facilities fails here rather than on LinkedIn.

CI-SAFETY: pure. The registry headlines are read with `ast` and evaluated in an
isolated namespace — routes.brain_capability_radar imports psycopg2 and is never
imported. routes.media_fact_check_guard is stdlib-only and always imports, so
the gate half really runs in the pytest-only unit-tests job. The card half needs
Flask (routes.og_cards) and skips there.

Run:  python3 -m pytest tests/test_capability_headline_facility_counts.py -v
"""
from __future__ import annotations

import ast
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

RADAR = os.path.join(ROOT, "routes", "brain_capability_radar.py")

# The live reading on 2026-08-23, the day of the second refusal — taken from the
# refusal text itself (distinct=18621) and the card URL the run emitted
# (v=18704&t=26387&m=320&dl=2145&c=178&tl=73). Replaying the failing day is the
# point: these numbers are what the gate actually saw.
DISTINCT, VERIFIED, RECORDS = 18621, 18704, 26387

# Every key any REGISTRY headline interpolates. A headline that reads a key not
# listed here raises KeyError and fails loudly — a new source cannot slip past
# this fence by inventing a field name.
LIVE = {
    # canonical (evergreen sources, check=_canonical_stats)
    "distinct": DISTINCT, "verified": VERIFIED, "tracked": RECORDS,
    "countries": 178, "markets": 320, "deals": 2145, "tools": 73,
    # metric_sql rows (launch + milestone sources)
    "n": RECORDS, "mw": 254000.0, "states": 48, "bas": 66, "engines": 9,
    "distinct_buildings": DISTINCT,
    # live `check` sources
    "core_at_1": 6.0, "terms_str": '"data center", "fiber", "capacity"',
}

# ★ ADVERSARIAL DEFAULT. mode="milestone" headlines interpolate `_milestone`,
# the crossed round number. Defaulting it to the ROW pile means a new milestone
# headline that writes "{_milestone} facilities" renders 26,387 and is refused
# HERE. A safe-looking default would make this fence vacuous for exactly the
# class it exists to catch.
MILESTONE_DEFAULT = RECORDS
MILESTONE = {
    "facility_coverage": 26000,      # floor(26,387 / 1,000) — a SOURCE-RECORD bucket
    "country_coverage": 170,         # floor(178 / 10)
    "dcpi_markets": 300,             # floor(320 / 25)
    "ai_citations": 40,
    "requests_served_total": 5000000,
}


def _registry_headline_fns():
    """{key: headline(row) -> str} for every REGISTRY source, WITHOUT importing
    the module (it imports psycopg2; the pytest-only CI job has no psycopg2).

    Each `headline` lambda is evaluated from its own source text in a namespace
    holding nothing but the builtins an f-string headline may legitimately use.
    """
    src = open(RADAR, encoding="utf-8").read()
    tree = ast.parse(src)
    registry = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "REGISTRY" for t in node.targets):
            registry = node.value
            break
    assert isinstance(registry, ast.List), "REGISTRY list not found in " + RADAR
    ns = {"__builtins__": {"int": int, "float": float, "len": len, "str": str,
                           "round": round, "abs": abs, "min": min, "max": max}}
    out = {}
    for elt in registry.elts:
        assert isinstance(elt, ast.Dict), "REGISTRY entry is not a dict literal"
        fields = {}
        for k, v in zip(elt.keys, elt.values):
            if isinstance(k, ast.Constant):
                fields[k.value] = v
        key = getattr(fields.get("key"), "value", None)
        assert key, "REGISTRY entry has no literal `key`"
        hl = fields.get("headline")
        assert hl is not None, "REGISTRY source %s has no headline" % key
        if isinstance(hl, ast.Constant):            # a plain string headline
            out[key] = (lambda text: (lambda _row: text))(str(hl.value))
            continue
        out[key] = eval(compile(  # noqa: S307 - evaluating our own source
            ast.Expression(ast.parse(ast.get_source_segment(src, hl),
                                     mode="eval").body),
            "<registry:%s>" % key, "eval"), ns)
    assert out, "no REGISTRY headlines found — fence would be vacuous"
    return out


HEADLINE_FNS = _registry_headline_fns()


def _render(key, live=None):
    """Render one REGISTRY headline against a pinned reading."""
    row = dict(live if live is not None else LIVE)
    row["_milestone"] = MILESTONE.get(key, MILESTONE_DEFAULT)
    try:
        return HEADLINE_FNS[key](row)
    except KeyError as e:
        raise AssertionError(
            "REGISTRY source %r reads row key %s, which this fence does not pin. "
            "Add it to LIVE with an HONEST value so the gate below can judge the "
            "rendered claim." % (key, e))


REGISTRY_HEADLINES = [(k, _render(k)) for k in HEADLINE_FNS]


@pytest.fixture(autouse=True)
def pin_live_counts(monkeypatch):
    """The gate sees the 2026-08-23 reading, so the verdict is deterministic."""
    from routes import media_fact_check_guard as g
    monkeypatch.setattr(g, "_live_facility_counts", lambda: (DISTINCT, RECORDS))


def _over(text):
    from routes.media_fact_check_guard import check_facility_count_claims
    return check_facility_count_claims(text or "").get("over") or []


# ── the fence ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,headline",
                         REGISTRY_HEADLINES,
                         ids=[k for k, _ in REGISTRY_HEADLINES])
def test_registry_headline_clears_the_rows_ne_buildings_gate(key, headline):
    """Every announceable headline passes the class that refused the 16:00 slot."""
    over = _over(headline)
    assert not over, (
        "brain_capability_radar REGISTRY source %r publishes a row count as "
        "buildings — claim-breaker rows_ne_buildings will refuse the post:\n"
        "  headline: %s\n  over: %s\n"
        "Quote `distinct` (distinct buildings), or say 'source records' for the "
        "pile." % (key, headline, [c.get("raw") for c in over]))


def test_the_refused_08_23_copy_still_fails():
    """Two-sided: the gate + the pinning above really can refuse.

    Verbatim the string the 16:00 slot was refused for. If this ever passes, the
    fence above has gone vacuous and proves nothing.
    """
    refused = ("%s facilities are now saveable to a durable, per-agent shortlist"
               % format(RECORDS, ","))
    assert _over(refused), (
        "the rows_ne_buildings gate no longer refuses the verbatim 2026-08-23 "
        "copy — every assertion in this file is now vacuous")


def test_facility_coverage_milestone_is_labelled_source_records():
    """The source that wrote the 2026-08-17 post fires on a ROW bucket.

    Its value_key stays "n" on purpose (data_milestone_snapshots holds a
    row-scale baseline; re-pointing it at the distinct count would mute the
    source, not fix it), so the crossed number MUST be called source records and
    the citeable building count must appear beside it.
    """
    hl = dict(REGISTRY_HEADLINES)["facility_coverage"]
    assert "source records" in hl.lower(), (
        "facility_coverage crosses a raw-row bucket; the copy must say so: " + hl)
    assert format(DISTINCT, ",") in hl, (
        "facility_coverage must also carry the citeable distinct-building "
        "count: " + hl)


def test_no_facility_claim_is_derived_from_a_row_pile_key():
    """The gate has a 5% tolerance, so a row count that happens to sit just under
    the ceiling passes it while still being rows sold as buildings — on
    2026-08-23 `verified` was 18,704 against a distinct 18,621, over by 0.4%.

    So test the PROPERTY, not the magnitude: re-render every headline with the
    two definitionally-row-pile keys poisoned to an absurd value. A facility
    claim that moves with them is derived from a row pile, whatever today's
    reading happens to be. Copy that names them as source records is untouched,
    because "999,999 source records" raises no facility claim at all.
    """
    poisoned = dict(LIVE)
    poisoned["tracked"] = poisoned["verified"] = 999999
    bad = []
    for key, _ in REGISTRY_HEADLINES:
        rendered = _render(key, poisoned)
        if _over(rendered):
            bad.append("%s: %s" % (key, rendered))
    assert not bad, (
        "a facility claim moves when `tracked`/`verified` move — those keys are "
        "row piles (COUNT(*) and keeper rows), never buildings. Quote `distinct`, "
        "or name them as source records:\n  " + "\n  ".join(bad))


# ── the two SQL strings the whole design rests on ────────────────────────────

# check_facility_count_claims measures this copy against
# canonical_stats.get_canonical_stats()["facilities_verified"]. The radar runs
# its OWN connection, so the only thing keeping composer and gate on the same
# scale is that they run the SAME query. Drift here re-opens the bug with both
# sides looking correct in isolation.
_DISTINCT_SQL = ("SELECT COUNT(DISTINCT canonical_slug) FROM discovered_facilities "
                 "WHERE COALESCE(is_duplicate,0)=0 AND canonical_slug IS NOT NULL")


def _sql_literals(path):
    """Every string constant in a module, whitespace-normalised. Adjacent string
    literals are folded by the parser, so a query split across source lines
    arrives here as one string."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    return {" ".join(n.value.split()) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def test_radar_and_gate_count_buildings_with_the_same_query():
    """The composer's ceiling and the gate's ceiling must be one query."""
    radar = _sql_literals(RADAR)
    canon = _sql_literals(os.path.join(ROOT, "canonical_stats.py"))
    assert _DISTINCT_SQL in canon, (
        "canonical_stats.py no longer runs the distinct-building query this "
        "fence pins — check_facility_count_claims' ceiling moved; re-point BOTH "
        "sides together, never one")
    assert _DISTINCT_SQL in radar, (
        "brain_capability_radar._canonical_stats no longer runs the same "
        "distinct-building query as canonical_stats.py. Composer and gate are "
        "now measuring different things, which is how 26,387 was published as "
        "facilities in the first place")


def test_canonical_stats_publishes_the_distinct_key():
    """`distinct` missing from the out dict is a SILENT death, not a loud one:
    the headline lambdas KeyError, capability_radar_leads() catches per source
    and logs 'source skipped', and the 16:00 card just stops appearing."""
    tree = ast.parse(open(RADAR, encoding="utf-8").read())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_canonical_stats"), None)
    assert fn, "_canonical_stats not found in " + RADAR
    keys = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict):
            keys |= {k.value for k in node.keys
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    for needed in ("distinct", "tracked", "countries", "markets", "deals"):
        assert needed in keys, (
            "_canonical_stats() stopped publishing %r — every evergreen headline "
            "reading it would KeyError and be swallowed as 'source skipped'"
            % needed)


# ── the OG data-card: the same numbers, rendered into a PNG the gate can't read ─

try:
    from routes.og_cards import _dc_spec as _DC_SPEC
    _HAVE_OG = True
except Exception:  # pragma: no cover - pytest-only CI has no Flask
    _DC_SPEC = None
    _HAVE_OG = False

_needs_og = pytest.mark.skipif(not _HAVE_OG, reason="routes.og_cards needs Flask")

_CARD_KINDS = ["provenance_envelope", "intl_grid_telemetry", "agent_memory",
               "error_envelope", "tool_catalog", "weekly_ledger"]
_CARD_NUMS = {"d": DISTINCT, "v": VERIFIED, "t": RECORDS, "m": 320,
              "dl": 2145, "c": 178, "tl": 73}


@_needs_og
@pytest.mark.parametrize("kind", _CARD_KINDS)
def test_card_slot_labelled_facilities_carries_the_building_count(kind):
    """A card slot that says "facilities" must render `d`, never `t` or `v`.

    The claim-breaker reads the post TEXT; the card is an image, so a row count
    published here is invisible to the gate and reaches LinkedIn unchallenged.
    """
    spec = _DC_SPEC(kind, dict(_CARD_NUMS))
    assert spec, "no card spec for cap_%s" % kind
    slots = [(spec.get("unit"), spec.get("number"))]
    for st in (spec.get("stats") or []):
        slots.append((st.get("label"), st.get("n")))
    checked = 0
    for label, shown in slots:
        if not label or "facilit" not in str(label).lower():
            continue
        checked += 1
        assert shown == format(DISTINCT, ","), (
            "cap_%s renders %r in a slot labelled %r — that is a row pile "
            "(records=%s, keeper rows=%s) published as buildings, into an "
            "image the text gate cannot read."
            % (kind, shown, label, format(RECORDS, ","), format(VERIFIED, ",")))
    if kind in ("provenance_envelope", "error_envelope", "weekly_ledger"):
        assert checked, ("cap_%s no longer labels any slot 'facilities' — if the "
                         "card was re-cut, re-point this fence at the new slot "
                         "rather than deleting it" % kind)
