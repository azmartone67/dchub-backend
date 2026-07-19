"""
brain_issue_janitor.py — 2026-07-01. CLOSE-OUT half of the L15 issue loop.

L15 (brain_layer15_auto_action) OPENS a GitHub issue for every high-confidence
L14 causal chain, but nothing ever CLOSES them — the issue body literally says
"Close manually after fixing — L15 won't re-open". Result: the open-issue list
only grows (11 open brain-l15 issues as of 2026-07-01).

This janitor closes the loop:

  DE-PRIORITISED close — a brain-l15-auto issue older than the grace window
    whose chain title is NO LONGER present in the CURRENT top-ranked L14 causal
    chains. NOTE (2026-07-07 honesty fix): a chain dropping out of the ranked
    set is NOT proof the problem was fixed — chains thrash in/out every cycle.
    So this is closed as state_reason=not_planned with an explicit "NOT a
    verified fix" note, NOT state_reason=completed. A TRUE completed close
    requires the issue's own "How to verify" metric to pass (brain_fix_outcome
    _verify) — wiring that in is the tracked follow-up. If the problem recurs,
    L14 re-derives the chain and L15 re-files a fresh issue (dedup blocks only
    against OPEN issues + a 7d local title window) — so closing is safe.

  STALE close — a brain-l15-auto issue open longer than STALE_DAYS (default 21)
    is closed as state_reason=not_planned regardless: three weeks un-actioned
    means the queue entry is noise, and L15 will re-file if still real.

  L23 PROPOSAL close — brain-l23-lifecycle capability proposals older than
    PROPOSAL_STALE_DAYS (default 30) are closed as not_planned (same rationale).

  L22 STALE close (2026-07-19) — brain-l22-auto-code investigation issues older
    than L22_STALE_DAYS (default 14) are closed as not_planned. Safe because
    L22's recipe-class dedup lives in the brain_auto_code_actions DB ledger
    (window-based), NOT in GitHub open-issue state — closing the issue neither
    floods nor blinds; the recipe re-drafts after its dedup window if the
    signature recurs. Before this, l22 issues had NO close-out path at all
    (#1604 was the live example).

  MIRROR RECOVERY close (2026-07-19) — the brain-mirror workflow maintains ONE
    rolling issue while honest_score < 3.0 and updates it in place, but never
    closes it when the score recovers. When the LIVE mirror report shows
    honest_score >= 3.0, open brain-mirror issues close as state_reason=
    completed citing the live score (an honest, verified close). While the
    score is still low the issue is a live alert — it is NEVER stale-closed,
    and an unreachable/unparseable report closes nothing (fail closed).

SAFETY INVARIANTS:
  · Only issues carrying the exact `brain-l15-auto`, `brain-l23-lifecycle`,
    `brain-l22-auto-code`, or `brain-mirror` label are EVER touched — a
    human-filed issue can never match.
  · RESOLVED closes FAIL CLOSED: if the L14 causal fetch errors OR returns
    zero chains, NO resolution-closes happen that run (an empty chain list
    must never be read as "everything is fixed"). Age-based stale closes
    still run — they don't depend on the causal read.
  · Per-run cap (BRAIN_ISSUE_JANITOR_MAX_PER_RUN, default 10) bounds blast
    radius; kill switch BRAIN_ISSUE_JANITOR_DISABLE=1 halts all writes
    instantly without a deploy (same convention as BRAIN_L15_DISABLE).
  · Every close posts a breadcrumb comment explaining the auto-close and the
    re-file path, and is recorded in brain_janitor_closes for audit.
  · NEVER deletes, locks, or edits issue bodies — close + comment only.

Endpoints:
  GET  /api/v1/brain/issue-janitor/status — dry preview, never writes
  POST /api/v1/brain/issue-janitor/run    — acts (admin-gated); ?dry=1 previews

Cron: dchub-scheduler.py 'brain_issue_janitor' — 2x daily.
"""

import os
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
brain_issue_janitor_bp = Blueprint("brain_issue_janitor", __name__)

_GITHUB_REPO = (os.environ.get("GITHUB_REPO") or "azmartone67/dchub-backend").strip()
_L15_LABEL = "brain-l15-auto"
_L23_LABEL = "brain-l23-lifecycle"
_L22_LABEL = "brain-l22-auto-code"
_MIRROR_LABEL = "brain-mirror"
_MIRROR_SCORE_THRESHOLD = 3.0  # mirrors brain-mirror.yml's file gate
_L15_TITLE_PREFIX = "[brain-l15] "
# NOTE (2026-07-03): CI-noise issues ([workflow-failed]/[STATUS], labels
# workflow-failure/monitoring/auto) are owned by the dedicated GH Action
# .github/workflows/issue-autoclose.yml — it closes them on workflow RECOVERY
# (more correct than a time window) + dedup + stale>3d. This janitor stays
# scoped to brain-l15-auto / brain-l23-lifecycle so the two never double-act.


def _token() -> str:
    return (os.environ.get("GITHUB_TOKEN") or "").strip()


def _admin_key() -> str:
    return (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()


def _disabled() -> bool:
    return os.environ.get("BRAIN_ISSUE_JANITOR_DISABLE", "") in ("1", "true", "True", "yes")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _grace_days() -> int:
    return max(0, _env_int("BRAIN_ISSUE_JANITOR_GRACE_DAYS", 1))


def _stale_days() -> int:
    return max(1, _env_int("BRAIN_ISSUE_JANITOR_STALE_DAYS", 21))


def _proposal_stale_days() -> int:
    return max(1, _env_int("BRAIN_ISSUE_JANITOR_PROPOSAL_STALE_DAYS", 30))


def _l22_stale_days() -> int:
    return max(1, _env_int("BRAIN_ISSUE_JANITOR_L22_STALE_DAYS", 14))


def _max_per_run() -> int:
    return max(0, _env_int("BRAIN_ISSUE_JANITOR_MAX_PER_RUN", 10))


def _verify_completed_closes() -> bool:
    """BRAIN_JANITOR_VERIFY_COMPLETED (default OFF). When ON, a dropped-from-
    causal-set issue may be closed state_reason=completed — but ONLY when a
    brain_fix_outcomes row proves the finding was VERIFIED RESOLVED on main.
    Off = legacy honest path (every drop closes not_planned). This is the
    tracked follow-up named in the 2026-07-07 honesty note above: it gives the
    janitor its FIRST honest 'completed' path instead of forcing every real fix
    to read as a mere de-prioritisation."""
    return os.environ.get("BRAIN_JANITOR_VERIFY_COMPLETED", "0") == "1"


def _verify_window_days() -> int:
    return max(1, _env_int("BRAIN_JANITOR_VERIFY_WINDOW_DAYS", 14))


def _verified_resolution_for(chain_title: str):
    """Return an evidence dict {id, klass, file_path, reason, verified_at} if a
    brain_fix_outcomes row with resolved IS TRUE confidently matches THIS issue's
    causal chain title within the verify window; else None.

    Deliberately CONSERVATIVE — a false 'completed' is exactly the dishonesty the
    janitor honesty-fix removed. brain_fix_outcomes has no issue backlink (the
    tracked gap), so we require a SPECIFIC token match (the outcome's klass, or
    its file basename, appearing in the chain title) and we CITE the outcome id +
    reason in the close comment for audit. Any ambiguity or error -> None (fall
    through to the honest not_planned path). Read-only; never raises."""
    title = (chain_title or "").strip().lower()
    if len(title) < 8:
        return None
    conn = None
    try:
        from main import get_db
        conn = get_db()
        if not conn:
            return None
        cur = conn.cursor()
        cur.execute(
            "SELECT id, COALESCE(klass,''), COALESCE(file_path,''), COALESCE(reason,''), verified_at "
            "FROM brain_fix_outcomes "
            "WHERE resolved IS TRUE AND verified_at >= NOW() - (%s || ' days')::interval "
            "ORDER BY verified_at DESC LIMIT 200",
            (str(_verify_window_days()),),
        )
        rows = cur.fetchall() or []
        try: cur.close()
        except Exception: pass
        for rid, klass, fpath, reason, vat in rows:
            k = (klass or "").strip().lower()
            base = ""
            if fpath:
                base = fpath.replace("\\", "/").split("/")[-1].rsplit(".", 1)[0].strip().lower()
            # require a specific (>=4 char) token to actually appear in the title
            if (k and len(k) >= 4 and k in title) or (base and len(base) >= 4 and base in title):
                return {"id": rid, "klass": klass, "file_path": fpath,
                        "reason": reason, "verified_at": str(vat)}
        return None
    except Exception as e:
        logger.warning("issue-janitor verify lookup failed: %s", e)
        return None
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


def _gh(method: str, path: str, body=None, params=None):
    import requests
    return requests.request(
        method,
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "dchub-brain-issue-janitor/1.0",
        },
        json=body,
        params=params,
        timeout=20,
    )


def _current_chain_titles() -> set[str] | None:
    """Titles of the CURRENT L14 causal chains (any confidence). Returns None
    on fetch failure or an empty analysis — callers MUST treat None as
    'unknown' and skip resolution-closes (fail closed)."""
    try:
        import requests
        r = requests.get("http://localhost:8080/api/v1/brain/causal", timeout=10)
        if r.status_code != 200:
            return None
        chains = ((r.json() or {}).get("analysis") or {}).get("causal_chains") or []
        titles = {(c.get("title") or "")[:120] for c in chains if c.get("title")}
        return titles or None
    except Exception:
        return None


def _open_issues(label: str) -> list[dict]:
    out: list[dict] = []
    page = 1
    while page <= 3:  # 300 issues is far beyond any sane pileup
        r = _gh("GET", f"/repos/{_GITHUB_REPO}/issues",
                params={"state": "open", "labels": label,
                        "per_page": 100, "page": page})
        if r.status_code != 200:
            break
        batch = [i for i in (r.json() or []) if "pull_request" not in i]
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return out


def _mirror_recovered():
    """Live honest_score if it has RECOVERED (>= threshold), else None.

    Reads the same field brain-mirror.yml gates on
    (.honest_grade.honest_score). FAIL CLOSED: unreachable report, missing
    key, or unparseable score all return None — a broken probe must never
    close a live alert. Loopback first (janitor runs in-app), public edge
    as fallback."""
    import json as _json
    import urllib.request as _rq
    port = os.environ.get("PORT", "8080")
    for url in (f"http://127.0.0.1:{port}/api/v1/brain/mirror/report",
                "https://dchub.cloud/api/v1/brain/mirror/report"):
        try:
            with _rq.urlopen(_rq.Request(
                    url, headers={"User-Agent": "dchub-issue-janitor/1.0"}),
                    timeout=15) as resp:
                body = _json.loads(resp.read(262144))
            score = (body.get("honest_grade") or {}).get("honest_score")
            score = float(score)
            return score if score >= _MIRROR_SCORE_THRESHOLD else None
        except Exception:
            continue
    return None


def _age_days(issue: dict) -> float:
    try:
        created = _dt.datetime.fromisoformat(
            (issue.get("created_at") or "").replace("Z", "+00:00"))
        return (_dt.datetime.now(_dt.timezone.utc) - created).total_seconds() / 86400.0
    except Exception:
        return -1.0


def _close_issue(number: int, state_reason: str, comment: str) -> tuple[bool, str]:
    try:
        cr = _gh("POST", f"/repos/{_GITHUB_REPO}/issues/{number}/comments",
                 body={"body": comment})
        if cr.status_code not in (200, 201):
            logger.warning("issue-janitor: comment on #%s failed (%s)",
                           number, cr.status_code)
        r = _gh("PATCH", f"/repos/{_GITHUB_REPO}/issues/{number}",
                body={"state": "closed", "state_reason": state_reason})
        if r.status_code != 200:
            return False, f"github_{r.status_code}: {r.text[:150]}"
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:150]}"


def _record_close(number: int, title: str, reason: str, ok: bool, err: str):
    conn = None
    try:
        from main import get_db
        conn = get_db()
        if not conn:
            return
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brain_janitor_closes (
                id          SERIAL PRIMARY KEY,
                closed_at   TIMESTAMPTZ DEFAULT NOW(),
                kind        TEXT NOT NULL,
                number      INTEGER NOT NULL,
                title       TEXT,
                reason      TEXT,
                ok          BOOLEAN,
                error       TEXT
            )
        """)
        cur.execute(
            "INSERT INTO brain_janitor_closes (kind, number, title, reason, ok, error) "
            "VALUES ('issue', %s, %s, %s, %s, %s)",
            (number, title[:200], reason, ok, err or None),
        )
        conn.commit()
        try: cur.close()
        except Exception: pass
    except Exception as e:
        logger.warning("issue-janitor record failed: %s", e)
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


def janitor_sweep(dry_run: bool = True) -> dict:
    """Scan open brain-labeled issues and close the resolved/stale ones.
    dry_run=True (or the kill switch) → zero GitHub writes, returns the
    would-close list."""
    if _disabled():
        dry_run = True
    if not _token():
        return {"ok": False, "error": "no_github_token"}

    chain_titles = _current_chain_titles()
    grace, stale = _grace_days(), _stale_days()
    cap = _max_per_run()

    candidates: list[dict] = []  # {number, title, reason, state_reason, comment}

    for issue in _open_issues(_L15_LABEL):
        num, title = issue.get("number"), (issue.get("title") or "")
        age = _age_days(issue)
        if age < 0:
            continue
        chain_title = title[len(_L15_TITLE_PREFIX):] if title.startswith(_L15_TITLE_PREFIX) else title
        if age >= stale:
            candidates.append({
                "number": num, "title": title, "reason": f"stale_{stale}d",
                "state_reason": "not_planned",
                "comment": (f"🤖 **Auto-closed by the brain issue janitor** — open "
                            f"{int(age)}d with no action. If this is still real, L14 "
                            f"will re-derive the chain and L15 will re-file a fresh "
                            f"issue automatically."),
            })
        elif age >= grace and chain_titles is not None and chain_title[:120] not in chain_titles:
            # HONESTY FIX (2026-07-07): a chain dropping out of the CURRENT
            # top-ranked L14 set is NOT evidence the underlying problem was
            # fixed — chains thrash in and out of the ranked set every cycle, so
            # closing these as state_reason="completed" ("reads as resolved")
            # manufactured a false "done ✓" that inflated the brain's success
            # numbers and let the SAME finding re-file next cycle.
            #
            # FOLLOW-UP WIRED (2026-07-08, BRAIN_JANITOR_VERIFY_COMPLETED, default
            # OFF): before de-prioritising, check for a brain_fix_outcomes row
            # that VERIFIED this finding resolved on main. If one exists (specific
            # token match, cited in the comment) we close state_reason=completed —
            # the janitor's first HONEST 'completed' path, so a real verified fix
            # stops reading as a mere de-prioritisation. Otherwise (flag off, or no
            # verified outcome) we keep the honest not_planned close below.
            _ev = _verified_resolution_for(chain_title) if _verify_completed_closes() else None
            if _ev:
                _tag = (_ev.get("klass") or _ev.get("file_path") or "n/a")
                candidates.append({
                    "number": num, "title": title, "reason": "verified_resolved",
                    "state_reason": "completed",
                    "comment": ("🤖 **Auto-closed by the brain issue janitor — VERIFIED "
                                "RESOLVED.** A fix-outcome check confirmed this finding's "
                                f"condition is resolved on `main`: outcome #{_ev.get('id')} "
                                f"(`{_tag}`), verified {_ev.get('verified_at')} — "
                                f"{_ev.get('reason') or 'anti-pattern gone from main'}. "
                                "Closing as completed (evidence cited above). If the "
                                "condition regresses, L14 re-derives the chain and L15 "
                                "re-files automatically."),
                })
            else:
                candidates.append({
                    "number": num, "title": title, "reason": "dropped_from_causal_set_unverified",
                    "state_reason": "not_planned",
                    "comment": ("🤖 **Auto-closed by the brain issue janitor** — this "
                                "causal chain is no longer in the current top-ranked "
                                "L14 set, so it's being de-prioritised. **This is NOT a "
                                "verified fix** — the underlying condition was not "
                                "checked against this issue's own \"How to verify\" "
                                "metric. If the problem persists it will re-surface and "
                                "L15 will re-file. Closing to keep the queue honest, not "
                                "to claim resolution."),
                })

    for issue in _open_issues(_L23_LABEL):
        num, title = issue.get("number"), (issue.get("title") or "")
        age = _age_days(issue)
        if age >= _proposal_stale_days():
            candidates.append({
                "number": num, "title": title,
                "reason": f"proposal_stale_{_proposal_stale_days()}d",
                "state_reason": "not_planned",
                "comment": ("🤖 **Auto-closed by the brain issue janitor** — this "
                            "capability proposal sat unactioned past the stale "
                            "window. The brain will re-propose it if it still "
                            "scores as valuable."),
            })

    # 2026-07-19: l22 investigation issues previously had NO close-out path.
    # Stale-close only — L22's recipe-class dedup is a DB-ledger window
    # (brain_auto_code_actions), so closing here can't flood and the recipe
    # re-drafts if the drift signature recurs.
    for issue in _open_issues(_L22_LABEL):
        num, title = issue.get("number"), (issue.get("title") or "")
        age = _age_days(issue)
        if age >= _l22_stale_days():
            candidates.append({
                "number": num, "title": title,
                "reason": f"l22_stale_{_l22_stale_days()}d",
                "state_reason": "not_planned",
                "comment": (f"🤖 **Auto-closed by the brain issue janitor** — this "
                            f"L22 investigation prompt sat unactioned for "
                            f"{int(age)}d. **Not a verified fix** — if the drift "
                            f"signature recurs, the L22 recipe re-drafts a fresh "
                            f"issue after its dedup window."),
            })

    # 2026-07-19: the brain-mirror workflow maintains ONE rolling issue while
    # honest_score < 3.0 but never closes it on recovery. Close as COMPLETED
    # only against the LIVE score (fail closed on any probe error); while the
    # score is low the issue is a live alert and is never stale-closed.
    mirror_open = _open_issues(_MIRROR_LABEL)
    if mirror_open:
        recovered_score = _mirror_recovered()
        if recovered_score is not None:
            for issue in mirror_open:
                candidates.append({
                    "number": issue.get("number"),
                    "title": issue.get("title") or "",
                    "reason": "mirror_score_recovered",
                    "state_reason": "completed",
                    "comment": (f"🤖 **Auto-closed by the brain issue janitor — "
                                f"RECOVERED.** The live mirror report now grades "
                                f"honest_score {recovered_score:.2f}/4 (threshold "
                                f"{_MIRROR_SCORE_THRESHOLD}). The brain-mirror "
                                f"workflow re-files automatically if the score "
                                f"drops below threshold again."),
                })

    candidates = candidates[:cap]
    if dry_run:
        return {"ok": True, "dry_run": True, "disabled": _disabled(),
                "causal_chains_seen": len(chain_titles) if chain_titles else 0,
                "would_close": candidates, "would_close_count": len(candidates)}

    closed, errors = [], []
    for c in candidates:
        ok, err = _close_issue(c["number"], c["state_reason"], c["comment"])
        _record_close(c["number"], c["title"], c["reason"], ok, err)
        (closed if ok else errors).append(
            {"number": c["number"], "reason": c["reason"], **({"error": err} if err else {})})

    return {"ok": True, "dry_run": False,
            "causal_chains_seen": len(chain_titles) if chain_titles else 0,
            "closed": closed, "closed_count": len(closed), "errors": errors}


@brain_issue_janitor_bp.route("/api/v1/brain/issue-janitor/status", methods=["GET"])
def issue_janitor_status():
    """Always-dry preview of what the next run would close."""
    preview = janitor_sweep(dry_run=True)
    return jsonify(
        ok=True,
        disabled=_disabled(),
        grace_days=_grace_days(),
        stale_days=_stale_days(),
        proposal_stale_days=_proposal_stale_days(),
        l22_stale_days=_l22_stale_days(),
        max_per_run=_max_per_run(),
        preview=preview,
    )


@brain_issue_janitor_bp.route("/api/v1/brain/issue-janitor/run", methods=["POST"])
def issue_janitor_run():
    """Acting sweep (cron target). ?dry=1 forces a preview. Admin-gated when
    DCHUB_ADMIN_KEY is set — same convention as L15's run endpoint."""
    if _admin_key():
        provided = (request.headers.get("X-Admin-Key") or "").strip()
        if provided != _admin_key():
            return jsonify(error="unauthorized"), 401
    dry = request.args.get("dry") == "1"
    result = janitor_sweep(dry_run=dry)
    status = 200 if result.get("ok") else 503
    return jsonify(result), status
