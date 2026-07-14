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
# :sess=<sid> is appended LAST by routes/stripe_direct_upgrade.py:_build_url when
# the agent paywall link carried an Mcp-Session-Id. The sid charset is sanitized to
# [A-Za-z0-9_.-] there, so this stops cleanly at any following ':'. findall+[-1]
# takes the trailing (real) sess even in the pathological case where a caller-
# supplied ?ref= smuggled its own ':sess=' earlier in the string.
_SESS_RE = re.compile(r":sess=([A-Za-z0-9_.-]+)")

# client_reference_id shapes that are NOT a bare MCP session id and must never be
# treated as one (each has its own dedicated handler upstream): structured agent
# ref without a sess, legacy/per-surface web refs, pair codes, top-ups, durable
# pack/sub key refs. A plain server.mjs session id (a UUID) matches none of these.
_RESERVED_CREF_PREFIXES = ("mcp:", "ref_", "web__", "dcm-", "tu-", "pk-", "k-")


def session_id_from_cref(client_reference_id: str | None) -> str | None:
    """Resolve the MCP session_id that a Stripe client_reference_id points at, so
    it can be matched against mcp_upgrade_signals.session_id.

    Two agent-driven shapes carry a session:
      • 'mcp:tool=<t>:ref=<r>:sess=<sid>'  (routes/stripe_direct_upgrade.py) → <sid>
      • '<sid>'  bare UUID (server.mjs _stripeWithSession / Fix E)          → as-is

    Returns None for web/organic refs (ref_…, web__…), pair codes (DCM-…),
    top-ups (tu-…), durable key refs (pk-/k-…), and an 'mcp:…' ref with no
    ':sess=' — none of which name a signal session. Never raises."""
    if not client_reference_id:
        return None
    s = str(client_reference_id).strip()
    if not s:
        return None
    m = _SESS_RE.findall(s)
    if m:
        return m[-1]
    if s.lower().startswith(_RESERVED_CREF_PREFIXES):
        return None
    return s


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


def _tool_from_session(cur, client_reference_id: str | None) -> str | None:
    """METERED path: the $10 owner_purchase_url binds client_reference_id to the
    mcp_session_id (Fix E), NOT tool=. Recover the driving tool from
    mcp_high_intent_sessions, which records (mcp_session_id, tool_name) at the
    moment the agent hit a paid tool's paywall (same session that saw the CTA).
    Read-only — no change to the session-binding plumbing."""
    sid = (client_reference_id or "").strip()
    if not sid or "tool=" in sid:   # subscription path is handled by parse_tool
        return None
    try:
        cur.execute(
            "SELECT tool_name FROM mcp_high_intent_sessions "
            "WHERE mcp_session_id = %s "
            "ORDER BY paid_call_count_24h DESC NULLS LAST LIMIT 1", (sid,))
        r = cur.fetchone()
        t = (r[0] if r else "") or ""
        return t.strip().lower() or None
    except Exception:
        return None


def record_conversion(client_reference_id: str | None, stripe_session_id: str | None,
                      source: str = "stripe_checkout") -> dict:
    """Best-effort: record a per-tool conversion. Idempotent on the Stripe
    session id. NEVER raises. Called from checkout.session.completed.
    Subscription path → tool from client_reference_id ('mcp:tool=…'); metered
    $10 path → tool from the session_id lookup (mcp_high_intent_sessions)."""
    tool = parse_tool(client_reference_id)
    src = source
    try:
        with _conn() as c:
            with c.cursor() as cur:
                _ensure_table(cur)
                if not tool:
                    tool = _tool_from_session(cur, client_reference_id)
                    if tool:
                        src = source + ":session_lookup"
                if not tool:
                    return {"ok": False, "skipped": "no_tool"}
                cur.execute(
                    """INSERT INTO conversion_attribution
                         (tool, source, client_reference_id, stripe_session_id)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (stripe_session_id) DO NOTHING""",
                    (tool, src, (client_reference_id or "")[:300], stripe_session_id))
            c.commit()
        return {"ok": True, "tool": tool, "source": src}
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
