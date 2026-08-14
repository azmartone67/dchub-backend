"""routes/brain_spec_debt.py — the SPEC-DEBT QUEUE (Phase 0, 2026-08-14).

THE GAP THIS SURFACES (measured 08-13 in #2633): 60 of 60 merged [brain-spec]
PRs carried an unchecked human checklist and not one was ever completed. Specs
auto-merge in minutes, the PR closes, and the obligation evaporates — while the
landed-spec fingerprint dedup (#2350) makes the condition unfilable forever.
The landed corpus in docs/brain-proposals/ IS the debt book; nothing rendered
it. This module renders it.

DERIVATION (the honest rules, mutation-tested in tests/test_spec_debt_queue.py):
  · a landed doc with at least one unchecked `- [ ]` item  → OPEN obligation
  · a doc whose checklist exists and is fully checked      → CLOSED
  · a doc with NO checklist at all                         → UNKNOWN — its own
    bucket with its own count, NEVER folded into closed. (The mpp_challenge
    lesson: a queue that reclassifies what it cannot read flatters itself.)
  · an empty / missing corpus                              → UNMEASURED, never
    "zero debt". A flattering zero is a bug.

EVERY NUMBER CARRIES ITS BASIS: which directory, which pattern, when scanned.
Ages come from the doc's own `_Filed <iso>` stamp (written by the spec filer);
a doc without a parseable date has age_days=null — UNMEASURED, not 0.

READ-ONLY. This module observes and surfaces; it opens, merges, closes and
modifies NOTHING (Phase 0: feedback before autonomy — zero new actuation).

Surface:  GET /api/v1/brain/spec-debt          (JSON; admin-gated)
          — mounted under /api/v1/brain/ to inherit CF bypass rule 6407517b;
            a NEW /api/v1/* prefix launches STALE (CF Rule #3).
Kill:     SPEC_DEBT_QUEUE_DISABLE=1
Consumers: the squasher portal verdict (routes/squasher_portal.py) reads
spec_debt_summary(); the PM brief may read the endpoint later — this module
deliberately does not wire itself into the PM brief (collision guard).
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

# ── classification (pure; the mutation-tested core) ──────────────────

_UNCHECKED_RE = re.compile(r"^\s*[-*]\s+\[ \]", re.M)
_CHECKED_RE = re.compile(r"^\s*[-*]\s+\[[xX]\]", re.M)
_FILED_RE = re.compile(r"_Filed\s+(\d{4}-\d{2}-\d{2}[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)")
_ISO_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_AGENDA_RE = re.compile(r"^(?:agenda|prop)-(\d+)", re.I)

OPEN, CLOSED, UNKNOWN = "open", "closed", "unknown"


def classify_doc_text(text: str) -> str:
    """OPEN / CLOSED / UNKNOWN for one landed doc.

    An unchecked `- [ ]` item is an open obligation regardless of anything
    else in the doc. A checklist that exists and is fully checked is closed.
    A doc with NO checklist is UNKNOWN — it is NOT closed: nobody ever wrote
    down the obligation, so nobody can claim it was met."""
    t = text or ""
    if _UNCHECKED_RE.search(t):
        return OPEN
    if _CHECKED_RE.search(t):
        return CLOSED
    return UNKNOWN


def parse_filed_at(text: str):
    """The doc's own filing timestamp (datetime, UTC) or None.

    Primary: the `_Filed <iso> · agenda #N_` stamp the spec filer writes.
    Fallback: the first ISO date anywhere in the doc. None → the caller
    reports age as UNMEASURED (null), never 0."""
    m = _FILED_RE.search(text or "")
    if m:
        raw = m.group(1).strip().replace(" ", "T").replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
    m = _ISO_DATE_RE.search(text or "")
    if m:
        try:
            return datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def doc_title(text: str, fallback: str) -> str:
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("# ").strip()[:200]
    return fallback


def agenda_id_for(filename: str):
    """The agenda/prop number from the doc filename, or None."""
    m = _AGENDA_RE.match(os.path.basename(filename or ""))
    return int(m.group(1)) if m else None


# ── the scan ─────────────────────────────────────────────────────────

def corpus_dir() -> str:
    override = os.environ.get("SPEC_DEBT_CORPUS_DIR")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "docs", "brain-proposals")


def scan_corpus(directory: str | None = None) -> dict:
    """Scan the landed-spec corpus and derive the debt book.

    Returns a dict whose `state` is MEASURED or UNMEASURED. An empty or
    missing corpus is UNMEASURED with a reason — it is NEVER "0 open debt":
    this scanner cannot tell 'no obligations' from 'cannot see the corpus'."""
    d = directory or corpus_dir()
    now = datetime.now(timezone.utc)
    basis = {
        "source_dir": d,
        "derivation": ("per-doc: any unchecked '- [ ]' => OPEN obligation; "
                       "checklist present and fully checked => CLOSED; "
                       "no checklist => UNKNOWN (its own bucket — never "
                       "counted as closed)"),
        "age_basis": ("the doc's own '_Filed <iso>' stamp (fallback: first "
                      "ISO date in the doc); no parseable date => "
                      "age_days=null, sorted last"),
        "scanned_at": now.isoformat(),
    }
    try:
        names = sorted(f for f in os.listdir(d) if f.endswith(".md"))
    except Exception as e:
        return {"state": "UNMEASURED",
                "reason": f"corpus unreadable: {type(e).__name__}: {str(e)[:120]}",
                "counts": None, "open_obligations": None, "basis": basis}
    if not names:
        return {"state": "UNMEASURED",
                "reason": "corpus directory exists but holds no .md docs — "
                          "refusing to report zero debt from an empty read",
                "counts": None, "open_obligations": None, "basis": basis}

    counts = {OPEN: 0, CLOSED: 0, UNKNOWN: 0}
    open_rows, unknown_rows = [], []
    unreadable = 0
    for name in names:
        try:
            with open(os.path.join(d, name), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception:
            unreadable += 1
            counts[UNKNOWN] += 1  # unreadable ≠ closed — it lands in UNKNOWN
            unknown_rows.append({"doc": name, "note": "unreadable"})
            continue
        state = classify_doc_text(text)
        counts[state] += 1
        if state == OPEN:
            filed = parse_filed_at(text)
            age = ((now - filed).total_seconds() / 86400.0) if filed else None
            open_rows.append({
                "doc": name,
                "agenda_id": agenda_id_for(name),
                "title": doc_title(text, name),
                "filed_at": filed.isoformat() if filed else None,
                "age_days": round(age, 1) if age is not None else None,
                "unchecked_items": len(_UNCHECKED_RE.findall(text)),
            })
        elif state == UNKNOWN:
            unknown_rows.append({"doc": name})

    # Oldest first; docs with no parseable date sort LAST (their age is
    # unmeasured, not infinite).
    open_rows.sort(key=lambda r: (r["age_days"] is None, -(r["age_days"] or 0.0)))

    basis["docs_scanned"] = len(names)
    basis["docs_unreadable"] = unreadable
    return {"state": "MEASURED", "counts": {**counts, "total_docs": len(names)},
            "open_obligations": open_rows, "unknown_docs": unknown_rows,
            "basis": basis}


def spec_debt_summary() -> dict:
    """Small never-raises summary for the squasher portal verdict."""
    try:
        s = scan_corpus()
        if s.get("state") != "MEASURED":
            return {"known": False, "state": s.get("state"),
                    "reason": s.get("reason")}
        c = s["counts"]
        rows = s.get("open_obligations") or []
        oldest = rows[0] if rows else None
        return {"known": True, "state": "MEASURED",
                "open": c[OPEN], "closed": c[CLOSED], "unknown": c[UNKNOWN],
                "total_docs": c["total_docs"],
                "oldest_age_days": (oldest or {}).get("age_days"),
                "oldest_doc": (oldest or {}).get("doc")}
    except Exception as e:  # noqa: BLE001
        return {"known": False, "state": "UNMEASURED",
                "reason": f"{type(e).__name__}: {str(e)[:120]}"}


# ── blueprint (lazy flask, admin-gated, no-store) ────────────────────
try:
    from flask import Blueprint, jsonify, request

    brain_spec_debt_bp = Blueprint("brain_spec_debt", __name__)

    def _admin_ok() -> bool:
        """Mirror of the squasher portal's gate — header, ?admin_key=, cookie."""
        keys = set()
        for n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
            v = os.environ.get(n)
            if v:
                keys.add(v)
        sent = (request.headers.get("X-Internal-Key")
                or request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or request.cookies.get("dchub_innov_key") or "").strip()
        return bool(sent) and sent in keys

    def _no_store(resp):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["CDN-Cache-Control"] = "no-store"
        return resp

    @brain_spec_debt_bp.get("/api/v1/brain/spec-debt")
    def spec_debt_endpoint():
        if os.environ.get("SPEC_DEBT_QUEUE_DISABLE", "0") == "1":
            return _no_store(jsonify(ok=False, error="disabled")), 503
        if not _admin_ok():
            return _no_store(jsonify(ok=False, error="admin key required")), 401
        try:
            limit = max(1, min(int(request.args.get("limit", "25")), 250))
        except Exception:
            limit = 25
        try:
            s = scan_corpus()
        except Exception as e:  # noqa: BLE001
            return _no_store(jsonify(ok=False, state="UNMEASURED",
                                     error=str(e)[:200])), 200
        rows = s.get("open_obligations")
        if isinstance(rows, list):
            s["open_obligations_total"] = len(rows)
            s["open_obligations"] = rows[:limit]
        return _no_store(jsonify(ok=True, **s))
except Exception:  # pragma: no cover — no-flask test env
    brain_spec_debt_bp = None
