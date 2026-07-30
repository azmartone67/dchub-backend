#!/usr/bin/env python3
"""mcp_facts_export.py — generate the canonical MCP facts JSON from the live SoTs.

THE CROSS-LANGUAGE BRIDGE. The dchub-mcp-server (Node) repo cannot import these
Python source-of-truth modules, so its manifests (smithery.yaml, README,
mcp-server.json, server.json) drift independently — that's how "Pro $199",
"countries 140", "EU ~12 zones", and stale tool counts kept reappearing.

This emits ONE JSON file — canonical/mcp_facts.json — holding the canonical
facts (pricing, calls/day, headline numbers, grid coverage, description).
sync-tools-manifest.mjs checks the Node-repo surfaces against it, and — since
2026-07-30 — server.mjs COMPOSES its initialize `instructions` figures from it
at startup behind a freshness gate keyed on `generated_at` (fresh + complete →
figures; stale/partial/absent → prose without figures, never stale figures).

★2026-07-30 (the 311-markets lesson): every number here is a FLOOR that rounds
DOWN from the two public truth endpoints, fetched LIVE at export time:
    /api/v1/stats/canonical        (facilities_distinct, dcpi_markets_scored,
                                    countries_covered, deals_tracked)
    /api/v1/infrastructure/stats   (infrastructure_assets_total + per-layer
                                    substations, transmission, fiber, gas,
                                    US plants, subsea cables + landings)
Two keys this replaced were themselves over-claims: `facilities` floored the
RAW discovered_facilities pile (23k rows ≈ 1.5x the buildings — the March 2026
backfill wrote several rows per site); the citable population is
facilities_distinct (COUNT(DISTINCT canonical_slug) — distinct BUILDINGS, per
the endpoint's own provenance block). And `markets` published the exact 311 —
which counted score ROWS, not scored markets (live dcpi_markets_scored = 306).

If either endpoint fails or a required field is missing, the export FAILS and
writes NOTHING: a stale-but-honest facts file (the server gate ages it out at
45 days) beats a fresh file built from guesses.

NOT emitted here (Node owns them, code-derived):
  - version    → server.json .version
  - tool_count → server.mjs _registeredToolNames (the live tools/list count).

Run:  python3 mcp_facts_export.py   (re-run after ANY tier/number/coverage edit;
then commit canonical/mcp_facts.json in the mcp-server repo).
"""
import datetime
import json
import os
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tier_registry as t          # noqa: E402
import canonical_stats as c        # noqa: E402

PRICE_TIERS = ["starter", "developer", "pro", "team", "enterprise"]
DAILY_TIERS = ["free", "identified", "starter", "developer", "pro", "enterprise"]

BASE_URL = os.environ.get("DCHUB_BASE_URL", "https://dchub.cloud").rstrip("/")

# MEASURED CONSTANTS — no queryable endpoint exposes these yet; each was
# measured live 2026-07-30 and floors DOWN. RE-MEASURE before raising (the
# eu_zones pattern in canonical_stats.py — never publish a configured count):
#   generating_units_global: gem_power row count (~182k GEM GIPT units across
#     ALL statuses — a UNIT inventory, never "power plants"; GEM releases
#     ~2x/year, so this moves slowly).
#   live_feeds / grid_regions: independent upstream feeds (EIA, NESO/Elexon,
#     ENTSO-E, Taipower, OCCTO, KPX, ONS) and the distinct regions/operators
#     get_grid_scoreboard returns live (49 measured 2026-07-30).
GENERATING_UNITS_GLOBAL = "182k"
LIVE_FEEDS = 7
GRID_REGIONS = 49


class ExportError(RuntimeError):
    pass


def _get_json(path: str) -> dict:
    # requests, not urllib — regression-lint bans urllib on Railway (#1940).
    # NB: a default library UA gets bot-filtered at the edge — always set one.
    resp = requests.get(BASE_URL + path, timeout=30,
                        headers={"User-Agent": "dchub-mcp-facts-export/1.0"})
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict):
        raise ExportError(f"{path}: non-object response")
    return body


def _require_int(d: dict, key: str, source: str) -> int:
    v = d.get(key)
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ExportError(f"{source} missing/non-numeric `{key}` (got {v!r}) — "
                          "refusing to export a guessed floor")
    if n <= 0:
        raise ExportError(f"{source} `{key}` = {n} — a zero/negative count is a "
                          "wrong-table symptom, not a floor")
    return n


def _floor(n: int, step: int) -> str:
    """Round DOWN to a clean citation-safe floor, e.g. 15367 -> '15,300+'."""
    return f"{(n // step) * step:,}+"


def _floor_k(n: int) -> str:
    """Round DOWN to a whole-thousand 'Nk' floor, e.g. 126841 -> '126k'."""
    if n < 1000:
        raise ExportError(f"k-floor on {n} would publish '0k'")
    return f"{n // 1000}k"


def _live_numbers() -> dict:
    canon = _get_json("/api/v1/stats/canonical")
    cs = canon.get("stats") or {}
    infra = _get_json("/api/v1/infrastructure/stats")
    ist = infra.get("stats") or {}
    return {
        # /api/v1/stats/canonical — facilities_distinct is "the field to cite"
        # per its own provenance block (distinct BUILDINGS, not raw rows).
        "facilities": _floor(_require_int(cs, "facilities_distinct", "stats/canonical"), 100),
        "countries": _floor(_require_int(cs, "countries_covered", "stats/canonical"), 10),
        "markets": _floor(_require_int(cs, "dcpi_markets_scored", "stats/canonical"), 100),
        "deals": _floor(_require_int(cs, "deals_tracked", "stats/canonical"), 100),
        # /api/v1/infrastructure/stats — asset layers only (facilities excluded
        # by the endpoint's own definition; summing them would double-count).
        "substations": _floor_k(_require_int(ist, "substations", "infrastructure/stats")),
        "infrastructure_assets_total": _floor(
            _require_int(infra, "infrastructure_assets_total", "infrastructure/stats"), 10000),
        "transmission_lines": _floor_k(_require_int(ist, "transmission_lines", "infrastructure/stats")),
        "fiber_routes": _floor_k(_require_int(ist, "fiber_routes", "infrastructure/stats")),
        "gas_pipelines": _floor_k(_require_int(ist, "gas_pipelines", "infrastructure/stats")),
        # `power_plants` on this endpoint is the US layer (13k); the GLOBAL GEM
        # inventory is generating UNITS and lives in the measured constant below.
        "power_plants_us": _floor_k(_require_int(ist, "power_plants", "infrastructure/stats")),
        "submarine_cables": _floor(_require_int(ist, "submarine_cables", "infrastructure/stats"), 10),
        "cable_landings": _floor(
            _require_int(ist, "submarine_cable_landings", "infrastructure/stats"), 100),
        "generating_units_global": GENERATING_UNITS_GLOBAL,
        "live_feeds": LIVE_FEEDS,
        "grid_regions": GRID_REGIONS,
    }


def build() -> dict:
    s = c.get_canonical_stats()
    numbers = _live_numbers()
    numbers["pipeline_gw"] = int(s.get("pipeline_gw", 369))
    return {
        "_generated_by": "dchub-backend/mcp_facts_export.py",
        "_warning": ("CANONICAL — DO NOT HAND-EDIT. Regenerate via "
                     "`python3 mcp_facts_export.py`. version + tool_count are "
                     "owned by sync-tools-manifest.mjs (server.json / server.mjs). "
                     "numbers are floors that round DOWN from /api/v1/stats/canonical "
                     "+ /api/v1/infrastructure/stats; server.mjs composes the "
                     "initialize instructions from this file behind a freshness "
                     "gate keyed on generated_at (see _composeInstructions)."),
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "description": ("Real-time data-center, power-grid & energy intelligence "
                        "for AI agents — the only MCP server an LLM can both query "
                        "and cite."),
        "remote_url": "https://dchub.cloud/mcp",
        "repo": "github.com/azmartone67/dchub-mcp-server",
        "license": "CC-BY-4.0",
        "pricing_usd_month": {k: t.price(k) for k in PRICE_TIERS},
        "calls_per_day": {k: t.calls_per_day(k) for k in DAILY_TIERS},
        "numbers": numbers,
        "grid_coverage": {
            "us_isos": int(s.get("isos", 7)),
            "eia_balancing_authorities": int(s.get("utility_bas", 43)),
            "eu_entsoe_zones": int(s.get("eu_zones", 24)),
            "phrase_full": c.grid_coverage_phrase("full"),
            "phrase_short": c.grid_coverage_phrase("short"),
        },
    }


def main() -> int:
    try:
        body = json.dumps(build(), indent=2) + "\n"
    except Exception as e:   # ExportError, URLError, timeout — all mean the same thing:
        # Write NOTHING on failure: the committed facts file stays as-is, and
        # the server-side freshness gate ages it out rather than serving a
        # guess. Exit non-zero so a cron/CI wrapper surfaces the failure.
        print(f"EXPORT FAILED — nothing written: {e}", file=sys.stderr)
        return 1
    # primary consumer = the Node mcp-server repo; secondary = a servable backend copy
    targets = [
        os.path.join(HERE, "..", "dchub-mcp-server", "canonical", "mcp_facts.json"),
        os.path.join(HERE, "static", ".well-known", "mcp_facts.json"),
    ]
    for path in targets:
        repo_dir = os.path.dirname(os.path.dirname(path))      # the repo root
        if not os.path.isdir(repo_dir):
            print(f"  skipped {os.path.normpath(path)} (repo not present)")
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"  wrote {os.path.normpath(path)}")
    print("\n" + body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
