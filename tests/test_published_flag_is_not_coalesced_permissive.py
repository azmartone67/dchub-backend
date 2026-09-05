"""r-published-not-coalesced — `COALESCE(published, true)` is right in exactly
one place, and wrong everywhere it answers "may I show this row?".

THE COLUMN
----------
`market_power_scores.published` is NULLABLE and its schema DEFAULT is **false**.
So `COALESCE(published, true) = true` inverts the table's own stated default: it
treats a row nobody ever released as released.

WHY THIS IS NOT A BLANKET BAN
-----------------------------
util/dcpi_score_row.MAY_PUBLISH uses the same words correctly, because there the
predicate sits inside a NOT EXISTS that BLOCKS a twin from publishing while its
canonical is live. Assuming an unknown canonical IS live blocks the twin — the
conservative answer, since the failure it prevents is both rows being published
as separate markets at once.

Same idiom, opposite correctness, decided by whether it is read under a
negation. A fence that simply banned the string would have "fixed" MAY_PUBLISH
into the duplication bug r-twin-unpublish exists to end.

So these tests pin the SPLIT:
  * the canon reader must use PUBLISHED_ONLY, never a hand-rolled predicate
  * MAY_PUBLISH keeps its COALESCE, and keeps the note saying why

Zero NULLs exist today, so none of this changes a number — which is the reason
to pin it now rather than after the first NULL arrives.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CANON = ROOT / "canonical_stats.py"
ROW = ROOT / "util" / "dcpi_score_row.py"
PREDICATE = "PUBLISHED_ONLY"
PERMISSIVE = "coalesce(published, true)"


def _sql_texts(path, table="market_power_scores"):
    """(lineno, SQL) for every query over `table`, f-string slots rendered as
    the source of the expression they interpolate. ast.walk descends into
    f-strings, so their literal chunks are excluded — otherwise each query is
    seen a second time without its predicate."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    inside = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
              for v in n.values}
    out = []
    for node in ast.walk(tree):
        if id(node) in inside:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str)
                else ast.unparse(v.value)
                for v in node.values
                if isinstance(v, (ast.Constant, ast.FormattedValue)))
        else:
            continue
        if table in " ".join(text.split()):
            out.append((getattr(node, "lineno", 0), " ".join(text.split())))
    return out


def test_canon_reads_the_published_universe_through_the_shared_name():
    """Canon is what every other surface is measured against."""
    sqls = _sql_texts(CANON)
    assert sqls, "canonical_stats.py no longer queries market_power_scores"
    offenders = [(ln, s[:90]) for ln, s in sqls if PREDICATE not in s]
    assert not offenders, (
        "a canon query does not use the shared predicate:\n  "
        + "\n  ".join(f"line {ln}: {s}" for ln, s in offenders))


def test_canon_does_not_coalesce_the_flag_permissive():
    """The inversion, stated directly.

    Checked on SQL reconstructed from the AST, not on raw source, so the
    comment that EXPLAINS the fix can quote the bad idiom without tripping it.
    """
    offenders = [ln for ln, s in _sql_texts(CANON) if PERMISSIVE in s.lower()]
    assert not offenders, (
        f"canonical_stats.py line(s) {offenders} coalesce `published` to true. "
        f"The column defaults to false; this counts a row nobody released.")


def test_may_publish_keeps_its_coalesce():
    """The other half — so a later sweep cannot 'make it consistent'.

    Rewriting MAY_PUBLISH to `canon.published = true` would let a twin publish
    beside a canonical whose state is unknown.
    """
    src = ROW.read_text(encoding="utf-8")
    tree = ast.parse(src)
    value = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "MAY_PUBLISH"
                        for t in node.targets)):
            value = ast.unparse(node.value)
    assert value, "MAY_PUBLISH is not a module-level assignment any more"
    assert "COALESCE(canon.published, true)" in value, (
        "MAY_PUBLISH no longer assumes an unknown canonical is live. That "
        "predicate is read under a NOT EXISTS: assuming-live BLOCKS the twin, "
        "which is the conservative direction. Making it match PUBLISHED_ONLY "
        "re-opens twin duplication.")


def test_the_reason_for_the_asymmetry_is_written_down():
    """Two spellings that look inconsistent will be 'fixed' unless the file
    says why they differ."""
    src = ROW.read_text(encoding="utf-8")
    assert "negation" in src.lower(), (
        "the note explaining why MAY_PUBLISH keeps COALESCE while "
        "PUBLISHED_ONLY does not is gone; without it the asymmetry reads as "
        "an oversight and gets tidied away.")
