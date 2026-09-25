"""oauth_store.py — durable backend store for the MCP OAuth 2.1 AS (Phase 2 / P2).

The gateway's OAuth Authorization Server (dchub-mcp-server/oauth.mjs) keeps its
clients / authorization-codes / access-tokens in-memory, which die on every
gateway redeploy → an armed OAuth flow would mass-log-out on each deploy. This
is the durable layer: ONE table, THREE internal-only endpoints (store / fetch /
consume). The gateway write-throughs here and read-throughs on a cache miss;
single-use authorization codes are consumed ATOMICALLY (DELETE ... RETURNING)
so a code can't be replayed even across gateway replicas.

INTERNAL-ONLY: every endpoint requires the gateway↔backend X-Internal-Key
(same trust callAPIWrite already uses). No user-facing surface. Nothing calls
these until DCHUB_OAUTH_ENABLED is armed on the gateway (P3), so this is inert
in practice today — shipping it now just lays the durable layer.
"""
from __future__ import annotations

import os
import json
import logging

from flask import Blueprint, request, jsonify
import psycopg2
from routes._swallowed_writes import note_swallowed_write

logger = logging.getLogger(__name__)
oauth_store_bp = Blueprint("oauth_store", __name__)

_KINDS = ("client", "code", "token")

_DDL = """
CREATE TABLE IF NOT EXISTS oauth_store (
    kind        TEXT NOT NULL,
    store_key   TEXT NOT NULL,
    data        JSONB NOT NULL,
    expires_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, store_key)
);
CREATE INDEX IF NOT EXISTS oauth_store_expiry_idx ON oauth_store (expires_at);
"""


def _db():
    url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not url:
        return None
    try:
        return psycopg2.connect(url, sslmode="require", connect_timeout=5)
    except Exception:
        return None


def _authorized() -> bool:
    """Gateway↔backend internal trust only (X-Internal-Key / X-Admin-Key),
    mirroring routes/mcp_registry_outreach.py::_admin_authorized."""
    provided = (request.headers.get("X-Internal-Key")
                or request.headers.get("X-Admin-Key") or "")
    if not provided:
        return False
    try:
        from internal_auth import is_valid_internal_key
        if is_valid_internal_key(provided):
            return True
    except Exception:
        pass
    expected = (os.environ.get("DCHUB_INTERNAL_KEY")
                or os.environ.get("DCHUB_ADMIN_KEY"))
    return bool(expected) and provided == expected


def _cleanup(cur):
    """Opportunistic expiry sweep — cheap, bounded by the expiry index."""
    try:
        cur.execute("DELETE FROM oauth_store WHERE expires_at IS NOT NULL AND expires_at < now()")
    except Exception:
        note_swallowed_write("oauth_store", where="oauth_store._cleanup")
        pass


@oauth_store_bp.route("/api/v1/oauth/store", methods=["POST"])
def oauth_store_put():
    if not _authorized():
        return jsonify(error="unauthorized"), 401
    d = request.get_json(silent=True) or {}
    kind = (d.get("kind") or "").strip().lower()
    key = (d.get("key") or "").strip()
    data = d.get("data")
    if kind not in _KINDS or not key or not isinstance(data, dict):
        return jsonify(error="invalid_request"), 400
    try:
        ttl = int(d.get("ttl_s") or 0)
    except (ValueError, TypeError):
        ttl = 0
    c = _db()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute(_DDL)
            _cleanup(cur)
            cur.execute(
                """
                INSERT INTO oauth_store (kind, store_key, data, expires_at)
                VALUES (%s, %s, %s::jsonb,
                        CASE WHEN %s > 0 THEN now() ON CONFLICT DO NOTHING + (%s || ' seconds')::interval ELSE NULL END)
                ON CONFLICT (kind, store_key) DO UPDATE
                    SET data = EXCLUDED.data, expires_at = EXCLUDED.expires_at
                """,
                (kind, key, json.dumps(data), ttl, ttl),
            )
        c.commit()
        return jsonify(ok=True), 200
    except Exception as e:
        logger.warning("oauth_store put failed: %s", e)
        return jsonify(error="store_failed"), 500
    finally:
        try:
            c.close()
        except Exception:
            pass


@oauth_store_bp.route("/api/v1/oauth/fetch", methods=["GET"])
def oauth_store_fetch():
    if not _authorized():
        return jsonify(error="unauthorized"), 401
    kind = (request.args.get("kind") or "").strip().lower()
    key = (request.args.get("key") or "").strip()
    if kind not in _KINDS or not key:
        return jsonify(error="invalid_request"), 400
    c = _db()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute(_DDL)
            cur.execute(
                """
                SELECT data FROM oauth_store
                 WHERE kind = %s AND store_key = %s
                   AND (expires_at IS NULL OR expires_at > now())
                """,
                (kind, key),
            )
            row = cur.fetchone()
        return jsonify(data=(row[0] if row else None)), 200
    except Exception as e:
        logger.warning("oauth_store fetch failed: %s", e)
        return jsonify(error="fetch_failed"), 500
    finally:
        try:
            c.close()
        except Exception:
            pass


@oauth_store_bp.route("/api/v1/oauth/consume", methods=["POST"])
def oauth_store_consume():
    """Atomic single-use: delete-and-return the row only if not expired. Used
    for authorization codes so a code can't be replayed even across replicas."""
    if not _authorized():
        return jsonify(error="unauthorized"), 401
    d = request.get_json(silent=True) or {}
    kind = (d.get("kind") or "").strip().lower()
    key = (d.get("key") or "").strip()
    if kind not in _KINDS or not key:
        return jsonify(error="invalid_request"), 400
    c = _db()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute(_DDL)
            cur.execute(
                """
                DELETE FROM oauth_store
                 WHERE kind = %s AND store_key = %s
                   AND (expires_at IS NULL OR expires_at > now())
                RETURNING data
                """,
                (kind, key),
            )
            row = cur.fetchone()
        c.commit()
        return jsonify(data=(row[0] if row else None)), 200
    except Exception as e:
        logger.warning("oauth_store consume failed: %s", e)
        return jsonify(error="consume_failed"), 500
    finally:
        try:
            c.close()
        except Exception:
            pass
