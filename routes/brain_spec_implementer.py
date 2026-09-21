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
spec written in step 2 exists precisely to make it concrete — its unchecked
`- [ ]` items are the implementation steps a human was meant to follow. So the
fix is not to force the old directive through; it is to re-drive the SAME code
drafter with the spec's own obligations as the directive. New input, not a
bypassed guard.

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
        if len(item) >= _MIN_OBLIGATION_CHARS:
            out.append(item)
    return out


def build_directive(spec_name: str, title: str, obligations: list) -> str:
    """The directive handed to the CODE drafter.

    ★ It says IMPLEMENT and names the spec. The original directive failed
    because it asked for a plan; this one carries the plan's own steps and
    asks for the edit. It also states that filing another spec is not an
    acceptable answer — the spec fallback is exactly what produced the loop,
    and without this line the drafter's cheapest exit is to re-file one.
    """
    steps = "\n".join(f"{i}. {o}" for i, o in enumerate(obligations[:12], 1))
    return (
        f"IMPLEMENT the already-approved spec docs/brain-proposals/{spec_name}"
        f"{(' — ' + title) if title else ''}.\n\n"
        f"The spec is MERGED and its design is settled; what is missing is the "
        f"code. Make the smallest real code change that satisfies these "
        f"outstanding obligations:\n\n{steps}\n\n"
        f"Do NOT file another spec or design document — one already exists and "
        f"filing a second is what this task exists to stop. If you cannot "
        f"express this as a concrete code edit, refuse and say which "
        f"obligation blocks you."
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
    title = ""
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    if state != "open" or not obligations:
        return {"ok": True, "spec": spec_name, "state": state,
                "obligations": obligations, "would_act": False,
                "reason": ("spec has no unchecked obligations — nothing to "
                           "implement" if state != "open" else
                           "obligations present but all below the "
                           f"{_MIN_OBLIGATION_CHARS}-char floor (headings, "
                           "not steps)")}
    return {"ok": True, "spec": spec_name, "state": state, "title": title,
            "obligations": obligations, "would_act": True,
            "directive": build_directive(spec_name, title, obligations)}


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


def sweep(limit: int = 1, apply: bool = False) -> dict:
    """Drive the oldest open obligations. Capped, dry-run by default."""
    try:
        from routes.brain_spec_debt import spec_debt_summary
        summary = spec_debt_summary()
    except Exception as e:  # pragma: no cover
        return {"ok": False, "reason": f"debt summary unavailable: {e}"}
    items = summary.get("open_obligations")
    if not isinstance(items, list):
        # ★ null/absent is UNMEASURED. An empty result here must never render
        # as "no debt" — the debt book itself reports UNMEASURED rather than
        # zero for exactly this reason.
        return {"ok": False, "reason": "debt book returned no obligation list",
                "state": summary.get("state")}
    n = max(1, min(int(limit or 1), _MAX_PER_CALL))
    results = []
    for it in items[:n]:
        # `doc` is the debt book's key (verified against the live payload
        # 2026-09-21); `file` is accepted only so a rename there degrades to a
        # skip rather than to a silent sweep over empty names.
        name = (it or {}).get("doc") or (it or {}).get("file") or ""
        if not name:
            continue
        results.append(implement_spec(name, apply=apply))
    return {"ok": True, "apply": bool(apply), "armed": _armed(),
            "cap": _MAX_PER_CALL, "requested": limit, "ran": len(results),
            "open_obligations_total": summary.get("open_obligations_total"),
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
