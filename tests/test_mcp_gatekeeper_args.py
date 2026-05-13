"""mcp_gatekeeper._safe_echo_args sanity tests.

Phase JJ (PR #44) added echo_args to the upgrade-required JSON
response so the CTA can say "you asked for state=GA". The sanitizer
must:
  - Keep small scalar args (str/int/float/bool, len <= 64)
  - Drop nested / oversized values (security: no PII spill)
  - Tolerate None / empty / wrong-type input
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GK = os.path.join(ROOT, "mcp_gatekeeper.py")


def _safe_echo_args():
    src = open(GK, encoding="utf-8").read()
    m = re.search(
        r"^def _safe_echo_args\(.*?(?=^def |^class |\Z)",
        src, re.DOTALL | re.MULTILINE,
    )
    assert m, "_safe_echo_args not found in mcp_gatekeeper.py"
    # The function uses `Optional[Dict]` from typing — preload typing
    # symbols into the exec namespace so the def line parses cleanly.
    ns = {}
    from typing import Optional, Dict
    ns["Optional"], ns["Dict"] = Optional, Dict
    exec(m.group(0), ns)
    return ns["_safe_echo_args"]


def test_keeps_state_and_iso_scalars():
    fn = _safe_echo_args()
    out = fn({"state": "GA", "iso": "PJM", "market": "atlanta"})
    assert out == {"state": "GA", "iso": "PJM", "market": "atlanta"}


def test_drops_unknown_keys():
    """Only the allowlisted keys make it through — guards against
    accidentally echoing arbitrary kwargs."""
    fn = _safe_echo_args()
    out = fn({"state": "GA", "secret_token": "tok_xxx", "password": "p"})
    assert out == {"state": "GA"}
    assert "secret_token" not in out
    assert "password" not in out


def test_drops_none_and_empty():
    fn = _safe_echo_args()
    out = fn({"state": None, "iso": "", "market": "atlanta"})
    # `None` is filtered out by the `if v is None: continue` guard.
    # Empty string IS a valid str of length 0 ≤ 64, so it's kept.
    assert "state" not in out
    assert out.get("market") == "atlanta"


def test_drops_oversized_values():
    """Values over 64 chars are dropped to prevent payload bloat."""
    fn = _safe_echo_args()
    big = "x" * 200
    out = fn({"state": "GA", "city": big})
    assert out.get("state") == "GA"
    assert "city" not in out


def test_drops_non_scalar_types():
    """Lists, dicts, sets — none should leak."""
    fn = _safe_echo_args()
    out = fn({
        "state": "GA",
        "extra_list": ["a", "b"],
        "extra_dict": {"k": "v"},
    })
    assert out == {"state": "GA"}


def test_handles_none_input():
    fn = _safe_echo_args()
    assert fn(None) == {}


def test_handles_non_dict_input():
    fn = _safe_echo_args()
    assert fn("not a dict") == {}
    assert fn([1, 2, 3]) == {}


def test_keeps_numeric_scalars():
    fn = _safe_echo_args()
    out = fn({"lat": 33.7, "lon": -84.4, "radius_km": 50, "limit": 25})
    assert out == {"lat": 33.7, "lon": -84.4, "radius_km": 50, "limit": 25}


def test_keeps_bool_scalars():
    fn = _safe_echo_args()
    out = fn({"limit": True})  # weird but technically scalar
    assert out == {"limit": True}
