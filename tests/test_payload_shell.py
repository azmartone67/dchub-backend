"""tests/test_payload_shell.py — Payload master shell (#38, 2026-07-27).

Guards routes/payload_master_shell.py. This shell measures what an agent
RECEIVES, and its two dangerous failure modes are both self-inflicted:

  (1) THE INSTRUMENT POLLUTES ITS OWN MEASUREMENT. Lanes 1-3 need a real MCP
      response, so the probe makes real calls — which land in mcp_call_log and
      would inflate the very traffic lanes 4-5 report. That is exactly the
      artifact that made June's "hundreds of calls/wk" an illusion
      (reference_dchub_mcp_traffic_rfo_0701). The probe must be OPT-IN, tagged,
      and excluded by name from every traffic lane.
  (2) A LANE THAT GUESSES WHEN NO PROBE EXISTS. With no stored probe, lanes 1-3
      must render "?" — never green, never a fabricated number.

Plus the house invariants: 404-not-5xx kill switch, snapshot never on the
replica.

Run:  python3 -m pytest tests/test_payload_shell.py -v
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MAIN = _ROOT / "main.py"
_SHELL = _ROOT / "routes" / "payload_master_shell.py"
sys.path.insert(0, str(_ROOT))

from routes.payload_master_shell import (  # noqa: E402
    PROBE_PLATFORM, classify_payload, coherence_issues)


def _src() -> str:
    return _SHELL.read_text(encoding="utf-8")


def _func_src(name: str) -> str:
    tree, lines = ast.parse(_src()), _src().splitlines()
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return "\n".join(lines[n.lineno - 1:n.end_lineno])
    raise AssertionError(f"{name}() not found")


# ── wiring ────────────────────────────────────────────────────────────

def test_blueprint_registered():
    s = _MAIN.read_text(encoding="utf-8")
    assert "from routes.payload_master_shell import payload_master_shell_bp" in s
    assert "app.register_blueprint(payload_master_shell_bp)" in s


def test_kill_switch_404_not_5xx():
    guards = 0
    for node in ast.walk(ast.parse(_src())):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Call)
                and getattr(node.test.func, "id", None) == "_disabled"):
            continue
        guards += 1
        codes = [c.value for c in ast.walk(node) if isinstance(c, ast.Constant)
                 and isinstance(c.value, int) and 100 <= c.value < 600]
        assert codes and all(c == 404 for c in codes), \
            f"_disabled() guard at line {node.lineno} returns {codes}"
    assert guards >= 3, f"expected a guard per endpoint, found {guards}"


def test_write_conn_never_replica():
    assert "NEON_REPLICA_URL" not in _func_src("_write_conn")
    assert "NEON_REPLICA_URL" in _func_src("_conn")


# ── (1) the probe must not pollute what it measures ───────────────────

def test_probe_is_opt_in():
    """A probe makes REAL MCP calls. It must never fire on a plain tick."""
    body = _func_src("payload_probe")
    assert 'request.args.get("probe")' in body and '!= "1"' in body, \
        "probe endpoint no longer requires ?probe=1"
    tick = _func_src("_run_tick")
    assert "run_probe()" not in tick, \
        "the tick calls run_probe() — every refresh would hit the MCP server"


def test_probe_tags_itself():
    body = _func_src("run_probe")
    assert "PROBE_PLATFORM" in body, "probe traffic is untagged and unfilterable"
    assert PROBE_PLATFORM and not PROBE_PLATFORM.startswith("dchub_"), \
        "probe tag must not look like a real platform/key"


@pytest.mark.parametrize("lane", ["_lane_wait", "_lane_navigation"])
def test_traffic_lanes_exclude_probe_traffic(lane):
    """★ The core guard. Without this the shell inflates the call counts,
    the one-tool share and the wait totals it reports — the instrument
    manufacturing its own findings."""
    body = _func_src(lane)
    assert "PROBE_PLATFORM" in body, \
        f"{lane} does not exclude probe traffic — it will measure itself"


# ── (2) no probe ⇒ "?" , never a guess ────────────────────────────────

@pytest.mark.parametrize("lane,check_id", [
    ("_lane_first_response", "fr_data"),
    ("_lane_envelope", "env_ratio"),
    ("_lane_coherence", "coh_contradict"),
])
def test_lane_renders_unknown_without_a_probe(lane, check_id):
    body = _func_src(lane)
    assert "critical=True" in body, f"{lane} load-bearing check is not critical"
    # the no-probe branch must emit pass=None
    m = re.search(rf'_check\(\s*"{check_id}"[^)]*?None', body, re.S)
    assert m, f"{lane} does not emit pass=None when no probe is stored"


# ── the analysis, against the REAL captured payload ───────────────────

def test_classify_counts_selling_as_envelope_not_data():
    """The measured live call: 0 data fields, ~99% envelope."""
    payload = {
        "_entity": "market", "tool": "get_market_intel", "quota": {"tier": "free"},
        "auto_trial_key": "k" * 40, "persist_command": "p" * 140,
        "persist_hint": "h" * 180, "high_intent_instructions": "i" * 600,
        "upgrade_url": "u" * 90, "pricing": {"developer_usd_month": 49},
    }
    c = classify_payload(payload)
    assert c["data_fields"] == 0, "selling keys were counted as data"
    assert c["envelope_keys"] >= 5
    assert c["envelope"] / c["total"] > 0.8


def test_classify_counts_real_data_as_data():
    """A response that actually answers must NOT read as envelope."""
    payload = {"_entity": "market", "tool": "get_market_intel",
               "market": "ashburn", "dcpi_score": 83, "verdict": "BUILD",
               "power_mw_available": 250, "upgrade_url": "u" * 60}
    c = classify_payload(payload)
    assert c["data_fields"] == 4, f"expected 4 data fields, got {c['data_fields']}"
    assert c["data"] > c["envelope"]


def test_metadata_inflates_neither_side():
    c = classify_payload({"_entity": "m", "tool": "t", "quota": {"tier": "free"}})
    assert c["data_fields"] == 0 and c["envelope_keys"] == 0
    assert c["meta"] > 0


@pytest.mark.parametrize("first,second,expect", [
    # the exact contradiction measured live
    ({"retry_instructions": "trial key is ALREADY applied … No header, no reconnect",
      "auto_bound_session": True, "remaining_full_today": 3, "data_fields": 0},
     {"retry_instructions": "Add header X-API-Key … and reconnect",
      "auto_bound_session": False, "remaining_full_today": 3}, 3),
    # a coherent pair must report NOTHING
    ({"retry_instructions": "Add header X-API-Key then retry",
      "auto_bound_session": False, "remaining_full_today": 3, "data_fields": 2},
     {"retry_instructions": "Add header X-API-Key then retry",
      "auto_bound_session": False, "remaining_full_today": 2}, 0),
])
def test_coherence_detects_contradictions_without_crying_wolf(first, second, expect):
    assert len(coherence_issues(first, second)) == expect


def test_coherence_never_raises_on_junk():
    for a, b in ((None, None), ("x", {}), ({}, "y"), ({"a": 1}, {"b": 2})):
        assert isinstance(coherence_issues(a, b), list)


def test_every_lane_names_an_actuator():
    from routes.payload_master_shell import _LANES
    assert len(_LANES) == 5
    for key, _label, fn, act in _LANES:
        assert act and len(act) > 25, f"{key} has no actionable actuator"


# ── live ──────────────────────────────────────────────────────────────

_DB = (os.environ.get("NEON_REPLICA_URL") or os.environ.get("DATABASE_URL")
       or os.environ.get("NEON_DATABASE_URL"))
_live = pytest.mark.skipif(not _DB, reason="no DB URL")


@_live
def test_live_tick_runs_without_probing():
    """A tick must never make an outbound call — proven by it succeeding with
    an unroutable MCP URL configured."""
    os.environ["MCP_INTERNAL_URL"] = "http://127.0.0.1:9/mcp"   # discard port
    from routes.payload_master_shell import _run_tick
    p = _run_tick()
    assert p["ok"] and p["lanes_total"] == 5
