"""Guard the autonomous Inspector→L22 auto-fire gate.

The Inspector loop hands code-fix candidates to L22 after every brief (hook E
in routes/brain_inspector._generate_brief). That arm is autonomous but
productive (it fills the /brain/innovation proposal stream), so it stays LIVE
by default with a master kill-switch: BRAIN_L22_AUTOPR_LIVE=0 flips it to
dry-run.

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


def test_default_is_live():
    # Productive flywheel stays on unless explicitly killed.
    mode, path = _run(None)
    assert mode == "live"
    assert path.endswith("/auto-code/run")
    assert not path.endswith("/dry-run")


def test_one_is_live():
    mode, path = _run("1")
    assert mode == "live"
    assert path.endswith("/auto-code/run")


def test_zero_is_the_kill_switch():
    mode, path = _run("0")
    assert mode == "dry_run"
    assert path.endswith("/auto-code/dry-run")


def test_only_exact_zero_kills_it():
    # The kill-switch is the literal "0" only — any other value stays live,
    # so a typo'd flag can't silently throttle the productive arm.
    for val in ("1", "true", "yes", "LIVE", "00", "0 x", "false"):
        mode, _ = _run(val)
        assert mode == "live", f"{val!r} should stay live"


def test_whitespace_padded_zero_is_kill_switch():
    # .strip() means "  0  " still kills it (matches operator intent).
    mode, _ = _run("  0  ")
    assert mode == "dry_run"
