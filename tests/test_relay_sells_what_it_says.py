"""The agent→human relay page sells the plan its copy names (2026-09-02).

MEASURED, not remembered. /upgrade/h/<token> is the ONE human hop the MCP
funnel produces — the revenue shell counts 102 real human opens in 30 days.
Its copy says "Unlock full data — $10 one-time"; its button went to
api.dchub.cloud/pricing/upgrade?tier=free|identified&direct=1, and
routes/_stripe_links.resolve_tier fell through to 'developer' for any tier
word it did not know. Live 302 to the $49/mo Developer link, verified
2026-09-02T00:29Z at api.dchub.cloud and at the Railway origin. 0 paid.

Three things are pinned, each against the code that would break it:
  1. resolve_tier never falls through to a monthly plan (routes/_stripe_links.py);
  2. the relay button carries PACK_TIER — the token's `tier` is the CALLER's
     tier, not a plan (routes/human_relay.py);
  3. checkout-integrity lane 3 reads the relay page and fails on exactly the
     live shape (routes/checkout_integrity_master_shell.py), so the class is
     caught by the shell next time, not by a QA sweep.

House rule: tests never import main. Hermetic — every fetch and DB write is
stubbed; the relay page is rendered in-process through its blueprint.
"""
import os
import re
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

flask = pytest.importorskip("flask")

AUDIT_UA = "dchub-checkout-integrity/1.0 (+https://dchub.cloud; internal-audit)"
HUMAN_UA = ("Mozilla/5.0 (Macintosh) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36")


def _src(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ── 1 · the resolver ─────────────────────────────────────────────────

@pytest.mark.parametrize("word", ["free", "identified", "anon", "anonymous",
                                  "", "not-a-plan", "FREE"])
def test_a_tier_canon_cannot_name_resolves_to_the_pack_never_a_plan(word):
    """★ The live bug. `free`/`identified` describe who is asking, not what
    they are buying; the fall-through must be the one-time pack."""
    import tier_registry
    from routes._stripe_links import (resolve_tier, PACK_TIER, ONE_TIME_TIERS,
                                      STRIPE_LINKS, get_stripe_url)
    got = resolve_tier("", word)
    assert got == PACK_TIER, f"{word!r} resolved to {got!r}"
    assert got in ONE_TIME_TIERS
    # a one-time pack has NO monthly price — that is what "never a plan" means
    assert tier_registry.price(got) == 0
    assert get_stripe_url(word) == STRIPE_LINKS[PACK_TIER]


def test_explicit_budget_and_tool_resolution_still_win():
    """The fix changes the DEFAULT only. A named plan, a budget hint and a
    tool-gated tier all still resolve as before."""
    from routes._stripe_links import resolve_tier
    assert resolve_tier("", "founding") == "founding"
    assert resolve_tier("", "PRO") == "pro"
    assert resolve_tier("analyze_site", "free") == "pro"          # tool wins over caller tier
    assert resolve_tier("search_facilities", "") == "developer"   # developer-gated tool
    assert resolve_tier("", "", "tight") == "starter"


def test_the_pack_is_one_link_one_price_cross_pinned_to_the_credit_pack():
    """metered and pack5 are the same Stripe link; the USD the lane quotes is
    the price the credit-pack webhook branch verifies (PACK10_PRICE_CENTS)."""
    from routes._stripe_links import (STRIPE_LINKS, TIER_ONE_TIME_USD, PACK_TIER,
                                      ONE_TIME_TIERS)
    assert STRIPE_LINKS["metered"] == STRIPE_LINKS["pack5"]
    assert set(TIER_ONE_TIME_USD) == set(ONE_TIME_TIERS)
    src = _src("routes", "mcp_conversion_plays.py")
    m = re.search(r"PACK10_PRICE_CENTS\s*=\s*int\(os\.environ\.get\('[^']+',\s*'(\d+)'\)\)", src)
    assert m, "PACK10_PRICE_CENTS default not found"
    assert int(m.group(1)) == TIER_ONE_TIME_USD[PACK_TIER] * 100


def test_the_email_capture_page_uses_the_same_resolver():
    """Two resolvers with two defaults is how this drifted; keep one."""
    src = _src("routes", "checkout_email_capture.py")
    i = src.index("def _resolve_tier")
    body = src[i:i + 700]
    assert "from routes._stripe_links import resolve_tier" in body
    assert 'return "developer"' not in body


# ── 2 · the relay page ───────────────────────────────────────────────

@pytest.fixture()
def relay_app(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "test-secret")
    monkeypatch.delenv("DCHUB_HUMAN_RELAY_DISABLE", raising=False)
    from routes import human_relay
    logged = []
    monkeypatch.setattr(human_relay, "_log_open",
                        lambda info, token, valid: logged.append((info, valid)))
    app = flask.Flask("relay-test")
    app.register_blueprint(human_relay.human_relay_bp)
    return app, human_relay, logged


def _button(html):
    from routes.checkout_integrity_master_shell import _UPGRADE_ANCHOR_RE
    from html import unescape
    from urllib.parse import urlsplit, parse_qs
    found = _UPGRADE_ANCHOR_RE.findall(html)
    assert len(found) == 1, "the relay page has exactly one upgrade button"
    href, label = found[0]
    href = unescape(href)
    q = parse_qs(urlsplit(href).query)
    return q, re.sub(r"<[^>]+>", " ", label).strip()


def test_relay_button_carries_the_pack_tier_for_a_free_caller(relay_app):
    app, relay, logged = relay_app
    from routes._stripe_links import PACK_TIER, resolve_tier, ONE_TIME_TIERS
    tok = relay.make_relay_token("sess-1", "get_dchub_recommendation", "free")
    r = app.test_client().get(f"/upgrade/h/{tok}", headers={"User-Agent": HUMAN_UA})
    assert r.status_code == 200
    q, label = _button(r.get_data(as_text=True))
    assert q["tier"] == [PACK_TIER], q
    assert q["direct"] == ["1"] and q["from"] == ["mcp_relay"]
    assert q["tool"] == ["get_dchub_recommendation"] and q["sid"] == ["sess-1"]
    assert "$10 one-time" in label
    assert resolve_tier(q["tool"][0], q["tier"][0]) in ONE_TIME_TIERS
    # the CALLER's tier still reaches the funnel log — it is data, not the plan
    assert logged and logged[0][0]["tier"] == "free" and logged[0][1] is True


def test_relay_button_carries_the_pack_tier_for_an_identified_caller(relay_app):
    """The live paywall mints tier=IDENTIFIED (get_dchub_recommendation,
    2026-09-02T00:42Z) — the other half of the live shape."""
    app, relay, _ = relay_app
    from routes._stripe_links import PACK_TIER
    tok = relay.make_relay_token("", "", "identified")
    q, _label = _button(app.test_client().get(f"/upgrade/h/{tok}").get_data(as_text=True))
    assert q["tier"] == [PACK_TIER]


def test_relay_button_carries_the_pack_tier_for_a_bad_token(relay_app):
    """A garbage token still renders (never a dead end) and still sells the pack."""
    app, _, logged = relay_app
    from routes._stripe_links import PACK_TIER
    r = app.test_client().get("/upgrade/h/not.a.token")
    assert r.status_code == 200
    q, label = _button(r.get_data(as_text=True))
    assert q["tier"] == [PACK_TIER] and "$10 one-time" in label
    assert logged and logged[0][1] is False


def test_the_shells_audit_costume_is_not_logged_as_a_human_open(monkeypatch):
    """★ Lane 3 now opens the relay page every 120 s. The revenue shell scores
    relay_opens as 'real human opens' — the audit must not write a row."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/relay")
    monkeypatch.delenv("DCHUB_HUMAN_RELAY_DISABLE", raising=False)
    connects = []
    fake_pg = types.SimpleNamespace(
        connect=lambda *a, **k: connects.append(a) or (_ for _ in ()).throw(RuntimeError("stub")))
    monkeypatch.setitem(sys.modules, "psycopg2", fake_pg)
    from routes import human_relay
    app = flask.Flask("relay-ua-test")
    app.register_blueprint(human_relay.human_relay_bp)
    c = app.test_client()
    assert c.get("/upgrade/h/x.y", headers={"User-Agent": AUDIT_UA}).status_code == 200
    assert connects == [], "the audit costume must not reach the DB"
    assert c.get("/upgrade/h/x.y", headers={"User-Agent": HUMAN_UA}).status_code == 200
    assert len(connects) == 1, "a real browser open must still be logged"


def test_the_shell_wears_the_costume_the_relay_recognises():
    from routes import human_relay, checkout_integrity_master_shell as ci
    assert ci._UA.startswith(human_relay._AUDIT_UA_PREFIXES)


# ── 3 · lane 3 reads the relay page ──────────────────────────────────

def _relay_html(tier, label="Unlock full data — $10 one-time", tool="analyze_site"):
    return ("<html><body><a class='btn' href='https://api.dchub.cloud/pricing/upgrade"
            "?from=mcp_relay&amp;tier=%s&amp;direct=1&amp;tool=%s'>%s</a></body></html>"
            % (tier, tool, label))


@pytest.fixture()
def shell():
    from routes import checkout_integrity_master_shell as ci
    return ci


@pytest.fixture()
def canon(shell):
    c = shell._canon()
    assert c
    return c


def _lane(shell, monkeypatch, canon, relay_body, relay_err=None):
    def fake(path):
        if path == shell._RELAY_SURFACE:
            return (relay_body, relay_err) if relay_err is None else (None, relay_err)
        return ("", None)
    monkeypatch.setattr(shell, "_fetch", fake)
    return shell._lane_label_vs_plan(canon)


def test_lane3_fails_the_live_shape_a_tier_canon_cannot_name(shell, monkeypatch, canon):
    """tier=free under a $10 label — what production served for three months
    while this lane read PASS over 14 anchored CTAs."""
    checks = _lane(shell, monkeypatch, canon, _relay_html("free"))
    bad = [k for k in checks if k["id"] == "relay_tier_named"]
    assert bad and bad[0]["pass"] is False and bad[0]["critical"]
    assert "tier='free'" in bad[0]["detail"]
    assert shell._lane_verdict(checks) == "FAIL"


def test_lane3_fails_a_one_time_label_over_a_monthly_plan(shell, monkeypatch, canon):
    checks = _lane(shell, monkeypatch, canon, _relay_html("developer"))
    bad = [k for k in checks if k["id"] == "relay_label_plan" and k["pass"] is False]
    assert bad and "one-time" in bad[0]["detail"] and "'developer'" in bad[0]["detail"]


def test_lane3_fails_a_wrong_pack_price(shell, monkeypatch, canon):
    checks = _lane(shell, monkeypatch, canon,
                   _relay_html("metered", label="Unlock — $5 one-time"))
    bad = [k for k in checks if k["id"] == "relay_label_price"]
    assert bad and bad[0]["pass"] is False


def test_lane3_passes_the_honest_relay_button(shell, monkeypatch, canon):
    from routes._stripe_links import PACK_TIER
    checks = _lane(shell, monkeypatch, canon, _relay_html(PACK_TIER))
    ok = [k for k in checks if k["id"] == "relay_label_plan"]
    assert ok and ok[0]["pass"] is True
    assert not any(k["pass"] is False for k in checks)
    # the relay counts toward "CTAs examined" so the lane can claim a sweep
    assert any(k["id"] == "labels_agree" for k in checks)


def test_lane3_is_indeterminate_when_the_relay_is_unreadable(shell, monkeypatch, canon):
    """An unreachable relay is '?', never PASS — the green-by-silence fence."""
    checks = _lane(shell, monkeypatch, canon, None, relay_err="HTTP 503")
    k = [c for c in checks if c["id"] == "relay_readable"]
    assert k and k[0]["pass"] is None and k[0]["critical"]
    assert shell._lane_verdict(checks) != "PASS"


def test_lane3_passes_the_page_the_relay_actually_renders(shell, monkeypatch, canon, relay_app):
    """End to end in-process: the REAL page from routes/human_relay.py through
    the REAL lane. Mutating either side (relay tier, resolver default, lane
    block) turns this red."""
    app, relay, _ = relay_app
    tok = relay.make_relay_token("s", "get_dchub_recommendation", "identified")
    body = app.test_client().get(f"/upgrade/h/{tok}").get_data(as_text=True)
    checks = _lane(shell, monkeypatch, canon, body)
    relay_checks = [k for k in checks if k["id"].startswith("relay_")]
    assert relay_checks, "the lane did not examine the relay page"
    assert all(k["pass"] is True for k in relay_checks), relay_checks


def test_lane3_reads_the_relay_surface_live():
    """The lane must FETCH the relay (out-in), not import a constant."""
    src = _src("routes", "checkout_integrity_master_shell.py")
    i = src.index("def _relay_label_vs_plan")
    body = src[i:src.index("def _lane_founding_capacity")]
    assert "_fetch(_RELAY_SURFACE)" in body
    assert "resolve_tier(tool_param, tier_param)" in body, \
        "resolve the href the way the backend does, not by hand"
    assert "_relay_label_vs_plan(canon)" in src[src.index("def _lane_label_vs_plan"):i]
