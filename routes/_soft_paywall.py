"""Phase WW-2 (2026-05-17) — reusable soft-paywall helper.

Applies the Phase WW preview-and-CTA pattern to any REST endpoint that
returns a list. Anon/FREE callers see N records + a structured upgrade
prompt; IDENTIFIED+ see the full set.

Why a helper instead of inline copy-paste:
  - Round 4 found 8+ leaking endpoints
  - Each needed the same 30-line snippet
  - Helper makes the pattern one line, makes inconsistency impossible

Usage:
    from routes._soft_paywall import maybe_paywall
    @app.route("/api/v1/big-thing")
    def big_thing():
        results = fetch_all_things()
        payload = {"things": results, "count": len(results)}
        return maybe_paywall(payload, list_key="things",
                             preview_cap=10, teaser="all 285 things")

Design notes:
  - explicit ?limit=N param on the upstream call BYPASSES the gate.
    Rationale: explicit slice = legit dashboard tile, no need to paywall.
  - Default tier needed is IDENTIFIED (matches all MCP IDENTIFIED tools).
  - resolve_tier import is lazy + try/except so a tier_gate import bug
    can never break the endpoint — it just returns the unfiltered data.
"""
from __future__ import annotations
from typing import Any
from flask import jsonify, request


_DEFAULT_PREVIEW_CAP = 10


def maybe_paywall(payload: dict,
                  list_key: str,
                  preview_cap: int = _DEFAULT_PREVIEW_CAP,
                  teaser: str = "the full dataset",
                  required_tier_name: str = "IDENTIFIED"):
    """Apply soft-paywall to the list at payload[list_key]. Returns a
    Flask jsonify response, or the original payload if the gate doesn't
    fire (caller is IDENTIFIED+ OR ?limit= was passed OR import fails).

    Always returns the data — never blocks. The point is to incentivize
    upgrade by showing what's missing, not to wall off discovery.
    """
    # Caller passed ?limit=N → respect that, no gate
    if request.args.get("limit"):
        return jsonify(payload), 200

    rows = payload.get(list_key, [])
    if not isinstance(rows, list):
        return jsonify(payload), 200
    total = len(rows)
    if total <= preview_cap:
        return jsonify(payload), 200

    # Try to resolve tier; if anything goes wrong, return unfiltered.
    try:
        from util.tier_gate import resolve_tier, Tier as _T
        tier, _ = resolve_tier()
        # Map required_tier_name → enum value
        needed = getattr(_T, required_tier_name, _T.IDENTIFIED)
        if tier >= needed:
            return jsonify(payload), 200
    except Exception:
        return jsonify(payload), 200

    # Soft-paywall: truncate the list, inject CTA
    payload[list_key] = rows[:preview_cap]
    payload["_gated"] = True
    payload["_preview_only"] = True
    payload["_total_available"] = total
    payload["_hidden_count"] = total - preview_cap
    payload["_required_tier"] = required_tier_name
    payload["_upgrade_cta"] = (
        f"Showing top {preview_cap} of {total}. "
        f"Get {teaser} free — claim a key in 30s at "
        f"POST /api/v1/keys/claim, then pass X-API-Key header. "
        f"(Auto-trial mints inline on 402 responses too.)"
    )
    payload["_signup_url"] = "https://dchub.cloud/signup"
    payload["_pricing_url"] = "https://dchub.cloud/pricing"
    # Update the visible count to match what's actually returned
    if "count" in payload:
        payload["count"] = preview_cap
    return jsonify(payload), 200
