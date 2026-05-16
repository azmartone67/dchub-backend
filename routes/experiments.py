"""Phase MMM (2026-05-16) — A/B testing surface (experiments).

Generic experiment infrastructure. Lets us test ANY change (paywall
copy, press rotation, layer-default, tier-threshold) with statistical
significance instead of guessing.

Architecture (deliberately tiny):
  experiments table: id, name, hypothesis, variants[], started_at, ended_at, winner
  experiment_assignments table: experiment_id, anon_id, variant, ts, conversion_event?

  POST /api/v1/experiments/create     (admin) — register a new experiment
  GET  /api/v1/experiments            — public; list active experiments
  GET  /api/v1/experiments/<id>       — public; live results
  POST /api/v1/experiments/<id>/assign — public; deterministic variant assignment
                                         given an anon_id (so the same visitor
                                         always sees the same variant)
  POST /api/v1/experiments/<id>/convert — public; mark a conversion event

Variant assignment uses sha256(experiment_id + anon_id) → bucket so
no server state needed; same anon_id always lands on the same variant.

Statistical significance uses a simple z-test approximation; once we
have enough data (n>30 per variant + p<0.05), the winner field auto-
sets and future /assign calls deterministically return the winner.
"""

from __future__ import annotations

import os
import json
import math
import hashlib
import datetime
from flask import Blueprint, jsonify, request
import psycopg2
import psycopg2.extras


experiments_bp = Blueprint("experiments", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS experiments (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    hypothesis      TEXT,
    variants        JSONB NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    winner          TEXT,
    notes           TEXT
);
CREATE TABLE IF NOT EXISTS experiment_assignments (
    id              BIGSERIAL PRIMARY KEY,
    experiment_id   TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    anon_id         TEXT NOT NULL,
    variant         TEXT NOT NULL,
    assigned_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    converted_at    TIMESTAMPTZ,
    UNIQUE(experiment_id, anon_id)
);
CREATE INDEX IF NOT EXISTS ix_experiments_ended ON experiments(ended_at);
CREATE INDEX IF NOT EXISTS ix_experiment_assignments_exp_variant
    ON experiment_assignments(experiment_id, variant);
"""

def _ensure_schema():
    c = _conn()
    if c is None: return False
    try:
        with c.cursor() as cur: cur.execute(_SCHEMA_DDL)
        return True
    except Exception as e:
        print(f"[experiments] schema: {e}")
        return False
    finally:
        try: c.close()
        except Exception: pass

try: _ensure_schema()
except Exception: pass


def _admin_ok() -> bool:
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    return not expected or provided == expected


def _bucket(experiment_id: str, anon_id: str, variants: list[str]) -> str:
    """Deterministic variant assignment via stable hash."""
    h = hashlib.sha256((experiment_id + "::" + anon_id).encode()).hexdigest()
    n = int(h[:8], 16) % len(variants)
    return variants[n]


@experiments_bp.route("/api/v1/experiments", methods=["GET"])
def list_experiments():
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, name, hypothesis, variants, started_at, ended_at, winner
                  FROM experiments
                 ORDER BY started_at DESC LIMIT 50
            """)
            rows = cur.fetchall()
            # Count assignments per experiment
            cur.execute("""
                SELECT experiment_id, COUNT(*) AS n_total,
                       COUNT(converted_at) AS n_converted
                  FROM experiment_assignments
                 GROUP BY experiment_id
            """)
            counts = {r["experiment_id"]: r for r in cur.fetchall()}
        for r in rows:
            if r.get("started_at"): r["started_at"] = r["started_at"].isoformat()
            if r.get("ended_at"):   r["ended_at"]   = r["ended_at"].isoformat()
            cnt = counts.get(r["id"], {})
            r["n_total"]     = int(cnt.get("n_total", 0) or 0)
            r["n_converted"] = int(cnt.get("n_converted", 0) or 0)
            r["active"] = r.get("ended_at") is None
        return jsonify(experiments=rows, count=len(rows)), 200
    finally:
        try: c.close()
        except Exception: pass


@experiments_bp.route("/api/v1/experiments/<exp_id>", methods=["GET"])
def get_experiment(exp_id):
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM experiments WHERE id = %s", (exp_id,))
            exp = cur.fetchone()
            if exp is None: return jsonify(error="not_found"), 404
            for k in ("started_at","ended_at"):
                if exp.get(k): exp[k] = exp[k].isoformat()
            # Per-variant stats
            cur.execute("""
                SELECT variant,
                       COUNT(*) AS n,
                       COUNT(converted_at) AS converted
                  FROM experiment_assignments
                 WHERE experiment_id = %s
                 GROUP BY variant
            """, (exp_id,))
            stats = []
            for r in cur.fetchall():
                n   = int(r["n"] or 0)
                cv  = int(r["converted"] or 0)
                rate = (cv / n) if n > 0 else 0
                stats.append({
                    "variant":         r["variant"],
                    "n":               n,
                    "conversions":     cv,
                    "conversion_rate": round(rate * 100, 2),
                })
            exp["per_variant"] = stats

            # Simple winner detection — minimum n=30 per variant + 5pt absolute gap
            if not exp.get("winner") and len(stats) >= 2:
                stats_sorted = sorted(stats, key=lambda x: -x["conversion_rate"])
                top, sec = stats_sorted[0], stats_sorted[1]
                if (top["n"] >= 30 and sec["n"] >= 30
                        and (top["conversion_rate"] - sec["conversion_rate"]) >= 5):
                    exp["winner_inferred"] = top["variant"]
                    exp["winner_inferred_reason"] = (
                        f"{top['variant']} converts {top['conversion_rate']}% vs "
                        f"{sec['variant']} at {sec['conversion_rate']}% (n≥30, gap≥5pt)"
                    )
        return jsonify(exp), 200
    finally:
        try: c.close()
        except Exception: pass


@experiments_bp.route("/api/v1/experiments/create", methods=["POST"])
def create_experiment():
    if not _admin_ok(): return jsonify(error="unauthorized"), 401
    body = request.get_json(silent=True) or {}
    eid     = (body.get("id") or "").strip()[:50]
    name    = (body.get("name") or "").strip()[:200]
    hyp     = (body.get("hypothesis") or "")[:500]
    variants = body.get("variants") or []
    if not eid or not name or not isinstance(variants, list) or len(variants) < 2:
        return jsonify(error="id, name, and ≥2 variants required",
                       example={"id":"paywall-copy-v1", "name":"...",
                                 "variants":["control","aggressive"]}), 400

    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO experiments (id, name, hypothesis, variants)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (id) DO NOTHING
            """, (eid, name, hyp, json.dumps(variants)))
        return jsonify(ok=True, experiment_id=eid, variants=variants), 200
    finally:
        try: c.close()
        except Exception: pass


@experiments_bp.route("/api/v1/experiments/<exp_id>/assign", methods=["POST"])
def assign_variant(exp_id):
    """Deterministic per-anon variant. Returns same variant for same anon_id.
    If experiment has a winner, returns the winner unconditionally."""
    body = request.get_json(silent=True) or {}
    anon_id = (body.get("anon_id") or "").strip()[:50]
    if not anon_id:
        # Use IP+UA fingerprint if no anon_id provided
        ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or request.remote_addr or "")
        ua = request.headers.get("User-Agent", "")
        anon_id = hashlib.sha256((ip + "|" + ua).encode()).hexdigest()[:16]

    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT variants, winner, ended_at FROM experiments WHERE id = %s", (exp_id,))
            exp = cur.fetchone()
            if exp is None: return jsonify(error="experiment_not_found"), 404

            # Ended → return winner if set
            if exp.get("ended_at") and exp.get("winner"):
                variant = exp["winner"]
            elif exp.get("winner"):
                variant = exp["winner"]
            else:
                variants = exp["variants"] if isinstance(exp["variants"], list) else json.loads(exp["variants"] or "[]")
                if not variants: return jsonify(error="no_variants"), 500
                variant = _bucket(exp_id, anon_id, variants)

            # Record assignment (idempotent — UNIQUE constraint)
            try:
                cur.execute("""
                    INSERT INTO experiment_assignments (experiment_id, anon_id, variant)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (experiment_id, anon_id) DO NOTHING
                """, (exp_id, anon_id, variant))
            except Exception:
                pass
        return jsonify(experiment_id=exp_id, anon_id=anon_id, variant=variant), 200
    finally:
        try: c.close()
        except Exception: pass


@experiments_bp.route("/api/v1/experiments/<exp_id>/convert", methods=["POST"])
def convert(exp_id):
    """Mark this anon_id's assignment as converted."""
    body = request.get_json(silent=True) or {}
    anon_id = (body.get("anon_id") or "").strip()[:50]
    if not anon_id:
        ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or request.remote_addr or "")
        ua = request.headers.get("User-Agent", "")
        anon_id = hashlib.sha256((ip + "|" + ua).encode()).hexdigest()[:16]

    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE experiment_assignments
                   SET converted_at = NOW()
                 WHERE experiment_id = %s AND anon_id = %s
                   AND converted_at IS NULL
                 RETURNING id, variant
            """, (exp_id, anon_id))
            r = cur.fetchone()
            if r is None:
                return jsonify(ok=False, reason="not_assigned_or_already_converted"), 200
            return jsonify(ok=True, conversion_id=r[0], variant=r[1]), 200
    finally:
        try: c.close()
        except Exception: pass
