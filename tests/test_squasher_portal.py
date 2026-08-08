"""Tests for routes/squasher_portal.py.

The verdict function is the whole point of the page: it must NOT be able to
say green while nothing is shipping, and it must NOT turn an unreadable stage
into a pass. Both are the failure modes this platform has actually shipped
(BLIND≠RED; "green means the 26 things I look at are fine").
"""

from routes import squasher_portal as sp


def _d(**act):
    """A snapshot whose act stage is under test; other stages healthy."""
    base = {
        "detect": {"known": True, "open_red": 25},
        "route": {"known": True, "active": 23},
        "propose": {"known": True, "considered_10": 55, "generated_10": 2},
        "act": {"known": True, "enabled": True, "breaker_tripped": False,
                "landed_7d": 0, "last_merge_days": 44.0},
        "verify": {"known": True, "closure_pct": 2.2},
    }
    base["act"].update(act)
    return base


# ── the verdict cannot lie green ────────────────────────────────────────

def test_green_requires_a_fix_to_have_LANDED():
    assert sp.verdict_for(_d(landed_7d=1))["state"] == "GREEN"


def test_zero_landed_is_never_green_however_healthy_the_rest():
    # Kills: grading on "armed and no breaker" — true for six weeks while
    # nothing shipped.
    v = sp.verdict_for(_d(landed_7d=0))
    assert v["state"] == "AMBER"
    assert "7d" in v["headline"]


def test_a_disarmed_lane_is_RED_even_with_a_recent_merge():
    # Kills: reading `enabled` after the landed check — a lane someone turned
    # off would show green off historical merges.
    v = sp.verdict_for(_d(enabled=False, landed_7d=3))
    assert v["state"] == "RED"
    assert "disarmed" in v["headline"].lower()


def test_a_tripped_breaker_is_RED_and_outranks_everything():
    v = sp.verdict_for(_d(breaker_tripped=True, landed_7d=5))
    assert v["state"] == "RED"
    assert "breaker" in v["headline"].lower()


def test_unreadable_act_stage_is_UNKNOWN_not_a_pass():
    # BLIND != RED and BLIND != PASS. Kills: defaulting `known` to a verdict.
    v = sp.verdict_for({"act": {"known": False}, "propose": {}})
    assert v["state"] == "UNKNOWN"


def test_idle_lane_names_PROPOSE_as_the_bottleneck():
    # When work exists but nothing is proposed, the operator must be pointed at
    # the propose stage, not at the merge lane that has nothing to merge.
    d = _d(landed_7d=0)
    d["propose"] = {"known": True, "considered_10": 54, "generated_10": 0}
    v = sp.verdict_for(d)
    assert v["state"] == "AMBER"
    assert "PROPOSE" in v["detail"]


# ── rendering must never invent a zero ──────────────────────────────────

def test_unknown_renders_as_a_dash_never_zero():
    # A dash means "could not look"; a 0 means "measured none". Conflating them
    # is how a dead stage reads as a healthy one.
    assert sp._n(None) == "—"
    assert sp._n(0) == "0"


def test_render_produces_a_page_for_a_fully_blind_snapshot():
    d = {"as_of": "2026-08-08T00:00:00+00:00",
         "detect": {}, "route": {}, "propose": {}, "act": {}, "verify": {}}
    d["verdict"] = sp.verdict_for(d)
    html = sp.render(d)
    assert "Bug squasher" in html
    assert "v-UNKNOWN" in html
    assert "—" in html          # dashes, not fabricated zeros


def test_render_escapes_untrusted_text():
    d = _d(landed_7d=0)
    d["as_of"] = "<script>alert(1)</script>"
    d["propose"] = {"known": True, "considered_10": 1, "generated_10": 0,
                    "runs": [{"ts": "2026-08-08T00:00:00", "source": "<img/>",
                              "considered": 1, "generated": 0}]}
    d["verdict"] = sp.verdict_for(d)
    html = sp.render(d)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img/>" not in html


def test_collect_shape_is_stable_without_a_request_context():
    # _get fails soft outside an app context, so collect() must still return
    # every stage key rather than raising — the page renders "cannot read".
    out = sp.collect()
    for k in ("detect", "route", "propose", "act", "verify", "verdict"):
        assert k in out
    assert out["act"]["known"] is False


# ── loopback auth (the bug the page's own honesty rule caught on run 1) ──

def test_self_auth_headers_carry_the_admin_key(monkeypatch):
    # Kills: dropping the headers from _get, which made detect/route/act all
    # 401 -> {} -> "cannot read the auto-merge lane" on the first live run.
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "int")
    h = sp._self_auth_headers()
    assert h["X-Admin-Key"] == "adm"
    assert h["X-Internal-Key"] == "int"


def test_self_auth_headers_omit_absent_keys(monkeypatch):
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    monkeypatch.delenv("INTERNAL_KEY", raising=False)
    assert sp._self_auth_headers() == {}


def test_get_passes_headers_to_the_test_client(monkeypatch):
    seen = {}

    class _R:
        status_code = 200
        def get_json(self):  # noqa: D102
            return {"ok": True}

    class _C:
        def __enter__(self):  # noqa: D105
            return self
        def __exit__(self, *a):  # noqa: D105
            return False
        def get(self, path, headers=None):  # noqa: D102
            seen["headers"] = headers
            return _R()

    class _App:
        def test_client(self):  # noqa: D102
            return _C()

    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    import flask
    monkeypatch.setattr(flask, "current_app", _App(), raising=False)
    out = sp._get("/api/v1/brain/automerge/status")
    assert out == {"ok": True}
    assert seen["headers"].get("X-Admin-Key") == "adm"
