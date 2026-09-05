"""
routes/brain_capability_ledger.py — Brain Capability Ledger / Source 8 (2026-06-20).

WHY THIS EXISTS
---------------
The Investigator's REASON step (routes/brain_investigator.py) keeps recommending
things the platform ALREADY SHIPS. The clearest example: it told us to "build a
durable-key-on-first-call surface so agents don't re-mint every session" — that
surface (`persist_command` / `buildAutoMintBlock`) has been LIVE in the MCP
gateway for weeks. The brain loops on it because GATHER never handed it a list of
what already exists; the model can't avoid re-inventing what it can't see.

Source 8 closes that gap. It maintains a small, HONEST ledger of live (and
flag-gated / inert) capabilities and feeds REASON a compact "ALREADY BUILT"
evidence line. This is a self-knowledge source, not a metrics source — it stops
the brain recommending work that's done.

WHAT IT IS
----------
  (a) A tiny table `brain_live_capabilities (name PK, location, status, note,
      updated_at)` created idempotently via DIRECT psycopg2 (safe_db SKIPs DDL
      under SKIP_DDL=1, same trap brain_investigator.init_investigator_schema
      sidesteps).
  (b) An idempotent seed (ON CONFLICT DO UPDATE) from a CURATED, hand-verified
      capability list — every item checked against the live gateway source
      (the dchub-mcp-server repo's server.mjs, line refs in the
      `loc` field). Status is HONEST: LIVE = on the default code path / a
      registered tool; FLAG-GATED = behind an env toggle (default recorded);
      INERT = present but OFF/not enforced (so the brain doesn't "discover" it
      as a gap either).
  (c) gather_live_capabilities() -> list[dict] — the GATHER-step Source 8 hook.
      Returns ONE compact evidence item (the shape Sources 1-6 use:
      {"claim","source","value"}) summarizing what already exists, so the REASON
      step knows NOT to recommend building it. Read-only at gather time; reads
      the ledger if present, else falls back to the in-code curated list so it
      works even before the seed has run.

SAFETY (this runs inside a LIVE autonomous system)
--------------------------------------------------
  · READ-ONLY at gather time. The ONLY write anywhere in this module is
    CREATE TABLE IF NOT EXISTS + the idempotent seed UPSERT, and that runs ONCE
    at startup (register/init), never during an investigation.
  · NO arbitrary / model-generated SQL. Every statement here is a pre-written
    literal; the seed is parameterized (execute_values / %s), never f-string
    interpolated.
  · BEST-EFFORT. gather_live_capabilities() and the seed both try/except every
    DB touch and degrade to "source unavailable" / a no-op — they NEVER raise
    and NEVER block gather_evidence()'s synchronous ~48s investigation.
  · FLAG-GATED. The GATHER hook only emits when BRAIN_CAPABILITY_LEDGER_ENABLED
    is truthy. (The seed itself is harmless — a tiny CREATE + UPSERT — so it
    runs at init regardless; if the flag is off, the ledger simply sits unused.)
  · HONEST. The ledger does NOT over-claim. `persist_command` is recorded as
    LIVE (its original "DARK" framing in the brain's own recommendation was
    OUTDATED — verified live at server.mjs:1427). Flag-gated and inert items are
    labeled as such, not dressed up as shipped.

WIRING (main.py)
----------------
  from routes.brain_capability_ledger import register_capability_ledger
  register_capability_ledger(app)   # seeds once at startup; never raises

  The Investigator's gather_evidence() calls gather_live_capabilities() behind
  the BRAIN_CAPABILITY_LEDGER_ENABLED flag (wired separately in
  brain_investigator.py — NOT edited here).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


# ── env / flags ──────────────────────────────────────────────────────
def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _enabled() -> bool:
    """The GATHER-step Source 8 hook is gated by BRAIN_CAPABILITY_LEDGER_ENABLED.
    Default OFF (dark) — gather_live_capabilities() returns [] until an operator
    flips the flag, matching the Source 7/8 flag convention."""
    return _truthy(os.environ.get("BRAIN_CAPABILITY_LEDGER_ENABLED"))


# ── DB (direct psycopg2 — mirrors brain_investigator._conn) ──────────
def _conn():
    """Raw psycopg2 connection (DATABASE_URL / NEON_DATABASE_URL). Direct, NOT
    safe_db: safe_db SKIPs DDL under SKIP_DDL=1, which would silently drop the
    CREATE TABLE. Best-effort: returns None on any failure, never raises."""
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if dsn:
            return _pg.connect(dsn, sslmode="require", connect_timeout=6)
    except Exception as e:
        logger.warning("brain_capability_ledger: _conn failed: %s", e)
    return None


# ── The CURATED, HONEST capability list ──────────────────────────────
# Verified item-by-item against the LIVE gateway source
# (the dchub-mcp-server repo's server.mjs). Each tuple is
# (name, location, status, note). status ∈ {LIVE, FLAG-GATED, INERT}.
#   LIVE       = on the default code path / a registered tool (treat as BUILT).
#   FLAG-GATED = behind an env toggle; default recorded in the note.
#   INERT      = present but OFF / not enforced (NOT a gap — already considered).
# The brain should treat every LIVE (and ON-by-default FLAG-GATED) item as
# ALREADY BUILT and STOP recommending it.
_CURATED_CAPABILITIES: list[tuple[str, str, str, str]] = [
    # ── 2026-08-12: BRAIN-INTERNAL capability. ★Everything below this block was
    # customer-facing gateway surface only, so the ledger was blind to the
    # brain's own engineering capability — and in one audit session on
    # 2026-08-11 the SAME THREE capabilities were proposed as missing work by a
    # human reviewer who had read the codebase: the loop graph, fix-history
    # recall, and an authenticated RAG retrieve. All three were already shipped.
    # This ledger exists to stop exactly that and could not, because it did not
    # know they existed. Re-proposal is cheap to prevent, expensive to repeat.
    ("loop dependency graph (producer→consumer edges on the loop board)",
     "routes/graph_master_shell.py LOOP_EDGES; routes/system_loops.py "
     "apply_loop_edges/count_alive_on_stale_input/order_producers_first",
     "LIVE",
     "Shell #49 (2026-08-02). Every row of /api/v1/system/loops carries "
     "input_status (ok|stale|unknown|no_declared_input), so a loop running on "
     "dead input no longer reports a green board, and the babysitter heals the "
     "PRODUCER first. Do NOT propose 'add loop edges' — the remaining gap is "
     "COVERAGE: 3 of 7 probed loops still declare no input (2026-08-11)."),

    ("fix-history recall (prior fixes retrieved at investigate time)",
     "routes/brain_rag.py:1401 retrieve_prior_fixes; called by "
     "routes/brain_investigator.py:949; ingest at "
     "POST /api/v1/admin/brain/rag/ingest-fix-history",
     "LIVE",
     "gh_issue + commit + resolved_finding docs embedded in "
     "brain_corpus_embeddings under source_table='fix_history'. Deliberately "
     "NOT in PUBLIC_CORPORA — it holds internal issues and commit bodies. "
     "intelligence_expansion_master_shell already fails a lane if nobody calls "
     "it, so 'wire up fix history' is DONE, not a gap."),

    ("authenticated RAG retrieve over ANY corpus (incl. brain-internal)",
     "routes/brain_rag.py:1633 /api/v1/admin/brain/rag/retrieve?q=&k=&corpus=",
     "LIVE",
     "The admin path passes `corpus` through with NO PUBLIC_CORPORA filter "
     "(:1644), so corpus=fix_history works today with X-Admin-Key. ★The public "
     "/api/v1/rag/search DOES filter to PUBLIC_CORPORA (:1816) and returns "
     "count:0 for engineering queries — reading that zero as 'the capability is "
     "missing' is how this got re-proposed. An empty result from a surface you "
     "cannot see into is UNKNOWN, not ABSENT."),

    ("internal-probe envelope (instrument-failed vs measured-empty)",
     "util/internal_fetch.py probe/health_of; shell #63 "
     "routes/context_integrity_master_shell.py; GET /admin/context-integrity",
     "LIVE",
     "PR #2596 (2026-08-11). _internal() used to collapse a timeout, a 500 and "
     "an honest empty into one bare {}; 17 of 20 live L18 lessons were the "
     "brain re-learning that. probe() returns {ok,data,reason,status,empty} and "
     "L14 ships ctx.context_health. Lane 2 of #63 is the standing meter."),

    # 1. The exact surface the brain kept recommending — it's LIVE, not dark.
    ("persist_command / durable-key-at-first-call",
     "server.mjs:1380 buildAutoMintBlock (literal @1427); paywall paths @2397,2604",
     "LIVE",
     "Emits structuredContent.persist_command + persist_hint "
     "('claude mcp add dchub --header X-API-Key:...') on every successful "
     "paywall auto-mint, shipping the durable key on the FIRST call. This is "
     "the EXACT surface the brain recommended building — it already exists. "
     "(The brain's own prior 'DARK' framing was OUTDATED; verified live.)"),

    # 2. Trial auto-bind to session ("wow on call #1", no reconnect).
    ("trial auto-bind to session (full data inline on call #1)",
     "server.mjs:792 _autoBindTrialToSession; gate _anonInlineFullEnabled @1342/1346",
     "FLAG-GATED",
     "DCHUB_ANON_INLINE_FULL default ON. Anon first-touch on a flagship "
     "trial-taste tool gets FULL data inline + a minted key bound to the "
     "session (no reconnect). 'off' = 1-row taste + add-header copy."),

    # 3. claim_free_key — instant no-email durable key, auto-bound.
    ("claim_free_key (instant no-email durable key, auto-bound)",
     "server.mjs:3670 trackedTool; mints via /api/v1/keys/claim",
     "LIVE",
     "Mints a free durable key with no email and auto-binds it to the session "
     "(r86) so the next call is full-tier with no reconnect. Optional email "
     "arg makes the key recoverable."),

    # 4. bind_email — recoverable key + receipt routing, consent-aware.
    ("bind_email (recoverable key + receipt routing, consent-aware)",
     "server.mjs:3805 trackedTool",
     "LIVE",
     "Optional; the key works unbound. Marketing opt-in defaults OFF; the "
     "consent/purpose line is in the tool description."),

    # 5. recover_my_key.
    ("recover_my_key (re-issue key tied to an email)",
     "server.mjs:3872 trackedTool",
     "LIVE",
     "Recovers / re-issues a key tied to an email address."),

    # 6. unlock_more_data — one-click checkout relay.
    ("unlock_more_data (one-click checkout relay)",
     "server.mjs:3910 trackedTool",
     "LIVE",
     "Returns one-click Stripe links ($10 / 1,000 API calls one-time pack, Developer "
     "$49). Used as the next_tool throughout the upgrade hints."),

    # 7. Depth-tease _upgrade — flagship-synthesis full depth = Developer+.
    ("depth-tease _upgrade (full depth = Developer+, sub-dev = top-3 + upgrade)",
     "server.mjs:1121-1206 buildDepthTease/_teaseDepth; wired @2800 (10 tools)",
     "LIVE",
     "DEPTH_TEASE_TOOLS (10 flagship-synthesis tools), DEPTH_TEASE_KEEP=3. "
     "Sub-dev callers get top-3 + an _upgrade routed through unlock_more_data; "
     "leads with the $10 / 1,000 API calls pack."),

    # 8. "1 of N" deprivation preamble.
    ("'1 of N' deprivation preamble (quantified-deprivation conversion lever)",
     "server.mjs:1514-1529 trimForTrial; anon header @1680",
     "LIVE",
     "'You're seeing 1 of N results…' + 'answered using 1 of 300+ markets' anon "
     "header. The quantified-deprivation conversion lever (r-unlock)."),

    # 9. next_session retention hook.
    ("next_session retention hook (machine-discoverable comeback path)",
     "server.mjs:1845 _NEXT_SESSION + 1852 _withNextSession; stamped @1875/1887",
     "LIVE",
     "structuredContent.next_session (get_changes since=24h + save_site / "
     "set_site_alert / set_market_alert) stamped on EVERY full-data response. "
     "structuredContent-only, no prose."),

    # 10. _bind structuredContent hint on full-data grid/fiber/market responses.
    ("_bind structuredContent hint (anon/free nudge toward bind_email)",
     "server.mjs:1091 withBindHint; wired @2813 (BIND_CTA_TOOLS)",
     "LIVE",
     "Lightweight structured nudge toward bind_email on full-data grid/fiber/"
     "market responses for anon/unidentified-free callers."),

    # 11. Map-CTA upsell (in-tool link to the live Land & Power map).
    ("map-CTA upsell (in-tool link to the live Land & Power map)",
     "server.mjs:1053 MAP_TOOLS + 1062 mapHref; depth-tease @1189-1191",
     "LIVE",
     "Appends map_cta / map_url / map_relay (logged 302 via /api/v1/go/map) on "
     "map-feeding tools."),

    # ── PRESENT but OFF / inert — recorded so the brain does NOT 'discover'
    #    these as gaps and recommend building them. ──────────────────────
    ("claim-success durability carrot (CLAIM_CAROT_COPY)",
     "server.mjs:1113",
     "FLAG-GATED",
     "CLAIM_CAROT_COPY default ON — claim-success durability carrot copy."),

    ("anonymous per-IP daily soft cap (ANON_DAILY_CAP)",
     "server.mjs:1287 ANON_DAILY_CAP",
     "INERT",
     "DCHUB_ANON_DAILY_CAP default 0 = OFF / NOT enforced. Present but inert — "
     "not a missing feature, a deliberately-off one."),

    ("per-platform tool descriptions",
     "server.mjs:2133 DCHUB_PER_PLATFORM_DESC_DISABLE",
     "FLAG-GATED",
     "Per-platform tool descriptions; disable via "
     "DCHUB_PER_PLATFORM_DESC_DISABLE (default enabled)."),
]


def init_capability_ledger_schema() -> None:
    """Create brain_live_capabilities via DIRECT psycopg2 (safe_db SKIPs DDL
    under SKIP_DDL=1). Idempotent; never raises. This is the ONLY DDL write in
    the module and the ONLY write the safety rails permit."""
    conn = _conn()
    if conn is None:
        logger.warning("brain_capability_ledger: no DB; skipping schema init")
        return
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS brain_live_capabilities (
                        name       TEXT PRIMARY KEY,
                        location   TEXT,
                        status     TEXT,
                        note       TEXT,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
        logger.info("brain_capability_ledger: schema ready")
    except Exception as e:
        logger.warning("brain_capability_ledger: schema init failed: %s", e)
    finally:
        try: conn.close()
        except Exception: pass


def seed_capability_ledger() -> int:
    """Idempotently UPSERT the curated capability list into the ledger
    (ON CONFLICT (name) DO UPDATE so re-seeds refresh location/status/note and
    bump updated_at). Parameterized — NO f-string SQL. Best-effort: returns the
    number of rows written, or 0 on any failure. Never raises."""
    if not _CURATED_CAPABILITIES:
        return 0
    conn = _conn()
    if conn is None:
        logger.warning("brain_capability_ledger: no DB; skipping seed")
        return 0
    written = 0
    try:
        rows = [(name, loc, status, note)
                for (name, loc, status, note) in _CURATED_CAPABILITIES]
        with conn.cursor() as cur:
            sql = (
                "INSERT INTO brain_live_capabilities "
                "(name, location, status, note, updated_at) "
                "VALUES (%s, %s, %s, %s, NOW()) "
                "ON CONFLICT (name) DO UPDATE SET "
                "location = EXCLUDED.location, "
                "status = EXCLUDED.status, "
                "note = EXCLUDED.note, "
                "updated_at = NOW()"
            )
            try:
                from psycopg2.extras import execute_batch
                execute_batch(cur, sql, rows, page_size=50)
            except Exception:
                # Fall back to per-row inserts if execute_batch is unavailable.
                for r in rows:
                    cur.execute(sql, r)
            conn.commit()
            written = len(rows)
        logger.info("brain_capability_ledger: seeded %d capabilities", written)
    except Exception as e:
        logger.warning("brain_capability_ledger: seed failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return 0
    finally:
        try: conn.close()
        except Exception: pass
    return written


def _read_ledger() -> list[tuple[str, str, str]]:
    """Read (name, status, location) from the ledger, ORDER BY status then name
    so LIVE items sort first. Read-only, LIMITed, fast (≤16 tiny rows, PK scan).
    Best-effort: returns [] on any error so the caller can fall back to the
    in-code curated list. Never raises."""
    conn = _conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, status, location FROM brain_live_capabilities "
                "ORDER BY status ASC, name ASC LIMIT 64"
            )
            return [(r[0], r[1], r[2]) for r in (cur.fetchall() or [])]
    except Exception as e:
        logger.warning("brain_capability_ledger: read failed: %s", e)
        try: conn.rollback()
        except Exception: pass
        return []
    finally:
        try: conn.close()
        except Exception: pass


def _summarize(items: list[tuple[str, str, str]]) -> str:
    """Build the compact 'ALREADY BUILT — do not recommend' value string from
    (name, status, location) rows. LIVE items lead; flag-gated/inert follow as a
    short tail so REASON sees they're already considered, not gaps."""
    live = [n for (n, st, _loc) in items if (st or "").upper() == "LIVE"]
    flagged = [n for (n, st, _loc) in items
               if (st or "").upper() in ("FLAG-GATED", "INERT")]
    # 2026-06-20: lead with the capabilities the brain has actually mis-flagged
    # as build-gaps (durable-key / first-call / persist / retention), so they
    # survive the 200-char evidence-block value clip even in the tail — the
    # claim field (never clipped) already names persist_command; this makes the
    # value robust too.
    _PRI = ("persist", "durable", "first-call", "first call", "claim_free_key",
            "bind_email", "retention", "next_session", "key")

    def _rank(name: str):
        nl = (name or "").lower()
        return (0 if any(k in nl for k in _PRI) else 1, nl)
    live.sort(key=_rank)
    flagged.sort(key=_rank)
    parts: list[str] = []
    if live:
        parts.append("ALREADY LIVE (do NOT recommend building): " + "; ".join(live))
    if flagged:
        parts.append("present but flag-gated/inert (already considered, NOT gaps): "
                     + "; ".join(flagged))
    return " | ".join(parts)


def gather_live_capabilities() -> list[dict]:
    """GATHER-step Source 8. Return a compact evidence item summarizing what the
    platform ALREADY ships, so the REASON step does not recommend building it.

    Shape matches Sources 1-6: a single {"claim","source","value"} dict in a
    list (or [] when the flag is off / nothing to report).

    READ-ONLY: reads the ledger if present; falls back to the in-code curated
    list so it works even before the seed has run. Best-effort — [] on any
    error, NEVER raises, NEVER blocks gather_evidence()."""
    if not _enabled():
        return []
    try:
        items = _read_ledger()
        if not items:
            # Fall back to the in-code curated list (ledger not seeded yet).
            items = [(name, status, loc)
                     for (name, loc, status, _note) in _CURATED_CAPABILITIES]
        if not items:
            return []
        summary = _summarize(items)
        if not summary:
            return []
        live_n = sum(1 for (_n, st, _l) in items if (st or "").upper() == "LIVE")
        return [{
            "claim": (
                "Capabilities the platform ALREADY ships (MCP gateway) — the "
                "REASON step must NOT recommend building any of these; they "
                "exist. Includes the durable-key-on-first-call surface "
                "(persist_command) the brain has repeatedly mis-flagged as a gap."
            ),
            "source": "brain_live_capabilities ledger (verified vs dchub-mcp-server/server.mjs)",
            "value": f"{live_n} live capabilities — {summary}",
        }]
    except Exception as e:
        logger.warning("brain_capability_ledger: gather failed: %s", e)
        return []


def init_capability_ledger() -> None:
    """Create the table then seed it idempotently. Best-effort; never raises.
    Called once at startup by register_capability_ledger()."""
    try:
        init_capability_ledger_schema()
    except Exception as e:
        logger.warning("brain_capability_ledger: schema init skipped: %s", e)
    try:
        seed_capability_ledger()
    except Exception as e:
        logger.warning("brain_capability_ledger: seed skipped: %s", e)


def register_capability_ledger(app=None) -> None:
    """Idempotent registration helper for main.py. Seeds the ledger ONCE at
    startup (CREATE TABLE IF NOT EXISTS + idempotent UPSERT). Takes an optional
    `app` for call-site symmetry with the other register_* helpers; there is no
    blueprint to register (this module is a GATHER-step source, not an
    endpoint). Best-effort — never raises."""
    try:
        init_capability_ledger()
    except Exception as e:
        logger.warning("brain_capability_ledger: register/seed skipped: %s", e)
