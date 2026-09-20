"""resolve_canon()["public"] must receive the measured asset floors.

★ THE BUG. Every resolver block in resolve_canon() assigns c["public"][<key>].
The four asset keys had NO such assignment, so c["public"] kept the copy of
PINNED["public"] it was seeded with, and /api/v1/canon/phrases — which reads
resolve_public_floors() -> resolve_canon()["public"] — could not publish a
measured asset floor however well it had been measured.

Proven 2026-09-19 by injecting a PERFECT _live_public_floors() and reading
c["public"]: facilities and deals moved, while substations "127,000+",
fiber_routes "58,000+", transmission_lines "94,000+" and assets "320,000+" all
came back UNCHANGED at the pin. _live_public_floors() was consumed only by
canon_nums(), for the {canon_*} PLACEHOLDERS — a different path this endpoint
never touches.
"""
import pytest

import ai_surface_canon as canon

ASSET_KEYS = ("substations", "fiber_routes", "transmission_lines", "assets")


@pytest.fixture(autouse=True)
def no_live_probes(monkeypatch):
    """resolve_canon() probes the live REST and MCP endpoints.

    These tests call the REAL resolve_canon() — that is the point, since the
    defect was a missing assignment inside it — so its HTTP helpers have to be
    stubbed or the no-network hook fails the unit-tests step. Stubbed here
    rather than registered as network debt: the probes are irrelevant to which
    keys land in c["public"], and a test that reaches production to check a
    dict assignment is measuring the wrong thing anyway."""
    monkeypatch.setattr(canon, "_get", lambda *a, **k: None)
    monkeypatch.setattr(canon, "_mcp_tool_names", lambda *a, **k: [])
    monkeypatch.setattr(canon, "_mcp_tool_count", lambda *a, **k: 0)
    monkeypatch.setattr(canon, "_mcp_server_version", lambda *a, **k: "")


def _pin(key):
    return (canon.PINNED.get("public") or {}).get(key)


@pytest.fixture
def distinct_floors():
    """Floors that differ from every pin they replace.

    ★ A fixture whose value EQUALS the pin cannot tell a working writer from a
    no-op — the first probe of this fix used 127,000+ for substations, which is
    exactly its pin, and read as a failure when nothing was wrong. Assert the
    difference rather than trusting the literals to stay unequal."""
    floors = {
        "substations": "128,000+",
        "fiber_routes": "59,000+",
        "transmission_lines": "95,000+",
        "assets": "330,000+",
    }
    for key, value in floors.items():
        assert value != _pin(key), (
            "%s fixture equals its pin (%s) — this test would pass on a no-op"
            % (key, _pin(key))
        )
    return floors


def test_every_asset_floor_reaches_public(monkeypatch, distinct_floors):
    monkeypatch.setattr(canon, "_live_public_floors", lambda: dict(distinct_floors))
    pub = (canon.resolve_canon() or {}).get("public") or {}
    for key, value in distinct_floors.items():
        assert pub.get(key) == value, (
            "%s did not reach public: got %r, pin is %r"
            % (key, pub.get(key), _pin(key))
        )


def test_an_unmeasured_floor_leaves_the_pin_standing(monkeypatch):
    """Absent means unmeasured, and the pin is the honest fallback."""
    monkeypatch.setattr(canon, "_live_public_floors", dict)
    pub = (canon.resolve_canon() or {}).get("public") or {}
    for key in ASSET_KEYS:
        assert pub.get(key) == _pin(key)


def test_the_writer_moves_only_the_keys_that_measured(monkeypatch, distinct_floors):
    """A partially-warm cache must not drag its siblings along."""
    only = {"transmission_lines": distinct_floors["transmission_lines"]}
    monkeypatch.setattr(canon, "_live_public_floors", lambda: dict(only))
    pub = (canon.resolve_canon() or {}).get("public") or {}
    assert pub.get("transmission_lines") == only["transmission_lines"]
    for key in ASSET_KEYS:
        if key != "transmission_lines":
            assert pub.get(key) == _pin(key), "%s moved without measuring" % key


def test_a_raising_floors_lookup_does_not_break_the_payload(monkeypatch):
    """Fail-soft, like every sibling block: the pin stands, nothing raises."""
    def boom():
        raise RuntimeError("cache exploded")
    monkeypatch.setattr(canon, "_live_public_floors", boom)
    c = canon.resolve_canon() or {}
    pub = c.get("public") or {}
    for key in ASSET_KEYS:
        assert pub.get(key) == _pin(key)
    assert "_asset_floors_error" in c


def test_the_endpoint_publishes_the_healed_floor_end_to_end(monkeypatch, distinct_floors):
    """The pair that matters: the writer lands the value AND the earned-label
    rule in resolve_public_floors() is willing to call it live. Both read the
    same _live_public_floors(), so they cannot disagree about 'measured'."""
    monkeypatch.setattr(canon, "_live_public_floors", lambda: dict(distinct_floors))
    out = canon.resolve_public_floors()
    for key, value in distinct_floors.items():
        assert out[key] == value
        assert out["_source"][key] == "live", "%s landed but was not labelled live" % key
