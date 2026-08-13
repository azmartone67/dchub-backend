#!/usr/bin/env python3
"""tests/test_selfheal_master_shell.py — the shell that reports green-by-silence
must not commit it.

NO NETWORK, NO DB. Every lane is exercised against fakes.

The shell exists because a loop reported success while nothing moved. A shell
with the same defect would be worse than none: it would launder the problem it
was built to expose. So the properties fenced here are mostly about what the
shell does when it CANNOT see — unreadable table, unreachable gateway, crashed
lane — plus the one substantive lesson it encodes: which column means "fresh".

Run standalone:   python3 tests/test_selfheal_master_shell.py
Run under pytest: pytest tests/test_selfheal_master_shell.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from routes import selfheal_master_shell as S  # noqa: E402


# ── honesty semantics ────────────────────────────────────────────────

def test_indeterminate_is_never_silently_a_pass():
    assert S._lane_verdict([S._check("x", "x", None, "unreadable")]) == "INDETERMINATE"
    assert S._lane_verdict([S._check("x", "x", True, "ok"),
                            S._check("y", "y", None, "unreadable")]) == "INDETERMINATE"


def test_empty_lane_is_indeterminate_not_passed():
    """A lane that produced no checks measured nothing."""
    assert S._lane_verdict([]) == "INDETERMINATE"


def test_critical_fail_is_failed_and_soft_fail_is_degraded():
    assert S._lane_verdict([S._check("x", "x", False, "bad", critical=True)]) == "FAILED"
    assert S._lane_verdict([S._check("x", "x", False, "bad")]) == "DEGRADED"
    assert S._lane_verdict([S._check("x", "x", True, "ok")]) == "PASSED"


def test_crashed_lane_is_indeterminate():
    def boom():
        raise RuntimeError("boom")
    assert S._lane_verdict(S._safe_lane(boom)) == "INDETERMINATE"


# ── lane 1 · the column that actually means "fresh" ──────────────────

def test_upsert_tables_are_not_measured_on_created_at():
    """power_plants was called 12 days stale on 2026-08-12 by reading created_at
    (frozen 07-31 by the upsert) while last_updated showed 14,474 of 14,480 rows
    touched that morning. The wrong column manufactures false alarms AND false
    calm, so the choice is pinned."""
    cols = {t: c for t, c, _, _ in S.FRESHNESS}
    assert cols["power_plants"] == "last_updated", "upsert loader — created_at freezes"
    assert cols["transmission_lines"] == "last_updated"
    assert cols["gas_pipelines"] == "last_updated"
    # r-newsdead (2026-08-13): this line used to read `cols["news"] == "created_at"`
    # and was WRONG on both halves. `news` is the abandoned table — nothing has
    # written it since the live loader moved to news_articles — and created_at
    # there is NULL for every row. The live signal is news_articles.fetched_at,
    # the loader's own heartbeat. published_at will not do: feeds publish weeks
    # ahead (max is 2026-09-21), so it reads fresh long after the loader dies.
    assert cols["news_articles"] == "fetched_at"
    assert "news" not in cols, "the abandoned news table must not be tracked"


def test_every_freshness_entry_documents_its_column_choice():
    for table, col, sla, why in S.FRESHNESS:
        assert isinstance(sla, int) and sla > 0, f"{table}: SLA must be a positive hour count"
        assert why and len(why) > 20, (
            f"{table}: the column choice must carry its reason — an undocumented "
            f"column is how the wrong one gets picked next time"
        )


class _FakeConn:
    """Returns None for every scalar — i.e. the table could not be read."""
    def __init__(self, value=None):
        self._v = value

    def cursor(self):
        outer = self

        class _C:
            def execute(self, *a, **k):
                if outer._v is Exception:
                    raise RuntimeError("unreadable")

            def fetchone(self):
                return None if outer._v is None else (outer._v,)

            def fetchall(self):
                return []
        return _C()


def test_unreadable_table_is_indeterminate_not_fresh():
    """The bug this shell hunts is a green signal over no evidence. A table we
    cannot read is NOT fresh."""
    checks = S._lane_movement(_FakeConn(None))
    assert checks, "lane must emit a check per table even when unreadable"
    assert all(c["status"] == "INDETERMINATE" for c in checks)
    assert S._lane_verdict(checks) == "INDETERMINATE"


# ── lane 3 · ask the live surface, not the repo ──────────────────────

def test_unreachable_gateway_is_indeterminate_not_pass(monkeypatch):
    monkeypatch.setattr(S, "_live_tools_payload", lambda *a, **k: None)
    checks = S._lane_live_surface(True)
    probe = [c for c in checks if c["id"] == "L3.1"][0]
    assert probe["status"] == "INDETERMINATE", (
        "an unreachable gateway says nothing about what agents are served"
    )
    assert S._lane_verdict(checks) == "INDETERMINATE"


def test_skipping_the_probe_does_not_report_success(monkeypatch):
    assert S._lane_verdict(S._lane_live_surface(False)) == "INDETERMINATE", (
        "opting out of the check must not manufacture a pass"
    )


def test_broken_example_still_served_is_a_critical_fail(monkeypatch):
    monkeypatch.setattr(S, "_live_tools_payload",
                        lambda *a, **k: 'blah Try: list_transactions year=2026 blah')
    monkeypatch.setattr(S, "_fallback_worker_text", lambda *a, **k: "clean")
    checks = S._lane_live_surface(True)
    assert S._lane_verdict(checks) == "FAILED"
    probe = [c for c in checks if c["id"] == "L3.1"][0]
    assert "list_transactions year=2026" in probe["detail"]


def test_clean_gateway_passes(monkeypatch):
    monkeypatch.setattr(S, "_live_tools_payload",
                        lambda *a, **k: '{"tools":[{"name":"list_transactions"}]}')
    monkeypatch.setattr(S, "_fallback_worker_text", lambda *a, **k: "clean")
    checks = S._lane_live_surface(True)
    assert S._lane_verdict(checks) == "PASSED"


def test_lane3_never_reports_a_repo_version_as_its_basis():
    """★ the correction this file exists for.

    The first version of lane 3 opened with "repo worker.js WORKER_VERSION =
    ...". That is dchub-backend/worker.js — MCP_FALLBACK_TOOLS, which serves
    only when the origin is down. It would have read as reassuring while every
    agent was still served broken text from dchub-mcp-server. A basis that
    cannot see the thing it measures is worse than no check."""
    import ast
    import inspect
    # Strip the docstring and comments: this function's docstring NAMES the
    # removed behaviour on purpose, and a substring match on prose would read
    # that history as the defect. Same trap the password-reset guard hit.
    tree = ast.parse(inspect.getsource(S._lane_live_surface).lstrip())
    fn = tree.body[0]
    body = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)) else fn.body
    code = "\n".join(ast.unparse(n) for n in body)
    assert "WORKER_VERSION" not in code, (
        "lane 3 must not present a repo version as evidence about the live surface"
    )


def test_stale_fallback_is_reported(monkeypatch):
    """The fallback IS the answer when the origin is down, so drift there is a
    latent regression that only surfaces during an incident."""
    monkeypatch.setattr(S, "_live_tools_payload", lambda *a, **k: "clean live")
    monkeypatch.setattr(S, "_fallback_worker_text",
                        lambda *a, **k: "MCP_FALLBACK_TOOLS ... get_news topic=AI ...")
    checks = S._lane_live_surface(True)
    fb = [c for c in checks if c["id"] == "L3.2"][0]
    assert fb["status"] == "FAIL"
    assert S._lane_verdict(checks) == "DEGRADED", (
        "a stale fallback degrades the lane without masking a healthy live path"
    )


def test_unreadable_fallback_is_indeterminate(monkeypatch):
    monkeypatch.setattr(S, "_live_tools_payload", lambda *a, **k: "clean live")
    monkeypatch.setattr(S, "_fallback_worker_text", lambda *a, **k: None)
    checks = S._lane_live_surface(True)
    assert S._lane_verdict(checks) == "INDETERMINATE"


def test_broken_examples_list_is_not_empty():
    """An empty watchlist would make lane 3 vacuously green."""
    assert len(S.BROKEN_EXAMPLES) >= 4


# ── config sanity ────────────────────────────────────────────────────

def test_confidence_floor_would_have_caught_inv_100046():
    """#100046 recorded confidence 0.15 and refutation.survived=false, said in
    its own words that no mechanical fix could be asserted, and still became a
    merged PR. The floor must sit above it."""
    assert S.CONFIDENCE_FLOOR > 0.15


if __name__ == "__main__":
    class _MP:
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)

        def undo(self):
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)

    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if not (_name.startswith("test_") and callable(_fn)):
            continue
        _mp = _MP()
        try:
            if "monkeypatch" in _fn.__code__.co_varnames[:_fn.__code__.co_argcount]:
                _fn(_mp)
            else:
                _fn()
            print(f"✓ {_name}")
        except AssertionError as _e:
            _failed += 1
            print(f"✗ {_name}: {_e}")
        finally:
            _mp.undo()
    print(f"\n{'FAILED' if _failed else 'PASSED'} — {_failed} failure(s)")
    sys.exit(1 if _failed else 0)
