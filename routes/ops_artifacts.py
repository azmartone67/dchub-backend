"""ops_artifacts — small machine-written JSON blobs, served from the API
instead of from files a bot pushes to the deployed branch.

WHY THIS EXISTS. dchub-frontend's `main` was taking ~148 direct bot pushes a
day, and that is the reason a required status check cannot be enabled there: a
required check REJECTS a push whose commit carries no check yet, so it would
block every one of them. Most of that churn moved to the repo's `bot-state`
branch on 2026-09-04. What could NOT move is the state a live dashboard fetches
from the repo, because nothing else served it:

    data/growth.json          growth/index.html, audit/index.html
    data/stats-history.json   growth/index.html
    health.json               health/index.html, audit/index.html
    qa/last-run.json          health/index.html, audit/index.html
    qa/qa-history.json        health/index.html
    qa/discovered.json        audit/index.html
    scripts/learned-skills.json      audit/index.html
    qa/anthropic-suggestions.json    audit/index.html

bot-state's own README names these as the remaining blocker. This module is the
place they move TO: the workflow that computes one POSTs it here instead of
committing it, and the dashboard GETs it instead of fetching a repo path.

★ NO NEW SECRET. The write is gated by the SAME X-Admin-Key that five
  dchub-frontend workflows already send to this backend (slug-freeze-daily,
  dcpi-recompute-missing, gem-refresh, brain-self-direct, gas-pipeline-ingest),
  so a publisher needs no credential it does not already hold.

★ AN ALLOWLIST, NOT A BUCKET. Only the names below are accepted. An open
  key-value store on an admin-gated endpoint is a place for anything to end up,
  and "anything" is what nobody audits. A new artifact is a reviewed diff here.

★ A MISSING ARTIFACT IS A 404 WITH A REASON, NEVER AN EMPTY OBJECT. `{}` parses
  fine and renders as zeroes, which is how a dashboard shows a confident 0 for
  data it never received. The 404 names the workflow that should have published
  it, so the failure points at its own cause.
"""
import json
import logging
import os

from flask import Blueprint, Response, jsonify, request

from internal_auth import is_valid_internal_key

log = logging.getLogger("ops_artifacts")
ops_artifacts_bp = Blueprint("ops_artifacts", __name__)

_get_db = None

# name -> (what it is, which workflow publishes it)
ARTIFACTS = {
    "growth":               ("data/growth.json",             "stats-snapshot.yml"),
    "stats-history":        ("data/stats-history.json",      "stats-snapshot.yml"),
    "health":               ("health.json",                  "qa-brain.yml"),
    "qa-last-run":          ("qa/last-run.json",             "qa-brain.yml"),
    "qa-history":           ("qa/qa-history.json",           "qa-brain.yml"),
    "qa-discovered":        ("qa/discovered.json",           "qa-brain.yml"),
    "learned-skills":       ("scripts/learned-skills.json",  "qa-evolve.yml"),
    "anthropic-suggestions": ("qa/anthropic-suggestions.json", "qa-evolve.yml"),
}

# The largest of these on 2026-09-05 is qa/qa-history.json at ~180 KB. The cap
# is deliberately close to that rather than generous: this is dashboard state,
# and anything materially bigger is a different kind of thing that should get
# its own endpoint and its own review.
MAX_BYTES = 512 * 1024

_DDL = """
CREATE TABLE IF NOT EXISTS ops_artifacts (
    name       TEXT PRIMARY KEY,
    body       JSONB       NOT NULL,
    bytes      INTEGER     NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def register_ops_artifacts(app, get_db_func):
    global _get_db
    _get_db = get_db_func
    app.register_blueprint(ops_artifacts_bp)
    try:
        conn = _get_db()
        cur = conn.cursor()
        cur.execute(_DDL)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:                      # a boot must not die on this
        log.warning("ops_artifacts: table ensure failed: %s", e)
    log.info("✅ ops_artifacts registered: /api/v1/ops/artifact/<name> (%d names)",
             len(ARTIFACTS))


def _admin_ok():
    """Same gate five frontend workflows already pass to reach this backend."""
    sent = (request.headers.get("X-Admin-Key")
            or request.headers.get("X-Internal-Key") or "").strip()
    if not sent:
        return False
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("ADMIN_KEY") or "").strip()
    if expected and sent == expected:
        return True
    return is_valid_internal_key(sent)


@ops_artifacts_bp.route("/api/v1/ops/artifact/<name>",
                        methods=["GET", "POST", "PUT"])
def artifact(name):
    """One path, both directions.

    ★ Deliberately ONE decorator rather than a GET one and a POST one.
    regression_lint.py's `duplicate-route` check reads the same path declared
    twice as a duplicate regardless of method, and it is right to: two
    decorators on one path are two places a reader has to find before they know
    what the URL does.
    """
    if request.method in ("POST", "PUT"):
        return _put_artifact(name)
    return _get_artifact(name)


def _get_artifact(name):
    if name not in ARTIFACTS:
        return jsonify({
            "error": "unknown_artifact",
            "name": name,
            "known": sorted(ARTIFACTS),
        }), 404
    was, publisher = ARTIFACTS[name]
    try:
        conn = _get_db()
        cur = conn.cursor()
        cur.execute("SELECT body, bytes, updated_at FROM ops_artifacts WHERE name = %s",
                    (name,))
        row = cur.fetchone()
        cur.close()
        conn.close()
    except Exception as e:
        log.warning("ops_artifacts GET %s: %s", name, e)
        return jsonify({"error": "store_unavailable", "name": name}), 503

    if not row:
        # NOT an empty object — see the module docstring.
        return jsonify({
            "error": "not_published_yet",
            "name": name,
            "was": was,
            "publisher": publisher,
            "detail": f"{publisher} has not POSTed '{name}' yet. This is the "
                      f"absence of data, not a value.",
        }), 404

    body, nbytes, updated_at = row
    resp = Response(json.dumps(body), mimetype="application/json")
    resp.headers["X-Artifact-Updated-At"] = updated_at.isoformat()
    resp.headers["X-Artifact-Bytes"] = str(nbytes)
    resp.headers["X-Artifact-Was"] = was
    # Dashboard state. Short enough that a page is never minutes stale, long
    # enough that a reload storm does not become a query storm.
    resp.headers["Cache-Control"] = "public, max-age=60, s-maxage=120"
    resp.headers["Access-Control-Expose-Headers"] = (
        "X-Artifact-Updated-At, X-Artifact-Bytes, X-Artifact-Was")
    return resp


def _put_artifact(name):
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    if name not in ARTIFACTS:
        return jsonify({
            "error": "unknown_artifact",
            "name": name,
            "known": sorted(ARTIFACTS),
            "detail": "Names are an allowlist. Add it in routes/ops_artifacts.py "
                      "so the addition is a reviewed diff.",
        }), 400

    raw = request.get_data(cache=False) or b""
    if len(raw) > MAX_BYTES:
        return jsonify({"error": "too_large", "bytes": len(raw),
                        "max_bytes": MAX_BYTES}), 413
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception as e:
        return jsonify({"error": "invalid_json", "detail": str(e)[:200]}), 400
    if not isinstance(body, (dict, list)):
        return jsonify({"error": "invalid_shape",
                        "detail": "top level must be an object or an array"}), 400

    try:
        conn = _get_db()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO ops_artifacts (name, body, bytes, updated_at)
               VALUES (%s, %s::jsonb, %s, NOW())
               ON CONFLICT (name) DO UPDATE
                 SET body = EXCLUDED.body,
                     bytes = EXCLUDED.bytes,
                     updated_at = NOW()""",
            (name, json.dumps(body), len(raw)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        log.warning("ops_artifacts POST %s: %s", name, e)
        return jsonify({"error": "store_write_failed", "name": name}), 503

    return jsonify({"ok": True, "name": name, "bytes": len(raw)}), 200


@ops_artifacts_bp.route("/api/v1/ops/artifacts", methods=["GET"])
def list_artifacts():
    """What exists, when it was last written, and what has never been written.

    Published separately from the artifacts themselves so "nobody has POSTed
    health since 03:00" is observable without polling eight endpoints.
    """
    seen = {}
    try:
        conn = _get_db()
        cur = conn.cursor()
        cur.execute("SELECT name, bytes, updated_at FROM ops_artifacts")
        for n, b, u in cur.fetchall():
            seen[n] = {"bytes": b, "updated_at": u.isoformat()}
        cur.close()
        conn.close()
    except Exception as e:
        log.warning("ops_artifacts list: %s", e)
        return jsonify({"error": "store_unavailable"}), 503

    out = []
    for n in sorted(ARTIFACTS):
        was, publisher = ARTIFACTS[n]
        out.append({"name": n, "was": was, "publisher": publisher,
                    "published": n in seen, **seen.get(n, {})})
    return jsonify({
        "artifacts": out,
        "count": len(out),
        "published": sum(1 for a in out if a["published"]),
        "max_bytes": MAX_BYTES,
    })
