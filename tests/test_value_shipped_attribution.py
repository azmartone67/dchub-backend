"""value-shipped's code_fixes must count only what the BRAIN'S PIPELINE shipped.

MEASURED 2026-09-21 via GET /api/v1/admin/brain/pr-outcomes: of 88 merged
/pull/ rows, ALL were brain_authored=TRUE, but only 7 came from a brain
pipeline branch. The monitor sets brain_authored from `Co-Authored-By: Claude`,
which every human-directed Claude Code session PR carries — so code_fixes
credited the brain with ~12x its own output.

The first fix here was wrong and is the reason this file exists: it filtered
`brain-spec:` titles on an UNCHECKED theory that spec PRs inflated the metric.
It excluded zero rows — spec PRs are `[brain-spec] …` and none are in the
table at all.

The replacement SQL was executed against the 100 real rows in a local
Postgres 18.6: code_fixes 30d = 7, non-pipeline = 81, 7 + 81 = 88 exactly.
Dropping the pipeline filter sent code_fixes back to 88; dropping the
`[brain-` title arm dropped it to 6. The CI unit-tests job has no database, so
this file pins the query's STRUCTURE — the arms whose removal was shown to
matter on real rows.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "routes", "brain_v2_layer4.py"), encoding="utf-8").read()


def _block():
    a = SRC.index("    _PIPELINE = (")
    b = SRC.index("    # R3b (2026-06-16): the x5")
    return SRC[a:b]


def test_code_fixes_is_filtered_to_the_pipeline():
    """Removing this sent code_fixes from 7 back to 88 on the real rows."""
    blk = _block()
    code_q = blk[blk.index("code_7d, code_30d = _dual("):blk.index("nonpipe_7d")]
    assert code_q.count("_PIPELINE") == 2, (
        "code_fixes no longer filters to brain-pipeline PRs in BOTH windows — "
        "it will count every Co-Authored-By: Claude session PR again")


def test_pipeline_predicate_names_every_origin():
    """Each arm was shown load-bearing: losing the title arm dropped 7 -> 6."""
    m = re.search(r"_PIPELINE = \((.*?)\)\n", _block(), re.S)
    assert m, "_PIPELINE definition not found"
    pred = m.group(1)
    for arm in ("brain-v2/%", "brain/fix-%", "brain-l5%", "[brain-%"):
        assert arm in pred, f"pipeline predicate lost the {arm!r} arm"


def test_docs_only_pipeline_prs_are_not_code_fixes():
    """★ Assert it is USED in the query, not merely DEFINED. The first version
    of this test checked `"_NOT_DOCS_ONLY" in block` — the definition line
    alone satisfies that, so a mutation removing it from one window survived."""
    blk = _block()
    code_q = blk[blk.index("code_7d, code_30d = _dual("):blk.index("nonpipe_7d")]
    assert code_q.count("_NOT_DOCS_ONLY") == 2, (
        "docs-only exclusion is missing from a code_fixes window")
    assert "files_changed" in blk, (
        "the docs-only exclusion must read the REAL file list — the column "
        "exists, and an earlier version wrongly claimed it did not")


def test_the_stale_spec_theory_is_gone():
    """The first fix's premise was false. Its filter and its label must not
    come back: a field named for spec PRs that counts session PRs is a lie."""
    assert "spec_prs_excluded" not in SRC
    assert "NOT LIKE 'brain-spec:%'" not in SRC, (
        "the colon-form spec filter is back — it matched zero rows")


def test_excluded_rows_are_reported_but_not_counted():
    """Visible, never summed: shipped_7d/30d feed _sum_nonnull()."""
    assert "non_pipeline_prs_excluded=" in SRC
    for d in ("shipped_7d = {", "shipped_30d = {"):
        i = SRC.index(d)
        body = SRC[i:SRC.index("}", i)]
        assert "nonpipe" not in body, (
            f"excluded rows leaked into {d.split()[0]} — they would be summed "
            "straight back into total_shipped")
