"""brain_spec_implementer.py — drive a LANDED spec to an implementation.

THE LOOP THIS BREAKS (measured live 2026-09-21)
===============================================
1. The brain finds a problem and the CODE drafter refuses the directive —
   it is a "build/instrument/gather" plan, not a single-file edit.
2. The spec fallback files `docs/brain-proposals/<item>.md`. That PR merges.
3. Next cycle, `brain_pr_opener.open_spec_pr` hits the landed-spec fingerprint
   dedup and returns `awaiting_implementation: True` with a note that says, in
   words, "needs an implementation, not another spec".
4. NOTHING CONSUMES THAT. The finding re-fires and the cycle repeats.

Readings on 2026-09-21: 106 of 111 `brain-spec:` merges since 09-01 changed
only `docs/` (95%). 376 landed specs, 238 with open obligations. All 30
declined approvals on the innovation board carry that same dedup note, and
`can_override` was false on every one of the 45 board items.

★ WHY THE OBVIOUS FIX DOES NOT WORK. The tempting change is
`can_override=True` on the `declined` branch of `approval_view()`, mirroring
what be#4622 did for `withheld`. It would be an INERT BUTTON: `override` is
only ever consulted against `_pr_block_reason(_verdict)` — the VERDICT gate
(brain_innovation_dashboard:1005, :1024). The decline comes from the drafter's
fingerprint dedup, which `override` never reaches. Pressing it would re-POST,
pass a gate that was not blocking, hit the identical dedup and decline again.

★ THE ACTUAL INSIGHT. Step 1 refused because the DIRECTIVE was too vague. The
spec written in step 2 carries the content that was missing, so the fix is not
to force the old directive through — it is to re-drive the SAME code drafter
with the spec's own words. New input, not a bypassed guard.

★★★ CORRECTED 2026-09-21, BY THE DRY RUN, BEFORE ANYTHING WAS ARMED. The first
version of this module took that content from the spec's unchecked `- [ ]`
items. That was wrong. Measured over all 380 landed specs: of the 242 with
unchecked obligations, **242 carry ONLY a 4-line template and ZERO carry a
real implementation step.** The directive it produced for agenda-41 read "Make
the smallest real code change that satisfies: 1. Confirm this is still worth
doing" — not a task. Driving the drafter with that 242 times would have
produced 242 refusals or 242 bad edits.

The real content is `## The approved recommendation`: present in 242 of 242,
median 375 characters. That is what the drafter now receives. The checklist is
stripped (see _BOILERPLATE_OBLIGATIONS) and only genuinely non-template items
are appended.

★ Specs whose triage recorded BLOCKED are SKIPPED. Their boxes are unchecked
on purpose — they wait on an owner decision, not on engineering. Only 1 of 242
today, but it is the OLDEST, so an age-ordered sweep hits it first.

READ-PATH REUSE. Classification comes from `brain_spec_debt` (Phase 0,
read-only, mutation-tested) rather than a second copy of the rules — two
readers of one corpus drift, and the debt book is the one that is already
guarded.

DRY-RUN BY DEFAULT. `implement_spec(..., apply=False)` returns exactly what it
would do and opens nothing. Actuation requires BOTH `apply=True` and
`SPEC_IMPLEMENTER_ARM=1`, so an accidental call cannot open PRs.

Kill: SPEC_IMPLEMENTER_DISABLE=1  ·  Arm: SPEC_IMPLEMENTER_ARM=1
Surface: POST /api/v1/brain/spec-debt/implement   (admin-gated)
"""
from __future__ import annotations

import os
import re
import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
brain_spec_implementer_bp = Blueprint("brain_spec_implementer", __name__)

#: Matches an unchecked obligation and captures its text. Deliberately the same
#: anchor brain_spec_debt._UNCHECKED_RE uses to COUNT them — if the two ever
#: disagree, the queue and the actuator are reading different documents.
_UNCHECKED_ITEM_RE = re.compile(r"^\s*[-*]\s+\[ \]\s*(.+?)\s*$", re.M)

#: A spec whose obligations are all one or two words is not implementable; it
#: is a heading list. Driving the drafter with it wastes a model call and
#: produces a refusal that looks like the drafter's judgement.
_MIN_OBLIGATION_CHARS = 12

#: ★★★ THE CHECKLIST IS BOILERPLATE. Measured over all 380 landed specs on
#: origin/main 2026-09-21: of the 242 carrying unchecked obligations, **242
#: contain ONLY these lines and ZERO contain a real implementation step.**
#: The first version of this module built the directive from the checklist, on
#: the premise that the spec makes the refused directive concrete. The premise
#: was FALSE and the dry run is what caught it — the directive produced for
#: agenda-41 read "Make the smallest real code change that satisfies: 1.
#: Confirm this is still worth doing", which is not a task.
#:
#: The real content is `## The approved recommendation` — present in 242 of
#: 242, median 375 characters. That is what the drafter gets.
_BOILERPLATE_OBLIGATIONS = frozenset({
    "confirm this is still worth doing",
    "scope it to a concrete change (file(s) + approach)",
    "implement + verify",
    "or close this pr if superseded / not worth it",
    "or discard this pr if superseded / not worth it",
})

#: The section carrying the actual recommendation.
_REC_RE = re.compile(
    r"^##\s+The approved recommendation\s*$(.*?)(?=^##\s|\Z)", re.M | re.S)

#: A spec whose triage recorded BLOCKED left its boxes unchecked ON PURPOSE —
#: it is waiting on an owner decision, not on engineering. Driving a code
#: drafter at a funding call is worse than doing nothing. Rare (1 of 242) but
#: it is the oldest item, so a sweep ordered by age hits it FIRST.
_BLOCKED_RE = re.compile(r"\bBLOCKED\b")

#: Hard ceiling per call. The debt book holds 238 open obligations; an
#: unbounded sweep would open hundreds of PRs on one button press.
_MAX_PER_CALL = 3


def _disabled() -> bool:
    return (os.environ.get("SPEC_IMPLEMENTER_DISABLE") or "").strip() == "1"


def _armed() -> bool:
    return (os.environ.get("SPEC_IMPLEMENTER_ARM") or "").strip() == "1"


def unchecked_obligations(text: str) -> list:
    """The spec's unchecked checklist items, in document order.

    Pure — the CI unit-tests job installs pytest only, so the guard
    AST-extracts this rather than importing the module.
    """
    out = []
    for m in _UNCHECKED_ITEM_RE.finditer(text or ""):
        item = (m.group(1) or "").strip()
        # Strip markdown emphasis so the directive reads as an instruction
        # rather than as formatting the drafter has to see past.
        # ★ UNDERSCORES ARE DELIBERATELY NOT STRIPPED. The first version used
        # `[*_`]+` and turned "backfill bar_table" into "backfill bartable" —
        # this string is an instruction to a CODE drafter, so a mangled
        # identifier is a wrong edit, not a cosmetic slip. Markdown emphasis
        # via underscore is rare in these docs; snake_case is everywhere.
        item = re.sub(r"[*`]+", "", item).strip()
        if len(item) < _MIN_OBLIGATION_CHARS:
            continue
        # ★ Drop the template. Keeping it produced directives whose steps were
        # "Confirm this is still worth doing" — see _BOILERPLATE_OBLIGATIONS.
        if item.lower() in _BOILERPLATE_OBLIGATIONS:
            continue
        out.append(item)
    return out


def approved_recommendation(text: str) -> str:
    """The spec's `## The approved recommendation` body — the real content.

    "" when the section is absent, which the caller must treat as
    not-implementable rather than as an empty instruction.
    """
    m = _REC_RE.search(text or "")
    if not m:
        return ""
    body = m.group(1)
    # Drop blockquote framing and the _Filed stamp; keep the prose.
    lines = [ln.strip() for ln in body.splitlines()]
    lines = [ln for ln in lines
             if ln and not ln.startswith(">") and not ln.startswith("_Filed")]
    return " ".join(lines).strip()


def is_blocked(text: str) -> bool:
    """True when triage recorded BLOCKED — unchecked ON PURPOSE."""
    return bool(_BLOCKED_RE.search(text or ""))


def build_directive(spec_name: str, title: str, recommendation: str,
                    obligations: list) -> str:
    """The directive handed to the CODE drafter.

    ★ It says IMPLEMENT and names the spec. The original directive failed
    because it asked for a plan; this one carries the plan's own steps and
    asks for the edit. It also states that filing another spec is not an
    acceptable answer — the spec fallback is exactly what produced the loop,
    and without this line the drafter's cheapest exit is to re-file one.
    """
    body = (
        f"IMPLEMENT the already-approved spec docs/brain-proposals/{spec_name}"
        f"{(' — ' + title) if title else ''}.\n\n"
        f"The spec is MERGED and its design is settled; what is missing is the "
        f"code.\n\nTHE APPROVED RECOMMENDATION:\n{recommendation.strip()}\n\n"
    )
    # Only real steps reach the directive; the 4-line template is stripped
    # upstream, so `obligations` is usually empty and this block is skipped.
    if obligations:
        steps = "\n".join(f"{i}. {o}" for i, o in enumerate(obligations[:12], 1))
        body += f"OUTSTANDING OBLIGATIONS:\n{steps}\n\n"
    return body + (
        "Make the smallest real code change that delivers that recommendation. "
        "Do NOT file another spec or design document — one already exists and "
        "filing a second is what this task exists to stop. If the "
        "recommendation needs a decision rather than an edit, or you cannot "
        "express it as a concrete code change, refuse and say why."
    )


def plan_for_spec(spec_name: str, corpus=None) -> dict:
    """What implementing one landed spec would involve. Opens nothing.

    Returns `ok:False` with a reason rather than raising, so the caller can
    render the refusal instead of a stack trace.
    """
    try:
        from routes.brain_spec_debt import corpus_dir, classify_doc_text
    except Exception as e:  # pragma: no cover
        return {"ok": False, "reason": f"spec debt reader unavailable: {e}"}
    d = corpus or corpus_dir()
    path = os.path.join(d, spec_name)
    if not os.path.isfile(path):
        # ★ NOT "no obligations". A spec we cannot read is UNMEASURED, and the
        # debt book makes the same distinction for the same reason: a reader
        # that reclassifies what it cannot see flatters itself.
        return {"ok": False, "spec": spec_name, "reason": "spec not found in corpus"}
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except Exception as e:
        return {"ok": False, "spec": spec_name, "reason": f"unreadable: {e}"}

    state = classify_doc_text(text)
    obligations = unchecked_obligations(text)
    recommendation = approved_recommendation(text)
    title = ""
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    common = {"ok": True, "spec": spec_name, "state": state, "title": title,
              "obligations": obligations,
              "recommendation_chars": len(recommendation)}
    if state != "open":
        return {**common, "would_act": False,
                "reason": "spec has no unchecked obligations — nothing to do"}
    if is_blocked(text):
        # ★ Unchecked ON PURPOSE. This is the one refusal that must not be
        # read as a failure of the actuator.
        return {**common, "would_act": False, "blocked": True,
                "reason": ("triage recorded BLOCKED — this spec waits on an "
                           "owner decision, not on code")}
    if not recommendation:
        # ★ NOT "nothing to implement". A spec we cannot read the
        # recommendation out of is UNMEASURED; saying otherwise would let a
        # heading rename silently empty the queue.
        return {**common, "would_act": False,
                "reason": ("no '## The approved recommendation' section — "
                           "cannot build a directive from this spec")}
    return {**common, "would_act": True,
            "recommendation": recommendation,
            "directive": (build_directive(spec_name, title, recommendation,
                                          obligations)
                          + _spec_lessons(title))}


def _spec_lessons(title: str) -> str:
    """What past attempts on this spec's finding family taught
    (routes/brain_lessons.py), or "". The drafter's own label is
    "implement <doc>", which names no finding, so the lookup happens here
    where the spec's heading does."""
    try:
        h = title or ""
        if h.startswith("Brain proposal") and "\u2014" in h:
            h = h.split("\u2014", 1)[1].strip()
        target = _load_spec_debt_issues().spec_target(h) or {}
        issue = target.get("issue") if isinstance(target, dict) else None
        if not issue:
            return ""
        from routes.brain_lessons import lessons_for
        return lessons_for(issue)
    except Exception:  # noqa: BLE001 — lessons must never block a plan
        return ""


def implement_spec(spec_name: str, kind: str = "spec", item_id: int = 0,
                   apply: bool = False) -> dict:
    """Drive ONE landed spec to a code PR. NEVER raises.

    apply=False (the default) returns the plan and opens nothing.
    """
    if _disabled():
        return {"ok": False, "reason": "SPEC_IMPLEMENTER_DISABLE=1"}
    plan = plan_for_spec(spec_name)
    if not plan.get("ok") or not plan.get("would_act"):
        return plan
    plan["armed"] = _armed()
    if not apply:
        plan["acted"] = False
        plan["note"] = "dry run — pass apply=true to open a PR"
        return plan
    if not _armed():
        # ★ Two independent switches. apply=true alone cannot open a PR, so a
        # mis-scripted call or a curl with one flag too many is inert.
        plan["acted"] = False
        plan["note"] = "apply requested but SPEC_IMPLEMENTER_ARM is not set"
        return plan
    try:
        from routes.brain_guardrails import draft_and_open_pr
        pr = draft_and_open_pr(
            plan["directive"], "",
            label=f"implement {spec_name[:70]}")
        if not isinstance(pr, dict):
            return {**plan, "acted": False, "ok": False,
                    "error": "drafter returned a non-dict"}
        # ★ The drafter's spec fallback is NOT wired here on purpose. In
        # _attempt_pr a refusal falls back to open_spec_pr; doing that here
        # would file a spec about a spec, which is the loop with an extra
        # step. A refusal is recorded as a refusal.
        return {**plan, "acted": bool(pr.get("acted")), "pr": pr}
    except Exception as e:  # noqa: BLE001
        logger.warning("[spec-implementer] %s failed: %s", spec_name, str(e)[:160])
        return {**plan, "acted": False, "ok": False, "error": str(e)[:300]}


#: Verdict precedence for choosing what to drive. ★ Only FIRING is evidence
#: that work is needed. `quiet_*` means the finding stopped firing — measured
#: 2026-09-21, 57 of the open specs were quiet, including all 29
#: iso_metric_count_zero_24h specs whose cause was fixed on 2026-09-07 (#4097)
#: four days AFTER they were filed. `unmeasured` is NOT quiet and NOT firing;
#: it is ranked after firing so a driven spec is always one we can justify.
_DRIVE_RANK = {"firing": 0, "unmeasured": 1, None: 2}


def plan_sweep(docs: list, verdicts: dict) -> dict:
    """Choose what to drive. PURE — no DB, no network, no clock.

    docs      [{"doc": name, "target": {issue,url,url_prefix} | None}, ...]
    verdicts  {(issue, url): verdict}  from brain_detector_ledger.read_evidence

    ★ THREE FIXES, EACH MEASURED ON 2026-09-21:

    1. EVERY open doc, not the debt book's preview. spec_debt_summary()'s
       `open_obligations` is TRIMMED — 25 of 242. The old sweep iterated it,
       so it could only ever consider the first 25.
    2. FOLD THE FAN-OUT. One finding filed once per SITE — 29 x
       iso_metric_count_zero_24h, one per ISO code — is one problem. Grouping
       by the finding's `issue` takes 242 open specs to 165 units. The
       representative CARRIES its sites: `facility_duplicates_unmarked x 7`
       may be seven genuinely different duplicates, so they are handed on,
       not discarded.
    3. DRIVE ONLY WHAT IS STILL HAPPENING. A quiet finding is skipped. The old
       order was the debt book's AGE order, whose oldest item is the single
       spec BLOCKED on an owner decision — so a naive sweep hit it first.

    Specs with no finding target (prose / agenda) are returned separately as
    `needs_human` — nothing here can tell whether they are still wanted.
    """
    groups, needs_human = {}, []
    for d in docs or []:
        t = (d or {}).get("target")
        if not t:
            needs_human.append(d.get("doc"))
            continue
        groups.setdefault(t.get("issue") or "", []).append(d)

    drive, skipped_quiet = [], []
    for issue, members in groups.items():
        vs = [verdicts.get((m["target"]["issue"], m["target"]["url"]))
              for m in members]
        # A class is quiet only if EVERY site is quiet. One live site is
        # enough to keep it — the fan-out must not hide a real firing.
        if vs and all(str(v or "").startswith("quiet") for v in vs):
            skipped_quiet.append({"issue": issue, "sites": len(members)})
            continue
        # ★ ONLY LIVE SITES COUNT. The first version reported every member,
        # so iso_metric_count_zero_24h ranked FIRST as "29 sites" — measured
        # 2026-09-21, 26 of those 29 were quiet (fixed 09-07 by #4097), 1
        # unmeasured, and only WACM and WAUW still firing. Sorting on the raw
        # member count would put a 2-site problem at the top of the queue
        # dressed as a 29-site one. Quiet sites are reported, never counted.
        live_members = [(m, v) for m, v in zip(members, vs)
                        if not str(v or "").startswith("quiet")]
        best = min((_DRIVE_RANK.get(v, 2) for _m, v in live_members), default=2)
        rep = live_members[0][0]
        drive.append({"issue": issue, "doc": rep["doc"], "rank": best,
                      "verdict": [k for k, r in _DRIVE_RANK.items()
                                  if r == best][0],
                      "sites": [m["target"]["url"] for m, _v in live_members],
                      "site_count": len(live_members),
                      "quiet_sites": len(members) - len(live_members)})
    drive.sort(key=lambda x: (x["rank"], -x["site_count"], x["issue"]))
    return {"drive": drive, "skipped_quiet": skipped_quiet,
            "needs_human": needs_human,
            "counts": {"open_docs": len(docs or []), "classes": len(groups),
                       "drive": len(drive), "skipped_quiet": len(skipped_quiet),
                       "needs_human": len(needs_human)}}


def _load_spec_debt_issues():
    """scripts/spec_debt_issues.py by path. It is pure (no network, no clock)
    and owns spec_target(); loading it keeps one parser, not two. scripts/ is
    not a package, hence the file-location load."""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "scripts", "spec_debt_issues.py")
    sp = importlib.util.spec_from_file_location("_spec_debt_issues", path)
    mod = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(mod)
    return mod


def _open_docs_with_targets() -> list:
    """Every OPEN doc in the corpus with its parsed finding target."""
    from routes.brain_spec_debt import corpus_dir, classify_doc_text
    sdi = _load_spec_debt_issues()
    d = corpus_dir()
    out = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".md"):
            continue
        try:
            text = open(os.path.join(d, name), encoding="utf-8",
                        errors="replace").read()
        except Exception:
            continue
        if classify_doc_text(text) != "open" or is_blocked(text):
            continue
        h = next((ln[2:].strip() for ln in text.splitlines()
                  if ln.startswith("# ")), "")
        # The filer frames every heading as "Brain proposal — <finding>";
        # spec_target() parses the finding, so strip the frame first.
        if h.startswith("Brain proposal") and "\u2014" in h:
            h = h.split("\u2014", 1)[1].strip()
        out.append({"doc": name, "target": sdi.spec_target(h)})
    return out


def sweep(limit: int = 1, apply: bool = False) -> dict:
    """Drive open specs whose finding is STILL FIRING, one per problem class.

    Capped, dry-run by default. See plan_sweep() for why each rule exists.
    """
    try:
        docs = _open_docs_with_targets()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "state": "UNMEASURED",
                "reason": f"could not read the corpus: {str(e)[:160]}"}
    targets = [d["target"] for d in docs if d["target"]]
    try:
        from routes.brain_detector_ledger import read_evidence
        ev = read_evidence(targets) if targets else {"findings": []}
    except Exception as e:  # noqa: BLE001
        # ★ No evidence is NOT "everything is firing". Without verdicts the
        # quiet filter cannot run, and driving blind is exactly how 29 PRs
        # would be opened against a bug fixed two weeks earlier.
        return {"ok": False, "state": "UNMEASURED",
                "reason": f"finding evidence unavailable: {str(e)[:160]}"}
    verdicts = {(f.get("issue"), f.get("url")): f.get("verdict")
                for f in (ev.get("findings") or [])}
    plan = plan_sweep(docs, verdicts)
    n = max(1, min(int(limit or 1), _MAX_PER_CALL))
    results = [dict(implement_spec(item["doc"], apply=apply),
                    issue=item["issue"], verdict=item["verdict"],
                    site_count=item["site_count"])
               for item in plan["drive"][:n]]
    return {"ok": True, "apply": bool(apply), "armed": _armed(),
            "cap": _MAX_PER_CALL, "requested": limit, "ran": len(results),
            "counts": plan["counts"],
            "skipped_quiet": plan["skipped_quiet"][:20],
            "next_up": [{k: v for k, v in x.items() if k != "sites"}
                        for x in plan["drive"][:10]],
            "results": results}


@brain_spec_implementer_bp.route("/api/v1/brain/spec-debt/implement",
                                 methods=["POST"])
def implement_endpoint():
    try:
        from routes.brain_mechanical_classifier import _admin_ok
    except Exception:
        return jsonify(ok=False, error="admin gate unavailable"), 503
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    body = request.get_json(silent=True) or {}
    apply_ = bool(body.get("apply"))
    spec = (body.get("spec") or "").strip()
    if spec:
        # Defend the corpus path: a spec name is a bare filename, never a path.
        if "/" in spec or "\\" in spec or spec.startswith("."):
            return jsonify(ok=False, error="spec must be a bare filename"), 400
        return jsonify(**implement_spec(spec, apply=apply_))
    try:
        limit = int(body.get("limit") or 1)
    except Exception:
        limit = 1
    return jsonify(**sweep(limit=limit, apply=apply_))
