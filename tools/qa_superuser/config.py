#!/usr/bin/env python3
"""Endpoints, seats and credentials for the QA super-user.

Credentials come from the environment only. The four seats are separate on
purpose — "gated for anon" and "gated for a paying key" are different facts, and
a probe run from the wrong seat proves nothing (shell #49).
"""
from __future__ import annotations

import os

from .finding import SEAT_ADMIN, SEAT_ANON, SEAT_PAID


def _env(name: str, default: str = "") -> str:
    # ★ .strip() on every env value that becomes a URL or a header: a trailing
    # newline in DCHUB_INTERNAL_API became %0a and raised InvalidURL at urlopen().
    return (os.environ.get(name) or default).strip()


# ── surfaces ────────────────────────────────────────────────────────────────
# The public edge is what a real caller hits, so that is what we probe. The
# Railway origin is used ONLY where the edge is known to be structurally unable
# to serve the request (admin POSTs die on the zone's 15s route timeout -> 503).
EDGE = _env("QA_EDGE_BASE", "https://dchub.cloud").rstrip("/")
ORIGIN = _env("QA_ORIGIN_BASE",
              "https://dchub-backend-production.up.railway.app").rstrip("/")
MCP_URL = _env("DCHUB_MCP_URL", "https://dchub.cloud/mcp").rstrip("/")

# ── credentials, by seat ────────────────────────────────────────────────────
PAID_KEY = _env("DCHUB_REVIEWER_KEY")     # a real paying key
ADMIN_KEY = _env("DCHUB_ADMIN_KEY")
FREE_KEY = _env("DCHUB_API_KEY")          # free/starter tier, if provisioned

SEATS = {
    SEAT_ANON: None,
    SEAT_PAID: PAID_KEY or None,
    SEAT_ADMIN: ADMIN_KEY or None,
}


def seat_available(seat: str) -> bool:
    """True when we hold a credential for this seat.

    A missing credential is BLIND, never RED — we did not observe the seat, we
    were merely unable to sit in it.
    """
    if seat == SEAT_ANON:
        return True
    return bool(SEATS.get(seat))


# ── the flagship tools we exercise as a real agent ──────────────────────────
# get_market_intel is the platform's #1 tool by call volume (18,629 calls/30d at
# the shell-#38 measurement) — if anything is worth watching from the caller's
# seat every few hours, it is this one.
FLAGSHIP_TOOL = "get_market_intel"
FLAGSHIP_ARGS = {"market": "ashburn"}

# A second, structurally different tool so a single tool's quirk cannot masquerade
# as a platform-wide verdict.
SECOND_TOOL = "rank_markets"
SECOND_ARGS = {"limit": 5}

# ── the anonymous daily cap, and why the meter check needs a ROTATING tool ────
#
# ★★ THE ANON BUDGET IS KEYED ON (client_ip, TOOL, utc-day) — NOT ON THE SESSION.
#
# The probe used to open a fresh MCPSession before any budget-dependent check and
# call that "a caller WITH trial budget". That premise is false, and it cost this
# board four days of signal (2026-08-04 → 08-07): the quota-movement check
# reported "NOT RE-CONFIRMED" on every one of ~24 consecutive runs and nobody
# could see why, because a spent meter was being filed as a GAUGE rather than as
# the unobserved measurement it actually was.
#
# Measured from mcp-server `server.mjs`:
#
#     _trialFullRemaining(ipKey, tool, cap):
#         _trialDayCounts.get(`${ipKey}:${tool}:${day}`)      // ← ip, TOOL, day
#     const TRIAL_DAILY_FULL_CAP = parseInt(DCHUB_TRIAL_TOOL_DAILY_FULL || '2')
#
# and confirmed live on 2026-08-07 from two different sessions and two different
# User-Agents on one egress IP: the second session inherited
# `full_answers_remaining_today: 0`. A new session buys nothing. A new DAY or a
# new TOOL does.
#
# So the meter-movement check rotates the tool it spends. The cap is 2 calls per
# tool per IP per day and the harness runs 6x/day, which means:
#
#   * one FIXED tool can never be observed after the day's first run, but
#   * a DIFFERENT capped tool each run lands on a fresh 2-call budget every time.
#
# ★ get_market_intel is deliberately EXCLUDED from this pool. It is FLAGSHIP_TOOL
#   and TIER_PROBE_CALLS, so the other anon checks already spend both of its
#   daily calls before the meter check runs — picking it would guarantee the very
#   blindness this rotation exists to end.
#
# Membership mirrors ALWAYS_PARTIAL_PREVIEW in server.mjs (the set of tools the
# cap actually governs). A tool that leaves that set stops being metered and this
# check will correctly report it as unmetered rather than broken.
# Two DIFFERENT argument sets per tool, so an unchanged meter cannot be explained
# away as a cached response. Every one of these tools declares `required: []`.
METERED_TOOLS = [
    ("get_grid_intelligence", {"iso": "ERCOT"}, {"iso": "PJM"}),
    ("get_fiber_intel", {"market": "ashburn"}, {"market": "dallas"}),
    ("rank_markets", {"limit": 5}, {"limit": 7}),
    ("ai_capacity_index", {"limit": 5}, {"limit": 7}),
    ("get_gas_intelligence", {"state": "TX"}, {"state": "PA"}),
]

# The documented front door. The server's own instructions say to call this first
# for any multi-capability question, so a broken front door is a critical defect.
FRONT_DOOR_TOOL = "execute_plan"
FRONT_DOOR_ARGS = {"intent": "rank markets for a 200 MW AI campus"}

# Tools sampled for the tier-self-report check. get_energy_prices leads because
# it is where the defect was found: a keyless session was told
# `caller_tier: 'pro'` in the same envelope that gated it to a 1-result preview.
# The other two are structurally different tools, so one handler's quirk cannot
# masquerade as a platform-wide verdict.
TIER_PROBE_CALLS = [
    ("get_energy_prices", {"state": "TX"}),
    ("get_market_intel", {"market": "ashburn"}),
    ("get_iso_context", {"iso": "ERCOT"}),
]

# Public pages every visitor and crawler touches.
#
# ★ /press and the catalog pages were added 2026-08-05, after /press spent hours
#   in an infinite 308 loop (ERR_TOO_MANY_REDIRECTS in a real browser) and this
#   list never noticed — because /press was not on it. The catalog pages were
#   added for the same reason: /operators served "0 tracked" under index,follow
#   for months.
PUBLIC_PAGES = [
    "/",
    "/agent",
    "/pricing",
    "/ai",
    "/mcp-standing",
    "/press",
    "/markets",
    "/operators",
    "/state-of-power",
]

# ── numbers a public surface publishes, and the live endpoint that owns each ──
#
# ★ Every entry here is a number that WAS wrong on a live page: /pricing said
#   "81 MCP tools" against a live 82 and "15,000+ facilities" against a canon
#   floor of 16,500+, and the homepage published two different facility floors
#   at once. Each was found by a human reading the page. Nothing in the harness
#   compared a published number to its source.
#
# ★ The threshold is not invented — `/api/v1/canon/phrases` IS the platform's
#   own declared floor, and house doctrine is that a floor ROUNDS DOWN. So a
#   page may publish LESS than canon (conservative, honest) but never MORE
#   (an over-claim), and never two different values for one population.
CANON_PHRASES_PATH = "/api/v1/canon/phrases"

# (page, canon key, regex capturing the published number)
PUBLISHED_NUMBERS = [
    ("/pricing", "tools", r"(\d+)\s+MCP\s+tools"),
    ("/pricing", "facilities", r"([\d,]+)\+\s+facilit"),
    ("/", "facilities", r"([\d,]+)\+\s+facilit"),
]

# ── catalog pages that must never publish a zero ────────────────────────────
# (path, the live endpoint whose count proves the page is lying)
CATALOG_PAGES = [
    ("/operators", "/api/v1/operators"),
]

# An operator known to be tracked, used to prove the brief surface resolves.
# equinix is the seed the platform hand-QA's; /api/v1/operators/equinix reported
# 543 facilities at the same moment /api/v1/operator-brief/equinix answered
# "operator_not_found".
BRIEF_PROBE_OPERATOR = "equinix"

# Admin surfaces that must be closed to crawlers. /admin returned HTTP 200 to
# Googlebot with three of four indexation signals saying "index".
ADMIN_SURFACES = ["/admin"]

GOOGLEBOT_UA = ("Mozilla/5.0 (compatible; Googlebot/2.1; "
                "+http://www.google.com/bot.html)")

# Machine-readable discovery surfaces. These are how agents and AI crawlers find
# us at all, so a 404 here is invisible-but-total.
DISCOVERY_PATHS = [
    "/robots.txt",
    "/llms.txt",
    "/sitemap.xml",
    "/.well-known/mcp.json",
]

# Timeouts. Generous — a slow answer is a finding, a timeout is BLIND, and we
# would rather wait than manufacture blindness.
HTTP_TIMEOUT = int(_env("QA_HTTP_TIMEOUT", "30"))
MCP_TIMEOUT = int(_env("QA_MCP_TIMEOUT", "90"))

# Repo + state
GH_REPO = _env("GH_REPO", "azmartone67/dchub-backend")
STATE_BRANCH = _env("QA_STATE_BRANCH", "qa-superuser-state")
STATE_PATH = _env("QA_STATE_PATH", "state/board.json")
ISSUE_LABEL = "qa-superuser"

DRY_RUN = bool(_env("QA_DRY_RUN"))
