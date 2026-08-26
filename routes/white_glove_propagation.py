"""
routes/white_glove_propagation.py — canonical-facts propagation (2026-07-18).
=============================================================================

r-white-glove BUILD 1: convert the white-glove registry watch from
WATCH-ONLY to a FIRING service for the "refresh data" leg.

The registry-freshness shell + presence crawler already DETECT stale
listing copy ("58 tools", "3,000+ deals" on a directory page while the
canon says 74 / "1,400+"), but nothing propagated the canonical numbers
outward on a schedule. This module is the daily propagation job:

  (a) reads the pinned canon (ai_surface_canon.PINNED, live tool count
      overlaid fail-soft via the presence crawler's resolver);
  (b) probes every listing in mcp_presence_crawler.SEED_REGISTRIES for
      OLD numbers in the listing HTML — any digit-pattern near "tools"
      that ≠ canon, plus stale deal/facility claims and the curated
      textual stale-markers from the canon;
  (c) registries WITH an automated refresh path (smithery / lobehub /
      glama / pulsemcp manifest re-crawl, official-registry republish
      via the existing mcp-registry-weekly-sync.yml workflow, the real
      POST submitters for mcphive / cursor.directory) — trigger them
      through the existing auto_resubmit_listing dispatcher + one
      workflow_dispatch, and record the outcome;
  (d) human-gated registries (mcp.so dashboard, cursor forum, login/
      captcha walls) — file ONE consolidated brain finding (canonical
      writer, dedupes on issue+url) + ONE GitHub issue titled
      "[white-glove] listing copy drift" containing PASTE-READY
      corrected copy per listing (the canonical per-registry description
      block). The SAME issue is updated on subsequent runs (fingerprint
      dedupe — same drift set = no touch; changed drift set = body
      PATCH), never a new issue per day.

DRIFT DETECTOR — ZERO-FALSE-POSITIVE BY DESIGN
----------------------------------------------
detect_number_drift() is a pure function (unit-tested in
tests/test_white_glove_drift_detector.py). Canon numbers present on a
page NEVER flag. To keep that guarantee it deliberately under-reaches:

  · tool counts: only integers 12-500 adjacent to the word "tools"
    (excluding "tool calls"/"tools.json") and ≠ the canon/live count.
    The free-tier "~10 tools" mention is allowlisted.
  · deals: integers adjacent to "deals"/"transactions" outside the
    canon band [floor, floor+599] (canon "1,400+" → 1400-1999 OK; the
    old over-claims 2,000+/3,000+/4,000+ all flag).
  · facilities: integers adjacent to "facilities" outside
    [floor, 2×floor] (canon "12,650+" → 12650-25300 OK; the stale
    10,706 and the inflated 50,000+ both flag).
  · stale markers: only the LETTERED subset of PINNED["stale_markers"]
    ("58 tools", "4,000+ M&A", "100 calls/day", …) — the bare-number
    markers ("317 ", "2.1.22") are skipped here because on arbitrary
    third-party listing HTML they collide with unrelated numerics.

Wiring:
  · crawler_scheduler SCHEDULE slot (20, 20) — daily, one hour after the
    19:00 mcp_presence_auto_fix sweep so this run can VERIFY it.
  · Kill switch: WHITE_GLOVE_PROPAGATE_DISABLE=1 (checked in the runner
    AND here).
  · Admin endpoint: POST /api/v1/admin/white-glove/propagate
    (?dry_run=1 default for manual pokes; the cron fires live).
    GET /api/v1/admin/white-glove/status — recent run snapshots.

All brain_findings writes go through
routes.brain_findings_writer.upsert_brain_finding (canonical writer —
the live table has no UNIQUE(issue,url), hand-rolled ON CONFLICT fails
silently).
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import os
import re
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from util.json_column import json_for_column

logger = logging.getLogger(__name__)

white_glove_propagation_bp = Blueprint("white_glove_propagation", __name__)

# ── Config ────────────────────────────────────────────────────────────
KILL_SWITCH_ENV = "WHITE_GLOVE_PROPAGATE_DISABLE"
ISSUE_TITLE = "[white-glove] listing copy drift"
ISSUE_LABEL = "white-glove"
_GITHUB_REPO = (os.environ.get("GITHUB_REPO") or "azmartone67/dchub-backend").strip()
_SYNC_WORKFLOW = "mcp-registry-weekly-sync.yml"   # official republish + smithery rescan
MAX_LISTING_FETCHES = 20    # SEED_REGISTRIES is ~16; hard ceiling regardless

# Registries whose refresh path is AUTOMATED (see mcp_presence_crawler
# SUBMITTERS): real form POST, or upstream manifest/README re-crawl that the
# daily-manifest-sync + mcp-registry-weekly-sync workflows keep fresh.
AUTO_PATH_REGISTRIES = {
    "mcphive",              # real POST submitter
    "cursor_directory",     # real POST submitter
    "smithery",             # manifest re-crawl + weekly-sync rescan
    "lobehub",              # manifest/badge re-crawl
    "glama",                # manifest re-crawl (glama refresh on their cadence)
    "pulsemcp",             # manifest re-crawl
    "mcp_official_registry",  # mcp-registry-publish / weekly-sync republish
}
# Everything else in SEED_REGISTRIES (mcp.so + secondary, smith_land, dxt_so,
# yellowmcp, klavis_ai, cline, continue_dev, awesome_mcp_servers) is
# HUMAN-GATED: dashboard edit, forum post, or PR someone must review.


def _disabled() -> bool:
    return (os.environ.get(KILL_SWITCH_ENV) or "").strip().lower() in (
        "1", "true", "yes")


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


# ── Canon ─────────────────────────────────────────────────────────────
def _parse_floor(public_str) -> int | None:
    """'1,400+' → 1400 · '12,650+' → 21000 · '300+' → 300."""
    try:
        digits = re.sub(r"[^\d]", "", str(public_str or ""))
        return int(digits) if digits else None
    except Exception:
        return None


def load_canon() -> dict:
    """Live-resolved canon + fail-soft live tool-count overlay.

    Returns {tools, tools_live, deals_floor, facilities_floor,
    markets_floor, version, endpoint, stale_markers, canon_source}.
    Pure-import fallback values keep this usable even fully offline.

    ★ resolve_canon(), NOT bare PINNED. PINNED["public"] is the DB-DOWN
    fallback — ai_surface_canon says so in its own comments, and warns that
    "every downstream consumer (registry submitters, description builders,
    white-glove propagation) kept pasting it" the last time it froze. This
    lane pasted it again: measured 2026-08-18, the run of 08-16 carried
    facilities_floor=18000 while /api/v1/canon/phrases (which resolve_canon
    serves) said "18,300+". The lane whose whole job is scrubbing stale
    numbers off our listings was writing a 300-stale number ONTO them, and
    every registry it "corrected" was corrected to the wrong value.

    resolve_canon() probes live HTTP, which is why the crawler hot paths
    (ai_interconnection, main:513) deliberately use PINNED instead. This is
    a once-a-day lane, so the probe cost is irrelevant here. Fail-soft: any
    resolver error and the pinned floors stand, with canon_source recording
    which one actually supplied the numbers."""
    out = {
        "tools": None, "tools_live": None,
        "deals_floor": None, "facilities_floor": None, "markets_floor": None,
        "version": None, "endpoint": "https://dchub.cloud/mcp",
        "stale_markers": [], "stale_markers_regex": [], "canon_source": "none",
    }
    try:
        from ai_surface_canon import PINNED
        out["tools"] = PINNED.get("tools_advertised")
        out["version"] = PINNED.get("version")
        out["endpoint"] = PINNED.get("mcp_endpoint") or out["endpoint"]
        pub = PINNED.get("public") or {}
        out["deals_floor"] = _parse_floor(pub.get("deals"))
        out["facilities_floor"] = _parse_floor(pub.get("facilities"))
        out["markets_floor"] = _parse_floor(pub.get("markets"))
        out["stale_markers"] = list(PINNED.get("stale_markers") or [])
        out["stale_markers_regex"] = list(PINNED.get("stale_markers_regex") or [])
        out["canon_source"] = "pinned"
    except Exception as e:
        logger.warning("[white-glove] PINNED import failed: %s", e)
    # ★ Overlay the LIVE-resolved public strings. Per-field and fail-soft:
    # one unresolvable field must not drag the other two back to the pinned
    # fallback, and a resolver that returns nothing leaves the floor alone
    # rather than publishing a None or a 0.
    #
    # ★★ THE OVERLAY ONLY EVER RAISES A FLOOR. NEVER LOWERS ONE. resolve_canon()
    # does not raise when the DB is down — it DEGRADES. Measured 2026-08-18 with
    # no DATABASE_URL it returned, without error, facilities=400 and deals=1400
    # against pinned floors of 18,000 and 1,800. A `> 0` sanity check does not
    # catch that: 400 is a perfectly positive integer. Taking the live value
    # unconditionally would let one DB hiccup mid-run paste "400+ facilities"
    # onto every registry listing — a 45x UNDER-claim, and strictly worse than
    # the 300-stale number this whole change exists to fix.
    #
    # max() is also the documented semantic: ai_surface_canon calls these
    # FLOORS, says they "round DOWN", and says to "re-floor downward if the
    # fleet ever shrinks" — a deliberate human edit to PINNED, never something
    # an automated lane infers from one degraded probe.
    try:
        from ai_surface_canon import resolve_canon
        live_pub = (resolve_canon() or {}).get("public") or {}
        resolved, rejected = [], []
        for field, key in (("deals_floor", "deals"),
                           ("facilities_floor", "facilities"),
                           ("markets_floor", "markets")):
            val = _parse_floor(live_pub.get(key))
            if not (isinstance(val, int) and val > 0):
                continue
            pinned_val = out.get(field)
            if isinstance(pinned_val, int) and val < pinned_val:
                rejected.append(f"{key}={val}<{pinned_val}")
                continue
            out[field] = val
            resolved.append(key)
        if rejected:
            logger.warning("[white-glove] resolve_canon returned BELOW-FLOOR "
                           "values, keeping pinned (%s) — this is what a "
                           "DB-down resolver looks like", ", ".join(rejected))
            out["canon_below_floor"] = rejected
        if resolved:
            out["canon_source"] = "resolve_canon:" + ",".join(resolved)
    except Exception as e:
        logger.warning("[white-glove] resolve_canon overlay failed, pinned "
                       "floors stand: %s", e)
    # Live tool count (the crawler's resolver hits /api/v1/mcp/tools.json
    # with a tier_registry fallback). Fail-soft — pinned stays the floor.
    try:
        from routes.mcp_presence_crawler import _our_actual_tool_count
        live = _our_actual_tool_count()
        if isinstance(live, int) and live > 0:
            out["tools_live"] = live
    except Exception as e:
        logger.info("[white-glove] live tool count unavailable: %s", e)
    return out


# ── Pure drift detector ──────────────────────────────────────────────
# Numbers must not be a fragment of a bigger numeric ("2.4.4", "1,400").
_NUM = r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)"
# "58 tools", "58+ tools", "58 live MCP tools" (≤2 CURATED modifier
# words — a generic \w+ here matched "300 markets and tools") — but NOT
# "979 lifetime tool calls" and NOT "tools.json".
_TOOLS_FWD_RE = re.compile(
    _NUM + r"\s*\+?\s*(?:(?:live|mcp|full|total|new|different|available|"
           r"distinct|specialized|powerful|data|api)\s+){0,2}tools?\b"
           r"(?!\s*(?:calls?\b|\.json))",
    re.IGNORECASE)
# "tools: 58" / "tools = 58" / JSON '"tools": 58'
_TOOLS_REV_RE = re.compile(
    r"\btools?\s*[\"']?\s*[:=]\s*[\"']?(\d{1,3}(?:,\d{3})+|\d+)",
    re.IGNORECASE)
_DEALS_RE = re.compile(
    _NUM + r"\s*\+?\s*(?:tracked\s+)?(?:M&A\s+)?(?:deals?|transactions)\b",
    re.IGNORECASE)
_FACILITIES_RE = re.compile(
    _NUM + r"\s*\+?\s*(?:data[\s-]?center\s+|global\s+|discovered\s+)?"
    r"facilit(?:y|ies)\b",
    re.IGNORECASE)

TOOL_COUNT_MIN_PLAUSIBLE = 12   # "~10 tools" free-tier mention is legit copy
TOOL_COUNT_MAX_PLAUSIBLE = 500
DEALS_BAND_WIDTH = 600          # canon floor .. floor+599 is honest live range
FACILITIES_BAND_MULT = 2        # canon floor .. 2×floor is honest live range

# Directory pages list MANY servers' tool counts. Only judge copy within
# this many chars of a DC Hub mention — other servers' numbers are not
# our drift.
_DCHUB_MENTION_RE = re.compile(r"dc[\s\-_]?hub|dchub", re.IGNORECASE)
DCHUB_WINDOW_CHARS = 1200


def _to_int(s: str) -> int | None:
    try:
        return int(s.replace(",", ""))
    except Exception:
        return None


def _snippet(text: str, start: int, end: int, pad: int = 60) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - pad):end + pad]).strip()[:200]


def _dchub_windows(text: str) -> str:
    """Concatenated ±DCHUB_WINDOW_CHARS slices around DC Hub mentions
    (merged when overlapping). Empty string when we're not mentioned —
    a page that isn't about us has no copy of ours to drift."""
    spans: list[list[int]] = []
    for m in _DCHUB_MENTION_RE.finditer(text):
        lo = max(0, m.start() - DCHUB_WINDOW_CHARS)
        hi = min(len(text), m.end() + DCHUB_WINDOW_CHARS)
        if spans and lo <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], hi)
        else:
            spans.append([lo, hi])
    return "\n".join(text[lo:hi] for lo, hi in spans)


def _official_registry_latest_only(body: str) -> str:
    """The official-registry listing URL is a SEARCH endpoint that returns
    EVERY published version (26 at last count) — historical descriptions
    legitimately carry retired counts ('33 MCP tools', '21k+'), so scanning
    the whole payload made this registry read PERMANENTLY drifted and buried
    real drift (2026-07-26: the isLatest 2.5.1 entry was clean while the loop
    flagged four stale tool-counts from 2.1.x-era versions). Clients resolve
    the latest version, so drift is judged on the isLatest entry ONLY.
    Fail-soft: any parse surprise returns the body unchanged (over-detection
    is safer than silence)."""
    try:
        d = json.loads(body)
        for s in (d.get("servers") or []):
            meta = ((s.get("_meta") or {})
                    .get("io.modelcontextprotocol.registry/official") or {})
            if meta.get("isLatest"):
                return json.dumps(s)
    except Exception:
        pass
    return body


def detect_number_drift(page_text: str, canon: dict,
                        scope_to_dchub: bool = True) -> list[dict]:
    """Pure detector. Returns [{kind, found, expected, context}, …].

    ZERO-FALSE-POSITIVE CONTRACT: any page whose DC Hub copy matches the
    canon (or the live overlay) produces []. See module docstring for
    the deliberate under-reach rules that guarantee it. With
    scope_to_dchub (default), only text near a DC Hub mention is judged
    — directory pages listing OTHER servers' counts never flag."""
    if not page_text:
        return []
    text = _html.unescape(page_text)
    if scope_to_dchub:
        text = _dchub_windows(text)
        if not text:
            return []
    findings: list[dict] = []
    seen: set[tuple] = set()

    def _add(kind, found, expected, m):
        key = (kind, found)
        if key in seen:
            return
        seen.add(key)
        findings.append({
            "kind": kind,
            "found": found,
            "expected": expected,
            "context": _snippet(text, m.start(), m.end()),
        })

    # tools ------------------------------------------------------------
    tools_ok = {n for n in (canon.get("tools"), canon.get("tools_live"))
                if isinstance(n, int)}
    if tools_ok:
        expected_str = "/".join(str(n) for n in sorted(tools_ok))
        for m in list(_TOOLS_FWD_RE.finditer(text)) + list(_TOOLS_REV_RE.finditer(text)):
            n = _to_int(m.group(1))
            if n is None or n in tools_ok:
                continue
            if n < TOOL_COUNT_MIN_PLAUSIBLE or n > TOOL_COUNT_MAX_PLAUSIBLE:
                continue
            _add("tools", n, expected_str, m)

    # deals ------------------------------------------------------------
    deals_floor = canon.get("deals_floor")
    if isinstance(deals_floor, int) and deals_floor > 0:
        for m in _DEALS_RE.finditer(text):
            n = _to_int(m.group(1))
            if n is None:
                continue
            if deals_floor <= n < deals_floor + DEALS_BAND_WIDTH:
                continue
            if n < 100:   # "3 deals this week"-style editorial, not our claim
                continue
            _add("deals", n, f"{deals_floor:,}+", m)

    # facilities -------------------------------------------------------
    fac_floor = canon.get("facilities_floor")
    if isinstance(fac_floor, int) and fac_floor > 0:
        for m in _FACILITIES_RE.finditer(text):
            n = _to_int(m.group(1))
            if n is None:
                continue
            if fac_floor <= n <= fac_floor * FACILITIES_BAND_MULT:
                continue
            if n < 1000:  # "12 facilities in Ashburn" — not the global claim
                continue
            _add("facilities", n, f"{fac_floor:,}+", m)

    # curated stale markers (LETTERED subset only — see module docstring)
    low = text.lower()
    for marker in canon.get("stale_markers") or []:
        if not re.search(r"[A-Za-z]", marker):
            continue   # bare numbers / version strings collide on 3p HTML
        if marker.lower() in low:
            idx = low.find(marker.lower())
            findings.append({
                "kind": "stale_marker",
                "found": marker,
                "expected": "(scrub — known-stale claim)",
                "context": _snippet(text, idx, idx + len(marker)),
            })

    # ★2026-08-22: withdrawn-CAPABILITY markers (regex). Catch a retired
    # capability still advertised as live — the class the literal/number
    # markers above cannot see. Each entry is {re, label}; the pattern is
    # written so our own corrected copy (which pairs the term with
    # "withdrawn") never matches. A malformed pattern is skipped, not raised
    # (one bad canon entry must not blind the whole detector).
    for entry in canon.get("stale_markers_regex") or []:
        pat = (entry or {}).get("re") if isinstance(entry, dict) else entry
        if not pat:
            continue
        try:
            m = re.search(pat, text, re.IGNORECASE)
        except re.error:
            continue
        if m:
            findings.append({
                "kind": "stale_capability",
                "found": m.group(0),
                "expected": "(scrub — withdrawn capability advertised as live)",
                "label": (entry or {}).get("label") if isinstance(entry, dict) else None,
                "context": _snippet(text, m.start(), m.end()),
            })
    return findings


# ── DB helpers ────────────────────────────────────────────────────────
def _db_conn():
    try:
        from routes.mcp_presence_crawler import _db_conn as _c
        return _c()
    except Exception:
        return None


def ran_today() -> bool | None:
    """Has a NON-dry run already landed today (UTC)? None = could not tell.

    ★ THE SCHEDULER'S MEMORY IS NOT DURABLE. crawler_scheduler keeps its
    already-ran set in a plain in-process dict (`last_run_hours`), reset when the
    UTC day changes, and only fires a slot while `now.minute < 5`. So a redeploy
    anywhere inside 20:00–20:04 loses the whole day: the new process starts with
    an empty dict, the five-minute window has already passed, and nothing ever
    catches up. Measured 2026-08-05: white-glove ran on 3 of the last 7 days
    (missing 07-30, 08-02, 08-03, 08-05), and main took 10 commits between 19:00
    and 21:00Z on 08-03 alone.

    ★ None, NEVER False, when the DB cannot be read. A guard that answers "no" on
    an outage would let the catch-up slot re-run a job that already ran — the
    same unmeasured-as-zero mistake this codebase keeps paying for. The caller
    treats None as "do not assume it is safe to re-run".
    """
    conn = _db_conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            _ensure_runs_table(cur)
            cur.execute(
                "SELECT 1 FROM white_glove_runs "
                " WHERE COALESCE(dry_run, FALSE) = FALSE"
                "   AND created_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')"
                " LIMIT 1")
            return cur.fetchone() is not None
    except Exception as e:  # noqa: BLE001
        logger.warning("white-glove ran_today check failed: %s", e)
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _ensure_runs_table(cur) -> None:
    cur.execute(
        "CREATE TABLE IF NOT EXISTS white_glove_runs ("
        " id BIGSERIAL PRIMARY KEY,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " dry_run BOOLEAN,"
        " checked INT, drifted INT, auto_path INT, human_gated INT,"
        " issue_url TEXT, payload JSONB)")


# ── GitHub: one consolidated, fingerprint-deduped issue ──────────────
def _gh_token() -> str:
    return (os.environ.get("PR_SUBMIT_TOKEN")
            or os.environ.get("GITHUB_TOKEN") or "").strip()


def _gh_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _fingerprint(drifts_by_registry: dict) -> str:
    """Stable hash of the drift SET (registry, kind, found) — same set on
    the next run means the open issue is already accurate → no touch."""
    flat = sorted(
        (reg, d["kind"], str(d["found"]))
        for reg, dl in drifts_by_registry.items() for d in dl)
    return hashlib.sha256(json.dumps(flat).encode()).hexdigest()[:16]


def _paste_ready_block(registry_name: str, listing: dict, canon: dict,
                       drifts: list[dict]) -> str:
    """Per-listing markdown section with copy an operator can paste."""
    try:
        from routes.mcp_presence_crawler import _build_canonical_description
        desc = _build_canonical_description(registry_name)
    except Exception:
        t = canon.get("tools_live") or canon.get("tools") or "74"
        desc = (f"DC Hub MCP: {t} live tools for data-center infrastructure — "
                f"facilities, DCPI markets, tracked deals, ISO grid, fiber, gas.")

    # Claim Loop step 3: never hand an operator paste-ready copy that trips a
    # lie class (an over-claim we just told them to publish). Run the generated
    # description through the claim-breaker; when the gate is TRUSTED and the
    # copy is not ok, withhold it and substitute a canon-clean, count-free line
    # (fail closed for canon copy). UNTRUSTED / disabled / any error -> keep the
    # copy and log (the gate must not silently blank the human loop).
    try:
        from routes.claim_breaker import breaker as _claim_breaker
        _cb = _claim_breaker(desc, "canon")
        if _cb.get("trusted") and not _cb.get("ok"):
            _cls = ", ".join(
                sorted({v.get("cls", "?") for v in (_cb.get("violations") or [])}))
            logger.warning(
                "[white-glove] claim-breaker withheld %s copy (lie class %s): %s",
                registry_name, _cls,
                "; ".join(v.get("detail", "") for v in (_cb.get("violations") or [])[:2])[:200])
            desc = ("DC Hub MCP: live tools for data-center infrastructure — "
                    "facilities, DCPI markets, tracked deals, ISO grid, fiber, "
                    "energy, water and tax. Real-time, versioned, cited.")
        elif not _cb.get("trusted"):
            logger.warning(
                "[white-glove] claim-breaker UNTRUSTED for %s copy — keeping "
                "generated description as-is", registry_name)
    except Exception as _cbe:
        logger.info("[white-glove] claim-breaker unavailable (%s) — keeping copy",
                    _cbe)
    wrong = "; ".join(
        f"shows **{d['found']}** for {d['kind']} (canon: {d['expected']})"
        for d in drifts[:6])
    lines = [
        f"### {registry_name}",
        f"- Listing: {listing.get('listing_url')}",
    ]
    if listing.get("submit_url"):
        lines.append(f"- Edit / submit: {listing.get('submit_url')}")
    lines += [
        f"- Drift: {wrong}",
        "",
        "**Paste-ready corrected description:**",
        "```",
        desc,
        "```",
        "",
    ]
    return "\n".join(lines)


def _issue_body(drifts_by_registry: dict, listings_by_name: dict,
                canon: dict, fp: str) -> str:
    tools = canon.get("tools_live") or canon.get("tools")
    head = [
        f"<!-- wg-fp:{fp} -->",
        "## White-glove: listing copy drift (human-gated registries)",
        "",
        "Daily canonical-facts propagation found stale numbers on listings "
        "that need a human. Either they have **no automated refresh path** "
        "(dashboard/forum/PR-gated), or they have one and it reported that it "
        "**cannot finish** — our upstream manifest already carries canon and "
        "the listing is still wrong, so the registry's re-crawl is not landing "
        "and waiting will not fix it. "
        "Paste the corrected copy below into each listing's edit surface.",
        "",
        "**Canonical numbers (ai_surface_canon):** "
        f"`{tools} tools` · `{(canon.get('deals_floor') or 0):,}+ tracked deals` · "
        f"`{(canon.get('facilities_floor') or 0):,}+ facilities` · "
        f"`{(canon.get('markets_floor') or 0):,}+ markets` · "
        f"endpoint `{canon.get('endpoint')}`",
        "",
        "**Canonical MCP config block (universal copy-paste):**",
        "```json",
        json.dumps({"dchub": {"transport": "streamable-http",
                              "url": canon.get("endpoint")}}, indent=2),
        "```",
        "",
    ]
    sections = [
        _paste_ready_block(reg, listings_by_name.get(reg, {}), canon, dl)
        for reg, dl in sorted(drifts_by_registry.items())
    ]
    tail = [
        "---",
        "*Auto-maintained by `routes/white_glove_propagation.py` (daily "
        "cron `white_glove_propagate`). This SAME issue is updated in place "
        "when the drift set changes; it is not re-filed. Close it once every "
        "listing above is corrected — it will reopen only on NEW drift.*",
    ]
    return "\n".join(head + sections + tail)


def upsert_drift_issue(drifts_by_registry: dict, listings_by_name: dict,
                       canon: dict, dry_run: bool) -> dict:
    """Create-or-update THE consolidated GitHub issue. Never raises."""
    token = _gh_token()
    if not token:
        return {"ok": False, "action": "skipped", "error": "no_github_token"}
    fp = _fingerprint(drifts_by_registry)
    if dry_run:
        return {"ok": True, "action": "dry_run", "fingerprint": fp}
    try:
        import requests
        # Find the existing open issue by exact title (first 100 open).
        r = requests.get(
            f"https://api.github.com/repos/{_GITHUB_REPO}/issues",
            headers=_gh_headers(token),
            params={"state": "open", "per_page": 100},
            timeout=15)
        existing = None
        if r.status_code == 200:
            for it in (r.json() or []):
                if "pull_request" in it:
                    continue
                if (it.get("title") or "").strip() == ISSUE_TITLE:
                    existing = it
                    break
        body = _issue_body(drifts_by_registry, listings_by_name, canon, fp)
        if existing:
            if f"wg-fp:{fp}" in (existing.get("body") or ""):
                return {"ok": True, "action": "unchanged",
                        "number": existing.get("number"),
                        "url": existing.get("html_url"), "fingerprint": fp}
            r2 = requests.patch(
                f"https://api.github.com/repos/{_GITHUB_REPO}/issues/"
                f"{existing.get('number')}",
                headers=_gh_headers(token),
                json={"body": body}, timeout=15)
            if r2.status_code == 200:
                return {"ok": True, "action": "updated",
                        "number": existing.get("number"),
                        "url": existing.get("html_url"), "fingerprint": fp}
            return {"ok": False, "action": "update_failed",
                    "error": f"github_{r2.status_code}"}
        r3 = requests.post(
            f"https://api.github.com/repos/{_GITHUB_REPO}/issues",
            headers=_gh_headers(token),
            json={"title": ISSUE_TITLE, "body": body,
                  "labels": [ISSUE_LABEL, "registry-drift"]},
            timeout=15)
        if r3.status_code in (200, 201):
            d = r3.json() or {}
            return {"ok": True, "action": "created",
                    "number": d.get("number"), "url": d.get("html_url"),
                    "fingerprint": fp}
        return {"ok": False, "action": "create_failed",
                "error": f"github_{r3.status_code}: {r3.text[:150]}"}
    except Exception as e:
        return {"ok": False, "action": "error",
                "error": f"{type(e).__name__}: {str(e)[:150]}"}


def _trigger_registry_sync_workflow(dry_run: bool) -> dict:
    """workflow_dispatch the existing daily registry sync (official-registry
    republish + Smithery re-scan + coverage report) so the automated-path
    registries re-pull the canonical numbers TODAY, not on their own
    cadence. Never raises."""
    token = _gh_token()
    if not token:
        return {"ok": False, "error": "no_github_token"}
    if dry_run:
        return {"ok": True, "dry_run": True,
                "would_dispatch": _SYNC_WORKFLOW}
    try:
        import requests
        r = requests.post(
            f"https://api.github.com/repos/{_GITHUB_REPO}/actions/workflows/"
            f"{_SYNC_WORKFLOW}/dispatches",
            headers=_gh_headers(token),
            json={"ref": "main"}, timeout=15)
        return {"ok": r.status_code == 204,
                "http_status": r.status_code, "workflow": _SYNC_WORKFLOW}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}


# ── The daily job ─────────────────────────────────────────────────────
# ── customer-onboarding lane (2026-07-29) ────────────────────────────────
# ★WHY THIS LIVES IN WHITE GLOVE. "White glove" here used to mean ONE thing:
# registry/listing-copy propagation. Nothing watched whether a HUMAN who just
# paid us was actually looked after — so the only way to answer "did this
# customer get white-glove treatment?" was to open Stripe and hand-query four
# tables, which is exactly what had to be done for alexander@ryex.net on
# 2026-07-29. That audit found: entitlements perfect (conversion row,
# users.plan, active api_key, mcp_dev_keys.tier=paid, all inside one second)
# but FOUR welcome emails in three seconds, no Stripe receipt, and zero API
# calls 40 minutes after paying. None of it was visible anywhere.
#
# A propagation checker that guards our LISTINGS but not our CUSTOMERS has the
# priority backwards. This lane makes each new payer a checked surface.
#
# Read-only and fail-soft: it must never affect the propagation pass.
_ONBOARD_LOOKBACK_DAYS = int(os.environ.get("WHITE_GLOVE_CUSTOMER_DAYS", "14"))


def check_customer_onboarding() -> dict:
    """Audit recently-paid customers end to end. Returns a summary dict.

    Per customer, three independent questions:
      entitled   — do users.plan / an ACTIVE api_key / mcp_dev_keys.tier all
                   exist? (money taken but access not granted is the worst
                   possible failure, so it is checked first)
      welcomed   — EXACTLY ONE welcome email. Zero is silence; more than one is
                   the flood that shipped four in three seconds.
      activated  — any MCP call after paying. Paid-but-never-called is the
                   real churn signal and nothing else reports it.
    """
    out = {"ok": True, "window_days": _ONBOARD_LOOKBACK_DAYS,
           "customers": [], "problems": 0, "checked": 0}
    conn = _db_conn()
    if conn is None:
        out["ok"] = False
        out["error"] = "no_db"
        return out
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT LOWER(c.user_email), MIN(c.created_at)
                     FROM mcp_conversions c
                    WHERE c.created_at >= NOW() - make_interval(days => %s)
                      AND c.user_email IS NOT NULL
                      AND c.stripe_customer_id IS NOT NULL
                      AND c.refunded_at IS NULL
                    GROUP BY 1 ORDER BY 2 DESC LIMIT 50""",
                (_ONBOARD_LOOKBACK_DAYS,))
            payers = cur.fetchall() or []
            for email, paid_at in payers:
                rec = {"email": email, "paid_at": str(paid_at)[:19]}
                # entitled
                cur.execute(
                    """SELECT (SELECT COALESCE(plan,'') FROM users
                                WHERE LOWER(email)=%s LIMIT 1),
                              (SELECT COUNT(*) FROM api_keys k
                                WHERE k.user_id IN (SELECT id FROM users
                                                     WHERE LOWER(email)=%s)),
                              (SELECT COUNT(*) FROM mcp_dev_keys
                                WHERE LOWER(COALESCE(email,''))=%s)""",
                    (email, email, email))
                plan, nkeys, ndev = cur.fetchone() or ("", 0, 0)
                rec["plan"] = plan or None
                rec["entitled"] = bool(plan and plan != "free"
                                       and int(nkeys or 0) > 0)
                # welcomed — exactly one
                cur.execute(
                    """SELECT COUNT(*) FROM welcome_email_log
                        WHERE LOWER(email)=%s
                          AND status IN ('sent','sent_via_resend')
                          AND COALESCE(plan,'') NOT LIKE 'receipt%%'""",
                    (email,))
                nw = int((cur.fetchone() or [0])[0] or 0)
                rec["welcome_emails"] = nw
                rec["welcomed"] = (nw == 1)
                # activated — any call after paying.
                # ★★UNMEASURED, NOT ZERO. `api_keys.key_prefix` stores a SHORT
                # generic prefix ("dchub_dev_"), while `mcp_call_log.api_key`
                # holds the caller key. Equality can NEVER match, and a LIKE on
                # a 10-char generic prefix would match every dev key on the
                # platform — so a naive join reports 0 calls for EVERYONE and
                # reads as "no customer ever activated". That is a false zero of
                # exactly the kind this codebase keeps getting burned by
                # (/health funnel, prewall projections, mcp_conversions refunds),
                # and it would be the most damaging one yet: it would make a
                # healthy customer base look dead.
                # Until customer→key attribution is joinable, report None and
                # SAY it is unmeasured. A missing metric is honest; a fabricated
                # zero is not.
                rec["calls_since_paid"] = None
                rec["activated"] = None
                rec["activation_note"] = (
                    "UNMEASURED — api_keys.key_prefix is a generic prefix and "
                    "does not join to mcp_call_log.api_key; needs a real "
                    "customer->key link before activation can be reported")
                rec["ok"] = bool(rec["entitled"] and rec["welcomed"])
                if not rec["ok"]:
                    out["problems"] += 1
                out["customers"].append(rec)
                out["checked"] += 1
    except Exception as e:          # noqa: BLE001
        out["ok"] = False
        out["error"] = str(e)[:180]
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def run_white_glove_propagation(dry_run: bool = False) -> dict:
    """One propagation pass. Defensive — never raises."""
    if _disabled():
        return {"ok": True, "skipped": True,
                "reason": f"{KILL_SWITCH_ENV}=1"}
    started = datetime.now(timezone.utc)
    canon = load_canon()
    summary: dict = {
        "ok": True,
        "dry_run": bool(dry_run),
        "canon": {k: canon.get(k) for k in
                  ("tools", "tools_live", "deals_floor",
                   "facilities_floor", "version")},
        "checked": 0, "fetch_failed": 0,
        "drifted": [], "clean": [],
        "auto_path_fired": [], "human_gated": [],
    }
    # ★Customer onboarding is now part of every white-glove pass. Fail-soft:
    # a customer-lane error must never break listing propagation.
    try:
        summary["customers"] = check_customer_onboarding()
    except Exception as _ce:
        summary["customers"] = {"ok": False, "error": str(_ce)[:150]}
    try:
        from routes.mcp_presence_crawler import SEED_REGISTRIES, _polite_get
    except Exception as e:
        summary["ok"] = False
        summary["error"] = f"crawler_import: {str(e)[:150]}"
        return summary

    listings_by_name = {r["registry_name"]: r for r in SEED_REGISTRIES}
    drifts_by_registry: dict[str, list] = {}

    for reg in SEED_REGISTRIES[:MAX_LISTING_FETCHES]:
        name = reg["registry_name"]
        url = reg.get("listing_url")
        if not url:
            continue
        summary["checked"] += 1
        try:
            body, status = _polite_get(url)
        except Exception as e:
            body, status = None, None
            logger.info("[white-glove] fetch %s failed: %s", name, e)
        if not body:
            summary["fetch_failed"] += 1
            continue
        if name == "mcp_official_registry":
            body = _official_registry_latest_only(body)
        drifts = detect_number_drift(body, canon)
        if drifts:
            drifts_by_registry[name] = drifts
            summary["drifted"].append(
                {"registry": name,
                 "drifts": [{k: d[k] for k in ("kind", "found", "expected")}
                            for d in drifts[:6]]})
        else:
            summary["clean"].append(name)

    # ── (c) automated refresh paths ──────────────────────────────────
    auto_drifted = [n for n in drifts_by_registry if n in AUTO_PATH_REGISTRIES]
    # ★2026-07-28 r-closeloop: registries land in AUTO_PATH_REGISTRIES on the
    # promise that something automated will fix them, and (d) below excludes
    # them from the human-gated issue on the strength of that promise. Nothing
    # ever checked whether the promise was kept. Smithery's submitter returned
    # an unconditional ok=True while doing nothing, so its listing drifted for
    # days: too "automated" for the human loop, and the automation was a no-op.
    # A registry whose submitter reports it cannot finish now escalates, so the
    # only way out of this set is a fix that actually landed.
    escalated: set[str] = set()
    if auto_drifted:
        try:
            from routes.mcp_presence_crawler import auto_resubmit_listing
            for name in auto_drifted:
                try:
                    r = auto_resubmit_listing(name, dry_run=dry_run)
                except Exception as e:
                    r = {"ok": False, "error": str(e)[:150]}
                if r.get("escalate"):
                    escalated.add(name)
                summary["auto_path_fired"].append({
                    "registry": name,
                    "ok": bool(r.get("ok")),
                    "submitter": r.get("submitter"),
                    "manifest_upstream": bool(r.get("requires_manifest_update")),
                    "rate_limited": bool(r.get("rate_limited")),
                    "escalated": bool(r.get("escalate")),
                    # ★2026-08-25: this cap was [:200], which cut the
                    # remediation mid-flag — the 08-25 row ended
                    # "...sync-tools-manifest.mjs -", where a bare "-" reads
                    # as the whole flag. A truncated INSTRUCTION is not a
                    # shorter instruction, it is a wrong one. 600 clears
                    # every string _submitter_* builds with room to spare;
                    # the cap stays so an unbounded upstream error message
                    # cannot bloat the payload column.
                    "next_action": (r.get("next_action") or "")[:600],
                })
        except Exception as e:
            summary["auto_path_error"] = str(e)[:150]
        summary["workflow_dispatch"] = _trigger_registry_sync_workflow(dry_run)
    summary["escalated_to_human"] = sorted(escalated)

    # ── (d) human-gated: ONE finding + ONE issue ─────────────────────
    human_drifted = {n: d for n, d in drifts_by_registry.items()
                     if n not in AUTO_PATH_REGISTRIES or n in escalated}
    summary["human_gated"] = sorted(human_drifted.keys())
    issue_result = {}
    if human_drifted:
        issue_result = upsert_drift_issue(
            human_drifted, listings_by_name, canon, dry_run)
        summary["github_issue"] = issue_result

    # brain finding (consolidated; canonical writer; auto-resolve on clean).
    # Skipped on dry-run so a manual ?dry_run=1 poke stays side-effect-free.
    conn = _db_conn()
    if conn is not None:
        if dry_run:
            summary["finding_written"] = "skipped_dry_run"
        else:
            _write_consolidated_finding(conn, summary, human_drifted,
                                        issue_result)
        # run snapshot (fail-soft; written on dry-run too, flagged as such)
        try:
            with conn.cursor() as cur:
                _ensure_runs_table(cur)
                cur.execute(
                    "INSERT INTO white_glove_runs "
                    "(dry_run, checked, drifted, auto_path, human_gated, "
                    " issue_url, payload) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (bool(dry_run), summary["checked"],
                     len(drifts_by_registry), len(auto_drifted),
                     len(human_drifted),
                     (issue_result or {}).get("url"),
                     json_for_column(summary, 60000)))
            conn.commit()
        except Exception as e:
            logger.info("[white-glove] snapshot insert failed: %s", e)
            try:
                conn.rollback()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass

    summary["elapsed_s"] = round(
        (datetime.now(timezone.utc) - started).total_seconds(), 1)
    return summary


def _write_consolidated_finding(conn, summary: dict, human_drifted: dict,
                                issue_result: dict) -> None:
    """ONE consolidated brain finding via the canonical writer; resolves
    by absence when everything is clean. Never raises."""
    try:
        from routes.brain_findings_writer import upsert_brain_finding
        with conn.cursor() as cur:
            if human_drifted:
                detail = (
                    "White-glove propagation: stale listing copy on "
                    f"{len(human_drifted)} human-gated registr"
                    f"{'y' if len(human_drifted) == 1 else 'ies'} "
                    f"({', '.join(sorted(human_drifted))}). "
                    "Paste-ready corrected copy per listing lives in the "
                    f"consolidated GitHub issue '{ISSUE_TITLE}'"
                    + (f" — {issue_result.get('url')}"
                       if issue_result.get("url") else "")
                    + ". Automated-path registries are refreshed by the "
                    "same cron via auto_resubmit_listing + the "
                    "mcp-registry-weekly-sync workflow.")
                upsert_brain_finding(
                    cur,
                    issue="white_glove_listing_copy_drift",
                    url="https://dchub.cloud/admin/registry-freshness",
                    count=len(human_drifted),
                    detail=detail[:1900],
                    detector="white_glove_propagation",
                    status="open")
            else:
                upsert_brain_finding(
                    cur,
                    issue="white_glove_listing_copy_drift",
                    url="https://dchub.cloud/admin/registry-freshness",
                    count=1,
                    detail="All probed human-gated listings match the "
                           "canonical numbers — resolved by absence.",
                    detector="white_glove_propagation",
                    status="resolved")
        conn.commit()
        summary["finding_written"] = True
    except Exception as e:
        summary["finding_written"] = False
        summary["finding_error"] = str(e)[:150]
        try:
            conn.rollback()
        except Exception:
            pass


# ── Endpoints ─────────────────────────────────────────────────────────
@white_glove_propagation_bp.route("/api/v1/admin/white-glove/propagate",
                                  methods=["POST"])
def propagate_endpoint():
    if not _admin_ok():
        return jsonify(ok=False, error="admin_key_required"), 401
    if _disabled():
        return jsonify(ok=True, skipped=True,
                       reason=f"{KILL_SWITCH_ENV}=1"), 200
    raw = (request.args.get("dry_run") or "1").strip().lower()
    dry_run = raw not in ("0", "false", "no", "off")

    # ★ THE ran_today() GUARD LIVES HERE NOW, NOT ONLY IN THE SCHEDULER.
    # It used to exist solely in crawler_scheduler._run_white_glove_propagate,
    # so this endpoint ran propagation unconditionally — every caller was one
    # POST away from a duplicate day of real outbound registry submissions.
    # That was survivable while the only caller was the in-process slot. It is
    # not survivable now that an off-worker cron POSTs here as a backstop:
    # without this, the backstop would re-submit every listing on the ~60% of
    # days the lane ALREADY ran.
    #
    # Only NON-dry runs are gated. A dry run submits nothing, and ran_today()
    # deliberately ignores dry rows anyway.
    #
    # ★ None means "could not tell", and it SKIPS. Per ran_today.__doc__ the
    # guard answers None rather than False on a DB outage precisely so a
    # caller cannot read an outage as permission to re-run. Treating unknown
    # as go is the unmeasured-as-zero mistake this codebase keeps paying for.
    force = (request.args.get("force") or "").strip().lower() in (
        "1", "true", "yes", "on")
    if not dry_run and not force:
        already = ran_today()
        if already is not False:
            return jsonify(
                ok=True, skipped=True,
                reason=("already_ran_today" if already
                        else "ran_today_unknown_db_unreadable"),
                ran_today=already,
                hint="pass ?force=1 to override"), 200

    return jsonify(run_white_glove_propagation(dry_run=dry_run)), 200


@white_glove_propagation_bp.route("/api/v1/admin/white-glove/status",
                                  methods=["GET"])
def status_endpoint():
    if not _admin_ok():
        return jsonify(ok=False, error="admin_key_required"), 401
    out = {"ok": True, "disabled": _disabled(), "runs": []}
    conn = _db_conn()
    if conn is None:
        out["error"] = "db_unavailable"
        return jsonify(out), 200
    try:
        with conn.cursor() as cur:
            _ensure_runs_table(cur)
            cur.execute(
                "SELECT created_at, dry_run, checked, drifted, auto_path, "
                "human_gated, issue_url FROM white_glove_runs "
                "ORDER BY created_at DESC LIMIT 14")
            for row in cur.fetchall():
                out["runs"].append({
                    "created_at": row[0].isoformat() if row[0] else None,
                    "dry_run": row[1], "checked": row[2], "drifted": row[3],
                    "auto_path": row[4], "human_gated": row[5],
                    "issue_url": row[6],
                })
        conn.commit()
    except Exception as e:
        out["error"] = str(e)[:150]
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return jsonify(out), 200


def register_white_glove_propagation(app) -> None:
    app.register_blueprint(white_glove_propagation_bp)
    logger.info("white_glove_propagation registered: "
                "POST /api/v1/admin/white-glove/propagate, "
                "GET /api/v1/admin/white-glove/status")
