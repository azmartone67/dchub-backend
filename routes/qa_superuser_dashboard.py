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
from util.json_column import json_for_column

logger = logging.getLogger(__name__)

qa_superuser_dashboard_bp = Blueprint("qa_superuser_dashboard", __name__)

C_REPO = "azmartone67/dchub-backend"
# ★ The ROLLING board issue — the one deduped issue this harness keeps current,
#   and the authoritative board. One source for the link the operator clicks and
#   for the escalation fallback below, so they can never point at different
#   issues. (board.py finds it dynamically by marker; here it is a constant,
#   exactly as the footer link always has been.)
BOARD_ISSUE_NUMBER = 2186
ISSUE_URL = f"https://github.com/{C_REPO}/issues/{BOARD_ISSUE_NUMBER}"
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
            created_at     TIMESTAMPTZ DEFAULT NOW(),
            proposal_state  TEXT,
            proposal_detail TEXT,
            pr_url          TEXT,
            pr_number       INTEGER,
            proposal_at     TIMESTAMPTZ,
            proposal_evidence_sha TEXT
        )"""
    )
    # ★ The live table is not necessarily the table in this file. A CREATE TABLE
    #   IF NOT EXISTS is a no-op against an EXISTING table with an older column
    #   set, so every column added after first deploy needs its own ADD COLUMN —
    #   otherwise the code ships green and every write fails on a missing column.
    for col, ddl in (("proposal_state", "TEXT"), ("proposal_detail", "TEXT"),
                     ("pr_url", "TEXT"), ("pr_number", "INTEGER"),
                     ("proposal_at", "TIMESTAMPTZ"),
                     # When we told a human this finding is stuck. Stamped once
                     # so the escalation never repeats — a channel that re-pages
                     # every 4h gets muted, and then it is not a channel.
                     ("parked_escalated_at", "TIMESTAMPTZ"),
                     # ★★★ THE PROPOSAL NEEDS ITS OWN PROVENANCE. The row's single
                     # `evidence_sha` belongs to the INVESTIGATION and is
                     # overwritten every time the finding is re-analysed, so it
                     # cannot say what evidence a proposal was made against.
                     # Live 2026-09-18: a refusal written 11.9 days earlier
                     # rendered beside a 4.1h-old analysis with no indication it
                     # described different evidence.
                     ("proposal_evidence_sha", "TEXT"),
                     # How many times the SAME evidence was re-analysed and
                     # refuted again. Bounds the refuted-retry below; resets
                     # when the evidence moves or an analysis survives.
                     ("refuted_reruns", "INTEGER NOT NULL DEFAULT 0")):
        cur.execute("ALTER TABLE qa_superuser_investigations "
                    f"ADD COLUMN IF NOT EXISTS {col} {ddl}")


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
# ★ A SEPARATE marker for a run that did not run. Sharing one marker would make
#   the "did not run" note overwrite the very analysis it exists to preserve;
#   sharing none would stack a fresh note on every failed attempt. Two markers,
#   each deduping only against its own kind.
INVESTIGATION_DEGRADED_MARKER = "<!-- qa-superuser:investigation-degraded -->"


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
    cannot = result.get("cannot_investigate")
    marker = INVESTIGATION_DEGRADED_MARKER if cannot else INVESTIGATION_MARKER
    out = [marker,
           "## 🧠 Brain investigation",
           "",
           f"_Finding_ `{finding.get('key', '?')}` — "
           f"{finding.get('title', '(untitled)')}", ""]

    if cannot:
        out += [f"**The investigation did not run:** `{cannot}`", "",
                "This is not a finding about the product — it is the analysis "
                "step failing. The finding itself stands unexplained, and any "
                "earlier analysis on this issue still stands.", ""]
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
                   VALUES (%s, %s, %s, NOW() ON CONFLICT DO NOTHING)
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
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,NOW() ON CONFLICT DO NOTHING)
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
                       refuted_reruns = CASE
                           WHEN qa_superuser_investigations.evidence_sha
                                    = EXCLUDED.evidence_sha
                            AND qa_superuser_investigations.survived IS FALSE
                            AND EXCLUDED.survived IS FALSE
                           THEN COALESCE(qa_superuser_investigations.refuted_reruns, 0) + 1
                           ELSE 0 END,
                       created_at     = NOW()""",
                (key, ev_sha, question[:2000],
                 (result.get("recommendation") or "")[:8000],
                 float(result.get("confidence") or 0.0),
                 ref.get("survived"), json_for_column(result, 200000),
                 brain_id, issue_number),
            )
        stored = True
    except Exception as e:  # noqa: BLE001
        # ★ A DB failure must not ALSO cost the comment. The store and the
        #   comment are two independent delivery channels for the same analysis;
        #   aborting the second because the first failed turns one outage into
        #   total loss of a 48-second investigation. Store-first ordering is kept
        #   — only the abort is removed.
        logger.warning("[qa-superuser] investigation store failed (still "
                       "commenting): %s", e)
        stored = False

    if not issue_number:
        logger.info("[qa-superuser] finding %s has no issue — stored=%s",
                    key, stored)
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
        if ok and stored:
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
        # ★ "not in the latest run" is a CLAIM about the board. When the board
        #   could not be read at all, that claim is unfounded — and it sends the
        #   operator hunting for a vanished finding instead of a broken DB.
        if data.get("error") or not data.get("latest"):
            return jsonify({"ok": False, "error": "board unreadable",
                            "reason": data.get("error")
                            or "no run has been recorded yet"}), 503
        return jsonify({"ok": False,
                        "error": "finding not in the latest run"}), 404

    question = derive_question(finding)

    try:
        import threading
        threading.Thread(target=_run_investigation, args=(finding,),
                         daemon=True).start()
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:200]}), 500

    return jsonify({
        "ok": True,
        "dispatched": True,
        "note": "DISPATCHED, not finished — the chain runs ~48s, then the "
                "result is stored against this finding and posted as a comment "
                "on its GitHub issue. Reload in a minute.",
        "question": question,
        "will_comment_on": finding.get("issue_number"),
    })


def _run_investigation(finding: dict) -> tuple[bool, str]:
    """Investigate ONE finding, synchronously. Returns (stored, detail).

    ★ ONE code path, called by BOTH the button and the automatic lane. It was
      inline in the request handler; the auto lane would have had to re-implement
      the flag-off check, the `cannot_investigate` rule and the persist call, and
      a second copy of a rule this delicate drifts. Same reason `repo_path()`
      exists in propose.py: a guard that inspects one thing while another is
      applied is not a guard.

    Never raises — a caller iterating a list must not lose the rest of the list
    to one bad finding.
    """
    key = finding.get("key")
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

    try:
        import requests as _rq
        r = _rq.post(url, json={"question": question}, timeout=300,
                     headers={"X-Admin-Key": admin,
                              "User-Agent": "dchub-qa-superuser/1.0"})
        logger.info("[qa-superuser] investigate -> %s", r.status_code)
        if r.status_code != 200:
            return False, f"brain returned HTTP {r.status_code}"
        payload = r.json() or {}
        # ★ Flag-off is a 200 with enabled:false and NO result. Storing that
        # would put an empty analysis on the issue and read as "the brain
        # looked and had nothing to say".
        if payload.get("enabled") is False:
            logger.info("[qa-superuser] investigator ships dark — nothing stored")
            return False, "the investigator ships dark (enabled:false)"
        result = payload.get("result") or {}
        # ★★★ A RUN THAT DID NOT RUN MUST NOT OVERWRITE ONE THAT DID.
        # `cannot_investigate` (no API key, model timeout) comes back as a
        # 200 with a result whose recommendation is None and confidence 0.0.
        # Stored, it replaced a good prior analysis and then rendered in the
        # green "analysed" box as "confidence 0.00" — a real answer erased by
        # a non-answer, and the non-answer displayed as an answer. BLIND is
        # not a verdict; it is the absence of one. Comment so the operator
        # learns the analysis step is broken, but leave the row alone.
        if result.get("cannot_investigate"):
            logger.warning("[qa-superuser] investigation did not run: %s",
                           result.get("cannot_investigate"))
            if issue_number:
                _post_issue_comment(
                    int(issue_number),
                    render_investigation_comment(meta, result),
                    marker=INVESTIGATION_DEGRADED_MARKER)
            return False, f"cannot_investigate: {result.get('cannot_investigate')}"
        _persist_investigation(meta, ev_sha, question, result,
                               payload.get("id"), issue_number)
        return True, "stored"
    except Exception as e:  # noqa: BLE001
        logger.warning("[qa-superuser] investigate failed: %s", str(e)[:200])
        return False, f"{type(e).__name__}: {str(e)[:160]}"


# ── the automatic lane ──────────────────────────────────────────────────────
# Investigate, never propose. Every argument in propose.py for keeping the merge
# button human applies to generating a DIFF without being asked; none of them
# applies to READING. An investigation writes one row and one issue comment,
# changes no behaviour, and its whole value is being finished before a human
# opens the board.
AUTO_INVESTIGATE_DEFAULT_LIMIT = 3
AUTO_INVESTIGATE_MAX_LIMIT = 10
# A board this old describes a platform that has moved. Matches the dashboard's
# own staleness banner rather than inventing a second threshold.
AUTO_INVESTIGATE_MAX_BOARD_AGE_H = 9.0

# ★★★ WHY A COOLDOWN AND NOT JUST `state != current`.
#
# An investigation is bound to `evidence_sha`, so it goes STALE the moment the
# evidence string changes — which for some findings is EVERY RUN, by design.
# The quota-meter check rotates the tool it spends (the anon cap is keyed on
# (ip, tool, day), so a fixed tool can only be observed once a day), and its
# evidence names that tool and its numbers. Different tool, different string,
# different sha, stale investigation, eligible again — for a finding that has
# already crossed the pass/fail line 9 times.
#
# Left alone that flapper sits FIRST in board order and eats one of three slots
# every 4h forever: a ~48s model call re-explaining a known-unstable finding,
# while a genuinely new red waits for the next run. Staleness is the right rule
# for "should a human re-read this?" and the wrong one for "should we spend the
# budget again?".
#
# A brand-new finding has no prior row and is never delayed by this.
AUTO_INVESTIGATE_COOLDOWN_H = 12.0

# ★★ A REFUTED analysis that is still CURRENT used to be final: the lane
#   skipped it ("already has a current investigation") until the evidence
#   string moved, which for a steady RED is never. The propose gate stays
#   exactly as strict — this only buys the finding another ANALYSIS, spaced and
#   capped, so a first draft that got knocked down is not the last word. The
#   park/hand-off to the agent lane still happens immediately (park_verdict is
#   unchanged); a later analysis that SURVIVES simply un-parks it.
REFUTED_RETRY_H = 24.0
REFUTED_MAX_RERUNS = 2


def is_actionable_finding(f: dict) -> bool:
    """Does this board finding represent REAL WORK?

    Eligible = the two classes a human would click, and no others:
      * an OBSERVED failure — RED at critical/major, and
      * an INSTRUMENT FAULT — our own probe broken, which is invisible
        anywhere but this board.
    A GAUGE makes no pass/fail claim to investigate. A genuinely-unobserved
    (BLIND) surface is a request to look again, not a defect.

    ★ SHARED, and that is the point. Two lanes consume this board — the
    auto-investigate dispatcher below, and brain_qa_superuser_intake, which
    seeds the brain's worklist. If they disagreed about what counts as real
    work, one of them would be spending budget on findings the other had
    decided were not evidence, and nothing would report the disagreement.

    Literals, not imports: findings arrive as JSONB from the board, and
    `tools.qa_superuser` is imported lazily elsewhere in this module precisely
    because it is not guaranteed importable in the deployed backend.
    """
    return bool(
        (f.get("verdict") == "RED"
         and f.get("severity") in ("critical", "major"))
        or f.get("instrument_fault")
    )


def _mark_parked_escalated(key: str) -> bool:
    """Stamp that we have handed this finding to a human. Never raises."""
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            _ensure_investigations(cur)
            cur.execute("UPDATE qa_superuser_investigations "
                        "SET parked_escalated_at=NOW() WHERE finding_key=%s",
                        (key,))
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[qa-superuser] parked stamp failed for %s: %s", key, e)
        return False
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def parked_candidates(findings: list[dict], park) -> list[dict]:
    """Actionable findings that are STUCK and have not been escalated yet.

    Pure; `park` is `tools.qa_superuser.propose.park_verdict`, injected for the
    same reason the propose gate is — one definition, no drift.
    """
    out = []
    for f in findings or []:
        if not is_actionable_finding(f):
            continue
        # ★ Unreadable is not "not escalated". On a DB blip every finding would
        #   look un-escalated and the lane would re-page the human for all of
        #   them at once — the one way to guarantee this channel gets ignored.
        if f.get("investigation_unreadable"):
            continue
        if f.get("parked_escalated_at"):
            continue
        parked, why = park(f)
        if parked:
            out.append((f, why))
    return out


def _escalate_parked(latest: dict) -> dict:
    """Tell a human, ONCE, about every red the loop has stopped trying on.

    ★★★ WHY THIS EXISTS. A refuted investigation is CURRENT, so the investigate
    lane skips it forever ("already has a current investigation") and the
    propose gate refuses it forever (a refuted recommendation must not become a
    diff). Both are correct. Together they are a dead end that nothing reports:
    the finding stays RED, gets re-skipped every 4h, and the loop's silence is
    indistinguishable from the loop working.

    ★ It writes to the GITHUB ISSUE, not just this page. The issue is the
      authoritative board — it survives a dchub outage, which is exactly the
      condition under which a stuck critical matters most.

    Best-effort in every direction: a failure here must never take down the
    investigate lane it rides on.
    """
    try:
        from tools.qa_superuser import propose as P
    except Exception as e:  # noqa: BLE001
        return {"escalated": [], "error": f"propose module unavailable: "
                                          f"{type(e).__name__}: {str(e)[:120]}"}

    done, failed = [], []
    for f, why in parked_candidates(latest.get("findings") or [], P.park_verdict):
        key, issue_no = f.get("key"), f.get("issue_number")
        # ★★★ A FINDING WITHOUT ITS OWN ISSUE STILL HAS TO REACH SOMEONE.
        #   `issue_number` is only ever set for findings a human already clicked
        #   "Open an issue" on (board.py fills it from open issues carrying the
        #   key marker). Measured live 2026-09-19: the one parked finding on the
        #   board had issue_number=None. So the first version of this lane could
        #   only page about findings someone was ALREADY watching — it reported
        #   the rest as "no issue to comment on" and moved on, which is the
        #   detect-without-actuate leak this whole change exists to close,
        #   committed by the fix for it.
        #
        #   The rolling board issue always exists and is the authoritative
        #   board, so it is the channel of last resort. Never a dead end.
        to_issue, via_board = issue_no, False
        if not to_issue:
            to_issue, via_board = BOARD_ISSUE_NUMBER, True
        inv = f.get("investigation") or {}
        body = (
            "### ⏸ The automated loop has stopped on this finding\n\n"
            f"**{f.get('title') or key}**\n\n"
            f"{why}.\n\n"
            "So this will not move on its own: it is not eligible for "
            "re-analysis (its investigation is current) and it is not eligible "
            "for a proposed fix (the gate refuses it, correctly — a "
            "recommendation the brain's own refutation knocked down must not "
            "become a diff).\n\n"
            f"**What was observed**\n\n> {(f.get('evidence') or '')[:600]}\n\n"
            + (f"**The analysis that did not hold**"
               f"{' (confidence ' + format(inv['confidence'], '.2f') + ')' if inv.get('confidence') is not None else ''}"
               f"\n\n> {(inv.get('recommendation') or '')[:600]}\n\n"
               if inv.get("recommendation") else "")
            + "**This needs a human decision.** The usual answers are: the "
              "finding is real and needs a fix the loop cannot write; the "
              "check itself is wrong and should be changed; or the condition "
              "is acceptable and the check should be retired.\n\n"
              "_Posted once. This comment will not repeat._")
        if via_board:
            body = (body.replace(
                "### ⏸ The automated loop has stopped on this finding",
                "### ⏸ The automated loop has stopped on a finding")
                + f"\n\n_Posted here because `{key}` has no issue of its own. "
                  "Open one from the board if you want the discussion "
                  "separate._")
        if _post_issue_comment(int(to_issue), body):
            if _mark_parked_escalated(key):
                done.append({"key": key, "issue": int(to_issue),
                             "via_board_issue": via_board})
            else:
                # Posted but not stamped — report it, because the next run will
                # post again and a duplicate is the visible symptom.
                failed.append({"key": key, "why": "commented but stamp failed"})
        else:
            failed.append({"key": key, "why": "issue comment failed"})
    return {"escalated": done, "failed": failed}


def auto_investigate_candidates(findings: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split findings into (to investigate, skipped-with-reason).

    Pure, so the selection rule is testable without a database or a brain.

    Eligibility is `is_actionable_finding` above — an observed RED at
    critical/major, or an instrument fault. Everything below it is about
    whether an ELIGIBLE finding is worth model budget RIGHT NOW (already
    analysed? still in cooldown?), which is this lane's question alone; the
    brain intake shares the eligibility rule but not these.
    """
    todo, skipped = [], []
    for f in findings:
        key = f.get("key")
        if not is_actionable_finding(f):
            continue  # not a candidate at all — silent, not "skipped"

        # ★ UNREADABLE IS NOT "ABSENT", and the flag is a SEPARATE KEY —
        #   `_attach_investigations` sets `investigation_unreadable` on the
        #   finding and leaves `investigation` absent entirely. Reading it as a
        #   state on the investigation dict would make this guard dead code and
        #   send every finding down the "never investigated" path on any DB
        #   blip: the chain re-runs and stacks a second wall of analysis on the
        #   issue. We cannot tell an un-investigated finding from one analysed
        #   an hour ago, so we do not dispatch.
        if f.get("investigation_unreadable"):
            skipped.append({"key": key, "why": "investigation state unreadable "
                                               "— cannot tell if it was already "
                                               "analysed, so not dispatching"})
            continue
        inv = f.get("investigation") or {}
        if inv.get("state") == "current":
            if inv.get("survived") is False:
                reruns = int(inv.get("refuted_reruns") or 0)
                r_age = _age_hours(inv.get("at")) if inv.get("at") else None
                if reruns >= REFUTED_MAX_RERUNS:
                    skipped.append({
                        "key": key,
                        "why": f"refuted {reruns + 1}x against this same "
                               "evidence — retries exhausted, handed off"})
                    continue
                if r_age is not None and r_age >= REFUTED_RETRY_H:
                    todo.append(f)
                    continue
                skipped.append({
                    "key": key,
                    "why": "current analysis was refuted; re-analysis due "
                           f"{REFUTED_RETRY_H:.0f}h after it "
                           f"({reruns}/{REFUTED_MAX_RERUNS} retries used)"})
                continue
            skipped.append({"key": key,
                            "why": "already has a current investigation"})
            continue

        age = _age_hours(inv.get("at")) if inv.get("at") else None
        if age is not None and age < AUTO_INVESTIGATE_COOLDOWN_H:
            skipped.append({
                "key": key,
                "why": f"investigated {age:.1f}h ago (cooldown "
                       f"{AUTO_INVESTIGATE_COOLDOWN_H}h) — its evidence moved, "
                       "but re-analysing this often spends budget without "
                       "learning anything new"})
            continue
        todo.append(f)
    return todo, skipped


# ── auto-propose ───────────────────────────────────────────────
#
# ONE candidate per run by default. This lane spends a reasoning-tier model call
# and a unit of the daily PR budget per candidate, and unlike the investigate
# lane its output is a branch and a pull request. Six runs a day × 1 is the most
# it can produce, before the PR-opener's own `can_open_pr()` budget.
AUTO_PROPOSE_DEFAULT_LIMIT = 1
AUTO_PROPOSE_MAX_LIMIT = 3


def squasher_owns_qa_reds() -> bool:
    """Is the squasher agent lane the actor for QA reds? (2026-09-25)

    That lane files every red whose investigation passes the SAME gate this
    endpoint uses (routes/squasher_agent_lane.qa_ready_basis) and fixes it
    with a tool-using agent that can edit any file in dchub-backend or
    dchub-mcp-server; this endpoint's proposer makes one find-and-replace in
    one backend file. Two actors on one red is two PRs for one defect, so this
    lane stands down while that one is enabled, and resumes by itself when it
    is disabled (SQUASHER_AGENT_DISABLE / SQUASHER_QUEUE_DISABLE).
    QA_AUTO_PROPOSE_ALONGSIDE_SQUASHER=1 restores the old both-lanes shape.
    The dashboard's human "Propose fix" button is not affected."""
    if (os.environ.get("QA_AUTO_PROPOSE_ALONGSIDE_SQUASHER") or "").strip() == "1":
        return False
    try:
        from routes.squasher_agent_lane import _disabled
    except Exception:  # noqa: BLE001 — no lane to defer to
        return False
    return not _disabled()


def auto_propose_candidates(findings: list[dict], gate) -> tuple[list, list]:
    """Split findings into (to propose, skipped-with-reason). Pure.

    `gate` is `tools.qa_superuser.propose.gate_investigation` — INJECTED rather
    than imported, so this is testable without the tools tree AND so there is
    exactly ONE definition of "fit to generate code from", shared with the
    dashboard button and the propose endpoint's own check. A second copy of that
    rule would drift, and the drift would be a lane proposing from evidence the
    server would refuse.

    Eligibility is `is_actionable_finding` — the SAME predicate the investigate
    lane and the brain intake use. On top of it:
      * the investigation must pass the gate (exists, current, survived
        refutation, has a recommendation);
      * the finding must have NO proposal yet.
    """
    todo, skipped = [], []
    for f in findings or []:
        key = f.get("key")
        if not is_actionable_finding(f):
            continue  # not a candidate at all — silent, not "skipped"

        # ★ Same trap as the investigate lane: `investigation_unreadable` is a
        #   SEPARATE KEY and `investigation` is absent entirely when the DB
        #   could not be read. Without this, a DB blip reads as "no proposal
        #   yet" on every finding and the lane opens a PR per red on recovery.
        if f.get("investigation_unreadable"):
            skipped.append({"key": key,
                            "why": "investigation state unreadable — cannot "
                                   "tell what was already proposed, so not "
                                   "dispatching"})
            continue

        # ★★★ ONE AUTONOMOUS ATTEMPT PER FINDING, EVER. Deliberately blunt.
        #   A refusal ("'find' appears 3× — ambiguous") is a property of the
        #   file, not of the moment; re-running the same model against the same
        #   shape buys the same answer and another unit of budget. If the
        #   evidence genuinely moves, a human can still click the button — the
        #   lane degrades to today's behaviour, never to silence with no
        #   recourse.
        #   ★ A finer rule — "already proposed against THIS evidence" — becomes
        #     possible once `proposal_evidence_sha` lands (be#4775). Not built
        #     on it here so this ships un-stacked.
        if f.get("proposal"):
            pp = f.get("proposal") or {}
            skipped.append({
                "key": key,
                "why": f"a proposal already exists for this finding "
                       f"(state={pp.get('state')!r}) — this lane makes one "
                       f"attempt per finding; re-proposing is a human decision"})
            continue

        ok, why = gate(f.get("investigation"))
        if not ok:
            skipped.append({"key": key, "why": why})
            continue
        todo.append(f)
    return todo, skipped


@qa_superuser_dashboard_bp.route(
    "/api/v1/admin/qa-superuser/auto-propose", methods=["POST"])
def qa_superuser_auto_propose():
    """Write a patch for every red whose CAUSE is already established.

    ★ WHAT THIS CHANGES. `auto-investigate` stops at the analysis by design and
      says so. That left the loop with no actuator: a finding could be red for
      days, analysed every 12h, and never routed anywhere, because `propose` was
      a button on a page nobody had open. This is that actuator.

    ★★★ IT OPENS A **DRAFT**, AND THAT IS THE SAFETY CONTROL.
      `.github/workflows/auto-enable-automerge.yml` arms GitHub native
      auto-merge on every NON-DRAFT PR at open, and skips drafts precisely so a
      propose-only lane stays propose-only. A non-draft PR from this lane would
      merge to main on green and deploy, with nobody having read the diff — and
      the board's own footer promises the opposite. Draft keeps that promise
      true. `brain_automerge` does not reach these either: it filters head
      branches starting `brain/autofix-`, and the opener names ours
      `brain/fix-…`.

    ★ IT NEVER MERGES AND NEVER DEPLOYS. It opens a draft for a human.

    Refuses, loudly and with a reason, on every condition the investigate lane
    refuses on — kill switch, unreadable board, stale board, unfired canary —
    because a proposal is strictly more expensive than an analysis and must not
    have a weaker gate than the thing it depends on.
    """
    if not _admin_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    if (os.environ.get("QA_AUTO_PROPOSE") or "").strip() in ("0", "off",
                                                             "false"):
        return jsonify({"ok": False, "refused": "kill switch",
                        "reason": "QA_AUTO_PROPOSE is off"}), 200

    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get("dry_run"))
    try:
        limit = int(body.get("limit") or AUTO_PROPOSE_DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = AUTO_PROPOSE_DEFAULT_LIMIT
    limit = max(1, min(limit, AUTO_PROPOSE_MAX_LIMIT))

    # ★ REFUSE RATHER THAN PROPOSE UNGATED. If the gate cannot be loaded there
    #   is no way to tell a sound investigation from a refuted one, and the
    #   failure-safe direction for a lane that opens PRs is to do nothing.
    try:
        from tools.qa_superuser import propose as P
    except Exception as e:  # noqa: BLE001
        logger.error("[qa-superuser] tools/ not importable under gunicorn: %s", e)
        return jsonify({"ok": False, "refused": "propose lane unavailable",
                        "reason": f"tools.qa_superuser.propose import failed: "
                                  f"{type(e).__name__}: {str(e)[:160]}"}), 503

    data = _load(limit=1)
    latest = data.get("latest") or {}
    if data.get("error") or not latest:
        return jsonify({"ok": False, "refused": "board unreadable",
                        "reason": data.get("error")
                        or "no run has been recorded yet"}), 503

    age = _age_hours(latest.get("generated_at"))
    if age is not None and age > AUTO_INVESTIGATE_MAX_BOARD_AGE_H:
        return jsonify({"ok": False, "refused": "board is stale",
                        "reason": f"the latest run is {age:.1f}h old (limit "
                                  f"{AUTO_INVESTIGATE_MAX_BOARD_AGE_H}h) — a "
                                  "patch written for it would fix a platform "
                                  "that has since moved",
                        "stale_hours": age}), 200

    if not latest.get("canary_fired"):
        return jsonify({"ok": False, "refused": "must-fail control did not fire",
                        "reason": "the harness could not be shown capable of "
                                  "reporting a failure on this run; a diff "
                                  "written from an untrusted run is worse than "
                                  "no diff"}), 200

    _attach_investigations(latest)
    todo, skipped = auto_propose_candidates(latest.get("findings") or [],
                                            P.gate_investigation)
    handed_to = None
    if todo and squasher_owns_qa_reds():
        handed_to = "squasher_agent_lane"
        skipped += [{"key": f.get("key"),
                     "why": "handed to the squasher agent lane, which files "
                            "this red on its next claim — one actor per red"}
                    for f in todo]
        todo = []
    deferred = todo[limit:]
    todo = todo[:limit]

    if dry_run:
        return jsonify({
            "ok": True, "dry_run": True,
            "would_dispatch": [f.get("key") for f in todo],
            "deferred_to_next_run": [f.get("key") for f in deferred],
            "handed_to": handed_to,
            "skipped": skipped})

    dispatched = []
    for f in todo:
        key = f.get("key")
        # ★ Stamped with the evidence THIS run selected on, exactly as the
        #   human-clicked dispatcher does. Without it an autonomous proposal
        #   has no provenance, and its outcome would read as current forever
        #   however far the finding later moved — the defect #4775 fixed, which
        #   this lane would have quietly reintroduced on its own path.
        _mark_proposal(key, "running", "writing a patch and validating it",
                       evidence=f.get("evidence") or "")
        meta = {"key": key, "title": f.get("title"),
                "surface": f.get("surface"), "seat": f.get("seat"),
                "evidence": f.get("evidence"),
                "red_when": f.get("red_when"),
                "issue_number": f.get("issue_number"),
                "investigation": f.get("investigation"),
                # ★ The flag the opener turns into draft=True. Set HERE, at the
                #   only place that dispatches without a human, rather than
                #   defaulting it on in _run_proposal — a human who clicked the
                #   button has already made the decision draft exists to defer.
                "auto": True}
        try:
            import threading
            threading.Thread(target=_run_proposal, args=(meta,),
                             daemon=True).start()
            dispatched.append(key)
        except Exception as e:  # noqa: BLE001
            _mark_proposal(key, "error", f"could not dispatch: {e}",
                           evidence=f.get("evidence") or "")

    return jsonify({
        "ok": True,
        "dispatched": dispatched,
        "deferred_to_next_run": [f.get("key") for f in deferred],
        "handed_to": handed_to,
        "skipped": skipped,
        "note": "DISPATCHED, not finished. Each writes a patch, validates it "
                "against the real file and opens a DRAFT pull request. This "
                "lane never marks a PR ready, never merges and never deploys.",
    })


@qa_superuser_dashboard_bp.route(
    "/api/v1/admin/qa-superuser/auto-investigate", methods=["POST"])
def qa_superuser_auto_investigate():
    """Investigate every red the board is carrying that nobody has explained.

    ★ THE GAP THIS CLOSES. `investigate` and `propose-fix` are BUTTONS. Nothing
      called them on a schedule, so a critical red waited for a human to open a
      page — and a crashed probe waited forever, because until #2503 its card
      had no buttons at all. "I open the issues but they just sit there" was
      still true one layer up from where propose.py answered it.

    ★ IT STOPS AT THE ANALYSIS. It does not propose, open a PR, merge or deploy.
      The diff stays a human decision for the reason propose.py documents at
      length: the edge-caching finding's own remedy named TWO opposite fixes,
      and picking wrong re-creates the Neon stampede.

    Refuses, loudly and with a reason, when:
      * the kill switch is set (QA_AUTO_INVESTIGATE=0);
      * the board cannot be read — never guess at what is red;
      * the board is stale (> 9h) — that analysis would explain a platform that
        has since moved, which is exactly what `state: stale` exists to flag;
      * the must-fail control did not fire. Reds do survive an untrusted run by
        rule, so a human may still click. But an UNATTENDED lane spending model
        budget on a run the harness itself says cannot be trusted is the wrong
        default — fix the harness first.
    """
    if not _admin_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    if (os.environ.get("QA_AUTO_INVESTIGATE") or "").strip() in ("0", "off",
                                                                 "false"):
        return jsonify({"ok": False, "refused": "kill switch",
                        "reason": "QA_AUTO_INVESTIGATE is off"}), 200

    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get("dry_run"))
    try:
        limit = int(body.get("limit") or AUTO_INVESTIGATE_DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = AUTO_INVESTIGATE_DEFAULT_LIMIT
    limit = max(1, min(limit, AUTO_INVESTIGATE_MAX_LIMIT))

    data = _load(limit=1)
    latest = data.get("latest") or {}
    if data.get("error") or not latest:
        return jsonify({"ok": False, "refused": "board unreadable",
                        "reason": data.get("error")
                        or "no run has been recorded yet"}), 503

    age = _age_hours(latest.get("generated_at"))
    if age is not None and age > AUTO_INVESTIGATE_MAX_BOARD_AGE_H:
        return jsonify({"ok": False, "refused": "board is stale",
                        "reason": f"the latest run is {age:.1f}h old (limit "
                                  f"{AUTO_INVESTIGATE_MAX_BOARD_AGE_H}h) — an "
                                  "analysis of it would explain a platform "
                                  "that has since moved",
                        "stale_hours": age}), 200

    if not latest.get("canary_fired"):
        return jsonify({"ok": False, "refused": "must-fail control did not fire",
                        "reason": "the harness could not be shown capable of "
                                  "reporting a failure on this run; fix the "
                                  "harness before spending model budget on it"
                        }), 200

    _attach_investigations(latest)
    todo, skipped = auto_investigate_candidates(latest.get("findings") or [])

    # ★ A cap that silently drops work reads as "everything was handled". Say
    #   what was deferred and how much — the next run picks it up.
    deferred = todo[limit:]
    todo = todo[:limit]

    if dry_run:
        # Name who WOULD be escalated without writing a comment or a stamp.
        # ★★★ THE PREVIEW MUST NAME THE SAME CHANNEL THE RUN WILL USE. The
        #   first version listed bare keys, so it said "would escalate X" about
        #   a finding the real pass then reported as un-escalatable. A dry run
        #   that is more optimistic than the wet run is worse than none: it is
        #   the only thing an operator reads before arming something.
        try:
            from tools.qa_superuser import propose as _P
            would_park = [
                {"key": f.get("key"),
                 "issue": int(f.get("issue_number") or BOARD_ISSUE_NUMBER),
                 "via_board_issue": not f.get("issue_number")}
                for f, _w in parked_candidates(
                    latest.get("findings") or [], _P.park_verdict)]
        except Exception:  # noqa: BLE001
            would_park = None
        return jsonify({
            "ok": True, "dry_run": True,
            "would_dispatch": [f.get("key") for f in todo],
            "deferred_to_next_run": [f.get("key") for f in deferred],
            "would_escalate_parked": would_park,
            "skipped": skipped})

    def _bg():
        # ★ SEQUENTIAL, deliberately. Each investigation is a ~48s model call;
        #   firing the whole board at once would put N of them in gunicorn
        #   threads against a small pool, which is how the QA lane would become
        #   the outage it exists to detect.
        for f in todo:
            stored, detail = _run_investigation(f)
            logger.info("[qa-superuser] auto-investigate %s -> %s (%s)",
                        f.get("key"), "stored" if stored else "not stored",
                        detail)

    try:
        import threading
        threading.Thread(target=_bg, daemon=True).start()
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:200]}), 500

    # ★ AFTER the dispatch, and deliberately not gated on it. These findings are
    #   the ones this lane has ALREADY decided not to analyse; whether today's
    #   dispatch succeeded has no bearing on whether a human should be told the
    #   loop is stuck on them.
    parked = _escalate_parked(latest)

    return jsonify({
        "ok": True,
        "dispatched": [f.get("key") for f in todo],
        "deferred_to_next_run": [f.get("key") for f in deferred],
        "skipped": skipped,
        "parked_escalated": parked,
        "note": "DISPATCHED, not finished — each runs ~48s, sequentially. "
                "Results are stored per finding and commented on their issues. "
                "This lane never proposes, opens a PR, merges or deploys. It "
                "DOES comment once on any red it has permanently stopped "
                "trying on, so a dead end reaches a human instead of being "
                "re-skipped in silence.",
    })


def _mark_proposal(key: str, state: str, detail: str,
                   pr_url: str | None = None, pr_number=None,
                   evidence: str | None = None) -> None:
    """Record where a proposal got to. Best-effort; never raises.

    ★ This row IS the delivery channel for the refusal reason. Going async moved
    the outcome out of the HTTP response, so if this write is skipped the
    operator gets a spinner that never resolves — which is exactly the "issues
    just sit there" shape this whole change exists to remove.
    """
    c = _conn()
    if c is None:
        logger.warning("[qa-superuser] no db — proposal state %r lost", state)
        return
    try:
        with c.cursor() as cur:
            _ensure_investigations(cur)
            cur.execute(
                """UPDATE qa_superuser_investigations
                      SET proposal_state=%s, proposal_detail=%s,
                          pr_url=COALESCE(%s, pr_url),
                          pr_number=COALESCE(%s, pr_number),
                          proposal_at=NOW(),
                          proposal_evidence_sha=
                              COALESCE(%s, proposal_evidence_sha)
                    WHERE finding_key=%s""",
                (state, (detail or "")[:2000], pr_url, pr_number,
                 evidence_sha(evidence) if evidence is not None else None,
                 key))
    except Exception as e:  # noqa: BLE001
        logger.warning("[qa-superuser] proposal state write failed: %s", e)
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def _run_proposal(meta: dict) -> None:
    """The slow half of propose-fix: write a patch, validate it, open a PR.

    Runs on a daemon thread so the CF edge's 15s budget cannot turn a real
    outcome into a false "no PR". EVERY exit path writes a proposal state —
    a silent return here is indistinguishable from work still in progress.
    """
    key = meta["key"]

    # ★★★ ONE WRITER, SO NO EXIT PATH CAN FORGET THE PROVENANCE. Every
    # `_mark_proposal` below goes through this, which means the evidence this
    # proposal was made against is stamped on the row by construction rather
    # than by each of six call sites remembering to pass it. A refusal whose
    # provenance is missing is exactly the row that later reads as current.
    def _mark(state: str, detail: str, pr_url=None, pr_number=None) -> None:
        # ★ Calls the MODULE-LEVEL writer. An earlier revision of this closure
        #   called `_mark` here — itself — and recursed until RecursionError on
        #   every single proposal outcome. It survived its own test suite
        #   because nothing there drove _run_proposal end to end; the draft
        #   tests, which do, caught it on the first run.
        _mark_proposal(key, state, detail, pr_url, pr_number,
                       evidence=meta.get("evidence") or "")

    try:
        from tools.qa_superuser import propose as P
        from routes.brain_investigator import _call_model

        text, err, model = _call_model(
            "You propose SURGICAL single-file code fixes, or you decline. "
            "Declining is a correct and frequent answer. You never guess at "
            "file contents.",
            P.build_fix_prompt(meta, meta.get("investigation") or {}),
            tier="reasoning", max_tokens=2000, schema=P.FIX_SCHEMA)
        if err or not text:
            _mark("error", f"model call failed: {err}")
            return

        fix = _parse_proposal(text)
        if not fix:
            _mark("error",
              "the model's reply could not be parsed as a proposal")
            return

        # ★ Resolve the path through the SAME helper the validator uses, so the
        #   path that is checked is the path that gets written.
        path, why = P.repo_path(fix.get("file") or "")
        content = None
        if path is not None:
            try:
                from routes.brain_pr_opener import _get_file
                content, _ = _get_file(path)
            except Exception as e:  # noqa: BLE001
                logger.warning("[qa-superuser] could not read %s: %s", path, e)
        ok, why = P.validate_fix(fix, content)
        if not ok:
            _mark("refused", why)
            return

        base = ((os.environ.get("DCHUB_INTERNAL_API") or "").strip()
                or (os.environ.get("RAILWAY_BACKEND_URL") or "").strip()
                or "http://127.0.0.1:"
                + ((os.environ.get("PORT") or "8080").strip()))
        if base and not base.startswith("http"):
            base = "https://" + base
        import requests as _rq
        r = _rq.post(
            base.rstrip("/") + "/api/v1/brain/open-pr-for-finding",
            headers={"X-Admin-Key": (os.environ.get("DCHUB_ADMIN_KEY") or "").strip(),
                     "User-Agent": "dchub-qa-superuser/1.0"},
            json={"issue": "generic_find_replace",
                  "pr_title": P.pr_title_for(meta),
                  "url": f"{meta.get('surface')}::{key}",
                  "detail": (meta.get("evidence") or "")[:1500],
                  "file": path, "find": fix.get("find"),
                  "replace": fix.get("replace", ""),
                  # ★★★ A PR opened WITHOUT a human gets auto-merge armed on it
                  #   the moment it opens (auto-enable-automerge.yml fires on
                  #   every non-draft PR) and would land on main and deploy on
                  #   green. Draft is what keeps "every success is a PR for you
                  #   to review" true for this lane.
                  "draft": bool(meta.get("auto"))},
            timeout=90)
        try:
            out = r.json() if r.content else {}
        except Exception:  # noqa: BLE001
            out = {}

        if not out.get("ok"):
            # ★ Forward the lane's OWN explanation. Collapsing "duplicate: an
            #   open PR already exists" and "autonomy_gate_closed: daily budget
            #   spent" into one opaque token throws away the only actionable
            #   part — and they call for opposite responses.
            detail = (out.get("reason") or out.get("error")
                      or f"HTTP {r.status_code}: {r.text[:200]}")
            _mark("refused", f"the PR lane declined: {detail}")
            return

        pr_url = out.get("pr_url")

        # ★★★ VERIFY THE DRAFT, DO NOT ASSUME IT. `out["draft"]` is echoed from
        #   GitHub's own response. If we asked for a draft and did not get one,
        #   auto-merge is ALREADY armed on that PR and the safety property this
        #   lane depends on is gone. Say so on the card in the loudest terms
        #   available rather than recording a clean "opened" — a silently
        #   non-draft autonomous PR is the one outcome nobody would go looking
        #   for. An older backend that ignores the field lands here too, which
        #   is the point: the check reads the RESULT, not the request.
        if meta.get("auto") and not out.get("draft"):
            _mark("error",
                  f"OPENED BUT NOT A DRAFT — {pr_url}. This lane requested a "
                  f"draft; GitHub reports draft={out.get('draft')!r}. "
                  f"auto-enable-automerge arms native auto-merge on non-draft "
                  f"PRs, so this may merge and deploy without review. Convert "
                  f"it to a draft or close it.", pr_url, out.get("pr_number"))
            logger.error("[qa-superuser] auto proposal opened NON-DRAFT: %s",
                         pr_url)
            return

        _mark("opened", f"validated: {why}", pr_url, out.get("pr_number"))
        if meta.get("issue_number") and pr_url:
            _post_issue_comment(
                int(meta["issue_number"]),
                f"### 🔧 Proposed fix\n\n{pr_url}\n\n"
                f"> {(fix.get('rationale') or '').strip()[:600]}\n\n"
                f"_Generated from the investigation above and validated against "
                f"the file ({why}). **Not merged** — review the diff._")
    except Exception as e:  # noqa: BLE001
        logger.exception("[qa-superuser] proposal failed for %s", key)
        _mark("error", f"{type(e).__name__}: {str(e)[:300]}")


def _parse_proposal(text: str) -> dict:
    """Parse the model's reply, strict first then lenient.

    ★ `_call_model` is documented to FAIL SOFT: a 400 on a structured attempt
    retries the same model with the LEGACY free-text body. So a reply may arrive
    as prose-wrapped or fenced JSON even though a schema was passed.
    `parse_structured_json` is strict by design and returns None for those,
    which would report "could not parse a proposal" for a perfectly good fix.
    """
    try:
        from routes.brain_llm_structured import parse_structured_json
        got = parse_structured_json(text)
        if isinstance(got, dict) and got:
            return got
    except Exception:  # noqa: BLE001
        pass
    # Lenient fallback: strip a ``` fence, then take the outermost {...}.
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    i, j = body.find("{"), body.rfind("}")
    if i == -1 or j <= i:
        return {}
    try:
        got = json.loads(body[i:j + 1])
        return got if isinstance(got, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


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

    # ★ First route in the repo to import from tools/ — every other consumer of
    #   that tree runs on the GH Actions runner, not under gunicorn. CWD is the
    #   repo root and gunicorn puts it on sys.path, so this resolves; but an
    #   unproven deployment assumption deserves a legible failure rather than a
    #   500 and a traceback someone has to go find. ("isolated = LOGIC,
    #   prod = DEPLOYMENT" — shell #38.)
    try:
        from tools.qa_superuser import propose as P
    except Exception as e:  # noqa: BLE001
        logger.error("[qa-superuser] tools/ not importable under gunicorn: %s", e)
        return jsonify({"ok": False, "error": "propose lane unavailable",
                        "reason": f"tools.qa_superuser.propose import failed: "
                                  f"{type(e).__name__}: {str(e)[:160]}"}), 503

    data = _load(limit=1)
    latest = (data.get("latest") or {})
    _attach_investigations(latest)
    finding = next((f for f in (latest.get("findings") or [])
                    if f.get("key") == key), None)
    if finding is None:
        # ★ "not in the latest run" is a CLAIM about the board. When the board
        #   could not be read at all, that claim is unfounded — and it sends the
        #   operator hunting for a vanished finding instead of a broken DB.
        if data.get("error") or not data.get("latest"):
            return jsonify({"ok": False, "error": "board unreadable",
                            "reason": data.get("error")
                            or "no run has been recorded yet"}), 503
        return jsonify({"ok": False,
                        "error": "finding not in the latest run"}), 404

    ok, why = P.gate_investigation(finding.get("investigation"))
    if not ok:
        return jsonify({"ok": False, "error": "not_ready", "reason": why}), 412

    # ★★★ EVERYTHING SLOW HAPPENS OFF-REQUEST, and this is not a preference.
    # The board is served through the CF edge, so the browser's POST lands on the
    # worker, whose ROUTE_TIMEOUTS has no `/api/v1/admin/` prefix and therefore
    # applies DEFAULT = 15s. The work below is a reasoning-tier model call (tens
    # of seconds) followed by ~8 sequential GitHub calls. The edge would abort at
    # 15s and return its own 503 envelope — while gunicorn ran on, OPENED the PR,
    # and spent a unit of the daily change budget. The operator would be told
    # "no PR" about a PR that exists.
    #
    # The sibling endpoint 90 lines up documents this exact constraint: "awaiting
    # it through the CF edge is impossible anyway — the zone's 15s route timeout
    # 503s admin POSTs." I wrote a synchronous handler underneath it anyway.
    #
    # ★ The refusal REASON is not lost by going async — that was the original
    # argument for staying synchronous. It is stored on the row and rendered on
    # the card, which outlives the request either way.
    # Stamped at DISPATCH, against the evidence the operator was looking at when
    # they clicked — not at completion, which can be a minute later and after a
    # fresh probe run has moved the finding underneath it.
    _mark_proposal(key, "running", "writing a patch and validating it",
                   evidence=finding.get("evidence") or "")
    meta = {"key": key, "title": finding.get("title"),
            "surface": finding.get("surface"), "seat": finding.get("seat"),
            "evidence": finding.get("evidence"),
            "red_when": finding.get("red_when"),
            "issue_number": finding.get("issue_number"),
            "investigation": finding.get("investigation")}
    try:
        import threading
        threading.Thread(target=_run_proposal, args=(meta,), daemon=True).start()
    except Exception as e:  # noqa: BLE001
        _mark_proposal(key, "error", f"could not dispatch: {e}",
                       evidence=finding.get("evidence") or "")
        return jsonify({"ok": False, "error": str(e)[:200]}), 500

    return jsonify({
        "ok": True, "dispatched": True,
        "note": "DISPATCHED, not finished — a patch is being written and "
                "validated against the real file (~1 min). The outcome, "
                "including the reason if it is refused, lands on this card.",
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
        _mark_investigations_unreadable(findings, "database unreachable")
        return
    try:
        with c.cursor() as cur:
            _ensure_investigations(cur)
            cur.execute("SELECT finding_key, evidence_sha, recommendation, "
                        "confidence, survived, issue_number, commented, "
                        "created_at, proposal_state, proposal_detail, pr_url, "
                        "pr_number, proposal_at, parked_escalated_at, "
                        "proposal_evidence_sha, "
                        "refuted_reruns "
                        "FROM qa_superuser_investigations")
            for row in cur.fetchall() or []:
                rows[row[0]] = row[1:]
    except Exception as e:  # noqa: BLE001
        # ★ BLIND != "none". Returning silently here made every card render
        #   "no investigation yet" — a factual claim about the world — when the
        #   truth was that we could not look. That is the same collapse the probe
        #   refuses everywhere else, committed by its own dashboard.
        logger.warning("[qa-superuser] investigation read failed: %s", e)
        _mark_investigations_unreadable(findings, str(e)[:160])
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
        (sha, rec_text, conf, survived, issue_no, commented, at,
         p_state, p_detail, pr_url, pr_number, p_at, parked_at, p_sha,
         refuted_reruns) = rec
        f["parked_escalated_at"] = parked_at.isoformat() if parked_at else None
        cur_sha = evidence_sha(f.get("evidence") or "")
        f["investigation"] = {
            "state": "current" if sha == cur_sha else "stale",
            "recommendation": rec_text,
            "confidence": float(conf) if conf is not None else None,
            "survived": survived,
            "issue_number": issue_no,
            "commented": bool(commented),
            "at": at.isoformat() if at else None,
            "refuted_reruns": int(refuted_reruns or 0),
        }
        # ★ The card must be able to SAY "the loop has stopped on this". Same
        #   predicate the escalation uses — one definition, so the page and the
        #   GitHub comment can never disagree about what is stuck. Soft import:
        #   a missing tools tree costs the banner, never the board.
        try:
            from tools.qa_superuser.propose import park_verdict as _pv
            _parked, _why = _pv(f)
            if _parked:
                f["parked"] = {"why": _why,
                               "escalated_at": f.get("parked_escalated_at")}
        except Exception:  # noqa: BLE001
            pass

        if p_state:
            # ★★★ A SECOND FIELD, NOT AN OVERLOADED `state`. `state` answers
            #   "what happened" (opened / running / refused / error);
            #   `evidence_state` answers "is that still about what is on
            #   screen". Folding freshness into `state` would make
            #   "refused" mean two things and the card could not say both.
            #
            # ★★ THREE VALUES, because "I do not know" is not "it changed".
            #   Rows written before this column existed have no provenance. The
            #   honest report is that it was not recorded — asserting the
            #   evidence CHANGED would be inventing a comparison that never
            #   ran, which is the collapse this board refuses everywhere else.
            #   It still renders with the stale treatment: an outcome that
            #   cannot be tied to the current evidence must not read as live.
            f["proposal"] = {
                "state": p_state,
                "evidence_state": ("unknown" if not p_sha
                                   else "current" if p_sha == cur_sha
                                   else "stale"),
                "detail": p_detail,
                "pr_url": pr_url,
                "pr_number": pr_number,
                "at": p_at.isoformat() if p_at else None,
            }


def _mark_investigations_unreadable(findings: list, why: str) -> None:
    """Say "I could not look", never "there is nothing there"."""
    for f in findings:
        f["investigation_unreadable"] = why


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
details.folded summary{cursor:pointer;color:var(--muted,#9a9a9a)}
details.folded[open] summary{margin-bottom:.35rem}
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
// Hours since an ISO stamp, or null when it cannot be read — null is NOT
// zero, and a caller must be able to tell "fresh" from "unreadable".
function hoursSince(iso){
  if(!iso) return null;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? (Date.now() - t) / 3600000 : null;
}

// ★ 2026-09-25 — WHAT NO LONGER DESCRIBES THE CARD IS FOLDED, NOT DELETED.
//   The owner's board carried a 49-day-old acknowledgement, a 6.5-day-old
//   REFUTED analysis and a 18.7-day-old refusal, all about earlier evidence,
//   rendered at full size above a finding that is only BLIND — three paragraphs
//   of answers to questions the card is no longer asking. Each already said
//   "the evidence has CHANGED"; the reader still had to read past all three.
//   A stale block now renders as one closed <details> line: the history is one
//   click away, and the card's first screen is about what it reports NOW.
function fold(summary, body){
  return `<details class="acked stale folded"><summary>${summary}</summary>${body}</details>`;
}

// The two classes the server's lanes treat as work (is_actionable_finding).
// Anything else — BLIND, GAUGE, PASS — is never re-analysed by the brain.
function isActionable(f){
  return (f.verdict === 'RED' && (f.severity === 'critical' || f.severity === 'major'))
    || !!f.instrument_fault;
}

function card(f, cls){
  const sev = f.severity && f.severity !== 'info'
    ? `<span class="chip sev">${esc(f.severity)}</span>` : '';
  const unstable = (f.transitions||0) >= 4
    ? `<span class="chip unstable">UNSTABLE ${f.transitions}x</span>` : '';
  const age = f.failing_since
    ? `<span class="chip age">red for ${span(f.failing_since)}</span>` : '';
  const ack = !f.ack ? '' : f.ack.state === 'stale'
    ? fold(`Older acknowledgement (${ago(f.ack.at)}) — the evidence has CHANGED since`,
        `<div class="row">What you signed off on is not what this check is
           reporting now.${f.ack.note ? ' ' + esc(f.ack.note) : ''}</div>`)
    : `<div class="acked"><b>Acknowledged ${ago(f.ack.at)}.</b>
      ${f.ack.note ? ' ' + esc(f.ack.note) : ''}</div>`;

  // The brain's analysis, shown where the finding is — with the same stale rule
  // as an ack, and with the refutation verdict FIRST. A recommendation the
  // investigator itself knocked down must not read like an answer.
  const iv = f.investigation;
  const ivRefuted = !!iv && iv.survived === false;
  const ivBody = !iv ? '' : `
      ${ivRefuted
        ? ` <b style="color:var(--amber)">Did NOT survive the brain's own
            refutation — treat as a lead, not an answer.</b>` : ''}
      ${iv.recommendation ? `<div class="row">${esc(iv.recommendation)}</div>` : ''}
      ${iv.commented && iv.issue_number
        ? `<div class="row"><a target="_blank" rel="noopener"
             href="https://github.com/${REPO}/issues/${iv.issue_number}"
             >posted to issue #${iv.issue_number} ↗</a></div>` : ''}`;
  const inv = !iv ? '' : iv.state === 'stale'
    ? fold(`🧠 Older analysis (${ago(iv.at)}${ivRefuted ? ', refuted' : ''}) — it
             explains an earlier observation, not this one`,
        `<div class="row">The evidence has CHANGED since this was written.${
           isActionable(f) ? ' The investigate lane re-analyses a red on its next run.'
           : ' This check is not red now, so the brain will not re-analyse it —'
             + ' kept for history only.'}</div>${ivBody}`)
    : `<div class="acked">
      <b>🧠 Analysed ${ago(iv.at)}${iv.confidence!=null
         ? ' · confidence ' + iv.confidence.toFixed(2) : ''}.</b>${ivBody}
    </div>`;

  // The outcome of a proposal, delivered here rather than in the HTTP response
  // — the work runs off-request to survive the edge's 15s budget, so this card
  // IS the channel. A refusal is shown with its reason: "'find' appears 3x —
  // ambiguous" is the useful output, and a lane that declines more often than it
  // succeeds must say why or it just looks broken.
  // ★★★ THE DEAD END, SAID OUT LOUD. Red, analysed, and ineligible for both
  //   re-analysis and a proposed fix. Before this the card showed a refuted
  //   analysis and simply no buttons, which reads as "nothing to do here"
  //   rather than "this is waiting on you" — and a CRITICAL sat in it for 3.5
  //   days. The gate is not loosened; the dead end is reported.
  const pk = f.parked;
  const parked = !pk ? '' : `<div class="acked stale">
      <b>⏸ The automated loop has stopped on this finding.</b>
      ${esc(pk.why)}. It will not be re-analysed and no fix can be proposed
      from it — <b>this needs your decision</b>: fix it by hand, change the
      check, or retire the check.
      ${pk.escalated_at
        ? `<div class="row">Posted to the issue ${ago(pk.escalated_at)}.</div>`
        : `<div class="row">Not yet posted to the issue — the next probe run
             does that.</div>`}
    </div>`;

  const pp = f.proposal;
  // ★★★ THE OUTCOME GETS THE SAME STALENESS RULE AS THE ACK AND THE ANALYSIS.
  //   It did not have one. `proposal_state` was rendered raw, so a refusal
  //   written against evidence that has since moved read as the card's CURRENT
  //   disposition forever. Live 2026-09-18: "No PR — refused 11.9d ago" sat
  //   directly beneath "Analysed 4.1h ago" on a finding 3.5 days old, and the
  //   operator reasonably read the refusal as the answer to the analysis above
  //   it. The two were about different observations.
  //
  // ★ `unknown` is rendered DIFFERENTLY from `stale`. Rows predating the
  //   provenance column cannot support the claim "the evidence has CHANGED" —
  //   saying so would invent a comparison that never ran. They get the stale
  //   TREATMENT (an outcome that cannot be tied to current evidence must not
  //   read as live) with an honest reason.
  const ppStale = pp && pp.evidence_state && pp.evidence_state !== 'current';
  const ppNote = !ppStale ? '' : (pp.evidence_state === 'unknown'
    ? `<div class="row"><b>⚠ Recorded before proposal provenance was tracked.</b>
         Which evidence it was made against was never stored, so it cannot be
         shown to describe what this card reports now.</div>`
    : `<div class="row"><b>⚠ This outcome describes OLDER evidence than the
         finding now shows.</b> It is not a verdict on what is on screen —
         re-investigate, then propose again.</div>`);
  // A dispatch that never wrote a terminal state leaves "reload in a minute"
  // on screen forever. Past this the thread is gone, not slow.
  const ppRunStuck = pp && pp.state === 'running'
    && (hoursSince(pp.at) === null || hoursSince(pp.at) > 1);
  const propInner = !pp ? '' : `
      ${pp.state === 'opened'
        ? `<b>🔧 PR opened ${ago(pp.at)} — not merged.</b>
           <a target="_blank" rel="noopener" href="${esc(pp.pr_url)}"
              >${esc(pp.pr_url)} ↗</a>`
        : pp.state === 'running'
        ? (ppRunStuck
            ? `<b>🔧 Writing a patch — started ${ago(pp.at)} and never
               finished.</b> The worker was restarted or the thread died; no
               outcome was ever written. Propose again.`
            : `<b>🔧 Writing a patch…</b> started ${ago(pp.at)}; reload in a minute.`)
        : pp.state === 'refused'
        ? `<b>🔧 No PR — refused ${ago(pp.at)}.</b> ${esc(pp.detail)}`
        : `<b>🔧 Proposal errored ${ago(pp.at)}.</b> ${esc(pp.detail)}`}
      ${ppNote}`;
  const prop = !pp ? '' : ppStale
    ? fold(`🔧 Older proposal outcome (${esc(pp.state)}, ${ago(pp.at)}) — made
             against earlier evidence`, propInner)
    : `<div class="acked ${pp.state === 'refused' || ppRunStuck ? 'stale' : ''}">${
        propInner}</div>`;

  // Actions exist on RED — and on an INSTRUMENT FAULT. A gauge makes no claim
  // to act on, and an unobserved finding is a request to look again, not a
  // defect to route.
  // "Propose a fix" appears only once an investigation exists — a diff written
  // from a symptom rather than a cause is the thing this tool refuses to make.
  //
  // ★ This condition MIRRORS the server's gate_investigation, all four clauses.
  //   It used to omit the recommendation check, so the button appeared on
  //   investigations the server would always refuse — offering an action that
  //   could only fail. A client gate looser than the server's is a lie about
  //   what is available.
  //
  // ★★ `|| f.instrument_fault` is the whole fix for "why isn't the brain
  //    fixing this?". `registries` crashed on every run for two days and NO
  //    card offered a single action, because a crashed probe is BLIND and this
  //    gate read RED-only. Rule 1 (BLIND is never a failure) is about the
  //    PLATFORM's verdict and stays intact — the finding is still not red, not
  //    counted, and makes no claim about the product. But a bug in this repo
  //    that only this board can see has to be routable from this board, or it
  //    is a defect with no reader. It is also the single most fixable class
  //    here: a crashed probe has a stack trace and a file, which is exactly
  //    the "one exact edit in one file" shape the proposer refuses everything
  //    else for.
  const canPropose = !!(iv && iv.state === 'current' && iv.survived !== false
                        && iv.recommendation && iv.recommendation.trim());
  const actionable = f.verdict === 'RED' || !!f.instrument_fault;
  const acts = !actionable ? '' : `<div class="acts" data-key="${esc(f.key)}">
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
    ${ack}${inv}${parked}${prop}${acts}
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
      // ★ A fast 200 means DISPATCHED, not "no PR" and not "PR". The work runs
      // off-request because the edge would abort it at 15s; the OUTCOME —
      // including the refusal reason, which is the useful part — arrives on the
      // card. Announcing a verdict here would be announcing one we do not have.
      out.textContent = 'dispatching…';
      const r = await post('/api/v1/admin/qa-superuser/propose-fix', {key});
      out.textContent = r.ok
        ? 'dispatched — a patch is being written and validated (~1 min); the '
          + 'outcome, PR or refusal, appears on this card'
        : ('not started: ' + (r.reason || r.error || '?'));
      el.classList.toggle('done', !!r.ok);
      el.classList.toggle('warn', !r.ok);
      if (r.ok) setTimeout(load, 3000);
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
  // Instrument faults are BLIND (they claim nothing about the product) but they
  // are OUR bug, so they get their own section with actions — see `actionable`
  // in card(). Listed among the unobserved they read as "a third party was
  // down" and draw the same response: none.
  const faults = F.filter(f=>f.verdict==='BLIND' && f.instrument_fault);
  const blind = F.filter(f=>f.verdict==='BLIND' && !f.instrument_fault);
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
    <div class="tile ${faults.length?'amber':'slate'}"><div class="n">${faults.length}</div>
      <div class="l">instrument faults</div></div>
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

  ${faults.length ? `<h2>🔧 Instrument faults — our bug, not a platform verdict
      <span class="cnt">${faults.length}</span></h2>
    <div class="card"><div class="row">These surfaces were <b>not measured at
      all</b> because this harness is broken, not the product. They are not red
      and are not counted as failures — but a surface that never ran cannot
      report the red it would have found, so every run they persist the board is
      narrower than it looks. They are actionable here.</div></div>
    ${faults.map(f=>card(f,'blind')).join('')}` : ''}

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
    The board never merges, deploys or executes. When a red's cause is
    already established it is handed to the bug squasher's agent lane, which
    opens a <b>draft</b> pull request for you to review — draft specifically,
    because this repo arms auto-merge on every non-draft PR the moment it opens
    (the repo variable <code>SQUASHER_AGENT_AUTOMERGE=1</code> is the only
    thing that changes that).
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
