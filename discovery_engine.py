"""Phase YY (2026-05-15) — Discovery engine compatibility shim.

The `crawler_scheduler.py` (at the v74-ish refactor) imports four
discovery functions from a module named `discovery_engine`:

    from discovery_engine import (
        init_discovery_tables, run_peeringdb_discovery,
        run_osm_discovery, run_datacentermap_discovery,
    )

That module never existed in this repo — the functions were refactored
into `routes/discovery_routes.py` but `crawler_scheduler` was never
updated. The ImportError was caught silently and logged as
"Discovery engine not available" → discovery has been silent for 2+
days (zero new facilities in `discovered_facilities`).

Fix: re-export the canonical implementations under the historical
`discovery_engine` module name. crawler_scheduler now imports
successfully, facility discovery resumes on the next 07:00 / 19:00
UTC cron tick.

If `routes/discovery_routes` ever gets renamed/moved again, update
this shim — it's the only place that knows the old API name.
"""

from __future__ import annotations

import sys

# Try the canonical Phase II location first (routes blueprint).
# Fall back to api_server.py for the two functions only defined there.
try:
    from routes.discovery_routes import (
        init_discovery_tables,
        run_peeringdb_discovery,
        run_osm_discovery,
        run_datacentermap_discovery,
    )
except ImportError as _e:
    # Last-resort fallback for init + peeringdb (api_server has them too)
    print(f"[discovery_engine] routes.discovery_routes import failed: {_e}",
          file=sys.stderr)
    try:
        from api_server import init_discovery_tables, run_peeringdb_discovery
    except ImportError:
        def init_discovery_tables():
            print("[discovery_engine] init_discovery_tables: no implementation found",
                  file=sys.stderr)
            return None
        def run_peeringdb_discovery():
            print("[discovery_engine] run_peeringdb_discovery: no implementation found",
                  file=sys.stderr)
            return {"found": 0, "added": 0, "error": "no_implementation"}
    # The other two might not be reachable — fail-soft so the crawler
    # falls back to whatever sources DO load.
    def _missing(name):
        def _fn():
            print(f"[discovery_engine] {name}: no implementation found "
                  f"(routes.discovery_routes import failed; api_server lacks it)",
                  file=sys.stderr)
            return {"found": 0, "added": 0, "error": "no_implementation"}
        return _fn
    try:
        run_osm_discovery  # noqa: F821
    except NameError:
        run_osm_discovery = _missing("run_osm_discovery")
    try:
        run_datacentermap_discovery  # noqa: F821
    except NameError:
        run_datacentermap_discovery = _missing("run_datacentermap_discovery")


__all__ = [
    "init_discovery_tables",
    "run_peeringdb_discovery",
    "run_osm_discovery",
    "run_datacentermap_discovery",
]
