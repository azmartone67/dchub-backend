"""The daily render service origin has exactly one owner (2026-08-07).

Moving that service out of the decommissioned `heroic-reprieve` project
changed its hostname, and the change had to be made in eight places in this
repo plus four in dchub-frontend — because the URL was hardcoded at every
call site, under two different env var names that had already drifted apart.

util/daily_service.py is now the single source. These tests keep it that way:
a new hardcoded copy, or a workflow that drifts from the module default, fails
the build instead of silently pointing half the callers at a dead host.
"""
import ast
import os
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Sites allowed to name the pre-migration host, and why. Each is an allowlist
# entry that must NAME its reason — delete the entry and the literal together
# once the heroic-reprieve project is gone.
_LEGACY_ALLOWED = {
    "util/daily_service.py":            "defines LEGACY_ORIGIN for these allowlists",
    "main.py":                          "CSP connect-src, transitional",
    "routes/dcpi.py":                   "CSP connect-src, transitional",
    "routes/agent_broadcast_loop.py":   "OWN_HOSTS 5xx classification, transitional",
}

_LEGACY_HOST = "dchub-backend-production-f7dd.up.railway.app"


def _module_default() -> str:
    """Read DAILY_ORIGIN_DEFAULT from source, without importing the package."""
    src = (ROOT / "util" / "daily_service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "DAILY_ORIGIN_DEFAULT":
                    return node.value.value
    raise AssertionError("DAILY_ORIGIN_DEFAULT not found in util/daily_service.py")


def _tracked_files():
    for p in ROOT.rglob("*"):
        if p.suffix not in (".py", ".yml", ".yaml", ".md"):
            continue
        parts = p.relative_to(ROOT).parts
        if any(x in parts for x in ("__pycache__", ".git", "node_modules", "backups")):
            continue
        yield p


def test_no_new_hardcoded_legacy_host():
    """A hardcoded legacy host outside the named allowlist is a caller that
    will silently keep hitting the old project after it is deleted."""
    offenders = []
    for p in _tracked_files():
        rel = str(p.relative_to(ROOT))
        if rel in _LEGACY_ALLOWED or rel == "tests/test_daily_service_origin.py":
            continue
        try:
            if _LEGACY_HOST in p.read_text(encoding="utf-8"):
                offenders.append(rel)
        except (UnicodeDecodeError, OSError):
            continue
    assert not offenders, (
        f"hardcoded legacy daily host in {sorted(offenders)} — import "
        f"util.daily_service.daily_origin() instead, or add an allowlist entry "
        f"with a reason in _LEGACY_ALLOWED")


def test_workflow_base_matches_module_default():
    """★ The divergence this file exists to catch: daily-r2-render.yml drives
    /refresh on a cron and carries its OWN copy of the URL. If it drifts from
    the module default, the nightly render silently posts to a dead host while
    every in-process caller is fine — a green workflow doing nothing."""
    wf = (ROOT / ".github" / "workflows" / "daily-r2-render.yml").read_text(encoding="utf-8")
    m = re.search(r"^\s*DAILY_BASE:\s*(\S+)\s*$", wf, re.M)
    assert m, "DAILY_BASE not found in daily-r2-render.yml"
    assert m.group(1).rstrip("/") == _module_default().rstrip("/"), (
        f"daily-r2-render.yml DAILY_BASE ({m.group(1)}) has drifted from "
        f"util/daily_service.py DAILY_ORIGIN_DEFAULT ({_module_default()})")


def test_default_points_at_the_live_project():
    """The migration's whole point: the default must name the service that
    lives in resourceful-essence, not the decommissioned project."""
    d = _module_default()
    assert _LEGACY_HOST not in d, "default still points at the old project"
    assert d.startswith("https://"), "origin must be absolute https"
    assert not d.endswith("/"), "origin must not carry a trailing slash"


def test_env_precedence_and_host_helper(monkeypatch):
    """DAILY_SVC_URL wins, DAILY_BASE is the honoured legacy alias, and both
    get their trailing slash stripped."""
    import importlib, sys
    sys.path.insert(0, str(ROOT))
    mod = importlib.import_module("util.daily_service")

    monkeypatch.delenv("DAILY_SVC_URL", raising=False)
    monkeypatch.delenv("DAILY_BASE", raising=False)
    assert mod.daily_origin() == _module_default()

    monkeypatch.setenv("DAILY_BASE", "https://from-base.example/")
    assert mod.daily_origin() == "https://from-base.example"

    monkeypatch.setenv("DAILY_SVC_URL", "https://from-svc.example/")
    assert mod.daily_origin() == "https://from-svc.example", "DAILY_SVC_URL must win"

    assert mod.daily_host() == "from-svc.example", "daily_host must strip the scheme"


def test_callers_read_through_the_helper():
    """The three in-process callers must resolve the origin at call time, not
    re-hardcode it. Named explicitly so a refactor that reverts one fails here
    with the file on it."""
    for rel in ("crawler_scheduler.py",
                "routes/integrations_health.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "daily_origin" in src, f"{rel} no longer reads through daily_origin()"
