"""GSC coverage fixes 2026-07-28: soft-404, slug doubling, freeze safety.

Driven by the Search Console Coverage export: 299 Soft 404s, 3,454 Not-found,
9,819 Page-with-redirect. AST-extracts the real functions rather than importing
(routes/* pull in main.py; house rule: tests NEVER import main).

★ pytest functions only. No module-scope work, no module-scope sys.exit -- a
  bare exit at import is a COLLECTION error that kills the entire run (this bit
  twice on 2026-07-28: #1797 and the byte-identical damage in
  tests/test_targeted_evidence.py).
"""
import ast
import functools
import hashlib
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FREEZE = REPO_ROOT / "routes" / "facility_slug_freeze.py"
SEO = REPO_ROOT / "routes" / "seo_pages.py"
DEEPDIVE = REPO_ROOT / "routes" / "market_deep_dive.py"

WANT = {"_slugify", "_stable_hash8", "build_canonical_slug",
        "_dedupe_provider_prefix"}


@functools.lru_cache(maxsize=1)
def _slug_fns():
    tree = ast.parse(FREEZE.read_text(encoding="utf-8"))
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in WANT]
    found = {n.name for n in body}
    assert found == WANT, "AST extraction missing {}".format(WANT - found)
    g = {"re": re, "hashlib": hashlib}
    mod = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "<extracted>", "exec"), g)
    return g


# ── the doubling bug ───────────────────────────────────────────────────
def test_provider_prefix_is_not_repeated():
    b = _slug_fns()["build_canonical_slug"]
    assert b("NTT", "NTT Frankfurt").startswith("ntt-frankfurt-"), (
        "ntt-ntt-frankfurt: provider repeated when the name already carries it")
    assert b("Pentech", "Pentech").startswith("pentech-"), "pentech-pentech"
    assert not b("Equinix", "Equinix SP3 Sao Paulo").startswith("equinix-equinix")


def test_provider_still_prefixed_when_the_name_lacks_it():
    b = _slug_fns()["build_canonical_slug"]
    assert b("Equinix", "SP3 Sao Paulo").startswith("equinix-sp3-sao-paulo-"), (
        "the provider must still disambiguate when the name does not carry it")


def test_dedupe_matches_on_token_boundary_only():
    d = _slug_fns()["_dedupe_provider_prefix"]
    # "int" is a prefix of "internap" but NOT a token -- must NOT be eaten
    assert d("int", "internap-dc1") == "int-internap-dc1"
    assert d("ntt", "ntt-frankfurt") == "ntt-frankfurt"
    assert d("ntt", "ntt") == "ntt"


def test_hash_is_unchanged_by_the_dedupe():
    """The rename must not re-identify the row: same provider|name -> same hash."""
    g = _slug_fns()
    h = g["_stable_hash8"]("NTT", "NTT Frankfurt")
    assert g["build_canonical_slug"]("NTT", "NTT Frankfurt").endswith("-" + h)


def test_short_or_missing_names_still_return_none():
    b = _slug_fns()["build_canonical_slug"]
    assert b("NTT", "") is None
    assert b("NTT", "ab") is None


def test_no_provider_means_no_prefix():
    b = _slug_fns()["build_canonical_slug"]
    assert b("", "Frankfurt Two").startswith("frankfurt-two-")


# ── the freeze must stay set-once (the anti-churn guarantee) ───────────
def test_backfill_only_writes_rows_where_canonical_slug_is_null():
    src = FREEZE.read_text(encoding="utf-8")
    seg = src.split("def backfill_canonical_slugs", 1)[1].split("\ndef ", 1)[0]
    assert "WHERE canonical_slug IS NULL" in seg, (
        "the SELECT must only pick unfrozen rows")
    assert "AND t.canonical_slug IS NULL" in seg, (
        "the UPDATE must re-assert set-once, or a re-run would move live URLs")


def test_backfill_mints_an_alias_for_the_pre_dedupe_slug():
    """Freezing the deduped form without aliasing the doubled one would 404
    every already-indexed URL for those rows."""
    src = FREEZE.read_text(encoding="utf-8")
    seg = src.split("def backfill_canonical_slugs", 1)[1].split("\ndef ", 1)[0]
    assert "INSERT INTO facility_slug_aliases" in seg
    assert "provider-dedupe" in seg
    assert "ON CONFLICT (old_slug) DO NOTHING" in seg, (
        "alias insert must be idempotent -- the backfill is re-runnable")


# ── soft 404 -> real 404 ───────────────────────────────────────────────
def test_empty_market_returns_404_not_a_redirect_to_the_hub():
    seo = SEO.read_text(encoding="utf-8")
    seg = seo.split("def _markets_dir_redirect", 1)[1].split("\ndef ", 1)[0]
    assert 'redirect("/markets/directory"' not in seg, (
        "mass-redirecting empty markets to one hub is Google's definition of a "
        "soft 404 -- GSC measured 299 of them")
    assert "404" in seg


def test_market_deepdive_also_returns_404():
    dd = DEEPDIVE.read_text(encoding="utf-8")
    assert 'redirect("/markets/directory", code=302)' not in dd, (
        "the deep-dive path emitted the same soft-404 redirect")
    assert "_markets_404_response" in dd


def test_the_404_body_still_links_onward():
    """An honest 404 must not be a dead end -- its links are still crawled."""
    seo = SEO.read_text(encoding="utf-8")
    seg = seo.split("def _error_page", 1)[1].split("\ndef ", 1)[0]
    assert "/markets/directory" in seg and "/facilities" in seg
    dd = DEEPDIVE.read_text(encoding="utf-8")
    seg2 = dd.split("def _markets_404_response", 1)[1].split("\ndef ", 1)[0]
    assert "/markets/directory" in seg2
    assert "noindex" in seg2
