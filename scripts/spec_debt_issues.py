#!/usr/bin/env python3
"""scripts/spec_debt_issues.py — one owner for `spec-debt` issues (2026-09-13).

MEASURED 2026-09-12: brain-spec-debt-tracker.yml had filed 171 issues since
2026-08-13 and nothing had ever closed one — 0 of 171, not one comment. Two
gaps, either one enough to make the pile permanent:

  1. ONE ISSUE PER MERGED PR, not per problem. The brain re-proposes a finding
     for every new target, so the 171 issues were 71 problems
     (29 x iso_metric_count_zero_24h, one per grid region).
  2. NO CLOSER. Whether an obligation is met is decided in the spec ledger,
     docs/brain-proposals/ (routes/brain_spec_debt.py). The 2026-08-16/17 sweeps
     closed 132 docs there and 0 issues; nothing reconciled the two.

This file is the one definition both workflows use:

  class_key(title)   Which problem a spec PR or spec-debt issue is about — the
                     fold key. brain-spec-debt-tracker.yml appends to the open
                     issue with the same key instead of opening another one.
  resolve_doc(name)  A doc's ledger state. A doc closed "as a class member of X"
                     or "as an exact re-file of X" is NOT done: its obligation
                     moved to X, so this follows the pointer and reports X.
                     Measured 2026-09-12: 13 of the 14 closed docs behind open
                     issues pointed at a doc that was still OPEN.
  plan(...)          Fold + close decisions. Pure — no network, no clock.

An issue closes only when EVERY spec it tracks resolves to CLOSED. Anything
unreadable keeps it open: a doc missing from the corpus, a PR whose files could
not be listed, an empty corpus, a pointer cycle.

CLI (network through `gh`):
  class-key        --title T
  find-class-issue --repo R --title T     the open issue for T's problem, or nothing
  reconcile        --repo R --corpus DIR [--apply] [--max-closes N] [--summary F]

Without --apply, `reconcile` prints the plan and writes nothing.
"""
from __future__ import annotations

import argparse
import functools
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABEL = "spec-debt"
PROSE_KEY_CHARS = 48
MAX_POINTER_HOPS = 8
DEFAULT_MAX_CLOSES = 30

_PR_MARKER_RE = re.compile(r"spec-debt-for-pr-(\d+)")
_SNAKE_KEY_RE = re.compile(r"([a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?=$|[\s:(@])")
_PROSE_CUT_RE = re.compile(r"\s+(?:\(observed at|—\s*observed|@\s)")
_CHECKED_LINE_RE = re.compile(r"^\s*[-*]\s+\[[xX]\].*$", re.M)
# Ordered: the most specific hand-off phrase wins when a doc carries several.
# Case-insensitive: the 2026-08-17 collapse wrote both "act on X.md" in a
# checklist line and "Act on X.md." in its prose. A path prefix
# (`docs/brain-proposals/X.md`) is allowed and dropped.
_POINTER_RES = [
    re.compile(phrase + r"\s+`?(?:docs/brain-proposals/)?([A-Za-z0-9][A-Za-z0-9._-]*\.md)",
               re.I)
    for phrase in (r"class member of", r"exact re-file of", r"re-file of",
                   r"implement against", r"canonical is", r"act on")
]


# ── the ledger's own rule, not a copy of it ──────────────────────────────

@functools.lru_cache(maxsize=1)
def _ledger():
    """routes/brain_spec_debt.py, loaded by FILE PATH so the OPEN/CLOSED rule is
    the one the ledger endpoint uses, without importing the routes package."""
    spec = importlib.util.spec_from_file_location(
        "_spec_debt_ledger", os.path.join(ROOT, "routes", "brain_spec_debt.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── which problem ────────────────────────────────────────────────────────

def _strip_prefixes(title: str) -> str:
    t = (title or "").strip()
    t = re.sub(r"^\[spec-debt\]\s*", "", t)
    t = re.sub(r"^\[brain-[^\]]*\]\s*", "", t)
    t = re.sub(r"^(?:inv|agenda|prop)\s+#\d+:\s*", "", t)
    t = re.sub(r"^\[[a-z_]+\]\s*", "", t)
    t = re.sub(r"^Brain finding:\s*", "", t)
    return re.sub(r"^qa_[a-z]+\s+", "", t)


def class_key(title: str) -> str:
    """The problem a spec PR or a spec-debt issue is about.

    Titles are `[brain-spec] inv #100603: <heading[:70]>`, re-filed as
    `[spec-debt] inv #100603: ...`. A heading leads with the finding's detector
    key when it has one — `iso_metric_count_zero_24h (observed at: grid_data:
    iso=EU_BE)`, `[reliability] Brain finding: operator_profile_gap:Digital
    Realty @ /op` — and that key is the problem; the target after it is not.

    A heading with no key is prose (`5 of 8 published story link(s) are dead`).
    The 70-character cut lands at a different point depending on the prefix —
    `qa_critical A paying key...` loses 12 characters of its tail — so prose
    compares on its digit-normalised, lower-cased first 48 characters; the
    shortest real variant is 57. Returns "" for a title with nothing in it,
    and "" never folds.
    """
    t = _strip_prefixes(title)
    if not t:
        return ""
    m = _SNAKE_KEY_RE.match(t)
    if m:
        return m.group(1)
    t = _PROSE_CUT_RE.split(t)[0]
    t = re.sub(r"\s+", " ", re.sub(r"\d[\d,.]*", "#", t.lower())).strip()
    return ("prose:" + t[:PROSE_KEY_CHARS].rstrip()) if t else ""


def target_of(title: str) -> str:
    """What this copy of the problem was observed at — display only."""
    t = _strip_prefixes(title)
    m = re.search(r"\(observed at:?\s*([^)]*)", t)
    if m:
        return m.group(1).strip()[:60]
    m = re.match(r"[a-z][a-z0-9_]*:(.+?)(?:\s+@\s|\s*$)", t)
    if m:
        return m.group(1).strip()[:60]
    m = re.search(r"\s@\s+(.+)$", t)
    return m.group(1).strip()[:60] if m else ""


# ── the ledger state of one doc ──────────────────────────────────────────

def _pointer(text: str, own: str):
    """Where a CLOSED doc says its obligation went, or None.

    Read only where triage writes it — checked checklist lines, and everything
    from the first `## Triage` heading on — so a recommendation that merely
    names another .md file in its prose is not mistaken for a hand-off."""
    idx = text.find("## Triage")
    zone = "\n".join(_CHECKED_LINE_RE.findall(text))
    if idx >= 0:
        zone += "\n" + text[idx:]
    for rx in _POINTER_RES:
        for m in rx.finditer(zone):
            name = os.path.basename(m.group(1))
            if name != own:
                return name
    return None


def _evidence(text: str) -> str:
    heads = [l.strip() for l in text.splitlines() if l.startswith("## Triage")]
    if heads:
        return heads[-1]
    return "every checklist item is checked"


def resolve_doc(name: str, corpus_dir: str) -> dict:
    """The ledger state of the obligation a doc stands for.

    state: open | closed | unknown | missing | cycle. Only `closed` is closed.
    `terminal` is the doc that holds the obligation after following hand-offs;
    `chain` lists every doc read on the way."""
    mod = _ledger()
    start = os.path.basename(name)
    chain, cur = [], start
    while True:
        if cur in chain or len(chain) >= MAX_POINTER_HOPS:
            return {"doc": start, "state": "cycle", "terminal": cur, "chain": chain,
                    "evidence": "hand-off pointers loop or run too deep"}
        chain.append(cur)
        try:
            with open(os.path.join(corpus_dir, cur), encoding="utf-8",
                      errors="replace") as fh:
                text = fh.read()
        except OSError:
            return {"doc": start, "state": "missing", "terminal": cur, "chain": chain,
                    "evidence": f"{cur} is not in the corpus"}
        state = mod.classify_doc_text(text)
        if state != mod.CLOSED:
            return {"doc": start, "state": state, "terminal": cur, "chain": chain,
                    "evidence": "unchecked checklist item(s) remain"
                    if state == mod.OPEN else "no checklist"}
        nxt = _pointer(text, cur)
        if not nxt:
            return {"doc": start, "state": "closed", "terminal": cur, "chain": chain,
                    "evidence": _evidence(text)}
        cur = nxt


# ── decisions ────────────────────────────────────────────────────────────

def tracked_prs(issue: dict) -> list[int]:
    """Every spec PR an issue tracks: its own (body) plus any folded or
    appended into it (comments), in first-seen order."""
    texts = [issue.get("body") or ""]
    texts += [(c or {}).get("body") or "" for c in issue.get("comments") or []]
    seen, out = set(), []
    for text in texts:
        for m in _PR_MARKER_RE.finditer(text):
            n = int(m.group(1))
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


# ── has the finding behind a spec stopped firing? ────────────────────────
#
# ★ 2026-09-13. "Not seen lately" is not "stopped firing": brain_findings bumps
#   last_seen on every write and resolves any row quiet for 24h whether or not
#   its detector ran, and one live radar sweep left 54 of 143 detectors
#   unfinished. The verdicts come from routes/brain_detector_ledger.py, which
#   records which detectors completed each sweep and what they reported; this
#   side only maps a spec to its finding and acts on quiet_proven.

EVIDENCE_PATH = "/api/v1/brain/spec-debt/finding-evidence"
EVIDENCE_ORIGIN = "https://dchub-backend-production.up.railway.app"
EVIDENCE_UA = "dchub-spec-debt-reconcile/1.0"

_SQUASHER_HEADING_RE = re.compile(r"^(.+?) \(observed at: (.+?)\)\. What is")
_FINDING_HEADING_RE = re.compile(
    r"Brain finding: (.+?)(?: @ (.+?))?(?: \((?:seen x\d+|value [\d,]+)\))?$")


def doc_heading(doc: str, corpus_dir: str):
    """The H1 of a landed spec — the untruncated heading its PR title cut."""
    try:
        with open(os.path.join(corpus_dir, os.path.basename(doc)), encoding="utf-8",
                  errors="replace") as fh:
            m = re.search(r"^# Brain proposal — (.+)$", fh.read(), re.M)
    except OSError:
        return None
    return m.group(1).strip() if m else None


def spec_target(heading):
    """The brain_findings row a spec was filed for — {issue, url, url_prefix} —
    or None when the spec did not come from one.

    Squasher investigations quote the finding exactly: "<issue> (observed at:
    <url>). What is…". Agenda and proposal titles quote it cut to 90
    characters, so their url may be a prefix. QA-board, QA-intake and audit
    specs never came from brain_findings at all."""
    h = (heading or "").strip()
    if not h or re.search(r"— observed from the \w+ seat", h) or re.match(r"(qa_[a-z]+|audit_[HM]) ", h):
        return None
    m = _SQUASHER_HEADING_RE.match(h)
    if m:
        return {"issue": m.group(1), "url": m.group(2), "url_prefix": False}
    m = _FINDING_HEADING_RE.search(h)
    if m:
        return {"issue": m.group(1), "url": m.group(2) or "", "url_prefix": True}
    return None


def evidence_key(issue, url) -> str:
    return f"{str(issue or '')[:200]}|{str(url or '')[:500]}"


def quiet_verdicts(issues: list[dict], pr_docs: dict, corpus_dir: str, evidence: dict, *,
                   extra_prs: dict | None = None) -> list[dict]:
    """Per issue: firing | quiet_proven | quiet_unproven | unmeasured. Pure.

    An issue is quiet_proven only if EVERY spec it tracks maps to a finding the
    evidence endpoint judged quiet_proven. One firing spec makes it firing; one
    spec with no measurable finding makes it unmeasured."""
    rows = []
    for iss in issues:
        prs = tracked_prs(iss)
        prs += [p for p in (extra_prs or {}).get(iss["number"], []) if p not in prs]
        specs = []
        for p in prs:
            docs = pr_docs.get(p)
            if not docs:
                specs.append({"pr": p, "verdict": "unmeasured",
                              "reason": "spec PR files unreadable" if docs is None
                              else "spec PR landed no doc"})
                continue
            for d in docs:
                target = spec_target(doc_heading(d, corpus_dir))
                if target is None:
                    specs.append({"pr": p, "doc": os.path.basename(d), "verdict": "unmeasured",
                                  "reason": "not filed from a brain_findings row"})
                    continue
                ev = evidence.get(evidence_key(target["issue"], target["url"]))
                specs.append({"pr": p, "doc": os.path.basename(d), "target": target,
                              "verdict": (ev or {}).get("verdict") or "unmeasured",
                              "reason": (ev or {}).get("reason") or "no evidence returned for it",
                              "detector_fn": (ev or {}).get("detector_fn")})
        seen = {s["verdict"] for s in specs}
        verdict = ("unmeasured" if not specs else "firing" if "firing" in seen
                   else "quiet_proven" if seen == {"quiet_proven"}
                   else "unmeasured" if "unmeasured" in seen else "quiet_unproven")
        rows.append({"number": iss["number"], "verdict": verdict, "specs": specs})
    return rows


def plan(issues: list[dict], pr_docs: dict, corpus_dir: str, *,
         repo: str = "azmartone67/dchub-backend",
         max_closes: int = DEFAULT_MAX_CLOSES,
         evidence: dict | None = None, close_on_quiet: bool = False) -> dict:
    """What to close and what to fold. Writes nothing.

    issues          open spec-debt issues: number, title, body, comments[{body}]
    pr_docs         {pr_number: [doc paths] | None}; None = files could not be read
    evidence        {evidence_key: {verdict, reason, detector_fn}} from the evidence
                    endpoint, or None when it was not read (the quiet arm is skipped)
    close_on_quiet  close quiet_proven issues; otherwise they are only reported
    """
    try:
        corpus_ok = any(f.endswith(".md") for f in os.listdir(corpus_dir))
    except OSError:
        corpus_ok = False
    ordered = sorted(issues, key=lambda i: i["number"])

    closes, kept = [], []
    for iss in ordered:
        prs = tracked_prs(iss)
        if not corpus_ok:
            kept.append({"number": iss["number"], "reason": "corpus unreadable"})
            continue
        if not prs:
            kept.append({"number": iss["number"], "reason": "no spec PR marker"})
            continue
        unreadable = [p for p in prs if pr_docs.get(p) is None]
        if unreadable:
            kept.append({"number": iss["number"],
                         "reason": f"files unreadable for PR(s) {unreadable}"})
            continue
        docs = [(p, d) for p in prs for d in pr_docs[p]]
        if not docs:
            kept.append({"number": iss["number"], "reason": "no ledger doc landed"})
            continue
        res = [dict(resolve_doc(d, corpus_dir), pr=p) for p, d in docs]
        if all(r["state"] == "closed" for r in res):
            closes.append({"number": iss["number"], "resolutions": res,
                           "comment": ledger_close_comment(res, repo)})
        else:
            still = sorted({r["terminal"] for r in res if r["state"] != "closed"})
            kept.append({"number": iss["number"], "reason": "open in ledger: " + ", ".join(still)})

    closing = {c["number"] for c in closes}
    groups: dict[str, list[dict]] = {}
    for iss in ordered:
        k = class_key(iss["title"])
        if k and iss["number"] not in closing:
            groups.setdefault(k, []).append(iss)

    budget = max(0, max_closes - len(closes))
    closes = closes[:max_closes]
    folds, deferred = [], 0
    for k in sorted(groups, key=lambda key: groups[key][0]["number"]):
        members = groups[k]
        if len(members) < 2:
            continue
        canon, dups = members[0], members[1:]
        if budget <= 0:
            deferred += len(dups)
            continue
        take, rest = dups[:budget], dups[budget:]
        budget -= len(take)
        deferred += len(rest)
        have = set(tracked_prs(canon))
        announce = [d for d in take if not set(tracked_prs(d)) <= have]
        folds.append({
            "key": k,
            "canonical": canon["number"],
            "canonical_comment": fold_canonical_comment(k, canon, announce, len(members))
            if announce else None,
            "duplicates": [{"number": d["number"],
                            "comment": fold_duplicate_comment(k, canon["number"], d)}
                           for d in take],
        })
    # The quiet arm runs over what survives the two arms above, and a surviving
    # issue that is absorbing copies this run is judged on their specs as well.
    quiet = {"read": evidence is not None, "armed": bool(close_on_quiet),
             "classified": [], "closes": [], "would_close": []}
    if evidence is not None:
        folded_away = {d["number"] for f in folds for d in f["duplicates"]}
        by_number = {i["number"]: i for i in ordered}
        extra = {f["canonical"]: [p for d in f["duplicates"] for p in tracked_prs(by_number[d["number"]])]
                 for f in folds}
        candidates = [i for i in ordered
                      if i["number"] not in closing and i["number"] not in folded_away]
        quiet["classified"] = quiet_verdicts(candidates, pr_docs, corpus_dir, evidence,
                                             extra_prs=extra)
        proven = [{"number": q["number"], "specs": q["specs"],
                   "comment": quiet_close_comment(q["specs"])}
                  for q in quiet["classified"] if q["verdict"] == "quiet_proven"]
        if close_on_quiet:
            quiet["closes"], rest = proven[:budget], proven[budget:]
            deferred += len(rest)
        else:
            quiet["would_close"] = proven
    return {"corpus_ok": corpus_ok, "examined": len(ordered), "closes": closes,
            "folds": folds, "kept": kept, "deferred": deferred, "quiet": quiet}


# ── what the issues say ──────────────────────────────────────────────────

def _markers(prs) -> str:
    return " · ".join(f"spec-debt-for-pr-{p}" for p in prs)


def fold_canonical_comment(key: str, canon: dict, dups: list[dict], total: int) -> str:
    problem = key[len("prose:"):] if key.startswith("prose:") else key
    rows = []
    prs = []
    for d in dups:
        dprs = tracked_prs(d)
        prs.extend(dprs)
        rows.append(f"| #{d['number']} | {target_of(d['title']) or '—'} | "
                    + ", ".join(f"#{p}" for p in dprs) + " |")
    return (
        "### Same problem, filed again — folded here\n\n"
        f"The spec-debt tracker used to open one issue per merged spec PR, so this "
        f"problem (`{problem}`) was filed {total} times. The copies below are closed "
        f"as duplicates, and their spec PRs are tracked on this issue now.\n\n"
        "| issue | observed at | spec PR |\n|---|---|---|\n" + "\n".join(rows) + "\n\n"
        "This issue closes once every spec it tracks is closed in "
        "`docs/brain-proposals/`.\n\n"
        f"<sub>{_markers(prs)} · spec-debt-reconcile</sub>\n")


def fold_duplicate_comment(key: str, canon_number: int, dup: dict) -> str:
    problem = key[len("prose:"):] if key.startswith("prose:") else key
    target = target_of(dup["title"])
    return (
        f"Folded into #{canon_number} — the same problem (`{problem}`)"
        + (f", filed again for `{target}`" if target else "") + ". "
        f"Its spec PR is tracked there now, so this copy is closed as a duplicate.\n\n"
        f"<sub>spec-debt-folded-into-#{canon_number} · spec-debt-reconcile</sub>\n")


def ledger_close_comment(resolutions: list[dict], repo: str) -> str:
    rows = []
    for r in resolutions:
        link = f"https://github.com/{repo}/blob/main/docs/brain-proposals/{r['terminal']}"
        via = "" if r["terminal"] == r["doc"] else f" (via `{r['doc']}`)"
        rows.append(f"| #{r['pr']} | [{r['terminal']}]({link}){via} | {r['evidence']} |")
    return (
        "Closing — the spec ledger says the obligation this issue tracks is met.\n\n"
        "| spec PR | ledger doc | state |\n|---|---|---|\n" + "\n".join(rows) + "\n\n"
        "A doc counts as closed when every checklist item is checked. A doc closed "
        "as a copy of another doc counts only once that doc is closed too.\n\n"
        "<sub>spec-debt-reconcile · ledger-closed</sub>\n")


# ── network (gh) ─────────────────────────────────────────────────────────

def _gh(args: list[str], *, stdin: str | None = None) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, input=stdin,
                       timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def fetch_open_issues(repo: str, fields: str = "number,title,body,createdAt,comments"):
    return json.loads(_gh(["issue", "list", "--repo", repo, "--label", LABEL,
                           "--state", "open", "--limit", "1000", "--json", fields]))


def fetch_pr_docs(repo: str, prs) -> dict:
    out = {}
    for pr in sorted(set(prs)):
        try:
            names = _gh(["api", f"repos/{repo}/pulls/{pr}/files", "--paginate",
                         "--jq", ".[].filename"]).split()
        except RuntimeError:
            out[pr] = None
            continue
        out[pr] = [n for n in names
                   if n.startswith("docs/brain-proposals/") and n.endswith(".md")]
    return out


def _close(repo: str, number: int, reason: str) -> None:
    try:
        _gh(["api", "-X", "PATCH", f"repos/{repo}/issues/{number}",
             "-f", "state=closed", "-f", f"state_reason={reason}"])
    except RuntimeError:
        if reason != "duplicate":
            raise
        _gh(["api", "-X", "PATCH", f"repos/{repo}/issues/{number}",
             "-f", "state=closed", "-f", "state_reason=not_planned"])


def _comment(repo: str, number: int, body: str) -> None:
    _gh(["issue", "comment", str(number), "--repo", repo, "--body-file", "-"],
        stdin=body)


def quiet_close_comment(specs: list[dict]) -> str:
    rows = []
    for s in specs:
        t = s.get("target") or {}
        finding = f"`{t.get('issue', '')}`" + (f" @ `{t['url']}`" if t.get("url") else "")
        rows.append(f"| #{s['pr']} | {finding} | {s.get('reason', '')} |")
    return (
        "Closing — the brain finding behind every spec this issue tracks has provably "
        "stopped firing.\n\n"
        "| spec PR | finding | evidence |\n|---|---|---|\n" + "\n".join(rows) + "\n\n"
        "\"Stopped firing\" is not \"not seen lately\". Each finding's detector completed "
        "at least 6 recorded runs over at least 7 days without reporting it "
        "(`brain_detector_runs`), completed within the last day, and is reviewed as a "
        "detector whose silence means the target is healthy. If the problem comes back, "
        "the brain files it again.\n\n"
        "<sub>spec-debt-reconcile · quiet-closed</sub>\n")


class EvidenceUnavailable(RuntimeError):
    """The evidence endpoint could not be read. Never read as "nothing is firing"."""


def _curl_post_json(url: str, headers: dict, body: dict, max_time: float = 60.0):
    """POST JSON -> (status, text).

    Headers go to curl on stdin, never argv, so the admin key stays out of the
    process list (the scripts/wait_for_deployed_commit.py pattern);
    regression_lint blocks urllib.request.urlopen."""
    import tempfile

    def quoted(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as fh:
        json.dump(body, fh)
        path = fh.name
    config = "".join(f"header = {quoted(f'{k}: {v}')}\n" for k, v in headers.items())
    config += f"url = {quoted(url)}\n"
    try:
        proc = subprocess.run(
            ["curl", "-q", "-sS", "--max-time", f"{max_time:g}", "-X", "POST",
             "--data-binary", f"@{path}", "-w", "\n%{http_code}", "--config", "-"],
            input=config, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=max_time + 15)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise EvidenceUnavailable(f"curl did not complete: {e}"[:200])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if proc.returncode != 0:
        raise EvidenceUnavailable(f"no response (curl exit {proc.returncode}: "
                                  f"{proc.stderr.strip()[:160]})")
    text, _, status = proc.stdout.rpartition("\n")
    if not status.isdigit():
        raise EvidenceUnavailable(f"no HTTP status from curl ({status[:40]!r})")
    return int(status), text


def evidence_targets(issues: list[dict], pr_docs: dict, corpus_dir: str) -> list[dict]:
    """Every distinct finding behind the specs these issues track."""
    seen, out = set(), []
    for iss in issues:
        for p in tracked_prs(iss):
            for d in pr_docs.get(p) or []:
                t = spec_target(doc_heading(d, corpus_dir))
                if t and evidence_key(t["issue"], t["url"]) not in seen:
                    seen.add(evidence_key(t["issue"], t["url"]))
                    out.append(t)
    return out


def fetch_evidence(targets: list[dict], meta: dict | None = None) -> dict:
    """{evidence_key: {verdict, reason, detector_fn, ...}} from the backend.

    Raises EvidenceUnavailable on anything short of a MEASURED answer — a
    missing key, no response, a non-200, or a body that is not the evidence.
    `meta`, when given, receives the ledger's own extent (first/last sweep,
    sweep count) so a run can say how much evidence exists at all."""
    key = os.environ.get("DCHUB_ADMIN_KEY", "")
    if not key:
        raise EvidenceUnavailable("DCHUB_ADMIN_KEY is not set")
    base = (os.environ.get("DCHUB_BACKEND_BASE") or EVIDENCE_ORIGIN).rstrip("/")
    out = {}
    for i in range(0, len(targets), 400):
        status, text = _curl_post_json(
            base + EVIDENCE_PATH,
            {"X-Admin-Key": key, "Content-Type": "application/json", "User-Agent": EVIDENCE_UA},
            {"findings": targets[i:i + 400]})
        try:
            data = json.loads(text)
        except ValueError:
            raise EvidenceUnavailable(f"HTTP {status}; the body is not JSON: {text[:120]!r}")
        if status != 200 or not isinstance(data, dict) or data.get("state") != "MEASURED":
            why = (data.get("reason") or data.get("error") or data.get("state")) \
                if isinstance(data, dict) else "unexpected body"
            raise EvidenceUnavailable(f"HTTP {status}: {why}")
        for f in data.get("findings") or []:
            out[evidence_key(f.get("issue"), f.get("url"))] = f
        if meta is not None:
            meta["ledger"] = data.get("ledger")
    return out


# ── ticking a DOC whose findings provably stopped firing (2026-09-26) ──────
#
# The quiet arm above closes ISSUES. The obligation lives in the DOC, and
# nothing ticked one: /api/v1/brain/spec-debt counted the same 245 open docs
# whatever the evidence said. This ticks an OPEN doc's checklist only when
# EVERY target it stands for is quiet_proven — its own finding, plus each
# member rolled up into it by a class collapse ("— was `X.md`" roster lines).
# Any target that is firing, unproven, unmeasured, or not from brain_findings
# (a QA or prose spec) keeps the doc open. The ledger arm then closes the
# issues that tracked it. Written by .github/workflows/spec-debt-quiet-docs.yml.

DEFAULT_MAX_DOC_TICKS = 20
QUIET_DOC_MARKER = "(spec-debt quiet proof)"
_ROSTER_MEMBER_RE = re.compile(r"^- `.*` — was `([A-Za-z0-9][A-Za-z0-9._-]*\.md)`", re.M)
_TEMPLATE_BOX_RE = re.compile(
    r"^- \[ \] (Confirm this is still worth doing|Scope it to a concrete change "
    r"\(file\(s\) \+ approach\)|Implement \+ verify|Or (?:discard|close) this PR if "
    r"superseded / not worth it)\s*$", re.M)


def doc_targets(doc: str, corpus_dir: str) -> list[dict] | None:
    """Every brain_findings target an open doc stands for, or None when any of
    them cannot be named — an unnameable target can never be proven quiet."""
    try:
        with open(os.path.join(corpus_dir, doc), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    names = [doc] + [m for m in _ROSTER_MEMBER_RE.findall(text) if m != doc]
    out, seen = [], set()
    for name in names:
        t = spec_target(doc_heading(name, corpus_dir))
        if t is None:
            return None
        k = evidence_key(t["issue"], t["url"])
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def quiet_doc_plan(corpus_dir: str, evidence: dict, *, only=None,
                   max_ticks: int = DEFAULT_MAX_DOC_TICKS) -> dict:
    """Which open docs to tick. Pure given the evidence.

    ticks  [{doc, targets: [{issue, url, verdict, reason, detector_fn}]}]
    held   [{doc, reason}] — open docs with a nameable target that stay open"""
    mod = _ledger()
    ticks, held = [], []
    for name in sorted(os.listdir(corpus_dir)):
        if not name.endswith(".md") or (only is not None and name not in only):
            continue
        with open(os.path.join(corpus_dir, name), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        if mod.classify_doc_text(text) != mod.OPEN:
            continue
        targets = doc_targets(name, corpus_dir)
        if not targets:
            continue
        if len(_TEMPLATE_BOX_RE.findall(text)) != 4 or "- [ ]" in _TEMPLATE_BOX_RE.sub("", text):
            held.append({"doc": name, "reason": "checklist is not the 4-line template"})
            continue
        judged = []
        for t in targets:
            e = evidence.get(evidence_key(t["issue"], t["url"])) or {}
            judged.append({**t, "verdict": e.get("verdict") or "unmeasured",
                           "reason": e.get("reason") or "no evidence returned for it",
                           "detector_fn": e.get("detector_fn")})
        bad = [j for j in judged if j["verdict"] != "quiet_proven"]
        if bad:
            held.append({"doc": name, "reason": f"{len(bad)} of {len(judged)} target(s) not "
                         f"quiet_proven (first: {bad[0]['url']} — {bad[0]['verdict']})"})
            continue
        ticks.append({"doc": name, "targets": judged})
    return {"ticks": ticks[:max_ticks], "deferred": max(0, len(ticks) - max_ticks), "held": held}


def tick_doc_text(text: str, targets: list[dict], date: str) -> str:
    """The doc with its template checklist ticked and the evidence recorded."""
    n = len(targets)
    lines = "".join(f"- `{t['issue']}` @ `{t['url']}` — {t['reason']}\n" for t in targets)
    block = (f"## Triage — {date} {QUIET_DOC_MARKER} — CLOSED, stopped firing\n\n"
             f"Every target this doc stands for ({n}) is `quiet_proven` in the detector "
             f"ledger (routes/brain_detector_ledger.py): its detector completed repeated "
             f"recorded runs over days without reporting it, and that detector is reviewed "
             f"as absence-provable. Evidence at {date}:\n\n{lines}\n"
             f"If any of these fires again, the brain files a fresh spec for it.\n")
    reasons = [
        f"yes, and measured — all {n} target(s) stopped firing",
        "no change scoped — the evidence below shows the condition cleared",
        f"verified by the detector ledger: {n} of {n} target(s) quiet_proven",
        f"closed {date}: every target provably stopped firing {QUIET_DOC_MARKER}",
    ]
    i = [0]

    def rep(m):
        r = reasons[i[0]]
        i[0] += 1
        return f"- [x] {m.group(1)} — {r}"

    ticked = _TEMPLATE_BOX_RE.sub(rep, text)
    at = ticked.find("\n## Human checklist")
    if at < 0:
        at = ticked.find("\n- [x] Confirm this is still worth doing")
    return ticked[:at] + "\n" + block + ticked[at:]


def _quiet_docs_summary(p: dict, note: str, applied: bool) -> str:
    out = ["## spec-debt quiet docs", "",
           f"- evidence: {note}",
           f"- {'ticked' if applied else 'would tick (shadow)'}: {len(p['ticks'])}"
           + (f"; deferred past the cap: {p['deferred']}" if p["deferred"] else "")]
    for t in p["ticks"]:
        out.append(f"  - `{t['doc']}` ({len(t['targets'])} target(s))")
    out.append(f"- held open (a target not quiet_proven): {len(p['held'])}")
    for h in p["held"][:40]:
        out.append(f"  - `{h['doc']}`: {h['reason']}")
    return "\n".join(out) + "\n"


def apply(p: dict, repo: str, *, pause: float = 1.0) -> dict:
    """Carry out a plan. A copy is never closed unless its spec PRs were carried
    to the surviving issue first."""
    done = {"closed": [], "folded": [], "quiet_closed": [], "errors": []}
    for c in p["closes"]:
        try:
            _comment(repo, c["number"], c["comment"])
            time.sleep(pause)
            _close(repo, c["number"], "completed")
            time.sleep(pause)
            done["closed"].append(c["number"])
        except RuntimeError as e:
            done["errors"].append(f"#{c['number']}: {e}")
    for f in p["folds"]:
        if f["canonical_comment"]:
            try:
                _comment(repo, f["canonical"], f["canonical_comment"])
                time.sleep(pause)
            except RuntimeError as e:
                done["errors"].append(f"#{f['canonical']} (fold not carried over): {e}")
                continue
        for d in f["duplicates"]:
            try:
                _comment(repo, d["number"], d["comment"])
                time.sleep(pause)
                _close(repo, d["number"], "duplicate")
                time.sleep(pause)
                done["folded"].append(d["number"])
            except RuntimeError as e:
                done["errors"].append(f"#{d['number']}: {e}")
    for q in (p.get("quiet") or {}).get("closes") or []:
        try:
            _comment(repo, q["number"], q["comment"])
            time.sleep(pause)
            _close(repo, q["number"], "completed")
            time.sleep(pause)
            done["quiet_closed"].append(q["number"])
        except RuntimeError as e:
            done["errors"].append(f"#{q['number']}: {e}")
    return done


def _normalise_reason(reason) -> str:
    return re.sub(r"\d+(?:\.\d+)?", "#", str(reason or ""))[:110]


def _summary(p: dict, done: dict | None, evidence_note: str = "") -> str:
    lines = [
        "## spec-debt reconcile",
        "",
        f"- examined: {p['examined']} open spec-debt issue(s); corpus readable: {p['corpus_ok']}",
        f"- close (ledger says met): {len(p['closes'])} — "
        + (", ".join(f"#{c['number']}" for c in p["closes"]) or "none"),
        f"- fold (same problem): {sum(len(f['duplicates']) for f in p['folds'])} into "
        f"{len(p['folds'])} issue(s); deferred past the cap: {p['deferred']}",
    ]
    for f in p["folds"]:
        lines.append(f"  - #{f['canonical']} `{f['key']}` <- "
                     + ", ".join(f"#{d['number']}" for d in f["duplicates"]))
    q = p.get("quiet") or {}
    if q.get("read"):
        from collections import Counter
        verdicts = Counter(c["verdict"] for c in q.get("classified") or [])
        lines.append(f"- quiet ({'ARMED' if q.get('armed') else 'shadow'}; evidence "
                     f"{evidence_note}): " + ", ".join(
                         f"{k} {verdicts.get(k, 0)}"
                         for k in ("firing", "quiet_proven", "quiet_unproven", "unmeasured")))
        acting = q.get("closes") if q.get("armed") else q.get("would_close")
        lines.append(f"  - {'closing' if q.get('armed') else 'would close'}: "
                     + (", ".join(f"#{c['number']}" for c in acting or []) or "none"))
        reasons = Counter(_normalise_reason(s.get("reason"))
                          for c in q.get("classified") or [] for s in c["specs"]
                          if s["verdict"] in ("quiet_unproven", "unmeasured"))
        lines += [f"  - {n} × {reason}" for reason, n in reasons.most_common(6)]
    else:
        lines.append(f"- quiet: not run — evidence {evidence_note or 'not read'}")
    if done is None:
        lines.append("- DRY RUN — nothing written")
    else:
        lines.append(f"- written: closed {len(done['closed'])}, folded {len(done['folded'])}, "
                     f"quiet-closed {len(done.get('quiet_closed') or [])}, "
                     f"errors {len(done['errors'])}")
        lines += [f"  - {e}" for e in done["errors"]]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("class-key")
    k.add_argument("--title", required=True)
    f = sub.add_parser("find-class-issue")
    f.add_argument("--repo", required=True)
    f.add_argument("--title", required=True)
    r = sub.add_parser("reconcile")
    r.add_argument("--repo", required=True)
    r.add_argument("--corpus", required=True)
    r.add_argument("--apply", action="store_true")
    r.add_argument("--max-closes", type=int, default=DEFAULT_MAX_CLOSES)
    r.add_argument("--summary", default="")
    r.add_argument("--plan-json", default="")
    r.add_argument("--no-evidence", action="store_true",
                   help="skip the quiet arm and its evidence read")
    r.add_argument("--require-evidence", action="store_true",
                   help="exit 2 when the evidence endpoint could not be read")
    q = sub.add_parser("quiet-docs")
    q.add_argument("--corpus", required=True)
    q.add_argument("--apply", action="store_true", help="edit the docs in --corpus")
    q.add_argument("--date", default=time.strftime("%Y-%m-%d", time.gmtime()))
    q.add_argument("--max", type=int, default=DEFAULT_MAX_DOC_TICKS)
    q.add_argument("--only", default="", help="comma-separated doc names")
    q.add_argument("--verify", action="store_true",
                   help="exit 1 unless every --only doc would still be ticked")
    q.add_argument("--summary", default="")
    args = ap.parse_args(argv)

    if args.cmd == "quiet-docs":
        only = set(filter(None, args.only.split(","))) or None
        targets, seen = [], set()
        for name in sorted(os.listdir(args.corpus)):
            if name.endswith(".md") and (only is None or name in only):
                for t in doc_targets(name, args.corpus) or []:
                    k = evidence_key(t["issue"], t["url"])
                    if k not in seen:
                        seen.add(k)
                        targets.append(t)
        try:
            meta = {}
            evidence = fetch_evidence(targets, meta) if targets else {}
        except EvidenceUnavailable as e:
            print(f"UNMEASURED — evidence unreadable: {e}")
            return 2
        ledger = meta.get("ledger") or {}
        note = (f"read for {len(evidence)} of {len(targets)} target(s); detector ledger: "
                f"{ledger.get('sweeps', 0)} sweep(s) since {ledger.get('first_sweep') or 'never'}")
        p = quiet_doc_plan(args.corpus, evidence, only=only,
                           max_ticks=10 ** 6 if args.verify else args.max)
        if args.verify:
            still = {t["doc"] for t in p["ticks"]}
            lost = sorted((only or set()) - still)
            for h in p["held"]:
                print(f"{h['doc']}: {h['reason']}")
            print("VERIFIED" if not lost else f"NO LONGER QUIET: {', '.join(lost)}")
            return 0 if not lost else 1
        if args.apply:
            for t in p["ticks"]:
                path = os.path.join(args.corpus, t["doc"])
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(tick_doc_text(text, t["targets"], args.date))
        text = _quiet_docs_summary(p, note, args.apply)
        print(text)
        if args.summary:
            with open(args.summary, "a", encoding="utf-8") as fh:
                fh.write(text)
        return 0

    if args.cmd == "class-key":
        print(class_key(args.title))
        return 0

    if args.cmd == "find-class-issue":
        key = class_key(args.title)
        if not key:
            return 0
        issues = fetch_open_issues(args.repo, "number,title")
        hits = sorted(i["number"] for i in issues if class_key(i["title"]) == key)
        if hits:
            print(hits[0])
        return 0

    issues = fetch_open_issues(args.repo)
    prs = [p for i in issues for p in tracked_prs(i)]
    pr_docs = fetch_pr_docs(args.repo, prs)
    evidence, evidence_note = None, "skipped (--no-evidence)"
    if not args.no_evidence:
        try:
            targets, meta = evidence_targets(issues, pr_docs, args.corpus), {}
            evidence = fetch_evidence(targets, meta) if targets else {}
            ledger = meta.get("ledger") or {}
            evidence_note = (f"read for {len(evidence)} of {len(targets)} finding(s); detector "
                             f"ledger: {ledger.get('sweeps', 0)} sweep(s) since "
                             f"{ledger.get('first_sweep') or 'never'}")
        except EvidenceUnavailable as e:
            evidence_note = f"UNAVAILABLE — {e}"
    p = plan(issues, pr_docs, args.corpus, repo=args.repo, max_closes=args.max_closes,
             evidence=evidence,
             close_on_quiet=os.environ.get("SPEC_DEBT_CLOSE_ON_QUIET", "0") == "1")
    if args.plan_json:
        with open(args.plan_json, "w", encoding="utf-8") as fh:
            json.dump(p, fh, indent=1)
    done = apply(p, args.repo) if args.apply else None
    text = _summary(p, done, evidence_note)
    print(text)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    if done and done["errors"]:
        return 1
    # After every other arm has run: an unreadable evidence endpoint is a
    # failed run, so a quiet arm that silently never works cannot look green.
    return 2 if evidence is None and args.require_evidence else 0


if __name__ == "__main__":
    sys.exit(main())
