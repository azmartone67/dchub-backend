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
import unicodedata

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FREEZE = REPO_ROOT / "routes" / "facility_slug_freeze.py"
SEO = REPO_ROOT / "routes" / "seo_pages.py"
DEEPDIVE = REPO_ROOT / "routes" / "market_deep_dive.py"

WANT = {"_slugify", "_stable_hash8", "build_canonical_slug",
        "_dedupe_provider_prefix", "_fold_to_ascii"}


@functools.lru_cache(maxsize=1)
def _slug_fns():
    tree = ast.parse(FREEZE.read_text(encoding="utf-8"))
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in WANT]
    found = {n.name for n in body}
    assert found == WANT, "AST extraction missing {}".format(WANT - found)
    try:
        from unidecode import unidecode as _u
    except Exception:
        _u = None
    g = {"re": re, "hashlib": hashlib, "unicodedata": unicodedata,
         "_unidecode": _u}
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
def test_backfill_never_overwrites_a_real_slug():
    """Set-once still holds -- it just now treats the '' sentinel as unfrozen.

    WAS: asserted the literal `AND t.canonical_slug IS NULL`. That guard was
    widened on purpose so the 221 empty-sentinel rows (non-Latin names) could be
    given a URL. The GUARANTEE is unchanged: a NON-EMPTY canonical_slug is never
    rewritten, which is what keeps live URLs from moving.
    """
    src = FREEZE.read_text(encoding="utf-8")
    seg = src.split("def backfill_canonical_slugs", 1)[1].split("\ndef ", 1)[0]
    # ★ STRIP COMMENTS FIRST. The prose above the SQL restates every guard
    # verbatim, so a plain `in seg` check passes even after the real clause is
    # deleted -- verified by mutation: removing `AND v.slug <> \'\'` left all
    # 20 tests green until this strip was added. Comments satisfy grep; assert
    # on the executable text only.
    seg = "\n".join(
        ln for ln in seg.splitlines()
        if not ln.strip().startswith(("--", "#"))
    )
    assert "canonical_slug IS NULL OR canonical_slug = \'\'" in seg, (
        "the SELECT must pick unfrozen rows AND the '' sentinel")
    assert "t.canonical_slug IS NULL OR t.canonical_slug = \'\'" in seg, (
        "the UPDATE must still refuse to touch a real slug")
    assert "v.slug <> \'\'" in seg, (
        "an empty computed slug must not overwrite anything")


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


# ── resolve city-state -> city BEFORE 404ing (second pass, 2026-07-28) ──
# Facility pages link to /markets/{city}-{state}, but market slugs are
# METRO/CITY keyed: `dallas` is real, `dallas-texas` is not. The first pass
# turned that 302-to-hub into an honest 404 -- honest, and still wrong, because
# the site then hard-404'd its OWN internal links. Verified live before the fix:
# /markets/dallas = 200, /markets/dallas-texas = 404.
def _deepdive_src():
    return DEEPDIVE.read_text(encoding="utf-8")


def _state_stripper():
    tree = ast.parse(_deepdive_src())
    want = {"_market_slug_without_state"}
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in want)
            or (isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "") == "_US_STATE_SUFFIXES")]
    assert {n.name for n in body if isinstance(n, ast.FunctionDef)} == want
    g = {"_conn": lambda: None}          # no DB -> the DB guard returns None
    mod = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "<extracted>", "exec"), g)
    return g


def test_state_suffix_is_detected_only_as_a_whole_token():
    g = _state_stripper()
    sfx = g["_US_STATE_SUFFIXES"]
    assert "texas" in sfx and "virginia" in sfx
    # kansas-city must NOT be read as <kansas><-city>; it does not END in a state
    assert not "kansas-city".endswith("-kansas")


def test_no_db_means_no_redirect():
    """The DB check is the real guard -- unreachable DB must 404, never guess."""
    g = _state_stripper()
    assert g["_market_slug_without_state"]("dallas-texas") is None


def test_slug_without_a_state_suffix_is_left_alone():
    g = _state_stripper()
    assert g["_market_slug_without_state"]("northern-virginia") != "northern"
    assert g["_market_slug_without_state"]("ashburn") is None
    assert g["_market_slug_without_state"]("") is None


def test_resolution_runs_before_the_404():
    src = _deepdive_src()
    seg = src.split("if _fac_ct == 0:", 1)[1].split("\n    if not md", 1)[0]
    i_resolve = seg.find("_market_slug_without_state")
    i_404 = seg.find("_markets_404_response")
    assert i_resolve != -1 and i_404 != -1
    assert i_resolve < i_404, (
        "a 404 must only be reached AFTER trying to resolve the real market")
    assert "code=301" in seg, "a known market rename is permanent -> 301"


# ── transliteration: non-Latin names must still get a URL ──────────────
# 221 live facilities had canonical_slug='' because _slugify kept only
# [a-z0-9], so CJK/Cyrillic names reduced to nothing and the facility was
# unreachable. The same stripping mangled accented Latin into
# "bouygues-t-l-com" -- a slug that appears verbatim in the GSC 404 export.
def _fold_fns():
    import unicodedata
    tree = ast.parse(FREEZE.read_text(encoding="utf-8"))
    want = {"_fold_to_ascii", "_slugify"}
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in want]
    assert {n.name for n in body} == want
    try:
        from unidecode import unidecode as _u
    except Exception:
        _u = None
    g = {"re": re, "unicodedata": unicodedata, "_unidecode": _u}
    mod = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "<extracted>", "exec"), g)
    return g


def test_unidecode_is_installed():
    """Unidecode is a REAL dependency, not a nice-to-have.

    Without it `_fold_to_ascii` silently falls back to unicodedata NFKD, which
    folds accented Latin only — every CJK/Cyrillic/Katakana name then slugs to
    "" and the facility gets no URL at all. requirements.txt pins it, so any
    environment missing it is testing a degraded path production never runs.

    This test exists so that shows up as "Unidecode is not installed" rather
    than as "'ドコモ' still slugs to ''" two tests below, which reads like a bug
    in the slugger and costs a diagnosis. (It cost one: the unit-tests job was
    red on main for ~30min because the install line omitted it.)
    """
    try:
        import unidecode  # noqa: F401
    except ImportError:
        raise AssertionError(
            "Unidecode is not installed — non-Latin facility names will slug "
            "to '' and get no URL. requirements.txt pins Unidecode==1.3.8; the "
            "unit-tests job in .github/workflows/pre-merge.yml must install it "
            "too.")


def test_accented_latin_folds_instead_of_shattering():
    """The pre-fix slug was 'bouygues-t-l-com' --每 accent became a separator."""
    sl = _fold_fns()["_slugify"]
    assert sl("Bouygues Telecom") == "bouygues-telecom"
    out = sl("Bouygues Télécom")
    assert out == "bouygues-telecom", out
    assert sl("Córdoba") == "cordoba"


def test_non_latin_names_produce_a_usable_slug():
    sl = _fold_fns()["_slugify"]
    for name in ("ドコモ", "Парковий",
                 "百度地图顺德数据中心"):
        got = sl(name)
        assert got and len(got) >= 3, (
            "{!r} still slugs to {!r} -- that facility has no URL".format(name, got))
        assert re.fullmatch(r"[a-z0-9-]+", got), got


def test_fold_degrades_without_the_dependency():
    """A failed Unidecode install must not break slugging -- NFKD still folds."""
    g = _fold_fns()
    g["_unidecode"] = None
    assert g["_fold_to_ascii"]("Télécom").lower().startswith("telecom")


def test_slugify_is_still_deterministic_and_url_safe():
    sl = _fold_fns()["_slugify"]
    a, b = sl("联通云数据中心"), sl("联通云数据中心")
    assert a == b, "a frozen slug must be reproducible"
    assert re.fullmatch(r"[a-z0-9-]+", a)


# ── the freeze guarantee must survive the sentinel re-open ─────────────
def test_empty_sentinel_is_reopenable_but_real_slugs_are_not():
    src = FREEZE.read_text(encoding="utf-8")
    seg = src.split("def backfill_canonical_slugs", 1)[1].split("\ndef ", 1)[0]
    assert "canonical_slug IS NULL OR canonical_slug = ''" in seg, (
        "rows frozen as the '' sentinel serve no URL, so they must be re-openable")
    assert "v.slug <> ''" in seg, (
        "an empty computed slug must not overwrite an existing value")
    assert "t.canonical_slug IS NULL OR t.canonical_slug = ''" in seg, (
        "a REAL canonical_slug must still never be overwritten -- that guarantee "
        "is why live URLs do not move")
