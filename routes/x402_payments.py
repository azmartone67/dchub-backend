"""routes/x402_payments.py — x402 agent-autonomous payment rail (2026-06-20).

MONETIZE THE AGENT DIRECTLY at the value moment. x402 (HTTP 402 + USDC) lets a
wallet-funded AI agent PAY PER CALL for the flagship tools with NO human and NO
account — removing the human-pays bottleneck that caps conversion at ~6/30d
despite 185+166 distinct free agents hitting get_grid_intelligence /
get_fiber_intel each month. This rail is ADDITIVE to the existing human
$5-pack / Developer path; it ships DARK behind X402_ENABLED until the operator
provides a receiving wallet.

ENDPOINTS (blueprint x402_bp):
  GET  /api/v1/x402/quote?tool=<t>  -> the x402 payment requirements for a tool
       (the "402 challenge": price, payTo, network, asset, facilitator).
       READ-ONLY + safe + works WITHOUT a wallet — it advertises the price so
       x402-aware agents/facilitators discover that this tool is machine-payable.
       The value-moment surface in the MCP gateway (server.mjs) cites this.
  POST /api/v1/x402/verify  -> verify a submitted X-PAYMENT against the
       operator-configured facilitator and, on success, mint a short-lived
       unlock token the agent presents on its retry. Flag-gated, best-effort.

PRICES (per call, USDC): flagship $0.10 (get_grid_intelligence, get_fiber_intel),
deep $0.50 (analyze_site, compare_sites, site reports), standard $0.03. Tunable
via env X402_PRICE_OVERRIDES (JSON) so price is a one-field change.

SAFETY:
  · DARK by default — x402_enabled() reads X402_ENABLED (default OFF). With it
    off, /verify refuses and /quote still advertises the price (harmless).
  · NO wallet baked in — payTo comes from X402_RECIPIENT_ADDRESS (operator).
    Until a wallet is set, /quote marks configured=false and /verify returns
    not_configured. The rail CANNOT receive funds until the operator wires it.
  · READ-ONLY at quote; /verify only calls the facilitator + writes an unlock
    row. Best-effort — every external/DB touch is wrapped; never raises.
  · No money moves through THIS process — settlement is on-chain via the
    facilitator. We verify proof + unlock; we never custody funds.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

x402_bp = Blueprint("x402_payments", __name__)

# ── prices (USDC per call) ───────────────────────────────────────────
# Flagship value-moment tools = the demand pool (185+166 distinct free agents).
_PRICE_USDC = {
    "get_grid_intelligence":      0.10,
    "get_fiber_intel":            0.10,
    "analyze_site":               0.50,
    "compare_sites":              0.50,
    "get_site_capacity_report":   0.50,
    "get_developer_brief":        0.50,
    "site_selection_canvas":      0.50,
}
_DEFAULT_PRICE_USDC = 0.03  # standard identified tools (≈ market median)
_USDC_DECIMALS = 6          # USDC atomic units = dollars * 10^6

# USDC contract on Base mainnet (overridable for other networks/testnet).
_DEFAULT_USDC_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# Hosted x402 facilitator (verify+settle). Coinbase / x402.org run free ones.
_DEFAULT_FACILITATOR = "https://x402.org/facilitator"


# ── env / flags ──────────────────────────────────────────────────────
def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def x402_enabled() -> bool:
    """Ships DARK. Arm with X402_ENABLED=true ONLY after a wallet is set."""
    return _truthy(os.environ.get("X402_ENABLED"))


def _recipient() -> str:
    return (os.environ.get("X402_RECIPIENT_ADDRESS") or "").strip()


def _network() -> str:
    return (os.environ.get("X402_NETWORK") or "base").strip()


def _asset() -> str:
    return (os.environ.get("X402_USDC_ASSET") or _DEFAULT_USDC_ASSET).strip()


def _facilitator() -> str:
    return (os.environ.get("X402_FACILITATOR_URL") or _DEFAULT_FACILITATOR).strip().rstrip("/")


def _price_overrides() -> dict:
    raw = os.environ.get("X402_PRICE_OVERRIDES")
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return {str(k): float(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except Exception:
        return {}


def price_for(tool: str) -> float:
    """USDC price for a tool. Env override > built-in table > default."""
    ov = _price_overrides()
    if tool in ov:
        return ov[tool]
    return _PRICE_USDC.get(tool, _DEFAULT_PRICE_USDC)


def _atomic(usd: float) -> str:
    """Dollars -> USDC atomic-unit STRING (6 decimals), as x402 requires."""
    return str(int(round(float(usd) * (10 ** _USDC_DECIMALS))))


# ── the 402 challenge (x402 `accepts` block) ─────────────────────────
def payment_requirements(tool: str) -> dict:
    """Build the x402 payment-requirements object for a tool — the body an
    HTTP 402 (or the MCP value-moment surface) presents so a wallet-funded
    agent can pay. Pure; no network/DB; safe to call anywhere."""
    usd = price_for(tool)
    recipient = _recipient()
    return {
        "x402Version": 1,
        "configured": bool(recipient),   # false until the operator sets a wallet
        "accepts": [{
            "scheme": "exact",
            "network": _network(),
            "maxAmountRequired": _atomic(usd),
            "resource": f"https://dchub.cloud/mcp#{tool}",
            "description": f"Full {tool} result — DC Hub live infrastructure data (1 call)",
            "mimeType": "application/json",
            "payTo": recipient,
            "maxTimeoutSeconds": 60,
            "asset": _asset(),
            "extra": {"name": "USDC", "version": "2"},
        }],
        "price_usd": usd,
        "facilitator": _facilitator(),
    }


# ── GET /quote — advertise the price (read-only, always safe) ────────
@x402_bp.route("/api/v1/x402/quote", methods=["GET"])
def x402_quote():
    tool = (request.args.get("tool") or "").strip()
    if not tool:
        return jsonify(error="missing ?tool="), 400
    req = payment_requirements(tool)
    out = jsonify({
        "ok": True,
        "tool": tool,
        "enabled": x402_enabled(),
        "machine_payable": bool(req["configured"]) and x402_enabled(),
        "payment": req,
        "human_alternative": "https://dchub.cloud/upgrade",
        "note": ("Pay this amount in USDC to payTo (per x402), then retry the "
                 "tool with the X-PAYMENT proof header — full data, no human, "
                 "no account."),
    })
    out.headers["Cache-Control"] = "public, max-age=300"
    out.headers["Access-Control-Allow-Origin"] = "*"
    return out, 200


# ── POST /verify — verify an X-PAYMENT via the facilitator ───────────
def _facilitator_verify(payment_header: str, requirements: dict) -> tuple[bool, dict]:
    """Call the facilitator's /verify. Best-effort: returns (ok, detail).
    Never raises. Returns (False, {...}) when not configured or on any error."""
    fac = _facilitator()
    if not fac or not _recipient():
        return False, {"reason": "not_configured"}
    try:
        body = json.dumps({
            "x402Version": 1,
            "paymentPayload": payment_header,
            "paymentRequirements": requirements.get("accepts", [{}])[0],
        }).encode()
        r = urllib.request.Request(
            fac + "/verify", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "dchub-x402/1.0"})
        with urllib.request.urlopen(r, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
        return bool(data.get("isValid") or data.get("valid")), data
    except Exception as e:
        logger.warning("x402 facilitator verify failed: %s", e)
        return False, {"reason": "facilitator_error", "detail": str(e)[:120]}


def _facilitator_settle(payment_header: str, requirements: dict) -> tuple[bool, dict]:
    """Call the facilitator's /settle to CAPTURE the funds on-chain. x402 is
    verify -> serve -> settle; we settle BEFORE unlocking so we never hand over
    full data without the USDC actually being captured. Best-effort; (ok, detail).
    Never raises."""
    fac = _facilitator()
    if not fac or not _recipient():
        return False, {"reason": "not_configured"}
    try:
        body = json.dumps({
            "x402Version": 1,
            "paymentPayload": payment_header,
            "paymentRequirements": requirements.get("accepts", [{}])[0],
        }).encode()
        r = urllib.request.Request(
            fac + "/settle", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "dchub-x402/1.0"})
        with urllib.request.urlopen(r, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
        return bool(data.get("success") or data.get("settled")), data
    except Exception as e:
        logger.warning("x402 facilitator settle failed: %s", e)
        return False, {"reason": "settle_error", "detail": str(e)[:120]}


@x402_bp.route("/api/v1/x402/verify", methods=["POST", "OPTIONS"])
def x402_verify():
    if request.method == "OPTIONS":
        return ("", 204)
    if not x402_enabled():
        return jsonify(ok=False, error="x402_disabled",
                       hint="rail ships dark; operator sets X402_ENABLED + wallet"), 503
    if not _recipient():
        return jsonify(ok=False, error="not_configured",
                       hint="X402_RECIPIENT_ADDRESS (receiving wallet) not set"), 503
    _body = request.get_json(silent=True) or {}
    payment = request.headers.get("X-PAYMENT") or _body.get("payment")
    tool = (request.args.get("tool") or _body.get("tool") or "").strip()
    if not payment or not tool:
        return jsonify(ok=False, error="missing X-PAYMENT header or tool"), 400
    reqs = payment_requirements(tool)
    # 1) VERIFY the payment authorization is valid.
    ok, detail = _facilitator_verify(payment, reqs)
    if not ok:
        return jsonify(ok=False, error="payment_unverified", detail=detail), 402
    # 2) SETTLE — actually capture the USDC on-chain BEFORE we unlock. If
    #    settlement fails we do NOT hand over data (no free data on an
    #    uncaptured payment).
    settled, settle_detail = _facilitator_settle(payment, reqs)
    if not settled:
        return jsonify(ok=False, error="settlement_failed", detail=settle_detail), 402
    # 3) Settled — mint a short-lived unlock token the agent presents on retry.
    token = "x402_" + str(int(time.time())) + "_" + tool
    _record_unlock(tool, token, reqs.get("price_usd"))
    return jsonify(ok=True, tool=tool, unlock_token=token, expires_in_s=300,
                   settlement=settle_detail.get("transaction") or settle_detail.get("network") or "settled",
                   note="Retry the tool with header X-Unlock: <unlock_token> for full data."), 200


def _record_unlock(tool: str, token: str, price_usd) -> None:
    """Best-effort ledger of a settled agent-pay-per-call (drives the meter +
    value-moment conversion reporting). Never raises."""
    try:
        import psycopg2
        dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not dsn:
            return
        conn = psycopg2.connect(dsn, sslmode="require", connect_timeout=6)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS x402_unlocks (
                        id BIGSERIAL PRIMARY KEY,
                        tool TEXT, token TEXT, price_usd DOUBLE PRECISION,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())
                """)
                cur.execute(
                    "INSERT INTO x402_unlocks (tool, token, price_usd) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (tool, token, price_usd))
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("x402 unlock ledger write skipped: %s", e)


def register_x402_payments(app) -> None:
    """Idempotent registration for main.py. Never raises."""
    try:
        app.register_blueprint(x402_bp)
    except Exception as e:
        logger.warning("x402_payments: blueprint registration failed: %s", e)
