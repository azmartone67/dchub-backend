#!/usr/bin/env python3
"""tests/test_facilities_hub_stored_slug.py — the geography hub must link the
URL the facility page actually SERVES, which is the row's frozen
discovered_facilities.canonical_slug.

NO NETWORK, NO DB.

r-hubslug (2026-09-05). facilities_hub._fac_slug was one more hand-rolled copy
of the facility slug composer — the one the r-routeslug sweep missed — and its
docstring claimed it was "byte-identical to the sitemap + live pages". Measured
against the freeze on the live German hub, 9 of 60 sampled links (15%) were
301s, in three defect classes:

  1. NO PROVIDER-PREFIX DEDUPE — datacenter-one-datacenter-one-dus1-c2e36834
     301 -> datacenter-one-dus1-c2e36834 (5 of the 9).
  2. NO ASCII FOLD — the local _slugify keeps the unicode word class, so
     …darz-darmstädter… was emitted where the page serves …darz-darmstdter…
     (4 of the 9).
  3. `len(name_slug) < 3` — PR #3911's bug, still resident here. _rows_to_facs
     skips a None slug, so the 28 one- and two-character facilities were
     dropped from the hub listing AND from the US state counts, silently, even
     though /facilities/oi-c40c6b79 has served 200 since the freeze rescued it.

★★ THE TRAP THIS FILE EXISTS TO HOLD SHUT. The obvious "fix" — delegate to
   build_canonical_slug(provider, name) — makes it WORSE. canonical_slug is
   set-once and FORWARD-ONLY: for rows frozen before the 2026-07-28 dedupe the
   DOUBLED form IS the live URL. Verified 2026-09-05 through the edge:

       /facilities/equinix-equinix-hk1-a3e2c448   200
       /facilities/equinix-hk1-a3e2c448           301 -> the doubled form
       /facilities/ntt-ntt-frankfurt-0b99120e     200
       /facilities/ntt-frankfurt-0b99120e         301 -> the doubled form

   facility_slug_freeze measured 45.7% of 5,064 frozen rows carrying the
   doubling, so a naive delegation would move roughly half the hub's links onto
   URLs that only redirect. Stored first; build only for unfrozen rows.
"""
import ast
import io
import json
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if "main" not in sys.modules:                                    # noqa: E402
    sys.modules["main"] = types.SimpleNamespace(
        get_read_db=lambda: None, get_db=lambda: None)

from flask import Flask                                          # noqa: E402

import facilities_hub as fh                                      # noqa: E402
import routes.seo_pages as seo                                   # noqa: E402
from routes.facility_slug_freeze import build_canonical_slug     # noqa: E402

HUB = os.path.join(ROOT, "facilities_hub.py")
SRC = io.open(HUB, encoding="utf-8").read()
TREE = ast.parse(SRC)


# ── source helpers ───────────────────────────────────────────────────────
def _fn(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("facilities_hub.%s no longer exists" % name)


def _code_of(name):
    """The function's source with its docstring and every comment line gone —
    so a prose mention of a retired form can never satisfy or defeat a check."""
    node = _fn(name)
    src = ast.get_source_segment(SRC, node) or ""
    doc = ast.get_docstring(node, clean=False)
    if doc:
        src = src.replace(doc, "", 1)
    return "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))


# ── fakes ────────────────────────────────────────────────────────────────
class _Cur:
    """Steps through canned result sets; answers the DDL probe out of band.

    ★ HONEST ABOUT THE SELECT LIST. A fake that hands back the stored slug no
      matter what the query asked for makes the whole file vacuous: mutating
      _canon_col to report the column permanently missing then recomposes every
      link in production while the tests stay green (measured — that mutation
      SURVIVED the first version of this file). So a query that selects
      `NULL AS canonical_slug` gets NULL back here too, exactly as Postgres
      would. The row's slug is a property of the SQL, not of the fixture.
    """

    def __init__(self, results, has_canon=True):
        self._results, self._has_canon = results, has_canon
        self._i, self._probed = -1, False
        self.last_sql = ""

    def execute(self, sql, *_a, **_k):
        if "information_schema.columns" in str(sql):
            self._probed = True
            return
        self.last_sql = str(sql)
        self._i += 1

    def fetchone(self):
        if self._probed:
            self._probed = False
            return (1,) if self._has_canon else None
        return None

    def fetchall(self):
        if not (0 <= self._i < len(self._results)):
            return []
        rows = self._results[self._i]
        if "NULL AS canonical_slug" in self.last_sql:
            return [r[:6] + (None,) if len(r) == 7 else r for r in rows]
        return rows

    def close(self):
        pass


class _Conn:
    def __init__(self, results, has_canon=True):
        self.cur = _Cur(results, has_canon)

    def cursor(self, **_kw):
        return self.cur

    def rollback(self):
        pass

    def close(self):
        pass


def _client(monkeypatch, rows, has_canon=True, known=frozenset()):
    monkeypatch.setattr(fh, "_CACHE", {})
    monkeypatch.setattr(fh, "_conn", lambda: _Conn([rows], has_canon))
    monkeypatch.setattr(seo, "_valid_market_slugs", lambda: known)
    app = Flask(__name__)
    app.register_blueprint(fh.facilities_hub_bp)
    return app.test_client()


def _row(name, provider, canon, grp="Some Market", state="TX"):
    return (name, provider, grp, "City", state, 1.0, canon)


# /facilities/<x> segments that are routes, not facility slugs (footer + nav).
_NOT_A_SLUG = {"directory", "in"}


def _hrefs(body):
    found = re.findall(r'href="https://dchub\.cloud/facilities/([^"/]+)"', body)
    return {h for h in found if h not in _NOT_A_SLUG}


# ── measured live through the edge, 2026-09-05 ───────────────────────────
# Frozen BEFORE the 2026-07-28 dedupe: the DOUBLED slug is the URL that serves
# 200 and today's builder output only 301s to it. (provider, name, stored)
FROZEN_DOUBLED = [
    ("Equinix", "Equinix HK1", "equinix-equinix-hk1-a3e2c448"),
    ("NTT", "NTT Frankfurt", "ntt-ntt-frankfurt-0b99120e"),
    # provider == name here, which is why the old compose doubled it; the
    # freeze STRIPPED the umlaut (it did not fold it), so the live segment is
    # …darmstdter…. Today's builder says darz-darmstadter-rechenzentrum-36894ab7,
    # which 301s to the stored value — a third form nobody ever served.
    ("DARZ - Darmstädter Rechenzentrum", "DARZ - Darmstädter Rechenzentrum",
     "darz-darmstdter-rechenzentrum-darz-darmstdter-rechenzentrum-36894ab7"),
]

# Frozen AFTER the dedupe: here the stored slug is the DEDUPED form and it was
# the retired _fac_slug's doubled output that 301'd. BOTH directions are real
# in the same table, which is exactly why neither the stored value nor the
# builder can be assumed. (provider, name, stored, what the old hub emitted)
FROZEN_DEDUPED = [
    ("DATACENTER ONE", "Datacenter One Dus1",
     "datacenter-one-dus1-c2e36834",
     "datacenter-one-datacenter-one-dus1-c2e36834"),
]


# ── 1. stored wins, verbatim ─────────────────────────────────────────────
def test_fac_slug_returns_the_stored_slug_verbatim():
    """★ The forward-only contract. Where a row is frozen, that string IS the
    URL — the hub may not recompose, prettify or re-fold it."""
    for provider, name, stored in FROZEN_DOUBLED:
        assert fh._fac_slug(provider, name, stored) == stored, (
            "%r/%r linked at a slug the page does not serve" % (provider, name))


def test_the_stored_and_recomposed_forms_really_differ():
    """Without this the test above is vacuous: if the builder happened to agree
    with every stored value, a naive delegation would pass unnoticed."""
    for provider, name, stored in FROZEN_DOUBLED:
        assert build_canonical_slug(provider, name) != stored, (
            "%r/%r no longer distinguishes stored from recomposed — pick a "
            "pair that does, or this file guards nothing" % (provider, name))


def test_an_ascii_folded_stored_slug_is_not_re_folded():
    """DARZ is frozen at …darmstdter… — the old slugify STRIPPED the umlaut.
    Today's builder FOLDS it (…darmstadter…), a URL that has never existed, and
    the retired hub _slugify kept `ä` verbatim, a third one. Only the stored
    string is the page."""
    provider, name, stored = FROZEN_DOUBLED[2]
    got = fh._fac_slug(provider, name, stored)
    assert got == stored, got
    assert "ä" not in got, "the retired unicode-keeping compose is back"
    assert got != build_canonical_slug(provider, name), (
        "the builder now agrees with the freeze on this row — replace it with "
        "one that still diverges or this case guards nothing")


def test_a_row_frozen_after_the_dedupe_is_not_re_doubled():
    """★ The other direction. Stored-first is not "always emit the doubled
    form" — for post-dedupe rows the stored value IS deduped, and re-doubling
    it is the bug the live German hub was shipping."""
    for provider, name, stored, old_hub in FROZEN_DEDUPED:
        got = fh._fac_slug(provider, name, stored)
        assert got == stored, got
        assert got != old_hub, (
            "%r is the retired hand-composed form; it 301s to %r"
            % (old_hub, stored))


# ── 2. unfrozen rows use the ONE composer ────────────────────────────────
def test_unfrozen_rows_delegate_to_the_freeze_builder():
    corpus = [
        ("Equinix", "Equinix HK1"), ("NTT", "NTT Frankfurt"),
        ("Télécom", "Télécom Paris DC"), ("中国电信", "4"),
        ("Air France", "B4"), ("Oi", "Oi"), ("1A", "1A"), ("SC", "SC"),
        ("", "Spark Data Centre"), ("Digital Realty", "43830 Devin Shafron Drive"),
    ]
    for provider, name in corpus:
        for empty in (None, ""):
            assert fh._fac_slug(provider, name, empty) == \
                build_canonical_slug(provider, name), (
                "%r/%r drifted from the one composer" % (provider, name))


def test_a_row_with_no_readable_name_is_still_skipped():
    for provider, name in [("Equinix", ""), ("Equinix", None), ("", "  "),
                           ("Equinix", "!!!")]:
        assert not fh._fac_slug(provider, name), (provider, name)


# ── 3. the hand-composed body is gone, structurally ──────────────────────
def test_fac_slug_is_a_single_delegating_return():
    """★ Pinned on the AST, not on text: the docstring quotes the retired forms
    on purpose, so a substring check would be satisfied by prose."""
    node = _fn("_fac_slug")
    body = [n for n in node.body if not (isinstance(n, ast.Expr)
                                         and isinstance(n.value, ast.Constant)
                                         and isinstance(n.value.value, str))]
    assert len(body) == 1 and isinstance(body[0], ast.Return), (
        "_fac_slug has grown a body again — it must do nothing but delegate")
    call = body[0].value
    assert isinstance(call, ast.Call) and getattr(call.func, "id", None) == \
        "frozen_slug_for_row", ast.dump(call)


def test_fac_slug_takes_the_stored_slug_as_an_argument():
    args = [a.arg for a in _fn("_fac_slug").args.args]
    assert args == ["provider", "name", "canonical_slug"], args


def _module_code():
    """SRC with every comment AND every docstring removed. The retired forms
    are quoted verbatim in the new docstrings, so a plain substring sweep over
    the file would find them and fail on prose."""
    code = "\n".join(l for l in SRC.splitlines()
                     if not l.lstrip().startswith("#"))
    for node in ast.walk(TREE):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                code = code.replace(doc, "", 1)
    return code


def test_the_module_no_longer_hand_composes_anything():
    code = _module_code()
    for gone in ("stable_hash8", "len(name_slug)", "{name_slug}-{h}"):
        assert gone not in code, (
            "%r is back in facilities_hub.py — the fourth copy of the composer"
            % gone)
    assert "from routes.facility_slug_freeze import frozen_slug_for_row" in SRC


def test_it_does_not_delegate_naively_to_the_builder():
    """★★ The trap. build_canonical_slug is FORWARD-ONLY and must never be the
    hub's link source: it would move every pre-dedupe-frozen link to a 301."""
    assert "build_canonical_slug" not in _code_of("_fac_slug")
    assert "build_canonical_slug" not in _code_of("_rows_to_facs")


# ── 4. the queries actually carry the column ─────────────────────────────
def test_both_listing_queries_select_the_stored_slug():
    for fname in ("facilities_in_country", "facilities_in_us_state"):
        code = _code_of(fname)
        assert "_canon_col(conn, cur)" in code, (
            "%s does not probe for canonical_slug" % fname)
        assert "power_mw, {_cs}" in code, (
            "%s no longer selects the stored slug — _rows_to_facs would see "
            "None for every row and silently recompose all of them" % fname)


def test_the_probe_is_a_probe_and_rolls_back_on_failure():
    code = _code_of("_canon_col")
    assert "information_schema.columns" in code, \
        "the hub must PROBE for canonical_slug, not assume live DDL"
    assert "conn.rollback()" in code, (
        "a failed probe leaves psycopg2 in a failed transaction and every "
        "later read on the connection returns nothing, silently")


def test_rows_to_facs_reads_the_seventh_column():
    code = _code_of("_rows_to_facs")
    assert "canon" in code and "_fac_slug(provider, name, canon)" in code, code


def test_the_countries_index_no_longer_drops_short_names():
    """The SQL twin of the len<3 rejection. Left in place it would understate
    every country's (N) by exactly the rows this change rescues, and disagree
    with the page the reader clicks through to."""
    assert "char_length(name) >= 3" not in _code_of("facilities_index")


# ── 5. end to end, through the route ─────────────────────────────────────
def test_the_country_page_links_the_stored_slug(monkeypatch):
    rows = ([_row(n, p, c) for p, n, c in FROZEN_DOUBLED]
            + [_row(n, p, c) for p, n, c, _o in FROZEN_DEDUPED])
    body = _client(monkeypatch, rows).get(
        "/facilities/in/de").get_data(as_text=True)
    hrefs = _hrefs(body)
    for provider, name, stored in FROZEN_DOUBLED:
        assert stored in hrefs, "%s missing from the hub listing" % stored
        assert build_canonical_slug(provider, name) not in hrefs, (
            "the hub emitted the recomposed form for %r — that URL only 301s"
            % name)
    for _p, _n, stored, old_hub in FROZEN_DEDUPED:
        assert stored in hrefs and old_hub not in hrefs, (
            "the hub re-doubled a post-dedupe row: emitted %r, page serves %r"
            % (old_hub, stored))


def test_the_itemlist_jsonld_carries_the_same_slug(monkeypatch):
    """A SECOND emission site. The rendered <a> and the ItemList are built from
    separate expressions and have drifted apart before."""
    rows = [_row(n, p, c) for p, n, c in FROZEN_DOUBLED]
    body = _client(monkeypatch, rows).get(
        "/facilities/in/de").get_data(as_text=True)
    blocks = [json.loads(m) for m in re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', body, re.S)]
    il = [b for b in blocks if b["@type"] == "ItemList"][0]
    urls = {x["url"].rsplit("/", 1)[-1] for x in il["itemListElement"]}
    assert urls == {c for _p, _n, c in FROZEN_DOUBLED}, urls


def test_a_two_character_name_is_listed_instead_of_dropped(monkeypatch):
    """The 28 rescued rows. Unfrozen they take the builder's slug; frozen they
    take the stored one. Either way the hub must stop skipping them."""
    rows = [_row("Oi", "Oi", None), _row("B4", "Air France", None),
            _row("RZ", "", "rz-06bf7c44")]
    body = _client(monkeypatch, rows).get(
        "/facilities/in/br").get_data(as_text=True)
    hrefs = _hrefs(body)
    assert "oi-c40c6b79" in hrefs and "air-france-b4-946b2b1a" in hrefs
    assert "rz-06bf7c44" in hrefs
    assert "Data Centers in Brazil</h1>" in body
    assert "3 tracked" in body, "the page total still omits the short names"


def test_short_names_count_toward_the_browse_by_state_block(monkeypatch):
    rows = [_row("Equinix DA11", "Equinix", None, state="TX"),
            _row("SC", "SC", None, state="TX")]
    body = _client(monkeypatch, rows).get(
        "/facilities/in/us").get_data(as_text=True)
    assert "Browse by state" in body
    m = re.search(r'/facilities/in/us/texas">Texas</a>\s*'
                  r'<span class="muted">\((\d+)\)</span>', body)
    assert m and m.group(1) == "2", (
        "the state count dropped the two-character name: %s"
        % (m.group(1) if m else "no Texas row at all"))


def test_the_page_still_renders_when_the_live_column_is_missing(monkeypatch):
    """Live DDL can lag repo DDL. The degrade is the builder, not a dark page.
    The rows DO carry stored slugs — it is the `NULL AS canonical_slug` select
    that strips them, which is what makes this the degrade and not the fixture."""
    rows = [_row(n, p, c) for p, n, c in FROZEN_DOUBLED]
    r = _client(monkeypatch, rows, has_canon=False).get("/facilities/in/de")
    assert r.status_code == 200
    hrefs = _hrefs(r.get_data(as_text=True))
    assert hrefs == {build_canonical_slug(p, n) for p, n, _c in FROZEN_DOUBLED}


def test_the_probe_names_the_column_when_it_is_there(monkeypatch):
    """★ The mutation that got away. _canon_col reporting the column missing
    when it exists silently recomposes every link on every hub page, and no
    behavioural test sees it unless the fixture honours the select list."""
    conn = _Conn([[]], has_canon=True)
    assert fh._canon_col(conn, conn.cursor()) == "canonical_slug"
    conn = _Conn([[]], has_canon=False)
    assert fh._canon_col(conn, conn.cursor()) == "NULL AS canonical_slug"
