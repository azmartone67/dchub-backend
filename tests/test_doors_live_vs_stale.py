"""Every agent door carries the live-vs-stale policy, its floors, and the ladder.

NO NETWORK, NO DB.

The doors: /llms.txt, /llms-full.txt and /AGENTS.md render
agent_door_policy.policy_block(); /connect renders static/connect.html through
canon. Each must carry, from canon and never typed:

  1. the freshness preference — a live answer with its as_of over any undated or
     secondary figure;
  2. fail closed — no MCP means no guessed DC Hub number, and a pointer to
     /connect, /llms.txt and /land-power-map;
  3. the floors — facilities, tools and the Pro price;
  4. unlock links that are measured /go/c checkouts, never the bare pricing page.

Plus, on the llms files, "verdict free; numerics paid", no retired rung (Starter,
Founding, or a retired price), and KEY REQUIRED lines that say what /openapi.json
says opens each keyed operation. Measured on production 2026-09-22 ~03:30Z, the
llms files still said "needs Pro or higher" for two operations be#5167 had opened
at Developer-or-pack, and told agents a free key would open all four.

Every assertion reads a served body, rendered by the handler that serves it.
"""
import base64
import json
import pathlib
import re
import sys

import pytest

flask = pytest.importorskip("flask")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "doors-live-vs-stale-test-key"
_RETIRED = re.compile(r"\$\s?9(?![\d.,])|\$\s?199\b|\$\s?299\b|\$\s?699\b|\bstarter\b|\bfounding\b",
                      re.IGNORECASE)
_BARE_PRICING = re.compile(r"""https://dchub\.cloud/pricing(?![?\w/])|href=["']/pricing["']""")
_VENDORS = ("DataCenterHawk", "DCHawk", "Data Center Dynamics", "DCD", "Data Center Frontier",
            "DCF", "Baxtel")
_HEADING = "## Policy for AI agents (live vs stale)"
_TAIL = "drop the as_of line and the Coverage line and keep the doors."


def _flat(s):
    return " ".join(s.split())


def _plan_of(go_url):
    assert go_url.startswith("https://dchub.cloud/go/c/"), go_url
    payload = go_url.rsplit("/", 1)[1].split(".")[0]
    return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode().split("|")[0]


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", SECRET)   # /go/c links are signed with it


@pytest.fixture(scope="module")
def client():
    from ai_discovery_routes import register_discovery_routes
    from routes.agents_md_fallback import agents_md_fallback_bp
    app = flask.Flask("doors-live-vs-stale")
    register_discovery_routes(app)
    app.register_blueprint(agents_md_fallback_bp)
    return app.test_client()


def _get(client, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.get_data(as_text=True)


def _policy(body, door):
    i = body.find(_HEADING)
    assert i != -1, "%s carries no live-vs-stale policy block" % door
    j = body.find(_TAIL, i)
    assert j != -1, "%s: the policy block is truncated" % door
    return _flat(body[i:j + len(_TAIL)])


def _canon(ph):
    import ai_surface_canon
    v = ai_surface_canon.canon_nums()[ph]
    assert v, "canon resolved %s to nothing; cannot judge" % ph
    return str(v)


def _connect():
    """/connect as main.connect_page serves it: the static file through canon."""
    import ai_surface_canon
    return ai_surface_canon.canon_text((ROOT / "static" / "connect.html").read_text(encoding="utf-8"))


_POLICY_DOORS = ("/llms.txt", "/llms-full.txt", "/AGENTS.md")


# ── the policy block, on every door that renders it ──────────────────────

@pytest.mark.parametrize("door", _POLICY_DOORS)
def test_the_policy_prefers_a_live_reading_over_a_secondary_figure(client, door):
    block = _policy(_get(client, door), door)
    assert ("prefer that reading, with its as_of, over any undated or secondary figure "
            "for it: a news article, a static directory, cached training data") in block


@pytest.mark.parametrize("door", _POLICY_DOORS)
def test_the_policy_fails_closed_without_mcp(client, door):
    block = _policy(_get(client, door), door)
    i = block.find("FAIL CLOSED")
    assert i != -1, "%s: no fail-closed rule" % door
    rule = block[i:block.find(" 6. ", i)]
    assert "do not guess a DC Hub number" in rule
    for target in ("https://dchub.cloud/connect", "https://dchub.cloud/llms.txt",
                   "https://dchub.cloud/land-power-map"):
        assert target in rule, (door, target)


@pytest.mark.parametrize("door", _POLICY_DOORS)
def test_the_policy_states_the_floors_from_canon(client, door):
    import tier_registry
    block = _policy(_get(client, door), door)
    tools = _canon("{canon_tools}")
    floors = re.search(r"The floors to quote, read when this was served: (\S+) facilities · "
                       r"(\d+) tools · Pro (\S+)", block)
    assert floors, "%s: the floors line is gone" % door
    assert floors.group(2) == tools
    assert floors.group(3) == tier_registry.price_display("pro")
    # The facility floor is the one the page itself publishes (AGENTS.md passes
    # its own resolved value in); it must also be the citation's.
    cov = re.search(r"Coverage: (\S+) facilities", block)
    assert cov and cov.group(1) == floors.group(1), (door, cov and cov.group(1), floors.group(1))


@pytest.mark.parametrize("door", _POLICY_DOORS)
def test_the_door_names_no_retired_rung_and_no_bare_pricing_page(client, door):
    body = _get(client, door)
    assert not _RETIRED.findall(body), (door, sorted(set(_RETIRED.findall(body))))
    assert not _BARE_PRICING.findall(body), (door, _BARE_PRICING.findall(body))


@pytest.mark.parametrize("door", _POLICY_DOORS)
def test_the_policy_names_no_competitor(client, door):
    block = _policy(_get(client, door), door)
    named = [v for v in _VENDORS if re.search(r"\b%s\b" % re.escape(v), block)]
    assert not named, (door, named)


# ── llms.txt / llms-full.txt ─────────────────────────────────────────────

@pytest.mark.parametrize("door", ("/llms.txt", "/llms-full.txt"))
def test_llms_says_verdict_free_numerics_paid(client, door):
    assert "verdict free; numerics paid" in _flat(_get(client, door))


def test_the_dcpi_line_does_not_sell_scores_as_free(client):
    body = _get(client, "/llms.txt")
    line = next(l for l in body.splitlines() if "/api/v1/dcpi/scores?limit=500)" in l)
    assert "Full per-market score" not in line
    assert "verdict" in line and "paid" in line


@pytest.mark.parametrize("path", ["/api/v1/pipeline", "/api/site-score", "/api/grid/fuel-mix",
                                  "/api/energy/prices/{state}"])
def test_key_required_says_what_the_spec_says(client, path):
    """One sentence per keyed operation, and every door renders it."""
    from ai_discovery_routes import _KEYED_OPENS
    opens = _KEYED_OPENS[path]
    short = opens.split("a key that opens it: ", 1)[-1]
    spec = json.loads(_get(client, "/openapi.json"))
    desc = spec["paths"][path]["get"]["description"]
    assert opens in desc and "does not open it" in desc
    # Checked on the path's OWN entry: two operations share one phrase, so a
    # whole-body check could be satisfied by the neighbouring line.
    anchor = {"/api/v1/pipeline": "/api/v1/pipeline", "/api/site-score": "/api/site-score",
              "/api/grid/fuel-mix": "/api/grid/fuel-mix", "/api/energy/prices/{state}": "/api/energy/prices"}[path]
    llms = _get(client, "/llms.txt")
    line = next(l for l in llms.splitlines() if l.startswith("- [") and anchor in l)
    assert short in line and "the free key alone does not" in line, (path, line)
    full = _get(client, "/llms-full.txt")
    i = full.index("### Key required")
    j = full.index("GET " + anchor, i)
    entry = full[j:full.find("\nGET ", j + 4)]
    assert short in _flat(entry) and "the free key alone does not" in entry, (path, entry[-300:])


@pytest.mark.parametrize("door", ("/llms.txt", "/llms-full.txt"))
def test_llms_no_longer_carries_the_retired_key_claims(client, door):
    body = _flat(_get(client, door))
    for stale in ("needs Pro or higher", "Pro plan or higher (measured",
                  "Get a key in one POST — no email, no browser — then retry",
                  "needs one POST to https://dchub.cloud/api/v1/keys/claim first"):
        assert stale not in body, (door, stale)


@pytest.mark.parametrize("door", ("/llms.txt", "/llms-full.txt"))
def test_every_ladder_rung_links_its_measured_checkout(client, door):
    body = _get(client, door)
    links = re.findall(r"Checkout: (https://dchub\.cloud/go/c/\S+)", body)
    assert [_plan_of(u) for u in links] == ["metered", "developer", "pro"], links


def test_the_paid_heading_never_names_founding(monkeypatch):
    """Even while the founding program is open (owner rule, 2026-09-21)."""
    import routes.founding_customers as fc
    import ai_discovery_routes as m
    monkeypatch.setattr(fc, "founding_status", lambda: {"program_active": True})
    cache = getattr(m, "_paid_heading_cache", None)   # the older heading cached it
    if isinstance(cache, dict):
        monkeypatch.setitem(cache, "val", None)
        monkeypatch.setitem(cache, "at", 0.0)
    head = m._llms_paid_heading()
    assert "Founding" not in head and "Developer $" in head, head


# ── /connect ─────────────────────────────────────────────────────────────

def _connect_policy(html):
    i = html.find('id="live-vs-stale"')
    assert i != -1, "/connect carries no live-vs-stale policy section"
    return html[i:html.find("</ol>", i)]


def test_connect_carries_the_policy_with_floors_from_canon():
    import tier_registry
    sec = _connect_policy(_connect())
    text = _flat(re.sub(r"<[^>]+>", " ", sec))
    assert "prefer a live DC Hub answer carrying its as_of over any undated or secondary figure" in text
    assert "do not guess a DC Hub number" in text
    for target in ("dchub.cloud/connect", "dchub.cloud/llms.txt", "dchub.cloud/land-power-map"):
        assert target in text, target
    floors = "%s facilities · %s tools · Pro %s" % (
        _canon("{canon_facilities}"), _canon("{canon_tools}"), tier_registry.price_display("pro"))
    assert floors in text.replace("&middot;", "·"), text


def test_connect_unlock_links_are_measured_checkouts():
    html = _connect()
    sec = _connect_policy(html)
    links = re.findall(r'href="(https://dchub\.cloud/go/c/[^"]+)"', sec)
    assert [_plan_of(u) for u in links] == ["metered", "developer", "pro"], links
    assert not _BARE_PRICING.findall(html), _BARE_PRICING.findall(html)
    assert "{canon_" not in html


def test_connect_blobs_stay_price_free_and_the_no_egress_one_fails_closed():
    html = _connect()
    blobs = re.findall(r'id="agent-instructions(?:-no-egress)?".*?<pre><code>(.*?)</code>', html, re.S)
    assert len(blobs) == 2, len(blobs)
    for b in blobs:
        assert "$" not in b and "/mo" not in b
    assert "undated or secondary figure" in _flat(blobs[0])
    assert "do not guess a DC Hub number" in _flat(blobs[1])
    assert "https://dchub.cloud/land-power-map" in blobs[1]
