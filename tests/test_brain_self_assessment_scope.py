"""Guard the SCOPE framing on the brain self-assessment payload.

/api/v1/brain/self-assessment is public (200, no key) and is advertised to
agents in /.well-known/ai-agents.json. /api/v1/status re-publishes the same
grade + rationale to anonymous callers. Both hand a reader `"grade": "C"`.

The grade covers the brain's SELF-REPAIR LOOP, not the infrastructure data DC
Hub serves. Without an explicit scope field a caller reasonably reads a C as
"the data is unreliable" — a claim the endpoint never makes. The old `purpose`
line ("Agents should fall back to deterministic logic when grade is C or
below") made that misreading worse by sounding like blanket advice to distrust
DC Hub.

These assert the scope block exists on BOTH publishers and that `purpose` stays
scoped. Dropping either republishes the ambiguity.

NOTE: the CI unit-tests job installs ONLY pytest (not requirements.txt), so
importing these Flask modules crashes collection. We AST-parse the source
instead. AST also ignores COMMENTS — which matters here, because the source
comments explaining this change contain the word "scope"; a plain substring
check on the file text would pass on the comment alone.
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRAIN = os.path.join(ROOT, "routes", "brain_learning.py")
STATUS = os.path.join(ROOT, "routes", "status_api.py")


def _func(path, name):
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in {path} — renamed?")


def _dict_with_key(fn, key):
    """The dict literal inside fn that has `key` among its literal keys."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if key in keys:
                return node
    return None


def _get(d, key):
    for k, v in zip(d.keys, d.values):
        if isinstance(k, ast.Constant) and k.value == key:
            return v
    return None


# ── self-assessment payload ──────────────────────────────────────────

def _payload():
    fn = _func(BRAIN, "brain_self_assessment")
    d = _dict_with_key(fn, "purpose")
    assert d is not None, "no payload dict with a 'purpose' key"
    return d


def test_payload_carries_scope_block():
    scope = _get(_payload(), "scope")
    assert scope is not None, (
        "self-assessment payload lost its 'scope' block — a public 'grade' "
        "with no statement of what is graded reads as a claim about the data")
    assert isinstance(scope, ast.Dict), "scope must be a literal dict"
    keys = [k.value for k in scope.keys if isinstance(k, ast.Constant)]
    for required in ("grades", "does_not_grade", "detail"):
        assert required in keys, f"scope block is missing '{required}'"


def test_scope_names_the_loop_and_disclaims_the_data():
    scope = _get(_payload(), "scope")
    grades = _get(scope, "grades")
    not_graded = _get(scope, "does_not_grade")
    assert isinstance(grades, ast.Constant) and "loop" in grades.value, (
        f"scope.grades must name the self-repair loop, got {grades!r}")
    assert isinstance(not_graded, ast.Constant) and "data" in not_graded.value, (
        f"scope.does_not_grade must disclaim the data, got {not_graded!r}")
    detail = _get(scope, "detail")
    assert isinstance(detail, ast.Constant)
    low = detail.value.lower()
    assert "says nothing" in low, "scope.detail must disclaim the data outright"
    # Point the reader at the real coverage signals rather than leaving them
    # with only a letter.
    assert "coverage" in low or "freshness" in low, (
        "scope.detail should redirect to the actual data-limit surfaces")


def test_purpose_is_scoped_not_blanket_distrust():
    purpose = _get(_payload(), "purpose")
    assert isinstance(purpose, ast.Constant), "purpose must be a literal string"
    val = purpose.value
    # The exact pre-2026-09-18 sentence, which read as blanket advice to
    # distrust DC Hub rather than advice about brain-proposed auto-fixes.
    assert "Brain's letter-grade self-assessment. Agents should fall back" \
        not in val, "purpose reverted to the unscoped wording"
    assert "scope" in val, "purpose must point the reader at the scope block"
    low = val.lower()
    assert "auto-fix" in low or "proposed" in low, (
        "purpose must say WHAT the fallback advice applies to")


# ── /api/v1/status, the second public publisher ──────────────────────

def test_status_forwards_scope():
    fn = _func(STATUS, "_build_status_payload")
    brain = _dict_with_key(fn, "rationale")
    assert brain is not None, "no brain block with a 'rationale' key"
    scope = _get(brain, "scope")
    assert scope is not None, (
        "/api/v1/status publishes grade + rationale anonymously but dropped "
        "'scope' — it would republish the exact ambiguity self-assessment fixed")
    # Must forward the upstream value, not hardcode a second copy that can
    # drift away from what self-assessment actually says.
    assert isinstance(scope, ast.Call), "status scope must forward sa.get('scope')"
    assert any(isinstance(a, ast.Constant) and a.value == "scope"
               for a in scope.args), "status must read the upstream 'scope' key"
