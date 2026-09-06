"""MCP Ecosystem Board — where DC Hub actually stands among MCP servers.

★ THE TWO RANKS, AND WHY BOTH HAVE TO BE ON ONE PAGE.

We publish "#1 on Smithery for data-center / energy / grid" in mcp-server.json
and in integration docs, and measured 2026-09-06 it is TRUE — #1 of 195 on
"data center", #1 of 153 on "energy", #1 on eight more domain terms. It is also
the least useful true thing we know about ourselves, because a category rank is
won on EMBEDDING SIMILARITY to a query, and nobody searches a registry for
"data center" and installs the winner. The rank an agent's behaviour produces is
the use rank, and on the same registry, on the same day, DC Hub sits at 3,573
uses — behind at least 161 servers in a 546-server sample of an 11,729-server
corpus.

    category rank   #1 of 195         what our copy claims
    use rank        no better than    what the ecosystem actually does
                    #162 of 11,729

Neither number is wrong. Publishing only the first is how a server can be #1 on
ten terms and still be nobody's installed tool. This board carries both, next to
each other, permanently.

★ WHY THIS LIVES HERE AND NOT IN THE mcp-server REPO. Both ranks were already
measurable. `scripts/registry_monitor.py` in dchub-mcp-server has probed
`registry.smithery.ai/servers?q=<term>` since July and writes
`state/rank_status.json` — on a LaunchAgent, on one Mac, read by nothing. And
this repo's shell #42 lane 3 published, in code, "Smithery's public API exposes
no ranked search endpoint we have verified", which stopped being true weeks ago
and nothing here learned. Two repos, one measurement, zero surfaces. A number
that reaches no dashboard is not a measurement, it is a file.

★ SAMPLING CEILING, PUBLISHED RATHER THAN HIDDEN. The registry answers at most
500 rows per query (10 pages × 50; pageSize=100 caps at 5 pages) against a
totalCount of ~11,729, and honours no sort parameter — verified 2026-09-06,
`sort`/`sortBy`/`order` all returned the identical first page. So the field
below is a SAMPLE: the empty query's 500 rows, unioned with the domain-term
searches. Our use position is therefore a FLOOR ("no better than #N"), never a
rank, and `field_is_a_sample` says so in the payload. A server outside the
sample cannot appear here, and that is a limit of the source, not a finding.

★ UNREADABLE IS NEITHER ABSENT NOR RANKED. Every position is three-valued:
an integer (found at that position), the string ">N" (searched, not in the
first N), or None (the search did not answer). A term whose fetch failed must
never render as ">50" — that is the shape that let five registries read clean
for months (#3896).

★ WHY THE HTML BOARD READS A SNAPSHOT AND NEVER FETCHES. `/admin/*` gets the
worker's 15s DEFAULT timeout; only `/api/v1/admin/` carries the 120s ceiling.
/admin/registry-distribution proves the consequence — measured 2026-09-06 it
502s at the edge in 17.1s and returns 200 in 16.0s straight from the Railway
origin, so the board that shell renders is unreachable at the URL an owner
would type. This one computes under /api/v1/admin/ and renders from the stored
row, so the page an owner opens cannot time out.

Endpoints
  GET /api/v1/admin/mcp-ecosystem           latest stored snapshot (JSON, fast)
  GET /api/v1/admin/mcp-ecosystem/refresh   probe + store (120s edge budget)
  GET /admin/mcp-ecosystem                  text board off the stored row
Kill: MCP_ECOSYSTEM_BOARD_DISABLE=1
"""
from __future__ import annotations

import json
import logging
import os

from flask import Blueprint, Response, jsonify, request

from utc_clock import utc_iso_z

logger = logging.getLogger(__name__)

mcp_ecosystem_board_bp = Blueprint("mcp_ecosystem_board", __name__)

SMITHERY_API = "https://registry.smithery.ai/servers"
OUR_QUALIFIED_NAME = "azmartone67/dchub"
_UA = {"User-Agent": "dchub-ecosystem-board/1.0"}
_TIMEOUT = 20

# The terms our own copy sells on. `data center` and `energy` lead because they
# are the two the owner asks about by name; the rest are here so a slip on a
# term we quietly depend on cannot be invisible the way `utility` was (it fell
# from #1 to off-page while being in no list at all — registry_monitor, 09-01).
DOMAIN_TERMS = (
    "data center", "energy", "datacenter", "data centers", "power grid",
    "power", "grid", "electricity", "capacity", "colocation",
    "fiber", "interconnection queue", "utility", "natural gas",
    "hyperscale", "site selection",
)

# Pages of the empty query. 10 × 50 is the ceiling the API enforces; asking for
# more returns the same 500 rows, so this is the whole readable field.
_FIELD_PAGES = 10
_FIELD_PAGE_SIZE = 50
_SEARCH_PAGE_SIZE = 50
TOP_N = 50          # the "top 20-50" the board maintains


def _disabled() -> bool:
    return (os.environ.get("MCP_ECOSYSTEM_BOARD_DISABLE") or "0") == "1"


def _admin_ok() -> bool:
    want = ((os.environ.get("DCHUB_ADMIN_KEY")
             or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(want) and got == want


def _db_url():
    return (os.environ.get("NEON_DATABASE_URL")
            or os.environ.get("DATABASE_URL"))


def _conn():
    import psycopg2
    return psycopg2.connect(_db_url(), connect_timeout=8)


# ── the source ───────────────────────────────────────────────────────
def _search(q: str, page: int = 1, page_size: int = _SEARCH_PAGE_SIZE,
            fetch=None):
    """One registry page. Returns (servers, total, error).

    `error` is a STRING when the fetch or parse failed and None when it worked,
    so every caller can tell "the registry says we are not here" from "the
    registry did not answer". Collapsing those is the whole failure this module
    is written against.
    """
    fetch = fetch or _http_get_json
    try:
        payload, err = fetch(SMITHERY_API, {"q": q, "pageSize": str(page_size),
                                            "page": str(page)})
    except Exception as e:  # noqa: BLE001
        return [], None, str(e)[:120]
    if err or not isinstance(payload, dict):
        return [], None, (err or "response was not an object")
    servers = payload.get("servers")
    if not isinstance(servers, list):
        return [], None, "response carried no server list"
    total = (payload.get("pagination") or {}).get("totalCount")
    return servers, total, None


def _http_get_json(url: str, params: dict):
    """requests, never urllib — house rule (regression_lint
    urllib-request-on-railway). Returns (payload, error_string_or_None)."""
    import requests
    try:
        r = requests.get(url, params=params, headers=_UA, timeout=_TIMEOUT)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        return r.json(), None
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:120]


def _is_us(qualified_name: str) -> bool:
    return "dchub" in (qualified_name or "").lower()


# ── lane A — category rank ───────────────────────────────────────────
def category_ranks(terms=DOMAIN_TERMS, fetch=None) -> list:
    """Our position per term. position is int | ">N" | None (unreadable)."""
    rows = []
    for term in terms:
        servers, total, err = _search(term, page_size=_SEARCH_PAGE_SIZE,
                                      fetch=fetch)
        if err:
            rows.append({"term": term, "position": None, "of": None,
                         "leader": None, "readable": False, "note": err})
            continue
        leader = (servers[0].get("qualifiedName") if servers else None)
        pos = None
        for i, s in enumerate(servers, 1):
            if _is_us(s.get("qualifiedName") or ""):
                pos = i
                break
        rows.append({
            "term": term,
            "position": pos if pos else f">{_SEARCH_PAGE_SIZE}",
            "of": total,
            "leader": leader,
            "readable": True,
            "held": pos == 1,
            "note": None,
        })
    return rows


# ── lane B — the field, and our floor in it ──────────────────────────
def ecosystem_field(terms=DOMAIN_TERMS, fetch=None) -> dict:
    """Top servers by useCount over the readable sample, plus our floor.

    Returns `our_use_rank_floor` — "no better than #N" — never a rank. With a
    500-row ceiling over 11,729 servers, any server outside the sample could
    sit above us, so the true rank can only be WORSE than this number. Calling
    a floor a rank is the over-claim this whole file exists to avoid.
    """
    sample: dict = {}
    errors = []

    for page in range(1, _FIELD_PAGES + 1):
        servers, _total, err = _search("", page=page,
                                       page_size=_FIELD_PAGE_SIZE, fetch=fetch)
        if err:
            errors.append(f"field page {page}: {err}")
            continue
        for s in servers:
            qn = s.get("qualifiedName")
            if qn:
                sample[qn] = s

    # Widen with the domain searches — these are the neighbours a buyer of ours
    # would meet, and they are cheap because the ranks lane fetches them anyway.
    for term in terms:
        servers, _total, err = _search(term, page_size=_SEARCH_PAGE_SIZE,
                                       fetch=fetch)
        if err:
            errors.append(f"field term {term!r}: {err}")
            continue
        for s in servers:
            qn = s.get("qualifiedName")
            if qn and qn not in sample:
                sample[qn] = s

    if not sample:
        return {"readable": False, "sample_size": 0, "corpus_total": None,
                "top": [], "our_use_count": None, "our_use_rank_floor": None,
                "field_is_a_sample": True, "errors": errors}

    _servers, corpus_total, _err = _search("", page_size=1, fetch=fetch)

    def _uses(s) -> int:
        v = s.get("useCount")
        return v if isinstance(v, int) else 0

    ours = next((s for qn, s in sample.items() if _is_us(qn)), None)
    our_uses = _uses(ours) if ours else None

    ranked = sorted(sample.values(), key=lambda s: -_uses(s))
    top = [{"rank": i,
            "qualified_name": s.get("qualifiedName"),
            "use_count": _uses(s),
            "remote": bool(s.get("remote")),
            "verified": bool(s.get("verified")),
            "is_us": _is_us(s.get("qualifiedName") or "")}
           for i, s in enumerate(ranked[:TOP_N], 1)]

    floor = None
    if our_uses is not None:
        floor = sum(1 for s in sample.values() if _uses(s) > our_uses) + 1

    return {
        "readable": True,
        "sample_size": len(sample),
        "corpus_total": corpus_total,
        "top": top,
        "our_use_count": our_uses,
        "our_use_rank_floor": floor,
        # Never let a reader mistake the sample for a census.
        "field_is_a_sample": True,
        "basis": (f"top {TOP_N} by useCount over {len(sample)} servers the "
                  f"registry returned (empty query, {_FIELD_PAGES} pages of "
                  f"{_FIELD_PAGE_SIZE} = its 500-row ceiling, unioned with the "
                  f"domain terms) against a corpus of {corpus_total}. The API "
                  f"honours no sort parameter, so this is a SAMPLE and our "
                  f"position is a FLOOR, not a rank."),
        "errors": errors,
    }


# ── lane C — onboarding lands, or it did not happen ──────────────────
_LISTING_PR_QUERY = "author:azmartone67 type:pr -user:azmartone67"
GITHUB_SEARCH = "https://api.github.com/search/issues"


def listing_pr_ledger(fetch=None) -> dict:
    """Upstream listing PRs by outcome. SUBMITTED IS NOT LISTED.

    The registry loop reports success when a PR opens. Measured 2026-09-06 that
    reporting covered 43 upstream PRs of which 5 ever merged — so the metric the
    loop watches moved 43 times while the thing it stands for happened 5 times.
    Fail-open to UNMEASURED: GitHub unreachable yields readable=False, never a
    zero that would read as "nothing outstanding".
    """
    fetch = fetch or _http_get_json
    payload, err = fetch(GITHUB_SEARCH, {"q": _LISTING_PR_QUERY,
                                         "per_page": "100"})
    if err or not isinstance(payload, dict):
        return {"readable": False, "note": err or "unexpected payload"}
    items = payload.get("items")
    if not isinstance(items, list):
        return {"readable": False, "note": "no items array"}
    merged = open_ = closed_unmerged = 0
    for it in items:
        pr = it.get("pull_request") or {}
        if pr.get("merged_at"):
            merged += 1
        elif it.get("state") == "open":
            open_ += 1
        else:
            closed_unmerged += 1
    total = merged + open_ + closed_unmerged
    return {"readable": True, "submitted": total, "merged": merged,
            "still_open": open_, "closed_unmerged": closed_unmerged,
            "land_rate_pct": round(100.0 * merged / total, 1) if total else None}


# ── snapshot ─────────────────────────────────────────────────────────
def _memoized(fetch):
    """One snapshot must not fetch the same page twice.

    The ranks lane and the field lane both read the domain-term searches; run
    naively that is 16 duplicate round-trips per snapshot. Scoped to a single
    build so a refresh is always a fresh read — a cache that outlived the run
    would turn "as_of" into a lie about when the numbers were taken.
    """
    inner = fetch or _http_get_json
    seen: dict = {}

    def cached(url, params):
        key = (url, tuple(sorted((params or {}).items())))
        if key not in seen:
            seen[key] = inner(url, params)
        return seen[key]
    return cached


def build_snapshot(fetch=None) -> dict:
    fetch = _memoized(fetch)
    ranks = category_ranks(fetch=fetch)
    field = ecosystem_field(fetch=fetch)
    ledger = listing_pr_ledger(fetch=fetch)
    held = [r["term"] for r in ranks if r.get("held")]
    readable = [r for r in ranks if r.get("readable")]
    return {
        "as_of": utc_iso_z(),
        "category": {
            "terms_probed": len(ranks),
            "terms_readable": len(readable),
            "terms_held_at_1": len(held),
            "held": held,
            "rows": ranks,
        },
        "field": field,
        "listing_prs": ledger,
    }


def _ensure_table(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mcp_ecosystem_snapshot (
            id          BIGSERIAL PRIMARY KEY,
            captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            payload     JSONB NOT NULL
        )
    """)


def store_snapshot(snap: dict) -> bool:
    """Persist one run. Best-effort: a board that cannot store must still
    return the numbers it just measured."""
    from util.json_column import json_for_column
    try:
        conn = _conn()
    except Exception as e:  # noqa: BLE001
        logger.warning("ecosystem snapshot store: no db (%s)", str(e)[:120])
        return False
    try:
        with conn, conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute("INSERT INTO mcp_ecosystem_snapshot (payload) "
                        "VALUES (%s)", (json_for_column(snap),))
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("ecosystem snapshot store failed: %s", str(e)[:160])
        return False
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def latest_snapshot() -> dict | None:
    try:
        conn = _conn()
    except Exception as e:  # noqa: BLE001
        logger.warning("ecosystem snapshot read: no db (%s)", str(e)[:120])
        return None
    try:
        with conn, conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute("SELECT payload, captured_at FROM mcp_ecosystem_snapshot "
                        "ORDER BY captured_at DESC LIMIT 1")
            row = cur.fetchone()
        if not row:
            return None
        payload = row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(payload, dict):
            payload.setdefault("as_of", str(row[1]))
        return payload
    except Exception as e:  # noqa: BLE001
        logger.warning("ecosystem snapshot read failed: %s", str(e)[:160])
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


# ── rendering ────────────────────────────────────────────────────────
def render_board(snap: dict | None) -> str:
    if not snap:
        return ("mcp-ecosystem: NO SNAPSHOT STORED YET.\n"
                "Run GET /api/v1/admin/mcp-ecosystem/refresh (120s edge budget) "
                "— this page deliberately never fetches, because /admin/* is "
                "capped at the worker's 15s DEFAULT.\n")
    L = []
    cat = snap.get("category") or {}
    field = snap.get("field") or {}
    led = snap.get("listing_prs") or {}

    floor = field.get("our_use_rank_floor")
    L.append(f"MCP ECOSYSTEM BOARD — as of {snap.get('as_of')}")
    L.append("")
    L.append(f"  category rank   #1 on {cat.get('terms_held_at_1')} of "
             f"{cat.get('terms_readable')} readable domain term(s)")
    L.append(f"  use rank        {('no better than #%d of %s' % (floor, field.get('corpus_total'))) if floor else 'UNMEASURED'}")
    L.append("  Both are true. The first is what our copy claims; the second is "
             "what the ecosystem does.")
    L.append("")

    L.append("── WHERE WE RANK (Smithery search position) ──")
    for r in cat.get("rows") or []:
        if not r.get("readable"):
            L.append(f"   ??  {r['term']:<24} UNREADABLE ({r.get('note')}) "
                     f"— not the same as absent")
            continue
        pos = r.get("position")
        mark = "#1 " if r.get("held") else "-- "
        lead = "" if r.get("held") else f"   leader: {r.get('leader')}"
        shown = f"#{pos}" if isinstance(pos, int) else str(pos)
        L.append(f"   {mark} {r['term']:<24} {shown:>6} of {r.get('of')}{lead}")
    L.append("")

    L.append(f"── THE FIELD (top {TOP_N} by useCount) ──")
    if not field.get("readable"):
        L.append("   UNREADABLE — the registry did not answer. Not a zero.")
    else:
        L.append(f"   basis: {field.get('basis')}")
        L.append(f"   DC Hub: {field.get('our_use_count')} uses"
                 f"{' — no better than #%d' % floor if floor else ''}")
        for row in field.get("top") or []:
            star = " <- US" if row.get("is_us") else ""
            L.append(f"   {row['rank']:>3}. {row['qualified_name']:<44} "
                     f"{row['use_count']:>9,}{star}")
    L.append("")

    L.append("── DID ONBOARDING LAND? (upstream listing PRs) ──")
    if not led.get("readable"):
        L.append(f"   UNMEASURED ({led.get('note')}) — never read as zero.")
    else:
        L.append(f"   submitted {led.get('submitted')}   merged "
                 f"{led.get('merged')}   still open {led.get('still_open')}   "
                 f"closed unmerged {led.get('closed_unmerged')}")
        L.append(f"   land rate {led.get('land_rate_pct')}% — a submitted PR is "
                 f"not a listing, and the loop reports on the submission.")
    for e in (field.get("errors") or [])[:6]:
        L.append(f"   note: {e}")
    return "\n".join(L) + "\n"


# ── endpoints ────────────────────────────────────────────────────────
@mcp_ecosystem_board_bp.route("/api/v1/admin/mcp-ecosystem", methods=["GET"])
def mcp_ecosystem_json():
    if _disabled():
        return jsonify({"disabled": True}), 200
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    snap = latest_snapshot()
    if not snap:
        return jsonify({"snapshot": None,
                        "hint": "GET /api/v1/admin/mcp-ecosystem/refresh"}), 200
    return jsonify(snap)


@mcp_ecosystem_board_bp.route("/api/v1/admin/mcp-ecosystem/refresh",
                              methods=["GET", "POST"])
def mcp_ecosystem_refresh():
    if _disabled():
        return jsonify({"disabled": True}), 200
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    snap = build_snapshot()
    snap["stored"] = store_snapshot(snap)
    return jsonify(snap)


@mcp_ecosystem_board_bp.route("/admin/mcp-ecosystem", methods=["GET"])
def mcp_ecosystem_board():
    if _disabled():
        return Response("board disabled", status=404, mimetype="text/plain")
    if not _admin_ok():
        return Response("unauthorized", status=401, mimetype="text/plain")
    return Response(render_board(latest_snapshot()), mimetype="text/plain")


def register_mcp_ecosystem_board(app) -> None:
    app.register_blueprint(mcp_ecosystem_board_bp)
