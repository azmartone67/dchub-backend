"""API slug-composer delegation guards (r-apislug, 2026-07-31).

The API half of the flywheel lane-5 drift: four API emitters in main.py — the
map dots endpoint, the authed facilities list, the free facilities list and
/search (the r-1348 mirrors) — hand-composed `{provider}-{name}-{hash8}`
WITHOUT the provider-prefix dedupe + ascii folding the freeze stores. Every
not-yet-frozen row was emitted under the doubled pre-dedupe form
(iron-mountain-iron-mountain-lon-3-…), a slug that DIFFERED from what the
freeze later stored. All four sites now DELEGATE to
facility_slug_freeze.build_canonical_slug (the one owner), and the three
SELECT*-backed sites prefer the row's frozen canonical_slug when present.

Resolution was never broken — the /facilities/<slug> resolver falls back on
the hash8 tail, which the dedupe does not touch — so these guards pin
cosmetic consistency: API, sitemap, 301s and the freeze speak ONE slug.

No DB, no network: source pinning + pure-function contract checks, same
style as tests/test_flywheel_honest_gates.py. Deliberately does NOT pin the
serve_sitemap fallback or routes/seo_pages.py — those are the r-lane5
surfaces with their own guards.
"""
import re
from pathlib import Path

import pytest

fsf = pytest.importorskip("routes.facility_slug_freeze")

ROOT = Path(__file__).resolve().parent.parent
MAIN = (ROOT / "main.py").read_text()

MARK = "r-apislug (2026-07-31)"


def _regions():
    """The four delegation sites, each anchored on its own r-apislug marker."""
    idxs = [m.start() for m in re.finditer(re.escape(MARK), MAIN)]
    assert len(idxs) == 4, f"expected 4 r-apislug sites in main.py, found {len(idxs)}"
    return [MAIN[i: i + 1700] for i in idxs]


# ── every site delegates; no site hand-composes ────────────────────────────

def test_all_four_api_sites_delegate_to_the_freeze_builder():
    for n, region in enumerate(_regions(), 1):
        assert "from routes.facility_slug_freeze import build_canonical_slug" in region, \
            f"r-apislug site {n} does not import the freeze builder"
        assert "build_canonical_slug(" in region, \
            f"r-apislug site {n} does not call the freeze builder"


def test_old_fstring_compose_forms_are_gone():
    """The exact hand-compose forms the delegation replaced. Scoped to the
    API sites' distinctive spellings — the serve_sitemap fallback
    (`full_slug = f"{provider_slug}-…`) is r-lane5's pin, not ours."""
    # map dots site (old: f['slug'] = f"{provider_slug}-{name_slug}-{short_hash}" …)
    assert 'f[\'slug\'] = f"{provider_slug}-' not in MAIN
    # r-1348 twins + /search (old: … f"{_ps}-{_ns}-{_h}" … / f"{_ns}-{_h}" …)
    assert "{_ns}-{_h}" not in MAIN
    # the local slugify twins the sites carried are gone with them
    for twin in ("def _slugify1", "def _slugify2", "def _fac_slug", "_shash1", "_shash2"):
        assert twin not in MAIN, f"local composer twin `{twin}` is back in main.py"


def test_stored_canonical_slug_preferred_where_selected():
    """The three SELECT*-backed sites (authed list, free list, /search) read
    the frozen canonical_slug first; the map site cannot (explicit column
    list against DDL-probed columns) and documents it."""
    stored_first = sum(".get('canonical_slug')" in r for r in _regions())
    assert stored_first == 3, \
        f"expected 3 stored-first sites, found {stored_first}"


# ── the builder contract the emitters now rely on ──────────────────────────

def test_builder_contract_the_emitters_rely_on():
    # the live lane-5 sample: provider-prefix dedupe applies
    s = fsf.build_canonical_slug("Iron Mountain", "Iron Mountain LON-3")
    assert s and s.startswith("iron-mountain-lon-3-"), s
    assert "iron-mountain-iron-mountain" not in s
    # token boundary: a provider that merely prefixes a word is kept
    s2 = fsf.build_canonical_slug("Int", "Internap Dallas")
    assert s2 and s2.startswith("int-internap-dallas-"), s2
    # ★ 2026-09-05: "ab" was `is None`. The len<3 rejection measured the name
    # FRAGMENT, but the slug always carries a provider prefix and an 8-char
    # hash, so `x-ab-<h>` is unique and perfectly citable. The old rule stranded
    # 28 Operational facilities with no URL from March to September.
    assert fsf.build_canonical_slug("X", "ab") == "x-ab-c343e6d0"
    # a MISSING name is still None — that guard is the real one, and every
    # call site coalesces it to ''/skip
    assert fsf.build_canonical_slug("X", "") is None
    # no provider → name-hash form
    s3 = fsf.build_canonical_slug("", "Standalone Site")
    assert s3 and s3.startswith("standalone-site-"), s3
    # ascii folding parity (the old hand-compose stripped these to junk)
    s4 = fsf.build_canonical_slug("Télécom", "Télécom Paris DC")
    assert s4 and s4.startswith("telecom-paris-dc-"), s4


def test_hash8_tail_is_unchanged_by_delegation():
    """Resolution safety: the /facilities/<slug> resolver falls back on the
    hash8 tail, and the old hand-compose keyed it via routes.facility_slug.
    stable_hash8 — the freeze builder must produce the same tail."""
    from routes.facility_slug import stable_hash8
    for prov, name in [("Iron Mountain", "Iron Mountain LON-3"),
                       (None, "Solo DC Campus"),
                       ("", "Café One Data")]:
        s = fsf.build_canonical_slug(prov, name)
        assert s is not None, (prov, name)
        assert s.endswith("-" + stable_hash8(prov, name)), (prov, name, s)
