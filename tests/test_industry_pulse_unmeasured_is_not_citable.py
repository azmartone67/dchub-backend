"""r-pulse-honesty — /api/v1/industry/pulse must not publish invented numbers.

WHAT SHIPPED
------------
The endpoint's cold payload carried a full set of literals from a 2026-05 hand
count — 21,374 facilities, 178 countries, 80 markets scored, 14 BUILD, 63
AVOID — labelled `"source": "canonical fallback"`, wrapped in a `citation`
block and a schema.org Dataset both stamped with TODAY's date. Live values at
the time of the fix: 327 markets / 24 BUILD / 217 AVOID.

Its stated audience is analysts (CBRE, JLL, Gartner, IDC), AI agents and
journalists, and the payload explicitly invites them to quote it without
permission. So this was not a stale cache — it was a fabricated dataset
presented as a fresh, citable observation.

It was reachable in production the whole time because the cache is per-PROCESS
and `_start_bg_compute_if_needed` — written for exactly this and referenced by
the module docstring — was never called from anywhere. The 30-minute cron POSTs
once, warming one replica; every request routed elsewhere got the invented
numbers. Measured before the fix: of eight consecutive requests seconds after a
successful refresh, three served the cold payload.

WHAT THESE PIN
--------------
  * the cold payload contains no numbers at all
  * nothing unmeasured is dressed for citation
  * the self-warm is actually CALLED, not merely defined
  * the DCPI reads use the published universe and invent nothing on failure

Read the shipped source rather than importing main.py, per the house rule.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "routes" / "industry_pulse.py"
SENTINEL = "_UNMEASURED"
SELF_WARM = "_start_bg_compute_if_needed"
PREDICATE = "PUBLISHED_ONLY"


@pytest.fixture(scope="module")
def src():
    return SRC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tree(src):
    return ast.parse(src)


def _fn(tree, name):
    return next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)


def test_the_cold_payload_contains_no_numbers(tree):
    """The defect, stated as directly as it can be stated.

    Every value in the unmeasured payload must be None, a string, a URL or an
    empty list. A number here is, by construction, a number nobody measured.
    """
    fn = _fn(tree, "_unmeasured_metrics")
    assert fn is not None, "_unmeasured_metrics is gone; move this fence with it"
    body = ast.unparse(fn)
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        body = body.replace(ast.unparse(fn.body[0]), "", 1)   # drop the docstring
    offenders = [n.value for n in ast.walk(ast.parse(body))
                 if isinstance(n, ast.Constant)
                 and isinstance(n.value, (int, float))
                 and not isinstance(n.value, bool)]
    assert not offenders, (
        f"_unmeasured_metrics carries literal number(s) {offenders}. Nothing in "
        f"it was measured, so every value must read as unknown — never a "
        f"remembered figure and never 0.")


def test_nothing_unmeasured_is_dressed_for_citation(tree):
    """A citation block over unmeasured values is the whole bug."""
    fn = _fn(tree, "_build_response")
    assert fn is not None, "_build_response is gone"
    body = ast.unparse(fn)
    assert "citation" in body, "the citation block vanished entirely"
    assert "_is_measured" in body, (
        "_build_response no longer asks whether anything was measured before "
        "emitting the citation / schema.org blocks. Those carry today's date "
        "and invite analysts and AI agents to quote the payload.")


def test_the_citation_is_actually_conditional(tree):
    """Positive form: the guard must GATE the blocks, not merely be mentioned."""
    fn = _fn(tree, "_build_response")
    gated = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        if "_is_measured" not in ast.unparse(node.test):
            continue
        emitted = ast.unparse(node.body)
        if "citation" in emitted and "schema_org" in emitted:
            gated.append(node.lineno)
    assert gated, (
        "no `if _is_measured(...)` branch emits BOTH the citation and the "
        "schema.org Dataset. If they are emitted unconditionally, an unmeasured "
        "payload is quotable again.")


def test_the_self_warm_runs_on_the_branch_that_serves_nothing(tree):
    """The reason the invented numbers were reachable at all.

    The function existed and the module docstring promised it ran. It was never
    called, so a replica the 30-minute cron missed served the cold payload
    forever — the cache is per-PROCESS.

    ★ Asserting merely that the HANDLER calls it somewhere is too weak, and a
    mutation proved it: deleting the call from the cold branch left the one on
    the stale branch, and the check stayed green. The branch that matters is
    the one that decided it has nothing to report — serving the unmeasured
    payload without kicking off a recompute is the permanent-cold state.
    """
    assert _fn(tree, SELF_WARM) is not None, f"{SELF_WARM} is gone"
    handler = _fn(tree, "industry_pulse")
    assert handler is not None, "the industry_pulse handler is gone"

    cold_branches = []
    for node in ast.walk(handler):
        if not isinstance(node, ast.If):
            continue
        body = ast.unparse(node.body)
        if "_unmeasured_metrics" in body:
            cold_branches.append(body)
    assert cold_branches, (
        "no branch of the handler serves _unmeasured_metrics — if the cold "
        "path moved, move this fence with it")
    for body in cold_branches:
        assert SELF_WARM in body, (
            f"the branch serving the unmeasured payload never calls "
            f"{SELF_WARM}. That replica will keep answering with nothing "
            f"measured until something else happens to warm it.")


def _sql_texts(tree):
    """(lineno, SQL) for every SELECT over the scores table.

    Reconstructed from the string node with each f-string slot rendered as the
    SOURCE of the expression it interpolates. The first cut of this check used
    a regex bounded by `[^"\']*`, which stops dead at the quote inside
    `WHERE verdict = 'BUILD'` — so it truncated the query BEFORE the predicate
    and reported correct code as broken. SQL is not a regex-shaped language;
    read it from the AST.

    ast.walk descends into an f-string, so its literal chunks are also visited
    as bare Constants — the same query minus its interpolated slot. Excluded.
    """
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
        if "FROM market_power_scores" in " ".join(text.split()):
            out.append((getattr(node, "lineno", 0), " ".join(text.split())))
    return out


def test_dcpi_reads_use_the_published_universe(tree):
    """The counts and the top-5 lists, once the cache IS warm.

    Even measured, these read the whole table — retired alias-twins included —
    so a market that was retired in July could rank into a published top-5.
    """
    offenders = [(ln, sql[:80]) for ln, sql in _sql_texts(tree)
                 if PREDICATE not in sql]
    assert not offenders, (
        "a DCPI read uses the unpublished universe:\n  "
        + "\n  ".join(f"line {ln}: {sql}" for ln, sql in offenders))


def test_no_read_falls_back_to_an_invented_number(src):
    """A failed read must produce null, not a remembered figure.

    `default=0` counts too: on the deals windows a failed read then becomes
    indistinguishable from a genuine zero.
    """
    import re
    bad = re.findall(r"default=(\d+)", " ".join(src.split()))
    assert not bad, (
        f"_safe_query still falls back to invented literal(s) {bad}. An "
        f"unmeasured count must read as unknown.")
