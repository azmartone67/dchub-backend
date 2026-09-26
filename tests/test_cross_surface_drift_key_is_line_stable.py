"""check_cross_surface_value_drift's finding key must not move when a line
is added above the literal.

The finding's `url` is its identity downstream (squasher row 487's key was
"routes/mcp_presence_crawler.py:2441"). While it was f"{rel}:{lineno}", any
edit above the literal changed the key: the old row self-cleared as if fixed
and a fresh row filed for the same unfixed literal.

Runs the REAL detector over the REAL allow-listed files; the shift is made by
prefixing blank lines to what the detector reads.
"""
from __future__ import annotations

import builtins
import io
import re
import sys
import types

import pytest


def _run(monkeypatch, markets=5000, countries=5000, prefix="", replace=None):
    cs = types.ModuleType("canonical_stats")
    cs._FALLBACK = {"facilities": 1}
    cs.get_canonical_stats = lambda force=False: {
        "facilities": 24600, "markets": markets, "countries": countries}
    monkeypatch.setitem(sys.modules, "canonical_stats", cs)
    from routes import brain_consistency_radar as r
    real_open = builtins.open

    def fake_open(path, *a, **kw):
        fh = real_open(path, *a, **kw)
        if not str(path).endswith(".py") or "b" in (a[0] if a else kw.get("mode", "r")):
            return fh
        with fh:
            txt = fh.read()
        if replace:
            txt = replace(str(path), txt)
        return io.StringIO(prefix + txt)

    monkeypatch.setattr(r, "open", fake_open, raising=False)
    return [f for f in r.check_cross_surface_value_drift()
            if f["issue"] == "cross_surface_metric_divergence"]


def test_the_key_survives_lines_added_above(monkeypatch):
    before = _run(monkeypatch)
    assert before, "control: the detector flagged nothing, so this test proves nothing"
    after = _run(monkeypatch, prefix="\n" * 7)
    assert [f["url"] for f in after] == [f["url"] for f in before]
    assert [f["line"] for f in after] == [f["line"] + 7 for f in before], \
        "CONTROL: the shift really reached the detector"


def test_the_key_carries_no_line_number(monkeypatch):
    for f in _run(monkeypatch):
        assert not re.search(r":\d+$", f["url"]), f["url"]
        assert re.fullmatch(r"[\w/.]+\.py#(markets|countries)=\d+", f["url"]), f["url"]


def test_the_line_is_still_cited_in_detail(monkeypatch):
    """brain_source_map pins file:line from the finding's free text; detail
    must keep carrying it now that url does not."""
    for f in _run(monkeypatch):
        rel = f["url"].partition("#")[0]
        assert f"{rel}:{f['line']}" in f["detail"], f


def test_a_repeated_literal_is_one_finding_citing_every_line(monkeypatch):
    target = "routes/state_of_power.py"
    extra = "\nX_DUP_A = {'markets': 777}\nX_DUP_B = {'markets': 777}\n"
    found = _run(monkeypatch, replace=lambda p, t: t + extra if p.endswith(target) else t)
    dup = [f for f in found if f["url"] == f"{target}#markets=777"]
    assert len(dup) == 1, [f["url"] for f in found]
    assert dup[0]["detail"].count(f"{target}:") == 2, dup[0]["detail"]
