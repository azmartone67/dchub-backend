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


def plan(issues: list[dict], pr_docs: dict, corpus_dir: str, *,
         repo: str = "azmartone67/dchub-backend",
         max_closes: int = DEFAULT_MAX_CLOSES) -> dict:
    """What to close and what to fold. Writes nothing.

    issues   open spec-debt issues: number, title, body, comments[{body}]
    pr_docs  {pr_number: [doc paths] | None}; None = the files could not be read
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
    return {"corpus_ok": corpus_ok, "examined": len(ordered), "closes": closes,
            "folds": folds, "kept": kept, "deferred": deferred}


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


def apply(p: dict, repo: str, *, pause: float = 1.0) -> dict:
    """Carry out a plan. A copy is never closed unless its spec PRs were carried
    to the surviving issue first."""
    done = {"closed": [], "folded": [], "errors": []}
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
    return done


def _summary(p: dict, done: dict | None) -> str:
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
    if done is None:
        lines.append("- DRY RUN — nothing written")
    else:
        lines.append(f"- written: closed {len(done['closed'])}, folded {len(done['folded'])}, "
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
    args = ap.parse_args(argv)

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
    p = plan(issues, fetch_pr_docs(args.repo, prs), args.corpus, repo=args.repo,
             max_closes=args.max_closes)
    if args.plan_json:
        with open(args.plan_json, "w", encoding="utf-8") as fh:
            json.dump(p, fh, indent=1)
    done = apply(p, args.repo) if args.apply else None
    text = _summary(p, done)
    print(text)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    return 1 if done and done["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
