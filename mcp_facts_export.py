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
DOWN. TWO OWNERS, split by what each is actually authoritative for:

  /api/v1/canon/phrases — the four GOVERNANCE figures:
      facilities  countries  markets  deals
    This is canonical_stats' own PUBLISHED output, so a regenerated facts file
    can never disagree with what that endpoint serves. ★ Fetched, NOT called
    in-process: facilities_verified_phrase() and friends read the DATABASE and
    fall back to the module's cold-start constants without one — measured
    2026-07-30, a local run emitted facilities "400+" against a live 15,300+
    (38x under) and wrote the file with exit 0. See _require_phrase().

  /api/v1/infrastructure/stats — the ASSET LAYERS, fetched LIVE and floored
    here because no governance function covers them:
      infrastructure_assets_total + per-layer substations, transmission,
      fiber, gas, US plants, subsea cables + landings

★ /api/v1/stats/canonical IS DELIBERATELY NOT FETCHED. It was, for the four
governance figures — and its `countries_covered` is the LEGACY `facilities`-table
artifact: 186, because it double-counts 9 full-name/ISO-code pairs ("USA"+"US",
"Germany"+"DE"). The deduped fleet spans 178 codes, so flooring the endpoint gave
"180+" where the honest floor is "170+". That is the exact over-claim #1949
corrected; deriving these from the endpoint would have re-shipped it through a
different file. Measured on the Railway origin 2026-07-30, AFTER #1949 merged:
countries_covered is still 186. Raw endpoint fields are not an audited basis.

Two keys an earlier revision replaced were themselves over-claims: `facilities`
floored the RAW discovered_facilities pile (23k rows ≈ 1.5x the buildings — the
March 2026 backfill wrote several rows per site) and `markets` published the
exact 311, which counted score ROWS, not scored markets. Both are now the
verified/floor phrase forms.

If the infrastructure endpoint fails or a required field is missing, the export
FAILS and writes NOTHING: a stale-but-honest facts file (the server gate ages it
out at 45 days) beats a fresh file built from guesses.

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

# The founding counter — the same endpoint the checkout-integrity shell
# reads (lane 4). Deliberately NOT under /api/v1/: CF rule #3 caches that
# prefix with override_origin, and a stale "program_active" would publish
# a price that has sold out.
FOUNDING_COUNTER_PATH = "/api/founding-members"

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


def _require_phrase(d: dict, key: str, source: str) -> str:
    """A published canon phrase: a non-empty string that contains a digit.

    ★ Why phrases are FETCHED and not computed in-process. The obvious form is
    `c.facilities_verified_phrase()` — but those functions read the DATABASE, and
    with no DB they return the module's cold-start _FALLBACK. Measured 2026-07-30
    running this exporter locally: facilities came out "400+" against a live
    15,300+ (a 38x UNDER-claim) and deals "1,400+", and the export wrote the file
    and exited 0, because the fail-hard contract only covered the HTTP fetch.
    A generator that silently swaps in fallbacks anywhere it lacks a DB is the
    same defect class this file exists to prevent, one layer up.
    /api/v1/canon/phrases is those same functions' PUBLISHED output, so reading
    it keeps the single-owner property, works without a DB, and comes under the
    fail-hard contract below.
    """
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ExportError(f"{source} missing/non-string `{key}` (got {v!r}) — "
                          "refusing to export a guessed phrase")
    if not any(ch.isdigit() for ch in v):
        raise ExportError(f"{source} `{key}` = {v!r} carries no digit — that is "
                          "a prose fallback, not a figure")
    return v.strip()


def _floor_k(n: int) -> str:
    """Round DOWN to a whole-thousand 'Nk' floor, e.g. 126841 -> '126k'."""
    if n < 1000:
        raise ExportError(f"k-floor on {n} would publish '0k'")
    return f"{n // 1000}k"


def _live_numbers() -> dict:
    """The ASSET-LAYER figures, floored down from the infrastructure endpoint.

    ★ The four governance figures (facilities / countries / markets / deals) are
    deliberately NOT here — build() fetches them from /api/v1/canon/phrases, the
    published output of the module that owns them. See the note in build().
    /api/v1/stats/canonical is no longer fetched at all: every field this export
    used to read from it is owned elsewhere, and its `countries_covered` is the
    legacy-table artifact (186 vs a true 178) that #1949 diagnosed. Removing the
    fetch removes the only path by which that artifact could reach a published
    figure.
    """
    infra = _get_json("/api/v1/infrastructure/stats")
    ist = infra.get("stats") or {}
    return {
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


def _pricing() -> dict:
    """pricing_usd_month — the flat monthly tiers, plus `founding` while the
    founding program is OPEN.

    2026-09-02 (finding 3): $99 founding is the SKU that sells — 10 of 14
    active external subs, 7 of 16 completions in 8 weeks — and it was absent
    from every agent-facing surface, this file included. It is a LIMITED
    programme (FOUNDING_CUSTOMERS_CAP), so it is emitted only while the live
    counter says there is stock; a closed programme drops the key and the
    next export publishes the standing tiers alone. The counter is read like
    every other figure here — fetched, fail-hard — because a facts file that
    advertises a sold-out price is the exact drift this exporter exists to
    end. What "founding" COUNTS (first-25-paid-of-any-plan today) is the
    owner's decision and is not decided here.
    """
    pricing = {k: t.price(k) for k in PRICE_TIERS}
    fc = _get_json(FOUNDING_COUNTER_PATH)
    active = fc.get("program_active")
    remaining = fc.get("remaining")
    if not isinstance(active, bool) or not isinstance(remaining, int):
        raise ExportError(f"{FOUNDING_COUNTER_PATH} missing program_active/remaining "
                          f"(got {active!r}/{remaining!r}) — refusing to guess "
                          "whether the founding price is still for sale")
    if active and remaining > 0:
        pricing["founding"] = t.price("founding")
    return pricing


def build() -> dict:
    s = c.get_canonical_stats()
    numbers = _live_numbers()
    # The four governance-owned figures, from the phrases owner's PUBLISHED
    # output. Set AFTER _live_numbers() so a future edit that reintroduces them
    # upstream cannot silently win. See _require_phrase() for why these are
    # fetched rather than called in-process.
    ph = _get_json("/api/v1/canon/phrases")
    for _k in ("facilities", "countries", "markets", "deals"):
        numbers[_k] = _require_phrase(ph, _k, "canon/phrases")
    numbers["pipeline_gw"] = int(s.get("pipeline_gw", 369))
    return {
        "_generated_by": "dchub-backend/mcp_facts_export.py",
        "_warning": ("CANONICAL — DO NOT HAND-EDIT. Regenerate via "
                     "`python3 mcp_facts_export.py`. version + tool_count are "
                     "owned by sync-tools-manifest.mjs (server.json / server.mjs). "
                     "numbers are floors that round DOWN: the governance figures "
                     "(facilities/countries/markets/deals) from canonical_stats, "
                     "the asset layers from /api/v1/infrastructure/stats; "
                     "server.mjs composes the "
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
        "pricing_usd_month": _pricing(),
        "calls_per_day": {k: t.calls_per_day(k) for k in DAILY_TIERS},
        # ★★ MERGE RESOLUTION 2026-07-30 (#1944 x #1949). Two owners collided
        # here and only one can be right per key:
        #   #1944 (this branch) fetches the public truth endpoints and floors
        #         DOWN itself — the right call for the ASSET LAYERS, which no
        #         governance function covers.
        #   #1949 (main) routed the four governance figures through
        #         canonical_stats so a regenerated facts file can NEVER disagree
        #         with what /api/v1/canon/phrases serves.
        # Resolved as: governance owns its four, the endpoints own the layers.
        #
        # ★ THE CONFLICT SURFACED A REAL OVER-CLAIM, not just a textual clash.
        # This branch derived `countries` from stats/canonical countries_covered
        # and floored it to "180+". That field is STILL 186 live (measured on the
        # Railway origin 2026-07-30, after #1949 merged) and 186 is the LEGACY
        # `facilities`-table artifact #1949 diagnosed: it double-counts 9
        # full-name/ISO-code pairs ("USA"+"US", "Germany"+"DE"). The deduped
        # fleet spans 178 codes, so the honest floor is "170+" — which is what
        # countries_verified_phrase() returns and what /api/v1/canon/phrases
        # publishes. Emitting "180+" would have re-shipped the exact over-claim
        # #1949 corrected, one commit later, through a different file.
        # ★ Do NOT "simplify" these four back onto the endpoints. The endpoint
        # numbers are raw; the phrase functions are the audited basis.
        #
        # The other three agree on both bases, verified live, so this changes no
        # published value: facilities 15,367 -> "15,300+", markets 307 -> "300+",
        # deals 1,612 -> "1,600+".
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
