"""
Guards for /go/p/<plan> — the /pricing page's checkout hop (frontend#1534).

WHY IT EXISTS
─────────────
/pricing is a static page, so it cannot mint the HMAC token /go/c verifies. Its
buttons linked straight to buy.stripe.com, and a press was never a server fact.
/go/p/<plan> stamps the plan, then 302s to the same Payment Link /go/c would pick.

WHAT MUST HOLD, weighted like the /go/c guards (a broken redirect costs a sale,
a dropped ref costs the attribution on a sale that still completes):
  * every plan /pricing sells lands on its canonical Payment Link;
  * the page's attribution id reaches Stripe as client_reference_id, intact,
    and junk never reaches the Location header;
  * a plan /pricing does not sell (starter above all: never offered) lands on
    /pricing, stamped as a miss;
  * the stamp goes to pricing_checkout_clicks and NEVER to mcp_checkout_clicks,
    whose every reader counts links an agent relayed.

The route is driven through a real Flask app with the real blueprint; the writer
is the real function against a recording connection. Nothing imports main.py.

A module of its own: tests/test_human_acted_v7_relayed_checkout.py derives the
table the relayed /go/c link writes from checkout_click_tracker's ONE INSERT.
"""
import ast
import os
import re
import types
from contextlib import contextmanager

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "routes", "pricing_click_tracker.py")
PRICING = "https://dchub.cloud/pricing"
# The shape pricing.html's attribution() builds: ref_<ref>__tool_<tool>__ts_<unix>.
PAGE_REF = "ref_pricing-page__tool_none__ts_1789990000"


@pytest.fixture
def client_and_stamps(monkeypatch):
    flask = pytest.importorskip("flask")
    from routes import pricing_click_tracker as tracker
    stamped = []
    monkeypatch.setattr(tracker, "_log_pricing_click", lambda *a: stamped.append(a))
    app = flask.Flask("go-p-test")
    app.register_blueprint(tracker.pricing_click_bp)
    return app.test_client(), stamped


@pytest.mark.parametrize("plan", ["metered", "developer", "pro"])
def test_each_plan_pricing_sells_lands_on_its_payment_link(client_and_stamps, plan):
    from routes._stripe_links import STRIPE_LINKS
    client, stamped = client_and_stamps
    r = client.get(f"/go/p/{plan}")
    assert (r.status_code, r.headers["Location"]) == (302, STRIPE_LINKS[plan])
    assert stamped == [(plan, "", True)]


def test_the_pages_attribution_reaches_stripe_intact(client_and_stamps):
    from routes._stripe_links import STRIPE_LINKS
    client, stamped = client_and_stamps
    r = client.get("/go/p/metered?ref=" + PAGE_REF)
    link = STRIPE_LINKS["metered"]
    want = link + ("&" if "?" in link else "?") + "client_reference_id=" + PAGE_REF
    assert (r.status_code, r.headers["Location"]) == (302, want)
    assert stamped == [("metered", PAGE_REF, True)]


@pytest.mark.parametrize("bad", ["a&b=c", "a b", "x\nLocation: evil", "a/b", "a#f", "x" * 201])
def test_a_ref_outside_the_charset_never_reaches_the_location(client_and_stamps, bad):
    from routes._stripe_links import STRIPE_LINKS
    client, stamped = client_and_stamps
    r = client.get("/go/p/pro", query_string={"ref": bad})
    assert (r.status_code, r.headers["Location"]) == (302, STRIPE_LINKS["pro"])
    assert stamped == [("pro", "", True)]


@pytest.mark.parametrize("plan", ["starter", "team", "enterprise", "pack5", "founding", "nope"])
def test_a_plan_pricing_does_not_sell_lands_on_pricing(client_and_stamps, plan):
    """Starter exists in STRIPE_LINKS and is never offered; the others are real
    links sold elsewhere or not at all. None of them is a /pricing button."""
    client, stamped = client_and_stamps
    r = client.get(f"/go/p/{plan}?ref={PAGE_REF}")
    assert (r.status_code, r.headers["Location"]) == (302, PRICING)
    assert stamped == [(plan, PAGE_REF, False)]


def test_the_plan_is_matched_case_insensitively(client_and_stamps):
    from routes._stripe_links import STRIPE_LINKS
    client, stamped = client_and_stamps
    r = client.get("/go/p/Pro")
    assert (r.status_code, r.headers["Location"]) == (302, STRIPE_LINKS["pro"])
    assert stamped == [("pro", "", True)]


def test_every_cold_plan_resolves_to_a_payment_link_and_starter_is_not_one():
    from routes import pricing_click_tracker as tracker
    from routes._stripe_links import STRIPE_LINKS
    assert set(tracker.COLD_PLANS) == {"metered", "developer", "pro"}
    for p in tracker.COLD_PLANS:
        assert STRIPE_LINKS.get(p, "").startswith("https://buy.stripe.com/"), p


# ── the writer, run for real against a recording connection ──────────────

class _Cursor:
    def __init__(self):
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))


def _load(names):
    with open(SRC) as f:
        tree = ast.parse(f.read())
    found = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names}
    assert set(found) == set(names), f"pricing_click_tracker lost {set(names) - set(found)}"
    ns = {}
    for name in names:
        exec(compile(ast.Module(body=[found[name]], type_ignores=[]), SRC, "exec"), ns)
    return ns


def _recording(ns):
    cur = _Cursor()

    @contextmanager
    def _conn():
        yield types.SimpleNamespace(cursor=lambda: cur)

    swallowed = []
    ns["_conn"] = _conn
    ns["note_swallowed_write"] = lambda *a, **k: swallowed.append(a)
    return cur, swallowed


def test_the_click_is_stamped_in_its_own_table_never_the_mcp_one():
    ns = _load(["_log_pricing_click"])
    cur, swallowed = _recording(ns)
    ns["request"] = types.SimpleNamespace(
        headers={"User-Agent": "Mozilla/5.0", "Referer": PRICING}, remote_addr="203.0.113.9")
    ns["_log_pricing_click"]("developer", PAGE_REF, True)
    assert not swallowed, cur.executed
    assert len(cur.executed) == 1, cur.executed
    sql, params = cur.executed[0]
    assert re.search(r"INSERT INTO\s+pricing_checkout_clicks\b", sql)
    assert "mcp_checkout_clicks" not in sql
    cols = [c.strip() for c in re.search(r"\(([^)]*)\)\s*VALUES", sql).group(1).split(",")]
    assert len(cols) == len(params) == sql.count("%s"), (cols, params)
    row = dict(zip(cols, params))
    assert (row["plan"], row["ref"], row["known_plan"]) == ("developer", PAGE_REF, True)
    assert (row["user_agent"], row["referrer"], row["ip"]) == ("Mozilla/5.0", PRICING, "203.0.113.9")


def test_a_press_without_a_ref_stores_null_not_an_empty_string():
    ns = _load(["_log_pricing_click"])
    cur, _ = _recording(ns)
    ns["request"] = types.SimpleNamespace(headers={}, remote_addr="203.0.113.9")
    ns["_log_pricing_click"]("pro", "", True)
    sql, params = cur.executed[0]
    cols = [c.strip() for c in re.search(r"\(([^)]*)\)\s*VALUES", sql).group(1).split(",")]
    assert dict(zip(cols, params))["ref"] is None


def test_the_table_is_created_on_the_modules_own_connection():
    """The pooled get_db() cursor skips DDL silently, so the CREATE must go
    through this module's _conn — and a database error must not raise at import."""
    ns = _load(["_ensure_pricing_table"])
    cur, _ = _recording(ns)
    ns.update({"_pg": object(), "_dsn": lambda: "postgres://x", "_PRICING_CLICKS_READY": [False]})
    assert ns["_ensure_pricing_table"]() is True
    assert any("CREATE TABLE IF NOT EXISTS pricing_checkout_clicks" in s for s, _ in cur.executed)

    @contextmanager
    def _down():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    ns.update({"_conn": _down, "_PRICING_CLICKS_READY": [False]})
    assert ns["_ensure_pricing_table"]() is False


def test_main_imports_and_registers_the_blueprint():
    """Unregistered means unreachable, and a comment naming the blueprint must not
    pass for a registration: read main.py's AST, not its text."""
    tree = ast.parse(open(os.path.join(REPO_ROOT, "main.py"), encoding="utf-8").read())
    imported = any(isinstance(n, ast.ImportFrom) and n.module == "routes.pricing_click_tracker"
                   and any(a.name == "pricing_click_bp" for a in n.names) for n in ast.walk(tree))
    registered = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "register_blueprint"
                     and any(isinstance(a, ast.Name) and a.id == "pricing_click_bp" for a in n.args)
                     for n in ast.walk(tree))
    assert imported and registered, (imported, registered)


# ── r-pro-trial-web (2026-09-24, owner): /go/p/pro_trial ─────────────────────
# The Pro 7-day trial link, sold on /pricing only. Kept OUT of COLD_PLANS (and so
# out of handoff_definition.CLICK_TO_PAY_PLANS, the lanes the 10-01 readout
# reads) and out of STRIPE_LINKS (checkout-integrity compares a link's charge to
# its label; a trial charges $0).
from routes._stripe_links import PRO_TRIAL_LINK as TRIAL  # noqa: E402  (canon)


def test_the_trial_lands_on_the_trial_link_with_the_pages_ref(client_and_stamps, monkeypatch):
    monkeypatch.delenv("DCHUB_PRO_TRIAL_LINK", raising=False)
    client, stamped = client_and_stamps
    r = client.get("/go/p/pro_trial")
    assert (r.status_code, r.headers["Location"]) == (302, TRIAL)
    r = client.get(f"/go/p/PRO_TRIAL?ref={PAGE_REF}")
    assert r.headers["Location"] == TRIAL + "?client_reference_id=" + PAGE_REF
    assert stamped == [("pro_trial", "", True), ("pro_trial", PAGE_REF, True)]


# Invalid values built from canon links, never a new literal (the repo's
# test_stripe_link_canonical ratchet scans every tracked file, tests included).
@pytest.mark.parametrize("val", ["off", "OFF", "0", "false", "disabled",
                                 "https://evil.example/x",
                                 "http://" + TRIAL.split("://", 1)[1],     # not https
                                 TRIAL.rsplit("/", 1)[0] + "/../x"])
def test_the_trial_can_be_switched_off_and_never_redirects_off_stripe(client_and_stamps, monkeypatch, val):
    monkeypatch.setenv("DCHUB_PRO_TRIAL_LINK", val)
    client, stamped = client_and_stamps
    r = client.get("/go/p/pro_trial")
    assert (r.status_code, r.headers["Location"]) == (302, PRICING)
    assert stamped == [("pro_trial", "", False)]


def test_the_trial_link_is_overridable(client_and_stamps, monkeypatch):
    from routes._stripe_links import STRIPE_LINKS
    other = STRIPE_LINKS["pro"]            # any other canonical Payment Link
    monkeypatch.setenv("DCHUB_PRO_TRIAL_LINK", other)
    client, _ = client_and_stamps
    assert client.get("/go/p/pro_trial").headers["Location"] == other


def test_the_trial_is_not_a_cold_plan_or_a_stripe_link():
    from routes import pricing_click_tracker as tracker
    from routes import handoff_definition as H
    from routes._stripe_links import STRIPE_LINKS
    assert "pro_trial" not in tracker.COLD_PLANS
    assert "pro_trial" not in H.CLICK_TO_PAY_PLANS
    assert "pro_trial" not in STRIPE_LINKS and TRIAL not in STRIPE_LINKS.values()
