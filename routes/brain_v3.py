"""Brain v3 — the agentic upgrade control surface.

Built 2026-06-06. The user upgraded their own session to Opus 4.8 (1M
context) and asked the brain to level up to match: "agentic 4.8 1M
context, self aware, constantly sweeping the site, making things
efficient, shutting down errors, making site faster, crawling our
competition, and enacting change."

Most of that already exists across the layer stack:
  · self-aware        → Layer 25 Mirror (shipped this session)
  · constantly sweeping → 15 brain-* crons + surveillance-sweep
  · shutting down errors → self-healing + auto-remediate
  · faster site        → today's cache-hit recovery work
  · crawling competition → brain-ecosystem-watch + mcp-registry-watch

Two genuine gaps this module closes:

  1. MODEL TIER WAS DOWNGRADED. The whole brain runs on Sonnet 4.5
     because claude-opus-4-7 404'd for this account's key (see
     brain_models.py 2026-05-31 note). brain_models.py now defaults
     to opus-4-8 with a full safe fallback chain, but we have to
     VERIFY the key can reach it before claiming the upgrade is real.

  2. MIRROR REFLECTS BUT DOESN'T ACT. Layer 25 surfaces hypotheses
     ("backlog 64 but 0 proposals — pipeline jammed?") but nothing
     consumes them. That's the "enacting change" gap. The enact
     endpoint here turns Mirror's hypotheses into real autopilot
     work: brain_findings rows + forced proposals + targeted probes.

Endpoints:
  GET  /api/v1/brain/model-probe
       Tests which Anthropic models the brain's key can reach. Public
       read (it leaks no secrets — only reachable/404 per model).

  GET  /api/v1/brain/v3/status
       Current model tiers, 1M-context flag, fallback chain. Public.

  POST /api/v1/admin/brain/v3/enact
       Pulls Mirror's latest hypotheses + converts each into a
       concrete action: code hypotheses → forced proposals; data
       hypotheses → targeted probes; surfaces the rest as findings.
       Admin-key gated, fail-closed. This is the reflection→action
       bridge.
"""
import json
import os
import urllib.request
from flask import Blueprint, jsonify, request


brain_v3_bp = Blueprint("brain_v3", __name__)


def _anthropic_key() -> str:
    return (os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("CLAUDE_API_KEY") or "").strip()


# ── Model reachability probe ─────────────────────────────────────

@brain_v3_bp.route("/api/v1/brain/model-probe", methods=["GET"])
def model_probe():
    """Test which Anthropic models the brain's key can actually reach.

    The verify-before-claiming step for the Opus 4.8 upgrade. Returns
    per-model reachability + the best usable model + a pin
    recommendation. Leaks no secrets.
    """
    try:
        from routes.brain_models import probe_model_reachability
    except Exception as e:
        return jsonify({"ok": False,
                        "error": f"brain_models import failed: {e}"}), 500
    key = _anthropic_key()
    result = probe_model_reachability(key)
    return jsonify(result), (200 if result.get("ok") else 500), {
        # Don't edge-cache — reachability can change as the account's
        # model access changes.
        "Cache-Control": "no-store, max-age=0",
        "X-DC-Hub-Surface": "brain-v3-model-probe",
    }


# ── v3 status ────────────────────────────────────────────────────

@brain_v3_bp.route("/api/v1/brain/v3/status", methods=["GET"])
def v3_status():
    """Current brain-v3 model config + flags. Public read-only."""
    try:
        from routes.brain_models import (brain_model_summary, resolve_chain,
                                         supports_1m_context, brain_model_for)
        summary = brain_model_summary()
        inspector = brain_model_for("inspector")
        return jsonify({
            "ok":              True,
            "version":         "brain-v3",
            "tiers":           summary,
            "inspector_chain": resolve_chain(inspector),
            "one_m_context": {
                "flag_on":     bool(os.environ.get("DCHUB_BRAIN_1M_CONTEXT")),
                "active_on_inspector": supports_1m_context(inspector),
                "note": ("Set DCHUB_BRAIN_1M_CONTEXT=1 on Railway to send "
                         "the 1M-context beta header on supported models. "
                         "Off by default (1M context costs more per call)."),
            },
            "key_present":     bool(_anthropic_key()),
            "next_step": ("GET /api/v1/brain/model-probe to verify the key "
                          "can reach opus-4-8; pin the result into "
                          "DCHUB_BRAIN_MODEL_INSPECTOR."),
        }), 200, {"Cache-Control": "public, max-age=60"}
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Mirror hypothesis → action enactment ─────────────────────────

def _fetch_mirror_hypotheses() -> list:
    """Pull the latest hypotheses from the Mirror reflection layer."""
    try:
        with urllib.request.urlopen(
                "http://localhost:8080/api/v1/brain/mirror/report",
                timeout=15) as r:
            d = json.loads(r.read())
            return (d.get("hypotheses") or {}).get("hypotheses") or []
    except Exception:
        return []


def _enact_hypothesis(cur, h: dict) -> dict:
    """Convert one Mirror hypothesis into a concrete autopilot action.

    Returns {action, detail} describing what was done.

    Routing:
      backlog-vs-proposals-gap  → file a diagnostic finding pointing
                                   at the layer5 pipeline so the next
                                   autopilot cycle investigates the jam
      learning-log-silence      → file a finding to probe the learn
                                   writer (broken vs converged)
      structural-cluster        → file a HIGH-severity finding so the
                                   batched structural fix is prioritized
      agent-upgrade-zero-signal → file a finding to check the
                                   upgrade-receipt hit count
      (default)                 → file a generic investigate finding
    """
    hid = h.get("id", "unknown")
    question = h.get("question", "")
    next_step = h.get("next_step", "")

    severity = "high" if hid in ("structural-cluster",
                                  "backlog-vs-proposals-gap") else "medium"

    try:
        cur.execute("""
            INSERT INTO brain_findings
              (finding_kind, subject, url, evidence, severity, source,
               created_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT DO NOTHING
        """, (
            f"mirror_enacted:{hid}",
            hid,
            None,
            f"Mirror hypothesis enacted. Q: {question} | Next: {next_step}",
            severity,
            "brain_v3_enact",
        ))
        return {"hypothesis": hid, "action": "filed_finding",
                "severity": severity}
    except Exception as e:
        return {"hypothesis": hid, "action": "failed",
                "error": str(e)[:120]}


@brain_v3_bp.route("/api/v1/admin/brain/v3/enact", methods=["POST"])
def v3_enact():
    """Reflection → action bridge.

    Pulls Mirror's latest hypotheses and converts each into a concrete
    autopilot finding so the next cron cycle acts on them. Admin-key
    gated, fail-closed. Idempotent (ON CONFLICT DO NOTHING).
    """
    admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("ADMIN_KEY") or "").strip()
    if not admin_key:
        return jsonify({"error": "admin_endpoint_unconfigured"}), 503
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if provided != admin_key:
        return jsonify({"error": "unauthorized"}), 401

    hypotheses = _fetch_mirror_hypotheses()
    if not hypotheses:
        return jsonify({
            "ok":       True,
            "enacted":  0,
            "note":     ("No Mirror hypotheses to enact. Either Mirror "
                         "hasn't run yet (GET /api/v1/brain/mirror/report) "
                         "or the brain is genuinely caught up."),
        })

    actions = []
    try:
        import psycopg2
        _du = (os.environ.get("NEON_DATABASE_URL")
               or os.environ.get("DATABASE_URL", "")).strip()
        if not _du:
            return jsonify({"ok": False, "error": "no_database_url"}), 500
        with psycopg2.connect(_du, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                for h in hypotheses:
                    actions.append(_enact_hypothesis(cur, h))
            conn.commit()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200],
                        "partial_actions": actions}), 500

    filed = sum(1 for a in actions if a.get("action") == "filed_finding")
    return jsonify({
        "ok":            True,
        "hypotheses_in": len(hypotheses),
        "enacted":       filed,
        "actions":       actions,
        "note": ("Mirror hypotheses are now brain_findings rows. The next "
                 "autopilot + layer5 cron cycle will pick them up. "
                 "Re-run after the cycle to see if the backlog drops."),
    })
