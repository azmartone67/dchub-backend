"""/.well-known/mcp.json must stay GENERATED; its static twin must stay unserved.

Two files in this repo are named `.well-known/mcp.json` and neither is the
document the world receives:

    .well-known/mcp.json          version 2.0.0, a `tiers` dict
    static/.well-known/mcp.json   version 1.0.0, a `tiers` dict
    LIVE  /.well-known/mcp.json   version 2.12.18, a prose `pricing` block
                                  plus anchor_intents + problem_taxonomy

The live one is built by main.py's handle_well_known() before_request branch.
Verified 2026-09-18: /.well-known/mcp.json → 200 with version 2.12.18 and
$9/$49/$99, while /static/.well-known/mcp.json → 404.

WHY THE TWINS ARE KEPT, NOT DELETED. main.py's own allowlist comment already
decided this, and the reasoning is worth not re-litigating:

    ★ Named EXPLICITLY, never a blanket "serve anything in
    static/.well-known". agent.json, ai-agents.json, ai-plugin.json and
    mcp.json all have DYNAMIC handlers in this same chain AND a stale static
    twin on disk; a catch-all here runs before them and would silently start
    serving the stale copy — trading a visible 404 for an invisible wrong
    answer.

There is also precedent against deleting-as-cleanup: the 2026-08-20 sweep
recorded in that same comment found copilot-agent.json, gemini.json and
mcp_facts.json present-but-404, and the fix was to START SERVING them. A file
sitting unserved in static/.well-known/ has previously meant a missing route,
not a dead file.

WHAT THIS PINS. The fence, so it cannot be removed by accident:
  1. the disk-serving allowlist must NOT list /.well-known/mcp.json;
  2. a dedicated /.well-known/mcp.json branch must still exist;
  3. that branch must not read a file — it generates;
  4. both twins must say on their face that they are not served.

Pure: no DB, no network, and it never imports main (tests/ must not). main.py
is read with `ast`; a failed parse or a missing handler FAILS here rather
than passing vacuously.
"""
import ast
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_HANDLER = "handle_well_known"
_TARGET = "/.well-known/mcp.json"
_TWINS = (".well-known/mcp.json", "static/.well-known/mcp.json")


def _handler_node():
    """The handle_well_known function node. Never None — this raises instead."""
    src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    assert src.strip(), "main.py is empty — this guard would pass vacuously"
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == _HANDLER:
            return node
    raise AssertionError(
        "main.py has no %s() — it was renamed or removed, and this guard now "
        "protects nothing. Re-point it at the new handler." % _HANDLER)


def _membership_tuples(fn):
    """Every `path in (...)` string tuple inside the handler."""
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], ast.In):
            continue
        for comp in node.comparators:
            if isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                vals = [e.value for e in comp.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                if vals:
                    out.append(vals)
    return out


def _eq_branches(fn, target):
    """Every `if path == target:` If-node inside the handler."""
    found = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (isinstance(test, ast.Compare) and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == target):
            found.append(node)
    return found


def test_the_handler_is_still_findable_and_still_branches():
    """Anti-vacuity floor for everything below."""
    fn = _handler_node()
    tuples = _membership_tuples(fn)
    assert tuples, (
        "no `path in (...)` allowlist found in %s() — the disk-serving "
        "allowlist this guard exists to police has moved or gone." % _HANDLER)
    assert any("/.well-known/" in v for group in tuples for v in group), (
        "the allowlist no longer mentions any .well-known path")


def test_the_static_twin_is_not_on_the_disk_serving_allowlist():
    """A catch-all or an added entry here would shadow the dynamic handler
    with the stale twin — a visible 404 traded for an invisible wrong answer."""
    fn = _handler_node()
    offenders = [group for group in _membership_tuples(fn) if _TARGET in group]
    assert not offenders, (
        "%s is listed in a disk-serving allowlist in %s(). That branch reads "
        "static/.well-known/<file> off disk and runs BEFORE the generated "
        "branch, so the stale twin (version 2.0.0 / 1.0.0) would start being "
        "served in place of the generated manifest (2.12.18). Serve the "
        "generated document, or delete the twins — not both."
        % (_TARGET, _HANDLER))


def test_the_generated_branch_still_exists_and_reads_no_file():
    fn = _handler_node()
    branches = _eq_branches(fn, _TARGET)
    assert len(branches) == 1, (
        "expected exactly one `path == %r` branch in %s(), found %d"
        % (_TARGET, _HANDLER, len(branches)))
    reads = [n for n in ast.walk(branches[0])
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "open"]
    assert not reads, (
        "the %s branch opens a file. This surface is GENERATED; reading the "
        "on-disk twin here is the exact substitution this guard prevents."
        % _TARGET)


def test_both_twins_declare_on_their_face_that_they_are_not_served():
    for rel in _TWINS:
        path = REPO_ROOT / rel
        assert path.exists(), (
            "%s is gone. If the twins were deliberately deleted, delete this "
            "guard in the same change — do not leave it asserting on nothing."
            % rel)
        doc = json.loads(path.read_text(encoding="utf-8"))
        note = doc.get("_comment", "")
        assert "NOT SERVED" in note, (
            "%s carries no `_comment` saying it is not the served artifact. "
            "Without it the next person edits this file expecting a live "
            "change — which is how it accumulated two repricings of drift."
            % rel)


def test_the_twins_are_not_the_live_document():
    """They must stay recognisably different from what is served, so nobody
    'fixes' one into looking authoritative. The live manifest carries
    anchor_intents + problem_taxonomy and a prose `pricing` block; these
    carry a `tiers` dict and neither key."""
    for rel in _TWINS:
        doc = json.loads((REPO_ROOT / rel).read_text(encoding="utf-8"))
        live_only = [k for k in ("anchor_intents", "problem_taxonomy", "pricing")
                     if k in doc]
        assert not live_only, (
            "%s has grown %s — keys that belong to the GENERATED manifest. "
            "This file is not served; making it resemble the live document "
            "invites someone to wire it up." % (rel, live_only))
