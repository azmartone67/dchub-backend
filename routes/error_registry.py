"""error_registry.py — the published, versioned error_code taxonomy for the
error_version:1 contract (Gemini partnership 'Normative Grounding' delta,
2026-07-12).

WHY: Gemini's routing state machine reacts to a STABLE taxonomy of error_codes
rather than parsing variable text strings. This module is the single source of
truth — every error_code an agent can receive from any DC Hub surface (MCP,
REST, WebMCP) is enumerated here with its severity and recovery semantics, and
served at:

    GET /api/v1/errors/registry              (JSON)
    GET /.well-known/dchub-error-registry.json  (discovery alias)

so an agent fetches the taxonomy ONCE and binds its state machine to it.

VERSIONING: ``registry_version`` bumps ONLY on a breaking change — a removed or
renamed code, or a changed severity (which would flip an agent's routing).
Purely additive new codes keep the version (an agent that doesn't know a new
code falls back to its ``severity``, which is always present in the payload).

Pure + importable with no Flask/DB/network dependency at module scope
(reference_dchub_green_main_0709 — pre-merge tests never import main); the
blueprint registration rides cron_heartbeat_bp.record_once (main.py wiring is
frozen for parallel worktree tracks — same pattern as analyst_note /
metric_truth_check / dark_availability_zones).
"""
from __future__ import annotations

import datetime
import logging

logger = logging.getLogger("error_registry")

# Bumped ONLY on a breaking taxonomy change (removed/renamed code or changed
# severity). Additive codes do NOT bump it.
REGISTRY_VERSION = 1

# The three severities = the agent-side state machine (mirrors error_envelope).
SEVERITIES = {
    "parameter_adjustment": (
        "Re-run IMMEDIATELY with the merged suggested_params — a deterministic "
        "corrector. suggested_params keys are always a strict subset of the "
        "tool's declared parameters."),
    "transient_backoff": (
        "Wait ~500ms and retry the SAME params. Rate-limit or upstream pool "
        "pressure — no parameter change unlocks it. Never carries "
        "suggested_params."),
    "fatal": (
        "Fail closed — no retry helps. A physical/logical boundary, a hard "
        "gate, or an internal fallback the agent should surface honestly."),
}

# error_code -> {severity, meaning, surfaces}. severity MUST be a key of
# SEVERITIES. Keep this the EXACT set emitted by the code (a drift-guard test
# asserts every entry is well-formed; wiring should validate emitted codes
# against is_registered()).
CODES = {
    # ── parameter_adjustment (correctable args) ──────────────────────────
    "invalid_iso": {
        "severity": "parameter_adjustment",
        "meaning": ("An unrecognized ISO/grid identifier. suggested_params.iso "
                    "carries the closest valid ISO (edit-distance <= 2); known "
                    "non-US grids are routed to get_grid_scoreboard via the hint."),
        "surfaces": ["mcp:get_grid_data", "mcp:get_iso_context",
                     "mcp:get_interconnection_queue", "mcp:get_refined_queue"],
    },
    "invalid_sites_param": {
        "severity": "parameter_adjustment",
        "meaning": ("The `sites` value could not be parsed (format, count "
                    "2-8, or coordinate range). suggested_params.sites carries "
                    "a valid example string to re-run with."),
        "surfaces": ["rest:/api/v1/fiber/cluster-latency",
                     "mcp:cluster_sites_by_latency"],
    },
    "invalid_criteria": {
        "severity": "parameter_adjustment",
        "meaning": ("An unrecognized ranking criteria enum. "
                    "suggested_params.criteria carries the closest valid enum "
                    "value (via difflib)."),
        "surfaces": ["rest:/api/v1/mcp/tools/rank_markets", "mcp:rank_markets"],
    },
    "max_latency_us_below_physics_floor": {
        "severity": "parameter_adjustment",
        "meaning": ("Advisory on a 200 SUCCESS: the requested max_latency_us "
                    "budget prunes ALL site pairs as physics_impossible. "
                    "suggested_params.max_latency_us carries the minimum "
                    "viable budget computed from the smallest round-trip "
                    "physics floor in the set."),
        "surfaces": ["rest:/api/v1/fiber/cluster-latency",
                     "mcp:cluster_sites_by_latency"],
    },
    # ── transient_backoff (retry same params) ────────────────────────────
    "database_unavailable": {
        "severity": "transient_backoff",
        "meaning": "The backing datastore was momentarily unreachable.",
        "surfaces": ["rest:/api/v1/mcp/tools/rank_markets"],
    },
    "rate_limit_exceeded": {
        "severity": "transient_backoff",
        "meaning": ("The caller exceeded its rate limit. Honor the Retry-After "
                    "header when present; otherwise wait ~500ms and retry."),
        "surfaces": ["rest:*"],
    },
    "tool_execution_failed": {
        "severity": "transient_backoff",
        "meaning": ("An MCP tool handler failed, usually from upstream backend "
                    "pressure. The original args were valid — retry once."),
        "surfaces": ["mcp:*"],
    },
    "webmcp_call_failed": {
        "severity": "transient_backoff",
        "meaning": ("A browser-side WebMCP bridge call failed — an origin-trial "
                    "token validation delay or a dropped network request. The "
                    "args were valid — retry once."),
        "surfaces": ["webmcp:*"],
    },
    # ── fatal (internal fallbacks — an agent surfaces these honestly) ─────
    "envelope_error": {
        "severity": "fatal",
        "meaning": ("The error-envelope builder itself failed and degraded to "
                    "a minimal valid envelope. No retry helps; surface as an "
                    "internal error."),
        "surfaces": ["internal"],
    },
    "unknown_error": {
        "severity": "fatal",
        "meaning": ("An error with no more specific code was raised. Surface "
                    "honestly; no deterministic recovery is defined."),
        "surfaces": ["internal"],
    },
}


def is_registered(error_code) -> bool:
    """True if ``error_code`` is in the published taxonomy. Wiring can assert
    this so an emitted code can never drift out of the registry. Never raises."""
    try:
        return error_code in CODES
    except Exception:
        return False


def severity_of(error_code):
    """The registered severity for a code, or None if unregistered. Never
    raises."""
    try:
        entry = CODES.get(error_code)
        return entry.get("severity") if entry else None
    except Exception:
        return None


def _runtime_generated_at():
    try:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()
    except Exception:
        return ""


def build_registry_payload() -> dict:
    """The full published payload. Pure; never raises."""
    try:
        return {
            "registry_version": REGISTRY_VERSION,
            "generated_at": _runtime_generated_at(),
            "provenance": {
                "source": "DC Hub Protocol Gateway",
                "license": "CC-BY-4.0",
                "cite_as": "DC Hub, dchub.cloud",
            },
            "severities": dict(SEVERITIES),
            "codes": {k: dict(v) for k, v in CODES.items()},
            "note": ("Bind your state machine to `severity`; it is always "
                     "present even for a code your build predates. "
                     "parameter_adjustment carries a strict-subset "
                     "suggested_params; transient_backoff never does."),
        }
    except Exception:
        # Minimal-but-valid — the registry must survive any bug here.
        return {"registry_version": REGISTRY_VERSION, "codes": {},
                "severities": dict(SEVERITIES)}


# ── blueprint (lazy flask import; registered via record_once) ────────────
def _make_blueprint():
    from flask import Blueprint, jsonify

    bp = Blueprint("error_registry", __name__)

    def _serve():
        return jsonify(build_registry_payload()), 200

    bp.add_url_rule("/api/v1/errors/registry", "error_registry_json",
                    _serve, methods=["GET"])
    bp.add_url_rule("/.well-known/dchub-error-registry.json",
                    "error_registry_wellknown", _serve, methods=["GET"])
    return bp


def register(app):
    """Idempotent registration. Fail-soft: a broken registration can never
    break boot or the heartbeat."""
    try:
        if "error_registry" in getattr(app, "blueprints", {}):
            return
        app.register_blueprint(_make_blueprint())
    except Exception as _e:  # pragma: no cover
        logger.warning("error_registry register skipped: %s", _e)
