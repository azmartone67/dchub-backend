"""Guard: an alarm that cannot reach a human is not an alarm.

Three separate notification paths were found dead on 2026-08-10, each one
looking perfectly wired from the outside. None of them had ever worked, and
none of them could fail loudly enough to say so.

  1. A DEAD HOSTNAME.  post-deploy-smoke.yml and dchub-qa.yml pointed their API
     checks at dchub-api-production.up.railway.app, a Railway service that no
     longer exists — it answers `x-railway-fallback: true` /
     {"message":"Application not found"} to everything. Both workflows had been
     red every ~30 minutes for as long as the run history goes back: 17 of the
     last 200 runs, every single one of them for this reason. A permanently red
     check is indistinguishable from a broken one, and teaches everybody to
     ignore it. The live service is dchub-backend-production.
       measured: dchub-api-production/api/health     -> 404
                 dchub-backend-production/api/health -> 200

  2. AN SMTP MISCONFIGURATION.  dchub-qa.yml's "Notify on failure" step used
     `secure: true` on port 587. That is implicit TLS against a STARTTLS port,
     so every notification died with
       SSL routines:tls_validate_record_header:wrong version number
     The QA check went red, and the email that existed to say so never sent.

  3. A MISSING TOKEN SCOPE.  Twelve workflows file a GitHub issue on failure.
     The repo's default_workflow_permissions is "read", so every attempt died
     with `Resource not accessible by integration (createIssue)` — and `|| true`
     swallowed the refusal. data-sync alone failed 23 of its last 40 runs and
     filed zero issues.

★ Comment lines are stripped before matching. Several files legitimately
DISCUSS the dead hostname in a comment explaining this history; only a live
reference counts. That distinction was itself a bug in
ingestion_integrity_master_shell.py the day before — matching raw text there
flagged a workflow for a credential it had stopped sending.

No network. Nothing runs at module scope.

Run locally:
    python3 -m pytest tests/test_alarm_reachability.py -v
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WF_DIR = ROOT / ".github" / "workflows"

DEAD_HOST = "dchub-api-production.up.railway.app"
LIVE_HOST = "dchub-backend-production.up.railway.app"

# Entrypoints that actually issue requests. Not a repo-wide sweep: a stale
# hostname inside a retired script is debt, not a broken alarm, and folding the
# two together makes this guard impossible to keep green.
LIVE_SOURCES = (
    "smoke_test.py",
    "crawler_scheduler.py",
    "dchub_mcp_server.py",
    "routes/brain_smoke_regression.py",
)


def _uncommented(body: str, marker: str = "#") -> str:
    return "\n".join(ln for ln in body.splitlines()
                     if not ln.lstrip().startswith(marker))


def _wf_files():
    return sorted(WF_DIR.glob("*.yml")) + sorted(WF_DIR.glob("*.yaml"))


# ── 1 · the dead hostname ────────────────────────────────────────────────────

def test_no_workflow_targets_the_dead_api_host():
    offenders = []
    for p in _wf_files():
        if DEAD_HOST in _uncommented(p.read_text(encoding="utf-8",
                                                 errors="replace")):
            offenders.append(p.name)
    assert not offenders, (
        f"{offenders} target {DEAD_HOST}, which answers 404 "
        f"'Application not found' to everything. Use {LIVE_HOST}.")


@pytest.mark.parametrize("rel", LIVE_SOURCES)
def test_no_live_entrypoint_targets_the_dead_api_host(rel):
    p = ROOT / rel
    if not p.exists():
        pytest.skip(f"{rel} not present")
    body = _uncommented(p.read_text(encoding="utf-8", errors="replace"))
    assert DEAD_HOST not in body, (
        f"{rel} still calls {DEAD_HOST} outside a comment — every request 404s")


# ── 2 · SMTP transport ───────────────────────────────────────────────────────

def test_smtp_notifications_do_not_mix_starttls_port_with_implicit_tls():
    """587 is STARTTLS and 465 is implicit TLS. `secure: true` on 587 fails the
    handshake before a byte of mail moves."""
    bad = []
    for p in _wf_files():
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        for jn, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                if "action-send-mail" not in str(step.get("uses") or ""):
                    continue
                w = step.get("with") or {}
                port = str(w.get("server_port", "")).strip()
                secure = str(w.get("secure", "")).strip().lower()
                if port == "587" and secure == "true":
                    bad.append(f"{p.name}:{jn}")
                if port == "465" and secure == "false":
                    bad.append(f"{p.name}:{jn} (465 needs secure: true)")
    assert not bad, f"SMTP port/secure mismatch — mail cannot send: {bad}"


# ── 3 · the token scope that makes `gh issue create` work ────────────────────

def _issue_creating_jobs():
    for p in _wf_files():
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        for jn, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            body = " ".join(s.get("run", "") for s in (job.get("steps") or [])
                            if isinstance(s, dict))
            if re.search(r"gh issue (create|comment)", body):
                yield p.name, jn, job.get("permissions", doc.get("permissions"))


def test_every_issue_filing_workflow_can_actually_file_issues():
    """The repo default is default_workflow_permissions=read, so a workflow that
    does not ASK for issues:write gets 'Resource not accessible by
    integration' — and `|| true` hides it."""
    bad = []
    for fname, jn, perms in _issue_creating_jobs():
        ok = perms == "write-all" or (isinstance(perms, dict)
                                      and perms.get("issues") == "write")
        if not ok:
            bad.append(f"{fname}:{jn} (permissions={perms!r})")
    assert not bad, (
        "these jobs file GitHub issues but lack issues:write, so every "
        f"attempt is refused: {bad}")


def test_the_guard_can_see_at_least_one_issue_filing_job():
    """Vacuity check: if the detector stops matching, the assertion above
    passes trivially and this guard silently stops guarding."""
    found = list(_issue_creating_jobs())
    assert len(found) >= 5, (
        f"only {len(found)} issue-filing jobs detected — the matcher probably "
        f"broke, which would make the scope test vacuously green")


# ── 4 · two writers, one ledger row ──────────────────────────────────────────

WATCH_PY = ROOT / "tools" / "deadman" / "watch.py"


def _conclusion_watched() -> set[str]:
    """Workflow filenames in watch.py's WORKFLOWS registry."""
    if not WATCH_PY.exists():
        return set()
    src = WATCH_PY.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^WORKFLOWS = \{(.*?)^\}", src, re.S | re.M)
    if not m:
        return set()
    return set(re.findall(r'"([\w.-]+\.ya?ml)"\s*:', _uncommented(m.group(1))))


def _dynamic_status_beaters() -> set[str]:
    """Workflows whose ingest-runs beat sends a COMPUTED status.

    A hardcoded `"status":"success"` loses nothing when the watcher overwrites
    it with the same word. A computed one carries information — no_new_data,
    error — that a bare conclusion beat destroys.
    """
    out = set()
    for p in _wf_files():
        body = _uncommented(p.read_text(encoding="utf-8", errors="replace"))
        if "ingest-runs/beat" not in body:
            continue
        if re.search(r'status\\?":\\?"\$', body):     # "status":"$VAR" / "${{ }}"
            out.add(p.name)
    return out


def test_no_feed_has_both_a_computed_beat_and_a_conclusion_watcher():
    """One-direction masking (SH52-002 B2, and again on 2026-08-10).

    tools/deadman/watch.py beats the GitHub Actions conclusion as a bare
    status="success" with no rows_inserted. When a producer also beats its own
    computed status, whichever writes last wins — and the watcher runs every
    2h, so it wins. news-ner-discovery beat an honest `no_new_data` (HTTP 200)
    and was clobbered back to `success` inside the same cycle, so its zero-row
    streak never cleared and a healthy feed stayed red for days.

    A feed gets ONE writer. If the producer computes a status, it owns the row.
    """
    both = sorted(_conclusion_watched() & _dynamic_status_beaters())
    assert not both, (
        "these workflows compute their own beat status AND sit in watch.py's "
        f"conclusion registry, so the watcher will overwrite them: {both}. "
        "Remove them from WORKFLOWS and let the producer be the single writer "
        "(the ledger fold, block 4, still covers staleness).")


def test_a_producer_that_owns_its_row_declares_its_own_cadence():
    """Removing a feed from WORKFLOWS removes watch.py as its cadence
    authority. If the producer does not send cadence_hours, the row falls back
    to _DEFAULT_CADENCE_H and the overdue threshold silently moves."""
    missing = []
    for name in sorted(_dynamic_status_beaters() - _conclusion_watched()):
        body = _uncommented((WF_DIR / name).read_text(encoding="utf-8",
                                                      errors="replace"))
        if not re.search(r"cadence_hours\\?\"?\s*:\s*\d+", body):
            missing.append(name)
    assert not missing, (
        f"{missing} own their ledger row but send no cadence_hours — the "
        f"overdue threshold falls back to the endpoint default")
