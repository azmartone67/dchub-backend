"""Phase 114 — /lab. Experiments incubator. Cookie-gated to admin + opt-in
testers. Each experiment has a hypothesis, signal, and graduation decision.

  GET  /lab               dashboard (cookie-gated)
  GET  /api/v1/lab/list   list experiments
  POST /api/v1/lab/create create new experiment
  POST /api/v1/lab/score/<id>  log a signal sample
  POST /api/v1/lab/decide/<id> graduate or kill
"""
import os, json, datetime
from flask import Blueprint, request, jsonify, render_template_string, make_response
import psycopg2, psycopg2.extras

lab_bp = Blueprint("lab", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    return psycopg2.connect(db, sslmode="require")


def _ensure():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lab_experiments (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                hypothesis TEXT,
                started_at TIMESTAMPTZ DEFAULT NOW(),
                status TEXT DEFAULT 'incubating',  -- incubating | graduated | killed
                signal_data JSONB,
                decision_at TIMESTAMPTZ,
                decision_note TEXT
            )""")
        c.commit()


def _is_authorized():
    admin = os.environ.get("DCHUB_ADMIN_KEY")
    return (
        request.headers.get("X-Admin-Key") == admin
        or request.cookies.get("dchub_lab_token") == admin
        or request.args.get("admin_key") == admin
    )


@lab_bp.route("/lab", methods=["GET"])
def lab_dashboard():
    if not _is_authorized():
        return make_response("<h1>/lab is private</h1>"
                             "<p>Set the X-Admin-Key header or paste ?admin_key=&lt;key&gt; once "
                             "to install the cookie for this browser.</p>", 403)
    _ensure()
    # If admin_key passed, set the cookie
    resp_html_buffer = []
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM lab_experiments ORDER BY started_at DESC LIMIT 50")
        rows = cur.fetchall()
    HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>DC Hub · Lab</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=JetBrains+Mono&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a12;--card:#11121a;--bd:#1f2030;--tx:#fff;--tx2:#9ca3af;--green:#10b981;--red:#ef4444;--orange:#f59e0b;}
body{font-family:Inter,system-ui;background:var(--bg);color:var(--tx);margin:0;padding:2rem 1.5rem;line-height:1.55}
h1{font-size:2rem;margin:0 0 0.4rem;font-weight:800}.flask{margin-right:0.5rem}.sub{color:var(--tx2);margin:0 0 2rem}
.exp{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:1.1rem;margin-bottom:1rem}
.exp h3{margin:0 0 0.3rem;font-weight:700}.h{color:var(--tx2);font-size:0.85rem;margin:0 0 0.6rem}
.meta{font-family:'JetBrains Mono',monospace;font-size:0.78rem;color:var(--tx2)}
.tag{display:inline-block;padding:0.15rem 0.5rem;border-radius:4px;font-size:0.7rem;font-weight:700;margin-left:0.4rem}
.incubating{background:rgba(245,158,11,0.18);color:var(--orange)}
.graduated{background:rgba(16,185,129,0.18);color:var(--green)}
.killed{background:rgba(239,68,68,0.18);color:var(--red)}
</style></head><body><h1><span class="flask">🧪</span>DC Hub · Lab</h1>
<p class="sub">Experiments cooking. Each has a hypothesis, a signal, and a graduation date.</p>"""
    for r in rows:
        sig = json.dumps(r.get('signal_data') or {})[:200]
        HTML += f'<div class="exp"><h3>{r["name"]}<span class="tag {r["status"]}">{r["status"]}</span></h3>'
        HTML += f'<p class="h">{r.get("hypothesis","") or ""}</p>'
        HTML += f'<div class="meta">started {r["started_at"].isoformat()[:10] if r.get("started_at") else "?"} · signal: {sig}</div></div>'
    HTML += "</body></html>"
    resp = make_response(HTML)
    if request.args.get("admin_key") == os.environ.get("DCHUB_ADMIN_KEY"):
        resp.set_cookie("dchub_lab_token", os.environ.get("DCHUB_ADMIN_KEY"),
                        httponly=True, secure=True, samesite="Lax", max_age=30*86400)
    return resp


@lab_bp.route("/api/v1/lab/list", methods=["GET"])
def list_experiments():
    _ensure()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM lab_experiments ORDER BY started_at DESC LIMIT 100")
        rows = cur.fetchall()
    for r in rows:
        for k in ("started_at","decision_at"):
            if r.get(k): r[k] = r[k].isoformat()
    return jsonify(experiments=rows, count=len(rows)), 200


@lab_bp.route("/api/v1/lab/create", methods=["POST"])
def create():
    if not _is_authorized(): return jsonify(error="unauthorized"), 401
    body = request.get_json(silent=True) or {}
    name = body.get("name"); hyp = body.get("hypothesis","")
    if not name: return jsonify(error="name required"), 400
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""INSERT INTO lab_experiments (name, hypothesis, signal_data)
            VALUES (%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id""", (name, hyp, json.dumps({})))
        eid = cur.fetchone()[0]; c.commit()
    return jsonify(id=eid, ok=True), 200


@lab_bp.route("/api/v1/lab/decide/<int:eid>", methods=["POST"])
def decide(eid):
    if not _is_authorized(): return jsonify(error="unauthorized"), 401
    body = request.get_json(silent=True) or {}
    decision = body.get("decision")  # 'graduated' | 'killed'
    note = body.get("note","")
    if decision not in ("graduated","killed"):
        return jsonify(error="decision must be graduated or killed"), 400
    _ensure()
    with _conn() as c, c.cursor() as cur:
        cur.execute("""UPDATE lab_experiments SET status=%s, decision_at=NOW(), decision_note=%s
                       WHERE id=%s RETURNING id""", (decision, note, eid))
        r = cur.fetchone(); c.commit()
    if not r: return jsonify(error="not found"), 404
    return jsonify(id=eid, decision=decision), 200

# === Phase 117A: /api/v1/lab/page is the CF-allowlisted alias for /lab ===
@lab_bp.route("/api/v1/lab/page", methods=["GET"])
def lab_dashboard_alias():
    return lab_dashboard()

