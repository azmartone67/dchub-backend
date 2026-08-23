"""Page-usage join — sitemap inventory x Cloudflare edge requests, split
human vs agent.

r-page-usage (2026-08-23). The question this answers is "which of our pages is
nobody using", and the reason it needs its own surface is that the two
instruments we already had each answer a strictly smaller question:

  * Microsoft Clarity is a JS beacon. It sees ONLY humans, and only the pages a
    human happened to load inside its short retention window. Absence from a
    128-session sample is not evidence of absence — this is the ABSENT != empty
    class. It also cannot see the agent channel AT ALL, because crawlers and
    MCP clients do not execute JS.
  * The backend's own request tables (crawler_visits, ai_access_log, ...) are
    written by specific detectors, not by a universal hook, and they are blind
    by construction to every statically-served page: `/` and `/land-power-map`
    are CF Pages assets that never reach this process.

Cloudflare sits in front of BOTH — static Pages assets and worker-proxied
backend pages, humans and agents alike — so the zone is the only vantage point
that can be joined against the full sitemap and produce a coverage answer
rather than a popularity list.

Surface:
  GET /api/v1/admin/page-usage    — the join (admin-key gated, fail-closed)

HONEST NO-OP without CF_ANALYTICS_READ_TOKEN — the same dedicated zone-scoped
token routes/cf_analytics.py already uses. This module deliberately imports
that module's client rather than re-deriving the token, so there is exactly one
place where zone credentials are resolved.

★ WHAT THIS DOES NOT CLAIM. Cloudflare's httpRequestsAdaptiveGroups is a
SAMPLED dataset — `count` is an estimate scaled from a sample, not a census.
The payload reports that, reports whether the row budget truncated the result,
and reports the request floor a path needed to appear at all. A page in
`zero_request_paths` is a page that did not appear in the sampled top-N, which
is a much weaker statement than "nobody ever loaded it", and the field names
say so.
"""

import datetime as _dt
import logging
import os
import re

import requests

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

page_usage_bp = Blueprint("page_usage", __name__)

# Loopback base, same reasoning as routes/cron_heartbeat.py: the sitemap is
# worker-served, so fetching it by public hostname would leave this process,
# traverse Cloudflare, and come back to these same handlers. Loopback reaches
# them directly and cannot trip the same-zone loop guard.
_BASE = (
    "http://127.0.0.1:{}".format(os.environ.get("PORT", "8080"))
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else "https://api.dchub.cloud"
)

# CF caps adaptive-group queries at 10k rows. We group by path only (not by
# path x user-agent, whose cardinality would burn the whole budget on a handful
# of busy pages), and run one query per audience class instead.
_EDGE_ROW_LIMIT = 10000

_DEFAULT_DAYS = 7
_MAX_DAYS = 30

# ── AUDIENCE CLASSIFIER ──────────────────────────────────────────────────
# Order matters and is load-bearing: bots overwhelmingly identify as
# "Mozilla/5.0 (compatible; Googlebot/2.1; ...)", so a browser-token test that
# ran first would classify most of the crawl fleet as human. SELF is checked
# before AGENT for the same reason in reverse — our own probes are the single
# largest UA cohort on this zone (~40% of requests at last decomposition), and
# counting them as demand is the self-traffic error this codebase has already
# paid for more than once.

# Our own traffic: probes, cron, health checks, the brain's own loops.
_SELF_RE = re.compile(r"dchub|DCHub|uptimerobot|better\s?uptime|pingdom", re.I)

# Declared automation of any kind — crawlers, AI agents, SDKs, CLIs.
_AGENT_RE = re.compile(
    r"bot\b|bot/|spider|crawler|crawl;|slurp|archiver|"
    r"googlebot|bingbot|yandex|duckduck|baiduspider|applebot|"
    r"gptbot|oai-searchbot|chatgpt|claudebot|anthropic|perplexity|"
    r"ccbot|bytespider|amazonbot|meta-externalagent|google-extended|"
    r"semrush|ahrefs|mj12|dotbot|petalbot|dataforseo|screaming\s?frog|"
    r"python-requests|python-httpx|httpx|aiohttp|urllib|scrapy|"
    r"curl/|wget/|go-http-client|java/|okhttp|node-fetch|axios|guzzle|"
    r"postman|insomnia|libwww|lwp::|mcp-|modelcontextprotocol",
    re.I,
)

# A real browser engine token. Only consulted AFTER self and agent miss.
_HUMAN_RE = re.compile(
    r"(chrome|crios|safari|firefox|fxios|edg[ea]?/|opera|opr/|samsungbrowser)",
    re.I,
)


def classify_ua(ua):
    """Bucket a user-agent string into 'self' | 'agent' | 'human' | 'unknown'.

    Pure function, deliberately exported without an underscore: it is the one
    piece of judgement in this module, so it is the piece that has to be
    directly testable. Every other step is plumbing.
    """
    s = (ua or "").strip()
    if not s:
        # An empty UA is automation that did not bother to declare itself. It
        # is NOT a human — but it is not an identified agent either, and
        # folding it into either bucket would overstate that bucket.
        return "unknown"
    if _SELF_RE.search(s):
        return "self"
    if _AGENT_RE.search(s):
        return "agent"
    if _HUMAN_RE.search(s):
        return "human"
    return "unknown"


def _norm_path(p):
    """Normalise an edge path or sitemap loc to a comparable key.

    Strips scheme+host, drops the query string, and collapses a trailing slash
    so `/pricing` and `/pricing/` are one page. The site's canonical form is
    the clean, slashless path.
    """
    s = (p or "").strip()
    if not s:
        return ""
    if s.startswith("http://") or s.startswith("https://"):
        # cut scheme, then everything up to the first path separator
        rest = s.split("://", 1)[1]
        slash = rest.find("/")
        s = rest[slash:] if slash >= 0 else "/"
    s = s.split("?", 1)[0].split("#", 1)[0]
    if len(s) > 1 and s.endswith("/"):
        s = s[:-1]
    return s or "/"


# ── INVENTORY ────────────────────────────────────────────────────────────

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def _fetch_xml(url, timeout=12):
    r = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": "DCHub-PageUsage/1.0", "X-DC-Internal-Cron": "1"},
    )
    r.raise_for_status()
    return r.text


def _sitemap_paths():
    """Every path the site publicly CLAIMS to publish, from its own sitemap.

    Reads the sitemap index and follows each child. Using the live sitemap
    rather than re-deriving the URL set from SQL is deliberate: the sitemap is
    the site's published promise, so the coverage answer stays correct when
    that promise changes, instead of drifting against a second copy of the
    query that built it.

    Returns (paths_by_section, error) — never raises.
    """
    try:
        index = _fetch_xml("{}/sitemap.xml".format(_BASE))
    except Exception as e:  # noqa: BLE001 - reported, never raised
        return {}, "sitemap index unreachable: {}".format(str(e)[:160])

    children = _LOC_RE.findall(index)
    by_section = {}

    if not children:
        return {}, "sitemap index contained no <loc> entries"

    for child in children:
        # Section name from sitemap-<section>.xml; the index itself only ever
        # points at shard files.
        base = child.rsplit("/", 1)[-1]
        section = base[len("sitemap-"):-len(".xml")] if (
            base.startswith("sitemap-") and base.endswith(".xml")) else base
        try:
            body = _fetch_xml(
                "{}/{}".format(_BASE, base) if _BASE not in child else child)
        except Exception as e:  # noqa: BLE001
            by_section.setdefault(section, set())
            logger.warning("[page_usage] shard %s failed: %s", base, str(e)[:120])
            continue
        locs = {_norm_path(u) for u in _LOC_RE.findall(body)}
        locs.discard("")
        by_section.setdefault(section, set()).update(locs)

    return by_section, None


# ── EDGE USAGE ───────────────────────────────────────────────────────────

# ★ THE SCALAR IS NOT GUESSABLE FROM IN-REPO EVIDENCE, so it is not guessed.
# Cloudflare names the datetime scalar `Time` on some analytics datasets and
# `DateTime` on others: routes/cf_analytics.py's ACCOUNT-scope
# httpRequestsAdaptiveGroups query declares `DateTime!` and works today, while
# the zone-scope adaptive schema is documented as `Time`. Rather than pick one
# and ship a query that 400s in production against a name nobody verified, the
# same query is emitted under both scalars and the second is tried only if the
# first is rejected. `_EDGE_SCALARS` is ordered zone-first because this IS the
# zone-scope query. Whichever wins is reported back in the payload, so the
# answer becomes evidence instead of staying folklore.
_EDGE_SCALARS = ("Time", "DateTime")

_EDGE_QUERY = """
query PageUsage($zoneTag: String!, $since: %(scalar)s!, $until: %(scalar)s!, $limit: Int!) {
  viewer {
    zones(filter: {zoneTag: $zoneTag}) {
      httpRequestsAdaptiveGroups(
        filter: {datetime_geq: $since, datetime_lt: $until}
        orderBy: [count_DESC]
        limit: $limit
      ) {
        count
        dimensions { clientRequestPath userAgent }
      }
    }
  }
}
"""


def _edge_usage(days, row_limit):
    """Per-path request counts from the Cloudflare zone, bucketed by audience.

    Returns (usage, meta, error) where usage maps normalised path ->
    {'human','agent','self','unknown','total'}.
    """
    try:
        from routes.cf_analytics import _cf_graphql, _CF_ZONE_ID, _CF_ZONE_TOKEN
    except Exception as e:  # noqa: BLE001
        return {}, {}, "cf_analytics client unavailable: {}".format(str(e)[:120])

    if not _CF_ZONE_TOKEN:
        return {}, {}, ("CF_ANALYTICS_READ_TOKEN not set — honest no-op. It is a"
                        " zone-scoped read token and must live in the Railway"
                        " backend env, not GitHub secrets.")
    if not _CF_ZONE_ID:
        return {}, {}, "CLOUDFLARE_ZONE_ID not set — cannot scope the query."

    until = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
    since = until - _dt.timedelta(days=days)
    variables = {
        "zoneTag": _CF_ZONE_ID,
        "since": since.isoformat().replace("+00:00", "Z"),
        "until": until.isoformat().replace("+00:00", "Z"),
        "limit": row_limit,
    }

    payload = None
    scalar_used = None
    last_error = None
    for scalar in _EDGE_SCALARS:
        attempt = _cf_graphql(
            _EDGE_QUERY % {"scalar": scalar}, variables, token=_CF_ZONE_TOKEN)
        if attempt and not attempt.get("errors"):
            payload, scalar_used = attempt, scalar
            break
        last_error = (str(attempt.get("errors"))[:200] if attempt
                      else "call failed (see logs)")
    if payload is None:
        return {}, {}, ("Cloudflare GraphQL rejected the query under every"
                        " datetime scalar tried {}: {}".format(
                            list(_EDGE_SCALARS), last_error))

    try:
        zones = payload["data"]["viewer"]["zones"]
        rows = zones[0]["httpRequestsAdaptiveGroups"] if zones else []
    except Exception:  # noqa: BLE001 - unknown shape yields an honest empty
        return {}, {}, "Cloudflare returned an unexpected shape."

    usage = {}
    audience_totals = {"human": 0, "agent": 0, "self": 0, "unknown": 0}
    for row in rows:
        try:
            dims = row.get("dimensions") or {}
            path = _norm_path(dims.get("clientRequestPath"))
            if not path:
                continue
            n = int(row.get("count") or 0)
            bucket = classify_ua(dims.get("userAgent"))
            slot = usage.setdefault(
                path, {"human": 0, "agent": 0, "self": 0, "unknown": 0, "total": 0})
            slot[bucket] += n
            slot["total"] += n
            audience_totals[bucket] += n
        except Exception:  # noqa: BLE001 - one bad row must not kill the join
            continue

    meta = {
        "edge_rows_returned": len(rows),
        "edge_row_limit": row_limit,
        # A full result set is the tell that CF truncated us: ordered by
        # count_DESC, so what fell off the end is the long tail — exactly the
        # pages this report is about. Say so rather than imply completeness.
        "truncated": len(rows) >= row_limit,
        "audience_totals": audience_totals,
        # Which scalar Cloudflare actually accepted — recorded so the next
        # person reads a fact instead of re-running this experiment.
        "datetime_scalar_accepted": scalar_used,
        "window_start": since.isoformat().replace("+00:00", "Z"),
        "window_end": until.isoformat().replace("+00:00", "Z"),
    }
    return usage, meta, None


# ── THE JOIN ─────────────────────────────────────────────────────────────

def _admin_ok():
    admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("ADMIN_KEY") or "").strip()
    if not admin_key:
        return jsonify({"error": "admin_endpoint_unconfigured"}), 503
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if provided != admin_key:
        return jsonify({"error": "unauthorized"}), 401
    return None


@page_usage_bp.route("/api/v1/admin/page-usage", methods=["GET"])
def page_usage():
    gate = _admin_ok()
    if gate:
        return gate

    try:
        days = max(1, min(_MAX_DAYS, int(request.args.get("days", _DEFAULT_DAYS))))
    except Exception:  # noqa: BLE001
        days = _DEFAULT_DAYS
    try:
        sample = max(0, min(500, int(request.args.get("sample", 25))))
    except Exception:  # noqa: BLE001
        sample = 25
    section_filter = (request.args.get("section") or "").strip().lower()

    by_section, inv_err = _sitemap_paths()
    if inv_err:
        return jsonify({"ok": False, "stage": "inventory", "error": inv_err}), 502

    usage, meta, edge_err = _edge_usage(days, _EDGE_ROW_LIMIT)
    if edge_err:
        return jsonify({
            "ok": True, "available": False, "stage": "edge", "skipped": edge_err,
            "inventory": {s: len(p) for s, p in sorted(by_section.items())},
        })

    sections_out = {}
    zero_examples = {}
    agent_only_examples = {}
    total_inventory = 0

    for section, paths in sorted(by_section.items()):
        if section_filter and section != section_filter:
            continue
        total_inventory += len(paths)
        human = agent = zero = agent_only = 0
        zeros, agents_only = [], []
        for p in sorted(paths):
            u = usage.get(p)
            if not u or u["total"] == 0:
                zero += 1
                if len(zeros) < sample:
                    zeros.append(p)
                continue
            if u["human"] > 0:
                human += 1
            else:
                agent_only += 1
                if len(agents_only) < sample:
                    agents_only.append({"path": p, "agent": u["agent"],
                                        "unknown": u["unknown"]})
            if u["agent"] > 0:
                agent += 1
        sections_out[section] = {
            "inventory": len(paths),
            "human_touched": human,
            "agent_touched": agent,
            "agent_only": agent_only,
            "absent_from_sample": zero,
        }
        if zeros:
            zero_examples[section] = zeros
        if agents_only:
            agent_only_examples[section] = agents_only

    # Paths the edge saw that the sitemap never claimed. High-signal: these are
    # live surfaces with no declared home, which is how orphan pages and stale
    # routes hide.
    claimed = set()
    for paths in by_section.values():
        claimed |= paths
    unclaimed = sorted(
        ({"path": p, "total": u["total"], "human": u["human"], "agent": u["agent"]}
         for p, u in usage.items() if p not in claimed and u["total"] > 0),
        key=lambda d: -d["total"])[:sample]

    return jsonify({
        "ok": True,
        "available": True,
        "window_days": days,
        "inventory_source": "{}/sitemap.xml".format(_BASE),
        "inventory_total": total_inventory,
        "sections": sections_out,
        "absent_from_sample_examples": zero_examples,
        "agent_only_examples": agent_only_examples,
        "served_but_not_in_sitemap": unclaimed,
        "edge": meta,
        "audience_rule": {
            "order": ["self", "agent", "human", "unknown"],
            "why": ("Order is load-bearing. Crawlers identify as"
                    " 'Mozilla/5.0 (compatible; Googlebot/2.1)', so a browser"
                    " test running first would score the crawl fleet as human;"
                    " and our own probes are the largest single cohort on this"
                    " zone, so they are removed before any demand is counted."),
        },
        "honesty": {
            "sampled": True,
            "sampling_note": ("Cloudflare httpRequestsAdaptiveGroups is a"
                              " SAMPLED dataset: count is an estimate scaled"
                              " from a sample, not a census."),
            "absent_means": ("'absent_from_sample' means the path did not"
                             " appear in the top {} rows by request count over"
                             " the window. That is NOT proof nobody loaded it."
                             .format(_EDGE_ROW_LIMIT)),
            "truncated": meta.get("truncated"),
            "clarity_blind_spots": ("Clarity cannot answer this question: it is"
                                    " a JS beacon, so it sees no agent traffic"
                                    " at all, and its page list is a hit list,"
                                    " not an inventory."),
        },
    })
