"""A plan-named key prefix grants its plan only when the key has an active row.

`dchub_pro_…`, `dchub_developer_…` and the other plan-named prefixes exist
because routes/partner_key_issuer.py (raw key in api_keys.key_hash) and
api_tier_gating.generate_api_key (sha256 in key_hash) mint them, and both write
an active api_keys row for every key they issue. `mcp_gatekeeper.resolve_tier`
used to return the plan a prefix names without looking the key up. It now
resolves every dchub_ key through `_resolve_from_db_hash` — the raw-or-sha256
key_hash match on an active row — and a key with no such row is FREE. What
tier a found row grants is tests/test_key_tier_follows_row.py's subject.

These run the real resolvers (`resolve_tier`, `routes.tier_gate.
_resolve_caller_tier`, `caller_is_privileged`, `require_tier`) with only the
row lookup replaced, so no database is involved.
tests/test_prefixed_key_row_sql.py runs the lookup itself against Postgres.
"""
import ast
import pathlib
import secrets

import pytest

flask = pytest.importorskip("flask")

import mcp_gatekeeper as mg  # noqa: E402
from routes import tier_gate  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Every plan-named prefix, with the tier it names. The long forms are
# partner_key_issuer's; the short forms are generate_api_key's.
PREFIXES = {
    "dchub_enterprise_": "ENTERPRISE",
    "dchub_developer_": "DEVELOPER",
    "dchub_starter_": "STARTER",
    "dchub_ent_": "ENTERPRISE",
    "dchub_pro_": "PRO",
    "dchub_dev_": "DEVELOPER",
    "dchub_sta_": "STARTER",
}
EXTERNAL = {"REMOTE_ADDR": "203.0.113.9"}   # not loopback: no trust signal


class _Rows:
    """The api_keys rows the lookup can see, and every key it was asked for."""

    def __init__(self):
        self.held, self.asked = {}, []

    def lookup(self, api_key):
        self.asked.append(api_key)
        return self.held.get(api_key)


@pytest.fixture
def rows(monkeypatch):
    r = _Rows()
    monkeypatch.setattr(mg, "_resolve_from_db_hash", r.lookup)
    monkeypatch.setattr(mg, "_key_store", {})
    monkeypatch.setattr(mg, "_row_tiers", {})
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return r


def _key(prefix):
    return prefix + secrets.token_urlsafe(24)


def _caller_tier(headers):
    app = flask.Flask(__name__)
    with app.test_request_context("/", headers=headers, environ_base=EXTERNAL):
        return tier_gate._resolve_caller_tier()[0]


@pytest.mark.parametrize("prefix", sorted(PREFIXES))
def test_a_prefixed_key_with_no_row_is_free(rows, prefix):
    key = _key(prefix)
    assert mg.resolve_tier(key) == mg.Tier.FREE
    assert _caller_tier({"X-API-Key": key}) == "FREE"
    assert rows.asked == [key, key], "the row lookup never ran for this key"


@pytest.mark.parametrize("prefix", sorted(PREFIXES))
def test_a_prefixed_key_with_an_active_row_keeps_its_plan(rows, prefix):
    key = _key(prefix)
    rows.held[key] = mg.Tier[PREFIXES[prefix]]
    assert _caller_tier({"X-API-Key": key}) == PREFIXES[prefix]


def test_the_query_parameter_is_held_to_the_same_rule(rows):
    key = _key("dchub_enterprise_")
    app = flask.Flask(__name__)
    with app.test_request_context("/?api_key=" + key, environ_base=EXTERNAL):
        assert tier_gate._resolve_caller_tier()[0] == "FREE"


def test_caller_is_privileged_needs_the_row(rows):
    key = _key("dchub_developer_")
    app = flask.Flask(__name__)
    with app.test_request_context("/", headers={"X-API-Key": key},
                                  environ_base=EXTERNAL):
        assert tier_gate.caller_is_privileged("DEVELOPER") is False
    rows.held[key] = mg.Tier.DEVELOPER
    with app.test_request_context("/", headers={"X-API-Key": key},
                                  environ_base=EXTERNAL):
        assert tier_gate.caller_is_privileged("DEVELOPER") is True


def test_require_tier_needs_the_row(rows, monkeypatch):
    monkeypatch.setattr(tier_gate, "_gate_response",
                        lambda *a, **k: (flask.jsonify(gated=True), 402))
    app = flask.Flask(__name__)

    @app.route("/paid")
    @tier_gate.require_tier("PRO")
    def paid():
        return "full"

    key = _key("dchub_pro_")
    client = app.test_client()
    assert client.get("/paid", headers={"X-API-Key": key},
                      environ_base=EXTERNAL).status_code == 402
    rows.held[key] = mg.Tier.PRO
    assert client.get("/paid", headers={"X-API-Key": key},
                      environ_base=EXTERNAL).status_code == 200


def test_markets_list_takes_no_tier_from_a_key_prefix():
    """main.py cannot be imported in tests, so read its startswith() calls."""
    banned = tuple(PREFIXES) + ("dev_", "pro_", "ent_")
    tree = ast.parse((ROOT / "main.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "startswith"]
    assert len(calls) > 50, "main.py's startswith() calls were not read"
    hits = sorted({(n.lineno, c.value) for n in calls for a in n.args
                   for c in ast.walk(a) if isinstance(c, ast.Constant)
                   and isinstance(c.value, str) and c.value in banned})
    assert not hits, hits
