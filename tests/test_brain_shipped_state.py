"""
tests/test_brain_shipped_state.py — r-shipstate (2026-07-31).

Pins the shipped-state feeds (routes/brain_shipped_state.py) and their seams
into the investigator, the L23 curator, and the innovation email. Behavior
over comments throughout: feeds are stubbed and OUTCOMES asserted. The one
hard consumer contract pinned here: brain_investigator._evidence_block clips
values at 200 chars, so every investigator-bound value must arrive <=200.
"""
import json
import os
import sys
import time
import types

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import routes.brain_shipped_state as bss  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    bss._CACHE.clear()
    monkeypatch.delenv("BRAIN_SHIPPED_STATE_DISABLE", raising=False)
    yield
    bss._CACHE.clear()


# ── is_live_tool matching ────────────────────────────────────────────

def test_is_live_tool_matches_case_sep_and_get_prefix(monkeypatch):
    monkeypatch.setattr(
        bss, "live_tool_names",
        lambda timeout=6: ["get_power_availability_timeline", "rank_markets"])
    # exact / case / separator insensitive
    assert bss.is_live_tool("Get-Power-Availability-Timeline") == \
        "get_power_availability_timeline"
    # leading get_ leniency — the alias an LLM curator actually produces
    assert bss.is_live_tool("power_availability_timeline") == \
        "get_power_availability_timeline"
    assert bss.is_live_tool("rank markets") == "rank_markets"
    # novel names stay novel
    assert bss.is_live_tool("get_quantum_siting") is None
    assert bss.is_live_tool("") is None


# ── kill switch ──────────────────────────────────────────────────────

def test_kill_switch_darkens_every_feed(monkeypatch):
    monkeypatch.setenv("BRAIN_SHIPPED_STATE_DISABLE", "1")

    class _Boom:
        def __getattr__(self, name):  # any touch = test failure
            raise AssertionError("disabled path touched a dependency")

    assert bss.live_tool_names() == []
    assert bss.recent_merged_prs() == []
    assert bss.gather_shipped_state() == []
    assert bss.shipped_state_block() == ""
    out = bss.reconcile_l23_shipped(conn=_Boom())
    assert out["matched"] == 0


# ── registry probe + pinned fallback ─────────────────────────────────

def _fake_canon(monkeypatch, live=None, live_raises=False,
                pinned=("tool_b", "tool_a")):
    mod = types.ModuleType("ai_surface_canon")

    def _names(timeout=6):
        if live_raises:
            raise RuntimeError("gateway down")
        return live

    mod._mcp_tool_names = _names
    mod.PINNED = {"tool_manifest": list(pinned)}
    monkeypatch.setitem(sys.modules, "ai_surface_canon", mod)


def test_live_tool_names_prefers_live_probe(monkeypatch):
    _fake_canon(monkeypatch, live=["z_tool", "a_tool", "a_tool"])
    names, src = bss.live_tool_names_with_source()
    assert names == ["a_tool", "z_tool"]  # deduped + sorted
    assert src == "tools/list"


def test_live_tool_names_falls_back_to_pinned_when_probe_dies(monkeypatch):
    _fake_canon(monkeypatch, live_raises=True)
    names, src = bss.live_tool_names_with_source()
    assert names == ["tool_a", "tool_b"]
    assert src == "pinned"


# ── investigator Source 9: the 200-char clip contract ────────────────

def test_gather_shipped_state_values_survive_evidence_clip(monkeypatch):
    fake_names = sorted(f"get_tool_{i:02d}_alpha" for i in range(82))
    monkeypatch.setattr(bss, "live_tool_names_with_source",
                        lambda timeout=6: (fake_names, "tools/list"))
    fake_prs = ([{"repo": "dchub-backend", "number": 2000 + i,
                  "title": f"fix(x): change number {i}",
                  "merged_at": "2026-07-30T00:00:00Z"} for i in range(12)]
                + [{"repo": "dchub-mcp-server", "number": 113,
                    "title": "feat(catalog): get_power_availability_timeline",
                    "merged_at": "2026-07-31T02:38:52Z"}])
    monkeypatch.setattr(bss, "recent_merged_prs",
                        lambda days=14, per_repo=30: fake_prs)

    items = bss.gather_shipped_state()
    assert len(items) >= 4
    for it in items:
        assert set(it) == {"claim", "source", "value"}
        # brain_investigator._evidence_block clips at 200 — nothing may rely
        # on content past that boundary.
        assert len(str(it["value"])) <= 200, it
    joined = " ".join(str(it["value"]) for it in items)
    # the WHOLE registry must survive chunking — a silently-truncated registry
    # is exactly the staleness this module exists to kill
    assert fake_names[0] in joined and fake_names[-1] in joined
    assert "#113" in joined
    claims = " ".join(it["claim"] for it in items)
    assert "NEVER propose" in claims
    assert "ALREADY LANDED" in claims


def test_shipped_state_block_contains_registry_and_prs(monkeypatch):
    monkeypatch.setattr(bss, "live_tool_names_with_source",
                        lambda timeout=6: (["get_a", "get_b"], "tools/list"))
    monkeypatch.setattr(
        bss, "recent_merged_prs",
        lambda days=14, per_repo=30: [{
            "repo": "dchub-backend", "number": 2042,
            "title": "fix(brain-l5): carry the source finding key",
            "merged_at": "2026-07-31T22:58:34Z"}])
    blk = bss.shipped_state_block()
    assert "get_a, get_b" in blk
    assert "#2042" in blk and "ALREADY EXIST" in blk
    assert len(bss.shipped_state_block(max_chars=120)) <= 120


# ── merged-PR fetch: filtering, partial failure, caching ─────────────

def test_recent_merged_prs_filters_and_survives_one_repo_failure(monkeypatch):
    calls = {"n": 0}
    recent = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(time.time() - 2 * 86400))
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(time.time() - 40 * 86400))

    def fake_github(url, timeout=6):
        calls["n"] += 1
        if "dchub-backend" in url:
            return [
                {"number": 1, "title": "merged  recent\nline", "merged_at": recent},
                {"number": 2, "title": "closed unmerged", "merged_at": None},
                {"number": 3, "title": "merged old", "merged_at": old},
            ]
        raise RuntimeError("second repo down")

    monkeypatch.setattr(bss, "_github_json", fake_github)
    out = bss.recent_merged_prs(days=14)
    assert [p["number"] for p in out] == [1]
    assert out[0]["repo"] == "dchub-backend"
    assert out[0]["title"] == "merged recent line"  # whitespace normalized
    # cached: a second call must not re-fetch
    n_before = calls["n"]
    assert bss.recent_merged_prs(days=14) == out
    assert calls["n"] == n_before


# ── the reconciler ───────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows):
        self.cur = _FakeCursor(rows)
        self.committed = False
        self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def test_reconcile_marks_only_live_matches(monkeypatch):
    monkeypatch.setattr(bss, "live_tool_names",
                        lambda timeout=6: ["get_power_availability_timeline"])
    rows = [
        (29, json.dumps({"name": "get_power_availability_timeline"})),
        (30, json.dumps({"name": "quantum_scoring"})),
        (31, "not json at all"),
    ]
    conn = _FakeConn(rows)
    out = bss.reconcile_l23_shipped(conn=conn)
    assert out["matched"] == 1 and out["ids"] == [29] and out["checked"] == 3
    update = [e for e in conn.cur.executed if e[0].startswith("UPDATE")]
    assert len(update) == 1
    sql, params = update[0]
    assert "shipped_at = NOW()" in sql and "id = ANY(%s)" in sql
    assert params[1] == [29]
    assert conn.committed is True
    assert conn.closed is False  # injected conns are the caller's to close


def test_reconcile_never_marks_on_empty_registry(monkeypatch):
    monkeypatch.setattr(bss, "live_tool_names", lambda timeout=6: [])

    class _Boom:
        def cursor(self):
            raise AssertionError("no registry -> DB must not be touched")

    out = bss.reconcile_l23_shipped(conn=_Boom())
    assert out == {"matched": 0, "ids": [], "names": [], "checked": 0}


# ── L23 seams ────────────────────────────────────────────────────────

def _l23_src():
    with open(os.path.join(_ROOT, "routes", "brain_layer23_lifecycle.py")) as f:
        return f.read()


def test_l23_stale_hardcoded_tool_list_is_gone():
    src = _l23_src()
    assert "Existing 23 MCP tools" not in src
    assert "{live_tools_context}" in src
    # a placeholder without its format kwarg is a runtime KeyError — pin both
    assert "live_tools_context=live_tools_ctx" in src
    assert "reconcile-shipped" in src
    assert "from routes.brain_shipped_state import is_live_tool" in src


def _import_l23():
    pytest.importorskip("flask")
    try:
        import routes.brain_layer23_lifecycle as l23
        return l23
    except Exception as e:  # pragma: no cover - env-dependent import chain
        pytest.skip(f"brain_layer23_lifecycle unimportable here: {e}")


def test_l23_prompt_formats_cleanly_with_live_tools_context():
    l23 = _import_l23()
    rendered = l23._LIFECYCLE_PROMPT.format(
        audit_summary="A", trend_context="T", dismissed_context="D",
        pending_context="P", shipped_context="S",
        live_tools_context="LIVE-TOOLS-SENTINEL")
    assert "LIVE-TOOLS-SENTINEL" in rendered
    assert '"kind": "mcp_tool|endpoint' in rendered  # escaped JSON braces intact


def test_l23_dup_guard_pending_includes_approved_false(monkeypatch):
    l23 = _import_l23()
    monkeypatch.setattr(bss, "is_live_tool", lambda n: None)
    cur = _FakeCursor([(5, None, False, None, json.dumps({"name": "foo_bar"}))])
    # approved=False + not dismissed was the fall-through: it must read PENDING
    assert l23._proposal_already_exists(cur, "foo-bar") == \
        "already pending (proposal #5)"


def test_l23_dup_guard_live_registry_short_circuits(monkeypatch):
    l23 = _import_l23()
    monkeypatch.setattr(bss, "is_live_tool", lambda n: "get_foo_bar")

    class _NoTouch:
        def execute(self, *a, **k):
            raise AssertionError("live hit must short-circuit before any SQL")

    out = l23._proposal_already_exists(_NoTouch(), "foo_bar")
    assert out == "already LIVE on the MCP gateway as 'get_foo_bar' (tools/list)"


# ── consumer-wiring seams (investigator + email) ─────────────────────

def test_investigator_and_email_are_wired():
    with open(os.path.join(_ROOT, "routes", "brain_investigator.py")) as f:
        inv = f.read()
    assert "from routes.brain_shipped_state import gather_shipped_state" in inv
    with open(os.path.join(_ROOT, "routes", "brain_innovation_email.py")) as f:
        mail = f.read()
    assert "from routes.brain_shipped_state import is_live_tool" in mail

