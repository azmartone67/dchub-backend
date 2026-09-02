"""D5 + D3 (QA sweep 2026-09-02) — reading a shell must not beat its feed, and
every ledger-beating shell's dashboard must actually render.

D5, MEASURED: a GET of /api/v1/admin/agent-pay-shell at 00:25:43.818Z; the
deadman board at 00:29Z showed `agent-pay-shell-daily last_run=
2026-09-02T00:25:43.818163` (same for loop-control 00:25:34, webmcp 00:25:42).
dashboard() -> _run_tick() -> _beat_ledger(): the READ wrote the ledger, and
every dashboard carries <meta http-equiv='refresh' content='120'>, so a browser
tab left open keeps a dead cron "alive" forever — exactly the failure the
ledger exists to catch. Only the scheduled POST master-tick may beat.

D3, MEASURED: GET /api/v1/admin/context-integrity -> 500 "unsupported format
character ';' (0x3b) at index 252"; GET /api/v1/admin/growth-funnel -> 500,
same error at index 516. `width:100%;` inside a %-formatted HTML string. The
POST master-tick kept working, so nobody noticed the GET was dead. This file
renders every beating shell's dashboard through its real formatter, with the
network and DB unreachable, and demands a 200.

Each shell runs with requests/urllib/psycopg2 stubbed dead and no
DATABASE_URL, so lanes read '?' and the tick completes in-process; the
module's own _beat_ledger is replaced by a recorder.
"""
from __future__ import annotations

import glob
import os

import pytest

flask = pytest.importorskip("flask")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "test-admin-key-d5"

# module stem -> (GET dashboard path, master-tick path, master-tick accepts GET?)
SHELLS = {
    "agent_pay":              ("/api/v1/admin/agent-pay-shell", "/api/v1/admin/agent-pay-shell/master-tick", True),
    "agentic_loop":           ("/admin/agentic-loop", "/api/v1/brain/agentic-loop/master-tick", False),
    "audit_closure":          ("/api/v1/admin/audit-closure", "/api/v1/admin/audit-closure/master-tick", True),
    "brain_ascension":        ("/api/v1/admin/brain-ascension", "/api/v1/admin/brain-ascension/master-tick", True),
    "checkout_integrity":     ("/api/v1/admin/checkout-integrity", "/api/v1/admin/checkout-integrity/master-tick", True),
    "context_integrity":      ("/api/v1/admin/context-integrity", "/api/v1/admin/context-integrity/master-tick", True),
    "fix_closure":            ("/api/v1/admin/fix-closure", "/api/v1/admin/fix-closure/master-tick", True),
    "growth_funnel":          ("/api/v1/admin/growth-funnel", "/api/v1/admin/growth-funnel/master-tick", True),
    "growthfix":              ("/api/v1/admin/growthfix", "/api/v1/admin/growthfix/master-tick", True),
    "intelligence_expansion": ("/api/v1/admin/intelligence-expansion", "/api/v1/admin/intelligence-expansion/master-tick", True),
    "loop_flywheel":          ("/api/v1/admin/loop-flywheel", "/api/v1/admin/loop-flywheel/master-tick", True),
    "loop_control":           ("/api/v1/admin/loop-control", "/api/v1/admin/loop-control/master-tick", True),
    "relay_closure":          ("/api/v1/admin/relay-closure-shell", "/api/v1/admin/relay-closure-shell/master-tick", True),
    "seven_levers":           ("/api/v1/admin/seven-levers", "/api/v1/admin/seven-levers/master-tick", True),
    "surface_truth":          ("/api/v1/admin/surface-truth", "/api/v1/admin/surface-truth/master-tick", True),
    "webmcp":                 ("/api/v1/admin/webmcp", "/api/v1/admin/webmcp/master-tick", True),
}


def _beating_stems() -> set[str]:
    """Every master shell that writes the dead-man ledger (same rule as
    tests/test_shell_beat_reports_red.py)."""
    out = set()
    for path in glob.glob(os.path.join(ROOT, "routes", "*_master_shell.py")):
        with open(path, encoding="utf-8") as fh:
            if "ingest-runs/beat" in fh.read():
                out.add(os.path.basename(path)[:-len("_master_shell.py")])
    return out


def test_the_table_covers_every_beating_shell():
    """Drift guard: a new shell that beats the ledger must be added here, or
    its dashboard could quietly become a keep-alive again."""
    beating = _beating_stems()
    assert beating, "no beating shells found — glob broken?"
    assert set(SHELLS) == beating, (
        f"table drift — missing: {sorted(beating - set(SHELLS))}, "
        f"stale: {sorted(set(SHELLS) - beating)}")


class _Dead(Exception):
    pass


def _dead(*a, **k):
    raise _Dead("network/db stubbed dead in CI")


@pytest.fixture
def quiet(monkeypatch):
    """No network, no DB, an admin key, no kill switches."""
    import requests
    import urllib.request
    import psycopg2
    for name in ("get", "post", "head", "put", "request"):
        monkeypatch.setattr(requests, name, _dead)
    monkeypatch.setattr(urllib.request, "urlopen", _dead)
    monkeypatch.setattr(psycopg2, "connect", _dead)
    for k in ("DATABASE_URL", "NEON_DATABASE_URL", "POSTGRES_URL"):
        monkeypatch.delenv(k, raising=False)
    for k in list(os.environ):
        if k.endswith("_SHELL_DISABLE") or k.endswith("_DISABLE"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", KEY)
    monkeypatch.setenv("AUDIT_CLOSURE_SHELL_PROBE", "0")
    monkeypatch.setenv("AGENT_PAY_SHELL_PROBE", "0")


def _app_for(stem, monkeypatch):
    import importlib
    mod = importlib.import_module(f"routes.{stem}_master_shell")
    beats: list = []
    monkeypatch.setattr(mod, "_beat_ledger", lambda *a, **k: beats.append((a, k)))
    if hasattr(mod, "_cache") and isinstance(mod._cache, dict) and "payload" in mod._cache:
        mod._cache["payload"] = None       # webmcp's 30s tick cache
    bps = [v for v in vars(mod).values() if isinstance(v, flask.Blueprint)]
    assert len(bps) == 1, f"{stem}: expected one Blueprint, found {len(bps)}"
    app = flask.Flask(f"t_{stem}")
    app.register_blueprint(bps[0])
    return app.test_client(), beats


@pytest.mark.parametrize("stem", sorted(SHELLS))
def test_get_dashboard_renders_and_does_not_beat(stem, quiet, monkeypatch):
    """D3: the page renders (200, not a %-format 500).
    D5: the read leaves the ledger untouched."""
    page, _tick, _ = SHELLS[stem]
    client, beats = _app_for(stem, monkeypatch)
    r = client.get(page, headers={"X-Admin-Key": KEY})
    assert r.status_code == 200, (
        f"{stem}: GET {page} -> {r.status_code}: {r.get_data(as_text=True)[:300]}")
    assert beats == [], (
        f"{stem}: a GET of {page} beat the dead-man ledger {len(beats)}x — "
        f"an open browser tab (120s auto-refresh) would keep a dead cron alive")


@pytest.mark.parametrize("stem", sorted(SHELLS))
def test_post_master_tick_still_beats_exactly_once(stem, quiet, monkeypatch):
    """The scheduled path (cron_heartbeat._DISPATCH POSTs) must still beat —
    a fix that silenced every beat would read as 16 dead shells."""
    _page, tick, _ = SHELLS[stem]
    client, beats = _app_for(stem, monkeypatch)
    r = client.post(tick, headers={"X-Admin-Key": KEY})
    assert r.status_code == 200, (
        f"{stem}: POST {tick} -> {r.status_code}: {r.get_data(as_text=True)[:300]}")
    assert len(beats) == 1, f"{stem}: POST master-tick beat {len(beats)}x, expected exactly 1"


@pytest.mark.parametrize("stem", sorted(s for s, v in SHELLS.items() if v[2]))
def test_get_master_tick_is_a_read_and_does_not_beat(stem, quiet, monkeypatch):
    """A GET of master-tick (humans, probes, the signal-watch workflow) is a
    read too. Only POST — the dispatcher's verb — stamps the feed."""
    _page, tick, _ = SHELLS[stem]
    client, beats = _app_for(stem, monkeypatch)
    r = client.get(tick, headers={"X-Admin-Key": KEY})
    assert r.status_code == 200, f"{stem}: GET {tick} -> {r.status_code}"
    assert beats == [], f"{stem}: GET {tick} beat the ledger {len(beats)}x"
