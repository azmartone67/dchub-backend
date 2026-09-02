"""Known-duplicate facilities must not be published as rivals (2026-07-28).

Diagnosis of GSC "Crawled - currently not indexed" (4,494). Measured:
  - 577 of 1000 sampled not-indexed URLs are /facilities/ pages
  - two of them rendered 100.0% IDENTICAL text (same facility, two rows)
  - the rejected pages are NOT thinner in the DB (provider +17pp, latitude
    +26pp vs the population) -- so it was never a data-completeness problem
  - 7,928 rows are flagged is_duplicate, 7,912 still had a canonical_slug,
    all served 200, and 7,877 were re-added to the sitemap by the LEGACY
    `facilities` union, which had no duplicate filter
  - every one of them emitted a SELF-canonical, so identical pages each
    claimed to be the original

pytest functions only -- no module-scope work or exit.
"""
import ast
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN = REPO_ROOT / "main.py"
PROFILE = REPO_ROOT / "routes" / "facility_profile_page.py"


def _code(path):
    """Source with comment lines stripped -- comments satisfy grep."""
    return "\n".join(
        ln for ln in path.read_text(encoding="utf-8").splitlines()
        if not ln.strip().startswith("#")
    )


def test_legacy_union_filters_known_duplicates():
    seg = _code(MAIN).split("_legacy_unioned = 0", 1)[1][:4000]
    assert "_drop_known_dupes" in seg, (
        "the legacy facilities union re-added 7,877 known duplicates; the "
        "seen_slugs guard cannot catch them because the discovered query "
        "already excluded them")


def test_both_union_paths_are_filtered():
    """The minimal fallback query cannot select canonical_slug, which is why
    the filter is applied in Python rather than SQL."""
    seg = _code(MAIN).split("_legacy_unioned = 0", 1)[1][:4000]
    assert seg.count("_drop_known_dupes(c.fetchall())") >= 2, (
        "primary AND fallback union must both be filtered")


def test_duplicate_set_failure_is_fail_open():
    seg = _code(MAIN).split("_dupe_slugs = set()", 1)[1][:2000]
    assert "if not _dupe_slugs or not rows:" in seg, (
        "an unloadable duplicate set must not empty the sitemap")


def test_duplicate_pages_canonicalise_to_their_twin():
    src = _code(PROFILE)
    assert "_canonical_twin_url" in src
    seg = src.split("canonical = f\"https://dchub.cloud/facilities/{_fslug}\"", 1)[1][:1200]
    assert "duplicate_of_id" in seg and "canonical = _twin" in seg, (
        "a row with a duplicate_of_id must point its canonical at the surviving "
        "row, not at itself")
    # ★ and it must NOT require is_duplicate. Gating on the visibility flag
    # forces a suppression to buy a canonical: is_duplicate=1 drops the row from
    # every filtered count and from the sitemap (how 9,318 facilities went
    # missing, per repair_dedup_keeper_election.py). Consolidation needs the
    # POINTER only -- the row stays live, counted, serving 200, and Google
    # merges the two URLs itself.
    assert 'fac.get("is_duplicate")' not in seg, (
        "consolidation must not be gated on the suppression flag")


def test_twin_lookup_refuses_an_unusable_target():
    """A canonical pointing at a 404 or at another duplicate is worse than a
    self-canonical, so the target must be live and non-duplicate."""
    # seo F5 (2026-09-02): the query moved into _canonical_twin_row (the
    # 301 path reads address/coords/pointer off the same row); the URL
    # wrapper delegates to it. The guards are asserted where the SQL is.
    src = PROFILE.read_text(encoding="utf-8")
    seg = src.split("def _canonical_twin_row", 1)[1].split("\ndef ", 1)[0]
    seg = "\n".join(ln for ln in seg.splitlines()
                    if not ln.strip().startswith("#"))
    assert "COALESCE(k.is_duplicate, 0) = 0" in seg
    assert "k.canonical_slug <> ''" in seg
    assert "if not dup_of_id:" in seg, "a null pointer must not query at all"
    wrapper = src.split("def _canonical_twin_url", 1)[1].split("\ndef ", 1)[0]
    assert "_canonical_twin_row(dup_of_id)" in wrapper


def test_twin_lookup_uses_a_connection_helper_that_exists():
    """This module has no _get_conn(); calling one would raise NameError into
    the helper's own `except Exception: return None` and the feature would
    silently never fire while every test still passed."""
    src = PROFILE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)}
    # ★ strip comments -- the docstring/comment in the helper NAMES the wrong
    # symbol on purpose (to record the trap), and a raw scan flags it. Third
    # time today a comment satisfied a grep-shaped assertion.
    seg = _code(PROFILE).split("def _canonical_twin_url", 1)[1].split("\ndef ", 1)[0]
    called = set(re.findall(r"\b(_[a-z_]+)\(", seg))
    for name in called:
        if name.startswith("_") and name not in ("_canonical_twin_url",):
            assert name in defined, (
                "{}() is called but not defined in this module".format(name))


def test_the_row_query_actually_selects_the_dedup_columns():
    """The twin-canonical branch reads fac["is_duplicate"] / ["duplicate_of_id"].

    ★ This shipped INERT the first time: the canonical override was correct and
    every test passed, but no query selected those columns, so
    fac.get("is_duplicate") was always None and the branch never ran. Caught
    only by fetching a known-duplicate page and seeing it still self-canonical.
    A feature whose INPUT is never loaded is indistinguishable from a feature
    that is absent.
    """
    src = _code(PROFILE)
    head = src.split("def _canonical_twin_url", 1)[0]
    # ★ Assert the SELECT clauses themselves. A raw count of "is_duplicate" is
    # useless here: `fac.get("is_duplicate")` and the _twin call contribute two
    # hits on their own, so a >=2 threshold passes even with every SELECT
    # stripped. Verified by mutation.
    selects = re.findall(r"is_duplicate,\s*duplicate_of_id", head)
    assert len(selects) >= 2, (
        "both discovered_facilities queries must SELECT is_duplicate + "
        "duplicate_of_id (found {})".format(len(selects)))
    # the legacy `facilities` table has neither column -> must be declared NULL
    assert "NULL AS is_duplicate" in head, (
        "the legacy-table query lacks these columns; select them as NULL or the "
        "query 500s on a legacy-only facility")


def test_dupe_set_excludes_slugs_a_LIVE_row_still_uses():
    """A duplicate and its twin usually SHARE one canonical_slug.

    ★ The first version of this filter took every slug belonging to a flagged
    duplicate and dropped it from the legacy union. Because the slug hashes
    provider|name — identical for a duplicate pair — 6,846 of the 7,157
    duplicate slugs ARE the live facility's own URL. Result: 21 live pages lost
    their sitemap entry. Measured after shipping, not before.
    "this slug belongs to a duplicate" != "this URL is redundant".
    """
    seg = _code(MAIN).split("_dupe_slugs = set()", 1)[1][:2500]
    assert "NOT EXISTS" in seg, (
        "the duplicate-slug set must exclude any slug a LIVE row still uses, "
        "or the filter removes real pages")
    assert "COALESCE(s.is_duplicate, 0) = 0" in seg


def test_frozen_slug_survives_the_name_length_guard():
    """A facility named in a non-Latin script must still reach the sitemap.

    ★ main.py's own slugify keeps only [a-z0-9], so a Chinese/Japanese/Cyrillic
    name computes an EMPTY name_slug. The `len(name_slug) < 3 -> continue` guard
    ran BEFORE the stored canonical_slug was consulted 15 lines later, so 210
    live facilities with a perfectly good transliterated frozen slug were
    dropped from the sitemap. Generation 53 proved it: the facility shard count
    did not move at all (15,717 before and after) while the freeze had already
    given those rows real URLs.
    """
    code = _code(MAIN)
    # ★ anchor on CODE, not a comment -- _code() strips comment lines, so a
    # comment anchor makes the split silently return one part and the test
    # dies with IndexError instead of asserting anything.
    seg = code.split("for row in fac_rows:", 1)[1][:2600]
    assert "_stored_first" in seg, "the frozen slug must be read before the guard"
    i_stored = seg.find("_stored_first =")
    i_guard = seg.find("len(name_slug) < 3")
    assert i_stored < i_guard, (
        "the stored canonical_slug must be consulted BEFORE the name-length "
        "guard, or frozen non-Latin slugs are skipped")
    assert "not _stored_first and" in seg, (
        "the guard must not fire when a frozen slug exists")
