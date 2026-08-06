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

# ── does the paywall actually bite? ─────────────────────────────────────────
#
# ★ THE MOST EXPENSIVE UNMEASURED NUMBER ON THE PLATFORM. On 2026-07-28 a manual
#   audit measured 11 gated vs 9,031 granted on payable tools — 0.12%, i.e. no
#   effective paywall — and that number went on to shape strategy for a week. A
#   trial cap shipped 2026-07-31 and changed it. Nobody re-measured; the stale
#   figure was still being quoted on 08-05 as the reason agents do not pay.
#
# ★ NO INVENTED THRESHOLD. The envelope publishes its own
#   `quota.full_answers_cap_today`. This check spends exactly that many calls,
#   then asserts the NEXT one is smaller. The platform declares the cap; we only
#   check it is honoured.
#
# ★ A neutral clientInfo.name is REQUIRED. The rollup excludes client names with
#   a `dchub-` prefix or `-probe`/`-health`/`-scanner`/`-checker` suffix, so a
#   probe named like a probe is correctly ignored and the rail looks broken.
PAYWALL_PROBE_TOOL = "get_grid_intelligence"
PAYWALL_PROBE_ARGS = {"market": "ashburn"}
PAYWALL_PROBE_CLIENT = "acme-siting-agent"
# Hard stop, so a mis-read cap can never turn this check into a load generator.
PAYWALL_PROBE_MAX_CALLS = 6

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
