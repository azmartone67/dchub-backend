"""
brain_scaffold_reconcile.py — reconcile merged scaffolds against evidence
(2026-08-31).

WHY THIS EXISTS
───────────────
Brain L6 opens a scaffold PR per strategic rec: a spec under docs/strategic/
and a never-imported stub at routes/_proposed_<slug>.py. Humans merge them.
Nothing ever looks at them again.

`expire_stale_draft_prs` drains stale DRAFTS, so it cannot help: a merged
scaffold is a file in the tree, not an open PR. The result is an unbounded
pile — 33 files at the time of writing, the oldest 7 weeks old — including
three built on one sentinel verdict that be#3448 later showed never held
(the server drops the OAuth challenge on `initialize` by design). Those
three had to be deleted by hand in be#3458 / be#3459.

WHAT THIS CAN AND CANNOT DECIDE
───────────────────────────────
The obvious design — "re-resolve each cited evidence key; if it no longer
resolves the scaffold is stale" — is WRONG, and wrong in the same way the
WorkOS probe was wrong. Measured across the 33 scaffolds in the tree, 34 of
92 cited keys (36%) name a root that is not a context source at all:

    competitor_signal   (the context key is `competitors`)
    customer_asks       (the context key is `feedback`)
    recidivist          (the context key is `recidivism`)
    past_lessons, market_news, news, now — no such source

Those citations never resolved, on any day. Treating "does not resolve now"
as "went stale" would manufacture a stale verdict for a third of the corpus
on evidence that says nothing of the kind.

Distinguishing "went stale" from "never resolved" needs a draft-time
snapshot of the resolved VALUE, which historical recs do not carry. So this
module refuses to emit a staleness verdict it cannot support, and reports
four honest states per citation instead:

    RESOLVES     the path walks cleanly in the live context
    MALFORMED    the root is not a context source — provable from the
                 schema alone, no baseline needed. The citation could not
                 have resolved when it was written either.
    UNRESOLVED   the root IS a real source but the rest of the path does
                 not walk today. Could be drift, could be a wrong subpath.
                 INDETERMINATE without a baseline — never called stale.
    SOURCE_DOWN  that source failed to load this run. Says nothing.

and one scaffold-level verdict that is always defensible:

    CITATIONS_ALL_MALFORMED  every citation names a non-existent source, so
                             the scaffold rests entirely on invented
                             provenance. Actionable now.
    CITATIONS_RESOLVE        at least one citation still walks.
    INDETERMINATE            everything else, including no citations at all.

NO NETWORK, NO DB. `reconcile()` takes the context dict and the scaffold
inventory as arguments so the whole rule set is testable offline. The
caller (scripts/reconcile_scaffold_evidence.py) does the I/O.
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import re
from typing import Any, Iterable, Optional

# Citation states
RESOLVES = "RESOLVES"
MALFORMED = "MALFORMED"
UNRESOLVED = "UNRESOLVED"
SOURCE_DOWN = "SOURCE_DOWN"

# Scaffold verdicts
V_ALL_MALFORMED = "CITATIONS_ALL_MALFORMED"
V_RESOLVE = "CITATIONS_RESOLVE"
V_INDETERMINATE = "INDETERMINATE"

_BRACKET_RE = re.compile(r"\[([^\]]*)\]")
_ASSERT_RE = re.compile(r"[=<>!~].*$")

_EVIDENCE_BLOCK_RE = re.compile(
    r"Evidence cited by the brain when proposing this:\n(.*?)\n\nTo unblock",
    re.S)
_EVIDENCE_LINE_RE = re.compile(r"^- `(.+)`$")
_WEEK_RE = re.compile(r"auto-drafted by Brain L6, week (\d{4}-\d{2}-\d{2})",
                      re.I)


def evidence_path(key: str) -> list:
    """Split one evidence key into path segments.

    `pages[/x]` and `pages./x` normalise together; a trailing assertion
    (`...verdict=broken`, `...rate_pct=33.3`) is dropped — it is a claim
    about the value, not part of the address.
    """
    if not isinstance(key, str):
        return []
    k = _BRACKET_RE.sub(r".\1", key.strip().lower())
    k = _ASSERT_RE.sub("", k)
    return [seg for seg in (s.strip().strip("'\"`,;:()") for s in k.split("."))
            if seg]


def resolve_citation(ctx: dict, key: str,
                     failed_sources: Optional[Iterable[str]] = None) -> dict:
    """Walk one evidence key through the live context.

    Returns {"key", "state", "path", "value_preview"}. `failed_sources`
    names roots whose fetch failed this run; a citation under one of those
    is SOURCE_DOWN, never MALFORMED or UNRESOLVED — a source we could not
    read is not evidence of anything.
    """
    path = evidence_path(key)
    out = {"key": key, "path": path, "state": UNRESOLVED,
           "value_preview": None}
    if not path:
        out["state"] = MALFORMED
        return out

    root = path[0]
    if root in set(failed_sources or ()):
        out["state"] = SOURCE_DOWN
        return out
    if not isinstance(ctx, dict) or root not in ctx:
        # Provable from the schema: there is no such source. This does not
        # depend on today's values, so it is equally true of the day the
        # citation was written.
        out["state"] = MALFORMED
        return out

    cur: Any = ctx
    for seg in path:
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(seg)]
                continue
            except (ValueError, IndexError):
                pass
        return out          # stays UNRESOLVED — indeterminate, not stale
    out["state"] = RESOLVES
    out["value_preview"] = _preview(cur)
    return out


def _preview(v: Any, limit: int = 120) -> str:
    s = repr(v)
    return s if len(s) <= limit else s[:limit - 1] + "…"


def parse_scaffold(path) -> dict:
    """Read one routes/_proposed_*.py and pull out what it claims.

    The scaffold embeds its own evidence block and draft week, so this
    works with no DB — which matters, because the pass must still run when
    the recommendations table is unreachable.
    """
    p = pathlib.Path(path)
    try:
        txt = p.read_text()
    except Exception as e:
        return {"file": str(p), "error": f"unreadable: {e}",
                "evidence_keys": [], "week_of": None}
    keys = []
    m = _EVIDENCE_BLOCK_RE.search(txt)
    if m:
        for line in m.group(1).splitlines():
            lm = _EVIDENCE_LINE_RE.match(line.strip())
            if lm:
                keys.append(lm.group(1))
    wm = _WEEK_RE.search(txt)
    return {"file": str(p), "slug": p.stem[len("_proposed_"):],
            "week_of": wm.group(1) if wm else None,
            "evidence_keys": keys}


def _age_days(week_of: Optional[str], today: Optional[_dt.date] = None):
    if not week_of:
        return None
    try:
        d = _dt.date.fromisoformat(week_of)
    except Exception:
        return None
    return ((today or _dt.date.today()) - d).days


def reconcile(ctx: dict, scaffolds: Iterable[dict],
              failed_sources: Optional[Iterable[str]] = None,
              today: Optional[_dt.date] = None) -> dict:
    """Reconcile every scaffold against the live context.

    Pure: no network, no DB, no filesystem. Everything it judges on is
    passed in, so every rule below is testable offline.
    """
    failed = set(failed_sources or ())
    rows = []
    for sc in scaffolds:
        cits = [resolve_citation(ctx, k, failed)
                for k in (sc.get("evidence_keys") or [])]
        counts = {RESOLVES: 0, MALFORMED: 0, UNRESOLVED: 0, SOURCE_DOWN: 0}
        for c in cits:
            counts[c["state"]] += 1

        if not cits:
            verdict, why = V_INDETERMINATE, "no evidence cited"
        elif counts[RESOLVES]:
            verdict, why = V_RESOLVE, (
                f"{counts[RESOLVES]}/{len(cits)} citation(s) still resolve")
        elif counts[MALFORMED] == len(cits):
            verdict, why = V_ALL_MALFORMED, (
                "every citation names a source that does not exist in the "
                "context schema — provenance is invented, not stale")
        else:
            verdict, why = V_INDETERMINATE, (
                "no citation resolves, but none is provably malformed "
                "either — needs a draft-time baseline to call")
        rows.append({
            "slug": sc.get("slug"), "file": sc.get("file"),
            "week_of": sc.get("week_of"),
            "age_days": _age_days(sc.get("week_of"), today),
            "verdict": verdict, "why": why,
            "counts": counts, "citations": cits,
        })

    rows.sort(key=lambda r: (r["verdict"] != V_ALL_MALFORMED,
                             -(r["age_days"] or 0)))
    summary = {v: sum(1 for r in rows if r["verdict"] == v)
               for v in (V_ALL_MALFORMED, V_RESOLVE, V_INDETERMINATE)}
    total_c = sum(sum(r["counts"].values()) for r in rows)
    return {
        "scaffolds": len(rows),
        "verdicts": summary,
        "citations_total": total_c,
        "citations": {k: sum(r["counts"][k] for r in rows)
                      for k in (RESOLVES, MALFORMED, UNRESOLVED, SOURCE_DOWN)},
        "sources_unavailable": sorted(failed),
        "rows": rows,
        "note": ("UNRESOLVED is INDETERMINATE, not stale: telling drift from "
                 "a citation that never resolved needs a draft-time value "
                 "snapshot, which historical recs do not carry."),
    }
