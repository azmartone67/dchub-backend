"""
Brain L11 — QA Agent (2026-05-18). ★ RETIRED 2026-09-12.

/api/v1/brain/qa-agent answers 410 on GET and POST. Live QA is fast-QA
(routes/brain_fast_qa.py, every ~30 min), alongside site-qa, qa-superuser and
dchub-qa.

What this module used to do: probe every public surface on a 6h cron and record
status, TTFB, dynamic-vs-static classification and regressions into
brain_qa_probes. That sweep was DISABLED 2026-05-19 (container crash-loop) and
never re-enabled, but GET went on serving the May snapshot as current surface
health for 116 days.

★ 2026-09-15 — the sweep machinery is DELETED rather than kept as history.
Nothing called it once the route returned 410: _probe(), _record(),
_ensure_table(), _previous_run_summary(), _regressions(), PROBE_PATHS and
_DYNAMIC_HINTS had no caller in the repo. _probe() also read only the first
64KB of each response and then made ABSENCE claims from that truncated read —
no hint found in 64KB was published as classification "static" for the whole
page, and a body that did not parse in 64KB as dynamic_signal "non-json". It
published nothing, because it was unreachable; it is deleted so it cannot be
wired back up.

Still live here:
  GET  /api/v1/brain/qa-agent                    -> 410, retired_payload()
  POST /api/v1/brain/qa-agent                    -> 410 (sweep cannot be re-armed)
  GET  /api/v1/brain/qa-agent/history?path=<p>   -> timestamped rows

brain_qa_probes (id, probed_at, path, status, ttfb_ms, total_ms, bytes,
classification, dynamic_signal, error) is now read-only from this codebase:
nothing here writes it and nothing here creates it. /history serves the rows the
sweep left behind, and every row carries its own probed_at, so what it returns
cannot pass for current health.
"""

from flask import Blueprint, jsonify, request

brain_layer11_bp = Blueprint("brain_layer11", __name__)

RETIRED_AT = "2026-09-12"
LAST_SWEEP_DISABLED = "2026-05-19"


def retired_payload() -> dict:
    """What /api/v1/brain/qa-agent answers now that L11 is retired."""
    return {
        "ok": False,
        "retired": True,
        "retired_at": RETIRED_AT,
        "sweep_disabled_since": LAST_SWEEP_DISABLED,
        "reason": ("L11's surface sweep was disabled on 2026-05-19 after a "
                   "container crash-loop and never re-enabled. This endpoint "
                   "went on serving that May sweep as current surface health, "
                   "so it no longer reports surface health at all."),
        "superseded_by": {
            "live_qa": "fast-QA: routes/brain_fast_qa.py, swept every ~30 min",
            "durable_output": "brain_findings rows with detector = 'fast_qa'",
            "history": "/api/v1/brain/qa-agent/history?path=<path> (timestamped rows)",
        },
    }


@brain_layer11_bp.route("/api/v1/brain/qa-agent", methods=["GET", "POST"])
def qa_agent():
    """RETIRED 2026-09-12 — GET and POST both answer 410. See retired_payload()."""
    # ★ The sweep was DISABLED 2026-05-19 (container crash-loop) and never
    # re-enabled, but GET kept serving that sweep as current surface health for
    # 116 days: brain_self_test passed on it, and L8 and L14 fed its 404s into
    # their prompts as live state. POST is refused too, so the crash-looping
    # sweep cannot be re-armed by hitting this endpoint. /history below is left
    # as-is — every row it returns carries its own probed_at.
    return jsonify(retired_payload()), 410


@brain_layer11_bp.route("/api/v1/brain/qa-agent/history", methods=["GET"])
def qa_history():
    """Per-path history — useful to spot flapping endpoints."""
    path = (request.args.get("path") or "").strip()
    if not path:
        return jsonify(ok=False, error="missing path param"), 400
    conn = None
    try:
        from main import get_db  # type: ignore
        conn = get_db()
        if not conn:
            return jsonify(ok=False, error="db unavailable"), 503
        cur = conn.cursor()
        cur.execute(
            "SELECT probed_at, status, total_ms, classification, error "
            "FROM brain_qa_probes WHERE path = %s "
            "ORDER BY probed_at DESC LIMIT 30",
            (path,),
        )
        history = [{"at": str(r[0]), "status": r[1], "total_ms": r[2],
                    "classification": r[3], "error": r[4]}
                   for r in cur.fetchall()]
        try: cur.close()
        except Exception: pass
        return jsonify(ok=True, path=path, count=len(history), history=history)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 503
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass
