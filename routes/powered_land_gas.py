"""Powered Land — Gas Pricing PRO surface (Phase 1 spine). 2026-06-04.
=====================================================================

Four-layer gas economics per DCPI market — the value-add no competitor
publishes at this granularity:

  1. Henry Hub spot ($/MMBtu)
  2. Regional basis differential at the nearest pricing hub (Algonquin
     Citygate / Transco Z6 / Tetco M3 / Chicago Citygate / SoCal Border /
     Waha / Dominion South / etc.)
  3. Delivered industrial/electric-power $/MMBtu (from eia_gas_prices,
     state-keyed)
  4. Heat-rate-derived gas-to-grid $/MWh — 5 scenarios:
       - new CCGT     (6,400 Btu/kWh)
       - avg CCGT     (6,800 Btu/kWh)
       - old CCGT     (7,500 Btu/kWh)
       - peaker       (10,500 Btu/kWh)
       - old peaker   (12,000 Btu/kWh)

Endpoints (Phase 1):

  GET  /api/v1/markets/<slug>/gas-pricing     — full 4-layer payload
                                                  (PRO; free = teaser)
  GET  /api/v1/markets/<slug>/gas-to-grid     — $/MWh derivation
                                                  (PRO; free = teaser)
  GET  /api/v1/markets/gas-pricing/methodology — fully public
  POST /api/v1/markets/gas-pricing/cron       — admin-gated refresh

Tier gating: imports routes.auth_context.get_auth_context and gates
the numeric payload on `is_at_least('pro')`. Free / Identified / Starter
callers see hub identification + a verdict + the same response shape with
numeric fields nulled and an `upgrade_hint` block. Anonymous gets the same
free-tier teaser (no key required to read methodology / structure).

Data source: EIA v2 API for Henry Hub + select regional spot indices via
eia_api.make_eia_request(). If EIA_API_KEY is not set the fetcher returns
a stub with `data_basis: 'synthetic_seed'` and recent representative
values rather than 500'ing — Phase 1 must ship without a live key.

Phase 2 (DEFERRED — documented here for the methodology endpoint):
  - per-utility delivered tariff scrapes (currently we use state-level
    EIA industrial/electric tariff averages from eia_gas_prices)
  - ICE / NYMEX forward curves for forward $/MMBtu scenarios
  - Intraday hub pricing (currently daily settle)
  - Real-time pipeline outage / OFO impact on basis
"""
from __future__ import annotations

import json
import os
import sys
import time
import hashlib
from datetime import datetime, timezone
from typing import Any, Optional

from flask import Blueprint, jsonify, request

# ── gas-to-grid $/MWh kill switch (2026-08-08) ──────────────────────────────
# The heat-rate arithmetic in gas_to_grid_usd_per_mwh() is correct and stays.
# What was wrong is the INPUT: five surfaces each picked the burner-tip price
# by a different rule and disagreed by up to 5.5x on the same market on the
# same day, with no sanity gate — this endpoint served a physically impossible
# $6.73/MWh for Phoenix stamped data_basis: "live". util/gas_index.py has the
# measurements. Nothing here is deleted; the flag decides what is published.
from util.gas_index import (
    gas_to_grid_enabled, gas_to_grid_unavailable, GAS_TO_GRID_FIELDS,
    WITHDRAWAL_HTTP_STATUS,
)

powered_land_gas_bp = Blueprint("powered_land_gas", __name__)

# ── Constants ────────────────────────────────────────────────────────

# Heat rates (Btu/kWh) for gas-to-grid $/MWh derivation. Phase 1 fixed
# scenarios; gas-to-grid endpoint also accepts a custom heat_rate_btu_per_kwh
# query param for what-if modelling.
HEAT_RATE_BTU_PER_KWH = {
    "combined_cycle_new":   6_400,    # H-class CCGT, 2024+ vintage
    "combined_cycle_avg":   6_800,    # fleet-average modern CCGT
    "combined_cycle_old":   7_500,    # F-class
    "simple_cycle_peaker": 10_500,    # aero-derivative peaker
    "simple_cycle_old":   12_000,     # older frame peakers
}

# Synthetic-seed fallback values used when EIA_API_KEY is not configured.
# Representative of Q2 2026 published values; flagged with
# data_basis='synthetic_seed' so callers don't mistake them for live.
SYNTHETIC_HENRY_HUB_USD_MMBTU = 2.95
SYNTHETIC_BASIS_BY_HUB = {
    # Hub key → typical basis differential vs Henry Hub (USD/MMBtu).
    # Positive = premium (Algonquin winter), negative = discount (Waha).
    "henry_hub":            0.00,
    "algonquin_citygate":   2.20,
    "transco_z6":           0.85,
    "tetco_m3":             0.20,
    "dominion_south":      -0.05,
    "transco_z5":           0.40,
    "chicago_citygate":     0.10,
    "florida_gas":          0.55,
    "houston_ship_channel": 0.05,
    "waha":                -0.85,
    "socal_border":         0.65,
    "malin":                0.30,
    "opal":                -0.20,
    "cheyenne_hub":        -0.15,
    "panhandle_eastern":   -0.30,
}

# State → primary pricing hub mapping. Primary hub is the most quoted
# index for that state's distribution / interstate take. Sub-state nuance
# (PA-east → Transco Z6, PA-west → Tetco M3) is documented but resolved
# at the market level via _resolve_market_hub() if a market name implies it.
_STATE_TO_HUB = {
    "MA": "algonquin_citygate", "CT": "algonquin_citygate",
    "RI": "algonquin_citygate", "NH": "algonquin_citygate",
    "VT": "algonquin_citygate", "ME": "algonquin_citygate",
    "NY": "transco_z6",         "NJ": "transco_z6",
    "PA": "tetco_m3",           "OH": "tetco_m3",
    "WV": "tetco_m3",
    "VA": "dominion_south",     "MD": "dominion_south",
    "DE": "dominion_south",     "DC": "dominion_south",
    "NC": "transco_z5",         "SC": "transco_z5",
    "GA": "transco_z5",
    "IL": "chicago_citygate",   "IN": "chicago_citygate",
    "WI": "chicago_citygate",   "MI": "chicago_citygate",
    "MN": "chicago_citygate",   "IA": "chicago_citygate",
    "LA": "henry_hub",          "MS": "henry_hub",
    "AR": "henry_hub",          "AL": "henry_hub",
    "TN": "henry_hub",          "KY": "henry_hub",
    "MO": "henry_hub",
    "TX": "houston_ship_channel",
    "NM": "waha",
    "OK": "panhandle_eastern",
    "KS": "panhandle_eastern",  "NE": "panhandle_eastern",
    "SD": "cheyenne_hub",       "ND": "cheyenne_hub",
    "CO": "cheyenne_hub",
    "WY": "opal",               "UT": "opal",
    "ID": "opal",               "MT": "opal",
    "CA": "socal_border",       "AZ": "socal_border",
    "NV": "socal_border",
    "OR": "malin",              "WA": "malin",
    "FL": "florida_gas",
    "HI": "henry_hub",          "AK": "henry_hub",  # no real hub; HH proxy
}

_HUB_NAME = {
    "algonquin_citygate":   "Algonquin Citygate",
    "transco_z6":           "Transco Zone 6 NY",
    "tetco_m3":             "Tetco M3",
    "dominion_south":       "Dominion South Point",
    "chicago_citygate":     "Chicago Citygate",
    "henry_hub":            "Henry Hub",
    "houston_ship_channel": "Houston Ship Channel",
    "waha":                 "Waha (Permian)",
    "socal_border":         "SoCal Border (Topock/Ehrenberg)",
    "malin":                "Malin (PG&E)",
    "opal":                 "Opal (Rockies)",
    "cheyenne_hub":         "Cheyenne Hub",
    "panhandle_eastern":    "Panhandle Eastern",
    "florida_gas":          "FGT Zone 3",
    "transco_z5":           "Transco Zone 5",
}

# Market slugs with explicit hub overrides (sub-state nuance, port-of-entry
# pricing, etc.). Phase 1 covers the marquee data-center markets; expand as
# Phase 2 lands. Anything not in this map falls back to _STATE_TO_HUB.
_MARKET_HUB_OVERRIDE = {
    "northern-virginia":      "dominion_south",
    "ashburn":                "dominion_south",
    "loudoun":                "dominion_south",
    "dallas-fort-worth":      "houston_ship_channel",
    "dallas":                 "houston_ship_channel",
    "houston":                "houston_ship_channel",
    "austin":                 "houston_ship_channel",
    "san-antonio":            "houston_ship_channel",
    "phoenix":                "socal_border",
    "los-angeles":            "socal_border",
    "san-diego":              "socal_border",
    "silicon-valley":         "malin",
    "san-francisco":          "malin",
    "santa-clara":            "malin",
    "portland":               "malin",
    "seattle":                "malin",
    "atlanta":                "transco_z5",
    "charlotte":              "transco_z5",
    "raleigh":                "transco_z5",
    "chicago":                "chicago_citygate",
    "columbus":               "tetco_m3",
    "boston":                 "algonquin_citygate",
    "new-york":               "transco_z6",
    "new-jersey":             "transco_z6",
    "northern-new-jersey":    "transco_z6",
    "miami":                  "florida_gas",
    "orlando":                "florida_gas",
    "denver":                 "cheyenne_hub",
    "salt-lake-city":         "opal",
    "las-vegas":              "socal_border",
    "kansas-city":            "panhandle_eastern",
    "minneapolis":            "chicago_citygate",
    "detroit":                "chicago_citygate",
    "indianapolis":           "chicago_citygate",
    "philadelphia":           "transco_z6",
    "pittsburgh":             "tetco_m3",
    "richmond":               "dominion_south",
    "nashville":              "henry_hub",
    "memphis":                "henry_hub",
    "new-orleans":            "henry_hub",
    "oklahoma-city":          "panhandle_eastern",
    "tulsa":                  "panhandle_eastern",
}

# Markets without a clean state lookup get an explicit state. Bare minimum
# — most slugs resolve via heuristic (split on '-', last token == state).
_MARKET_STATE_OVERRIDE = {
    "northern-virginia":      "VA",
    "silicon-valley":         "CA",
    "dallas-fort-worth":      "TX",
    "san-francisco":          "CA",
    "santa-clara":            "CA",
    "new-york":               "NY",
    "new-jersey":             "NJ",
    "northern-new-jersey":    "NJ",
    "salt-lake-city":         "UT",
    "las-vegas":              "NV",
    "kansas-city":            "MO",
    "oklahoma-city":          "OK",
    "san-antonio":            "TX",
    "los-angeles":            "CA",
    "san-diego":              "CA",
    "ashburn":                "VA",
    "loudoun":                "VA",
    "phoenix":                "AZ",
    "atlanta":                "GA",
    "chicago":                "IL",
    "boston":                 "MA",
    "miami":                  "FL",
    "orlando":                "FL",
    "denver":                 "CO",
    "seattle":                "WA",
    "portland":               "OR",
    "minneapolis":            "MN",
    "detroit":                "MI",
    "indianapolis":           "IN",
    "philadelphia":           "PA",
    "pittsburgh":             "PA",
    "richmond":               "VA",
    "nashville":              "TN",
    "memphis":                "TN",
    "new-orleans":            "LA",
    "tulsa":                  "OK",
    "columbus":               "OH",
    "houston":                "TX",
    "dallas":                 "TX",
    "austin":                 "TX",
    "charlotte":              "NC",
    "raleigh":                "NC",
}

# ── DB helpers ───────────────────────────────────────────────────────


def _conn():
    """Open a psycopg2 connection the CALLER fully owns (closes in its finally).

    r-conn-gc (2026-07-03): the prior body did `_iso_common.conn().__enter__()`.
    conn() is a @contextmanager whose `finally` closes the socket; calling
    `__enter__()` WITHOUT retaining the context-manager object left the closer
    generator unreferenced, so the GC finalized it and closed the connection
    MID-USE. That surfaced as "connection already closed" on the delivered-price
    reads (→ delivered_industrial/electric came back None in every payload) AND
    on every `_upsert_market` write (→ upsert_failed for all 38 markets, so
    market_gas_pricing stayed empty despite the EIA key being set and the cron
    running green). Fix: connect DIRECTLY so the connection's lifetime is bound
    only to the caller's own `finally`, not to a GC-vulnerable abandoned
    generator. Reuse `_iso_common.dsn()` for the same endpoint selection.
    """
    try:
        import psycopg2 as _pg
        try:
            from routes._iso_common import dsn as _dsn
            _d = _dsn()
        except Exception:
            _d = ""
        if not _d:
            _d = (os.environ.get("DATABASE_URL")
                  or os.environ.get("NEON_DATABASE_URL") or "")
        if _d:
            return _pg.connect(_d)
    except Exception:
        pass
    return None


# ── Auth ────────────────────────────────────────────────────────────


def _is_pro(req) -> bool:
    """True if the caller's tier is >= pro. Internal/admin = always True."""
    try:
        from routes.auth_context import get_auth_context, TIER_PRO
        ctx = get_auth_context(req)
        return ctx.is_at_least(TIER_PRO)
    except Exception as e:
        print(f"[powered_land_gas] auth resolve failed: {e}", file=sys.stderr)
        return False


def _admin_ok(req) -> bool:
    """X-Admin-Key gate for the nightly cron."""
    sent = (req.headers.get("X-Admin-Key")
            or req.headers.get("X-Internal-Key")
            or req.args.get("admin_key") or "").strip()
    if not sent:
        return False
    for env_name in ("DCHUB_ADMIN_KEY", "ADMIN_KEY",
                     "DCHUB_INTERNAL_KEY", "INTERNAL_KEY"):
        expected = (os.environ.get(env_name) or "").strip()
        if expected and sent == expected:
            return True
    return False


# ── Market resolution ────────────────────────────────────────────────


def _slug_norm(slug: str) -> str:
    return (slug or "").strip().lower().replace("_", "-")


def _resolve_market_state(slug: str) -> Optional[str]:
    """Best-effort market_slug -> state. Tries override map, then last
    token heuristic, then DB lookup against dcpi_markets / dcpi_metro_scores
    if available."""
    s = _slug_norm(slug)
    if s in _MARKET_STATE_OVERRIDE:
        return _MARKET_STATE_OVERRIDE[s]
    parts = s.split("-")
    if parts and len(parts[-1]) == 2 and parts[-1].isalpha():
        return parts[-1].upper()
    # DB fallback — best-effort, must not 500
    c = _conn()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute("SET statement_timeout = 3000")
            for tbl, col_slug, col_state in (
                ("dcpi_markets",       "slug",   "state"),
                ("dcpi_metro_scores",  "slug",   "state"),
                ("market_gas_pricing", "market_slug", "state"),
            ):
                try:
                    cur.execute(
                        f"SELECT {col_state} FROM {tbl} WHERE LOWER({col_slug}) = %s LIMIT 1",
                        (s,),
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        return str(row[0]).strip().upper()[:2]
                except Exception:
                    try:
                        c.rollback()
                    except Exception:
                        pass
    finally:
        try:
            c.close()
        except Exception:
            pass
    return None


def _resolve_market_hub(slug: str, state: Optional[str]) -> tuple[str, str]:
    """Return (hub_key, hub_display_name) for a market. Override map wins."""
    s = _slug_norm(slug)
    if s in _MARKET_HUB_OVERRIDE:
        hub = _MARKET_HUB_OVERRIDE[s]
        return hub, _HUB_NAME.get(hub, hub)
    if state and state.upper() in _STATE_TO_HUB:
        hub = _STATE_TO_HUB[state.upper()]
        return hub, _HUB_NAME.get(hub, hub)
    return "henry_hub", _HUB_NAME["henry_hub"]


def _market_display_name(slug: str) -> str:
    s = _slug_norm(slug)
    return " ".join(p.capitalize() for p in s.split("-")) or s


# ── EIA fetchers (best-effort) ───────────────────────────────────────


def _eia_key_present() -> bool:
    return bool((os.environ.get("EIA_API_KEY") or "").strip())


def _fetch_henry_hub_spot() -> tuple[Optional[float], str, str]:
    """Return (spot $/MMBtu, source, period). Best-effort.

    If EIA_API_KEY is unset, return the synthetic-seed value with
    source='synthetic_seed' so callers can render — no 500.
    """
    if not _eia_key_present():
        return (SYNTHETIC_HENRY_HUB_USD_MMBTU,
                "synthetic_seed", _today_str())
    try:
        from eia_api import make_eia_request
        # EIA v2: natural-gas/pri/fut Henry Hub (RNGWHHD) daily spot.
        params = {
            "frequency": "daily",
            "data[0]":  "value",
            "facets[series][]": "RNGWHHD",
            "sort[0][column]":    "period",
            "sort[0][direction]": "desc",
            "length": 1,
        }
        data, err = make_eia_request("natural-gas/pri/fut/data", params)
        if err or not data:
            return (SYNTHETIC_HENRY_HUB_USD_MMBTU,
                    "synthetic_seed_eia_unreachable", _today_str())
        try:
            rows = (data.get("response") or {}).get("data") or []
            if rows:
                val = float(rows[0].get("value") or 0)
                period = str(rows[0].get("period") or _today_str())
                if val > 0:
                    return (round(val, 3),
                            "eia_v2_natural-gas/pri/fut", period)
        except Exception:
            pass
        return (SYNTHETIC_HENRY_HUB_USD_MMBTU,
                "synthetic_seed_eia_empty", _today_str())
    except Exception as e:
        print(f"[powered_land_gas] HH fetch failed: {e}", file=sys.stderr)
        return (SYNTHETIC_HENRY_HUB_USD_MMBTU,
                "synthetic_seed_exception", _today_str())


def _basis_for_hub(hub_key: str) -> tuple[Optional[float], str]:
    """Phase 1: serve basis from the synthetic seed map. Phase 2 will
    integrate a live regional spot feed (EIA wholesale weekly + ICE).
    Returns (basis $/MMBtu vs HH, source)."""
    if hub_key in SYNTHETIC_BASIS_BY_HUB:
        return (SYNTHETIC_BASIS_BY_HUB[hub_key], "synthetic_seed_basis")
    return (0.0, "synthetic_seed_basis_fallback")


def _delivered_industrial(state: Optional[str]) -> tuple[Optional[float], str, Optional[str]]:
    """Latest industrial $/MMBtu for state. Returns (price, source, period)."""
    if not state:
        return (None, "no_state", None)
    c = _conn()
    if c is None:
        return (None, "no_db", None)
    try:
        with c.cursor() as cur:
            cur.execute("SET statement_timeout = 3000")
            cur.execute(
                """
                SELECT price, period
                  FROM eia_gas_prices
                 WHERE UPPER(state) = %s
                   AND price IS NOT NULL AND price > 0
                   AND (sector ILIKE %s OR sector = %s)
                 ORDER BY period DESC
                 LIMIT 1
                """,
                (state.upper(), "%indus%", "PIN"),
            )
            row = cur.fetchone()
            if row and row[0] is not None:
                return (round(float(row[0]), 3),
                        "eia_gas_prices", str(row[1]) if row[1] else None)
    except Exception as e:
        print(f"[powered_land_gas] delivered_industrial err: {e}",
              file=sys.stderr)
        try:
            c.rollback()
        except Exception:
            pass
    finally:
        try:
            c.close()
        except Exception:
            pass
    return (None, "eia_gas_prices_missing", None)


def _delivered_electric(state: Optional[str]) -> tuple[Optional[float], str, Optional[str]]:
    """Latest electric-power-sector $/MMBtu for state."""
    if not state:
        return (None, "no_state", None)
    c = _conn()
    if c is None:
        return (None, "no_db", None)
    try:
        with c.cursor() as cur:
            cur.execute("SET statement_timeout = 3000")
            cur.execute(
                """
                SELECT price, period
                  FROM eia_gas_prices
                 WHERE UPPER(state) = %s
                   AND price IS NOT NULL AND price > 0
                   AND (sector ILIKE %s OR sector = %s)
                 ORDER BY period DESC
                 LIMIT 1
                """,
                (state.upper(), "%electric%", "PEU"),
            )
            row = cur.fetchone()
            if row and row[0] is not None:
                return (round(float(row[0]), 3),
                        "eia_gas_prices", str(row[1]) if row[1] else None)
    except Exception as e:
        print(f"[powered_land_gas] delivered_electric err: {e}",
              file=sys.stderr)
        try:
            c.rollback()
        except Exception:
            pass
    finally:
        try:
            c.close()
        except Exception:
            pass
    return (None, "eia_gas_prices_missing", None)


# ── Heat-rate calc ───────────────────────────────────────────────────


def gas_to_grid_usd_per_mwh(gas_price_usd_mmbtu: float,
                            heat_rate_btu_per_kwh: float) -> Optional[float]:
    """Compute marginal $/MWh from gas price + heat rate.

    Formula:
      $/MWh = (heat_rate_btu_per_kwh / 1_000_000) * gas_price * 1000
            = (heat_rate_btu_per_kwh * gas_price) / 1000

    Returns None if either input is missing / nonpositive.
    """
    try:
        gp = float(gas_price_usd_mmbtu)
        hr = float(heat_rate_btu_per_kwh)
    except (TypeError, ValueError):
        return None
    if gp <= 0 or hr <= 0:
        return None
    return round((hr * gp) / 1000.0, 2)


def _gas_to_grid_scenarios(gas_price: Optional[float]) -> dict:
    """Return the 5 standard $/MWh scenarios for a given gas price."""
    if gas_price is None or gas_price <= 0:
        return {k: None for k in HEAT_RATE_BTU_PER_KWH}
    return {
        k: gas_to_grid_usd_per_mwh(gas_price, hr)
        for k, hr in HEAT_RATE_BTU_PER_KWH.items()
    }


# ── Cache row helpers ────────────────────────────────────────────────


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_cached(slug: str) -> Optional[dict]:
    """Read the most-recent cached row for a market, if fresh enough."""
    c = _conn()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute("SET statement_timeout = 3000")
            cur.execute(
                """
                SELECT market_slug, market_name, state,
                       pricing_hub_key, pricing_hub_name,
                       henry_hub_spot_usd_mmbtu, basis_diff_usd_mmbtu,
                       hub_spot_usd_mmbtu,
                       delivered_industrial_usd_mmbtu,
                       delivered_electric_usd_mmbtu,
                       usd_per_mwh_cc_new, usd_per_mwh_cc_avg,
                       usd_per_mwh_cc_old, usd_per_mwh_peaker,
                       usd_per_mwh_peaker_old,
                       hh_source, basis_source, delivered_source,
                       data_basis, fetched_at, period
                  FROM market_gas_pricing
                 WHERE market_slug = %s
                """,
                (slug,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [
                "market_slug", "market_name", "state",
                "pricing_hub_key", "pricing_hub_name",
                "henry_hub_spot_usd_mmbtu", "basis_diff_usd_mmbtu",
                "hub_spot_usd_mmbtu",
                "delivered_industrial_usd_mmbtu",
                "delivered_electric_usd_mmbtu",
                "usd_per_mwh_cc_new", "usd_per_mwh_cc_avg",
                "usd_per_mwh_cc_old", "usd_per_mwh_peaker",
                "usd_per_mwh_peaker_old",
                "hh_source", "basis_source", "delivered_source",
                "data_basis", "fetched_at", "period",
            ]
            out = {}
            for k, v in zip(cols, row):
                if v is None:
                    out[k] = None
                elif hasattr(v, "isoformat"):
                    out[k] = v.isoformat()
                else:
                    try:
                        out[k] = float(v) if k.startswith(("henry_", "basis_",
                                                           "hub_spot", "delivered_",
                                                           "usd_per_mwh_")) else v
                    except Exception:
                        out[k] = v
            return out
    except Exception as e:
        print(f"[powered_land_gas] _read_cached err: {e}", file=sys.stderr)
        try:
            c.rollback()
        except Exception:
            pass
    finally:
        try:
            c.close()
        except Exception:
            pass
    return None


def _compute_market_payload(slug: str) -> dict:
    """Compute the full payload for a market (live recompute, no cache).
    Always returns a complete dict — synthetic-seed fallback values fill
    any blanks so the UI never sees nulls cascading."""
    slug = _slug_norm(slug)
    state = _resolve_market_state(slug)
    hub_key, hub_name = _resolve_market_hub(slug, state)
    hh, hh_src, hh_period = _fetch_henry_hub_spot()
    basis, basis_src = _basis_for_hub(hub_key)
    hub_spot = None
    if hh is not None and basis is not None:
        hub_spot = round(hh + basis, 3)
    ind, ind_src, ind_period = _delivered_industrial(state)
    elec, elec_src, elec_period = _delivered_electric(state)
    delivered_basis = (
        ind if ind is not None
        else elec if elec is not None
        else hub_spot
    )
    grid_scenarios = _gas_to_grid_scenarios(delivered_basis)
    # ★2026-08-07: data_basis is NOT decided here any more. It used to be
    #   `"live" if hh_src.startswith("eia_") and delivered is not None`, which
    #   ignored layer 2 entirely — so a market with a live Henry Hub read and a
    #   live delivered tariff was stamped "live" while basis_diff_usd_mmbtu was
    #   still the hardcoded SYNTHETIC_BASIS_BY_HUB constant. Phoenix served
    #   basis_diff_usd_mmbtu: 0.65 / basis_source: "synthetic_seed_basis" /
    #   data_basis: "live". The envelope label is now derived from the actual
    #   per-field sources by _apply_honest_basis(), at SERVE time, so a cached
    #   row written under the old rule cannot carry a stale "live" out the door.
    return {
        "market_slug":             slug,
        "market_name":             _market_display_name(slug),
        "state":                   state,
        "pricing_hub_key":         hub_key,
        "pricing_hub_name":        hub_name,
        "henry_hub_spot_usd_mmbtu": hh,
        "basis_diff_usd_mmbtu":    basis,
        "hub_spot_usd_mmbtu":      hub_spot,
        "delivered_industrial_usd_mmbtu": ind,
        "delivered_electric_usd_mmbtu":   elec,
        "usd_per_mwh_cc_new":      grid_scenarios.get("combined_cycle_new"),
        "usd_per_mwh_cc_avg":      grid_scenarios.get("combined_cycle_avg"),
        "usd_per_mwh_cc_old":      grid_scenarios.get("combined_cycle_old"),
        "usd_per_mwh_peaker":      grid_scenarios.get("simple_cycle_peaker"),
        "usd_per_mwh_peaker_old":  grid_scenarios.get("simple_cycle_old"),
        "hh_source":               hh_src,
        "basis_source":            basis_src,
        "delivered_source":        ind_src if ind is not None else elec_src,
        "data_basis":              _envelope_basis_from_sources(
                                        hh_src, basis_src,
                                        ind_src if ind is not None else elec_src),
        "period":                  ind_period or elec_period or hh_period or _today_str(),
        "fetched_at":              datetime.now(timezone.utc).isoformat(),
    }


def _upsert_market(payload: dict) -> bool:
    """Insert / update the cached row. Returns True on success."""
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("SET statement_timeout = 5000")
            cur.execute(
                """
                INSERT INTO market_gas_pricing (
                    market_slug, market_name, state,
                    pricing_hub_key, pricing_hub_name,
                    henry_hub_spot_usd_mmbtu, basis_diff_usd_mmbtu,
                    hub_spot_usd_mmbtu,
                    delivered_industrial_usd_mmbtu,
                    delivered_electric_usd_mmbtu,
                    usd_per_mwh_cc_new, usd_per_mwh_cc_avg,
                    usd_per_mwh_cc_old, usd_per_mwh_peaker,
                    usd_per_mwh_peaker_old,
                    hh_source, basis_source, delivered_source,
                    data_basis, fetched_at, period
                ) VALUES (
                    %(market_slug) ON CONFLICT DO NOTHINGs, %(market_name)s, %(state)s,
                    %(pricing_hub_key)s, %(pricing_hub_name)s,
                    %(henry_hub_spot_usd_mmbtu)s, %(basis_diff_usd_mmbtu)s,
                    %(hub_spot_usd_mmbtu)s,
                    %(delivered_industrial_usd_mmbtu)s,
                    %(delivered_electric_usd_mmbtu)s,
                    %(usd_per_mwh_cc_new)s, %(usd_per_mwh_cc_avg)s,
                    %(usd_per_mwh_cc_old)s, %(usd_per_mwh_peaker)s,
                    %(usd_per_mwh_peaker_old)s,
                    %(hh_source)s, %(basis_source)s, %(delivered_source)s,
                    %(data_basis)s, NOW(), %(period)s
                )
                ON CONFLICT (market_slug) DO UPDATE SET
                    market_name             = EXCLUDED.market_name,
                    state                   = EXCLUDED.state,
                    pricing_hub_key         = EXCLUDED.pricing_hub_key,
                    pricing_hub_name        = EXCLUDED.pricing_hub_name,
                    henry_hub_spot_usd_mmbtu = EXCLUDED.henry_hub_spot_usd_mmbtu,
                    basis_diff_usd_mmbtu    = EXCLUDED.basis_diff_usd_mmbtu,
                    hub_spot_usd_mmbtu      = EXCLUDED.hub_spot_usd_mmbtu,
                    delivered_industrial_usd_mmbtu = EXCLUDED.delivered_industrial_usd_mmbtu,
                    delivered_electric_usd_mmbtu   = EXCLUDED.delivered_electric_usd_mmbtu,
                    usd_per_mwh_cc_new      = EXCLUDED.usd_per_mwh_cc_new,
                    usd_per_mwh_cc_avg      = EXCLUDED.usd_per_mwh_cc_avg,
                    usd_per_mwh_cc_old      = EXCLUDED.usd_per_mwh_cc_old,
                    usd_per_mwh_peaker      = EXCLUDED.usd_per_mwh_peaker,
                    usd_per_mwh_peaker_old  = EXCLUDED.usd_per_mwh_peaker_old,
                    hh_source               = EXCLUDED.hh_source,
                    basis_source            = EXCLUDED.basis_source,
                    delivered_source        = EXCLUDED.delivered_source,
                    data_basis              = EXCLUDED.data_basis,
                    fetched_at              = NOW(),
                    period                  = EXCLUDED.period
                """,
                payload,
            )
        c.commit()
        return True
    except Exception as e:
        print(f"[powered_land_gas] _upsert_market err: {e}", file=sys.stderr)
        try:
            c.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            c.close()
        except Exception:
            pass


# ── Honest per-field provenance + freshness (2026-08-07) ────────────
#
# The DCGI withdrawal notice points analysts at THIS endpoint as the surface
# that is still published. It was itself mislabelled: layer 2 (regional basis)
# has never been a measurement — _basis_for_hub() returns a constant out of
# SYNTHETIC_BASIS_BY_HUB — yet the envelope said data_basis: "live" whenever
# layers 1 and 3 happened to come from EIA. A modelled estimate presented as
# measured is the exact defect class the 2026-08 audit was about.
#
# Rules enforced below:
#   1. A field whose *_source is synthetic / seed / default / fallback / stub
#      is NEVER labelled "live". It gets its own per-field label.
#   2. The envelope label is the WEAKEST label across the fields actually
#      published — "live" only when every published number is measured.
#   3. Provenance is per-field (`field_basis`), not just at the envelope.
#   4. fetched_at carries an age and a stated staleness threshold.
#
# This runs at SERVE time on both the cached row and a fresh compute, because
# market_gas_pricing rows written before this change carry data_basis='live'
# in the column and must not be able to leak that label to a caller.

# Substrings that mark a source string as NOT a measurement.
_SYNTHETIC_SOURCE_MARKERS = (
    "synthetic", "seed", "fallback", "default", "stub", "placeholder",
)
# Source strings that mean "we have nothing", not "we made it up".
_UNAVAILABLE_SOURCES = ("", "no_state", "no_db", "none", "null", "unavailable")
# ...and substrings that mean the same. _delivered_industrial() returns
# (None, "eia_gas_prices_missing", None) when the state has no row — the feed
# NAME is real, so an exact-match list let it read as a live EIA source and
# dallas published `delivered_industrial_usd_mmbtu: basis=live` on a NULL.
# Caught on the live probe of this fix; absence is not a measurement.
_UNAVAILABLE_SOURCE_MARKERS = (
    "_missing", "not_found", "no_row", "no_data", "unavailable", "_error",
)

# Weakest-wins ordering for the envelope roll-up.
_BASIS_RANK = {"live": 3, "modeled": 2, "synthetic": 1, "unavailable": 0}

# Nightly refresh fires at 04:00 UTC. 36h allows exactly one missed run
# before the value stops being something an analyst should lean on.
GAS_PRICING_STALE_AFTER_HOURS = 36.0

# field → the *_source column that decides its label.
_FIELD_SOURCE_KEY = {
    "henry_hub_spot_usd_mmbtu":       "hh_source",
    "basis_diff_usd_mmbtu":           "basis_source",
    "delivered_industrial_usd_mmbtu": "delivered_source",
    "delivered_electric_usd_mmbtu":   "delivered_source",
}

# Derived fields inherit the WEAKEST label of their inputs — hub_spot is
# henry_hub + basis, so a synthetic basis makes hub_spot synthetic too.
_FIELD_DERIVED_FROM = {
    "hub_spot_usd_mmbtu":     ("hh_source", "basis_source"),
    "usd_per_mwh_cc_new":     ("hh_source", "basis_source", "delivered_source"),
    "usd_per_mwh_cc_avg":     ("hh_source", "basis_source", "delivered_source"),
    "usd_per_mwh_cc_old":     ("hh_source", "basis_source", "delivered_source"),
    "usd_per_mwh_peaker":     ("hh_source", "basis_source", "delivered_source"),
    "usd_per_mwh_peaker_old": ("hh_source", "basis_source", "delivered_source"),
}

_FIELD_NOTES = {
    "basis_diff_usd_mmbtu": (
        "Phase-1 SYNTHETIC SEED: a hardcoded typical differential vs Henry Hub "
        "for this pricing hub, not a live spot index read. Do not present this "
        "as a measured basis. Live regional spot is Phase 2."
    ),
    "hub_spot_usd_mmbtu": (
        "Derived as henry_hub_spot + basis_diff. Inherits the synthetic basis, "
        "so it is a modelled hub price, not a measured settle."
    ),
}


def _source_basis(src: Optional[str]) -> str:
    """Classify ONE source string into live / synthetic / unavailable.

    Anything carrying a synthetic/seed/default marker is synthetic, full stop —
    including 'synthetic_seed_eia_unreachable', which is a seed constant served
    because a live read failed and is emphatically not a live read.
    """
    s = (src or "").strip().lower()
    if s in _UNAVAILABLE_SOURCES:
        return "unavailable"
    if any(m in s for m in _UNAVAILABLE_SOURCE_MARKERS):
        return "unavailable"
    if any(m in s for m in _SYNTHETIC_SOURCE_MARKERS):
        return "synthetic"
    return "live"


def _weakest(labels) -> str:
    """Weakest-wins roll-up. 'unavailable' inputs are skipped (the derived
    value is already None); if everything is unavailable, so is the result."""
    real = [l for l in labels if l != "unavailable"]
    if not real:
        return "unavailable"
    return min(real, key=lambda l: _BASIS_RANK.get(l, 0))


def _envelope_basis_from_sources(hh_src, basis_src, delivered_src) -> str:
    """Envelope data_basis for the three primary source columns.

    'live' requires every published layer to be measured. Mixed reads are
    labelled 'mixed' — never 'live' — so an analyst cannot read the envelope
    and conclude the basis differential was observed.
    """
    labels = [_source_basis(hh_src), _source_basis(basis_src),
              _source_basis(delivered_src)]
    real = [l for l in labels if l != "unavailable"]
    if not real:
        return "unavailable"
    if all(l == "live" for l in real):
        return "live"
    if any(l == "live" for l in real):
        return "mixed"
    return "synthetic_seed"


def _age_hours(ts: Any) -> Optional[float]:
    """Hours between `ts` (ISO string or datetime) and now, UTC. None if
    unparseable — an unknown age is reported as unknown, not as zero."""
    if ts is None:
        return None
    try:
        if hasattr(ts, "tzinfo"):
            dt = ts
        else:
            s = str(ts).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return round(delta.total_seconds() / 3600.0, 2)
    except Exception:
        return None


def _apply_honest_basis(payload: dict) -> dict:
    """Relabel a gas-pricing payload in place: per-field provenance, an
    envelope label that cannot outrank its weakest field, and a stated age.

    Mutates and returns `payload`. Safe on a payload whose $/MWh fields have
    already been popped by the gas-to-grid kill switch — only keys that are
    actually present get a field_basis entry.
    """
    hh_src = payload.get("hh_source")
    basis_src = payload.get("basis_source")
    del_src = payload.get("delivered_source")
    src_by_key = {"hh_source": hh_src, "basis_source": basis_src,
                  "delivered_source": del_src}

    field_basis: dict = {}
    published: list = []

    for field, src_key in _FIELD_SOURCE_KEY.items():
        if field not in payload:
            continue
        label = _source_basis(src_by_key.get(src_key))
        # ★ A field with no value cannot be a live measurement, whatever its
        #   source column says. Backstop for source strings like
        #   'eia_gas_prices_missing' that name a real feed to say it had
        #   nothing — dallas served basis=live on a NULL delivered price.
        if payload.get(field) is None:
            label = "unavailable"
        entry = {"basis": label, "source": src_by_key.get(src_key)}
        if field in _FIELD_NOTES:
            entry["note"] = _FIELD_NOTES[field]
        field_basis[field] = entry
        published.append(label)

    for field, inputs in _FIELD_DERIVED_FROM.items():
        if field not in payload:
            continue
        label = _weakest([_source_basis(src_by_key.get(k)) for k in inputs])
        if payload.get(field) is None:
            label = "unavailable"
        entry = {"basis": label, "derived_from": list(inputs),
                 "source": "derived"}
        if field in _FIELD_NOTES:
            entry["note"] = _FIELD_NOTES[field]
        field_basis[field] = entry
        published.append(label)

    payload["field_basis"] = field_basis

    # Envelope: weakest-wins over the fields actually published.
    real = [l for l in published if l != "unavailable"]
    if not real:
        envelope = "unavailable"
    elif all(l == "live" for l in real):
        envelope = "live"
    elif any(l == "live" for l in real):
        envelope = "mixed"
    else:
        envelope = "synthetic_seed"
    payload["data_basis"] = envelope

    synthetic_fields = sorted(
        k for k, v in field_basis.items() if v.get("basis") == "synthetic"
    )
    payload["synthetic_fields"] = synthetic_fields
    if synthetic_fields:
        payload["data_basis_note"] = (
            "NOT fully measured. These fields are modelled or seeded, not "
            "observed: " + ", ".join(synthetic_fields) + ". See field_basis "
            "for the per-field source. The envelope label is the weakest "
            "per-field label, so it can never read 'live' while a seed "
            "constant is being served."
        )
    else:
        payload["data_basis_note"] = (
            "Every published field is sourced from a live feed; see "
            "field_basis for the per-field source."
        )

    # ── Freshness: an age and a threshold, not a bare timestamp ──────────
    age = _age_hours(payload.get("fetched_at"))
    payload["age_hours"] = age
    payload["staleness_threshold_hours"] = GAS_PRICING_STALE_AFTER_HOURS
    if age is None:
        payload["as_of_age"] = "unknown"
        payload["is_stale"] = None
        payload["staleness_note"] = (
            "fetched_at could not be parsed, so the age of this row is "
            "UNKNOWN. Treat it as unreliable until it refreshes."
        )
    else:
        payload["as_of_age"] = f"{age:.1f}h"
        payload["is_stale"] = age > GAS_PRICING_STALE_AFTER_HOURS
        payload["staleness_note"] = (
            "Refreshed nightly at 04:00 UTC, so an age up to ~24h is normal. "
            f"Beyond {GAS_PRICING_STALE_AFTER_HOURS:.0f}h (one missed refresh) "
            "these values should not be relied on for a pricing decision."
            + ("" if not payload["is_stale"] else
               f" THIS ROW IS STALE at {age:.1f}h.")
        )
    return payload


# ── Free-tier trim ──────────────────────────────────────────────────


_NUMERIC_FIELDS = (
    "henry_hub_spot_usd_mmbtu", "basis_diff_usd_mmbtu",
    "hub_spot_usd_mmbtu",
    "delivered_industrial_usd_mmbtu", "delivered_electric_usd_mmbtu",
    "usd_per_mwh_cc_new", "usd_per_mwh_cc_avg",
    "usd_per_mwh_cc_old", "usd_per_mwh_peaker",
    "usd_per_mwh_peaker_old",
)


def _trim_for_free(payload: dict) -> dict:
    """Mask numeric fields for free-tier callers but keep structure +
    hub identification + verdict so the UI can still render the layout."""
    out = dict(payload)
    for k in _NUMERIC_FIELDS:
        out[k] = None
    out["upgrade_hint"] = {
        "tier_required": "pro",
        "human_message": (
            "Full 4-layer gas pricing + heat-rate $/MWh derivation is a "
            "PRO feature. You're seeing the hub identification and verdict. "
            "Upgrade for the numeric values across all 300+ DCPI markets."
        ),
        "stripe_url": "https://buy.stripe.com/00w28o7BqaXLeP31QIaZi04",
        "signup_url": "https://dchub.cloud/pricing",
    }
    # A sample masked teaser-string so the UI can show a placeholder
    out["teaser_sample"] = "$X.XX/MMBtu — PRO unlock"
    return out


# ── Routes ──────────────────────────────────────────────────────────


@powered_land_gas_bp.route(
    "/api/v1/markets/<slug>/gas-pricing", methods=["GET", "OPTIONS"]
)
def market_gas_pricing(slug):
    """Full 4-layer gas pricing for a DCPI market. PRO-gated."""
    if request.method == "OPTIONS":
        return ("", 204)

    slug = _slug_norm(slug)
    if not slug or len(slug) > 80:
        return jsonify(ok=False, error="bad_slug"), 400

    payload = _read_cached(slug) or _compute_market_payload(slug)
    payload["ok"] = True
    payload["surface"] = "powered-land-gas-pricing"

    # ★ This endpoint is named "gas-pricing" but its cached row carries five
    #   usd_per_mwh_* columns, so it was a gas-to-grid $/MWh surface too —
    #   one of the five that disagreed. The $/MMBtu layers below are sourced
    #   EIA tariff reads and stay; only the $/MWh derivation is withdrawn.
    if not gas_to_grid_enabled():
        for _k in GAS_TO_GRID_FIELDS:
            payload.pop(_k, None)
        payload["gas_to_grid_status"] = gas_to_grid_unavailable()

    # ★ Relabel AFTER the kill switch has removed fields, so field_basis
    #   describes exactly what is on the wire, and BEFORE the free-tier trim,
    #   so provenance survives masking. A cached row carries the pre-2026-08-07
    #   data_basis column, which this overwrites — see _apply_honest_basis.
    _apply_honest_basis(payload)

    # Verdict — concrete, useful even at free tier
    if payload.get("hub_spot_usd_mmbtu") is not None:
        hs = float(payload["hub_spot_usd_mmbtu"])
        if hs < 2.5:
            payload["verdict"] = "GAS-ADVANTAGED"
        elif hs < 4.5:
            payload["verdict"] = "ADEQUATE"
        else:
            payload["verdict"] = "GAS-CONSTRAINED"
    else:
        payload["verdict"] = "UNKNOWN"
    # The verdict is a threshold on hub_spot, so it is exactly as measured as
    # hub_spot is — which, while basis is a seed constant, is "synthetic".
    payload["verdict_basis"] = (
        (payload.get("field_basis") or {})
        .get("hub_spot_usd_mmbtu", {})
        .get("basis", "unavailable")
    )

    if not _is_pro(request):
        payload = _trim_for_free(payload)
        payload["ok"] = True
        payload["surface"] = "powered-land-gas-pricing"
        # Re-attach verdict + hub info (already kept by _trim_for_free)

    resp = jsonify(payload)
    # PRO callers see fresh data; anon hits the gated payload; no edge cache.
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


@powered_land_gas_bp.route(
    "/api/v1/markets/<slug>/gas-to-grid", methods=["GET", "OPTIONS"]
)
def market_gas_to_grid(slug):
    """Heat-rate-derived $/MWh scenarios. PRO-gated.

    Optional query: heat_rate_btu_per_kwh=<float> adds a custom scenario.
    """
    if request.method == "OPTIONS":
        return ("", 204)

    slug = _slug_norm(slug)
    if not slug or len(slug) > 80:
        return jsonify(ok=False, error="bad_slug"), 400

    if not gas_to_grid_enabled():
        _out = gas_to_grid_unavailable("powered-land-gas-to-grid")
        _out["market_slug"] = slug
        _resp = jsonify(_out)
        _resp.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"
        # ★★★200, NOT 503 — see WITHDRAWAL_HTTP_STATUS in util/gas_index.py.
        return _resp, WITHDRAWAL_HTTP_STATUS

    base = _apply_honest_basis(_read_cached(slug) or _compute_market_payload(slug))
    gas_price = (
        base.get("delivered_electric_usd_mmbtu")
        or base.get("delivered_industrial_usd_mmbtu")
        or base.get("hub_spot_usd_mmbtu")
        or base.get("henry_hub_spot_usd_mmbtu")
    )

    scenarios = {
        "new_ccgt_6400_btu_kwh":   gas_to_grid_usd_per_mwh(gas_price, 6_400),
        "avg_ccgt_6800_btu_kwh":   gas_to_grid_usd_per_mwh(gas_price, 6_800),
        "old_ccgt_7500_btu_kwh":   gas_to_grid_usd_per_mwh(gas_price, 7_500),
        "peaker_10500_btu_kwh":    gas_to_grid_usd_per_mwh(gas_price, 10_500),
        "old_peaker_12000_btu_kwh": gas_to_grid_usd_per_mwh(gas_price, 12_000),
    }

    custom = None
    try:
        hr_arg = request.args.get("heat_rate_btu_per_kwh")
        if hr_arg is not None:
            hr_val = float(hr_arg)
            if 4_000 < hr_val < 25_000:
                custom = {
                    "heat_rate_btu_per_kwh": hr_val,
                    "usd_per_mwh": gas_to_grid_usd_per_mwh(gas_price, hr_val),
                }
    except Exception:
        custom = None

    payload = {
        "ok":                True,
        "surface":           "powered-land-gas-to-grid",
        "market_slug":       slug,
        "market_name":       base.get("market_name"),
        "state":             base.get("state"),
        "gas_price_basis":   "delivered_electric_then_industrial_then_hub_then_hh",
        "gas_price_used_usd_mmbtu": gas_price,
        "scenarios_usd_per_mwh": scenarios,
        "custom_scenario":   custom,
        "formula":           "$/MWh = (heat_rate_btu_per_kwh * gas_$/MMBtu) / 1000",
        "data_basis":        base.get("data_basis"),
        "data_basis_note":   base.get("data_basis_note"),
        "field_basis":       base.get("field_basis"),
        "synthetic_fields":  base.get("synthetic_fields"),
        "fetched_at":        base.get("fetched_at"),
        "age_hours":         base.get("age_hours"),
        "as_of_age":         base.get("as_of_age"),
        "is_stale":          base.get("is_stale"),
        "staleness_threshold_hours": base.get("staleness_threshold_hours"),
        "staleness_note":    base.get("staleness_note"),
    }

    if not _is_pro(request):
        # Free tier: keep ONE scenario as a teaser, mask the rest
        keep = "avg_ccgt_6800_btu_kwh"
        teaser_scenarios = {k: (v if k == keep else None)
                            for k, v in scenarios.items()}
        # Mask even the kept scenario to a "$X.XX" string so it's clearly gated
        teaser_scenarios[keep] = (
            f"~${teaser_scenarios[keep]:.0f}/MWh (illustrative — unlock for live)"
            if teaser_scenarios[keep] is not None
            else None
        )
        payload["scenarios_usd_per_mwh"] = teaser_scenarios
        payload["gas_price_used_usd_mmbtu"] = None
        payload["custom_scenario"] = None
        payload["upgrade_hint"] = {
            "tier_required": "pro",
            "human_message": (
                "Heat-rate-derived $/MWh across 5 scenarios is a PRO feature. "
                "Upgrade to unlock the full table + custom heat-rate input."
            ),
            "stripe_url": "https://buy.stripe.com/00w28o7BqaXLeP31QIaZi04",
            "signup_url": "https://dchub.cloud/pricing",
        }

    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


@powered_land_gas_bp.route(
    "/api/v1/markets/gas-pricing/methodology", methods=["GET", "OPTIONS"]
)
def gas_pricing_methodology():
    """Fully public methodology + Phase-2 roadmap."""
    if request.method == "OPTIONS":
        return ("", 204)
    return jsonify({
        "ok": True,
        "surface": "powered-land-gas-methodology",
        "version": "phase-1-spine-2026-06-04",
        "layers": [
            {"layer": 1, "name": "Henry Hub spot",
             "source": "EIA v2 natural-gas/pri/fut (RNGWHHD daily)",
             "unit": "$/MMBtu"},
            {"layer": 2, "name": "Regional basis differential",
             "source": "Synthetic seed (Phase 1) → EIA wholesale-weekly + "
                       "ICE forward curves (Phase 2)",
             "unit": "$/MMBtu vs HH",
             "data_basis": "synthetic",
             "warning": "NOT MEASURED. Layer 2 is a hardcoded typical "
                        "differential per pricing hub. Every field derived "
                        "from it (hub_spot_usd_mmbtu, the verdict, the $/MWh "
                        "scenarios) is therefore modelled, and the market "
                        "payload labels it so per-field in `field_basis`."},
            {"layer": 3, "name": "Delivered industrial / electric-power",
             "source": "eia_gas_prices (state-keyed, EIA bulk loader)",
             "unit": "$/MMBtu"},
            {"layer": 4, "name": "Gas-to-grid $/MWh derivation",
             "source": "Heat-rate × delivered $/MMBtu / 1000",
             "unit": "$/MWh"},
        ],
        "heat_rate_scenarios_btu_per_kwh": HEAT_RATE_BTU_PER_KWH,
        "formula": "$/MWh = (heat_rate_btu_per_kwh * gas_$/MMBtu) / 1000",
        "pricing_hubs": [
            {"key": k, "name": v}
            for k, v in sorted(_HUB_NAME.items())
        ],
        "refresh_cadence": "Nightly @ 04:00 UTC via /api/v1/markets/gas-pricing/cron",
        "staleness_policy": {
            "threshold_hours": GAS_PRICING_STALE_AFTER_HOURS,
            "statement": (
                "Every market payload carries age_hours / as_of_age against "
                f"fetched_at. Past {GAS_PRICING_STALE_AFTER_HOURS:.0f}h — one "
                "missed nightly refresh — is_stale flips true and the values "
                "should not be relied on for a pricing decision."
            ),
        },
        "labelling_policy": {
            "statement": (
                "A field whose source is synthetic / seed / default / fallback "
                "is never labelled 'live'. The envelope data_basis is the "
                "weakest per-field label, so 'live' at the envelope means every "
                "published number is measured; 'mixed' means at least one is not."
            ),
            "labels": ["live", "mixed", "synthetic_seed", "unavailable"],
            "per_field_key": "field_basis",
        },
        "phase_2_deferred": [
            "Per-utility delivered tariff scrapes "
            "(currently uses state-level EIA averages)",
            "ICE / NYMEX forward curves for forward $/MMBtu scenarios",
            "Intraday hub pricing (currently daily settle)",
            "Real-time pipeline outage / OFO impact on basis",
            "Live regional spot indices (Algonquin/Transco Z6/Tetco M3/...)",
        ],
        "tier_gating": {
            "anonymous": "verdict + hub identification (numbers masked)",
            "free":      "verdict + hub identification (numbers masked)",
            "identified": "verdict + hub identification (numbers masked)",
            "starter":   "verdict + hub identification (numbers masked)",
            "developer": "verdict + hub identification (numbers masked)",
            "pro":       "full 4-layer pricing + gas-to-grid $/MWh",
            "enterprise": "full + history (Phase 2)",
            "founding":   "full + history (Phase 2)",
        },
        "license": "Free for AI citation; data subject to https://dchub.cloud/terms",
        "citation_url": "https://dchub.cloud/powered-land",
    })


# Marquee markets seeded by the nightly cron. Phase 2 will fan out across
# all 300+ DCPI markets via a JOIN against dcpi_metro_scores; Phase 1
# refreshes this curated list daily so the headline markets always have
# fresh data even if the DCPI table query is slow / unavailable.
_PHASE1_MARKETS = [
    "northern-virginia", "ashburn", "dallas-fort-worth", "phoenix",
    "atlanta", "chicago", "silicon-valley", "santa-clara",
    "new-york", "northern-new-jersey", "boston", "columbus",
    "los-angeles", "san-francisco", "portland", "seattle",
    "miami", "orlando", "denver", "salt-lake-city",
    "las-vegas", "minneapolis", "houston", "austin",
    "san-antonio", "charlotte", "raleigh", "richmond",
    "nashville", "memphis", "new-orleans", "oklahoma-city",
    "tulsa", "kansas-city", "detroit", "indianapolis",
    "philadelphia", "pittsburgh",
]


@powered_land_gas_bp.route(
    "/api/v1/markets/gas-pricing/cron", methods=["POST"]
)
def gas_pricing_cron():
    """Nightly refresh — admin-gated. Recomputes Phase-1 marquee markets
    and upserts to market_gas_pricing. Idempotent."""
    if not _admin_ok(request):
        return jsonify(ok=False, error="forbidden"), 401

    markets = _PHASE1_MARKETS
    extra = request.args.get("markets") or request.args.get("slugs")
    if extra:
        markets = list({*markets, *[_slug_norm(s) for s in extra.split(",") if s.strip()]})

    refreshed = []
    failed = []
    started = time.time()
    deadline = started + 90.0   # short timeout (<30 min) so it cannot starve next cron slot
    for slug in markets:
        if time.time() > deadline:
            failed.append({"slug": slug, "reason": "timeout"})
            continue
        try:
            payload = _compute_market_payload(slug)
            ok = _upsert_market(payload)
            if ok:
                refreshed.append(slug)
            else:
                failed.append({"slug": slug, "reason": "upsert_failed"})
        except Exception as e:
            failed.append({"slug": slug, "reason": str(e)[:120]})

    return jsonify({
        "ok": True,
        "refreshed": len(refreshed),
        "failed":    len(failed),
        "duration_seconds": round(time.time() - started, 1),
        "markets":   refreshed,
        "errors":    failed,
        "eia_key_present": _eia_key_present(),
    })


# r-eia-status (2026-06-04): public-but-anon status endpoint surfacing
# EIA_API_KEY config + data freshness. Lets the operator curl ONE URL
# to know whether gas pricing is on real EIA data or the synthetic seed,
# how many markets are populated, and when the next cron refresh fires.
# Not admin-gated — the data itself is non-sensitive (boolean + counts),
# and surfacing it openly makes it easy for the autopilot to self-detect
# and for the user to spot drift without an admin key.
@powered_land_gas_bp.route(
    "/api/v1/markets/gas-pricing/status", methods=["GET"]
)
def gas_pricing_status():
    """Public env+data status for the gas-pricing surface."""
    eia_set = _eia_key_present()
    info = {
        "ok": True,
        "eia_api_key_set": eia_set,
        "data_basis": "mixed" if eia_set else "synthetic_seed",
        # ★2026-08-07: was "real_eia" when the key is set, which overstated it.
        #   Layer 2 (regional basis) is a seed constant regardless of the key,
        #   so the best this surface can be is MIXED. Per-market truth is in
        #   the payload's own field_basis, not in this env-level summary.
        "basis_layer_is_synthetic": True,
        "basis_layer_note": (
            "Layer 2 (regional basis differential) is a hardcoded Phase-1 seed "
            "for EVERY market, whether or not EIA_API_KEY is set. Setting the "
            "key makes layers 1 and 3 live; it does not make the basis live."
        ),
        "data_basis_explainer": (
            "EIA_API_KEY is configured — Henry Hub and the delivered tariff "
            "are live EIA reads. The regional basis differential is still a "
            "synthetic seed, so market payloads label themselves 'mixed', "
            "not 'live'."
            if eia_set else
            "EIA_API_KEY is NOT set in Railway env. Payloads return "
            "representative synthetic values so the surface stays alive, "
            "but they are NOT priced from live market data. Set the env "
            "var to flip data_basis to 'real_eia'."
        ),
        "setup": {
            "step_1_register": "https://www.eia.gov/opendata/register.php",
            "step_2_env_var":  "EIA_API_KEY (Railway → resourceful-essence → dchub-backend → Variables)",
            "step_3_redeploy": "Railway auto-redeploys on env change; next cron at 04:00 UTC picks it up.",
            "step_4_verify":   "Re-curl this endpoint; data_basis should be 'real_eia'.",
        },
        "cron": {
            "schedule_utc":    "04:00 daily (crawler_scheduler.SCHEDULE)",
            "endpoint":        "/api/v1/markets/gas-pricing/cron (admin-gated)",
            "soft_deadline_s": 90,
        },
        "phase_1_markets": list(_PHASE1_MARKETS),
        "phase_1_market_count": len(_PHASE1_MARKETS),
    }

    # Best-effort: count rows + last refresh from market_gas_pricing.
    try:
        c = _conn()
        if c:
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        SELECT COUNT(*)::int,
                               COUNT(*) FILTER (WHERE data_basis IN ('live','mixed','real_eia'))::int,
                               COUNT(*) FILTER (WHERE data_basis LIKE 'synthetic%%')::int,
                               COUNT(*) FILTER (WHERE basis_source LIKE '%%synthetic%%')::int,
                               MAX(fetched_at)::text
                          FROM market_gas_pricing
                    """)
                    row = cur.fetchone() or (0, 0, 0, 0, None)
            finally:
                try: c.close()
                except Exception: pass
            info["rows_total"]            = int(row[0] or 0)
            info["rows_eia_backed"]       = int(row[1] or 0)
            info["rows_synthetic"]        = int(row[2] or 0)
            # The row that actually matters: how many cached rows carry a
            # seed basis. Phase 1 answer is "all of them" — say so out loud.
            info["rows_with_synthetic_basis"] = int(row[3] or 0)
            info["last_refresh_at"]       = row[4]
            info["last_refresh_age_hours"] = _age_hours(row[4])
            info["staleness_threshold_hours"] = GAS_PRICING_STALE_AFTER_HOURS
    except Exception as e:
        info["rows_lookup_error"] = str(e)[:160]

    return jsonify(info), 200


# Programmatic helper for the crawler_scheduler runner. Wraps the cron
# endpoint in a loopback HTTP call so the schedule + global semaphore
# still apply (matches _run_ai_lab_auto_outreach pattern).
def run_gas_pricing_refresh():
    """Called by crawler_scheduler. Returns a dict with counts."""
    import requests as _rq
    key = (os.environ.get("DCHUB_ADMIN_KEY")
           or os.environ.get("DCHUB_INTERNAL_KEY")
           or os.environ.get("DCHUB_ADMIN_API_KEY") or "")
    base = os.environ.get("DCHUB_INTERNAL_API", "http://127.0.0.1:8080")
    if not key:
        # In-process fallback: bypass HTTP, call refresh directly.
        # We still respect the soft deadline by capping markets at the
        # Phase-1 set and using the standard upsert path.
        refreshed = []
        failed = []
        for slug in _PHASE1_MARKETS:
            try:
                payload = _compute_market_payload(slug)
                if _upsert_market(payload):
                    refreshed.append(slug)
                else:
                    failed.append(slug)
            except Exception:
                failed.append(slug)
        return {"in_process": True,
                "refreshed": len(refreshed), "failed": len(failed)}
    try:
        r = _rq.post(
            f"{base}/api/v1/markets/gas-pricing/cron",
            headers={"X-Admin-Key": key,
                     "User-Agent": "dchub-cron-gaspricing/1.0"},
            timeout=120,
        )
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json() or {}
    except Exception as e:
        return {"error": str(e)[:200]}
    return {}
