"""/api/v1/keys/validate's api_keys fallback: THE ROW DECIDES, not the MAX.

The fallback serves a key with no active mcp_dev_keys row (a dashboard dchub_
key or a partner key pointed at the Node MCP). It used to answer the MAX of
api_keys.rate_limit_tier, api_keys.plan and users.plan. The billing lifecycle
writes only rate_limit_tier: main.handle_payment_failed's dunning demote, both
cancel handlers and routes/expired_demote set it to 'free' and leave
api_keys.plan alone. So the preserved plan outvoted every one of them and a
demoted, canceled or lapsed key kept the paid tool set here, while
mcp_gatekeeper._tier_of_row (#5193) serves the same row FREE.

The owner confirmed the #5193 rule for this path, and that the demoted answer
stays the path's existing non-paid response, {valid:false, tier:free}.

★ EVERY tier_registry.TIERS NAME, in every lifecycle shape: as checkout writes
it (rate_limit_tier = api_tier(plan)), dunning-demoted, restored by
handle_invoice_paid (rate_limit_tier = plan), and canceled. NODE_TIER below is
hand-typed, not computed by the code under test, and it must cover TIERS
exactly, so a new plan name fails here until someone decides its Node tier.

★ THE FAKE READS THE SQL IT IS GIVEN. It answers the api_keys SELECT by column
NAME, so reordering that column list changes which value the handler sees
first, and these tests notice.
"""
import hashlib
import os
import sys
import types
from contextlib import contextmanager

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# flask_mcp_endpoints refuses to import without a DB URL; nothing connects
# until a request runs. Confined to the import and put back, because other
# suites skip on the variable's absence (see test_validate_reports_the_demote).
_injected = not (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL"))
if _injected:
    os.environ["NEON_DATABASE_URL"] = "postgresql://u:p@127.0.0.1:1/db"
try:
    import flask_mcp_endpoints as fme
finally:
    if _injected:
        os.environ.pop("NEON_DATABASE_URL", None)

import tier_registry

INTERNAL_KEY = "test-internal-key"
KEY = "dchub_DASHBOARD_KEY_NO_MCP_ROW"
EMAIL = "payer@example.com"

# The Node vocabulary each plan name lands on. Hand-typed on purpose: this is
# the mapping the change must NOT move (flask_mcp_endpoints._PAID_PLANS /
# _ENT_PLANS; starter and developer sit below 'paid' deliberately).
NODE_TIER = {
    "anonymous":     "free",
    "anon":          "free",
    "free":          "free",
    "identified":    "identified",
    "starter":       "starter",
    "developer":     "developer",
    "pro":           "paid",
    "founding":      "paid",
    "team":          "paid",
    "enterprise":    "enterprise",
    "research_seed": "enterprise",
    "admin":         "enterprise",
}
SERVED = ("paid", "enterprise")   # anything else: the path's non-paid answer
NOT_SERVED = {"valid": False, "tier": "free"}
PLANS = sorted(tier_registry.TIERS)


class _Cur:
    def __init__(self, rate_limit_tier, plan, user_plan):
        self.cols = {"ak.rate_limit_tier": rate_limit_tier, "ak.plan": plan,
                     "u.plan": user_plan, "u.email": EMAIL}
        self._row = None
        self.api_keys_params = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "FROM mcp_dev_keys" in s:
            self._row = None                  # no MCP row: the fallback's cohort
        elif "FROM api_keys ak LEFT JOIN users u" in s:
            self.api_keys_params = params
            select = s[s.index("SELECT") + 6:s.index(" FROM ")]
            self._row = tuple(self.cols[c.strip()] for c in select.split(","))
        else:
            raise AssertionError("unmodelled SQL: %r" % s)

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Pool:
    def __init__(self, cur):
        self._cur = cur

    @contextmanager
    def connection(self):
        yield _Conn(self._cur)


def _validate(monkeypatch, rate_limit_tier, plan, user_plan):
    """Drive the REAL handler through Flask with the pool swapped out."""
    from flask import Flask
    trial = types.ModuleType("routes.auto_trial")
    trial.validate_trial_key = lambda k: (False, "not_a_trial_key")
    monkeypatch.setitem(sys.modules, "routes.auto_trial", trial)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", INTERNAL_KEY)
    cur = _Cur(rate_limit_tier, plan, user_plan)
    monkeypatch.setattr(fme, "_pool", _Pool(cur))
    app = Flask(__name__)
    app.register_blueprint(fme.mcp_bp)
    r = app.test_client().post("/api/v1/keys/validate", json={"api_key": KEY},
                               headers={"X-Internal-Key": INTERNAL_KEY})
    assert r.status_code == 200, r.data
    assert cur.api_keys_params == (hashlib.sha256(KEY.encode()).hexdigest(), KEY), \
        "the api_keys fallback never ran"
    return r.get_json()


def _assert_answer(body, node_tier):
    if node_tier in SERVED:
        assert body["valid"] is True, body
        assert body["tier"] == node_tier, body
        assert body["tier_source"] == "api_keys_no_mcp_row", body
        assert body["tier_detail"]["effective"] == node_tier, body
    else:
        assert body == NOT_SERVED, body


def test_the_expected_table_covers_every_plan_name():
    assert set(NODE_TIER) == set(tier_registry.TIERS)


@pytest.mark.parametrize("plan", PLANS)
def test_a_row_as_checkout_writes_it_keeps_its_tier(monkeypatch, plan):
    # Checkout stores api_tier(plan) in rate_limit_tier. Also the row inside
    # the dunning grace window (failures 1-3): nothing is rewritten until the
    # demote stamps demoted_at.
    body = _validate(monkeypatch, tier_registry.api_tier(plan), plan, plan)
    _assert_answer(body, NODE_TIER[plan])


@pytest.mark.parametrize("plan", PLANS)
def test_a_dunning_demoted_row_is_free(monkeypatch, plan):
    # handle_payment_failed: rate_limit_tier='free'; api_keys.plan and
    # users.plan preserved.
    body = _validate(monkeypatch, "free", plan, plan)
    _assert_answer(body, "free")


@pytest.mark.parametrize("plan", PLANS)
def test_a_restored_row_gets_its_tier_back(monkeypatch, plan):
    # handle_invoice_paid: rate_limit_tier = plan, demote stamp cleared.
    body = _validate(monkeypatch, plan, plan, plan)
    _assert_answer(body, NODE_TIER[plan])


@pytest.mark.parametrize("plan", PLANS)
def test_a_canceled_row_is_free(monkeypatch, plan):
    # subscription deleted / updated->canceled, and expired_demote:
    # users.plan and rate_limit_tier go 'free', api_keys.plan is preserved.
    body = _validate(monkeypatch, "free", plan, "free")
    _assert_answer(body, "free")


# The Python gate's Tier in Node words (hand-typed: its PRO is Node's 'paid').
GATE_TO_NODE = {"FREE": "free", "IDENTIFIED": "identified", "STARTER": "starter",
                "DEVELOPER": "developer", "PRO": "paid", "ENTERPRISE": "enterprise"}

# (rate_limit_tier, api_keys.plan, users.plan) at each lifecycle step above.
SHAPES = {
    "checkout":        lambda p: (tier_registry.api_tier(p), p, p),
    "dunning_demoted": lambda p: ("free", p, p),
    "restored":        lambda p: (p, p, p),
    "canceled":        lambda p: ("free", p, "free"),
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("plan", PLANS)
def test_the_node_path_agrees_with_the_python_gate(monkeypatch, plan, shape):
    # The same row, read by mcp_gatekeeper._tier_of_row (#5193) and by this
    # handler, must land on the same tier. The tests above pin the absolute
    # answers, so this cannot pass by both sides regressing together.
    import mcp_gatekeeper
    row = SHAPES[shape](plan)
    body = _validate(monkeypatch, *row)
    _assert_answer(body, GATE_TO_NODE[mcp_gatekeeper._tier_of_row(*row).name])


@pytest.mark.parametrize("rate_limit_tier,plan,user_plan,node_tier", [
    (None,         "pro",        "free",       "paid"),        # no tier: plan decides
    ("",           "enterprise", None,         "enterprise"),
    ("   ",        "team",       None,         "paid"),        # blank is empty
    (None,         None,         "founding",   "paid"),        # only users.plan left
    (None,         "",           "enterprise", "enterprise"),
    (None,         None,         None,         "free"),        # nothing at all
    ("enterprise", None,         None,         "enterprise"),  # comp key, no plan
    ("enterprise", "free",       "free",       "enterprise"),  # the row, not the min
    ("Founding",   None,         None,         "paid"),        # case-insensitive
])
def test_the_first_non_empty_value_decides(monkeypatch, rate_limit_tier, plan,
                                           user_plan, node_tier):
    body = _validate(monkeypatch, rate_limit_tier, plan, user_plan)
    _assert_answer(body, node_tier)
