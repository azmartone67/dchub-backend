"""Key-mint scan guard (r-mint-scan, 2026-09-24).

One module, three jobs, because they share one definition of "scan traffic":

  1. KPI exclusion   — scored_trial_keys_cte() classifies auto_trial_keys rows
                       at QUERY time (no row is deleted or rewritten), so the
                       mint / reuse / retention KPIs can read the non-scan rows
                       and publish how many were excluded beside them.
  2. Mint rate limit — check_mint_rate() counts recent mints per caller IP and
                       per User-Agent before a new key is written.
  3. Spike alert     — weekly_mint_spike() is the pure decision behind the
                       brain radar detector check_weekly_mint_spike.

WHY (measured 2026-09-24, GET /api/v1/mcp/retention -> key_reuse):

    week        minted  distinct_ips  reused_2plus  returned_next_week
    2026-08-24     195           153           137                  30
    2026-08-31     226           188           187                   9
    2026-09-07     238           208           195                  17
    2026-09-14  11,442           145         9,082                   0

  11,442 mints from 145 ip hashes is ~79 per hash against a ~1.3/hash
  baseline, and summary.pct_reused_30d (the "79% reuse" figure) is computed
  over those rows. The week lines up with #4612/#4616 (2026-09-14/15), which
  removed the (ip_hash, ua) reuse the mint doors relied on to stop a retrying
  caller fanning out into N keys. That removal was a deliberate security fix —
  it handed a credential to a caller that had not presented it — so it is NOT
  reverted here and a limited caller is NOT handed an existing key; it gets a
  429 with Retry-After instead.

  The reuse figure has a second, independent inflation: a born-gated mint
  (notes 'gate_carry:N') is INSERTed with call_count = N >= 10, so it reads as
  "reused 2+" before anyone has used it once. real_calls subtracts that seed.

Every threshold is env-tunable and read per call (no redeploy to retune).
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import statistics

log = logging.getLogger("mint_guard")

# ── scan definition (KPI exclusion) ──────────────────────────────────────
# Probe / self-traffic UA markers. Mirrors mcp_retention._INTERNAL plus the
# common open-internet scanner UAs. Deliberately does NOT include generic HTTP
# client UAs (node, curl, python-requests): real agents send those.
SCAN_UA_RE = (r"(loop|dchub-|selfheal|probe|health|scanner|regression|mcp-test|"
              r"sweep|clawith|anthropicapi|zgrab|masscan|nuclei|nmap)")

# Our own MCP harness traffic. The MCP server tags it platform='dchub-internal'
# (server.mjs _INTERNAL_SELF_TAG) and every backend read predicate already
# excludes %dchub%. Its UA is plain `node`, which SCAN_UA_RE deliberately does
# not match, and it mints fewer than pair_per_day keys per caller, so until
# 2026-09-25 it counted as real agents. Measured that day over the non-scan
# mints of weeks 08-24..09-14: 224 of 970 keys were internal, and they carried
# 105k of the 105k logged calls on those keys (external keys: 24 calls).
INTERNAL_PLATFORM_LIKE = "%dchub%"

# notes on a born-gated mint: "gate_carry:<seed> (...)" — see auto_trial.py.
_SEED_SQL = r"COALESCE(substring(t.notes from '^gate_carry:([0-9]+)')::int, 0)"


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


def scan_thresholds() -> dict:
    """The live KPI scan definition. Published verbatim in the KPI payloads."""
    return {
        # one (ip_hash, UA) minting more than this many keys in one UTC day
        "pair_per_day": max(1, _env_int("DCHUB_MINT_SCAN_PAIR_PER_DAY", 20)),
        # one ip_hash (any UA) minting more than this in one UTC day. Higher
        # than the pair threshold because the MCP gateway's inline mints reach
        # the backend from its own egress, so several real agents can share
        # one ip_hash — this only catches volume no shared egress explains.
        "ip_per_day": max(1, _env_int("DCHUB_MINT_SCAN_IP_PER_DAY", 100)),
        "ua_regex": SCAN_UA_RE,
    }


def scored_trial_keys_cte(window_sql: str) -> tuple[str, tuple]:
    """SQL for two CTEs, `base` and `scored`, over auto_trial_keys.

    window_sql is TRUSTED SQL (a literal like "interval '30 days'" or a
    placeholder expression the caller parametrises itself) — never user input.
    Its own placeholders, if any, must be bound BEFORE the returned params.

    `scored` exposes every auto_trial_keys column plus:
        is_scan    bool  — the row belongs to scan traffic (see scan_thresholds)
        real_calls int   — call_count minus any gate_carry seed
    """
    th = scan_thresholds()
    sql = f"""
        base AS (
            SELECT t.*,
                   {_SEED_SQL} AS _seed,
                   COUNT(*) OVER (PARTITION BY t.request_ip_hash, t.request_ua,
                                               date_trunc('day', t.minted_at)) AS _pair_day_n,
                   COUNT(*) OVER (PARTITION BY t.request_ip_hash,
                                               date_trunc('day', t.minted_at)) AS _ip_day_n
              FROM auto_trial_keys t
             WHERE t.minted_at >= NOW() - {window_sql}
        ),
        scored AS (
            SELECT base.*,
                   (_pair_day_n > %s OR _ip_day_n > %s
                    OR COALESCE(request_ua, '') ~* %s
                    -- internal harness: tagged at mint (mcp_platform, read
                    -- tolerantly so a table without the column still works),
                    -- or recognised by the platform its calls were logged under
                    OR COALESCE(to_jsonb(base) ->> 'mcp_platform', '') ILIKE %s
                    OR EXISTS (SELECT 1 FROM mcp_call_log l
                                WHERE l.api_key = base.api_key
                                  AND l.platform ILIKE %s)) AS is_scan,
                   GREATEST(COALESCE(call_count, 0) - _seed, 0) AS real_calls
              FROM base
        )"""
    return sql, (th["pair_per_day"], th["ip_per_day"], th["ua_regex"],
                 INTERNAL_PLATFORM_LIKE, INTERNAL_PLATFORM_LIKE)


def scan_definition_note() -> dict:
    th = scan_thresholds()
    return {
        "definition": (
            f"A trial-key mint is SCAN traffic when, in its UTC day, its "
            f"(request_ip_hash, request_ua) pair minted more than "
            f"{th['pair_per_day']} keys, OR its request_ip_hash minted more than "
            f"{th['ip_per_day']} keys, OR its UA matches a probe marker, OR it is "
            f"DC Hub's own MCP harness (minted with, or called under, an MCP "
            f"platform matching '{INTERNAL_PLATFORM_LIKE}'). Rows are "
            f"never deleted: they are filtered at query time and counted in "
            f"excluded_scan_mints."),
        "pair_per_day": th["pair_per_day"],
        "ip_per_day": th["ip_per_day"],
        "ua_regex": th["ua_regex"],
        "internal_platform_like": INTERNAL_PLATFORM_LIKE,
        "reuse_basis": (
            "reused = real_calls > 1, where real_calls = call_count minus the "
            "gate_carry seed a born-gated mint is INSERTed with. Without the "
            "subtraction every born-gated key reads as reused before its first call."),
        "tune_with": ["DCHUB_MINT_SCAN_PAIR_PER_DAY", "DCHUB_MINT_SCAN_IP_PER_DAY"],
    }


# ── mint rate limit ──────────────────────────────────────────────────────
def mint_limits() -> dict:
    """Per-caller mint ceilings. 0 disables that one ceiling.

    Baseline (weeks 08-24..09-07): ~200 mints/week across ~180 ip hashes,
    i.e. ~1.3 mints per IP per WEEK and ~34 mints/day across every caller.
    A real agent mints a handful; these sit an order of magnitude above that.
    """
    return {
        "ip_hour": max(0, _env_int("DCHUB_MINT_RL_IP_PER_HOUR", 10)),
        "ip_day":  max(0, _env_int("DCHUB_MINT_RL_IP_PER_DAY", 30)),
        # UA ceilings are a distributed-scan backstop, not the main guard: a
        # common agent UA is shared by many real callers, so they sit far above
        # the whole system's baseline daily volume (~34/day).
        "ua_hour": max(0, _env_int("DCHUB_MINT_RL_UA_PER_HOUR", 60)),
        "ua_day":  max(0, _env_int("DCHUB_MINT_RL_UA_PER_DAY", 500)),
    }


def is_internal_request(req) -> bool:
    """True when the request carries the MCP gateway's internal key.

    Matters for WHICH scope is the caller's: the gateway calls /keys/auto-mint
    from its own egress (so the IP is the gateway's, the forwarded UA is the
    agent's), and calls /keys/claim with the agent's IP in X-Forwarded-For but
    its own UA. Counting a scope that belongs to the gateway would let one
    scanner lock every real agent out of the on-ramp.
    """
    try:
        sent = (req.headers.get("X-Internal-Key") or "").strip()
        if not sent:
            return False
        for name in ("DCHUB_INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
            want = (os.environ.get(name) or "").strip()
            if want and hmac.compare_digest(sent, want):
                return True
    except Exception:
        return False
    return False


# ── the MCP gateway's caller identity (r-mint-gateway-id, 2026-09-24) ──────
# Measured 2026-09-24 (auto_trial_keys, last 24h): the UA over the 500/day
# ceiling was "node" — 546 mints across 12 tools. It is the REAL agent UA the
# gateway forwards for Node-based MCP clients and Smithery's proxy, i.e. many
# unrelated callers pooled under one string. Counting it would lock every one of
# them out of the on-ramp together. And the gateway calls us from its own
# egress, so request_ip_hash was the gateway's IP (17 hashes for 546 mints), not
# the caller's.
#
# So for a gateway mint (internal key):
#   * the caller's IP comes from X-DCHub-Client-IP, honoured ONLY with a valid
#     internal key and only when it parses as an IP (anyone can call the origin
#     directly; without the key the header is ignored);
#   * the IP ceiling counts that forwarded IP, unless the platform reaches the
#     gateway through ONE shared egress (Smithery's proxy, Grok's connector):
#     there one IP is many people;
#   * the UA ceiling counts the forwarded UA only when it names a client, never
#     a pooled runtime default.
SHARED_EGRESS_PLATFORMS = frozenset({"smithery", "grok", "connectors-manager"})
POOLED_UAS = frozenset({"", "node", "undici", "node-fetch", "axios"})


def gateway_caller_ip(req) -> str:
    """The caller IP the MCP gateway forwarded, or '' (not internal, absent,
    or not an IP). Never trusted without the internal key."""
    try:
        if not is_internal_request(req):
            return ""
        raw = (req.headers.get("X-DCHub-Client-IP") or "").strip()
        if not raw or len(raw) > 64:
            return ""
        return str(ipaddress.ip_address(raw))
    except Exception:
        return ""


def ua_names_a_client(ua: str) -> bool:
    u = (ua or "").strip().lower()
    return bool(u) and u not in POOLED_UAS and not u.startswith(("node/", "undici/", "node-fetch/"))


def gateway_platform(req) -> str:
    """X-MCP-Platform, but only from our own gateway (an internal request) —
    a direct caller cannot label its mint. '' otherwise."""
    try:
        if not is_internal_request(req):
            return ""
        return (req.headers.get("X-MCP-Platform") or "").strip().lower()[:40]
    except Exception:
        return ""


def trial_mint_scopes(req, ua: str, forwarded_ip: str) -> tuple[bool, bool]:
    """(count_ip, count_ua) for mint_trial_for_request.

    Direct caller: both, as before. Gateway caller: the IP only when the
    gateway forwarded one and the platform is not a shared egress; the UA only
    when it names a client."""
    if not is_internal_request(req):
        return True, True
    platform = (req.headers.get("X-MCP-Platform") or "").strip().lower()
    count_ip = bool(forwarded_ip) and platform not in SHARED_EGRESS_PLATFORMS
    return count_ip, ua_names_a_client(ua)


# Count queries. {ip_col}/{ua_col}/{ts_col}/{table}/{where} are fixed
# per-source SQL fragments below — never caller input.
_SOURCES = {
    "trial": dict(table="auto_trial_keys", ts_col="minted_at",
                  ip_col="request_ip_hash", ua_col="request_ua", where="TRUE"),
    "claim": dict(table="mcp_dev_keys", ts_col="created_at",
                  ip_col="metadata->>'ip'", ua_col="metadata->>'user_agent'",
                  where="metadata->>'source' = 'claim_api'"),
}


def _count_sql(src: dict) -> str:
    ip, ua, ts = src["ip_col"], src["ua_col"], src["ts_col"]
    return f"""
        SELECT
          COUNT(*) FILTER (WHERE {ip} = %(ip)s AND {ts} > NOW() - interval '1 hour'),
          COUNT(*) FILTER (WHERE {ip} = %(ip)s),
          COUNT(*) FILTER (WHERE {ua} = %(ua)s AND {ts} > NOW() - interval '1 hour'),
          COUNT(*) FILTER (WHERE {ua} = %(ua)s),
          CEIL(EXTRACT(EPOCH FROM MIN({ts}) FILTER (WHERE {ip} = %(ip)s
                 AND {ts} > NOW() - interval '1 hour') + interval '1 hour' - NOW())),
          CEIL(EXTRACT(EPOCH FROM MIN({ts}) FILTER (WHERE {ip} = %(ip)s)
                 + interval '1 day' - NOW())),
          CEIL(EXTRACT(EPOCH FROM MIN({ts}) FILTER (WHERE {ua} = %(ua)s
                 AND {ts} > NOW() - interval '1 hour') + interval '1 hour' - NOW())),
          CEIL(EXTRACT(EPOCH FROM MIN({ts}) FILTER (WHERE {ua} = %(ua)s)
                 + interval '1 day' - NOW()))
          FROM {src['table']}
         WHERE {src['where']}
           AND {ts} > NOW() - interval '1 day'
           AND ({ip} = %(ip)s OR {ua} = %(ua)s)"""


def check_mint_rate(cur, source: str, *, ip_key: str, ua: str,
                    count_ip: bool = True, count_ua: bool = True) -> dict | None:
    """None when the caller may mint; otherwise the limit that was hit:

        {"scope": "ip"|"ua", "window": "hour"|"day", "limit": n,
         "count": c, "retry_after": seconds}

    ip_key is whatever the source table stores (auto_trial_keys: the 16-char
    ip hash; mcp_dev_keys: the raw metadata ip). FAIL-OPEN: any error returns
    None — a broken counter must never close the free-tier on-ramp.
    """
    lim = mint_limits()
    count_ip = count_ip and bool(ip_key) and (lim["ip_hour"] or lim["ip_day"])
    count_ua = count_ua and (lim["ua_hour"] or lim["ua_day"])
    if not (count_ip or count_ua):
        return None
    try:
        # A scope we must not count binds NULL: `col = NULL` matches no row.
        cur.execute(_count_sql(_SOURCES[source]),
                    {"ip": ip_key if count_ip else None,
                     "ua": (ua or "") if count_ua else None})
        row = cur.fetchone()
        if not row:
            return None
        ip_h, ip_d, ua_h, ua_d = (int(x or 0) for x in row[:4])
        ra = [max(1, int(x)) if x is not None else 3600 for x in row[4:8]]
    except Exception:
        return None
    checks = (
        ("ip", "hour", lim["ip_hour"], ip_h, ra[0], count_ip),
        ("ip", "day",  lim["ip_day"],  ip_d, ra[1], count_ip),
        ("ua", "hour", lim["ua_hour"], ua_h, ra[2], count_ua),
        ("ua", "day",  lim["ua_day"],  ua_d, ra[3], count_ua),
    )
    hit = [c for c in checks if c[5] and c[2] and c[3] >= c[2]]
    if not hit:
        return None
    # Report the ceiling that clears LAST, so a caller honouring Retry-After
    # is not refused again on its next attempt.
    scope, window, limit, count, retry, _ = max(hit, key=lambda c: c[4])
    return {"scope": scope, "window": window, "limit": limit,
            "count": count, "retry_after": retry}


# ── log-only first (owner decision, 2026-09-24) ──────────────────────────
# #5468 shipped the ceilings ENFORCING. The thresholds rest on public weekly
# aggregates only (nobody could read the top IPs/UAs behind the 09-14 spike),
# and this is the free-tier on-ramp, so the owner chose to watch before
# refusing: unless DCHUB_MINT_RL_MODE is exactly "enforce", a caller over a
# ceiling is LOGGED ("mint_rate_would_refuse ...") and minted as usual. Read
# those lines, tune DCHUB_MINT_RL_*, then set DCHUB_MINT_RL_MODE=enforce.
def mint_rate_enforced() -> bool:
    return (os.environ.get("DCHUB_MINT_RL_MODE") or "").strip().lower() == "enforce"


def mint_rate_decision(cur, source: str, **kw) -> dict | None:
    """What a mint door acts on: check_mint_rate's hit when enforcing, else
    None after logging the refusal it WOULD have made. Never raises."""
    hit = check_mint_rate(cur, source, **kw)
    if hit and not mint_rate_enforced():
        try:
            log.warning(
                "mint_rate_would_refuse source=%s scope=%s window=%s count=%s "
                "limit=%s mode=log", source, hit["scope"], hit["window"],
                hit["count"], hit["limit"])
        except Exception:
            pass
        return None
    return hit


def rate_limited_body(hit: dict) -> dict:
    """The additive refusal body shared by every mint door."""
    return {
        "ok": False,
        "error": "mint_rate_limited",
        "reason": "rate_limited",
        "scope": hit["scope"],
        "window": hit["window"],
        "limit": hit["limit"],
        "retry_after": hit["retry_after"],
        "detail": (
            f"Too many new keys minted for this {'address' if hit['scope'] == 'ip' else 'client'} "
            f"in the last {hit['window']} ({hit['count']}/{hit['limit']}). Reuse the key you "
            f"were already issued (send it as X-API-Key — presenting it returns the same "
            f"key, never a refusal), or retry after {hit['retry_after']}s."),
    }


# ── weekly spike alert ───────────────────────────────────────────────────
def spike_thresholds() -> dict:
    return {
        "multiple": max(1.0, float(_env_int("DCHUB_MINT_SPIKE_MULTIPLE", 5))),
        "min_mints": max(1, _env_int("DCHUB_MINT_SPIKE_MIN", 300)),
    }


def weekly_mint_spike(latest: int, trailing: list[int]) -> dict | None:
    """Pure decision: does `latest` (mints in the last 7 days) far exceed the
    trailing baseline (the 4 prior 7-day windows)?

    Fires when latest > multiple x median(trailing) AND latest >= min_mints
    (a floor so a quiet baseline of 3 cannot turn 16 mints into an alert).
    """
    th = spike_thresholds()
    trailing = [int(x or 0) for x in (trailing or [])]
    if len(trailing) < 2:
        return None          # not enough history to call anything a spike
    median = statistics.median(trailing)
    latest = int(latest or 0)
    if latest < th["min_mints"]:
        return None
    if latest <= th["multiple"] * max(median, 1):
        return None
    return {"latest": latest, "trailing": trailing, "median": median,
            "ratio": round(latest / max(median, 1), 1),
            "multiple": th["multiple"], "min_mints": th["min_mints"]}
