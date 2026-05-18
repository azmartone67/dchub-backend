"""Phase KKK (2026-05-17) — public install-count tracker.

Round 11 shipped dchub-mcp-helper to PyPI. Round 10 shipped
@dchub/mcp-helper source for npm. Both have live download counts
exposed via their respective APIs. This module pulls those counts on
a daily cron, caches them in package_install_stats, and exposes a
public widget endpoint for the homepage credibility signal.

Endpoints:
  GET  /api/v1/packages/stats           — latest stats (cached 1h)
  POST /api/v1/packages/refresh          — admin cron entry
  GET  /api/v1/packages/history          — last 30 days of daily counts

Data sources:
  PyPI:  https://pypistats.org/api/packages/dchub-mcp-helper/recent
         (free, no key, daily granularity)
  npm:   https://api.npmjs.org/downloads/point/last-day/@dchub/mcp-helper
         (free, no key, daily/week/month granularity)

If a package isn't yet published (npm), the source is gracefully
skipped + the stat shows as 0. Resolves to a real number once
publishing happens.
"""
from __future__ import annotations
import os
import datetime
from flask import Blueprint, jsonify, request


package_stats_bp = Blueprint("package_stats", __name__)


_PACKAGES = [
    {"ecosystem": "pypi",  "name": "dchub-mcp-helper"},
    {"ecosystem": "npm",   "name": "@dchub/mcp-helper"},
    # ↓ add more here as DC Hub ships more clients
]


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS package_install_stats (
    id              BIGSERIAL PRIMARY KEY,
    ecosystem       TEXT NOT NULL,            -- 'pypi' | 'npm'
    package_name    TEXT NOT NULL,
    snapshot_date   DATE NOT NULL DEFAULT CURRENT_DATE,
    downloads_today INT,                       -- yesterday's daily count
    downloads_7d    INT,
    downloads_30d   INT,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_package_install_day
    ON package_install_stats(ecosystem, package_name, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_package_install_recent
    ON package_install_stats(ecosystem, package_name, snapshot_date DESC);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


def _fetch_pypi(name: str) -> dict:
    """Returns {downloads_today, downloads_7d, downloads_30d} from pypistats,
    with a fallback to PyPI's own JSON API so brand-new packages register
    as 'present' (downloads_today=0) before pypistats catches up (24-48h
    indexing lag for new packages).

    Never raises. Best-effort."""
    try:
        import requests as _req

        # Primary: pypistats.org (has real download counts but 24-48h lag
        # for brand-new packages)
        try:
            r = _req.get(
                f"https://pypistats.org/api/packages/{name}/recent",
                headers={"User-Agent": "dchub-package-stats/1.0"},
                timeout=10,
            )
            if r.status_code == 200:
                d = (r.json() or {}).get("data") or {}
                if d:
                    return {
                        "downloads_today": d.get("last_day"),
                        "downloads_7d":    d.get("last_week"),
                        "downloads_30d":   d.get("last_month"),
                    }
        except Exception:
            pass

        # Fallback: confirm the package EXISTS on PyPI via its own JSON
        # API. Returns 0s for downloads but lets the homepage widget
        # show 'Available on PyPI' instead of staying hidden.
        try:
            r = _req.get(
                f"https://pypi.org/pypi/{name}/json",
                headers={"User-Agent": "dchub-package-stats/1.0"},
                timeout=8,
            )
            if r.status_code == 200:
                return {
                    "downloads_today": 0,
                    "downloads_7d":    0,
                    "downloads_30d":   0,
                }
        except Exception:
            pass
    except Exception:
        pass
    return {}


def _fetch_npm(name: str) -> dict:
    """Returns {downloads_today, downloads_7d, downloads_30d} from npmjs api."""
    try:
        import requests as _req
        out = {}
        for window, key in [("last-day", "downloads_today"),
                             ("last-week", "downloads_7d"),
                             ("last-month", "downloads_30d")]:
            r = _req.get(
                f"https://api.npmjs.org/downloads/point/{window}/{name}",
                timeout=10,
            )
            if r.status_code != 200:
                continue
            out[key] = (r.json() or {}).get("downloads")
        return out
    except Exception:
        return {}


def _refresh_all() -> dict:
    """Pull live download counts from PyPI + npm, INSERT today's snapshot."""
    out = {"refreshed_at": datetime.datetime.utcnow().isoformat() + "Z",
           "packages": [], "errors": []}
    c = _conn()
    if c is None:
        out["errors"].append("no_database"); return out
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            for p in _PACKAGES:
                eco = p["ecosystem"]; name = p["name"]
                if eco == "pypi":
                    stats = _fetch_pypi(name)
                elif eco == "npm":
                    stats = _fetch_npm(name)
                else:
                    continue
                if not stats:
                    out["errors"].append({"ecosystem": eco, "name": name,
                                          "err": "fetch_failed_or_404"})
                    continue
                try:
                    cur.execute("""
                        INSERT INTO package_install_stats
                            (ecosystem, package_name, snapshot_date,
                             downloads_today, downloads_7d, downloads_30d)
                        VALUES (%s, %s, CURRENT_DATE, %s, %s, %s)
                        ON CONFLICT (ecosystem, package_name, snapshot_date)
                        DO UPDATE SET
                            downloads_today = EXCLUDED.downloads_today,
                            downloads_7d    = EXCLUDED.downloads_7d,
                            downloads_30d   = EXCLUDED.downloads_30d,
                            captured_at     = NOW()
                    """, (eco, name,
                          stats.get("downloads_today"),
                          stats.get("downloads_7d"),
                          stats.get("downloads_30d")))
                    out["packages"].append({"ecosystem": eco, "name": name, **stats})
                except Exception as e:
                    out["errors"].append({"ecosystem": eco, "name": name,
                                          "err": str(e)[:80]})
    finally:
        try: c.close()
        except Exception: pass
    return out


@package_stats_bp.route("/api/v1/packages/stats", methods=["GET"])
def stats():
    """Public: latest snapshot per package. Cache 1h."""
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    out = {"as_of": datetime.datetime.utcnow().isoformat() + "Z",
           "packages": [], "totals": {"installs_today": 0, "installs_7d": 0,
                                       "installs_30d": 0}}
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT ON (ecosystem, package_name)
                       ecosystem, package_name, snapshot_date,
                       downloads_today, downloads_7d, downloads_30d,
                       captured_at
                  FROM package_install_stats
                 ORDER BY ecosystem, package_name, snapshot_date DESC
            """)
            rows = cur.fetchall() or []
        for r in rows:
            out["packages"].append({
                "ecosystem":       r["ecosystem"],
                "name":            r["package_name"],
                "snapshot_date":   r["snapshot_date"].isoformat() if r["snapshot_date"] else None,
                "downloads_today": r["downloads_today"],
                "downloads_7d":    r["downloads_7d"],
                "downloads_30d":   r["downloads_30d"],
                "install_url": (
                    f"https://pypi.org/project/{r['package_name']}/"
                    if r['ecosystem'] == 'pypi'
                    else f"https://www.npmjs.com/package/{r['package_name']}"
                ),
            })
            out["totals"]["installs_today"] += (r["downloads_today"] or 0)
            out["totals"]["installs_7d"]    += (r["downloads_7d"] or 0)
            out["totals"]["installs_30d"]   += (r["downloads_30d"] or 0)
    finally:
        try: c.close()
        except Exception: pass
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@package_stats_bp.route("/api/v1/packages/refresh", methods=["POST"])
def refresh():
    """Admin cron entry — pull fresh counts from PyPI + npm."""
    admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("ADMIN_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if admin_key and provided != admin_key:
        return jsonify(error="unauthorized"), 401
    return jsonify(_refresh_all()), 200


@package_stats_bp.route("/api/v1/packages/history", methods=["GET"])
def history():
    """Last 30 days of daily snapshots per package."""
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT ecosystem, package_name, snapshot_date,
                       downloads_today, downloads_7d
                  FROM package_install_stats
                 WHERE snapshot_date >= CURRENT_DATE - INTERVAL '30 days'
                 ORDER BY snapshot_date DESC, ecosystem, package_name
            """)
            rows = cur.fetchall() or []
        for r in rows:
            if r.get("snapshot_date"):
                r["snapshot_date"] = r["snapshot_date"].isoformat()
        resp = jsonify(history=rows, count=len(rows))
        resp.headers["Cache-Control"] = "public, max-age=3600"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 200
    finally:
        try: c.close()
        except Exception: pass
