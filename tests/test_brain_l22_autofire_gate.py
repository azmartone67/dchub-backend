"""Guard the autonomous Inspector→L22 auto-fire gate.

The Inspector loop hands code-fix candidates to L22 after every brief (hook E
in routes/brain_inspector._generate_brief). That arm is autonomous, so it must
be DRY-RUN by default — opening Issues/PRs only when BRAIN_L22_AUTOPR_LIVE=1.

brain_inspector imports Flask at module load and the CI unit-tests job installs
only pytest, so we AST-extract just _l22_autofire_mode and exec it in an
isolated namespace seeded with os — same pattern as the other pure-fn tests.
"""
import os
import ast

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BI = os.path.join(ROOT, "routes", "brain_inspector.py")


def _load_mode_fn():
    src = open(BI, encoding="utf-8").read()
    tree = ast.parse(src)
    seg = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_l22_autofire_mode":
            seg = ast.get_source_segment(src, node)
            break
    assert seg, "_l22_autofire_mode not found in brain_inspector.py"
    ns = {"os": os}
    exec(compile(seg, BI, "exec"), ns)
    return ns["_l22_autofire_mode"]


_mode = _load_mode_fn()


def _run(env_val):
    # set/clear the env var, call, restore
    prev = os.environ.get("BRAIN_L22_AUTOPR_LIVE")
    try:
        if env_val is None:
            os.environ.pop("BRAIN_L22_AUTOPR_LIVE", None)
        else:
            os.environ["BRAIN_L22_AUTOPR_LIVE"] = env_val
        return _mode()
    finally:
        if prev is None:
            os.environ.pop("BRAIN_L22_AUTOPR_LIVE", None)
        else:
            os.environ["BRAIN_L22_AUTOPR_LIVE"] = prev


def test_default_is_dry_run():
    mode, path = _run(None)
    assert mode == "dry_run"
    assert path.endswith("/auto-code/dry-run")


def test_zero_is_dry_run():
    mode, path = _run("0")
    assert mode == "dry_run"
    assert path.endswith("/auto-code/dry-run")


def test_one_is_live():
    mode, path = _run("1")
    assert mode == "live"
    assert path.endswith("/auto-code/run")
    assert not path.endswith("/dry-run")


def test_arbitrary_truthy_string_is_still_dry_run():
    # Only the exact literal "1" flips to live — "true"/"yes" stay safe.
    for val in ("true", "yes", "LIVE", "2", " 1 x"):
        mode, _ = _run(val)
        assert mode == "dry_run", f"{val!r} should stay dry_run"


def test_whitespace_padded_one_is_live():
    # .strip() means " 1 " still means live (matches operator intent).
    mode, _ = _run("  1  ")
    assert mode == "live"
