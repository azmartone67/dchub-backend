"""
Brain L12 — Site Expansion Tracker (2026-05-18).

Answers "is the site expanding?" in one curl. Counts every surface the
site exposes and tracks 7d/30d deltas so the answer is always a number,
not a feeling.

Surfaces counted:
  - Flask routes (from app.url_map)
  - Brain detectors (from brain_consistency_radar.scan_all)
  - Brain layers (count of routes/brain_layer*.py)
  - Cron jobs (from dchub-scheduler.py)
  - Media journalists in outreach list
  - Auto-publisher distribution channels
  - DB tables (to_regclass scan)

Endpoint: GET /api/v1/brain/expansion
Snapshot every 24h to brain_expansion_snapshots; deltas computed live.
"""

import os
import logging
import datetime as _dt
from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)
brain_layer12_bp = Blueprint("brain_layer12", __name__)


def _ensure_table():
    try:
        from main import get_db_conn  # type: ignore
        conn = get_db_conn()
        if not conn: return False
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brain_expansion_snapshots (
                id              SERIAL PRIMARY KEY,
                snapped_at      TIMESTAMPTZ DEFAULT NOW(),
                routes          INTEGER,
                detectors       INTEGER,
                brain_layers    INTEGER,
                cron_jobs       INTEGER,
                journalists     INTEGER,
                pub_channels    INTEGER,
                db_tables       INTEGER,
                code_lines      BIGINT,
                meta            JSONB
            )
        """)
        conn.commit()
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
        return True
    except Exception as e:
        logger.warning(f"L12 table create failed: {e}")
        return False


def _count_routes() -> int:
    try:
        from main import app  # type: ignore
        return len(list(app.url_map.iter_rules()))
    except Exception:
        return 0


def _count_detectors() -> int:
    try:
        from brain_consistency_radar import scan_all  # type: ignore
        # scan_all is the list of detectors; if it's a fn introspect __code__
        if callable(scan_all):
            import inspect
            src = inspect.getsource(scan_all)
            return src.count("check_")
        return len(scan_all)
    except Exception:
        # Fallback: grep the file
        try:
            with open("/Users/jonathanmartone/dchub-backend/brain_consistency_radar.py", "rb") as f:
                src = f.read().decode("utf-8", "ignore")
            # count `def check_...(`
            import re
            return len(re.findall(r"^def check_\w+\(", src, re.MULTILINE))
        except Exception:
            return 0


def _count_brain_layers() -> int:
    try:
        d = "/Users/jonathanmartone/dchub-backend/routes"
        return len([f for f in os.listdir(d)
                    if f.startswith("brain_layer") and f.endswith(".py")])
    except Exception:
        return 0


def _count_cron_jobs() -> int:
    try:
        with open("/Users/jonathanmartone/dchub-backend/dchub-scheduler.py", "rb") as f:
            src = f.read().decode("utf-8", "ignore")
        # Count scheduler.add_job(...) lines
        return src.count("scheduler.add_job(") or src.count(".add_job(")
    except Exception:
        return 0


def _count_journalists() -> int:
    try:
        from routes.media_outreach import JOURNALISTS  # type: ignore
        return len(JOURNALISTS)
    except Exception:
        return 0


def _count_pub_channels() -> int:
    chans = 0
    for env in ("LINKEDIN_ACCESS_TOKEN", "TWITTER_BEARER_TOKEN",
                "BLUESKY_HANDLE", "DCHUB_RESEND_API_KEY"):
        if os.environ.get(env, "").strip():
            chans += 1
    return chans


def _count_db_tables() -> int:
    try:
        from main import get_db_conn  # type: ignore
        conn = get_db_conn()
        if not conn: return 0
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public'")
        n = int((cur.fetchone() or [0])[0])
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
        return n
    except Exception:
        return 0


def _count_code_lines() -> int:
    """Total LoC across Python in the backend — proxy for surface size."""
    total = 0
    for root in ("/Users/jonathanmartone/dchub-backend",
                 "/Users/jonathanmartone/dchub-backend/routes"):
        try:
            for f in os.listdir(root):
                if not f.endswith(".py"): continue
                p = os.path.join(root, f)
                if not os.path.isfile(p): continue
                try:
                    with open(p, "rb") as fh:
                        total += fh.read().count(b"\n")
                except Exception: continue
        except Exception: continue
    return total


def _current_snapshot() -> dict:
    return {
        "routes":        _count_routes(),
        "detectors":     _count_detectors(),
        "brain_layers":  _count_brain_layers(),
        "cron_jobs":     _count_cron_jobs(),
        "journalists":   _count_journalists(),
        "pub_channels":  _count_pub_channels(),
        "db_tables":     _count_db_tables(),
        "code_lines":    _count_code_lines(),
    }


def _save_snapshot(snap: dict):
    if not _ensure_table(): return
    try:
        from main import get_db_conn  # type: ignore
        conn = get_db_conn()
        if not conn: return
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO brain_expansion_snapshots "
            "(routes, detectors, brain_layers, cron_jobs, journalists, "
            " pub_channels, db_tables, code_lines) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (snap["routes"], snap["detectors"], snap["brain_layers"],
             snap["cron_jobs"], snap["journalists"], snap["pub_channels"],
             snap["db_tables"], snap["code_lines"]),
        )
        conn.commit()
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
    except Exception as e:
        logger.warning(f"L12 save failed: {e}")


def _historical(days: int) -> dict | None:
    try:
        from main import get_db_conn  # type: ignore
        conn = get_db_conn()
        if not conn: return None
        cur = conn.cursor()
        cur.execute(
            "SELECT routes, detectors, brain_layers, cron_jobs, journalists, "
            "       pub_channels, db_tables, code_lines, snapped_at "
            "FROM brain_expansion_snapshots "
            "WHERE snapped_at < NOW() - INTERVAL %s "
            "ORDER BY snapped_at DESC LIMIT 1",
            (f"{days} days",),
        )
        row = cur.fetchone()
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
        if not row: return None
        return {"routes": row[0], "detectors": row[1], "brain_layers": row[2],
                "cron_jobs": row[3], "journalists": row[4],
                "pub_channels": row[5], "db_tables": row[6],
                "code_lines": row[7], "as_of": str(row[8])}
    except Exception:
        return None


def _delta(now: dict, then: dict | None) -> dict:
    if not then: return {}
    return {k: (now.get(k, 0) or 0) - (then.get(k, 0) or 0)
            for k in ("routes", "detectors", "brain_layers", "cron_jobs",
                       "journalists", "pub_channels", "db_tables", "code_lines")}


@brain_layer12_bp.route("/api/v1/brain/expansion", methods=["GET", "POST"])
def expansion():
    """Site expansion metrics. POST = snapshot now + return; GET = read-only."""
    snap = _current_snapshot()
    if not snap.get("routes"):
        return jsonify(ok=False, error="couldn't read app — partial snapshot",
                       snapshot=snap), 503
    from flask import request as _rq
    if _rq.method == "POST":
        _save_snapshot(snap)
    d7 = _historical(7)
    d30 = _historical(30)
    return jsonify(
        ok=True,
        as_of=_dt.datetime.utcnow().isoformat() + "Z",
        current=snap,
        delta_7d=_delta(snap, d7),
        delta_30d=_delta(snap, d30),
        baseline_7d=d7,
        baseline_30d=d30,
        verdict=("expanding" if (d7 and
                                 (snap["routes"] > d7["routes"] or
                                  snap["detectors"] > d7["detectors"] or
                                  snap["brain_layers"] > d7["brain_layers"]))
                 else "steady" if d7 else "no-baseline-yet"),
    )
