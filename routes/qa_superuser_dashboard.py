"""routes/qa_superuser_dashboard.py — the QA SUPER-USER BOARD as a browser page.

The QA super-user (tools/qa_superuser/) probes dchub from the CALLER'S seat —
anonymous agent, paying key, browser, crawler — and asserts on what actually
comes back on the wire. Its findings already land on a deduped GitHub issue.
This module gives the operator the same board without leaving the browser.

★★ THIS PAGE IS A CONVENIENCE VIEW, NOT THE SOURCE OF TRUTH — AND THAT IS
   LOAD-BEARING, NOT A DISCLAIMER.
   The whole reason the probe runs on GitHub Actions rather than in this worker
   is that a watcher hosted inside the thing it watches reports nothing at
   exactly the moment it matters. Serving its board from Flask reintroduces that
   coupling for the VIEW, so:
     · the GitHub issue remains authoritative and survives a dchub outage;
     · this page says so, in the header, on every render;
     · a failed beat NEVER fails the probe run (see board.py) — the issue is
       already written by then;
     · **this page being unreachable is itself a signal**, not an absence of news.

DATA FLOW (mirrors the dead-man ledger's beat pattern deliberately):
    GH Actions run -> POST /api/v1/admin/qa-superuser/beat  {the whole run JSON}
                   -> qa_superuser_runs (one row per run, JSONB)
    operator       -> GET  /api/v1/qa-superuser/dashboard?admin_key=...

Why the backend stores it instead of reading GitHub live: GH_TOKEN is not
present in Railway (it was deliberately removed), so a server-side GitHub fetch
would be a new secret and a new failure mode. The producer pushing to us needs
neither.

★ PATH SHAPE: both endpoints live under /api/ ON PURPOSE. The dchub.cloud CF
worker forwards every /api/ path to Railway unconditionally, while non-/api HTML
pages only reach Railway if they are in the worker's PHASE_282 allow-list — the
CF Error-1000 trap that the brain innovation dashboard documents.

AUTH: the same gate every admin dashboard here uses — X-Admin-Key /
X-Internal-Key header OR ?admin_key= query param (the query-param form is what
makes the page openable in a browser). No new auth scheme is invented.

SAFETY: read-only apart from the admin-gated beat. Every DB touch is wrapped; a
missing table yields empty, never a crash.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

qa_superuser_dashboard_bp = Blueprint("qa_superuser_dashboard", __name__)

ISSUE_URL = "https://github.com/azmartone67/dchub-backend/issues/2186"
WORKFLOW_URL = ("https://github.com/azmartone67/dchub-backend/actions/"
                "workflows/qa-superuser.yml")
HISTORY_LIMIT = 40


def _dsn() -> str:
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("NEON_DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or ""
    )


def _admin_ok() -> bool:
    """Same gate as the other admin dashboards — header or ?admin_key=."""
    expected = set()
    for name in ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "INTERNAL_KEY"):
        val = (os.environ.get(name) or "").strip()
        if val:
            expected.add(val)
    got = (
        request.headers.get("X-Admin-Key")
        or request.headers.get("X-Internal-Key")
        or request.args.get("admin_key")
        or ""
    ).strip()
    return bool(expected) and got in expected


def _conn():
    try:
        import psycopg2 as _pg
        dsn = _dsn()
        if not dsn:
            return None
        c = _pg.connect(dsn, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:  # noqa: BLE001
        logger.warning("[qa-superuser] db connect failed: %s", e)
        return None


def _ensure(cur) -> None:
    cur.execute(
        """CREATE TABLE IF NOT EXISTS qa_superuser_runs (
            id            BIGSERIAL PRIMARY KEY,
            generated_at  TIMESTAMPTZ NOT NULL,
            canary_fired  BOOLEAN NOT NULL,
            edge          TEXT,
            counts        JSONB NOT NULL,
            findings      JSONB NOT NULL,
            memory_ok     BOOLEAN,
            received_at   TIMESTAMPTZ DEFAULT NOW()
        )"""
    )
    # generated_at is the run's own clock, which is what every trend is drawn
    # against; received_at only records when it reached us.
    cur.execute(
        "CREATE INDEX IF NOT EXISTS qa_superuser_runs_gen_idx "
        "ON qa_superuser_runs (generated_at DESC)"
    )


# ── ingest ──────────────────────────────────────────────────────────────────
@qa_superuser_dashboard_bp.route("/api/v1/admin/qa-superuser/beat",
                                 methods=["POST"])
def qa_superuser_beat():
    """The probe posts its whole run here after it has written the GitHub issue.

    Deliberately dumb: it stores what it is given. The harness owns every
    judgement; this endpoint owns none of it. Storing a run whose canary did not
    fire is CORRECT — the dashboard needs to be able to say "the last run could
    not be trusted", which it cannot do if untrusted runs are silently dropped.
    """
    if not _admin_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    for field in ("generated_at", "counts", "findings"):
        if field not in payload:
            return jsonify({"ok": False, "error": f"missing {field}"}), 400

    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "no database"}), 503
    try:
        with c.cursor() as cur:
            _ensure(cur)
            cur.execute(
                """INSERT INTO qa_superuser_runs
                   (generated_at, canary_fired, edge, counts, findings, memory_ok)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (payload.get("generated_at"),
                 bool(payload.get("canary_fired")),
                 payload.get("edge"),
                 json.dumps(payload.get("counts") or {}),
                 json.dumps(payload.get("findings") or []),
                 payload.get("memory_ok")),
            )
            new_id = cur.fetchone()[0]
        return jsonify({"ok": True, "id": new_id})
    except Exception as e:  # noqa: BLE001
        logger.warning("[qa-superuser] beat failed: %s", e)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


# ── read ────────────────────────────────────────────────────────────────────
def _load(limit: int = HISTORY_LIMIT) -> dict:
    """Latest run + a short history. Never raises."""
    out = {"latest": None, "history": [], "error": None}
    c = _conn()
    if c is None:
        out["error"] = "database unreachable"
        return out
    try:
        with c.cursor() as cur:
            _ensure(cur)
            cur.execute(
                """SELECT generated_at, canary_fired, edge, counts, findings,
                          memory_ok, received_at
                     FROM qa_superuser_runs
                 ORDER BY generated_at DESC LIMIT %s""",
                (limit,),
            )
            rows = cur.fetchall() or []
        for i, r in enumerate(rows):
            rec = {
                "generated_at": r[0].isoformat() if r[0] else None,
                "canary_fired": bool(r[1]),
                "edge": r[2],
                "counts": r[3] or {},
                "memory_ok": r[5],
                "received_at": r[6].isoformat() if r[6] else None,
            }
            if i == 0:
                rec["findings"] = r[4] or []
                out["latest"] = rec
            out["history"].append({k: v for k, v in rec.items()
                                   if k != "findings"})
    except Exception as e:  # noqa: BLE001
        logger.warning("[qa-superuser] load failed: %s", e)
        out["error"] = str(e)[:200]
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def _age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0


@qa_superuser_dashboard_bp.route("/api/v1/qa-superuser/digest", methods=["GET"])
def qa_superuser_digest():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = _load()
    latest = data.get("latest") or {}
    data["ok"] = True
    data["stale_hours"] = _age_hours(latest.get("generated_at"))
    data["authoritative_board"] = ISSUE_URL
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── the page ────────────────────────────────────────────────────────────────
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>QA super-user — outside-in board</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<style>
:root{--bg:#0a0a12;--surface:#11121a;--surface2:#0d0e16;--bd:#1f2030;--tx:#fff;
  --tx2:#9ca3af;--tx3:#6b7280;--indigo:#6366f1;--violet:#a855f7;--green:#10b981;
  --amber:#f59e0b;--red:#ef4444;--slate:#64748b;
  --mono:'JetBrains Mono','SF Mono',ui-monospace,monospace;color-scheme:dark}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  background:var(--bg);color:var(--tx);margin:0;line-height:1.5;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1280px;margin:0 auto;padding:2rem 1.25rem 4rem}
.kicker{font-family:var(--mono);font-size:.74rem;color:#c4b5fd;text-transform:uppercase;
  letter-spacing:.14em;margin-bottom:.5rem;display:flex;align-items:center;gap:.5rem}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--green);
  box-shadow:0 0 8px var(--green);animation:p 2s ease-in-out infinite}
.pulse.bad{background:var(--red);box-shadow:0 0 8px var(--red)}
@keyframes p{0%,100%{opacity:1}50%{opacity:.35}}
h1{margin:0 0 .35rem;font-size:1.9rem;font-weight:800;letter-spacing:-.02em;
  background:linear-gradient(90deg,#fff,#c4b5fd);-webkit-background-clip:text;
  background-clip:text;color:transparent}
.sub{color:var(--tx2);max-width:900px;margin:0 0 1.25rem;font-size:.92rem}
.sub a{color:#c4b5fd}
.bar{display:flex;align-items:center;gap:.9rem;flex-wrap:wrap;margin-bottom:1.5rem;
  font-size:.78rem;color:var(--tx3);font-family:var(--mono)}
.banner{border-radius:10px;padding:.85rem 1rem;margin-bottom:1.25rem;font-size:.88rem;
  border:1px solid;line-height:1.55}
.banner.red{background:rgba(239,68,68,.09);border-color:rgba(239,68,68,.45);color:#fecaca}
.banner.amber{background:rgba(245,158,11,.08);border-color:rgba(245,158,11,.4);color:#fde68a}
.banner.info{background:rgba(99,102,241,.07);border-color:rgba(99,102,241,.32);color:#c7d2fe}
.tiles{display:grid;grid-template-columns:repeat(5,1fr);gap:.85rem;margin-bottom:1.75rem}
@media(max-width:860px){.tiles{grid-template-columns:repeat(2,1fr)}}
.tile{background:var(--surface);border:1px solid var(--bd);border-radius:12px;
  padding:.95rem 1rem}
.tile .n{font-family:var(--mono);font-size:1.7rem;font-weight:700;line-height:1.1}
.tile .l{font-size:.7rem;color:var(--tx3);text-transform:uppercase;
  letter-spacing:.1em;margin-top:.3rem}
.tile.red .n{color:var(--red)}.tile.green .n{color:var(--green)}
.tile.slate .n{color:var(--slate)}.tile.amber .n{color:var(--amber)}
h2{font-size:.78rem;color:var(--tx2);text-transform:uppercase;letter-spacing:.1em;
  margin:2rem 0 .85rem;font-weight:700;display:flex;align-items:center;gap:.55rem}
h2 .cnt{font-family:var(--mono);background:var(--surface);border:1px solid var(--bd);
  border-radius:99px;padding:.1rem .55rem;font-size:.72rem;color:#c4b5fd}
.card{background:var(--surface);border:1px solid var(--bd);border-radius:12px;
  padding:1rem 1.1rem;margin-bottom:.8rem;position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:var(--slate)}
.card.red::before{background:var(--red)}
.card.blind::before{background:var(--slate)}
.card.gauge::before{background:var(--indigo)}
.card h3{margin:0 0 .5rem;font-size:1rem;font-weight:650;display:flex;
  align-items:flex-start;gap:.5rem;flex-wrap:wrap}
.meta{font-family:var(--mono);font-size:.72rem;color:var(--tx3);margin-bottom:.6rem;
  display:flex;gap:.6rem;flex-wrap:wrap}
.chip{background:var(--surface2);border:1px solid var(--bd);border-radius:6px;
  padding:.08rem .45rem}
.chip.sev{color:#fecaca;border-color:rgba(239,68,68,.35)}
.chip.age{color:#fde68a;border-color:rgba(245,158,11,.3)}
.chip.unstable{color:#fde68a;border-color:rgba(245,158,11,.5);
  background:rgba(245,158,11,.08)}
.row{font-size:.86rem;margin:.35rem 0;color:var(--tx2)}
.row b{color:var(--tx);font-weight:600}
.row.obs{color:#e5e7eb}
code{font-family:var(--mono);font-size:.8rem;background:var(--surface2);
  border:1px solid var(--bd);border-radius:5px;padding:.05rem .3rem}
table{width:100%;border-collapse:collapse;font-size:.84rem}
th,td{text-align:left;padding:.5rem .6rem;border-bottom:1px solid var(--bd)}
th{color:var(--tx3);font-size:.7rem;text-transform:uppercase;letter-spacing:.08em}
td.v{font-family:var(--mono);color:#c4b5fd;white-space:nowrap}
td.e{color:var(--tx2);font-size:.8rem}
.spark{display:flex;align-items:flex-end;gap:2px;height:38px;margin:.4rem 0 0}
.spark i{flex:1;background:var(--green);border-radius:2px 2px 0 0;min-height:2px;
  opacity:.85}
.spark i.has{background:var(--red)}
.spark i.untrusted{background:var(--amber)}
details{margin-top:.5rem}
summary{cursor:pointer;color:var(--tx3);font-size:.8rem}
.pass{font-size:.84rem;color:var(--tx2);padding:.3rem 0;border-bottom:1px solid var(--bd)}
.pass b{color:#a7f3d0;font-weight:600}
.foot{margin-top:2.5rem;padding-top:1.25rem;border-top:1px solid var(--bd);
  color:var(--tx3);font-size:.8rem;line-height:1.7}
.err{color:#fecaca}
</style></head><body><div class="wrap" id="root">
<div class="kicker"><span class="pulse"></span>loading</div></div>
<script>
const KEY = new URLSearchParams(location.search).get('admin_key') || '';
const esc = s => String(s==null?'':s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function span(iso){
  if(!iso) return null;
  const h = (Date.now() - new Date(iso).getTime())/3.6e6;
  if (h < 1) return Math.round(h*60) + 'm';
  if (h < 48) return h.toFixed(1) + 'h';
  return (h/24).toFixed(1) + 'd';
}
// Two forms on purpose: "run 2m ago" is a point in time, "red for 6.8d" is a
// duration. Reusing one helper produced "failing 6.8d ago", which reads as the
// moment it started rather than how long it has been broken — and how long a
// red has been live is the single most useful number on this board.
function ago(iso){ return iso ? span(iso) + ' ago' : 'never'; }

function card(f, cls){
  const sev = f.severity && f.severity !== 'info'
    ? `<span class="chip sev">${esc(f.severity)}</span>` : '';
  const unstable = (f.transitions||0) >= 4
    ? `<span class="chip unstable">UNSTABLE ${f.transitions}x</span>` : '';
  const age = f.failing_since
    ? `<span class="chip age">red for ${span(f.failing_since)}</span>` : '';
  return `<div class="card ${cls}">
    <h3>${esc(f.title)}</h3>
    <div class="meta"><span class="chip">${esc(f.surface)}</span>
      <span class="chip">seat: ${esc(f.seat)}</span>${sev}${age}${unstable}</div>
    <div class="row obs"><b>Observed:</b> ${esc(f.evidence)}</div>
    <div class="row"><b>Measured from:</b> ${esc(f.basis)}</div>
    ${f.verdict==='RED' ? `<div class="row"><b>Red when:</b> ${esc(f.red_when)}</div>`:''}
    ${f.remedy ? `<div class="row"><b>Why it matters:</b> ${esc(f.remedy)}</div>`:''}
  </div>`;
}

function render(d){
  const L = d.latest;
  const root = document.getElementById('root');
  if (d.error && !L){
    root.innerHTML = `<div class="banner red"><b>Cannot read the board.</b>
      ${esc(d.error)}. The authoritative board is
      <a href="${ISSUE}">the GitHub issue</a> — it does not depend on this
      backend.</div>`;
    return;
  }
  if (!L){
    root.innerHTML = `<div class="banner amber"><b>No runs recorded yet.</b>
      The probe posts here after each run; until then the board lives only on
      <a href="${ISSUE}">GitHub</a>.</div>`;
    return;
  }
  const F = L.findings || [];
  const c = L.counts || {};
  const reds = F.filter(f=>f.verdict==='RED');
  const blind = F.filter(f=>f.verdict==='BLIND');
  const gauges = F.filter(f=>f.verdict==='GAUGE');
  const passes = F.filter(f=>f.verdict==='PASS');
  const stale = d.stale_hours!=null && d.stale_hours > 9;

  let banners = '';
  if (!L.canary_fired) banners += `<div class="banner red">
    <b>⚠️ THIS RUN IS NOT TRUSTWORTHY.</b> The must-fail control did not fire, so
    the harness could not be shown capable of reporting a failure. Every green
    below is unproven. Fix the harness before reading anything here as
    reassurance.</div>`;
  if (L.memory_ok === false) banners += `<div class="banner amber">
    <b>The board had no memory on this run.</b> Durable state could not be
    written, so NEW / REGRESSED / RECOVERED labels are unreliable.</div>`;
  if (stale) banners += `<div class="banner amber">
    <b>The last run was ${span(L.generated_at)} ago.</b> The probe runs every 4h — a gap
    this size means the workflow is not completing, or its beat is not reaching
    this backend. Check <a href="${WF}">the workflow</a>.</div>`;

  root.innerHTML = `
  <div class="kicker"><span class="pulse ${reds.length?'bad':''}"></span>
    outside-in QA · caller's seat</div>
  <h1>QA super-user board</h1>
  <p class="sub">Every master shell reads the database. This one <b>uses the
  product</b> — anonymous agent, paying key, browser, crawler — and asserts on
  what comes back on the wire.
  <b>⚪ unobserved</b> means the probe could not look; it is never a failure.
  <b>📊 gauge</b> reports a number because no threshold exists that the platform
  itself defines.</p>
  <div class="bar">
    <span>run ${ago(L.generated_at)}</span><span>·</span>
    <span>${esc(L.edge||'')}</span><span>·</span>
    <span>canary ${L.canary_fired?'fired ✓':'DID NOT FIRE ✗'}</span><span>·</span>
    <a href="${ISSUE}" style="color:#c4b5fd">authoritative board ↗</a><span>·</span>
    <a href="${WF}" style="color:#c4b5fd">workflow ↗</a>
  </div>
  ${banners}
  <div class="tiles">
    <div class="tile ${reds.length?'red':'green'}"><div class="n">${reds.length}</div>
      <div class="l">red</div></div>
    <div class="tile slate"><div class="n">${blind.length}</div>
      <div class="l">unobserved</div></div>
    <div class="tile amber"><div class="n">${gauges.length}</div>
      <div class="l">gauges</div></div>
    <div class="tile green"><div class="n">${passes.length}</div>
      <div class="l">passing</div></div>
    <div class="tile ${c.critical?'red':'slate'}"><div class="n">${c.critical||0}</div>
      <div class="l">critical</div></div>
  </div>

  <h2>Red <span class="cnt">${reds.length}</span></h2>
  ${reds.length ? reds.map(f=>card(f,'red')).join('')
    : `<div class="card"><div class="row">Nothing observed failing on this run.
       That is a claim about what was <i>looked at</i> — see unobserved
       below.</div></div>`}

  ${blind.length ? `<h2>Unobserved — not failures <span class="cnt">${blind.length}</span></h2>
    ${blind.map(f=>card(f,'blind')).join('')}` : ''}

  <h2>Gauges — tracked, no pass/fail claim <span class="cnt">${gauges.length}</span></h2>
  <div class="card gauge"><table><thead><tr><th>metric</th><th>value</th>
    <th>observed</th></tr></thead><tbody>
    ${gauges.map(f=>`<tr><td>${esc(f.title)}</td>
      <td class="v">${esc(f.value)}</td>
      <td class="e">${esc(String(f.evidence).slice(0,150))}</td></tr>`).join('')}
  </tbody></table></div>

  <h2>Trend <span class="cnt">${(d.history||[]).length} runs</span></h2>
  <div class="card"><div class="spark">
    ${(d.history||[]).slice().reverse().map(h=>{
      const r=(h.counts||{}).red||0;
      const cls = !h.canary_fired ? 'untrusted' : (r?'has':'');
      return `<i class="${cls}" style="height:${Math.min(100,(r*22)+8)}%"
        title="${esc(h.generated_at)} — ${r} red${h.canary_fired?'':' (canary did not fire)'}"></i>`;
    }).join('')}
  </div><div class="row" style="margin-top:.5rem;color:var(--tx3);font-size:.78rem">
    reds per run, oldest → newest. amber = the must-fail control did not fire, so
    that run's greens are unproven.</div></div>

  <h2>Passing <span class="cnt">${passes.length}</span></h2>
  <div class="card"><details><summary>show ${passes.length} passing check(s)</summary>
    <div style="margin-top:.6rem">
    ${passes.map(f=>`<div class="pass"><b>${esc(f.surface)}</b> ${esc(f.title)}
      — ${esc(String(f.evidence).slice(0,170))}</div>`).join('')}
    </div></details></div>

  <div class="foot">
    <b>This page is a convenience view, not the source of truth.</b> The probe runs
    on GitHub Actions rather than in this worker precisely so a watcher is not
    hosted inside the thing it watches — so the authoritative board is
    <a href="${ISSUE}" style="color:#c4b5fd">the GitHub issue</a>, which survives a
    dchub outage. <b>This page being unreachable is itself a signal.</b><br>
    Probe traffic self-identifies as <code>dchub-qa-superuser</code> — exclude it
    from reach and usage metrics by <b>User-Agent</b>, never by platform tag (the
    MCP server overwrites the platform field).<br>
    The board never merges, deploys or executes. It reports.
  </div>`;
}

const ISSUE = "__ISSUE__", WF = "__WF__";
fetch('/api/v1/qa-superuser/digest?admin_key=' + encodeURIComponent(KEY),
      {headers:{'Accept':'application/json'}})
  .then(r => r.ok ? r.json() : r.json().then(j=>{throw new Error(j.error||r.status)}))
  .then(render)
  .catch(e => {
    document.getElementById('root').innerHTML =
      `<div class="banner red"><b>Could not load the digest.</b>
       <span class="err">${esc(e.message)}</span> — if this is an auth error, add
       <code>?admin_key=&lt;key&gt;</code> to the URL. The authoritative board is
       <a href="${ISSUE}">the GitHub issue</a>.</div>`;
  });
</script></body></html>"""


@qa_superuser_dashboard_bp.route("/api/v1/qa-superuser/dashboard",
                                 methods=["GET"])
def qa_superuser_dashboard():
    """Browser-openable board. Auth is checked server-side before rendering."""
    if not _admin_ok():
        return Response(
            "<body style='font-family:-apple-system,sans-serif;background:#0a0a12;"
            "color:#9a9a9a;display:flex;align-items:center;justify-content:center;"
            "height:100vh;margin:0'><div style='text-align:center'>"
            "<h2 style='color:#fff'>QA super-user board</h2>"
            "<p>Add <code>?admin_key=&lt;key&gt;</code> to the URL.</p></div></body>",
            status=401, mimetype="text/html")
    page = _PAGE.replace("__ISSUE__", ISSUE_URL).replace("__WF__", WORKFLOW_URL)
    resp = Response(page, mimetype="text/html")
    # Live operational data must never sit in the CF edge cache.
    resp.headers["Cache-Control"] = "no-store"
    return resp


def register_qa_superuser_dashboard(app):
    try:
        app.register_blueprint(qa_superuser_dashboard_bp)
        logger.info("qa_superuser_dashboard registered")
    except Exception as e:  # noqa: BLE001
        logger.warning("qa_superuser_dashboard register failed: %s", e)
