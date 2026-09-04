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
    # ★2026-09-04. THE SURFACE THE FIRST PASS MISSED, and the one that matters
    # most: /.well-known/mcp.json is what MCP clients, registries and LLMs read
    # first. It is NOT served by main.py's _canonical_mcp_manifest() — a
    # before_request hook intercepts the route ahead of it (see the r68.1 note
    # at main.py) and builds the tool descriptions from THIS module via
    # tools_for_well_known(). So #3816 derived the count in a function that does
    # not serve, while three typed counts kept shipping from one that does;
    # verified by fetching the manifest from the Railway origin and finding all
    # three literals verbatim in the served body.
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
#: ★ [ \t-] and NOT \s: \s crosses newlines, and an unrelated numeric fallback
#:   on one line followed by a variable named `markets` on the next then reads
#:   as a claim. The first draft of this fence failed on exactly that. The
#:   HYPHEN is safe on that axis for the same reason a space is — it cannot
#:   cross a line — and it is load-bearing: the separator class is what decides
#:   whether the fence can see a claim at all.
#:
#: ★2026-09-04 — WHY THE HYPHEN WAS ADDED. This fence shipped matching only the
#:   spaced form, and mcp_tool_catalog's rank_markets summary typed the count as
#:   a COMPOUND ADJECTIVE — "<count>-market set" rather than "<count> markets".
#:   Same claim, same surface, one character of separator different, and the
#:   fence was blind to it: two of that module's three typed counts matched the
#:   spaced shape and the third, hyphenated, did not.
#:   Adding a file to CORRECTED_SOURCES under the spaced-only shape would have
#:   produced a green test beside a live literal, which is the exact failure
#:   this suite exists to prevent. A count that rots does not care which
#:   separator precedes the noun.
_HARDCODED_MARKET_CLAIM = re.compile(
    r"\b\d{3}\+?[ \t-]+(?:US[ \t-]+|U\.S\.[ \t-]+)?"
    r"(?:data[- ]cent(?:er|re)[ \t-]+)?markets?\b",
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


#: A SYNTHETIC three-digit stand-in for the shape tests below.
#:
#: Deliberately not the live count, for the reason this module's docstring
#: gives: a real figure written into a fixture is a second answer with no fence
#: on it, and it reads as a claim to anyone grepping this file for the number.
#: The fence matches a SHAPE, so any three digits exercise it identically — and
#: a number that was never real cannot rot into one that merely looks stale.
_SHAPE_DIGITS = "987"


def test_the_fence_catches_the_compound_adjective_shape():
    """★ A GUARD ON THE GUARD, and the reason this fence was widened.

    "<count> markets" and "<count>-market set" are the same claim on the same
    surface. The fence matched the first and not the second, so mcp_tool_catalog
    could have been added to CORRECTED_SOURCES and gone green with a typed
    literal still shipping on /.well-known/mcp.json.

    Mutating the separator class back to [ \\t] must fail here. Without this
    test that mutation is invisible: every other assertion in this file passes
    under it, because every other corrected source used the spaced form.
    """
    spaced = f"one ranked list across the {_SHAPE_DIGITS}+ market set"
    hyphen = f"one ranked list across the {_SHAPE_DIGITS}+-market set"
    assert _HARDCODED_MARKET_CLAIM.search(spaced), "the spaced shape regressed"
    assert _HARDCODED_MARKET_CLAIM.search(hyphen), (
        "the fence does not see a market count written as a compound adjective "
        "as a compound adjective. That is the shape rank_markets used, and a "
        "fence blind to it is a green test beside a live literal."
    )


def test_the_separator_class_still_cannot_cross_a_newline():
    """The hyphen widened the class; it must not have widened it to \\s.

    The original comment records a real first-draft failure: \\s let an
    unrelated numeric fallback on one line join a variable named `markets` on
    the next and read as a claim. A hyphen cannot cross a line; \\s can.
    """
    across_a_newline = f"timeout = {_SHAPE_DIGITS}\nmarkets = load()"
    assert not _HARDCODED_MARKET_CLAIM.search(across_a_newline), (
        "the separator class crosses newlines again — an unrelated number "
        "above a `markets` identifier now reads as a published claim"
    )


def test_a_hyphen_does_not_make_a_quantity_read_as_a_market_count():
    """Widening a claim shape must not make it collide with a non-claim.

    Same lesson as the bare-number denylist above: a shape that fires on
    a hyphenated MW value or an ISO date would arm this fence against
    legitimate content, and a fence that cries wolf gets deleted.
    """
    for benign in (f"a {_SHAPE_DIGITS}-MW market entry",
                   "as_of 2026-09-04 markets loaded",
                   f"{_SHAPE_DIGITS}-day market average"):
        assert not _HARDCODED_MARKET_CLAIM.search(benign), (
            f"{benign!r} is not a market-count claim but the fence fires on it"
        )


def test_the_served_catalog_resolves_every_count_from_canon():
    """★ The catalog that actually answers /.well-known/mcp.json.

    main.py's before_request hook intercepts the route ahead of
    _canonical_mcp_manifest() and builds tool descriptions from this module, so
    this is the string an MCP client reads. Two failures are possible and both
    must be caught: a typed literal (what this change removed) and an
    unresolved placeholder shipped to an agent, which is strictly worse.
    """
    from routes.mcp_tool_catalog import _curated_tools
    summaries = {t[0]: t[3] for t in _curated_tools()}
    for name in ("rank_markets", "get_market_intel", "claim_free_key"):
        s = summaries[name]
        assert "{canon" not in s, (
            f"{name} ships an unresolved placeholder to agents: {s!r}"
        )
        assert re.search(r"\d", s), (
            f"{name} resolved to a count-free string while canon is readable "
            f"here — the placeholder is not reaching a value: {s!r}"
        )


def test_the_curated_catalog_is_built_per_call_not_at_import():
    """★ WHY canon is safe in THIS module and not in _MCP_TOOL_HOOKS.

    The inverse of test_the_module_level_tool_catalog_stays_query_free. Binding
    canon to module-level data puts a DB query in the import path and freezes
    the cold-start value for the life of the process — which is exactly what
    happened here on 2026-09-02, when this catalog WAS a module-level list and
    served a frozen facilities floor beside a request-time-resolved sibling.

    A refactor back to a module-level constant would silently reintroduce that,
    so the per-call shape is pinned rather than left to a docstring.
    """
    import inspect
    import routes.mcp_tool_catalog as M
    assert inspect.isfunction(M._curated_tools), (
        "_curated_tools is no longer a function. If the catalog becomes "
        "module-level data again, its canon_text() calls run ONCE at import — "
        "a database query in the import path, and a value frozen at cold start."
    )
    a, b = M._curated_tools(), M._curated_tools()
    assert a is not b, "the catalog is memoised; canon can no longer heal"


def _tool_entry(rel: str, tool: str) -> str:
    """The source of ONE catalog tuple, keyed on its tool name.

    The catalog is a list of (name, category, tier, summary, example) tuples and
    tool names also appear inside each OTHER tool's prose as cross-references,
    so only a structural lookup reliably finds the entry rather than a mention.
    """
    tree = ast.parse(_strip_prose(os.path.join(REPO, rel)))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Tuple) and node.elts
                and isinstance(node.elts[0], ast.Constant)
                and node.elts[0].value == tool):
            return ast.unparse(node)
    raise AssertionError(f"{tool!r} is no longer a catalog entry in {rel}")


def test_the_free_key_pitch_derives_both_of_its_numbers():
    """★ The SIBLING literal, fenced at the source because no shape can see it.

    claim_free_key's summary carried two typed numbers in one parenthesis: the
    market count and a per-day call quota. Only the first has a claim shape this
    file's regex can match — a bare call quota is indistinguishable from any
    other small integer in prose — so a revert of the second would be invisible
    to every other assertion here. It gets a source-level fence instead.

    The quota itself is not asserted, deliberately: a figure written here is a
    second answer with no fence on it. ai_surface_canon owns it, and pins it
    only because every enforcement lane agrees (tier_registry rate_limit and
    mcp_daily, and the edge MCP_TIERS). A tier whose lanes disagree gets no
    placeholder, which is why there is no {canon_developer_calls} to reach for.

    ★ Anchored on the AST node, not on a substring search. The first textual
    occurrence of "claim_free_key" in this module is a CROSS-REFERENCE inside
    save_site's summary ("call claim_free_key if you don't have one"), so a
    naive .index() slices the wrong tool's prose and the fence reads a string
    it was never pointed at. The first draft of this test did exactly that and
    failed against correct code.
    """
    pitch = _tool_entry("routes/mcp_tool_catalog.py", "claim_free_key")
    assert "{canon_free_calls}" in pitch, (
        "claim_free_key types its own call quota again. Bind it to "
        "{canon_free_calls} — the free tier is the one number every "
        "enforcement lane agrees on, so canon can serve it honestly."
    )
    assert "{canon_markets}" in pitch, (
        "claim_free_key types the market count again"
    )
