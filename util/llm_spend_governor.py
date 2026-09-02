"""util/llm_spend_governor.py — shed customer-facing LLM calls before the brain starves.

★ WHY (2026-09-01). Every model call in the tree — the reasoning layers AND
the customer-facing narrative/demo generators — shares ONE Cloudflare AI
Gateway spend rule ($100 / 7d). On 09-01 the rule tripped and 429-blocked
everything for ~2-6h: L16 wrote `outcomes: {}`, corroboration read
"unavailable", the L7 step 503'd. Measured on the 7d ledger that day:
report_narrative 810 calls, demo 382, market_deep_dive 323 — together ~60% of
spend — while the reasoning layers (L14 28, L16 22, L18 28) were under 5%.
#3528 pages the owner when the gateway trips; nothing sheds load before it
does, so a blackout is total instead of partial.

WHAT. A soft cap. When the 7d spend crosses SHED_AT (80%) of it, callers in
the shed list are refused BEFORE the request is made and get a structured
`shed` response (status 429, `.json()["type"] == "shed"`) — the same shape
the gateway's own refusal arrives in, so the 20+ call sites' error handling
is untouched. The brain layers are never in the list.

DARK BY DEFAULT: the cap is read from LLM_SPEND_SOFT_CAP_USD and is OFF
when unset. The ledger (routes/brain_llm_spend.py) counts TOKENS and keeps no
price table on purpose — a stale one turns a measurement into a guess — so
the operator supplies the blended rate too (LLM_SPEND_USD_PER_MTOK, dollars
per million tokens, in + out). Both unset -> the governor reports itself
inactive and sheds nothing. GET /api/v1/brain/model-probe shows the state.

Kill switch: LLM_SPEND_GOVERNOR_DISABLE=1.
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

CAP_ENV = "LLM_SPEND_SOFT_CAP_USD"
RATE_ENV = "LLM_SPEND_USD_PER_MTOK"
SHED_LIST_ENV = "LLM_SPEND_SHED_CONSUMERS"
DISABLE_ENV = "LLM_SPEND_GOVERNOR_DISABLE"
SHED_AT = 0.80
WINDOW_DAYS = 7
# The customer-facing, cacheable consumers — layer labels are the calling
# module's own name (brain_llm_spend.instrumented_post). Never a brain layer.
DEFAULT_SHED = ("demo", "report_narrative", "market_deep_dive")
SHED_TYPE = "shed"

_CACHE = {"at": 0.0, "tokens": None}
_CACHE_TTL_S = 60.0


def _float_env(name: str):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if v > 0 else None


def config() -> dict:
    """Pure read of the environment. `active` is True only when BOTH numbers
    are present and the kill switch is off."""
    disabled = (os.environ.get(DISABLE_ENV) or "").strip() == "1"
    cap = _float_env(CAP_ENV)
    rate = _float_env(RATE_ENV)
    raw_list = (os.environ.get(SHED_LIST_ENV) or "").strip()
    shed = tuple(x.strip() for x in raw_list.split(",") if x.strip()) \
        if raw_list else DEFAULT_SHED
    return {"active": bool(cap and rate) and not disabled,
            "disabled": disabled, "cap_usd": cap, "usd_per_mtok": rate,
            "shed_at": SHED_AT, "window_days": WINDOW_DAYS,
            "shed_consumers": list(shed),
            "why_inactive": (None if (cap and rate) and not disabled else
                             ("kill switch" if disabled else
                              "%s and %s must both be set" % (CAP_ENV, RATE_ENV)))}


def decide(layer: str, spent_usd, cfg: dict | None = None) -> dict:
    """Pure. -> {shed: bool, reason, ratio, spent_usd, cap_usd, layer}.
    `spent_usd is None` means UNMEASURED: the ledger could not be read, and an
    unmeasured spend is never treated as over the cap."""
    cfg = cfg or config()
    out = {"shed": False, "layer": layer, "active": cfg["active"],
           "spent_usd": spent_usd, "cap_usd": cfg["cap_usd"], "ratio": None,
           "reason": None}
    if not cfg["active"]:
        out["reason"] = "governor inactive: %s" % cfg["why_inactive"]
        return out
    if spent_usd is None:
        out["reason"] = "spend unmeasured — never shed on a guess"
        return out
    ratio = float(spent_usd) / float(cfg["cap_usd"])
    out["ratio"] = round(ratio, 4)
    if layer not in cfg["shed_consumers"]:
        out["reason"] = "consumer not in the shed list"
        return out
    if ratio < cfg["shed_at"]:
        out["reason"] = "under %.0f%% of the soft cap" % (cfg["shed_at"] * 100)
        return out
    out["shed"] = True
    out["reason"] = ("7d spend %.2f of %.2f (%.0f%% >= %.0f%%) — shedding %s "
                     "so the brain's reasoning layers keep headroom"
                     % (spent_usd, cfg["cap_usd"], ratio * 100,
                        cfg["shed_at"] * 100, layer))
    return out


def tokens_7d(conn_fn=None) -> int | None:
    """Total tokens (in + out) in the ledger's window, cached 60s per process.
    None = unmeasured."""
    now = time.time()
    if _CACHE["tokens"] is not None and now - _CACHE["at"] < _CACHE_TTL_S:
        return _CACHE["tokens"]
    try:
        if conn_fn is None:
            from routes.brain_llm_spend import _conn as conn_fn
        c = conn_fn()
        if c is None:
            return None
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) "
                    "  FROM brain_llm_spend "
                    " WHERE created_at >= NOW() - make_interval(days => %s)",
                    (WINDOW_DAYS,))
                row = cur.fetchone()
                tokens = int((row or [0])[0] or 0)
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("[llm_spend_governor] ledger read failed: %s", str(e)[:120])
        return None
    _CACHE.update(at=now, tokens=tokens)
    return tokens


def spent_usd(cfg: dict | None = None, conn_fn=None):
    cfg = cfg or config()
    if not cfg["active"]:
        return None
    tokens = tokens_7d(conn_fn)
    if tokens is None:
        return None
    return tokens / 1_000_000.0 * float(cfg["usd_per_mtok"])


class ShedResponse:
    """A response-shaped refusal: status 429 and the same `.json()` contract
    the gateway's refusal has, so every caller's existing 429 handling applies.
    NOT a requests.Response — never sent anywhere."""
    status_code = 429
    ok = False

    def __init__(self, decision: dict):
        self.decision = decision
        self.headers = {"X-DCHub-Spend-Governor": SHED_TYPE}
        self._body = {"type": SHED_TYPE,
                      "error": {"type": "spend_governor_shed",
                                "message": decision.get("reason") or "shed"},
                      "governor": decision}
        self.text = str(self._body)

    def json(self):
        return dict(self._body)

    @property
    def content(self):
        return self.text.encode("utf-8")


def shed_response(layer: str, cfg: dict | None = None, conn_fn=None):
    """The one call brain_llm_spend.instrumented_post makes. None = proceed.
    Cheap when inactive: no ledger read at all."""
    cfg = cfg or config()
    if not cfg["active"] or layer not in cfg["shed_consumers"]:
        return None
    d = decide(layer, spent_usd(cfg, conn_fn), cfg)
    return ShedResponse(d) if d["shed"] else None


def status(conn_fn=None) -> dict:
    """For /api/v1/brain/model-probe: config + the live ratio (when active)."""
    cfg = config()
    out = dict(cfg)
    out["spent_usd_7d"] = spent_usd(cfg, conn_fn) if cfg["active"] else None
    out["ratio"] = (round(out["spent_usd_7d"] / cfg["cap_usd"], 4)
                    if out["spent_usd_7d"] is not None and cfg["cap_usd"] else None)
    out["shedding_now"] = bool(out["ratio"] is not None and out["ratio"] >= SHED_AT)
    out["note"] = ("dark until %s and %s are set; sheds only the listed "
                   "customer-facing consumers, never a brain layer"
                   % (CAP_ENV, RATE_ENV))
    return out
