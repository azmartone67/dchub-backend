"""Lane 6 has a CALL SITE, not just a module.

★ WHY THIS FILE EXISTS. tools/qa_superuser/claims.py shipped complete and
correct on 2026-08-29 and registered nothing for the rest of the day, because
run.py — the entrypoint the workflow actually executes (`python3 -m
tools.qa_superuser.run`) — never imported it. Measured that evening: the
harness had run 0.7h earlier with status=success, and the ledger held 58
claims, none of kind 'qa'. The only references to register_run_claims() in the
whole repo were its own definition and tests/test_qa_as_claims.py.

Those tests were not wrong; they were the wrong SHAPE. Every one of them calls
register_run_claims() directly, so deleting the call site leaves them green —
the same vacuous-guard pattern this repo has now hit three times (lane 3's
decision_schema() tested in isolation while its call site was reverted; the
brain_findings prefix guard that matched brain_findings_DISABLED).

So these tests drive main(). They fail if the registration is removed from the
entrypoint, which is the only failure that matters.

Run:  python3 -m pytest tests/test_qa_claims_callsite.py -v
"""
from __future__ import annotations

import json

import pytest

from tools.qa_superuser import claims as CL
from tools.qa_superuser import config as C
from tools.qa_superuser import run as R
from tools.qa_superuser.finding import INFO, PASS, RED, Finding


def _f(key, verdict=PASS, metric="get:/x n", expect=">= 1"):
    return Finding(key=key, surface="contract", seat="anon",
                   title="a check", verdict=verdict, severity=INFO,
                   evidence="observed", basis="GET /x", red_when="it breaks",
                   claim_metric=metric, claim_expect=expect)


@pytest.fixture
def harness(monkeypatch):
    """A run whose canary fired, with one registrable PASS. Records every
    register(...) the entrypoint makes."""
    calls = []

    def _register(**kw):
        calls.append(kw)
        return {"ok": True}

    monkeypatch.setattr(R, "collect", lambda: ([_f("a"), _f("b", verdict=RED)],
                                               True))
    monkeypatch.setattr(R, "http_register", lambda *a, **k: _register)
    monkeypatch.setattr(C, "DRY_RUN", False, raising=False)
    # main() does `from .board import actuate` lazily; patch the attribute so
    # the entrypoint's OTHER write stays out of this test.
    import tools.qa_superuser.board as B
    monkeypatch.setattr(B, "actuate", lambda run: None)
    return calls


# ── the call site ────────────────────────────────────────────────────────

def test_the_entrypoint_registers_qa_claims(harness, capsys):
    """★THE GUARD. Delete claims_for(...) from main() and this goes red.

    Named defect: run.py collects findings and never reaches the ledger, so
    the harness reports success while registering zero claims.
    """
    rc = R.main([])
    assert rc == 0
    assert len(harness) == 1, ("main() registered nothing — the qa-claims "
                               "call site is missing from the entrypoint")
    sent = harness[0]
    assert sent["kind"] == "qa"
    assert sent["subject"] == "qa:a"
    assert sent["expected_metric"] == "get:/x n"
    assert sent["expected_value"] == ">= 1"
    assert sent["horizon_hours"] == CL.DEFAULT_HORIZON_HOURS


def test_the_run_record_carries_the_claims_block(harness):
    """The board consumes `run`; coverage has to travel with it or the
    dashboard shows a green with no idea how much of it is re-asked."""
    seen = {}
    import tools.qa_superuser.board as B
    B_actuate = lambda run: seen.update(run)          # noqa: E731
    import pytest as _p
    with _p.MonkeyPatch.context() as mp:
        mp.setattr(B, "actuate", B_actuate)
        R.main([])
    assert "claims" in seen, "run record has no claims block"
    assert seen["claims"]["registered"] == 1
    assert seen["claims"]["backed"] == ["a"]
    # The RED is reported as unbacked WITH a reason, never silently dropped.
    assert "b" in seen["claims"]["unbacked"]


def test_json_output_carries_the_claims_block(harness, capsys):
    R.main(["--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["claims"]["registered"] == 1
    assert out["claims"]["coverage_of_passes"] == 1.0


# ── the gates ────────────────────────────────────────────────────────────

def test_no_actuate_plans_but_never_registers(harness):
    """Registering is a WRITE. --no-actuate must leave the ledger alone."""
    R.main(["--no-actuate"])
    assert harness == [], "--no-actuate wrote to the claim ledger"


def test_dry_run_plans_but_never_registers(harness, monkeypatch):
    monkeypatch.setattr(C, "DRY_RUN", True, raising=False)
    R.main([])
    assert harness == [], "DRY_RUN wrote to the claim ledger"


# ── the two directions of "cannot tell" ──────────────────────────────────

def test_a_missing_admin_key_is_reported_not_counted_as_zero(monkeypatch):
    """★ The dangerous direction. `registered: 0` with an absent credential
    reads as 'there was nothing to register' — the exact silent green this
    lane exists to end. It must come back None WITH a named reason."""
    monkeypatch.setattr(R, "collect", lambda: ([_f("a")], True))
    monkeypatch.setattr(R, "http_register", lambda *a, **k: None)
    monkeypatch.setattr(C, "DRY_RUN", False, raising=False)
    import tools.qa_superuser.board as B
    monkeypatch.setattr(B, "actuate", lambda run: None)
    out = R.claims_for([_f("a")], planned_only=False)
    assert out["registered"] is None, "a missing key was reported as zero"
    assert any("MISSING CREDENTIAL" in e for e in out["errors"])


def test_an_unproven_harness_registers_nothing(monkeypatch):
    """★ The other direction. When the canary does NOT fire, invalidate()
    demotes every PASS to BLIND, so a run that could not prove itself cannot
    mint reassurance. Load-bearing: if this ever stops holding, a broken
    harness starts registering greens."""
    calls = []
    monkeypatch.setattr(R, "collect", lambda: ([_f("a")], False))
    monkeypatch.setattr(R, "http_register",
                        lambda *a, **k: lambda **kw: calls.append(kw) or {"ok": True})
    monkeypatch.setattr(C, "DRY_RUN", False, raising=False)
    import tools.qa_superuser.board as B
    monkeypatch.setattr(B, "actuate", lambda run: None)
    rc = R.main([])
    assert rc == 2, "a run whose canary did not fire must exit non-zero"
    assert calls == [], "an unproven harness registered claims"


# ── the transport ────────────────────────────────────────────────────────

def test_http_register_is_none_without_a_key(monkeypatch):
    monkeypatch.setattr(C, "ADMIN_KEY", "", raising=False)
    assert CL.http_register() is None


def test_http_register_targets_the_origin_never_the_edge(monkeypatch):
    """★ Admin POSTs through the CF edge die on the zone's 15s route timeout.
    board.py already targets the origin for this reason."""
    monkeypatch.setattr(C, "ADMIN_KEY", "k", raising=False)
    monkeypatch.setattr(C, "ORIGIN", "https://origin.example", raising=False)
    monkeypatch.setattr(C, "EDGE", "https://dchub.cloud", raising=False)
    seen = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    def _post(url, **kw):
        seen["url"] = url
        seen["headers"] = kw.get("headers") or {}
        return _Resp()

    import sys
    import types
    fake = types.ModuleType("requests")
    fake.post = _post
    monkeypatch.setitem(sys.modules, "requests", fake)

    reg = CL.http_register()
    assert reg(kind="qa") == {"ok": True}
    assert seen["url"] == "https://origin.example/api/v1/brain/claims"
    assert "dchub.cloud" not in seen["url"]
    assert seen["headers"]["X-Admin-Key"] == "k"
