"""utils/claim_token.py — Shared HMAC claim-token signer/verifier (2026-06-07).

Round-1 (commit a297a7f9) shipped the MCP HIGH-INTENT one-click claim flow with
its own private HMAC sign/verify in routes/mcp_high_intent_claim.py. Round-2
adds a second kind of claim (state-of-2026 WEB visitor) that needs the SAME
guarantees (tamper-proof, 24h TTL, single-use) but the SAME secret + sign path,
so the existing /claim/<token> handler can recognize either kind.

This module is the canonical signer. mcp_high_intent_claim imports sign/verify
from here; state_visitor_claim does too. Both kinds embed a `kind` discriminator
in the payload so the unified /claim/<token> handler can route to the right
post-mint behavior (different copy, different attribution table).

Token format (URL-safe):
    <base64url(payload)>.<sig_hex>

Payload (pipe-delimited; backward-compatible with the 3-field MCP form):
    sid|tool|ts                — kind=mcp_session     (legacy, kind inferred)
    kind:state_of_2026_visitor|sid|extra|ts       — kind=state_of_2026_visitor

The legacy 3-field form is preserved bit-for-bit so a token minted by the
Round-1 mcp_high_intent_claim still verifies after this refactor. New tokens
use the 4-field "kind:..." prefix to disambiguate.

Signing:
    HMAC-SHA256(secret, payload_raw_bytes).hexdigest()[:32]
    secret = DCHUB_HMAC_SECRET env var, fallback to DCHUB_ADMIN_KEY[:32].

TTL: 24h on all kinds.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time


logger = logging.getLogger(__name__)


CLAIM_TOKEN_TTL_S = 24 * 3600     # 24h


# ── kinds ────────────────────────────────────────────────────────────

KIND_MCP_SESSION             = "mcp_session"             # Round 1: MCP paywall
KIND_STATE_OF_2026_VISITOR   = "state_of_2026_visitor"   # Round 2: web reader


# ── secret ────────────────────────────────────────────────────────────

def hmac_secret() -> bytes:
    """The signing secret. DCHUB_HMAC_SECRET if set; else first 32 chars of
    DCHUB_ADMIN_KEY; last-resort dev fallback so a missing env doesn't 500."""
    s = (os.environ.get("DCHUB_HMAC_SECRET") or "").strip()
    if not s:
        s = (os.environ.get("DCHUB_ADMIN_KEY") or "")[:32]
    if not s:
        s = "dchub-dev-secret-set-DCHUB_HMAC_SECRET"
        logger.warning("[claim_token] DCHUB_HMAC_SECRET unset — using dev fallback")
    return s.encode("utf-8")


# ── b64url helpers ──────────────────────────────────────────────────

def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = b"=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s.encode("ascii") + pad)


# ── sign ─────────────────────────────────────────────────────────────

def sign_claim_token(
    *,
    kind: str = KIND_MCP_SESSION,
    session_id: str = "",
    extra: str = "",
    ts: int | None = None,
) -> str:
    """Mint a tamper-proof, time-stamped claim token.

    For backward-compat with Round-1 mcp tokens: when kind=mcp_session, the
    payload is the legacy 3-field "sid|extra|ts" (extra = tool_name). When
    kind != mcp_session, the payload is "kind:<kind>|sid|extra|ts".
    """
    if ts is None:
        ts = int(time.time())
    sid = (session_id or "")[:100]
    ext = (extra or "")[:64]
    if kind == KIND_MCP_SESSION:
        payload_raw = f"{sid}|{ext}|{ts}".encode("utf-8")
    else:
        kind_clean = "".join(ch for ch in kind if ch.isalnum() or ch in "_-")[:48]
        payload_raw = f"kind:{kind_clean}|{sid}|{ext}|{ts}".encode("utf-8")
    sig = hmac.new(hmac_secret(), payload_raw, hashlib.sha256).hexdigest()[:32]
    return f"{_b64u(payload_raw)}.{sig}"


# ── verify ───────────────────────────────────────────────────────────

def verify_claim_token(token: str) -> dict | None:
    """Returns {kind, session_id, extra, ts} on success, None on any failure.

    Also rejects tokens older than CLAIM_TOKEN_TTL_S. Backward-compat:
    legacy 3-field tokens get kind=mcp_session implicitly.
    """
    if not token or "." not in token:
        return None
    try:
        payload_b64, sig_hex = token.rsplit(".", 1)
        payload_raw = _b64u_decode(payload_b64)
        expected = hmac.new(hmac_secret(), payload_raw,
                            hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig_hex, expected):
            return None
        decoded = payload_raw.decode("utf-8")

        # Sniff "kind:XXX|..." 4-field form vs legacy 3-field.
        if decoded.startswith("kind:"):
            head, _, rest = decoded.partition("|")
            kind = head[len("kind:"):]
            parts = rest.split("|")
            if len(parts) != 3:
                return None
            sid, extra, ts_s = parts
        else:
            parts = decoded.split("|")
            if len(parts) != 3:
                return None
            kind = KIND_MCP_SESSION
            sid, extra, ts_s = parts

        ts = int(ts_s)
        if (int(time.time()) - ts) > CLAIM_TOKEN_TTL_S:
            return None
        return {"kind": kind, "session_id": sid, "extra": extra, "ts": ts}
    except Exception:
        return None
