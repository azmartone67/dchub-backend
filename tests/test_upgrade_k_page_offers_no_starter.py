"""
The key-bound /upgrade/k/<token> page never offers Starter (frontend#1534).

Owner rule, 2026-09-21: prices come only from /pricing and tier_registry, and
Starter $9 is in the registry but NOT on /pricing, so it is never offered. The
page rendered a "Starter · $9/mo" button beside Developer. Rendered here through
the real _render_page, so a button reintroduced under any wording or link shape
fails. The /go/<tier> route still resolves `starter`: the page says it is
permanent, and a copy rendered before this change must not start 404ing.
"""
import importlib.util
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _render():
    spec = importlib.util.spec_from_file_location(
        "_uh_no_starter", os.path.join(ROOT, "routes", "upgrade_handoff.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._render_page("dch_live_" + "a" * 24, {"calls_30d": 12, "days_30d": 3,
                                                     "tools_30d": 2, "gated_30d": 1}, "tok123")


def test_the_page_offers_the_pack_and_developer_and_never_starter():
    html = _render()
    assert "/go/pack5" in html and "/go/developer" in html      # the positive control
    assert not re.search(r"starter", html, re.I), "Starter is offered on /upgrade/k"
    assert "$9" not in html
    assert "__GO_" not in html, "a placeholder was left unfilled"
