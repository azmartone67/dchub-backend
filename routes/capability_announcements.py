"""capability_announcements.py — brain-staged, OWNER-APPROVED platform announcements
served on /api/v1/whats-new. Pure stdlib (no flask, no psycopg2) on purpose.

WHY THIS EXISTS
---------------
The "New platform capabilities" cards on https://dchub.cloud/whats-new were
hardcoded HTML with FROZEN numbers. They went stale exactly the way frozen
numbers always do: the page still said "36 grids" and "tool #73" long after the
live scoreboard and the live tools/list had moved past both. This module makes
the same cards data, with every number resolved AT SERVE TIME from a named live
field, so a card can go out of date only if the underlying field does.

★ HOW APPROVAL WORKS — read before adding anything here
-------------------------------------------------------
Approval is a MERGED PULL REQUEST against this file. There is no admin button
and no auto-publish path.

  1. The brain drafts a candidate with stage_announcement_pr() below. That
     helper appends an entry with status=STATUS_PENDING to ANNOUNCEMENTS and
     opens a DRAFT PR via routes/drift_pr_writer.py (draft-only, repo
     allowlisted, and dark unless DCHUB_DRIFT_PR=1).
  2. A PENDING entry is NEVER served. capability_announcement_cards() renders
     entries with status == STATUS_APPROVED and nothing else. A draft that
     reaches main un-approved is still invisible to the public payload.
  3. The owner approves by editing that one word to STATUS_APPROVED in the PR
     and merging it. The merge commit IS the approval record — git blame names
     the approver and the PR carries the review.

So the failure mode is fail-CLOSED: the worst a broken brain can do is stage a
draft nobody sees. `approved_pr` is documentation of which PR carried the
approval; it does not gate rendering (a card that renders only when TWO fields
agree fails silently when one is forgotten, which is worse than a stale field).

★ EVERY CARD CARRIES ITS OWN VERIFICATION
-----------------------------------------
An entry's `body` is a TEMPLATE. Numbers appear only as {placeholders} that
name a key of _RESOLVERS, and each resolver declares:
    sql             the live query (run on the CALLER's cursor — never a nested
                    connection; the nested-connect-under-the-pooler trap in
                    brain_capability_radar starved every evergreen lead once)
    basis           the human-readable method string published with the card
    verify_endpoint the public endpoint an agent can call to check the figure
    verify_field    the exact field name in that endpoint's response
A card that cannot resolve every figure it names DOES NOT RENDER. It is
reported in `withheld` with a reason. UNMEASURED emits null + a reason, never 0,
and never a number frozen at authoring time.

Floors round DOWN ({field|floor}) and can therefore never exceed live reality.
No operational-MW, no pipeline-GW and no dollar aggregate may appear in an
entry — stage_announcement_pr() refuses copy that contains one, and
tests/test_whats_new_capability_announcements.py pins the same rule.
"""
from __future__ import annotations

import datetime
import logging
import re

logger = logging.getLogger("capability_announcements")

# ── Approval states. The ONLY value that reaches the public payload. ──────────
STATUS_APPROVED = "approved"   # owner merged the PR that set this → served
STATUS_PENDING = "pending"     # brain-staged draft → NEVER served
_STATUSES = (STATUS_APPROVED, STATUS_PENDING)

# Copy that must never appear in an announcement (house rule: no operational-MW
# or pipeline-GW figure anywhere, no dollar aggregate off deals.value).
_BANNED_COPY = re.compile(r"(?i)(\bMW\b|\bGW\b|\bmegawatt|\bgigawatt|\$)")

# A frozen number in the copy is the exact defect this module exists to kill
# ("36 grids", "73 tools", "tool #73"). Two or more consecutive digits in a
# title/body means somebody typed a count instead of binding one.
_FROZEN_NUMBER = re.compile(r"\d{2,}")

# {field} → live value, {field|floor} → floored "X,000+" claim.
_PLACEHOLDER = re.compile(r"\{([a-z_]+)(\|floor)?\}")


# ── Live figure resolvers. DB-only, single statement, no literal % (a literal
# percent-sign in a psycopg2 query string is a live 500). Each mirrors a field
# ALREADY published by /api/v1/stats/canonical so the page, the API and any
# agent that checks us all read the same number from the same query. ──────────
_RESOLVERS = {
    # ★ facilities_distinct is the citeable facility count. `facilities_verified`
    # is the same NAME over two DIFFERENT queries across our own endpoints
    # (is_duplicate=0 on /api/v1/whats-new vs duplicate_of_id IS NULL on
    # /api/v1/stats/canonical), so announcements must not cite it.
    # routes/facilities_by_dims.py: "New consumers should read facilities_distinct".
    "facilities_distinct": {
        "sql": ("SELECT COUNT(DISTINCT canonical_slug) FROM discovered_facilities "
                "WHERE canonical_slug IS NOT NULL"),
        "basis": ("distinct facilities after cross-source entity resolution "
                  "(COUNT(DISTINCT canonical_slug) over discovered_facilities)"),
        "verify_endpoint": "/api/v1/stats/canonical",
        "verify_field": "stats.facilities_distinct",
        "floor_step": 1000,
    },
    "facilities_records": {
        "sql": "SELECT COUNT(*) FROM discovered_facilities",
        "basis": "raw tracked discovery rows (COUNT(*) over discovered_facilities)",
        "verify_endpoint": "/api/v1/stats/canonical",
        "verify_field": "stats.facilities_records",
        "floor_step": 1000,
    },
    "countries_covered": {
        "sql": ("SELECT COUNT(DISTINCT country) FROM facilities "
                "WHERE country IS NOT NULL AND country <> ''"),
        "basis": "distinct non-empty country values over facilities",
        "verify_endpoint": "/api/v1/stats/canonical",
        "verify_field": "stats.countries_covered",
        "floor_step": 10,
    },
    # Byte-for-byte canonical_stats.py's markets query (COUNT(DISTINCT
    # market_name) minus the three aggregate regions) — NOT COUNT(*) of
    # market_power_scores rows, which is a different, larger number.
    "dcpi_markets_scored": {
        "sql": ("SELECT COUNT(DISTINCT market_name) FROM market_power_scores "
                "WHERE COALESCE(published, true) = true "
                "AND market_slug NOT IN ('pacific-nw-rural','rural-spp','upper-michigan')"),
        "basis": ("published DCPI markets (COUNT(DISTINCT market_name) over "
                  "market_power_scores, excluding the three aggregate regions)"),
        "verify_endpoint": "/api/v1/stats/canonical",
        "verify_field": "stats.dcpi_markets_scored",
        "floor_step": 100,
    },
}


# ── The registry. ONE entry per announcement. Numbers live in {placeholders}. ─
#
# Seed entries: these three mirror cards ALREADY LIVE on /whats-new (shipped
# 2026-07-13 as hardcoded HTML). The copy is the copy the owner already
# published; the only change is that the counts now bind live instead of being
# frozen — which is why they carry status=STATUS_APPROVED in the PR that adds
# this file. Anything the brain stages later arrives as STATUS_PENDING and
# stays invisible until the owner flips it in a PR.
#
# NOT seeded, deliberately: the "36 grids" scoreboard card and the "73 tools"
# catalogue card. Neither count has a live BACKEND field — zones_ranked comes
# from the Node MCP server and the tool count from tools/list on the public
# gate, and this module refuses to do HTTP egress inside a public request. They
# stay hardcoded in the page until a keyless backend projection exists; adding
# them here without a resolver would only produce a permanently withheld card.
ANNOUNCEMENTS = [
    # ── 2026-07-29 ────────────────────────────────────────────────────
    # These three were staged into data/platform_updates.json first, which feeds
    # /api/v1/platform-updates — NOT the `platform` block on /api/v1/whats-new
    # that this module serves. Two announcement systems shipped the same day and
    # the cards went into the one the page does not read, so they were live at
    # one endpoint and invisible on the page they were written for. Registered
    # here so they actually reach /whats-new. Consolidating the two stores is
    # tracked separately; duplicating three entries is the smaller wrong.
    {
        "key": "dcpi_methodology_published",
        "status": STATUS_APPROVED,
        "approved_pr": "owner-approved in the PR that adds this entry",
        "shipped_at": "2026-07-29",
        "tag": "Method",
        "title": "The DCPI method is now machine-readable",
        "body": ("Every indicator weight, every ceiling, every verdict band and the full "
                 "revision history are served as JSON at /api/v1/dcpi/methodology, emitted "
                 "from the same constants the scorer imports — so the published method and "
                 "the running index cannot drift into describing different formulas. The "
                 "known limitations ship in the same payload. Covers "
                 "{dcpi_markets_scored} scored markets."),
        "cta_href": "/api/v1/dcpi/methodology",
        "cta_label": "Read the method",
    },
    {
        "key": "cross_layer_site_discovery",
        "status": STATUS_APPROVED,
        "approved_pr": "owner-approved in the PR that adds this entry",
        "shipped_at": "2026-07-29",
        "tag": "Site discovery",
        "title": "Cross-layer site discovery produces candidates, not rankings",
        "body": ("GET /api/v1/sites/cross-layer builds a candidate set out of the physical "
                 "layers themselves — the substation layer is the search space, with fiber "
                 "coverage, carrier presence and market context from {dcpi_markets_scored} "
                 "scored markets attached to each anchor — instead of re-ranking a shortlist "
                 "the caller already had. It also declares what it refuses to answer: "
                 "constraint_coverage names measured power headroom as absent from every "
                 "layer, with the reason, so an agent can see the hole rather than infer a "
                 "figure nobody measured."),
        "cta_href": "/api/v1/sites/cross-layer?lat=39.0438&lon=-77.4874&radius_km=25",
        "cta_label": "See it over Loudoun",
    },
    # grid_scoreboard_honest_counts is DELIBERATELY NOT registered here. This
    # registry requires every entry to bind a live numeric resolver, and no
    # keyless SQL source publishes a grid-zone count — the scoreboard is
    # assembled in the MCP layer from live upstream calls, not from a table.
    # Relaxing that contract for one numberless card would cost more than the
    # card is worth, and binding an unrelated resolver (countries_covered is over
    # `facilities`, not grid zones) would be a non-sequitur dressed as evidence.
    # It is published at /api/v1/platform-updates, whose store permits
    # numberless entries by design.
    {
        "key": "provenance_envelope",
        "status": STATUS_APPROVED,
        "approved_pr": "owner-approved in the PR that added routes/capability_announcements.py",
        "shipped_at": "2026-07-11",
        "tag": "Trust",
        "title": "Provenance Envelope v1 on facility search",
        "body": ("search_facilities returns a verification tier per record — verified vs "
                 "tracked — plus source, a CC-BY-4.0 license and a citation-URL template. "
                 "Live right now: {facilities_distinct} distinct facilities inside a "
                 "{facilities_records}-record tracked frontier, across {countries_covered} "
                 "countries."),
        "cta_href": "/api/v1/stats/canonical",
        "cta_label": "See the counts",
    },
    {
        "key": "agent_memory",
        "status": STATUS_APPROVED,
        "approved_pr": "owner-approved in the PR that added routes/capability_announcements.py",
        "shipped_at": "2026-07-13",
        "tag": "Memory",
        "title": "Your agent remembers your shortlist",
        "body": ("save_site builds a durable list of candidate sites; next session "
                 "get_changes returns per-site deltas — verdict flips, DCPI score moves, "
                 "new nearby facilities — not just global market movers. Any of the "
                 "{facilities_distinct} distinct facilities in the live index can be saved."),
        "cta_href": "/connect#start",
        "cta_label": "Connect your AI",
    },
    {
        "key": "error_envelope",
        "status": STATUS_APPROVED,
        "approved_pr": "owner-approved in the PR that added routes/capability_announcements.py",
        "shipped_at": "2026-07-13",
        "tag": "Reliability",
        "title": "A machine-readable error contract",
        "body": ("Every error carries error_version:1 — a severity class, a deterministic "
                 "recovery hint and a server-computed suggested_params, so an agent "
                 "auto-corrects a bad parameter and re-runs without dropping context. It "
                 "covers every tool across the {dcpi_markets_scored} scored DCPI markets "
                 "and {facilities_distinct} distinct facilities."),
        "cta_href": "/docs/error-codes",
        "cta_label": "Error taxonomy",
    },
    # <<< brain-staged announcements are appended above this line >>>
]


# The append anchor for stage_announcement_pr(). drift_pr_writer applies an edit
# ONLY when `find` occurs EXACTLY ONCE in the file — so this constant is built
# from two fragments on purpose: writing the marker as one literal here would
# make it appear TWICE in this file (here and in ANNOUNCEMENTS above) and every
# staged PR would be silently skipped as "find occurs 2x".
_APPEND_MARKER = "    # <<< brain-staged announcements are " + "appended above this line >>>"


def _floor(n: int, step: int) -> str:
    """Round DOWN to a clean 'X,000+' floor so a published claim can never
    exceed live reality. Returns None when the floor would collapse to zero —
    an honest '0+' is not a claim worth publishing."""
    floored = (int(n) // int(step)) * int(step)
    if floored <= 0:
        return None
    return f"{floored:,}+"


def _resolve(cur, field: str, memo: dict) -> dict:
    """Resolve ONE live figure on the caller's cursor. Never raises.

    Returns {"ok": True, "value": int, ...spec} or {"ok": False, "reason": str}.
    A resolver failure is per-figure: it withholds the cards that name that
    field and leaves every other card alone."""
    if field in memo:
        return memo[field]
    spec = _RESOLVERS.get(field)
    if not spec:
        out = {"ok": False, "reason": f"no resolver registered for '{field}'"}
        memo[field] = out
        return out
    try:
        cur.execute(spec["sql"])
        row = cur.fetchone()
        value = int((row or [0])[0] or 0)
    except Exception as e:                       # unmeasured → null + reason, never 0
        out = {"ok": False,
               "reason": f"'{field}' unmeasured: {type(e).__name__}: {str(e)[:100]}"}
        memo[field] = out
        return out
    if value <= 0:
        out = {"ok": False, "reason": f"'{field}' resolved to a non-positive count"}
        memo[field] = out
        return out
    out = {"ok": True, "value": value, "field": field,
           "basis": spec["basis"],
           "verify_endpoint": spec["verify_endpoint"],
           "verify_field": spec["verify_field"],
           "floor_step": spec["floor_step"]}
    memo[field] = out
    return out


def _render(entry: dict, cur, memo: dict) -> tuple:
    """Render one entry's body against LIVE figures.

    Returns (card, None) on success or (None, reason) when the card must be
    withheld. Every {placeholder} must resolve — a card that cannot cite its
    live figure does not render."""
    body = entry.get("body") or ""
    fields = [(m.group(1), bool(m.group(2))) for m in _PLACEHOLDER.finditer(body)]
    if not fields:
        return None, "entry names no live figure (a card must cite one)"
    figures, rendered = [], body
    for field, is_floor in fields:
        got = _resolve(cur, field, memo)
        if not got.get("ok"):
            return None, got.get("reason") or f"'{field}' unresolved"
        if is_floor:
            text = _floor(got["value"], got["floor_step"])
            if not text:
                return None, f"'{field}' floors to zero — refusing to publish"
            token = "{" + field + "|floor}"
        else:
            text = f"{got['value']:,}"
            token = "{" + field + "}"
        rendered = rendered.replace(token, text)
        if not any(f["field"] == field for f in figures):
            figures.append({"field": field, "value": got["value"], "rendered": text,
                            "basis": got["basis"],
                            "verify_endpoint": got["verify_endpoint"],
                            "verify_field": got["verify_field"],
                            "floored": is_floor})
    card = {
        "key": entry.get("key"),
        "tag": entry.get("tag"),
        "title": entry.get("title"),
        "body": rendered,
        "cta_href": entry.get("cta_href"),
        "cta_label": entry.get("cta_label"),
        "shipped_at": entry.get("shipped_at"),
        # What an agent can call to check every number in this card itself.
        "verify": sorted({f["verify_endpoint"] for f in figures}),
        "figures": figures,
    }
    return card, None


def capability_announcement_cards(cur) -> dict:
    """READ-ONLY. Render every APPROVED announcement against live figures.

    Takes the CALLER's open cursor — no connection of its own, so it can never
    open a nested connection under the pooler. Never raises.

    {"ok": True, "cards": [...], "withheld": [{key, reason}], "as_of": iso,
     "approved_count": n, "staged_count": n}
    or {"ok": False, "reason": str} when the source itself is unavailable — the
    caller must then publish null + that reason, NOT an empty list (an empty
    list reads as "nothing shipped", which is a different and false claim)."""
    if cur is None:
        return {"ok": False, "reason": "no database cursor available for live figures"}
    try:
        approved = [e for e in ANNOUNCEMENTS if e.get("status") == STATUS_APPROVED]
        staged = [e for e in ANNOUNCEMENTS if e.get("status") != STATUS_APPROVED]
        cards, withheld, memo = [], [], {}
        for entry in approved:
            try:
                card, reason = _render(entry, cur, memo)
            except Exception as e:               # one bad entry never kills the block
                card, reason = None, f"render failed: {type(e).__name__}: {str(e)[:100]}"
            if card:
                cards.append(card)
            else:
                withheld.append({"key": entry.get("key"), "reason": reason})
                logger.info("[capability-announcements] withheld %s: %s",
                            entry.get("key"), reason)
        return {"ok": True, "cards": cards, "withheld": withheld,
                "approved_count": len(approved), "staged_count": len(staged),
                "as_of": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    except Exception as e:
        return {"ok": False,
                "reason": f"announcement registry unavailable: {type(e).__name__}: {str(e)[:100]}"}


def validate_copy(title: str, body: str) -> str | None:
    """Return a refusal reason if this copy may not be published, else None.

    Enforced on every brain-staged draft AND pinned by the guard test, so the
    two can never drift apart."""
    for label, text in (("title", title or ""), ("body", body or "")):
        if not text.strip():
            return f"{label} is empty"
        if _BANNED_COPY.search(text):
            return f"{label} contains a banned MW/GW/dollar figure"
        if _FROZEN_NUMBER.search(_PLACEHOLDER.sub("", text)):
            return (f"{label} contains a frozen number literal — every count must be a "
                    "{placeholder} bound to a live resolver")
    if not _PLACEHOLDER.search(body or ""):
        return "body names no live figure — a card must cite one"
    for m in _PLACEHOLDER.finditer(body or ""):
        if m.group(1) not in _RESOLVERS:
            return f"body binds '{m.group(1)}', which has no live resolver"
    return None


def stage_announcement_pr(key: str, tag: str, title: str, body: str,
                          cta_href: str, cta_label: str,
                          shipped_at: str = "", rationale: str = "") -> dict:
    """Brain entry point: open a DRAFT PR that appends this announcement as
    status=STATUS_PENDING. NOTHING IS PUBLISHED by this call.

    Double-gated and dark by default: routes/drift_pr_writer.py opens draft PRs
    only, against an allowlisted repo, and returns dry_run:true unless
    DCHUB_DRIFT_PR=1 and a PR token are both present. ★ ok:true with dry_run:true
    means NO PR EXISTS — check for pr_url before claiming one was opened.

    The owner approves by changing STATUS_PENDING to STATUS_APPROVED in that PR
    and merging it. Never raises."""
    key = (key or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,48}", key):
        return {"ok": False, "stage": "validate", "error": "key must be lower_snake_case"}
    if any(e.get("key") == key for e in ANNOUNCEMENTS):
        return {"ok": False, "stage": "validate", "error": f"key {key!r} already registered"}
    bad = validate_copy(title, body)
    if bad:
        return {"ok": False, "stage": "validate", "error": bad}
    if not (cta_href or "").startswith("/"):
        return {"ok": False, "stage": "validate", "error": "cta_href must be a site-relative path"}
    shipped = shipped_at or datetime.date.today().isoformat()

    snippet = (
        "    {\n"
        f"        \"key\": {key!r},\n"
        "        # ★ PENDING = staged by the brain, NEVER served. Approve by changing\n"
        "        # this to STATUS_APPROVED in this PR and merging it.\n"
        "        \"status\": STATUS_PENDING,\n"
        "        \"approved_pr\": \"\",\n"
        f"        \"shipped_at\": {shipped!r},\n"
        f"        \"tag\": {tag!r},\n"
        f"        \"title\": {title!r},\n"
        f"        \"body\": {body!r},\n"
        f"        \"cta_href\": {cta_href!r},\n"
        f"        \"cta_label\": {cta_label!r},\n"
        "    },\n"
    )
    edit = {"path": "routes/capability_announcements.py",
            "find": _APPEND_MARKER,
            "replace": snippet + _APPEND_MARKER}
    try:
        from routes.drift_pr_writer import open_drift_fix_pr
        return open_drift_fix_pr(
            "azmartone67/dchub-backend", [edit],
            rationale=(rationale or "Brain-staged platform announcement.") +
            "\n\nStaged as status=STATUS_PENDING — it is NOT served by "
            "/api/v1/whats-new until this PR flips it to STATUS_APPROVED and merges. "
            "Every number in the copy is a {placeholder} resolved live at serve time.")
    except Exception as e:
        return {"ok": False, "stage": "pr_writer",
                "error": f"{type(e).__name__}: {str(e)[:160]}"}
