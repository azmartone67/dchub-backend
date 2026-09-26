"""Install funnel — probe-excluded visitor -> mint-attempt -> mint, per /install page.

★ WHY THIS EXISTS (2026-09-20). /api/v1/ops/install-stats reads `minted: 0`
(probe-excluded) for the twelve /install/<client> pages, all-time. A zero at the
bottom of a funnel does not say WHERE it leaks, and the three candidate leaks
need three different fixes:

  * nobody came           -> distribution; no page change can convert it
  * came, never pressed   -> the page (CTA / copy)
  * pressed, got a key we
    cannot see            -> the ledger. /api/v1/keys/claim is idempotent per
                             (client_name, ip) AND hands an IP that already holds
                             unused keys its NEWEST one back — under that key's
                             ORIGINAL client_name. So a returning visitor who
                             presses "mint" on /install/claude can walk away with
                             a web-map key and never show as install-anything.

So each rung is measured separately and published beside the others.

RUNGS (per page; windows 7d / 30d / since the attempt ledger began):
  visitors       distinct (client IP, user-agent) pairs that loaded
                 /install/<slug> with a browser user-agent, from a non-datacenter
                 network, and got a 200/304 — Cloudflare zone analytics. These
                 pages are static Pages assets that never reach this process, so
                 the zone is the only place a visit exists. IPs are counted
                 in-process and never leave it.
  mint_attempts  distinct (ip hash, ua hash, UTC day) that POSTed
                 /api/v1/keys/claim as client_name=install-<slug>, written by the
                 claim handler on EVERY outcome — minted, reused, capped, failed.
  mints          keys minted as install-<slug>: the install-stats population and
                 its probe exclusion, imported from routes/install_stats.py rather
                 than re-typed, so the two surfaces cannot disagree on who is a
                 probe.

★ PROBE-EXCLUDED, AND IT SAYS WHAT THAT MEANS. Every figure below drops: the
reserved install-verify-% namespace (our end-to-end probes), our own QA and
monitor user-agents, declared bots/agents/SDKs, headless browsers, empty or
non-browser user-agents, and browser user-agents arriving from a datacenter ASN.
Each excluded class is still COUNTED, in `excluded_requests`, so the exclusions
are auditable instead of silent.

★ WHAT THIS DOES NOT CLAIM.
  * Cloudflare's httpRequestsAdaptiveGroups is SAMPLED. `edge.sample_interval_max`
    says whether this read was a census (1) or an estimate; when sampled, distinct
    visitors are a floor.
  * Our own manual browser visits carry a real browser UA from a residential IP
    and are indistinguishable from a human's. Bounded, not zero.
  * An attempt whose POST never reached this process (network, extension, edge
    5xx) is not an attempt here.
"""
import datetime as _dt
import hashlib
import logging
import re
import threading
import time

import psycopg2
from flask import Blueprint, jsonify

from routes.install_stats import (
    _CONTROL_PREFIX,
    _EXCLUDE_NOTHING,
    _INSTALL_PREFIX,
    _NOT_A_PROBE,
    _PROBE_PREFIX,
    _dsn,
)
from routes.page_usage import _norm_path, classify_ua as _page_usage_classify

log = logging.getLogger("install_funnel")
install_funnel_bp = Blueprint("install_funnel", __name__)

# The page roster. tests/test_install_funnel_contract.py pins it to the sitemap
# block in main.py, so a new page cannot ship without entering the funnel.
INSTALL_PAGES = (
    "claude", "chatgpt", "cursor", "grok", "perplexity", "gemini-cli",
    "claude-code", "claude-desktop", "cline", "vscode", "windsurf", "antigravity",
)

# Derived from the install-stats constants, never re-typed: `install-` and the
# reserved `install-verify-` probe namespace.
_INSTALL_STEM = _INSTALL_PREFIX.rstrip("%")
_PROBE_STEM = _PROBE_PREFIX.rstrip("%")

_LABEL = "probe-excluded"
_WINDOW_DAYS = (("7d", 7), ("30d", 30))
_CACHE_TTL_S = 600

# ── AUDIENCE ──────────────────────────────────────────────────────────────
# page_usage.classify_ua is the shared judgement and runs in the middle. Two
# classes it cannot see are handled around it:
#
#  * Our frontend QA fleet identifies as Brain-v2-* ("Brain-v2-headless/1.0
#    (DC Hub QA)", "Mozilla/5.0 (compatible; Brain-v2-csp/1.1)"). None of those
#    match page_usage's _SELF_RE, so they are checked FIRST, as self.
#  * A driven browser carries a real engine token ("HeadlessChrome/124.0"), so
#    classify_ua files it as human. Checked only after it says human.
_OWN_QA_RE = re.compile(r"brain-v2|dc hub qa|dchub", re.I)
_AUTOMATION_RE = re.compile(
    r"headless|lighthouse|pagespeed|ptst/|gtmetrix|playwright|puppeteer|"
    r"selenium|phantomjs|electron/",
    re.I,
)
# A browser UA from a cloud/hosting network is a scripted browser or a proxy far
# more often than a person. Deliberately NOT listed: Cloudflare, Akamai and
# Fastly — iCloud Private Relay and WARP egress through them, and those are
# people. `^google$` is AS15169 only; GOOGLE-FIBER is residential.
_DATACENTER_ASN_RE = re.compile(
    r"amazon|\baws\b|google-cloud|^google$|microsoft|azure|digitalocean|hetzner|"
    r"\bovh|linode|oracle|alibaba|tencent|choopa|vultr|contabo|scaleway|"
    r"online s\.?a\.?s|leaseweb|m247|datacamp|hostinger|ionos|psychz|quadranet|"
    r"colocrossing|servers-?com|g-?core|zenlayer|hivelocity|rackspace|softlayer",
    re.I,
)
_CLASSES = ("human", "self", "agent", "automation", "datacenter", "unknown")


def classify_visit(ua, asn_description=None):
    """Bucket one request into one of _CLASSES. Only 'human' is a visitor."""
    s = (ua or "").strip()
    if s and _OWN_QA_RE.search(s):
        return "self"
    base = _page_usage_classify(s)
    if base != "human":
        return base  # self | agent | unknown
    if _AUTOMATION_RE.search(s):
        return "automation"
    if asn_description and _DATACENTER_ASN_RE.search(str(asn_description).strip()):
        return "datacenter"
    return "human"


def _slug_of(path):
    p = _norm_path(path)
    if not p.startswith("/install/"):
        return None
    slug = p[len("/install/"):]
    if slug.endswith(".html"):
        slug = slug[:-len(".html")]
    return slug or None


def _hash16(s):
    return hashlib.sha256((s or "").encode("utf-8", "replace")).hexdigest()[:16]


# ── EDGE: visitors ────────────────────────────────────────────────────────
# Filtered to /install/% BEFORE grouping. page-usage groups the whole zone and
# its 10,000-row budget is spent on busy pages before a quiet one is reached —
# on 2026-09-20 it reported /install/claude-code at 36 agent requests over 7d
# and 28 over 30d, which is impossible for a complete count. A path filter
# spends the budget only on the pages being measured.
#
# The dimensions are tried richest-first because none of clientIP /
# clientASNDescription / datetimeHour / avg.sampleInterval had been exercised
# against this zone before this module; whichever variant Cloudflare accepts is
# published as `edge.variant`, so the answer is evidence rather than folklore.
_EDGE_VARIANTS = (
    ("ip+asn+hour+sampled",
     "clientRequestPath userAgent clientIP clientASNDescription edgeResponseStatus datetimeHour",
     "avg { sampleInterval }"),
    ("ip+asn+hour",
     "clientRequestPath userAgent clientIP clientASNDescription edgeResponseStatus datetimeHour", ""),
    ("ip+hour", "clientRequestPath userAgent clientIP edgeResponseStatus datetimeHour", ""),
    ("ua+date", "clientRequestPath userAgent edgeResponseStatus date", ""),
)
_EDGE_ROW_LIMIT = 10000
_EDGE_QUERY = """
query InstallFunnel($zoneTag: String!, $since: Time!, $until: Time!, $like: String!, $limit: Int!) {
  viewer {
    zones(filter: {zoneTag: $zoneTag}) {
      httpRequestsAdaptiveGroups(
        filter: {datetime_geq: $since, datetime_lt: $until, clientRequestPath_like: $like}
        orderBy: [count_DESC]
        limit: $limit
      ) {
        count
        %(extra)s
        dimensions { %(dims)s }
      }
    }
  }
}
"""
_RETENTION_QUERY = """
query InstallFunnelRetention($zoneTag: String!) {
  viewer { zones(filter: {zoneTag: $zoneTag}) {
    settings { httpRequestsAdaptiveGroups { notOlderThan maxDuration } }
  } }
}
"""
_accepted_variant = {"i": 0}


def _edge_rows(since, until):
    """-> (rows, meta, error). rows are raw Cloudflare groups."""
    try:
        from routes.cf_analytics import _CF_ZONE_ID, _CF_ZONE_TOKEN, _cf_graphql
    except Exception as e:  # noqa: BLE001
        return [], {}, "cf_analytics client unavailable: {}".format(str(e)[:120])
    if not _CF_ZONE_TOKEN:
        return [], {}, "CF_ANALYTICS_READ_TOKEN not set — visitors unmeasured (honest no-op)."
    if not _CF_ZONE_ID:
        return [], {}, "CLOUDFLARE_ZONE_ID not set — visitors unmeasured."
    variables = {
        "zoneTag": _CF_ZONE_ID,
        "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "until": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "like": "/install/%",
        "limit": _EDGE_ROW_LIMIT,
    }
    rejected = []
    for i in range(_accepted_variant["i"], len(_EDGE_VARIANTS)):
        name, dims, extra = _EDGE_VARIANTS[i]
        raw = _cf_graphql(_EDGE_QUERY % {"dims": dims, "extra": extra}, variables,
                          token=_CF_ZONE_TOKEN)
        if not raw:
            # Transport failure, not a schema answer. Falling through to a
            # poorer variant here would pin this process to it for good.
            return [], {"rejected_variants": rejected}, "edge query failed (no response)"
        # ★ Cloudflare answers HTTP 200 with an `errors` array. A 200 is not a
        # result; treating it as one is how a permissions failure reads as
        # "nobody visited".
        zones = ((raw.get("data") or {}).get("viewer") or {}).get("zones") or []
        if raw.get("errors") or not zones:
            rejected.append({"variant": name,
                             "error": str(raw.get("errors") or "no zone in response")[:200]})
            continue
        _accepted_variant["i"] = i
        rows = zones[0].get("httpRequestsAdaptiveGroups") or []
        meta = {"variant": name, "has_client_ip": "clientIP" in dims,
                "rejected_variants": rejected,
                "rows_returned": len(rows), "row_limit": _EDGE_ROW_LIMIT,
                "truncated": len(rows) >= _EDGE_ROW_LIMIT}
        meta.update(_edge_retention(_cf_graphql, _CF_ZONE_ID, _CF_ZONE_TOKEN))
        return rows, meta, None
    return [], {"rejected_variants": rejected}, "every edge query variant was rejected"


def _edge_retention(cf_graphql, zone, token):
    if "retention" in _accepted_variant:
        return _accepted_variant["retention"]
    raw = cf_graphql(_RETENTION_QUERY, {"zoneTag": zone}, token=token)
    try:
        s = raw["data"]["viewer"]["zones"][0]["settings"]["httpRequestsAdaptiveGroups"]
        out = {"retention_s": int(s.get("notOlderThan") or 0) or None,
               "max_query_duration_s": int(s.get("maxDuration") or 0) or None}
        _accepted_variant["retention"] = out
        return out
    except Exception:  # noqa: BLE001
        return {"retention_s": None, "max_query_duration_s": None}


def _aware(ts):
    """Postgres TIMESTAMP (naive) columns are UTC here; compare them as such."""
    if ts is not None and ts.tzinfo is None:
        return ts.replace(tzinfo=_dt.timezone.utc)
    return ts


def _parse_ts(dims):
    v = dims.get("datetimeHour")
    if v:
        return (_dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")),
                _dt.timedelta(hours=1))
    v = dims.get("date")
    if v:
        d = _dt.date.fromisoformat(str(v)[:10])
        return (_dt.datetime(d.year, d.month, d.day, tzinfo=_dt.timezone.utc),
                _dt.timedelta(days=1))
    return None, None


def summarize_edge(rows, windows, has_ip):
    """Raw Cloudflare groups -> {slug: {window: counts}}, plus zone-level facts.

    Pure. A bucket is in a window when any part of it overlaps the window, so a
    window never silently drops the hour it starts in. `has_ip` comes from the
    accepted QUERY, never from the rows: zero rows from an IP-grouped query is
    0 visitors, not "visitors unavailable".
    """
    per = {s: {w: {"_pairs": set(), "human_requests": 0,
                   "excluded_requests": {c: 0 for c in _CLASSES if c != "human"}}
               for w in windows} for s in INSTALL_PAGES}
    other_paths = {}
    sample_max = None
    all_audience_requests = 0
    self_requests = 0
    for row in rows or ():
        dims = row.get("dimensions") or {}
        n = int(row.get("count") or 0)
        si = ((row.get("avg") or {}).get("sampleInterval"))
        if si is not None:
            sample_max = max(sample_max or 0, float(si))
        slug = _slug_of(dims.get("clientRequestPath"))
        status = int(dims.get("edgeResponseStatus") or 0)
        if status not in (200, 304):
            continue  # a 308 from /install/x.html or a 404 is not a page view
        all_audience_requests += n
        if slug not in per:
            key = _norm_path(dims.get("clientRequestPath"))
            other_paths[key] = other_paths.get(key, 0) + n
            continue
        cls = classify_visit(dims.get("userAgent"), dims.get("clientASNDescription"))
        if cls == "self":
            self_requests += n
        ip = dims.get("clientIP")
        ts, span = _parse_ts(dims)
        for w, start in windows.items():
            if ts is not None and ts + span <= start:
                continue
            b = per[slug][w]
            if cls == "human":
                b["human_requests"] += n
                if ip:
                    b["_pairs"].add((ip, dims.get("userAgent") or ""))
            else:
                b["excluded_requests"][cls] += n
    for s in per:
        for w in per[s]:
            b = per[s][w]
            pairs = b.pop("_pairs")
            b["visitors"] = len(pairs) if has_ip else None
    facts = {
        "all_audience_requests": all_audience_requests,
        "self_requests_seen": self_requests,
        "visitor_unit": ("distinct (client IP, user-agent)" if has_ip
                         else "unavailable — the accepted variant has no clientIP; "
                              "human_requests is the only human figure"),
        "sample_interval_max": sample_max,
        "other_install_paths": sorted(other_paths.items(), key=lambda kv: -kv[1])[:10],
    }
    return per, facts


# ── ATTEMPTS: the ledger /keys/claim writes ───────────────────────────────
# One row per POST /api/v1/keys/claim made under an install-* client_name, on
# EVERY outcome. The reuse branches are the reason it exists: they hand back an
# existing key, so mcp_dev_keys never gains an install-* row for that visitor.
# Stores hashes, never the IP, the user-agent or the key. No Referer either:
# the zone worker injects `https://dchub.cloud` on every proxied request
# (tests/test_relay_open_provenance.py), so it cannot attest the page.
_DDL = (
    """CREATE TABLE IF NOT EXISTS install_mint_attempts (
        id              BIGSERIAL PRIMARY KEY,
        attempted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        client_name     TEXT NOT NULL,
        outcome         TEXT NOT NULL,
        key_client_name TEXT,
        key_hash        TEXT,
        ip_hash         TEXT,
        ua_hash         TEXT,
        ua_class        TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS install_mint_attempts_at_idx"
    " ON install_mint_attempts (attempted_at)",
    # The ledger's own birth certificate. A 30d attempt count from a ledger that
    # is three days old is a 3d count, and the only way to say so is to know
    # when it started. Written once, in the probe namespace, and never counted.
    """INSERT INTO install_mint_attempts (client_name, outcome)
       SELECT 'install-verify-ledger', 'ledger_started'
        WHERE NOT EXISTS (SELECT 1 FROM install_mint_attempts
                           WHERE outcome = 'ledger_started')""",
)
_ensured = {"ok": False}
_ensure_lock = threading.Lock()
OUTCOMES = ("minted", "reused", "reused_unused_cap", "failed")


def ensure_attempts_table():
    """DDL through db_utils.ddl_cursor — the pooled wrapper swallows CREATE TABLE."""
    if _ensured["ok"]:
        return True
    with _ensure_lock:
        if not _ensured["ok"]:
            from db_utils import ddl_cursor
            with ddl_cursor() as cur:
                for stmt in _DDL:
                    cur.execute(stmt)
            _ensured["ok"] = True
    return True


def record_install_attempt(client_name, outcome, *, api_key=None, key_client_name=None,
                           ip="", ua=""):
    """Append one attempt row. No-op for any client_name outside install-*.

    FAIL-OPEN and never raises: a claim must not break because its telemetry
    did. Returns True only when a row was written.
    """
    name = (client_name or "").strip()
    if not name.startswith(_INSTALL_STEM):
        return False
    try:
        ensure_attempts_table()
        dsn = _dsn()
        if not dsn:
            return False
        conn = psycopg2.connect(dsn, sslmode="require", connect_timeout=4)
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO install_mint_attempts
                         (client_name, outcome, key_client_name, key_hash,
                          ip_hash, ua_hash, ua_class)
                       VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                    (name[:80], (outcome or "unknown")[:40],
                     (key_client_name or None) and str(key_client_name)[:80],
                     _hash16(api_key) if api_key else None,
                     _hash16(ip) if ip else None,
                     _hash16(ua) if ua else None,
                     classify_visit(ua)),
                )
        finally:
            conn.close()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("install attempt not recorded (%s/%s): %s", name, outcome, e)
        return False


def _is_probe_attempt(client_name, ua_class):
    return (client_name or "").startswith(_PROBE_STEM) or ua_class == "self"


def summarize_attempts(rows, windows):
    """Attempt rows -> ({slug: {window: counts}}, probes). Pure.

    mint_attempts counts distinct (ip hash, ua hash, UTC day): the worker
    retries /keys/claim after a transient origin failure (IDEMPOTENT_POST_PATHS),
    so one press can be two rows, and a person pressing twice is one attempter.
    """
    per = {s: {w: {"_ids": set(), "attempt_requests": 0, "non_browser_requests": 0,
                   "by_outcome": {o: 0 for o in OUTCOMES}}
               for w in windows} for s in INSTALL_PAGES}
    probes = {"requests": 0, "clients": set()}
    for (client, at, outcome, ip_h, ua_h, ua_class, _kc) in rows or ():
        if outcome == "ledger_started":
            continue
        if _is_probe_attempt(client, ua_class):
            probes["requests"] += 1
            probes["clients"].add(client)
            continue
        slug = (client or "")[len(_INSTALL_STEM):] if (client or "").startswith(_INSTALL_STEM) else None
        if slug not in per:
            continue
        at = _aware(at)
        for w, start in windows.items():
            if at < start:
                continue
            b = per[slug][w]
            b["attempt_requests"] += 1
            if ua_class != "human":
                # Scripted POSTs that name an install page are not presses of
                # its button. Counted, never promoted to an attempt.
                b["non_browser_requests"] += 1
                continue
            b["_ids"].add((ip_h, ua_h, at.astimezone(_dt.timezone.utc).date()))
            if outcome in b["by_outcome"]:
                b["by_outcome"][outcome] += 1
    for s in per:
        for w in per[s]:
            per[s][w]["mint_attempts"] = len(per[s][w].pop("_ids"))
    probes["clients"] = sorted(probes["clients"])
    return per, probes


# ── MINTS: the install-stats population ───────────────────────────────────
_MINTS_SQL = f"""
    SELECT k.metadata->>'client_name' AS client, k.created_at
      FROM mcp_dev_keys k
     WHERE k.metadata->>'client_name' LIKE %s
       {_NOT_A_PROBE}
       AND k.created_at >= %s
"""
_ATTEMPTS_SQL = """
    SELECT client_name, attempted_at, outcome, ip_hash, ua_hash, ua_class,
           key_client_name
      FROM install_mint_attempts
     WHERE attempted_at >= %s
"""
_LEDGER_START_SQL = """
    SELECT MIN(attempted_at) FROM install_mint_attempts WHERE outcome = 'ledger_started'
"""


def summarize_mints(rows, windows):
    per = {s: {w: 0 for w in windows} for s in INSTALL_PAGES}
    for client, created_at in rows or ():
        slug = (client or "")[len(_INSTALL_STEM):]
        if slug not in per:
            continue
        for w, start in windows.items():
            if _aware(created_at) >= start:
                per[slug][w] += 1
    return per


def _db_reads(since):
    """-> (install_mint_rows, probe_mint_rows, control_mint_rows, attempt_rows,
    ledger_start, attempts_error). Mints and the control share _MINTS_SQL."""
    dsn = _dsn()
    if not dsn:
        raise RuntimeError("no DATABASE_URL")
    attempts_error = None
    try:
        ensure_attempts_table()
    except Exception as e:  # noqa: BLE001
        attempts_error = "attempt ledger unavailable: {}".format(str(e)[:160])
    conn = psycopg2.connect(dsn, sslmode="require", connect_timeout=8)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(_MINTS_SQL, (_INSTALL_PREFIX, _PROBE_PREFIX, since))
            mints = cur.fetchall()
            cur.execute(_MINTS_SQL, (_PROBE_PREFIX, _EXCLUDE_NOTHING, since))
            probe_mints = cur.fetchall()
            cur.execute(_MINTS_SQL, (_CONTROL_PREFIX, _PROBE_PREFIX, since))
            control_mints = cur.fetchall()
        attempts, ledger_start = [], None
        if attempts_error is None:
            try:
                with conn, conn.cursor() as cur:
                    cur.execute(_ATTEMPTS_SQL, (since,))
                    attempts = cur.fetchall()
                    cur.execute(_LEDGER_START_SQL)
                    ledger_start = (cur.fetchone() or [None])[0]
            except Exception as e:  # noqa: BLE001
                attempts_error = "attempt ledger read failed: {}".format(str(e)[:160])
    finally:
        conn.close()
    return mints, probe_mints, control_mints, attempts, ledger_start, attempts_error


# ── ASSEMBLY ──────────────────────────────────────────────────────────────
def _rate(num, den):
    """A rate exists only over a measured, non-zero denominator."""
    if den is None or num is None or den <= 0:
        return None
    return round(num / den, 4)


# The page is only named as the leak when zero presses from n visitors rules out
# a 10% press rate at 95% confidence: 1 - 0.05**(1/n) <= 0.10 needs n >= 29.
# Below that, "0 of n pressed" is what a healthy page produces too.
_PAGE_READING_MAX_UPPER = 0.10


def zero_rate_upper_95(n):
    """95% upper bound on a rate after 0 successes in n trials (exact binomial)."""
    if not n or n <= 0:
        return None
    return round(1 - 0.05 ** (1.0 / n), 4)


def verdict(visitors, attempts, mints, edge_live, attempts_full):
    """Name the first rung that is empty. Derived, never asserted."""
    if not edge_live or visitors is None:
        return {"rung": "unmeasured", "status": "hypothesis",
                "reading": "The visitor rung is not measured by this read, so no "
                           "rung can be named as the leak."}
    if visitors == 0:
        return {"rung": "distribution", "status": "observed",
                "reading": "No probe-excluded human visitor loaded any /install page "
                           "in this window. A page or CTA change cannot convert "
                           "traffic that does not arrive."}
    if not attempts_full:
        return {"rung": "attempts_not_covered", "status": "hypothesis",
                "reading": "Visitors arrived, but the attempt ledger does not cover "
                           "this whole window. Read the since_attempt_ledger window."}
    if attempts == 0:
        upper = zero_rate_upper_95(visitors)
        if upper > _PAGE_READING_MAX_UPPER:
            return {"rung": "distribution", "status": "observed",
                    "attempt_rate_upper_95": upper,
                    "reading": ("{} visitor(s) and no press. Too few to judge the page: "
                                "0 of {} is consistent with a press rate up to {:.0%}. "
                                "Traffic is the binding constraint.").format(
                                    visitors, visitors, upper)}
        return {"rung": "page", "status": "observed", "attempt_rate_upper_95": upper,
                "reading": "Human visitors arrived and none pressed mint."}
    if mints == 0:
        return {"rung": "claim", "status": "observed",
                "reading": "Visitors pressed mint and no new install-* key resulted "
                           "(reused, capped or failed — see by_outcome)."}
    return {"rung": "none", "status": "observed",
            "reading": "Every rung is non-zero; read the rates."}


def build_payload(now, windows, edge_per, edge_facts, edge_meta, edge_err,
                  att_per, att_probes, mint_per, probe_mints, control_mints,
                  ledger_start, attempts_err):
    edge_live = edge_err is None and edge_facts.get("all_audience_requests", 0) > 0
    # An attempt count is complete for a window only if the ledger already
    # existed when the window opened; before that, a zero is "not recorded".
    full = {w: (attempts_err is None and ledger_start is not None and ledger_start <= s)
            for w, s in windows.items()}
    pages, totals = [], {}
    for w in windows:
        totals[w] = {"visitors": 0 if edge_err is None else None,
                     "human_requests": 0 if edge_err is None else None,
                     "mint_attempts": 0 if attempts_err is None else None, "mints": 0}
    for slug in INSTALL_PAGES:
        rec = {"page": "/install/" + slug, "client_name": _INSTALL_STEM + slug}
        for w in windows:
            e = (edge_per.get(slug) or {}).get(w) or {}
            a = (att_per.get(slug) or {}).get(w) or {}
            v = e.get("visitors") if edge_err is None else None
            rec[w] = {
                "visitors": v,
                "human_requests": e.get("human_requests") if edge_err is None else None,
                "mint_attempts": a.get("mint_attempts") if attempts_err is None else None,
                "mints": mint_per[slug][w],
                "attempts_by_outcome": a.get("by_outcome"),
                "non_browser_attempt_requests": a.get("non_browser_requests"),
                "excluded_requests": e.get("excluded_requests"),
            }
            t = totals[w]
            if t["visitors"] is not None:
                t["visitors"] = None if v is None else t["visitors"] + v
            if t["human_requests"] is not None:
                t["human_requests"] += e.get("human_requests") or 0
            if t["mint_attempts"] is not None:
                t["mint_attempts"] += a.get("mint_attempts") or 0
            t["mints"] += mint_per[slug][w]
        pages.append(rec)
    for w, t in totals.items():
        t["label"] = _LABEL
        t["attempts_coverage"] = (
            "full" if full[w] else
            "none" if ledger_start is None else
            "partial — the attempt ledger began " + ledger_start.isoformat())
        # Rates only over a window every rung fully covers: before the ledger
        # existed, mints are counted and attempts are not, so attempt_to_mint
        # could exceed 1 and visitor_to_attempt would read a gap as a zero.
        t["visitor_to_attempt"] = _rate(t["mint_attempts"], t["visitors"]) if full[w] else None
        t["attempt_to_mint"] = _rate(t["mints"], t["mint_attempts"]) if full[w] else None
        t["verdict"] = verdict(t["visitors"], t["mint_attempts"], t["mints"],
                               edge_live, full[w])
    windows_out = {w: {"start": s.isoformat()} for w, s in windows.items()}
    control_n = len(control_mints or ())
    return {
        "ok": True,
        "generated_at": now.isoformat(),
        "label": _LABEL,
        "funnel": totals,
        "windows": windows_out,
        "pages": pages,
        "basis": {
            "visitors": ("distinct (client IP, user-agent) pairs that got a 200/304 "
                         "for /install/<slug> at the Cloudflare edge, human-classified "
                         "only. IPs are counted in-process and never published."),
            "mint_attempts": ("distinct (ip hash, ua hash, UTC day) that POSTed "
                              "/api/v1/keys/claim as install-<slug> with a browser "
                              "user-agent, on ANY outcome including a reused key "
                              "— install_mint_attempts, written by the claim handler."),
            "mints": ("keys minted as install-<slug>: the install-stats population "
                      "and exclusion clause, imported from routes/install_stats.py."),
            "excluded": {
                "probe_namespace": _PROBE_PREFIX + " (our end-to-end probes; see .probes)",
                "self": "our QA/monitor user-agents (Brain-v2-*, DC Hub QA, dchub-*, uptime monitors)",
                "agent": "declared bots, AI agents, SDKs and CLIs (routes/page_usage.py _AGENT_RE)",
                "automation": "headless / driven browsers (HeadlessChrome, Lighthouse, Playwright, …)",
                "datacenter": "browser user-agents from a cloud/hosting ASN (AWS, Azure, GCP, …)",
                "unknown": "empty or non-browser user-agents",
                "non_2xx": "redirects and errors — a 308 from /install/<slug>.html is not a view",
            },
            "not_excluded": ("our own manual browser visits: real browser UA, "
                             "residential IP — indistinguishable from a person."),
            "known_gap": ("a visitor who pastes the keyless connector URL never "
                          "touches /keys/claim; they are a visitor with no attempt, "
                          "which is the reading this surface exists to expose."),
        },
        "edge": dict(edge_meta or {}, error=edge_err, **(edge_facts or {})),
        "control": {
            "edge": {
                "why": ("A zero-visitor reading is only evidence if the same query "
                        "returns rows. all_audience_requests counts every audience "
                        "the identical query saw on these paths — including our own "
                        "probes (self_requests_seen)."),
                "all_audience_requests": edge_facts.get("all_audience_requests"),
                "instrument": "live" if edge_live else "unproven",
            },
            "mints": {
                "prefix": _CONTROL_PREFIX,
                "why": "The identical _MINTS_SQL against the known non-empty web-% prefix.",
                "minted_30d": control_n,
                "instrument": "live" if control_n > 0 else "unproven",
                "is_not_an_install_channel": True,
            },
        },
        "probes": {
            "why": "Our own traffic, counted by the same code that counts installs, never summed into them.",
            "mints_30d": len(probe_mints or ()),
            "attempt_requests_30d": att_probes.get("requests"),
            "attempt_clients": att_probes.get("clients"),
            "edge_self_requests": edge_facts.get("self_requests_seen"),
        },
        "attempt_ledger": {
            "table": "install_mint_attempts",
            "recorded_since": ledger_start.isoformat() if ledger_start else None,
            "error": attempts_err,
            "outcomes": list(OUTCOMES),
        },
        "evidence_status_claims": {
            "counts": {"status": "observed",
                       "note": "Direct counts by the definitions in .basis."},
            "verdict": {"status": "derived",
                        "note": ("Per window in .funnel[w].verdict, each with its own "
                                 "status: it names the first empty rung and is "
                                 "'hypothesis' whenever a rung it needs is unmeasured.")},
        },
    }


_cache = {"at": 0.0, "payload": None}
_cache_lock = threading.Lock()


@install_funnel_bp.route("/api/v1/ops/install-funnel", methods=["GET"])
def install_funnel():
    with _cache_lock:
        if _cache["payload"] is not None and time.time() - _cache["at"] < _CACHE_TTL_S:
            return _respond(_cache["payload"])
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
    since = now - _dt.timedelta(days=max(d for _, d in _WINDOW_DAYS))
    try:
        mints, probe_mints, control_mints, attempts, ledger_start, att_err = _db_reads(since)
    except Exception as e:  # noqa: BLE001
        log.warning("install_funnel db read failed: %s", e)
        return jsonify(ok=False, error="query_failed", detail=str(e)[:200]), 503
    windows = {w: now - _dt.timedelta(days=d) for w, d in _WINDOW_DAYS}
    ledger_start = _aware(ledger_start)
    if ledger_start is not None:
        windows["since_attempt_ledger"] = max(ledger_start, since)
    rows, meta, edge_err = _edge_rows(since, now)
    edge_per, facts = summarize_edge(rows, windows, bool(meta.get("has_client_ip")))
    att_per, att_probes = summarize_attempts(attempts, windows)
    payload = build_payload(now, windows, edge_per, facts, meta, edge_err,
                            att_per, att_probes, summarize_mints(mints, windows),
                            probe_mints, control_mints, ledger_start, att_err)
    with _cache_lock:
        _cache.update(at=time.time(), payload=payload)
    return _respond(payload)


def _respond(payload):
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


def register_install_funnel(app):
    try:
        app.register_blueprint(install_funnel_bp)
        log.info("install_funnel registered (/api/v1/ops/install-funnel)")
    except Exception as e:  # noqa: BLE001
        log.warning("install_funnel register failed: %s", e)
