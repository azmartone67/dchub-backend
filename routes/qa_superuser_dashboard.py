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

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

qa_superuser_dashboard_bp = Blueprint("qa_superuser_dashboard", __name__)

C_REPO = "azmartone67/dchub-backend"
ISSUE_URL = f"https://github.com/{C_REPO}/issues/2186"
WORKFLOW_URL = f"https://github.com/{C_REPO}/actions/workflows/qa-superuser.yml"
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
    #
    # ★ UNIQUE, and that is not cosmetic. A run is identified by the moment it
    # was generated, so a RETRIED beat — a workflow re-run, a network retry, an
    # operator re-dispatch — must update that run, not append a second copy.
    # Without this the trend sparkline grows phantom bars and the "runs" count
    # overstates coverage, which is the same class of quiet inflation this whole
    # tool exists to catch. (regression-lint's insert-no-on-conflict rule flagged
    # the missing clause; it was right, for a reason worth writing down.)
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS qa_superuser_runs_gen_uidx "
        "ON qa_superuser_runs (generated_at)"
    )


def _ensure_acks(cur) -> None:
    """Acknowledgements — bound to the EVIDENCE, not just the finding.

    ★ An ack stores a hash of the evidence it was given for, and expires the
    moment that evidence changes. Acknowledging a finding must mean "I have seen
    THIS", never "stop telling me about this class of thing" — otherwise the
    first ack silences every future, worse version of the same check, which is
    the muted-alarm failure that makes boards useless.
    """
    cur.execute(
        """CREATE TABLE IF NOT EXISTS qa_superuser_acks (
            finding_key   TEXT PRIMARY KEY,
            evidence_sha  TEXT NOT NULL,
            note          TEXT,
            acked_at      TIMESTAMPTZ DEFAULT NOW()
        )"""
    )


def evidence_sha(evidence: str) -> str:
    return hashlib.sha256((evidence or "").encode()).hexdigest()[:16]


def derive_question(f: dict) -> str:
    """Turn one finding into a question the brain can actually answer.

    ★ CARRIES ITS OWN EVIDENCE. The brain's investigator was measured refuting
    70% of its own drafts for citing evidence it was never given: gather_evidence()
    took no arguments, so 111 distinct questions received seven evidence
    signatures between them. A question that inlines what was observed is the
    cheapest possible fix for that.

    ★ AND IT IS SHORT ON PURPOSE. A long derived question timed out the REASON
    step outright (`cannot_investigate: call_fail:TimeoutError`) while a 182-char
    one completed. Length is a functional constraint here, not style.
    """
    ev = (f.get("evidence") or "").strip()
    if len(ev) > 170:
        ev = ev[:167].rstrip() + "..."
    q = (f"{f.get('title', 'QA finding')} — observed from the "
         f"{f.get('seat', '?')} seat on {f.get('surface', '?')}: {ev} "
         f"What is the root cause and the smallest correct fix?")
    return q[:320]


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
            # Idempotent by generated_at: a re-posted run REPLACES itself rather
            # than appending a duplicate. See the unique index in _ensure().
            cur.execute(
                """INSERT INTO qa_superuser_runs
                   (generated_at, canary_fired, edge, counts, findings, memory_ok)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (generated_at) DO UPDATE SET
                       canary_fired = EXCLUDED.canary_fired,
                       edge         = EXCLUDED.edge,
                       counts       = EXCLUDED.counts,
                       findings     = EXCLUDED.findings,
                       memory_ok    = EXCLUDED.memory_ok,
                       received_at  = NOW()
                   RETURNING id""",
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


# ── actions ─────────────────────────────────────────────────────────────────
# None of these fix anything. They route a finding into a lane that already
# exists and is already human-gated. The line that has held since the autonomy
# core was written — propose, never execute — is not moved by adding buttons.
@qa_superuser_dashboard_bp.route("/api/v1/admin/qa-superuser/ack",
                                 methods=["POST"])
def qa_superuser_ack():
    """Record that a human has seen this finding, bound to THIS evidence."""
    if not _admin_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    key = (body.get("key") or "").strip()
    if not key:
        return jsonify({"ok": False, "error": "missing key"}), 400
    sha = evidence_sha(body.get("evidence") or "")
    note = (body.get("note") or "").strip()[:500]

    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "no database"}), 503
    try:
        with c.cursor() as cur:
            _ensure_acks(cur)
            if body.get("clear"):
                cur.execute("DELETE FROM qa_superuser_acks WHERE finding_key=%s",
                            (key,))
                return jsonify({"ok": True, "acked": False})
            cur.execute(
                """INSERT INTO qa_superuser_acks
                       (finding_key, evidence_sha, note, acked_at)
                   VALUES (%s, %s, %s, NOW())
                   ON CONFLICT (finding_key) DO UPDATE SET
                       evidence_sha = EXCLUDED.evidence_sha,
                       note         = EXCLUDED.note,
                       acked_at     = NOW()""",
                (key, sha, note),
            )
        return jsonify({"ok": True, "acked": True, "evidence_sha": sha})
    except Exception as e:  # noqa: BLE001
        logger.warning("[qa-superuser] ack failed: %s", e)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


@qa_superuser_dashboard_bp.route("/api/v1/admin/qa-superuser/investigate",
                                 methods=["POST"])
def qa_superuser_investigate():
    """Hand ONE finding to the brain's investigator, evidence included.

    ★ DISPATCHES ON A THREAD AND RETURNS IMMEDIATELY. An investigation is an LLM
    call that brain-self-direct allows 180s; awaiting it here would pin a
    gunicorn worker for minutes and starve the small pool, and awaiting it
    through the CF edge is impossible anyway — the zone's 15s route timeout 503s
    admin POSTs. The investigator writes its own brain_investigations row on
    completion, so fire-and-forget loses nothing.

    ★ A fast 200 therefore means DISPATCHED, not finished. Say so in the
    response, or the next person reads it as a result.
    """
    if not _admin_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    key = (body.get("key") or "").strip()
    if not key:
        return jsonify({"ok": False, "error": "missing key"}), 400

    # Derive the question SERVER-side from the stored finding rather than
    # trusting whatever the page posts — the length and evidence constraints in
    # derive_question() are functional, and a client could violate both.
    finding = None
    data = _load(limit=1)
    for f in ((data.get("latest") or {}).get("findings") or []):
        if f.get("key") == key:
            finding = f
            break
    if finding is None:
        return jsonify({"ok": False,
                        "error": "finding not in the latest run"}), 404

    question = derive_question(finding)

    # ★ Internal/origin base, never the public edge (CF 15s timeout on admin
    # POSTs), and .strip() because a trailing newline in a Railway env value
    # becomes %0a and raises InvalidURL at request time.
    base = ((os.environ.get("DCHUB_INTERNAL_API") or "").strip()
            or (os.environ.get("RAILWAY_BACKEND_URL") or "").strip()
            or "http://127.0.0.1:" + ((os.environ.get("PORT") or "8080").strip()))
    if base and not base.startswith("http"):
        base = "https://" + base
    url = base.rstrip("/") + "/api/v1/brain/investigate"
    admin = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()

    def _bg():
        try:
            import requests as _rq
            r = _rq.post(url, json={"question": question}, timeout=300,
                         headers={"X-Admin-Key": admin,
                                  "User-Agent": "dchub-qa-superuser/1.0"})
            logger.info("[qa-superuser] investigate dispatched -> %s", r.status_code)
        except Exception as e:  # noqa: BLE001
            logger.warning("[qa-superuser] investigate failed: %s", str(e)[:200])

    try:
        import threading
        threading.Thread(target=_bg, daemon=True).start()
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:200]}), 500

    return jsonify({
        "ok": True,
        "dispatched": True,
        "note": "DISPATCHED, not finished — the investigator writes its own "
                "brain_investigations row when it completes (up to ~3 min).",
        "question": question,
    })


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


def _attach_acks(latest: dict) -> None:
    """Mark each finding acknowledged / stale-acknowledged / not acknowledged.

    ★ THREE STATES, NOT TWO. An ack is bound to the evidence it was given for.
    When the evidence changes the ack goes STALE rather than disappearing —
    "you acknowledged this, but what it says has changed since" is a different
    and more useful message than either "acknowledged" or silence. Collapsing
    that into a boolean would let one ack mute every future, worse version of
    the same finding.
    """
    findings = latest.get("findings") or []
    if not findings:
        return
    acks: dict[str, tuple] = {}
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            _ensure_acks(cur)
            cur.execute("SELECT finding_key, evidence_sha, note, acked_at "
                        "FROM qa_superuser_acks")
            for k, sha, note, at in cur.fetchall() or []:
                acks[k] = (sha, note, at)
    except Exception as e:  # noqa: BLE001
        logger.warning("[qa-superuser] ack read failed: %s", e)
        return
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass

    for f in findings:
        rec = acks.get(f.get("key"))
        if not rec:
            continue
        sha, note, at = rec
        current = evidence_sha(f.get("evidence") or "")
        f["ack"] = {
            "state": "current" if sha == current else "stale",
            "note": note,
            "at": at.isoformat() if at else None,
        }


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
    _attach_acks(latest)
    data["ok"] = True
    data["stale_hours"] = _age_hours(latest.get("generated_at"))
    data["authoritative_board"] = ISSUE_URL
    data["repo"] = C_REPO
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
.acts{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.85rem;padding-top:.75rem;
  border-top:1px solid var(--bd);align-items:center}
.btn{background:var(--surface2);border:1px solid var(--bd);color:var(--tx2);
  border-radius:8px;padding:.35rem .7rem;font-size:.78rem;cursor:pointer;
  font-family:inherit;transition:.15s;text-decoration:none;display:inline-block}
.btn:hover{border-color:var(--indigo);color:#c7d2fe;background:rgba(99,102,241,.09)}
.btn:disabled{opacity:.5;cursor:default}
.btn.done{border-color:rgba(16,185,129,.5);color:#a7f3d0;
  background:rgba(16,185,129,.08)}
.btn.warn{border-color:rgba(245,158,11,.45);color:#fde68a}
.acted{font-size:.76rem;color:var(--tx3);font-family:var(--mono)}
.acked{border-radius:8px;padding:.5rem .7rem;margin-top:.6rem;font-size:.8rem;
  border:1px solid rgba(16,185,129,.35);background:rgba(16,185,129,.06);
  color:#a7f3d0}
.acked.stale{border-color:rgba(245,158,11,.45);background:rgba(245,158,11,.07);
  color:#fde68a}
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
  const ack = f.ack ? `<div class="acked ${f.ack.state==='stale'?'stale':''}">
      ${f.ack.state === 'stale'
        ? `<b>Acknowledged ${ago(f.ack.at)} — but the evidence has CHANGED since.</b>
           What you signed off on is not what this check is reporting now.`
        : `<b>Acknowledged ${ago(f.ack.at)}.</b>`}
      ${f.ack.note ? ' ' + esc(f.ack.note) : ''}</div>` : '';

  // Actions exist only on RED. A gauge makes no claim to act on, and an
  // unobserved finding is a request to look again, not a defect to route.
  const acts = f.verdict !== 'RED' ? '' : `<div class="acts" data-key="${esc(f.key)}">
      <button class="btn" data-act="investigate">🧠 Ask the brain</button>
      <a class="btn" target="_blank" rel="noopener"
         href="${issueUrl(f)}">📋 Open an issue</a>
      <button class="btn ${f.ack && f.ack.state==='current' ? 'done' : ''}"
              data-act="ack">${f.ack && f.ack.state==='current'
                ? '✓ Acknowledged — undo' : '✓ Acknowledge'}</button>
      <span class="acted" data-out></span>
    </div>`;

  return `<div class="card ${cls}">
    <h3>${esc(f.title)}</h3>
    <div class="meta"><span class="chip">${esc(f.surface)}</span>
      <span class="chip">seat: ${esc(f.seat)}</span>${sev}${age}${unstable}</div>
    <div class="row obs"><b>Observed:</b> ${esc(f.evidence)}</div>
    <div class="row"><b>Measured from:</b> ${esc(f.basis)}</div>
    ${f.verdict==='RED' ? `<div class="row"><b>Red when:</b> ${esc(f.red_when)}</div>`:''}
    ${f.remedy ? `<div class="row"><b>Why it matters:</b> ${esc(f.remedy)}</div>`:''}
    ${ack}${acts}
  </div>`;
}

// ★ Client-side prefilled GitHub link, NOT a server-side issue create. The
// backend holds no GH_TOKEN by design, and adding one to open issues would be a
// new secret and a new failure mode for a job the browser can do for free. This
// opens GitHub's own new-issue form with everything filled in; the human presses
// Submit, so the human-gated line is preserved by construction rather than by
// policy.
function issueUrl(f){
  const title = `[qa-superuser] ${f.title}`;
  const body = [
    `Found by the outside-in QA super-user, probing from the **${f.seat}** seat.`,
    '',
    `**Observed:** ${f.evidence}`,
    `**Measured from:** ${f.basis}`,
    `**Red when:** ${f.red_when}`,
    f.remedy ? `**Why it matters:** ${f.remedy}` : '',
    '',
    f.failing_since ? 'Failing since ' + f.failing_since + '.' : '',
    (f.transitions||0) >= 4
      ? `⚠️ This check is UNSTABLE — it has crossed the pass/fail line ${f.transitions}x, so treat a single reading with care.`
      : '',
    '',
    `Board: ${ISSUE} · finding key ` + f.key,
  ].filter(Boolean).join('\\n');
  return `https://github.com/${REPO}/issues/new`
       + `?title=${encodeURIComponent(title)}`
       + `&body=${encodeURIComponent(body)}`
       + `&labels=${encodeURIComponent('qa-superuser')}`;
}

async function act(el){
  const wrap = el.closest('.acts');
  const key = wrap.dataset.key;
  const out = wrap.querySelector('[data-out]');
  const kind = el.dataset.act;
  const f = (LATEST.findings || []).find(x => x.key === key) || {};
  el.disabled = true;
  try {
    if (kind === 'investigate') {
      out.textContent = 'dispatching…';
      const r = await post('/api/v1/admin/qa-superuser/investigate', {key});
      // ★ A fast 200 means DISPATCHED, not finished — say so, or the next
      // person reads the green button as an answer.
      out.textContent = r.ok
        ? 'dispatched — the brain writes its result in ~3 min, not now'
        : ('failed: ' + (r.error || '?'));
      el.classList.toggle('done', !!r.ok);
      el.classList.toggle('warn', !r.ok);
    } else if (kind === 'ack') {
      const already = f.ack && f.ack.state === 'current';
      if (already) {
        await post('/api/v1/admin/qa-superuser/ack', {key, clear: true});
        out.textContent = 'acknowledgement cleared';
      } else {
        const note = prompt('Optional note — what did you conclude?') || '';
        const r = await post('/api/v1/admin/qa-superuser/ack',
                             {key, evidence: f.evidence, note});
        out.textContent = r.ok
          ? 'acknowledged — this expires automatically if the evidence changes'
          : ('failed: ' + (r.error || '?'));
      }
      setTimeout(load, 600);
    }
  } catch (e) {
    out.textContent = 'error: ' + e.message;
    el.classList.add('warn');
  } finally {
    el.disabled = false;
  }
}

async function post(path, body){
  const r = await fetch(path + '?admin_key=' + encodeURIComponent(KEY), {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Admin-Key': KEY},
    body: JSON.stringify(body),
  });
  try { return await r.json(); }
  catch (e) { return {ok: false, error: 'HTTP ' + r.status}; }
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

const ISSUE = "__ISSUE__", WF = "__WF__", REPO = "__REPO__";
let LATEST = {};

// Delegated, so re-rendering the board never leaves dead handlers behind.
document.addEventListener('click', e => {
  const btn = e.target.closest('.acts button[data-act]');
  if (btn) act(btn);
});

function load(){
  return fetch('/api/v1/qa-superuser/digest?admin_key=' + encodeURIComponent(KEY),
        {headers:{'Accept':'application/json'}})
    .then(r => r.ok ? r.json() : r.json().then(j=>{throw new Error(j.error||r.status)}))
    .then(d => { LATEST = d.latest || {}; render(d); })
    .catch(e => {
      document.getElementById('root').innerHTML =
        `<div class="banner red"><b>Could not load the digest.</b>
         <span class="err">${esc(e.message)}</span> — if this is an auth error, add
         <code>?admin_key=&lt;key&gt;</code> to the URL. The authoritative board is
         <a href="${ISSUE}">the GitHub issue</a>.</div>`;
    });
}
load();
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
    page = (_PAGE.replace("__ISSUE__", ISSUE_URL)
                 .replace("__WF__", WORKFLOW_URL)
                 .replace("__REPO__", C_REPO))
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
