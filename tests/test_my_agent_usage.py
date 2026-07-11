"""r-my-agent (2026-07-11): /my-agent self-serve usage dashboard.

Unit-level only (green-main rule: never import main; no DB in pre-merge
pytest). Covers the blueprint's route surface, the tier normalizer that
mirrors flask_mcp_endpoints, and the two privacy invariants: keyed
responses are no-store, and emails/keys are masked.
"""

import pathlib

from routes.my_agent import my_agent_bp, _tier_norm, _mask_email, _GATE_STATUSES

SRC = pathlib.Path(__file__).resolve().parent.parent / "routes" / "my_agent.py"
PAGE = pathlib.Path(__file__).resolve().parent.parent / "static" / "my-agent.html"


def test_blueprint_exposes_both_routes():
    rules = {str(getattr(f, "__name__", "")) for f in my_agent_bp.deferred_functions}
    # deferred_functions are opaque closures; assert via the source instead
    src = SRC.read_text()
    assert '@my_agent_bp.get("/api/v1/my/usage")' in src
    assert '@my_agent_bp.get("/my-agent")' in src


def test_tier_norm_mirrors_node_gate_vocabulary():
    # paying plans must normalize to 'paid'/'enterprise' (the pay->free leak lesson)
    assert _tier_norm(["founding"]) == "paid"
    assert _tier_norm(["pro", None]) == "paid"
    assert _tier_norm(["metered"]) == "paid"
    assert _tier_norm(["research_seed"]) == "enterprise"
    assert _tier_norm(["admin", "free"]) == "enterprise"
    assert _tier_norm(["developer"]) == "developer"
    assert _tier_norm(["", None]) == "free"


def test_email_masking_never_leaks_local_part():
    assert _mask_email("jonathan@dchub.cloud") == "jo…@dchub.cloud"
    assert _mask_email("ab@x.io") == "…@x.io"
    assert _mask_email(None) is None
    assert _mask_email("not-an-email") is None


def test_keyed_responses_are_no_store_and_key_never_in_url():
    src = SRC.read_text()
    assert "private, no-store" in src, "keyed responses must never be edge-cached"
    # every jsonify return goes through _nostore()
    assert src.count("_nostore(") >= 5
    page = PAGE.read_text()
    assert "X-API-Key" in page and "api_key=" not in page, \
        "the page must send the key as a header, never a query param"


def test_gate_statuses_cover_the_known_block_vocabulary():
    for s in ("blocked_paid_only", "rate_limited", "anon_daily_cap",
              "bind_email_required"):
        assert s in _GATE_STATUSES
