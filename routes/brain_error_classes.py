"""
brain_error_classes.py — seed registry of recurring error CLASSES that the
brain can self-fix.

Today the brain's Layer-4 text loop only fixes HTML placeholder
substitutions; Layer-5 (brain_v2_layer5.py) proposes free-form code
substitutions but does NOT auto-apply. Both are caller-side. Neither
has a *recognizer* for the recurring backend error patterns the healer
already surfaces (NoneType.fetchall, ON CONFLICT mismatch, legacy keys,
brain-radar 401 noise, conn-held forced-reclaim, …).

This module is that recognizer. Each entry is a known error class with
the regex that identifies it in Railway logs, the templated remediation
recipe, and the commit ID of the proven hand-fix that anchors the
confidence. Layer-5 (or a future Layer-6 auto-PR opener) is meant to
consume this registry: for any log line matching a class, propose the
templated fix against the file the regex captured.

Seed list (2026-05-23): the five classes we hit this session, each with
a shipped fix on main as the confidence anchor.

Phase ZZZZZ — brain takeover scaffold #1.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ErrorClass:
    id: str
    pattern: str          # regex applied to log lines / heal findings
    fix_template: str     # template id consumed by Layer-5 codegen
    description: str
    confidence: float     # 0.0..1.0
    shipped_proof: Optional[str] = None  # commit SHA where this class was first fixed by hand
    notes: str = ""


# Seed registry. Add a row when a recurring class earns at least one
# shipped hand-fix on main — the SHA is the confidence anchor.
REGISTRY: list[ErrorClass] = [
    ErrorClass(
        id="nonetype_fetchall",
        pattern=r"'NoneType'\s+object\s+has\s+no\s+attribute\s+'fetchall'",
        fix_template="split_chained_execute",
        description=(
            "psycopg2 cursor.execute(sql).fetchall() — execute() returns None "
            "in psycopg2 (unlike sqlite3). Split into c.execute(sql) then c.fetchall()."
        ),
        confidence=0.95,
        shipped_proof="e2e1999d",
        notes="Same shape applies to .fetchone() and .rowcount chained on execute.",
    ),
    ErrorClass(
        id="on_conflict_target_mismatch",
        pattern=r"(?i)on\s+conflict.*does\s+not\s+match|partial[- ]index\s+ON\s+CONFLICT",
        fix_template="include_where_predicate_in_on_conflict",
        description=(
            "Postgres partial-UNIQUE-index ON CONFLICT requires the conflict "
            "clause to include the same WHERE predicate as the index. Without "
            "it the upsert raises and watchdog loops the container."
        ),
        confidence=0.85,
        shipped_proof="fbcf723d",
        notes="Killed the 2026-05-21 gas_pipelines spam → watchdog → container loop.",
    ),
    ErrorClass(
        id="legacy_hardcoded_key_accepted",
        pattern=r"legacy\s+hardcoded\s+key\s+accepted.*migrate\s+caller",
        fix_template="migrate_internal_key_header",
        description=(
            "Caller still authenticates with the legacy hardcoded internal key. "
            "Migrate it to read DCHUB_INTERNAL_KEY from env + send X-Internal-Key "
            "header, and remove the legacy fallback once all callers migrate."
        ),
        confidence=0.7,
        notes="Migration is cross-file (caller + receiver); confidence stays sub-0.8.",
    ),
    ErrorClass(
        id="brain_radar_401_noise",
        pattern=r"\[brain-radar\]\s+\S+\s+HTTP\s+40[13]",
        fix_template="demote_expected_gate_log",
        description=(
            "401/403 on the brain-radar's anonymous probe of a gated endpoint "
            "is the radar's intended SIGNAL, not an error. Return None silently "
            "instead of printing WARN to stderr."
        ),
        confidence=0.95,
        shipped_proof="912b3fd3",
    ),
    ErrorClass(
        id="conn_held_forced_reclaim",
        pattern=r"FORCED\s+RECLAIM:\s+Connection\s+\d+\s+held\s+\d+s",
        fix_template="wrap_caller_try_finally_close",
        description=(
            "DB connection checked out and never released until the watchdog's "
            "forced-reclaim. Wrap the caller's body in try/finally so conn.close() "
            "runs on every exit path (success, return, exception)."
        ),
        confidence=0.85,
        shipped_proof="e2e1999d",
        notes="Stack-trace line in the log names the leaking caller — feed it to the template.",
    ),
    # Phase ZZZZZ-round5 (2026-05-23): 3 new patterns picked up from the
    # 2026-05-23 Railway logs the user shared. Each is a recurring class
    # that already costs cycles in production; getting them registered
    # lets Layer-5 propose templated PRs the next time they fire.
    ErrorClass(
        id="psycopg2_transaction_aborted",
        pattern=r"current\s+transaction\s+is\s+aborted,\s+commands\s+ignored",
        fix_template="rollback_then_retry_or_savepoint",
        description=(
            "psycopg2: one query inside a transaction raised, so every subsequent "
            "query in the same transaction returns 'current transaction is aborted'. "
            "Fix: wrap risky upserts in a SAVEPOINT and ROLLBACK TO that savepoint "
            "on error, OR call conn.rollback() in the except block before continuing. "
            "Don't keep issuing queries on a poisoned connection."
        ),
        confidence=0.9,
        notes="Observed in 2026-05-23 logs: fiber_routes upsert loop logged 20 'transaction aborted' warnings in succession, then '0 carrier routes written'. The seed function isn't catching the first ON CONFLICT error per row.",
    ),
    ErrorClass(
        id="external_api_404_silent",
        pattern=r"(PeeringDB|external\s+API)\s+returned\s+4(04|05)",
        fix_template="record_external_404_with_backoff",
        description=(
            "External API returned 404 (or 405) — the endpoint shape changed or "
            "the resource was removed. Fix: don't keep retrying every cycle. Log "
            "once, mark the source DEGRADED in source_health, and pause it from "
            "the discovery rotation until a human revisits."
        ),
        confidence=0.8,
        notes="2026-05-23: PeeringDB 404 fires every fiber discovery cycle (~5min). Wasted ~290 outbound calls/day.",
    ),
    ErrorClass(
        id="slow_request_threshold_breach",
        pattern=r"SLOW\s+REQUEST:\s+\S+\s+\S+\s+took\s+\d+\.\d+s\s+\(>\d+s\)",
        fix_template="move_to_background_task_or_paginate",
        description=(
            "A request exceeded the slow-threshold (typically 30s gunicorn timeout). "
            "Fix: (a) move the work to a background task and return 202 + a poll URL "
            "from the request, OR (b) paginate the underlying query so the request "
            "returns the first page in <2s. Don't leave it as a 60s+ blocking call."
        ),
        confidence=0.85,
        notes="2026-05-23: /api/v1/energy/eia-ingest/run took 69.9s — pure ingest, should be a cron not a request.",
    ),
]


def match(line: str) -> Optional[ErrorClass]:
    """Return the FIRST registered class whose pattern matches `line`, or None.

    Intentionally first-match (not best-match) — the registry is curated and
    ordered by specificity. The Layer-5 caller is expected to apply the
    matching class's fix_template against the file/line the log line implies.
    """
    if not line:
        return None
    for cls in REGISTRY:
        try:
            if re.search(cls.pattern, line, flags=re.IGNORECASE):
                return cls
        except re.error:
            continue
    return None


def summary() -> dict:
    """Compact snapshot of the registry — surfaced on /api/v1/brain/error-classes
    so the dashboard can show 'Brain knows N error classes; M have shipped proofs.'"""
    proofs = sum(1 for c in REGISTRY if c.shipped_proof)
    return {
        "total_classes": len(REGISTRY),
        "with_shipped_proof": proofs,
        "avg_confidence": round(sum(c.confidence for c in REGISTRY) / max(len(REGISTRY), 1), 2),
        "classes": [
            {
                "id": c.id,
                "description": c.description,
                "confidence": c.confidence,
                "shipped_proof": c.shipped_proof,
            }
            for c in REGISTRY
        ],
    }
