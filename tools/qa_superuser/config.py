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

# Public pages every visitor and crawler touches.
PUBLIC_PAGES = [
    "/",
    "/agent",
    "/pricing",
    "/ai",
    "/mcp-standing",
]

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
