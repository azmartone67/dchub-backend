#!/usr/bin/env python3
"""tests/test_deadman_self_beat_on_healthy_path.py — the watcher must beat its
OWN feed on every run, including the run where nothing is wrong.

★ THE DEFECT (2026-08-21). tools/deadman/watch.py writes a `deadman-watch`
liveness beat so shell #52 (a DIFFERENT scheduler) can tell when this job dies.
That block sat after `if not overdue: return` — so on every HEALTHY run, the
common case, main() returned before beating. Live: the public deadman board
read `deadman-watch OVERDUE — last success 6h ago (>2x cadence 2h)` while this
workflow had succeeded at 14:57 and 16:56; audit-closure lane A failed on "the
deadman watcher is itself alive". A watcher that only proves it is alive while
it is alarming is blind exactly when it should be reassuring.

This test EXECUTES main() with every external edge stubbed (Actions API,
ledger, beat) on an all-healthy fleet and asserts the self-beat was written.
Restoring the old order (self-beat after the early return) fails it.
"""
import importlib.util
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCH = os.path.join(ROOT, "tools", "deadman", "watch.py")


def _load():
    spec = importlib.util.spec_from_file_location("deadman_watch_under_test", WATCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run_healthy(monkeypatch):
    mod = _load()
    beats = []
    monkeypatch.setattr(mod, "beat", lambda feed, ts, status, cad: beats.append((feed, status)))
    monkeypatch.setattr(mod, "_assert_watch_margin", lambda: None)
    # Every watched workflow succeeded just now.
    monkeypatch.setattr(mod, "last_success", lambda wf: (mod.NOW.isoformat(), "success"))
    # The ledger reports nothing overdue.
    ledger = json.dumps({"tracked": 70, "any_overdue": False, "overdue": []}).encode()
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *a, **k: _Resp(ledger))
    mod.main()
    return beats


def test_self_beat_is_written_when_every_loop_is_healthy(monkeypatch, capsys):
    beats = _run_healthy(monkeypatch)
    assert ("deadman-watch", "success") in beats, (
        f"main() returned on the healthy path without beating its own feed — "
        f"beats written: {beats}")
    out = capsys.readouterr().out
    assert "DEADMAN ✓" in out, "the healthy verdict was not reached — stub drifted"
    assert "self-beat written" in out


def test_self_beat_precedes_the_healthy_early_return_in_source():
    """Belt and braces on the ORDER: the beat must come before the first
    verdict-dependent return in main(). An AST check, not a grep — comments
    cannot satisfy it."""
    import ast
    src = open(WATCH, encoding="utf-8").read()
    tree = ast.parse(src)
    main_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    beat_line = None
    healthy_return_line = None
    for node in ast.walk(main_fn):
        if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "beat"
                and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "deadman-watch"):
            beat_line = node.lineno
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print":
            for a in node.args:
                for c in ast.walk(a):
                    if isinstance(c, ast.Constant) and isinstance(c.value, str) and "DEADMAN ✓" in c.value:
                        healthy_return_line = node.lineno
    assert beat_line and healthy_return_line, "could not locate the beat or the healthy verdict"
    assert beat_line < healthy_return_line, (
        f"self-beat at line {beat_line} comes AFTER the healthy return near "
        f"line {healthy_return_line} — it will be skipped on every green run")
