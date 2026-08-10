"""r-frozen-slug-select (2026-08-09) — canonical_slug must be SELECTED.

`_render_profile` has done `fac.get("canonical_slug") or slug` since
2026-07-06 (r-frozen-slug). `_fetch_facility_by_slug` never put
canonical_slug in a column list — it only ever appeared in a WHERE — so
`.get()` returned None on every request and `_fslug` was always the slug the
request arrived on. Both things that read `_fslug` degraded silently:

  · rel=canonical — the exact self-canonical the r-frozen-slug comment was
    written to prevent;
  · _is_junk_facility(name, _fslug) — the noindex path, including the
    news-NER slug set from #2493.

Measured on production 2026-08-09, before the fix:

    /facilities/equinix-equinix-nj-campus-26f01f95   canonical → itself  ✓
    /facilities/totally-bogus-alias-name-26f01f95    canonical → ITSELF  ✗
    /facilities/copilot-07a85c97                     robots=noindex      ✓
    /facilities/zzz-alias-07a85c97                   robots=index,follow ✗

The hash8 fallback keys on MD5(provider|name)[:8] and ignores the slug's
name-part entirely, so `<anything>-<hash8>` resolves — an unbounded family of
alias URLs each declaring itself canonical.

★ These tests do not trust the column list by eyeballing it. The fake cursor
  derives its `description` from the SELECT text the module actually ships and
  builds each row from that, so a column dropped from the SQL disappears from
  the returned dict exactly as psycopg2 would drop it. `NULL AS canonical_slug`
  yields None, a bare `canonical_slug` yields the stored value — which is what
  makes the missing-column degrade test meaningful rather than circular.

★ pytest functions only — nothing runs at module scope (CLAUDE.md: a
  module-scope statement that raises is a COLLECTION error and kills the run).
"""
import pathlib
import re
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.facility_profile_page as fpp  # noqa: E402

FROZEN = "equinix-equinix-nj-campus-26f01f95"
ALIAS = "totally-bogus-alias-name-26f01f95"
JUNK_FROZEN = "copilot-07a85c97"
JUNK_ALIAS = "zzz-alias-07a85c97"

# One stored row. Values are keyed by the column NAME the SELECT exposes, so
# whatever the shipped SQL asks for is what the row hands back.
STORED = {
    "id": "df-1",
    "name": "Equinix NJ Campus",
    "provider": "Equinix",
    "city": "Secaucus",
    "state": "NJ",
    "country": "US",
    "region": "New York",
    "latitude": 40.79,
    "longitude": -74.06,
    "power_mw": 40,
    "status": "Operating",
    "address": "",
    "is_duplicate": None,
    "duplicate_of_id": None,
    "canonical_slug": FROZEN,
}


def _select_terms(sql):
    """[(exposed_name, expression)] for the top-level SELECT list of `sql`."""
    up = sql.upper()
    i = up.index("SELECT") + len("SELECT")
    j = up.index(" FROM ", i)
    parts, depth, cur = [], 0, ""
    for ch in sql[i:j]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    out = []
    for raw in parts:
        expr = " ".join(raw.split())
        if not expr:
            continue
        m = re.search(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)$", expr, re.I)
        out.append(((m.group(1) if m else expr.rsplit(".", 1)[-1]), expr))
    return out


class _Cur:
    """Cursor that answers from the SQL it is handed, psycopg2-style."""

    def __init__(self, has_canon, stored=None, rows_in=None):
        self.has_canon = has_canon          # {table: bool}
        self.stored = STORED if stored is None else stored
        # which table actually holds the row — a legacy-only facility has no
        # discovered_facilities twin, which is what the second chance is for
        self.rows_in = rows_in or ("discovered_facilities", "facilities")
        self.description = None
        self.sql_seen = []
        self._rows = []

    def execute(self, sql, params=None):
        self.sql_seen.append(sql)
        terms = _select_terms(sql)
        self.description = [(name,) for name, _ in terms]
        low = " ".join(sql.split()).lower()

        if "information_schema.columns" in low:
            tbl = (params or (None,))[0]
            self._rows = [(1,)] if self.has_canon.get(tbl) else []
            return
        # provenance prongs (util.facility_ner_noindex) — no rows here; the
        # suppression set is seeded directly by the tests that need it.
        if "source = '" in low:
            self._rows = []
            return
        # the frozen-slug exact match: these tests always arrive on an ALIAS,
        # so it must miss and hand the request to the hash8 fallback.
        if "canonical_slug = %s" in low:
            self._rows = []
            return
        if "md5(" in low:
            tbl = ("discovered_facilities" if "from discovered_facilities" in low
                   else "facilities")
            self._rows = [tuple(
                None if expr.upper().startswith("NULL")
                else self.stored.get(name)
                for name, expr in terms)] if tbl in self.rows_in else []
            return
        self._rows = []

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self, **_kw):
        return self._cur

    def rollback(self):
        pass

    def close(self):
        pass


def _fetch(slug, has_canon=None, stored=None, rows_in=None):
    """Run the SHIPPED _fetch_facility_by_slug against the fake DB."""
    if has_canon is None:
        has_canon = {"discovered_facilities": True, "facilities": True}
    cur = _Cur(has_canon, stored, rows_in)
    prev = sys.modules.get("main")
    sys.modules["main"] = types.SimpleNamespace(
        get_read_db=lambda: _Conn(cur), get_db=lambda: _Conn(cur))
    try:
        return fpp._fetch_facility_by_slug(slug), cur
    finally:
        if prev is None:
            sys.modules.pop("main", None)
        else:
            sys.modules["main"] = prev


# ── 1. the column reaches the dict ───────────────────────────────────────

def test_hash8_fallback_row_carries_the_frozen_slug():
    """★ THE fence. Pre-fix this key was absent and .get() returned None."""
    fac, _ = _fetch(ALIAS)
    assert fac is not None, "alias lookup should still resolve via hash8"
    assert "canonical_slug" in fac, (
        "canonical_slug is not in the hash8 SELECT — _render_profile's "
        "`fac.get('canonical_slug') or slug` silently falls back to the "
        "REQUEST slug and every alias URL self-canonicalises")
    assert fac["canonical_slug"] == FROZEN


def test_legacy_only_facility_also_carries_the_frozen_slug():
    """The `facilities` second chance is a serve path too, not a dead end —
    legacy-only rows (no discovered_facilities twin) resolve only there."""
    fac, cur = _fetch(ALIAS, rows_in=("facilities",))
    assert any("FROM facilities" in s and "duplicate_of_id" in s
               for s in cur.sql_seen), "the legacy fallback never ran"
    assert fac is not None
    assert fac.get("canonical_slug") == FROZEN
    assert f'href="https://dchub.cloud/facilities/{FROZEN}"' in \
        fpp._render_profile(fac, ALIAS)


# ── 2. what the column is FOR ────────────────────────────────────────────

def test_render_canonicalises_to_the_frozen_slug_not_the_request_slug():
    fac, _ = _fetch(ALIAS)
    html = fpp._render_profile(fac, ALIAS)
    assert f'href="https://dchub.cloud/facilities/{FROZEN}"' in html
    assert ALIAS not in html.split("<body")[0], (
        "the alias slug is still being declared canonical — this is the GSC "
        "alternate/canonical churn r-frozen-slug was written to stop")


def test_frozen_slug_page_still_self_canonicalises():
    """No regression for the ordinary case: arriving ON the frozen slug.

    (Passes on the unfixed module too, by design — it is a no-regression pin,
    not a fence. Mutation-checked 2026-08-09; the fences are the six above
    and below, each verified to FAIL without the SELECT change.)
    """
    fac, _ = _fetch(ALIAS)
    html = fpp._render_profile(fac, FROZEN)
    assert f'href="https://dchub.cloud/facilities/{FROZEN}"' in html


def test_junk_row_reached_through_an_alias_is_noindex():
    """Closes the "KNOWN, DELIBERATE GAP" in util/facility_ner_noindex.py.

    It was never a property of the NER slug set — the set was consulted with
    the REQUEST slug because the frozen one had not been selected.

    ★ This must run the whole path (fetch → render). An earlier draft built
      the fac dict by hand with canonical_slug already in it and passed
      against the unfixed module — it was testing _render_profile, which was
      never the broken half.
    """
    import util.facility_ner_noindex as ner
    junk_row = dict(STORED, id="df-junk", name="Copilot", provider="Copilot",
                    city="", state="", region="", latitude=None,
                    longitude=None, power_mw=None,
                    canonical_slug=JUNK_FROZEN)
    prev = dict(ner._cache)
    ner._cache.update({"slugs": frozenset({JUNK_FROZEN}),
                       "ts": 9e9, "next_try": 0.0})
    try:
        # the set holds the FROZEN slug only — it cannot see the alias, which
        # is exactly why the lookup has to hand back the frozen one
        assert ner.is_suppressed_slug(JUNK_FROZEN)
        assert not ner.is_suppressed_slug(JUNK_ALIAS)
        fac, _ = _fetch(JUNK_ALIAS, stored=junk_row)
        assert fac is not None
        html = fpp._render_profile(fac, JUNK_ALIAS)
        assert 'content="noindex"' in html, (
            "a junk row served under an alias slug still asks to be indexed")
        assert f'href="https://dchub.cloud/facilities/{JUNK_FROZEN}"' in html
    finally:
        ner._cache.clear()
        ner._cache.update(prev)


# ── 3. the probe — a missing column must degrade, never 404 ──────────────

def test_missing_column_degrades_and_does_not_kill_the_lookup():
    """★ The reason this is probed rather than just added.

    The discovered_facilities hash8 execute() carries no try/except of its
    own: an unprobed reference to a column that does not exist raises into
    the outer handler and returns None — a 404 on EVERY facility page.

    (Also passes on the unfixed module, necessarily: "degrades to the old
    behaviour" IS the old behaviour. It pins the blast radius of a
    pre-migration table, not the fix.)
    """
    fac, cur = _fetch(ALIAS, has_canon={"discovered_facilities": False,
                                        "facilities": False})
    assert fac is not None, "a missing column must not 404 the facility page"
    assert fac["name"] == "Equinix NJ Campus"
    assert fac.get("canonical_slug") is None
    # ... and the renderer falls back to exactly its pre-2026-08-09 behaviour
    html = fpp._render_profile(fac, ALIAS)
    assert f'href="https://dchub.cloud/facilities/{ALIAS}"' in html


def test_probe_is_per_table():
    """`facilities` may lag discovered_facilities — one probe cannot cover
    both, and guessing wrong on either is a silent fallback or a 404."""
    _, cur = _fetch(ALIAS)
    probed = [s for s in cur.sql_seen if "information_schema.columns" in s]
    assert len(probed) >= 2, (
        f"expected a probe per facility table, saw {len(probed)}")


def test_probe_runs_before_any_facility_select():
    """Ordering: the probe result is what builds every column list below it."""
    _, cur = _fetch(ALIAS)
    first_probe = next(i for i, s in enumerate(cur.sql_seen)
                       if "information_schema.columns" in s)
    first_fac = next(i for i, s in enumerate(cur.sql_seen)
                     if "duplicate_of_id" in s)
    assert first_probe < first_fac
