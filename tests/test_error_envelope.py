"""error_version:1 envelope contract tests (Gemini partnership, 2026-07-11).

Pure-unit — no DB, no JWT_SECRET, never imports main
(reference_dchub_green_main_0709). Covers:

  * the helper contract (full envelope shape + provenance block)
  * runtime as_of (datetime.now — NOT hardcoded)
  * the STRICT-SUBSET drop rule (invalid key dropped, valid kept,
    all-invalid → suggested_params omitted)
  * severity coercion + fail-soft on garbage input (never raises)
  * merge-without-clobber into an existing REST error dict
  * one wired surface (routes/cluster_latency.py) end-to-end

Run:  python3 -m pytest tests/test_error_envelope.py -q
"""
from __future__ import annotations

import datetime

from routes.error_envelope import (
    CITE_AS,
    ERROR_VERSION,
    LICENSE,
    PROVENANCE_SOURCE,
    VALID_SEVERITIES,
    error_mitigation,
    merge_error_mitigation,
    validate_suggested_params,
)


# ── helper contract ──────────────────────────────────────────────────────
def test_full_envelope_shape():
    env = error_mitigation(
        "invalid_criteria", "parameter_adjustment",
        "criteria 'cheap' invalid; use one of [...].",
        suggested_params={"criteria": "best_overall"},
        allowed_params=("criteria", "region", "limit"))
    assert env["error_version"] == ERROR_VERSION == 1
    p = env["provenance"]
    assert p["source"] == PROVENANCE_SOURCE == "DC Hub Protocol Gateway"
    assert p["license"] == LICENSE == "CC-BY-4.0"
    assert p["cite_as"] == CITE_AS == "DC Hub, dchub.cloud"
    assert set(p) == {"source", "as_of", "license", "cite_as"}
    m = env["_error_mitigation"]
    assert m["error_code"] == "invalid_criteria"
    assert m["severity"] == "parameter_adjustment"
    assert m["deterministic_hint"] and isinstance(m["deterministic_hint"], str)
    assert m["suggested_params"] == {"criteria": "best_overall"}


def test_as_of_is_runtime_date_not_hardcoded():
    before = datetime.datetime.now().strftime("%Y-%m-%d")
    env = error_mitigation("x", "fatal", "y")
    after = datetime.datetime.now().strftime("%Y-%m-%d")
    as_of = env["provenance"]["as_of"]
    # runtime date, tolerant of a midnight rollover mid-test
    assert as_of in (before, after)
    # strictly YYYY-MM-DD
    datetime.datetime.strptime(as_of, "%Y-%m-%d")


# ── STRICT-SUBSET drop rule (the loop-safety contract) ───────────────────
def test_strict_subset_keeps_valid_drops_invalid():
    env = error_mitigation(
        "e", "parameter_adjustment", "h",
        suggested_params={"criteria": "best_overall", "bogus": 1, "evil": 2},
        allowed_params=("criteria", "region"))
    assert env["_error_mitigation"]["suggested_params"] == {
        "criteria": "best_overall"}


def test_all_invalid_keys_omits_suggested_params():
    env = error_mitigation(
        "e", "parameter_adjustment", "h",
        suggested_params={"bogus": 1, "evil": 2},
        allowed_params=("criteria", "region"))
    assert "suggested_params" not in env["_error_mitigation"]


def test_none_suggested_params_omits_key():
    env = error_mitigation("e", "transient_backoff", "h")
    assert "suggested_params" not in env["_error_mitigation"]


def test_empty_dict_suggested_params_omits_key():
    env = error_mitigation("e", "parameter_adjustment", "h",
                           suggested_params={}, allowed_params=("a",))
    assert "suggested_params" not in env["_error_mitigation"]


def test_allowed_none_fails_closed():
    # 2026-07-12 hardening: no allowed-set => FAIL CLOSED (drop all), so a
    # caller that forgot to declare params can never leak an invalid key that
    # would break the agent's merge-retry loop. Matches the MCP side.
    env = error_mitigation("e", "parameter_adjustment", "h",
                           suggested_params={"anything": 1})
    assert "suggested_params" not in env["_error_mitigation"]  # dropped -> omitted


def test_validate_suggested_params_direct():
    assert validate_suggested_params({"a": 1, "b": 2}, ("a",)) == {"a": 1}
    assert validate_suggested_params({"a": 1}, ()) == {}      # nothing allowed
    assert validate_suggested_params(None, ("a",)) == {}
    assert validate_suggested_params("notadict", ("a",)) == {}
    assert validate_suggested_params({"a": 1}, None) == {}    # fail closed w/o allowed-set


# ── severity ─────────────────────────────────────────────────────────────
def test_invalid_severity_coerced_to_fatal():
    env = error_mitigation("e", "totally_bogus", "h")
    assert env["_error_mitigation"]["severity"] == "fatal"
    assert env["_error_mitigation"]["severity"] in VALID_SEVERITIES


def test_all_three_severities_pass_through():
    for sev in VALID_SEVERITIES:
        env = error_mitigation("e", sev, "h")
        assert env["_error_mitigation"]["severity"] == sev


# ── fail-soft ────────────────────────────────────────────────────────────
def test_failsoft_on_garbage_input_never_raises():
    env = error_mitigation(object(), None, object(),
                           suggested_params=object(), allowed_params=object())
    assert env["error_version"] == 1
    assert "as_of" in env["provenance"]
    m = env["_error_mitigation"]
    assert m["severity"] in VALID_SEVERITIES
    assert "suggested_params" not in m   # object() is not a dict → dropped


# ── merge-without-clobber ────────────────────────────────────────────────
def test_merge_preserves_existing_fields():
    target = {"error": "invalid criteria", "valid_options": ["a", "b"]}
    out = merge_error_mitigation(
        target, "invalid_criteria", "parameter_adjustment", "hint",
        suggested_params={"criteria": "a"}, allowed_params=("criteria",))
    assert out is target                         # mutated in place
    assert out["error"] == "invalid criteria"
    assert out["valid_options"] == ["a", "b"]
    assert out["error_version"] == 1
    assert out["_error_mitigation"]["suggested_params"] == {"criteria": "a"}


def test_merge_does_not_overwrite_existing_envelope_keys():
    target = {"error": "x", "provenance": {"mine": True}}
    out = merge_error_mitigation(target, "c", "fatal", "h")
    assert out["provenance"] == {"mine": True}   # not clobbered
    assert out["error_version"] == 1             # additive keys still land


def test_merge_non_dict_returns_valid_envelope():
    out = merge_error_mitigation(None, "c", "fatal", "h")
    assert out["error_version"] == 1
    assert "_error_mitigation" in out


# ── wired surface: routes/cluster_latency.py ─────────────────────────────
def test_cluster_physics_impossible_advisory_pure():
    from routes.cluster_latency import (
        CLUSTER_LATENCY_PARAMS,
        build_cluster_response,
        parse_sites,
        physics_impossible_mitigation,
    )
    sites, err = parse_sites("40.7128,-74.006:nyc;34.0522,-118.2437:la")
    assert err is None
    res = build_cluster_response(sites, budget_us=1.0)   # 1 µs — impossible
    assert res["pairs"] and all(p["physics_impossible"] for p in res["pairs"])
    m = physics_impossible_mitigation(res)
    assert m is not None
    assert m["severity"] == "parameter_adjustment"
    sp = m["suggested_params"]
    # STRICT SUBSET of the endpoint's own params
    assert set(sp).issubset(set(CLUSTER_LATENCY_PARAMS))
    assert isinstance(sp["max_latency_us"], int) and sp["max_latency_us"] > 1


def test_cluster_no_advisory_when_reachable():
    from routes.cluster_latency import (
        build_cluster_response,
        parse_sites,
        physics_impossible_mitigation,
    )
    sites, _ = parse_sites("39.04,-77.48:a;39.05,-77.49:b")  # ~1 km apart
    res = build_cluster_response(sites, budget_us=100000.0)
    assert physics_impossible_mitigation(res) is None


def test_cluster_bad_input_http_envelope_shape():
    """End-to-end through jsonify: the 400 error body carries the full
    error_version:1 envelope AND preserves the existing human text."""
    import flask

    from routes.cluster_latency import CLUSTER_LATENCY_PARAMS, cluster_latency_bp

    app = flask.Flask(__name__)
    app.register_blueprint(cluster_latency_bp)
    client = app.test_client()

    r = client.get("/api/v1/fiber/cluster-latency")   # no sites → 400
    assert r.status_code == 400
    body = r.get_json()
    assert body["error_version"] == 1
    assert body["provenance"]["source"] == "DC Hub Protocol Gateway"
    datetime.datetime.strptime(body["provenance"]["as_of"], "%Y-%m-%d")
    m = body["_error_mitigation"]
    assert m["error_code"] == "invalid_sites_param"
    assert m["severity"] == "parameter_adjustment"
    assert set(m["suggested_params"]).issubset(set(CLUSTER_LATENCY_PARAMS))
    assert "sites" in m["suggested_params"]
    # existing human-facing fields preserved (not clobbered)
    assert body.get("error") and "example" in body
