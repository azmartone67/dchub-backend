"""Named fiber carriers on the facility profile — and the total that must never ship.

r-fiber-names (2026-09-12). /api/v1/facilities/<slug>, the URL this page links
in its own FOOTER, has published fiber_carrier_count / on_net / fiber_providers
since 2026-07-17. Measured over 1,000 live pages sampled from the sitemap in
three independent draws on 2026-09-12, 31.2% carry named carriers (per-draw
27.2% / 28.5% / 37.1%) and the page rendered none of them.

★★★ THE COUNT IS A CO-LOCATION MEASURE, NOT A CONTRACT. Measured live the same
day, and this is the whole reason these tests exist:

    LADC4 - 530 W 6th St    34.048428,-118.25519  -> 477 carriers
    TurnKey Internet - CA   34.048336,-118.25508  -> 477 carriers

Two DIFFERENT facilities 13 m apart, byte-identical carrier lists. The number
describes One Wilshire, not TurnKey. Lunavi - Westin1809 reports 346 — the
Westin Building Exchange's list. The Anthropic New York record reported 263
"on-site" carriers purely because its coordinates were a Manhattan placeholder.

So the section renders NAMES and NEVER A TOTAL, and the guard for that is
structural as well as textual: no COUNT is fetched, so there is no total in the
process for a later edit to print.

These tests compile the two functions out of the AST into a bare namespace and
drive them against a stubbed DB — the pattern
tests/test_facility_nearby_generation.py established, including its
sys.modules["main"] save/restore (a fake `main` leaking out of this file is how
ten unrelated tests went red in the full suite once already).

Run:  python3 -m pytest tests/test_facility_fiber_connectivity.py -v
"""

import ast
import pathlib
import re
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "routes" / "facility_profile_page.py"
TEXT = SRC.read_text()
TREE = ast.parse(TEXT)

# A real slice of what the API returns for Lunavi - Westin1809 (Westin Building
# Exchange, Seattle) on 2026-09-12. Real names, because the renderer has to
# survive the punctuation real carrier names carry.
WESTIN = ["617A Corporation", "AARNet", "ACE CDN", "AEBC Internet Corp.",
          "ALLHOSTSHOP", "AS54444 s.r.o.", "AS8882", "ATGBB",
          "Academy City Internet", "Access Communications Co-operative",
          "Adobe Systems", "Advanced Communications Technology"]

SEATTLE = {"id": 12345, "_src_table": "discovered_facilities",
           "provider": "Lunavi Inc", "name": "Lunavi - Westin1809",
           "latitude": 47.614346, "longitude": -122.33888}


class _Cur:
    def __init__(self, rows, boom=False):
        self.rows, self.boom = rows, boom
        self.sql = self.params = None
        self.calls = 0

    def execute(self, q, p=None):
        self.calls += 1
        self.sql, self.params = q, p
        if self.boom:
            raise RuntimeError("db down")

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows, boom=False):
        self.c = _Cur(rows, boom)
        self.closed = False

    def cursor(self):
        return self.c

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _restore_main_module():
    """Put sys.modules["main"] back after every test in this file.

    Not housekeeping — see the docstring on the same fixture in
    tests/test_facility_nearby_generation.py. A fake `main` that survives this
    file is handed to whatever runs next alphabetically."""
    saved = sys.modules.get("main")
    had = "main" in sys.modules
    try:
        yield
    finally:
        if had:
            sys.modules["main"] = saved
        else:
            sys.modules.pop("main", None)


def _fn(name):
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} not found in {SRC}")


def _const(name):
    for n in TREE.body:
        if isinstance(n, ast.Assign) and any(
                getattr(t, "id", None) == name for t in n.targets):
            return ast.literal_eval(n.value)
    raise AssertionError(f"{name} not found in {SRC}")


CAP = _const("_FIBER_NAME_CAP")
KIN_DEG = _const("_FIBER_KIN_DEG")


def _load(carrier_names, boom=False, conn_none=False):
    """Return (fetch, render, conn). Rows are 1-tuples, as the real cursor gives."""
    rows = [(n,) for n in carrier_names]
    conn = None if conn_none else _Conn(rows, boom)
    fake = types.ModuleType("main")
    fake.get_read_db = lambda: conn
    sys.modules["main"] = fake
    ns = {
        "_esc": _real_esc(),
        "_FIBER_NAME_CAP": CAP,
        "_FIBER_KIN_DEG": KIN_DEG,
        "logger": types.SimpleNamespace(warning=lambda *a, **k: None),
    }
    for name in ("_fiber_carrier_names", "_fiber_connectivity_html"):
        exec(compile(ast.Module(body=[_fn(name)], type_ignores=[]),  # noqa: S102
                     str(SRC), "exec"), ns)
    return ns["_fiber_carrier_names"], ns["_fiber_connectivity_html"], conn


def _real_esc():
    """The page's own escaper, not a stub — an escaping test against a stub
    that returns str(x) unchanged proves nothing."""
    ns = {}
    exec(compile(ast.Module(body=[_fn("_esc")], type_ignores=[]),  # noqa: S102
                 str(SRC), "exec"), ns)
    return ns["_esc"]


def _visible(html):
    return re.sub(r"<[^>]+>", " ", html)


def _chips(html):
    return re.findall(r'<span class="chip fiber-chip">(.*?)</span>', html)


# ── it renders, with names ──────────────────────────────────────────────────

def test_section_renders_named_carriers_not_a_headline_number():
    """The reason the section exists: named networks are the fact that separates
    two colocation halls in the same city. A count cannot do that, and for
    co-located rows the count is not even about this building."""
    fetch, render, conn = _load(WESTIN)
    names = fetch(SEATTLE)
    out = render(names, "Lunavi - Westin1809")
    assert "Fiber connectivity" in out
    assert conn.c.calls == 1, "one query per page, like _nearby_generation_rows"
    for carrier in ("AARNet", "Adobe Systems", "Academy City Internet"):
        assert carrier in out, f"{carrier} is in the data and must be named"
    assert "Lunavi - Westin1809" in out
    assert len(_chips(out)) == len(WESTIN)


def test_prose_says_presence_not_cross_connect():
    """A reader must not take this for a contracted cross-connect list. The
    label is the only thing standing between a co-location measure and a
    procurement claim, so it is asserted, not left to review."""
    _, render, _ = _load(WESTIN)
    out = render(WESTIN, "Lunavi - Westin1809").lower()
    assert "cross-connect" in out, "the section must name what it is NOT"
    assert "peeringdb" in out, "the source must be on the page"
    assert "present at or immediately around" in out


def test_carrier_names_are_escaped_with_the_pages_own_escaper():
    """Carrier names are third-party strings from PeeringDB, rendered into
    HTML. `AS54444 s.r.o.` is benign; the next ingest may not be."""
    _, render, _ = _load([])
    out = render(['<script>alert(1)</script>', "Bob & Sons"], "X")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp;" in out


# ── it refuses to render an empty shell ─────────────────────────────────────

def test_no_carriers_renders_nothing_at_all():
    """An empty header promises a fact the page does not have. main.py defines
    on_net as carrier count > 0, so an empty list IS the on_net=false case —
    there is deliberately no second on_net switch here that could disagree with
    the names actually in hand."""
    fetch, render, conn = _load([])
    names = fetch(SEATTLE)
    assert names == []
    assert render(names, "X") == ""
    assert render(None, "X") == ""


def test_blank_carrier_names_are_not_a_section():
    """`carrier_name <> ''` in SQL does not catch a whitespace-only name; a row
    of those would otherwise render a header over a strip of empty pills."""
    fetch, render, _ = _load(["", "   ", None])
    assert fetch(SEATTLE) == []
    assert render(["  ", ""], "X") == ""


def test_a_query_error_costs_the_section_never_the_page():
    """Fail-soft is the contract this section shares with every other block on
    the page: it returns '', it does not raise into _render_profile."""
    fetch, render, _ = _load(WESTIN, boom=True)
    assert fetch(SEATTLE) == []
    assert render(fetch(SEATTLE), "X") == ""
    fetch2, _, _ = _load(WESTIN, conn_none=True)
    assert fetch2(SEATTLE) == []


# ── the no-coordinates decision ─────────────────────────────────────────────

def test_no_coordinates_means_no_section_and_no_query():
    """★ THE DECISION. A facility with no coordinate can only have acquired
    carriers by INHERITANCE: carrier_facility_ingestion skips candidates with no
    lat/lng (`if cand['lat'] and cand['lng']`), so the direct link can never
    have been made, and only the API union's "sameness cannot be disproven"
    branch remains. Measured over 1,000 live pages (2026-09-12, three draws):
    513 carry no usable coordinate and exactly TWO of them report any carrier —
    2 pages in 1,000, 0.64% of the sections that would otherwise render.

    Asserted as "no query ran", not merely "no names": a refusal that still
    opens a connection would be indistinguishable from an empty result."""
    fetch, render, conn = _load(WESTIN)
    fac = dict(SEATTLE, latitude=None, longitude=None)
    assert fetch(fac) == []
    assert conn.c.calls == 0, "the gate must fire BEFORE any DB work"
    assert render(fetch(fac), "X") == ""


def test_null_island_is_a_missing_coordinate_not_a_place():
    """0,0 passes both range checks — 0 is a legal latitude AND longitude — so
    the ingestion placeholder would otherwise read as a real position. The rule
    is owned by routes.provenance.normalize_coordinates and REUSED here rather
    than copied a fourth time."""
    fetch, _, conn = _load(WESTIN)
    assert fetch(dict(SEATTLE, latitude=0.0, longitude=0.0)) == []
    assert conn.c.calls == 0


def test_a_real_equatorial_coordinate_still_renders():
    """The 0,0 rule is "both together", not "either". lat=0 with a real
    longitude is Gabon/Ecuador/Kenya and must keep its section — a guard that
    swallowed those would be satisfied by blanket-nulling everything."""
    fetch, _, conn = _load(WESTIN)
    got = fetch(dict(SEATTLE, latitude=0.0, longitude=9.45))
    assert got == WESTIN
    assert conn.c.calls == 1


def test_unusable_coordinates_are_refused():
    """A string, a NaN-ish value or an out-of-range pair reaches this from the
    DB as readily as a good one."""
    fetch, _, conn = _load(WESTIN)
    for bad in ({"latitude": "n/a", "longitude": "n/a"},
                {"latitude": 91.0, "longitude": 10.0},
                {"latitude": 10.0, "longitude": 181.0}):
        assert fetch(dict(SEATTLE, **bad)) == []
    assert conn.c.calls == 0


# ── the cap ─────────────────────────────────────────────────────────────────

def test_cap_is_asserted_on_the_rendered_output_not_on_the_constant():
    """★ The cap lives in TWO places — the SQL LIMIT and the render slice — so
    it is asserted where a reader sees it: the number of chips in the HTML. A
    test that only read _FIBER_NAME_CAP would survive a renderer that ignored
    it entirely."""
    many = [f"Carrier {chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(60)]
    _, render, _ = _load([])
    out = render(many, "Big Building")
    assert len(_chips(out)) == CAP, f"expected {CAP} chips, got {len(_chips(out))}"


def test_the_fetch_asks_for_one_more_than_it_shows():
    """The overflow sentinel. Fetching cap+1 is how the section learns that more
    exist WITHOUT learning how many — the only way to be honest about what is
    hidden while keeping a total structurally out of reach."""
    fetch, _, conn = _load(WESTIN)
    fetch(SEATTLE)
    assert conn.c.params[-1] == CAP + 1, (
        f"SQL LIMIT must be {CAP + 1}, got {conn.c.params[-1]}")


def test_overflow_is_announced_without_a_number_and_only_when_real():
    """p90 of the rendering set is 111 carriers and the max is 648. What is
    hidden must be admitted — but "and 636 more" is the headline number this
    whole section refuses to print."""
    _, render, _ = _load([])
    over = render([f"Carrier {i:03d}x" for i in range(CAP + 1)], "X")
    assert "Further networks are recorded" in over
    exact = render([f"Net{chr(65 + i)}" for i in range(CAP)], "X")
    assert "Further networks are recorded" not in exact, (
        "nothing is hidden, so nothing may be implied")


# ── no bare total, by construction ──────────────────────────────────────────

def test_no_bare_total_reaches_the_html():
    """★★★ THE ONE THAT MATTERS. Rendered with digit-free names, the section's
    own prose must contain NO digit at all — not the shown count, not the
    hidden count, not the total. Asserted as "no digits" rather than "477 not
    in out": a literal ban passes vacuously the moment the wording changes, and
    fails wrongly when the banned number is a substring of another."""
    _, render, _ = _load([])
    names = [f"Carrier {chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(60)]
    out = render(names, "Big Building")
    stripped = re.sub(r'<span class="chip fiber-chip">.*?</span>', " ", out)
    digits = re.findall(r"\d", _visible(stripped))
    assert not digits, f"a number reached the section's prose: {digits}"
    assert "60" not in _visible(stripped) and "48" not in _visible(stripped)


def test_the_total_is_never_even_fetched():
    """Structural, not textual. There is no COUNT in the query, so no total
    exists in the process — a later edit cannot print one without first adding
    the thing this section was built to withhold."""
    src = ast.get_source_segment(TEXT, _fn("_fiber_carrier_names")).upper()
    assert "COUNT(" not in src, "the count must not be fetched at all"
    assert "FIBER_CARRIER_COUNT" not in src


def test_renderer_is_pure_and_opens_no_connection():
    """A page renderer must not open a connection — the separation that turned
    ten unrelated tests red when _nearby_generation_rows lived in the renderer."""
    src = ast.get_source_segment(TEXT, _fn("_fiber_connectivity_html"))
    for banned in ("get_read_db", "cursor", "execute", "import main"):
        assert banned not in src, f"{banned} does not belong in a renderer"


# ── the query shape the co-location argument depends on ─────────────────────

def test_kin_are_scoped_by_coordinates_with_no_cannot_be_disproven_branch():
    """The API unions carriers across twins AND across rows with absent
    coordinates, because sameness "cannot be disproven" there. This query keeps
    the co-located union and drops that branch — the page is a subset of the
    API, never a superset."""
    src = ast.get_source_segment(TEXT, _fn("_fiber_carrier_names"))
    assert "latitude IS NOT NULL AND longitude IS NOT NULL" in src
    assert "latitude IS NULL" not in src, (
        "an absent-coordinate disjunct is exactly what this drops")
    fetch, _, conn = _load(WESTIN)
    fetch(SEATTLE)
    assert KIN_DEG in conn.c.params, "the co-location window must be bound, not inlined"


def test_the_rows_own_id_is_always_in_the_kin_set():
    """The slug's frozen hash and a live MD5(provider|name) can DRIFT apart —
    measured: slug lunavi-inc-lunavi-westin1809-b5d054ae against a live hash of
    da81130d. Siblings are matched on the row's OWN provider|name and the row's
    own id is UNIONed in unconditionally, so a drifted hash degrades to "this
    row only", never to "no carriers"."""
    fetch, _, conn = _load(WESTIN)
    fetch(SEATTLE)
    assert str(SEATTLE["id"]) in conn.c.params
    assert SEATTLE["provider"] in conn.c.params
    assert SEATTLE["name"] in conn.c.params
    assert "UNION" in conn.c.sql


def test_the_slug_hash_has_exactly_one_spelling_and_it_is_the_canonical_one():
    """★ routes/facility_profile_page.py is FORBIDDEN to import stable_hash8 —
    tests/test_route_slug_compose_delegation.py bans it, because that import is
    the signature of the local slug composer r-routeslug deleted, and a reader
    cannot tell a hash used for LOOKUP from one used to MINT a slug. That guard
    caught this function on its first draft. The fix was not to weaken it: both
    sides of the comparison are built by routes.facility_slug.hash_sql, so this
    file carries no second spelling of the expression to drift."""
    src = ast.get_source_segment(TEXT, _fn("_fiber_carrier_names"))
    # The rule is about the IMPORT and the CALL, not the word — the comment
    # explaining why the import is banned has to be allowed to name it.
    assert "import stable_hash8" not in src, "the banned import must not return"
    assert "stable_hash8(" not in src, "nor a call to it under another name"
    assert "hash_sql" in src, "the canonical helper must be the source"
    assert "MD5(" not in src.upper(), \
        "a hand-written MD5 here would be a second spelling of the canon"
    fetch, _, conn = _load(WESTIN)
    fetch(SEATTLE)
    from routes.facility_slug import hash_sql
    assert hash_sql("s") in conn.c.sql and hash_sql("me") in conn.c.sql, (
        "both sides of the sibling match must be the helper's own expression")


def test_only_known_tables_can_reach_the_sql():
    """The table name cannot be parameterised, so it is whitelisted. A row
    carrying an unexpected _src_table gets no section, not a crafted query."""
    fetch, _, conn = _load(WESTIN)
    assert fetch(dict(SEATTLE, _src_table="pg_shadow; DROP TABLE x")) == []
    assert conn.c.calls == 0
    for good in ("discovered_facilities", "facilities"):
        f2, _, c2 = _load(WESTIN)
        assert f2(dict(SEATTLE, _src_table=good)) == WESTIN
        assert good in c2.c.sql
