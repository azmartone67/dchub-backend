"""r-us-market-count (2026-09-04) — no surface may hard-type the DCPI market count.

THE DEFECT
----------
A retired market count was published on six agent-facing surfaces: the
/.well-known/ai-agents.json DCPI endpoint description, the press example the
citation tool hands journalists, the explainDCPI module docstring and its
not-found hint, and three LinkedIn post generators.

It was wrong on BOTH axes.

  COUNT   It matched nothing live — not the total scored universe, not the US
          subset, not the mainland-only subset. Measured against
          /api/v1/dcpi/scores at the Railway origin, never _MARKETS_HARDCODED
          (most markets arrive from the dynamic loader).

  SCOPE   It called the markets US-only. The index is global: /api/v1/dcpi/scores
          returns London, Tokyo, Singapore, Bogota and Johannesburg. PR #3805
          removed the identical "U.S." claim from /dcpi's four published strings.

★ THE PART THAT MAKES THIS A DENYLIST BUG, NOT JUST A TYPO. The codebase already
  KNEW the number was retired. main.py's testimonial query has filtered rows
  containing it out of DISPLAYED quotes since 2026-06-04, commented as "retired
  inflation". Its sibling retired counts were added to ai_surface_canon's
  stale_markers, where ai_surface_sentinel scans served bodies for them. This one
  never was — so it was suppressed where a human would read it back and kept
  being published where an agent would. Fixing the call sites without arming the
  sentinel would leave exactly that gap open.

WHY NO FIGURE APPEARS IN THIS FILE
----------------------------------
Deliberate. A number written here to "document" the drift is a second answer with
no fence on it, and it rots exactly as the literal did. The live span is resolved
from canon at run time; the source of truth is /api/v1/dcpi/scores.
"""
from __future__ import annotations

import ast
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The surfaces this change corrected. Every one is agent-facing: a manifest an
#: LLM reads first, a citation a journalist quotes, a tool hint an LLM follows
#: after a miss, or a post published unattended.
CORRECTED_SOURCES = (
    "main.py",
    "routes/mcp_citation.py",
    "routes/mcp_explain_dcpi.py",
    "routes/linkedin_quad_daily.py",
    "routes/linkedin_content_engine.py",
    # ★2026-09-04, added after #3816 shipped: the SERVED tool catalog.
    # #3816 derived the count in main._canonical_mcp_manifest() — which is NOT
    # the responder for /.well-known/mcp.json. A before_request hook intercepts
    # that route first (main.py, see the note near the free-tier gate), and the
    # descriptions it returns are built HERE. So the PR fixed a shadowed path
    # and left the served one typed. Verified at the Railway origin, not at
    # dchub.cloud: the edge additionally shadows that path with an off-repo
    # zone worker, so the edge cannot answer "which handler won".
    "routes/mcp_tool_catalog.py",
)

#: The claim SHAPES a hard-typed market count takes. Matched against source with
#: comments and docstrings stripped, so prose explaining the fix cannot satisfy
#: (or trip) the fence — only live code counts.
#:
#: ★ Deliberately NOT a bare three-digit number. These same shapes are what went
#:   into ai_surface_canon's stale_markers, and a bare number there would collide
#:   with any MW value or thousands separator on a served body (/ai carries a
#:   4-digit number containing the retired count as a substring today). A claim
#:   shape cannot collide with a quantity that is not a market count.
#: ★ [- \t] and NOT \s: \s crosses newlines, and an unrelated numeric fallback
#:   on one line followed by a variable named `markets` on the next then reads
#:   as a claim. The first draft of this fence failed on exactly that. The
#:   hyphen is in the class because the tool catalog writes the claim as
#:   "300+-market set" — a form the space-only version walked straight past.
_HARDCODED_MARKET_CLAIM = re.compile(
    r"\b\d{3}\+?[- \t]+(?:US[- \t]+|U\.S\.[- \t]+)?"
    r"(?:data[- ]cent(?:er|re)[- \t]+)?markets?\b",
    re.IGNORECASE,
)

#: Retired counts that must stay denylisted so the sentinel catches a resurrection
#: on a SERVED body, not just in source.
_REQUIRED_STALE_MARKERS = ("285 market", "285 US")


def _strip_prose(path: str) -> str:
    """Source with comments AND docstrings removed.

    Both are needed. test_honest_numbers learned this the hard way: a comment
    stripper alone leaves docstrings, and a note explaining a drift fix then
    reads as the drift itself. Conversely, prose that quotes a retired figure
    must not be able to SATISFY a fence either.
    """
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    # ★ A SQL denylist pattern is the OPPOSITE of a claim: main.py's testimonial
    #   query filters retired counts OUT of displayed quotes. Scanning it as a
    #   claim would demand deleting the very guard that suppresses the defect.
    src = re.sub(r"%%[^%]*%%", "", src)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body[0].value.value = ""
    return ast.unparse(tree)


@pytest.mark.parametrize("rel", CORRECTED_SOURCES)
def test_no_surface_hard_types_a_market_count(rel):
    """★ The regression fence. Live code may not state a market count."""
    code = _strip_prose(os.path.join(REPO, rel))
    hits = _HARDCODED_MARKET_CLAIM.findall(code)
    matches = [m.group(0) for m in _HARDCODED_MARKET_CLAIM.finditer(code)]
    assert not hits, (
        f"{rel} hard-types a DCPI market count in live code: {matches!r}. "
        f"Bind it to canon instead — ai_surface_canon's {{canon_markets}} "
        f"placeholder, or canonical_stats.markets_phrase(). A typed count "
        f"beside a derived sibling is how the last one survived for months."
    )


def test_the_retired_count_is_denylisted_for_the_sentinel():
    """Source fixes today; the denylist catches the resurrection tomorrow.

    ai_surface_sentinel scans SERVED bodies against these markers, so this is
    the only check that sees a surface rendered by the off-repo zone worker,
    which this repo's source scan cannot reach at all.
    """
    from ai_surface_canon import PINNED
    markers = PINNED.get("stale_markers") or []
    for m in _REQUIRED_STALE_MARKERS:
        assert m in markers, (
            f"{m!r} is not in ai_surface_canon stale_markers. Its sibling "
            f"retired counts are listed there and the sentinel caught them; "
            f"this one was omitted, which is why it survived on six surfaces "
            f"while a DB filter suppressed it from displayed quotes."
        )


def test_the_denylist_markers_are_claim_shapes_not_bare_numbers():
    """★ A bare number here would fire on any MW value or thousands separator.

    Mutating these to a bare number is the tempting 'simplification' — and it
    would arm a high-severity sentinel against legitimate content. /ai carries
    a four-digit number containing the retired count as a substring right now.
    """
    from ai_surface_canon import PINNED
    markers = PINNED.get("stale_markers") or []
    for m in _REQUIRED_STALE_MARKERS:
        assert m in markers and not m.strip().isdigit(), (
            f"{m!r} must stay a CLAIM shape. ai_surface_sentinel does a plain "
            f"substring test on served bodies (ai_surface_sentinel.py), so a "
            f"bare number matches '4285', '2,285' and '285 MW'."
        )
        # A digits-only prefix with nothing else is the collision case.
        assert re.search(r"[A-Za-z]", m), f"{m!r} carries no distinguishing word"


def test_the_citation_tool_does_not_contradict_itself():
    """Both press strings must derive from the SAME placeholder.

    They disagreed inside one returned dict — two typed literals a field apart,
    both beside an already-derived facilities count. This is the tool whose
    output is meant to be quoted verbatim in journalism.
    """
    src = _strip_prose(os.path.join(REPO, "routes/mcp_citation.py"))
    press = src[src.index("def _press"):]
    press = press[:press.index("def _dcpi")]
    assert press.count("{canon_markets}") >= 2, (
        "the press 'text' and 'example' must BOTH resolve the market count "
        "from {canon_markets}. One typed and one derived is how they came to "
        "disagree inside a single response."
    )
    assert not _HARDCODED_MARKET_CLAIM.search(press), (
        "a typed market count is back in the press citation strings"
    )


def test_the_explain_hint_points_at_an_endpoint_that_exists():
    """The not-found hint pointed at a path that 404s.

    A hint is what an LLM follows after a miss, so a dead path turns one bad
    slug into two failed calls. Verified live: the old path returns 404 and
    /api/v1/dcpi/scores is the endpoint that serves the market list.
    """
    src = _strip_prose(os.path.join(REPO, "routes/mcp_explain_dcpi.py"))
    assert "/api/v1/dcpi/markets" not in src, (
        "explainDCPI's hint points at /api/v1/dcpi/markets, which 404s"
    )
    assert "/api/v1/dcpi/scores" in src, (
        "explainDCPI's hint must name an endpoint that actually serves"
    )


def test_canon_resolves_the_placeholder_when_it_can():
    """The happy path: the placeholder must actually be substituted.

    Without this, the fail-open test below passes vacuously — a _canon that
    always returned a count-free string would satisfy it while publishing no
    number at all.
    """
    import routes.mcp_explain_dcpi as M
    out = M._canon("full list of {canon_markets} market slugs.")
    assert "{canon" not in out, f"placeholder leaked into output: {out!r}"
    assert re.search(r"\d", out), (
        f"canon is importable here, so a count should have resolved: {out!r}"
    )


def test_canon_failure_yields_a_count_free_sentence_never_a_wrong_one():
    """★ The fail-open DIRECTION. Unreadable canon must DROP the number.

    This is the asymmetry the whole canon design rests on: a missing count is
    visible and self-correcting, a stale one is neither. A fail-open that
    substituted a literal would reintroduce the defect at the exact moment
    canon is least able to contradict it.

    ★ Exercises the except: branch for real by making the import fail, rather
      than asserting on the happy path — which is what the first draft of this
      test did, and it passed while proving nothing about the failure mode.
    """
    import sys
    import routes.mcp_explain_dcpi as M
    saved = sys.modules.get("ai_surface_canon")
    sys.modules["ai_surface_canon"] = None      # force the import to raise
    try:
        out = M._canon("full list of {canon_markets} market slugs.")
    finally:
        if saved is not None:
            sys.modules["ai_surface_canon"] = saved
        else:
            sys.modules.pop("ai_surface_canon", None)
    assert "{canon" not in out, f"placeholder leaked on the failure path: {out!r}"
    assert not re.search(r"\d", out), (
        f"a number survived canon failure — the fail-open substituted a "
        f"literal instead of dropping the count: {out!r}"
    )
    assert "market slugs" in out, f"sentence was mangled, not just de-numbered: {out!r}"


def test_the_module_level_tool_catalog_stays_query_free():
    """_MCP_TOOL_HOOKS is built at import: canon there = a DB query on import.

    It states no count for that reason. This pins the REASON, so a later
    'improvement' that binds it to canon has to confront the import cost.
    """
    src = _strip_prose(os.path.join(REPO, "routes/linkedin_content_engine.py"))
    hooks = src[src.index("_MCP_TOOL_HOOKS"):]
    hooks = hooks[:hooks.index("]") + 1]
    assert not _HARDCODED_MARKET_CLAIM.search(hooks), (
        "the module-level tool catalog states a market count again"
    )
    assert "canon" not in hooks.lower(), (
        "_MCP_TOOL_HOOKS is evaluated at IMPORT — resolving canon here puts a "
        "database query in the import path of every process that loads this "
        "module. It states no count on purpose."
    )


def test_every_corrected_source_still_parses_and_binds_its_names():
    """Guards the shape of THIS change: an f-string clause bound in one branch
    and used in another is a NameError that only fires on the published path."""
    for rel in CORRECTED_SOURCES:
        path = os.path.join(REPO, rel)
        tree = ast.parse(open(path, encoding="utf-8").read())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            assigned = {n.id for n in ast.walk(fn)
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
            assigned |= {(a.asname or a.name).split(".")[0]
                         for n in ast.walk(fn) if isinstance(n, ast.Import)
                         for a in n.names}
            assigned |= {(a.asname or a.name)
                         for n in ast.walk(fn) if isinstance(n, ast.ImportFrom)
                         for a in n.names}
            assigned |= {a.arg for a in fn.args.args}
            used = {n.id for n in ast.walk(fn)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            for local in ("_mk", "_mk_clause", "_mk_all", "_scope"):
                if local in used:
                    assert local in assigned, (
                        f"{rel}::{fn.name} reads {local!r} without binding it "
                        f"in the same function — a NameError on the path that "
                        f"actually publishes."
                    )
