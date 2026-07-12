"""error_code registry tests (Gemini 'Normative Grounding' delta, 2026-07-12).

Pure-unit — no DB, no JWT_SECRET, never imports main
(reference_dchub_green_main_0709). Guards:

  * every registered code is well-formed (valid severity, has meaning+surfaces)
  * severity is one of the three contract values
  * the drift-guard helpers (is_registered / severity_of)
  * the served payload shape (registry_version, codes, severities) via a
    throwaway Flask app + the module's own register()
  * the known emitted codes across every surface are all in the registry
"""
from __future__ import annotations

from routes.error_registry import (
    CODES,
    REGISTRY_VERSION,
    SEVERITIES,
    build_registry_payload,
    is_registered,
    register,
    severity_of,
)

_VALID_SEVERITIES = set(SEVERITIES)


def test_registry_version_is_int():
    assert isinstance(REGISTRY_VERSION, int) and REGISTRY_VERSION >= 1


def test_three_contract_severities():
    assert _VALID_SEVERITIES == {
        "parameter_adjustment", "transient_backoff", "fatal"}


def test_every_code_is_well_formed():
    for code, entry in CODES.items():
        assert isinstance(code, str) and code == code.lower()
        assert entry["severity"] in _VALID_SEVERITIES, code
        assert entry["meaning"] and isinstance(entry["meaning"], str), code
        assert entry["surfaces"] and isinstance(entry["surfaces"], list), code


def test_transient_backoff_never_implies_params():
    # documentation invariant: transient_backoff codes describe retry-same,
    # so their meaning must not promise a suggested_params corrector.
    for code, entry in CODES.items():
        if entry["severity"] == "transient_backoff":
            assert "suggested_params" not in entry["meaning"], code


def test_is_registered_and_severity_of():
    assert is_registered("invalid_iso")
    assert not is_registered("totally_made_up_code")
    assert not is_registered(None)
    assert severity_of("rate_limit_exceeded") == "transient_backoff"
    assert severity_of("nope") is None


def test_known_emitted_codes_are_registered():
    # the exact codes emitted across MCP / REST / WebMCP / fallbacks — the
    # taxonomy must cover every one (drift guard).
    emitted = {
        "invalid_iso", "invalid_sites_param", "invalid_criteria",
        "max_latency_us_below_physics_floor", "database_unavailable",
        "rate_limit_exceeded", "tool_execution_failed", "webmcp_call_failed",
        "envelope_error", "unknown_error",
    }
    missing = emitted - set(CODES)
    assert not missing, f"emitted codes missing from registry: {missing}"


def test_payload_shape():
    p = build_registry_payload()
    assert p["registry_version"] == REGISTRY_VERSION
    assert set(p["severities"]) == _VALID_SEVERITIES
    assert p["codes"]["invalid_iso"]["severity"] == "parameter_adjustment"
    # provenance present
    assert p["provenance"]["cite_as"] == "DC Hub, dchub.cloud"
    # deep-copied — mutating the payload must not corrupt the module registry
    p["codes"].clear()
    assert "invalid_iso" in CODES


def test_endpoint_serves_via_throwaway_app():
    from flask import Flask
    app = Flask(__name__)
    register(app)
    register(app)  # idempotent — second call is a no-op, must not raise
    client = app.test_client()
    for path in ("/api/v1/errors/registry",
                 "/.well-known/dchub-error-registry.json"):
        r = client.get(path)
        assert r.status_code == 200, path
        body = r.get_json()
        assert body["registry_version"] == REGISTRY_VERSION
        assert "invalid_iso" in body["codes"]


def test_register_failsoft_on_garbage_app():
    # a broken app object must never raise out of register()
    register(object())
