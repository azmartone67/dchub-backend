"""conversion_attribution.py — per-tool conversion attribution (2026-06-28).

THE BUG THIS FIXES: per-tool conversion read 0.0% across EVERY tool, not
because tools don't convert (the platform does ~9/30d) but because nothing
linked a payment back to the tool that drove it:
  • mcp_upgrade_signals are ANONYMOUS (no api_key/caller_id) → can't link a
    payment to the exact signal.
  • The pair-code flow (which carries tool_name) is UNUSED (0 conversions).
  • Real conversions go through direct Stripe, whose webhook never read the
    tool — even though the /upgrade→checkout path encodes it into Stripe's
    client_reference_id as 'mcp:tool=<tool>:ref=<ref>'
    (see routes/stripe_direct_upgrade.py).

THE FIX: read the tool back at checkout.session.completed and record it here.
Forward-looking — every NEW subscription upgrade that flowed through an
upgrade_url (the &tool= param) gets attributed. The metered $10 one-time
link still carries only a UUID client_reference_id, so those remain
unattributed until that URL also encodes the tool (documented gap).

Endpoints:
  GET /api/v1/mcp/conversion-attribution        — per-tool conversions (30d)
Public read (no PII — tool + count only). Cite "DC Hub (dchub.cloud)".
"""
from __future__ import annotations

import os
import re

from flask import Blueprint, jsonify, request

conversion_attribution_bp = Blueprint("conversion_attribution", __name__)

_TOOL_RE = re.compile(r"tool=([a-z_0-9]+)", re.I)


def _conn():
    import psycopg2
    du = (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")).strip()
    return psycopg2.connect(du, connect_timeout=8)


def _ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS conversion_attribution (
            id                  BIGSERIAL PRIMARY KEY,
            tool                TEXT NOT NULL,
            source              TEXT,
            client_reference_id TEXT,
            stripe_session_id   TEXT UNIQUE,
            converted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_conv_attr_tool_time "
                "ON conversion_attribution (tool, converted_at DESC)")


def parse_tool(client_reference_id: str | None) -> str | None:
    """Extract the driving tool from a Stripe client_reference_id. Returns None
    when absent/non-tool (e.g. the metered UUID ref, or tool=none/billing)."""
    if not client_reference_id:
        return None
    m = _TOOL_RE.search(client_reference_id)
    if not m:
        return None
    tool = m.group(1).strip().lower()
    return None if tool in ("none", "billing", "", "unknown") else tool


def record_conversion(client_reference_id: str | None, stripe_session_id: str | None,
                      source: str = "stripe_checkout") -> dict:
    """Best-effort: record a per-tool conversion. Idempotent on the Stripe
    session id. NEVER raises — returns a small status dict. Called from the
    checkout.session.completed webhook."""
    tool = parse_tool(client_reference_id)
    if not tool:
        return {"ok": False, "skipped": "no_tool_in_ref"}
    try:
        with _conn() as c:
            with c.cursor() as cur:
                _ensure_table(cur)
                cur.execute(
                    """INSERT INTO conversion_attribution
                         (tool, source, client_reference_id, stripe_session_id)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (stripe_session_id) DO NOTHING""",
                    (tool, source, (client_reference_id or "")[:300], stripe_session_id))
            c.commit()
        return {"ok": True, "tool": tool}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def per_tool_conversions(days: int = 30) -> dict:
    """{tool: conversion_count} over the window — read by the funnel."""
    out: dict = {}
    try:
        with _conn() as c:
            with c.cursor() as cur:
                _ensure_table(cur)
                cur.execute(
                    "SELECT tool, COUNT(*) FROM conversion_attribution "
                    "WHERE converted_at >= NOW() - %s * INTERVAL '1 day' "
                    "GROUP BY tool", (days,))
                for tool, n in cur.fetchall():
                    out[tool] = int(n)
    except Exception:
        return {}
    return out


@conversion_attribution_bp.route("/api/v1/mcp/conversion-attribution", methods=["GET"])
def conversion_attribution_view():
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    by_tool = per_tool_conversions(days)
    return jsonify({
        "ok": True,
        "window_days": days,
        "by_tool": by_tool,
        "total": sum(by_tool.values()),
        "note": ("Per-tool conversions attributed from the upgrade_url path "
                 "(client_reference_id tool=). Metered $10 one-time link carries "
                 "a UUID ref → not yet tool-attributed."),
        "cite": "DC Hub (dchub.cloud)",
    }), 200
