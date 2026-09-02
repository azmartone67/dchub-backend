"""Guard for the press headline's concentration clause — flask_mcp_endpoints
(2026-09-02).

★ THE DEFECT THIS GUARD EXISTS TO RETIRE

press_headline_metric is built to be quoted verbatim. Measured 2026-09-02
00:23Z it read:

    "DC Hub served 1,810 external AI-agent tool calls in the week of
     2026-08-24 (WoW withheld — ...)"

and weeks[-1] of the very series it read that 1,810 from also said:

    top_caller_calls 1,473   top_caller_pct 81.4   calls_net_of_top 337
    top_caller_client chain-hire   concentration_flag true

One caller — one IP, one tool, no key — was 81% of the week. The LEVEL was
honest and the sentence was not: it carried a number without the thing that
makes it mean something. The renderer ignored five fields it already had.

The fix renders the concentration off the SAME week row, never recomputed,
and only when concentration_flag is true — a week with no dominant caller
must read exactly as before.

Same harness as tests/test_funnel_population_collision.py: the REAL shipped
_build_press_headline is exec'd with only its constants and its one pure
helper bound, so a helper added at module level would NameError inside the
function's try and silently degrade every headline. That is why the clause is
inline, and this file would catch it moving.

Pure functions: no DB, no network, no Flask app.
"""
import ast
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FUNNEL = os.path.join(REPO_ROOT, "flask_mcp_endpoints.py")


def _load_headline_builder(series):
    tree = ast.parse(open(_FUNNEL, encoding="utf-8").read())
    wanted = {"PRESS_HEADLINE_BASIS"}
    funcs = {"_build_press_headline", "_fixed_window_claim"}
    picked = []
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id in wanted
                        for t in node.targets)):
            picked.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in funcs:
            picked.append(node)
    assert len(picked) == len(wanted) + len(funcs)
    ns = {"_press_series": lambda: series}
    exec(compile(ast.Module(body=picked, type_ignores=[]), _FUNNEL, "exec"), ns)
    return ns["_build_press_headline"], ns["PRESS_HEADLINE_BASIS"]


# The live 2026-W35 row, as weekly-series published it on 2026-09-02.
_W35 = {
    "week_start": "2026-08-24", "week_end_exclusive": "2026-08-31",
    "calls": 1810, "agents": 35, "partial": False, "status": "measured",
    "top_caller_calls": 1473, "top_caller_client": "chain-hire",
    "top_caller_pct": 81.4, "calls_net_of_top": 337, "agents_net_of_top": 34,
    "concentration_flag": True,
}
_W34 = {"week_start": "2026-08-17", "calls": 2527, "agents": 17,
        "partial": False, "status": "measured"}
_LIFETIME = 372681


def _series(last_week):
    return {
        "degraded": False,
        "weeks": [_W34, last_week],
        "wow": {"calls_pct": -28.4, "baseline_is_fixed": True,
                "current_week_start": last_week["week_start"],
                "baseline_week_start": "2026-08-17",
                # #202 lands inside W34 — the live reason the WoW is withheld
                "comparability": {"crosses_definition_change": True,
                                  "superseded_by_correction": False}},
    }


def _payload():
    return {
        "ai_agent_requests_external": _LIFETIME,
        "ai_agent_requests_total": _LIFETIME + 1,
        "ai_agent_top_platforms_external": [{"name": "Claude"}, {"name": "Meta AI"}],
    }


def test_the_live_W35_sentence_names_its_concentration():
    """★ THE REGRESSION — the exact sentence that was live on 2026-09-02."""
    build, basis = _load_headline_builder(_series(_W35))
    out = _payload()
    build(out)
    line = out["press_headline_metric"]
    assert "served 1,810 external AI-agent tool calls in the week of 2026-08-24" in line
    assert ", of which 1,473 (81%) came from a single caller (chain-hire); 337 from all others" in line, line
    # the level still leads, the WoW is still withheld, the lifetime clause still closes
    assert line.index("1,810") < line.index("1,473") < line.index("337 from all others")
    assert "WoW withheld" in line
    assert line.endswith(f"; {_LIFETIME:,} external requests led by Claude and Meta AI since launch.")
    assert out["press_headline_metric_basis"].startswith(basis)
    assert "concentration_flag" in out["press_headline_metric_basis"]


def test_a_week_with_no_dominant_caller_reads_exactly_as_before():
    """★ THE FALSE BRANCH. A clause that renders on every week is noise."""
    quiet = dict(_W35, top_caller_calls=300, top_caller_pct=16.6,
                 calls_net_of_top=1510, concentration_flag=False)
    build, basis = _load_headline_builder(_series(quiet))
    out = _payload()
    build(out)
    line = out["press_headline_metric"]
    assert "of which" not in line and "single caller" not in line, line
    assert "1,810" in line
    assert out["press_headline_metric_basis"] == basis, (
        "no clause rendered, so the basis must not describe one")


def test_the_clause_is_read_off_the_row_not_recomputed():
    """The numbers must be the row's own — including a pct that disagrees
    with calls/top_caller_calls arithmetic. weekly_series guarantees
    top + net == calls; the renderer must not second-guess it."""
    row = dict(_W35, top_caller_calls=900, top_caller_pct=50.0,
               calls_net_of_top=910)
    build, _ = _load_headline_builder(_series(row))
    out = _payload()
    build(out)
    assert "of which 900 (50%) came from a single caller (chain-hire); 910 from all others" in out["press_headline_metric"]


def test_a_row_without_concentration_fields_costs_only_the_clause():
    """Older series payloads (pre-concentration) or a failed net query carry
    no top_caller_* keys. The sentence must still publish, unchanged."""
    bare = {k: v for k, v in _W35.items() if not k.startswith(("top_caller", "calls_net", "agents_net", "concentration"))}
    build, basis = _load_headline_builder(_series(bare))
    out = _payload()
    build(out)
    assert "1,810" in out["press_headline_metric"]
    assert "of which" not in out["press_headline_metric"]
    assert out["press_headline_metric_basis"] == basis


def test_a_malformed_row_degrades_the_clause_never_the_sentence():
    broken = dict(_W35, top_caller_calls="not-a-number")
    build, _ = _load_headline_builder(_series(broken))
    out = _payload()
    build(out)
    line = out.get("press_headline_metric") or ""
    assert "1,810" in line, "the level must survive a bad concentration row"
    assert "of which" not in line


def test_the_clause_is_inline_so_the_fence_harness_can_still_run_it():
    """A module-level helper would be unbound in this harness and in
    test_funnel_population_collision's — every headline would silently
    degrade. Pin the constraint that keeps the clause where the harness can
    see it."""
    tree = ast.parse(open(_FUNNEL, encoding="utf-8").read())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_build_press_headline")
    src = ast.get_source_segment(open(_FUNNEL, encoding="utf-8").read(), fn)
    assert "concentration_flag" in src
    assert "calls_net_of_top" in src and "top_caller_client" in src
