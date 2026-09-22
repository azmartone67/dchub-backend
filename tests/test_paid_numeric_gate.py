"""util/paid_numeric_gate.py — who gets paid numerics, and what everyone else
gets (free/anon tighten, 2026-09-21).

The gate the refined queue, the grid-intelligence alias, the power totals,
the availability timeline, the AI capacity index and the rankings share. The
routes are driven in tests/test_free_tease_routes.py; this file pins the gate:

  * who resolves to the full answer: Developer and above from ANY credential
    require_plan reads (a paid website session included), the internal key,
    the admin key;
  * a key below Developer: the full answer for one pack credit, burned only on
    a delivered 200, else the tease;
  * everyone else: the tease, which the gate never marks private (it is
    caller-independent), while every full answer is private/no-store;
  * the gate fails closed: a mask that raises, or a 200 it cannot parse,
    never leaks the full body;
  * the 403 wall is the keyed-walls gate, and the 4xx hint middleware
    stays out of its body.

House rules: no DB, no network, main.py never imported, nothing at module
scope.
"""
import pytest
from flask import Flask, jsonify, request

import api_data_protection
import api_tier_gating
import routes.mcp_conversion_plays as ledger
import util.location_meter as location_meter
from util import paid_numeric_gate as gate

INTERNAL = "internal-test-key-0921"
ADMIN = "admin-test-key-0921"

KEYS = {
    "k-free": {"plan": "free"},
    "k-identified": {"plan": "identified"},
    "k-starter": {"plan": "starter"},
    "k-developer": {"plan": "developer"},
    "k-pro": {"plan": "pro"},
    "k-free-pack": {"plan": "free"},
    "k-admin-role": {"plan": "pro", "role": "admin"},
}
JWT_PLANS = {"jwt-developer": "developer", "jwt-free": "free", "jwt-pro": "pro"}


@pytest.fixture
def world(monkeypatch):
    """Credential lookups and the credit ledger at their module boundaries."""
    credits = {"k-free-pack": 5}
    burns = []
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", INTERNAL)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN)
    monkeypatch.delenv("DCHUB_AI_WARS_KEYS", raising=False)
    monkeypatch.setattr(api_tier_gating, "validate_api_key", lambda k: KEYS.get(k))
    # _get_decode_jwt() falls back to `import main` when unset: never here.
    monkeypatch.setattr(api_tier_gating, "_decode_jwt_fn",
                        lambda tok: {"user_id": tok} if tok in JWT_PLANS else None)
    monkeypatch.setattr(api_tier_gating, "get_user_plan",
                        lambda user_id=None, email=None: JWT_PLANS.get(user_id, "free"))
    monkeypatch.setattr(api_data_protection, "_resolve_key_tier", lambda k: None)

    def pack_active(api_key=None, mcp_session=None):
        return credits.get(api_key, 0) > 0

    def consume_credits(api_key, mcp_session_id, count=1):
        burns.append(api_key)
        credits[api_key] -= count
        return {"ok": True, "remaining": credits[api_key]}

    monkeypatch.setattr(location_meter, "pack_active", pack_active)
    monkeypatch.setattr(ledger, "consume_credits", consume_credits)
    return {"credits": credits, "burns": burns}


def _headers(cred):
    if cred is None:
        return {}
    kind, value = cred
    if kind == "key":
        return {"X-API-Key": value}
    if kind == "bearer":
        return {"Authorization": "Bearer " + value}
    if kind == "cookie":
        return {"Cookie": "dchub_token=" + value}
    if kind == "internal":
        return {"X-Internal-Key": value}
    if kind == "admin":
        return {"X-Admin-Key": value}
    raise AssertionError(kind)


# ── who is asking ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cred,want", [
    (None, gate.ANON),
    (("key", "k-unknown"), gate.ANON),
    (("key", "k-free"), gate.BELOW),
    (("key", "k-identified"), gate.BELOW),
    (("key", "k-starter"), gate.BELOW),
    (("key", "k-developer"), gate.FULL),
    (("key", "k-pro"), gate.FULL),
    (("cookie", "jwt-developer"), gate.FULL),
    (("cookie", "jwt-free"), gate.BELOW),
    (("bearer", "jwt-pro"), gate.FULL),
    (("internal", INTERNAL), gate.SAME),
    (("internal", "not-the-key"), gate.ANON),
    (("admin", ADMIN), gate.SAME),
    (("admin", "not-the-key"), gate.ANON),
    (("key", "k-admin-role"), gate.SAME),
])
def test_access_resolves_every_credential_require_plan_reads(world, cred, want):
    """A paid website session (cookie or Bearer JWT, no API key) must resolve
    to its plan: a gate that read X-API-Key alone locks paid web users out."""
    app = Flask(__name__)
    with app.test_request_context("/x", headers=_headers(cred)):
        assert gate.access()[0] == want


def test_access_fails_closed_to_the_tease(world, monkeypatch):
    def boom():
        raise RuntimeError("resolver down")
    monkeypatch.setattr(api_tier_gating, "get_request_principal", boom)
    app = Flask(__name__)
    with app.test_request_context("/x", headers={"X-API-Key": "k-pro"}):
        assert gate.access() == (gate.ANON, None)


# ── bands ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mw,band", [
    (None, None), ("n/a", None), (float("nan"), None),
    (0, "0 MW"), (0.4, "under 10 MW"), (9.99, "under 10 MW"),
    (10, "10–50 MW"), (49.9, "10–50 MW"), (50, "50–100 MW"),
    (100, "100–500 MW"), (500, "500 MW–1 GW"), (999, "500 MW–1 GW"),
    (1000, "1–5 GW"), (3200.0, "1–5 GW"), ("73824.1", "50–100 GW"),
    (5000, "5–10 GW"), (10_000, "10–50 GW"), (50_000, "50–100 GW"),
    (100_000, "100–500 GW"), (500_000, "500 GW–1 TW"), (1_126_273.7, "1 TW or more"),
    (-1200, "net decline 1–5 GW"),
])
def test_mw_band(mw, band):
    assert gate.mw_band(mw) == band


def test_band_fields_nulls_the_figure_and_keeps_the_key():
    row = {"state": "TX", "total_mw": 18925.0, "facility_count": 634}
    gate.band_fields(row, ("total_mw", "absent_mw"))
    assert row == {"state": "TX", "total_mw": None, "total_mw_band": "10–50 GW",
                   "facility_count": 634}


# ── the decorator ────────────────────────────────────────────────────────────

FULL_ROWS = [{"name": "a", "mw": 3200.0}, {"name": "b", "mw": 40.0}]


def _mask(payload):
    for r in payload["rows"]:
        gate.band_fields(r, ("mw",))
    return payload, len(payload["rows"])


def _app(mask=_mask, view_body=None, status=200):
    app = Flask(__name__)

    @app.route("/thing")
    @gate.tease_numerics(mask, locked=("rows[].mw",))
    def thing():
        body = view_body if view_body is not None else {"rows": [dict(r) for r in FULL_ROWS]}
        return jsonify(body), status

    return app


def _get(app, cred=None, path="/thing"):
    client = app.test_client()
    if cred and cred[0] == "cookie":
        # the test client owns the Cookie header; a raw one is overwritten
        client.set_cookie("dchub_token", cred[1])
        cred = None
    r = client.get(path, headers=_headers(cred))
    return r, r.get_json(silent=True)


def test_anonymous_gets_the_tease_with_the_envelope(world):
    r, body = _get(_app())
    assert r.status_code == 200
    assert [row["mw"] for row in body["rows"]] == [None, None]
    assert [row["mw_band"] for row in body["rows"]] == ["1–5 GW", "10–50 MW"]
    assert body["_gated"] is True and body["_preview_only"] is True
    assert body["_locked_fields"] == ["rows[].mw"]
    assert body["_total_available"] == 2
    assert body["upgrade_url"].startswith("https://dchub.cloud/")
    # caller-independent: the gate never marks the tease private
    assert "private" not in r.headers.get("Cache-Control", "")


@pytest.mark.parametrize("cred", [("key", "k-developer"), ("key", "k-pro"),
                                  ("cookie", "jwt-developer"), ("bearer", "jwt-pro")])
def test_full_callers_get_the_full_answer_private_and_unstored(world, cred):
    r, body = _get(_app(), cred)
    assert r.status_code == 200
    assert [row["mw"] for row in body["rows"]] == [3200.0, 40.0]
    assert "_gated" not in body
    assert "no-store" in r.headers["Cache-Control"] and "private" in r.headers["Cache-Control"]
    assert world["burns"] == []


@pytest.mark.parametrize("cred", [("internal", INTERNAL), ("admin", ADMIN),
                                  ("key", "k-admin-role")])
def test_internal_and_admin_get_exactly_what_they_got_before(world, cred):
    """The MCP server applies its own masks; the gate does not touch its answer,
    headers included."""
    r, body = _get(_app(), cred)
    assert r.status_code == 200
    assert body == {"rows": FULL_ROWS}
    assert "Cache-Control" not in r.headers and "Vary" not in r.headers


@pytest.mark.parametrize("cred", [("key", "k-free"), ("key", "k-identified"),
                                  ("cookie", "jwt-free"), ("key", "k-unknown")])
def test_below_developer_without_credits_gets_the_tease(world, cred):
    r, body = _get(_app(), cred)
    assert r.status_code == 200
    assert body["_gated"] is True
    assert all(row["mw"] is None for row in body["rows"])
    assert world["burns"] == []


def test_a_pack_key_gets_the_full_answer_for_one_credit(world):
    r, body = _get(_app(), ("key", "k-free-pack"))
    assert [row["mw"] for row in body["rows"]] == [3200.0, 40.0]
    assert world["burns"] == ["k-free-pack"]
    assert r.headers["X-DCHub-Access"] == "pack"
    assert "no-store" in r.headers["Cache-Control"]


def test_a_partner_egress_tease_carries_its_ref_and_is_not_shared(world, monkeypatch):
    """A keyless caller from a declared partner egress gets checkout links with
    a ref minted for that request (the keyed-walls ladder), so that tease must
    not ride a shared cache."""
    import routes.partner_attribution as pa
    monkeypatch.setattr(pa, "offer_ref_for_request", lambda path="": "a-0123456789abcdef01234567")
    r, body = _get(_app())
    assert body["_gated"] is True
    assert "no-store" in r.headers["Cache-Control"]
    monkeypatch.setattr(pa, "offer_ref_for_request", lambda path="": "")
    r, _ = _get(_app())
    assert "private" not in r.headers.get("Cache-Control", "")


def test_a_pack_key_spends_nothing_on_an_error(world):
    r, _ = _get(_app(status=503), ("key", "k-free-pack"))
    assert r.status_code == 503
    assert world["burns"] == []


def test_the_tease_masks_an_error_body_that_carries_a_partial_answer(world):
    r, body = _get(_app(status=503))
    assert r.status_code == 503
    assert body["rows"][0]["mw"] is None
    assert "_gated" not in body            # an error is not a preview


def test_a_mask_that_raises_never_serves_the_full_body(world):
    def broken(_payload):
        raise KeyError("rows")
    r, body = _get(_app(mask=broken))
    assert r.status_code == 503
    assert body["error"] == "preview_unavailable"
    assert "3200" not in r.get_data(as_text=True)


def test_a_200_the_gate_cannot_parse_never_passes_through(world):
    app = Flask(__name__)

    @app.route("/list")
    @gate.tease_numerics(_mask, locked=("mw",))
    def as_list():
        return jsonify([{"mw": 3200.0}])

    r, body = _get(app, path="/list")
    assert r.status_code == 503
    assert "3200" not in r.get_data(as_text=True)


def test_preview_args_recompute_the_tease_and_name_what_was_not_applied(world):
    """A view may compute its tease from a narrower query string, so the rows
    and counts it shows never depend on a withheld value."""
    app = Flask(__name__)
    seen = []

    @app.route("/q")
    @gate.tease_numerics(lambda p: (p, 0), locked=("x",),
                         preview_args=lambda a: [(k, v) for k, v in a.items(multi=True)
                                                 if k not in ("min_mw", "limit")]
                                                + [("limit", "3")])
    def q():
        seen.append(dict(request.args))
        return jsonify({"ok": True})

    r, body = _get(app, path="/q?iso=ERCOT&min_mw=500&limit=5000")
    assert seen == [{"iso": "ERCOT", "limit": "3"}]
    assert body["_preview_params_not_applied"] == ["limit", "min_mw"]
    seen.clear()
    _get(app, ("key", "k-developer"), path="/q?iso=ERCOT&min_mw=500&limit=5000")
    assert seen == [{"iso": "ERCOT", "min_mw": "500", "limit": "5000"}]


# ── the 403 wall ─────────────────────────────────────────────────────────────

def _wall_app():
    app = Flask(__name__)

    @app.route("/api/v1/walled")
    @gate.developer_or_pack_wall()
    def walled():
        return jsonify({"peak_mw": 101716})

    return app


@pytest.mark.parametrize("cred,status,code", [
    (None, 403, "plan_required"),
    (("key", "k-free"), 403, "plan_upgrade_required"),
    (("cookie", "jwt-free"), 403, "plan_upgrade_required"),
    (("key", "k-unknown"), 401, "invalid_api_key"),
])
def test_the_wall_refuses_below_developer_and_names_what_opens_it(world, cred, status, code):
    """The keyed-walls gate: require_plan(min_plan, pack_opens=True), whose 403
    offers only what opens the route over REST (the pack, then Developer)."""
    r, body = _get(_wall_app(), cred, path="/api/v1/walled")
    assert r.status_code == status and body["error"] == code
    assert "peak_mw" not in body
    assert "no-store" in r.headers["Cache-Control"]
    if status == 403:
        assert body["required_plan"] == "developer"
        assert body["upgrade_url"].startswith("https://dchub.cloud/")
        assert [o["plan"] for o in body["upgrade_options"]] == ["pack", "developer"]
        assert all(o["opens"] == "rest" for o in body["upgrade_options"])
    text = r.get_data(as_text=True)
    for banned in ("$9/", "Starter", "$199", "$299", "$699", "Founding"):
        assert banned not in text


@pytest.mark.parametrize("cred,same", [(("key", "k-developer"), False),
                                       (("cookie", "jwt-developer"), False),
                                       (("internal", INTERNAL), True), (("admin", ADMIN), True)])
def test_the_wall_opens_for_developer_and_internal(world, cred, same):
    r, body = _get(_wall_app(), cred, path="/api/v1/walled")
    assert r.status_code == 200 and body == {"peak_mw": 101716}
    if same:
        assert "Cache-Control" not in r.headers
    else:
        assert "no-store" in r.headers["Cache-Control"]


def test_the_wall_opens_for_a_pack_credit(world):
    r, body = _get(_wall_app(), ("key", "k-free-pack"), path="/api/v1/walled")
    assert r.status_code == 200 and body == {"peak_mw": 101716}
    assert world["burns"] == ["k-free-pack"]


def test_the_hint_middleware_leaves_the_wall_alone(world, monkeypatch):
    """The 4xx hint middleware appends a Starter pitch to small /api/ 4xx bodies
    unless they are a wall of this kind: tested through the real middleware,
    because a handler test never sees it."""
    from routes import paywall_hint_middleware as mw
    monkeypatch.setattr(mw, "_log_ab_event", lambda *a, **k: None)
    monkeypatch.setattr(mw, "_personal_hit_pitch", lambda *a, **k: "")
    app = _wall_app()
    mw.register_paywall_hint_middleware(app)
    r, body = _get(app, path="/api/v1/walled")
    assert r.status_code == 403
    assert "_upgrade_hint" not in body
    assert "Starter" not in r.get_data(as_text=True)
