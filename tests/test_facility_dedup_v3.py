"""facility_dedup_v3 — anonymous-provider variants (2026-07-28).

The planner is PURE, so these are real unit tests on the shipped function,
extracted with ast (importing routes/* pulls in main.py).

The case that matters most is the one it must REFUSE: 581 of the 1,205
same-(name,city) groups are distinct buildings sharing a generic name
(Amazon IAD85 / IAD75 / IAD96 @ Manassas). A false merge hides a real site.
"""
import ast
import functools
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "routes" / "facility_dedup_v3.py"
WANT = {"is_anonymous", "plan_group"}


@functools.lru_cache(maxsize=1)
def _mod():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in WANT)
            or (isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "") in ("_ANON", "_COORD_EPS"))]
    found = {n.name for n in body if isinstance(n, ast.FunctionDef)}
    assert found == WANT, "missing {}".format(WANT - found)
    g = {}
    mod = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "<extracted>", "exec"), g)
    return g


def _row(i, provider, slug="s", lat=1.0, lon=1.0, mw=None):
    return {"id": i, "provider": provider, "canonical_slug": slug,
            "latitude": lat, "longitude": lon, "power_mw": mw}


# ── the refusal that protects real sites ───────────────────────────────
def test_distinct_real_providers_are_never_merged():
    """Amazon IAD85 / IAD75 / IAD96 @ Manassas -- three real buildings."""
    p = _mod()["plan_group"]([
        _row(1, "Amazon IAD85", lat=38.779, lon=-77.542),
        _row(2, "Amazon IAD75", lat=38.779, lon=-77.543),
        _row(3, "Amazon IAD96", lat=38.780, lon=-77.540),
    ])
    assert p["skip"] == "multiple_real_providers"
    assert p["duplicates"] == []


def test_one_real_provider_plus_a_rival_is_still_refused():
    p = _mod()["plan_group"]([
        _row(1, "Equinix"), _row(2, "Digital Realty"), _row(3, ""),
    ])
    assert p["skip"] == "multiple_real_providers"


# ── the merge it should make ───────────────────────────────────────────
def test_anonymous_variants_fold_into_the_named_row():
    """BrainServe SA + blank + Unknown, all within ~200m."""
    p = _mod()["plan_group"]([
        _row(1, "BrainServe SA", slug="brainserve-sa-x", lat=46.545, lon=6.574),
        _row(2, "", slug="brainserve-y", lat=46.547, lon=6.573),
        _row(3, "Unknown", slug="unknown-brainserve-z", lat=46.547, lon=6.573),
    ])
    assert p["skip"] is None
    assert p["canonical"] == 1
    assert sorted(p["duplicates"]) == [2, 3]


def test_unknown_spellings_all_count_as_anonymous():
    a = _mod()["is_anonymous"]
    for v in ("", "  ", "Unknown", "unknown", "N/A", "none", "-", None):
        assert a(v), repr(v)
    for v in ("Equinix", "AWS", "NTT"):
        assert not a(v)


# ── coordinates VETO, never justify ────────────────────────────────────
def test_far_apart_rows_are_skipped_even_with_the_right_provider_shape():
    p = _mod()["plan_group"]([
        _row(1, "Acme", lat=40.0, lon=-100.0),
        _row(2, "", lat=41.0, lon=-100.0),
    ])
    assert p["skip"] == "coords_far_apart"


def test_missing_coordinates_do_not_block_a_merge():
    """A missing coordinate is not evidence of distance."""
    p = _mod()["plan_group"]([
        _row(1, "Acme", lat=None, lon=None),
        _row(2, "", lat=None, lon=None),
    ])
    assert p["skip"] is None and p["duplicates"] == [2]


# ── canonical selection ────────────────────────────────────────────────
def test_canonical_must_have_a_real_slug():
    """A canonical with no slug cannot receive the consolidated signal."""
    p = _mod()["plan_group"]([_row(1, "Acme", slug=""), _row(2, "")])
    assert p["skip"] == "named_row_has_no_slug"


def test_richest_row_wins_then_lowest_id_for_determinism():
    p = _mod()["plan_group"]([
        _row(5, "Acme", mw=None), _row(3, "Acme", mw=12.0), _row(9, ""),
    ])
    assert p["canonical"] == 3, "the row carrying power_mw should win"
    p2 = _mod()["plan_group"]([_row(7, "Acme"), _row(2, "Acme"), _row(9, "")])
    assert p2["canonical"] == 2, "ties break on lowest id, so plans are stable"


def test_a_group_of_only_anonymous_rows_is_left_alone():
    p = _mod()["plan_group"]([_row(1, ""), _row(2, "Unknown")])
    assert p["skip"] == "no_named_row"


def test_a_group_with_no_anonymous_row_is_left_alone():
    p = _mod()["plan_group"]([_row(1, "Acme"), _row(2, "Acme")])
    assert p["skip"] == "no_anonymous_row"


def test_single_row_is_not_a_group():
    assert _mod()["plan_group"]([_row(1, "Acme")])["skip"] == "single_row"


# ── the write path's own guarantees ────────────────────────────────────
def _code():
    return "\n".join(ln for ln in SRC.read_text(encoding="utf-8").splitlines()
                     if not ln.strip().startswith("#"))


def test_apply_reasserts_preconditions_at_write_time():
    seg = _code().split("def apply", 1)[1].split("def undo", 1)[0]
    assert "COALESCE(is_duplicate, 0) = 0" in seg, (
        "a row flagged between analyze and apply must be left alone")
    assert "dedup_method IS NULL" in seg, "must not clobber a v2 decision"
    assert "id <> %s" in seg, "a row can never be its own duplicate"


def test_undo_only_clears_this_modules_marks():
    seg = _code().split("def undo", 1)[1]
    assert "WHERE dedup_method = %s" in seg, (
        "a v3 undo must never roll back a v2 decision")


def test_apply_requires_explicit_confirmation():
    seg = _code().split("def apply", 1)[1].split("def undo", 1)[0]
    assert 'confirm' in seg and '"1"' in seg
