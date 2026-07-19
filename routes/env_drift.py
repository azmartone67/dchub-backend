"""env_drift.py — shared-critical env fingerprints (backend vs worker drift).

r-env-drift (2026-07-18). The Railway dashboard saves variables to ONE
service at a time, but the platform runs as two services (dchub-backend
web + dchub-worker) that must agree on a set of shared-critical secrets:
the model-relations eval tick, the crawler scheduler, and most crons
execute on the WORKER, so a key updated on the backend dashboard alone
silently no-ops. This bit three separate times on 2026-07-17 (GROQ, XAI,
MOONSHOT keys each landed on one service only).

This module exposes value FINGERPRINTS (sha256 prefix — never the value)
for an allowlist of vars that must match, gated behind the internal key.
routes/brain_consistency_radar.check_env_drift compares this service's
fingerprints against the worker's over the Railway private network and
files a brain finding naming any drifted vars.
"""
import hashlib
import os
from flask import Blueprint, jsonify, request

env_drift_bp = Blueprint("env_drift", __name__)

# Vars that MUST be identical on dchub-backend and dchub-worker. Deliberately
# an allowlist — plenty of vars legitimately differ per service (DCHUB_ROLE,
# RAILWAY_*, MEMORY_GC_LIMIT_MB, pool sizing). Names only ever leave the
# service as sha256 prefixes.
SHARED_CRITICAL_VARS = (
    "DATABASE_URL", "NEON_DATABASE_URL",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MISTRAL_API_KEY",
    "PERPLEXITY_API_KEY", "GROQ_API_KEY", "XAI_API_KEY",
    "MOONSHOT_API_KEY", "GEMINI_API_KEY", "GOOGLE_AI_KEY",
    "MODELREL_KEY_OPENAI", "MODELREL_KEY_MISTRAL", "MODELREL_KEY_META",
    "MODELREL_KEY_PERPLEXITY", "MODELREL_KEY_XAI", "MODELREL_KEY_GEMINI",
    "MODELREL_KEY_MOONSHOT", "MODELREL_KEY_COHERE", "COHERE_API_KEY",
    "MODELREL_KEY_DEEPSEEK", "MODELREL_KEY_QWEN", "MODELREL_KEY_ZAI",
    "DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "SMITHERY_TOKEN",
)


def env_fingerprints() -> dict:
    out = {}
    for name in SHARED_CRITICAL_VARS:
        val = (os.environ.get(name) or "").strip()
        out[name] = hashlib.sha256(val.encode()).hexdigest()[:12] if val else None
    return out


@env_drift_bp.route("/api/v1/internal/env-fingerprint", methods=["GET"])
def env_fingerprint():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key") or "").strip()
    allowed = {k for k in ((os.environ.get("DCHUB_INTERNAL_KEY") or "").strip(),
                           (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()) if k}
    if not sent or sent not in allowed:
        return jsonify({"error": "forbidden"}), 403
    return jsonify({"role": os.environ.get("DCHUB_ROLE", "?"),
                    "fingerprints": env_fingerprints()})
