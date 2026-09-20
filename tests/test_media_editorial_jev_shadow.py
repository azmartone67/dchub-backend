"""Guard the Jev shadow lane's two load-bearing properties.

1. IT CANNOT MOVE A VERDICT. The whole safety story is that record_shadow()
   runs after the decision is made and its return value is discarded. That is
   easy to break later with a one-line edit that looks like an improvement
   ("use the shadow when the editor is unavailable"). test_shadow_is_inert_*
   pin it structurally.

2. AN UNANSWERED PROBE IS NOT AGREEMENT. A lane with a bad key, a wrong
   request shape or a dead API produces rows whose outcome is never "ok". If
   those counted in the agreement numerator or denominator, a totally broken
   lane would report 100% agreement and read as a green light to swap the
   editorial desk onto a model that never actually answered. This repo has
   been bitten by that shape before — a rate whose denominator counts the
   unmeasured as passing. test_summarize_* pin it.

NOTE on the harness: the CI unit-tests job installs ONLY pytest (not
requirements.txt), so importing routes.media_editorial_jev_shadow would crash
collection on Flask. We AST-extract the pure helpers and exec them in an
isolated namespace — the same pattern as test_media_editorial_classify.py.
"""
import os
import ast

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHADOW = os.path.join(ROOT, "routes", "media_editorial_jev_shadow.py")
GATE = os.path.join(ROOT, "routes", "media_editorial_gate.py")

# The pure surface under test, plus the free names those functions close over.
_WANT_FUNCS = ("compare_verdicts", "summarize", "build_questions",
               "build_state", "_parse_answer")
_WANT_NAMES = ("ANSWERED_OUTCOMES", "_BODY_CHARS")


def _load_pure():
    src = open(SHADOW, encoding="utf-8").read()
    tree = ast.parse(src)
    pieces = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _WANT_NAMES
                for t in node.targets):
            pieces.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.FunctionDef) and node.name in _WANT_FUNCS:
            pieces.append(ast.get_source_segment(src, node))
    ns = {}
    exec(compile("\n\n".join(pieces), SHADOW, "exec"), ns)
    missing = [n for n in _WANT_FUNCS + _WANT_NAMES if n not in ns]
    # ★ A harness that silently extracted nothing would make every assertion
    # below vacuous. Fail loudly if a rename outruns this list.
    assert not missing, f"AST extraction missed {missing} — update _WANT_*"
    return ns


NS = _load_pure()


# ── 1. The shadow cannot move a verdict ──────────────────────────────────

def _gate_fn():
    src = open(GATE, encoding="utf-8").read()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "editorial_gate":
            return node, src
    raise AssertionError("editorial_gate() not found in media_editorial_gate.py")


def _record_shadow_calls(fn):
    """Every record_shadow() call inside editorial_gate, paired with the
    statement that IMMEDIATELY encloses it.

    ★ Do not reach for `for stmt in ast.walk(fn): for c in ast.walk(stmt)` —
    ast.walk yields editorial_gate itself first, so every call also comes back
    parented to the FunctionDef and the Expr assertion fails on correct code.
    Build a real parent map and climb to the nearest ast.stmt instead.
    """
    parent = {}
    for node in ast.walk(fn):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    found = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "record_shadow"):
            cur = node
            while cur in parent and not isinstance(cur, ast.stmt):
                cur = parent[cur]
            found.append((cur, node))
    return found


def test_shadow_is_called_at_all():
    """If this fails the lane is dead code and every other guard is vacuous."""
    fn, _ = _gate_fn()
    assert _record_shadow_calls(fn), "editorial_gate never calls record_shadow"


def test_shadow_is_inert_return_value_discarded():
    """record_shadow's result must never be bound or branched on."""
    fn, _ = _gate_fn()
    for stmt, _call in _record_shadow_calls(fn):
        assert isinstance(stmt, ast.Expr), (
            "record_shadow's return value is used in a "
            f"{type(stmt).__name__} — the shadow lane must stay inert")


def test_shadow_is_inert_verdict_not_derived_from_it():
    """Nothing the gate returns may mention the shadow module.

    A future edit that fell back to the shadow verdict would have to name it,
    so scanning the return dicts for shadow identifiers catches the realistic
    regression without pinning the exact code shape.
    """
    fn, _ = _gate_fn()
    banned = ("record_shadow", "shadow_verdict", "jev")
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and node.value is not None:
            rendered = ast.dump(node.value).lower()
            for b in banned:
                assert b not in rendered, (
                    f"editorial_gate returns a value derived from '{b}' — "
                    "the shadow lane must not influence the verdict")


def test_shadow_runs_after_the_verdict_is_recorded():
    """Ordering is the other half of inertness: called before _record_review,
    a slow or raising shadow could still starve the durable review row."""
    fn, _ = _gate_fn()
    calls = _record_shadow_calls(fn)
    shadow_line = min(c.lineno for _s, c in calls)
    review_lines = [n.lineno for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_record_review"]
    assert review_lines, "_record_review calls not found"
    assert min(review_lines) < shadow_line, (
        "record_shadow runs before the review row is written")


# ── 2. Unanswered is never agreement ─────────────────────────────────────

def _row(outcome, inc=None, sh=None, ms=None):
    return {"outcome": outcome, "incumbent_verdict": inc,
            "shadow_verdict": sh, "latency_ms": ms}


def test_summarize_all_failures_is_not_agreement():
    """A lane that never answered must report no rate at all, not 1.0."""
    rows = [_row("http_401") for _ in range(20)]
    s = NS["summarize"](rows)
    assert s["attempts"] == 20
    assert s["answered"] == 0
    assert s["agreed"] == 0
    assert s["agreement_rate"] is None, (
        "20 rejected calls reported an agreement rate — unanswered probes are "
        "being counted as agreement")
    assert s["coverage"]["ratio"] == 0.0
    assert s["outcomes"]["http_401"] == 20


def test_summarize_zero_rows_is_not_agreement():
    s = NS["summarize"]([])
    assert s["agreement_rate"] is None
    assert s["coverage"]["ratio"] is None
    assert "NOT agreement" in s["note"], (
        "an empty table must say in words that it is not agreement")


def test_summarize_denominator_excludes_unanswered():
    """8 answered (6 agree / 2 disagree) among 20 attempts → 0.75, not 0.3."""
    rows = [_row("ok", "publish", "publish", 200) for _ in range(6)]
    rows += [_row("ok", "publish", "draft", 200) for _ in range(2)]
    rows += [_row("timeout") for _ in range(12)]
    s = NS["summarize"](rows)
    assert s["attempts"] == 20 and s["answered"] == 8
    assert s["agreed"] == 6 and s["disagreed"] == 2
    assert s["agreement_rate"] == 0.75
    assert s["coverage"]["ratio"] == 0.4


def test_summarize_unparseable_verdict_is_not_agreement():
    """outcome=ok but a garbage verdict must land in unknown, not agreed."""
    rows = [_row("ok", "publish", "maybe", 100),
            _row("ok", "publish", None, 100),
            _row("ok", "publish", "publish", 100)]
    s = NS["summarize"](rows)
    assert s["answered"] == 3
    assert s["agreed"] == 1
    assert s["unknown_verdict"] == 2
    assert s["agreement_rate"] == round(1 / 3, 4)


def test_summarize_median_latency_only_from_answered():
    rows = [_row("ok", "draft", "draft", 100), _row("ok", "draft", "draft", 300),
            _row("timeout", ms=6000)]
    assert NS["summarize"](rows)["median_latency_ms"] == 300


# ── 3. compare_verdicts keeps "unknown" distinct from "disagree" ─────────

def test_compare_verdicts():
    cmp_ = NS["compare_verdicts"]
    assert cmp_("publish", "publish") == "agree"
    assert cmp_("draft", "DRAFT ") == "agree"
    assert cmp_("publish", "draft") == "disagree"
    for bad in (None, "", "maybe", "hold", 0):
        assert cmp_("publish", bad) == "unknown", (
            f"{bad!r} was folded into a real verdict comparison")
        assert cmp_(bad, "publish") == "unknown"


# ── 4. A shape mismatch stays diagnosable ────────────────────────────────

def test_parse_answer_reports_keys_it_saw():
    """We have never seen a live Jev response. When parsing fails the caller
    records parse_error WITH raw_keys, so the first armed run tells us the
    real contract instead of looking like a model that never answers."""
    verdict, conf, keys = NS["_parse_answer"]({"unexpected": 1, "usage": {}})
    assert verdict is None
    assert "unexpected" in keys and "usage" in keys


def test_parse_answer_documented_shape():
    payload = {"choices": {"verdict": {"choice": "draft", "confidence": 0.91}}}
    verdict, conf, _ = NS["_parse_answer"](payload)
    assert verdict == "draft" and conf == 0.91


def test_parse_answer_tolerates_alternate_shapes():
    for payload in (
        {"answers": {"verdict": {"value": "publish"}}},
        {"results": {"verdict": "publish"}},
        {"choices": {"only_question": {"choice": "publish"}}},
    ):
        verdict, _c, _k = NS["_parse_answer"](payload)
        assert verdict == "publish", f"failed on {payload}"


def test_parse_answer_rejects_non_dict():
    assert NS["_parse_answer"]("nope") == (None, None, [])


# ── 5. The two desks must see the same evidence ──────────────────────────

def test_state_body_truncation_matches_the_incumbent():
    """build_state must clip the body at the same 3500 chars _ask_editor does,
    or the token comparison measures our truncation, not the model."""
    gate_src = open(GATE, encoding="utf-8").read()
    assert "[:3500]" in gate_src, "incumbent truncation changed — update _BODY_CHARS"
    assert NS["_BODY_CHARS"] == 3500
    st = NS["build_state"]("t", "x" * 9000, "cat", "- [d] (c) headline")
    assert len(st["candidate_body"]) == 3500


def test_gate_and_shadow_share_one_context_builder():
    """Both desks must judge against byte-identical headline context."""
    gate_src = open(GATE, encoding="utf-8").read()
    assert gate_src.count("def _recent_lines(") == 1
    # _ask_editor and the shadow call site both go through it.
    assert gate_src.count("_recent_lines(recent)") == 2, (
        "the headline context is no longer built by one shared helper")


def test_questions_carry_instructions_not_just_a_key_name():
    """Jev never sees the question's key, so an empty instructions/criteria
    would make the key name the only signal — which is no signal."""
    q = NS["build_questions"]()["verdict"]
    assert q["type"] == "choice"
    assert len(q["instructions"]) > 80
    assert set(q["criteria"]) == {"publish", "draft"}
    for text in q["criteria"].values():
        assert len(text) > 40
