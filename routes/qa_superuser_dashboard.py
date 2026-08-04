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


def _ensure_investigations(cur) -> None:
    """The brain's analysis of a finding — bound to the EVIDENCE, like an ack.

    ★ The reason this table exists at all: the investigate action used to POST to
    the brain, log ``r.status_code``, and throw ``r.json()`` away. The endpoint is
    SYNCHRONOUS and returns the whole verified chain in-request, so the analysis
    was arriving intact and being discarded one line before it could be read. The
    row nobody looked at (``brain_investigations``) was the only survivor, keyed
    by question text rather than by finding. Hence: "I opened the issues but they
    just sit there" — the thinking happened and never reached the issue.

    ★ evidence_sha, same as acks: an investigation explains ONE observation. When
    the observation changes the analysis is stale, and stale analysis presented as
    current is how a board starts lying. It is retained (still useful history),
    just no longer labelled as an explanation of what is on screen now.
    """
    cur.execute(
        """CREATE TABLE IF NOT EXISTS qa_superuser_investigations (
            finding_key    TEXT PRIMARY KEY,
            evidence_sha   TEXT NOT NULL,
            question       TEXT,
            recommendation TEXT,
            confidence     DOUBLE PRECISION,
            survived       BOOLEAN,
            result_json    JSONB,
            brain_id       BIGINT,
            issue_number   INTEGER,
            commented      BOOLEAN DEFAULT FALSE,
            created_at     TIMESTAMPTZ DEFAULT NOW()
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


INVESTIGATION_MARKER = "<!-- qa-superuser:investigation -->"


def render_investigation_comment(finding: dict, result: dict) -> str:
    """Render one investigation as the GitHub comment a human will actually read.

    ★ Reports the REFUTATION, not just the recommendation. This investigator was
    measured refuting ~70% of its own drafts, so a comment that prints only the
    conclusion presents the 30% and the 70% identically. `survives_scrutiny:
    false` is the single most decision-relevant field in the payload and it leads
    here for that reason.

    ★ Prints `cannot_investigate` verbatim when present. A degraded run (no API
    key, model timeout) must not be rendered as a thin analysis — the difference
    between "the brain looked and found little" and "the brain never ran" is the
    whole value of the comment.
    """
    out = [INVESTIGATION_MARKER,
           "## 🧠 Brain investigation",
           "",
           f"_Finding_ `{finding.get('key', '?')}` — "
           f"{finding.get('title', '(untitled)')}", ""]

    cannot = result.get("cannot_investigate")
    if cannot:
        out += [f"**The investigation did not run:** `{cannot}`", "",
                "This is not a finding about the product — it is the analysis "
                "step failing. The finding itself stands unexplained.", ""]
        return "\n".join(out)

    ref = result.get("refutation") or {}
    survived = ref.get("survived")
    if survived is False:
        out += ["> [!WARNING]", "> **This recommendation did NOT survive the "
                "brain's own refutation pass.** Treat it as a lead, not an "
                "answer.", ""]

    conf = result.get("confidence")
    if isinstance(conf, (int, float)):
        out.append(f"**Confidence:** {conf:.2f}"
                   + ("  ·  **survived refutation:** "
                      + {True: "yes", False: "no"}.get(survived, "unknown")))
        out.append("")

    rec = (result.get("recommendation") or "").strip()
    out += ["### Recommendation", "", rec or "_(none returned)_", ""]

    decision = (result.get("decision_for_human") or "").strip()
    if decision:
        out += ["### The decision this needs from a human", "", decision, ""]

    caveats = [c for c in (result.get("caveats") or []) if str(c).strip()]
    if caveats:
        out += ["### Caveats"] + [f"- {c}" for c in caveats[:8]] + [""]

    weaknesses = [w for w in (ref.get("weaknesses_found") or []) if str(w).strip()]
    if weaknesses:
        out += ["### Weaknesses the refutation pass found"]
        out += [f"- {w}" for w in weaknesses[:8]] + [""]

    out += ["---",
            "_Posted by the QA super-user after routing this finding to "
            "`/api/v1/brain/investigate`. **Recommend-only** — nothing here has "
            "been applied. Read [the autonomy line]"
            "(https://github.com/" + C_REPO + "/blob/main/tools/qa_superuser/"
            "README.md) before acting on it._"]
    return "\n".join(out)


def _gh_headers() -> dict | None:
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        return None
    return {"Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {token}",
            "User-Agent": "dchub-qa-superuser/1.0"}


def find_marked_comment(comments: list, marker: str) -> int | None:
    """Return the id of the first comment carrying `marker`, else None.

    Pure, so the dedup rule is testable without GitHub.
    """
    for c in comments or []:
        if marker in ((c or {}).get("body") or ""):
            cid = (c or {}).get("id")
            if cid is not None:
                return int(cid)
    return None


def _post_issue_comment(issue_number: int, body: str,
                        marker: str | None = None) -> tuple[bool, str]:
    """Comment on an issue. Best-effort: never raises, always explains itself.

    ★ When `marker` is given this UPDATES the existing marked comment instead of
    appending a second one. Re-investigating a finding is a legitimate thing to
    do — a second opinion, or a look after the evidence moved — but each click
    appending another wall of analysis turns the issue into the thing every
    watcher on this platform has already learned not to build: a thread so noisy
    nobody reads it. One comment per finding, rewritten, matching how the board
    issue itself already behaves.
    """
    headers = _gh_headers()
    if headers is None:
        return False, "GITHUB_TOKEN not set"
    base = f"https://api.github.com/repos/{C_REPO}/issues"
    try:
        import requests as _rq
        existing_id = None
        if marker:
            # Fail-open: if the lookup breaks we append rather than lose the
            # analysis. A duplicate comment is a nuisance; a dropped one is the
            # bug this whole change exists to fix.
            try:
                lr = _rq.get(f"{base}/{int(issue_number)}/comments?per_page=100",
                             headers=headers, timeout=20)
                if lr.status_code == 200:
                    existing_id = find_marked_comment(lr.json(), marker)
            except Exception as e:  # noqa: BLE001
                logger.warning("[qa-superuser] comment lookup failed: %s",
                               str(e)[:120])

        if existing_id is not None:
            r = _rq.patch(f"https://api.github.com/repos/{C_REPO}/issues/"
                          f"comments/{existing_id}",
                          headers=headers, json={"body": body[:60000]},
                          timeout=25)
            verb = "updated"
        else:
            r = _rq.post(f"{base}/{int(issue_number)}/comments",
                         headers=headers, json={"body": body[:60000]},
                         timeout=25)
            verb = "created"
        if r.status_code in (200, 201):
            return True, f"{verb}: {(r.json() or {}).get('html_url') or '?'}"
        return False, f"HTTP {r.status_code}: {r.text[:160]}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:160]}"


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


def _persist_investigation(meta: dict, ev_sha: str, question: str, result: dict,
                           brain_id, issue_number) -> None:
    """Store the analysis, then put it where the human is already looking.

    Order matters: STORE FIRST, comment second. The comment is the part that can
    fail for reasons that have nothing to do with the analysis (token scope, a
    closed issue, rate limits), and losing a 48-second verified investigation
    because a REST call 403'd would rebuild the exact hole this closes.
    """
    key = meta["key"]
    c = _conn()
    if c is None:
        logger.warning("[qa-superuser] no db — investigation not stored")
        return
    ref = result.get("refutation") or {}
    try:
        with c.cursor() as cur:
            _ensure_investigations(cur)
            cur.execute(
                """INSERT INTO qa_superuser_investigations
                       (finding_key, evidence_sha, question, recommendation,
                        confidence, survived, result_json, brain_id,
                        issue_number, commented, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,NOW())
                   ON CONFLICT (finding_key) DO UPDATE SET
                       evidence_sha   = EXCLUDED.evidence_sha,
                       question       = EXCLUDED.question,
                       recommendation = EXCLUDED.recommendation,
                       confidence     = EXCLUDED.confidence,
                       survived       = EXCLUDED.survived,
                       result_json    = EXCLUDED.result_json,
                       brain_id       = EXCLUDED.brain_id,
                       issue_number   = EXCLUDED.issue_number,
                       commented      = FALSE,
                       created_at     = NOW()""",
                (key, ev_sha, question[:2000],
                 (result.get("recommendation") or "")[:8000],
                 float(result.get("confidence") or 0.0),
                 ref.get("survived"), json.dumps(result)[:200000],
                 brain_id, issue_number),
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("[qa-superuser] investigation store failed: %s", e)
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
        return

    if not issue_number:
        logger.info("[qa-superuser] finding %s has no issue — stored only", key)
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
        return

    ok, detail = _post_issue_comment(
        int(issue_number), render_investigation_comment(meta, result),
        marker=INVESTIGATION_MARKER)
    logger.info("[qa-superuser] comment on #%s: %s (%s)",
                issue_number, "ok" if ok else "FAILED", detail)
    try:
        if ok:
            with c.cursor() as cur:
                cur.execute("UPDATE qa_superuser_investigations "
                            "SET commented=TRUE WHERE finding_key=%s", (key,))
    except Exception as e:  # noqa: BLE001
        logger.warning("[qa-superuser] commented-flag update failed: %s", e)
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

    ev_sha = evidence_sha(finding.get("evidence") or "")
    issue_number = finding.get("issue_number")
    meta = {"key": key, "title": finding.get("title"),
            "surface": finding.get("surface"), "seat": finding.get("seat")}

    def _bg():
        try:
            import requests as _rq
            r = _rq.post(url, json={"question": question}, timeout=300,
                         headers={"X-Admin-Key": admin,
                                  "User-Agent": "dchub-qa-superuser/1.0"})
            logger.info("[qa-superuser] investigate -> %s", r.status_code)
            if r.status_code != 200:
                return
            payload = r.json() or {}
            # ★ Flag-off is a 200 with enabled:false and NO result. Storing that
            # would put an empty analysis on the issue and read as "the brain
            # looked and had nothing to say".
            if payload.get("enabled") is False:
                logger.info("[qa-superuser] investigator ships dark — nothing stored")
                return
            result = payload.get("result") or {}
            _persist_investigation(meta, ev_sha, question, result,
                                   payload.get("id"), issue_number)
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
        "note": "DISPATCHED, not finished — the chain runs ~48s, then the "
                "result is stored against this finding and posted as a comment "
                "on its GitHub issue. Reload in a minute.",
        "question": question,
        "will_comment_on": issue_number,
    })


@qa_superuser_dashboard_bp.route("/api/v1/admin/qa-superuser/propose-fix",
                                 methods=["POST"])
def qa_superuser_propose_fix():
    """Turn an investigated finding into a REVIEWABLE PR. Never merges.

    ★ SYNCHRONOUS on purpose, unlike investigate. This one call returns the
    thing the operator is waiting for — a PR URL, or the reason there isn't one —
    and every refusal path below is a reason worth reading. Fire-and-forget would
    turn "refused: 'find' appears 3x, ambiguous" into silence.

    ★ REFUSES MORE OFTEN THAN IT SUCCEEDS, by design. Most findings on this
    platform are not single-string fixes. A lane that always produces a PR would
    be producing wrong ones.
    """
    if not _admin_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    key = (body.get("key") or "").strip()
    if not key:
        return jsonify({"ok": False, "error": "missing key"}), 400

    from tools.qa_superuser import propose as P

    data = _load(limit=1)
    latest = (data.get("latest") or {})
    _attach_investigations(latest)
    finding = next((f for f in (latest.get("findings") or [])
                    if f.get("key") == key), None)
    if finding is None:
        return jsonify({"ok": False,
                        "error": "finding not in the latest run"}), 404

    ok, why = P.gate_investigation(finding.get("investigation"))
    if not ok:
        return jsonify({"ok": False, "error": "not_ready", "reason": why}), 412

    # Ask for a structured edit, grounded in the investigation.
    try:
        from routes.brain_investigator import _call_model
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False,
                        "error": f"investigator unavailable: {e}"}), 503

    text, err, model = _call_model(
        "You propose SURGICAL single-file code fixes, or you decline. Declining "
        "is a correct and frequent answer. You never guess at file contents.",
        P.build_fix_prompt(finding, finding.get("investigation") or {}),
        tier="reasoning", max_tokens=2000, schema=P.FIX_SCHEMA)
    if err or not text:
        return jsonify({"ok": False, "error": f"model call failed: {err}"}), 503
    try:
        from routes.brain_llm_structured import parse_structured_json
        fix = parse_structured_json(text) or {}
    except Exception:  # noqa: BLE001
        fix = {}
    if not fix:
        return jsonify({"ok": False,
                        "error": "could not parse a proposal"}), 502

    # ★ Validate against the REAL file before spending a PR slot. The shared
    #   lane validates too, but failing here costs a 422 instead of a unit of
    #   the daily change budget plus a PR someone has to close.
    path = (fix.get("file") or "").strip().lstrip("/")
    content = None
    if path and ".." not in path and not path.startswith("/"):
        try:
            from routes.brain_pr_opener import _get_file
            content, _ = _get_file(path)
        except Exception as e:  # noqa: BLE001
            logger.warning("[qa-superuser] could not read %s: %s", path, e)
    ok, why = P.validate_fix(fix, content)
    if not ok:
        return jsonify({"ok": False, "error": "refused", "reason": why,
                        "proposal": {k: fix.get(k)
                                     for k in ("file", "rationale", "why_not")},
                        "model": model}), 422

    # Hand it to the lane that already exists, is already guarded, and already
    # ends at a human pressing merge.
    base = ((os.environ.get("DCHUB_INTERNAL_API") or "").strip()
            or (os.environ.get("RAILWAY_BACKEND_URL") or "").strip()
            or "http://127.0.0.1:" + ((os.environ.get("PORT") or "8080").strip()))
    if base and not base.startswith("http"):
        base = "https://" + base
    admin = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    try:
        import requests as _rq
        r = _rq.post(
            base.rstrip("/") + "/api/v1/brain/open-pr-for-finding",
            headers={"X-Admin-Key": admin,
                     "User-Agent": "dchub-qa-superuser/1.0"},
            json={"issue": "generic_find_replace",
                  "pr_title": P.pr_title_for(finding),
                  "url": f"{finding.get('surface')}::{key}",
                  "detail": (finding.get("evidence") or "")[:1500],
                  "file": path, "find": fix.get("find"),
                  "replace": fix.get("replace", "")},
            timeout=60)
        out = r.json() if r.content else {}
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"PR lane failed: {e}"}), 503

    if not out.get("ok"):
        return jsonify({"ok": False, "error": "pr_lane_refused",
                        "status": r.status_code, "detail": out}), 502

    pr_url = out.get("pr_url")
    if finding.get("issue_number") and pr_url:
        _post_issue_comment(
            int(finding["issue_number"]),
            f"### 🔧 Proposed fix\n\n{pr_url}\n\n"
            f"> {(fix.get('rationale') or '').strip()[:600]}\n\n"
            f"_Generated from the investigation above and validated against the "
            f"file ({why}). **Not merged** — review the diff._")
    return jsonify({"ok": True, "pr_url": pr_url,
                    "pr_number": out.get("pr_number"),
                    "file": path, "validated": why,
                    "rationale": fix.get("rationale"), "model": model})


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


def _attach_investigations(latest: dict) -> None:
    """Attach the brain's analysis to each finding it explains.

    ★ Same three-state rule as acks, for the same reason: an investigation
    explains ONE observation. When the evidence moves the analysis is `stale` —
    still worth reading, no longer a description of what is on screen. Rendering
    a stale analysis as current is how you end up acting on last week's cause.
    """
    findings = latest.get("findings") or []
    if not findings:
        return
    rows: dict[str, tuple] = {}
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            _ensure_investigations(cur)
            cur.execute("SELECT finding_key, evidence_sha, recommendation, "
                        "confidence, survived, issue_number, commented, "
                        "created_at FROM qa_superuser_investigations")
            for row in cur.fetchall() or []:
                rows[row[0]] = row[1:]
    except Exception as e:  # noqa: BLE001
        logger.warning("[qa-superuser] investigation read failed: %s", e)
        return
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass

    for f in findings:
        rec = rows.get(f.get("key"))
        if not rec:
            continue
        sha, rec_text, conf, survived, issue_no, commented, at = rec
        f["investigation"] = {
            "state": "current"
            if sha == evidence_sha(f.get("evidence") or "") else "stale",
            "recommendation": rec_text,
            "confidence": float(conf) if conf is not None else None,
            "survived": survived,
            "issue_number": issue_no,
            "commented": bool(commented),
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
    _attach_investigations(latest)
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

  // The brain's analysis, shown where the finding is — with the same stale rule
  // as an ack, and with the refutation verdict FIRST. A recommendation the
  // investigator itself knocked down must not read like an answer.
  const iv = f.investigation;
  const inv = !iv ? '' : `<div class="acked ${iv.state==='stale'?'stale':''}">
      ${iv.state === 'stale'
        ? `<b>🧠 Analysis from ${ago(iv.at)} — the evidence has CHANGED since.</b>
           It explains an older observation than the one above.`
        : `<b>🧠 Analysed ${ago(iv.at)}${iv.confidence!=null
             ? ' · confidence ' + iv.confidence.toFixed(2) : ''}.</b>`}
      ${iv.survived === false
        ? ` <b style="color:var(--amber)">Did NOT survive the brain's own
            refutation — treat as a lead, not an answer.</b>` : ''}
      ${iv.recommendation ? `<div class="row">${esc(iv.recommendation)}</div>` : ''}
      ${iv.commented && iv.issue_number
        ? `<div class="row"><a target="_blank" rel="noopener"
             href="https://github.com/${REPO}/issues/${iv.issue_number}"
             >posted to issue #${iv.issue_number} ↗</a></div>` : ''}
    </div>`;

  // Actions exist only on RED. A gauge makes no claim to act on, and an
  // unobserved finding is a request to look again, not a defect to route.
  // "Propose a fix" appears only once an investigation exists — a diff written
  // from a symptom rather than a cause is the thing this tool refuses to make.
  const canPropose = iv && iv.state === 'current' && iv.survived !== false;
  const acts = f.verdict !== 'RED' ? '' : `<div class="acts" data-key="${esc(f.key)}">
      <button class="btn" data-act="investigate">🧠 Ask the brain</button>
      ${canPropose
        ? `<button class="btn" data-act="propose">🔧 Propose a fix (PR)</button>`
        : ''}
      ${f.issue_number
        ? `<a class="btn done" target="_blank" rel="noopener"
             href="https://github.com/${REPO}/issues/${f.issue_number}"
             >📋 Issue #${f.issue_number} ↗</a>`
        : `<a class="btn" target="_blank" rel="noopener"
             href="${issueUrl(f)}">📋 Open an issue</a>`}
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
    ${ack}${inv}${acts}
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
        ? 'dispatched — the brain answers in ~1 min, onto this card and the issue'
        : ('failed: ' + (r.error || '?'));
      el.classList.toggle('done', !!r.ok);
      el.classList.toggle('warn', !r.ok);
    } else if (kind === 'propose') {
      // ★ Report the REFUSAL REASON, not just failure. This lane declines more
      // often than it succeeds — "'find' appears 3x, ambiguous" is the useful
      // output, and collapsing it to a red button throws away the finding.
      out.textContent = 'writing a patch and validating it against the file…';
      const r = await post('/api/v1/admin/qa-superuser/propose-fix', {key});
      if (r.ok) {
        out.innerHTML = 'PR opened — <a target="_blank" rel="noopener" href="'
          + r.pr_url + '">' + esc(r.pr_url) + '</a> · not merged, review it';
        el.classList.add('done');
      } else {
        out.textContent = 'no PR: ' + (r.reason || r.error || '?');
        el.classList.add('warn');
      }
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
