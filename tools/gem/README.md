# GEM (Global Energy Monitor) re-ingest — quarterly refresh runbook

The GEM worldwide inventory (power, LNG, gas/oil pipelines, coal mines) is loaded
into Neon and served by `routes/gem_ingest.py` at:

- `GET /api/v1/global-power` (table `gem_power`, 182k units)
- `GET /api/v1/global-gas` (table `gem_gas`, LNG terminals)
- `GET /api/v1/global-gas-pipelines?fuel=Gas|Oil,NGL` (table `gem_gas_pipelines`)
- `GET /api/v1/global-coal-mines` (table `gem_coal_mines`)

The map layers + the MCP `get_global_power` tool read those endpoints.

## Why this is a MONITORED-but-manual loop
GEM's data downloads are **CC-BY but gated** — the files return HTTP 410 on direct
access and require a one-time form. A GitHub runner **cannot** auto-fetch them, so
the *fetch* step is manual (the owner downloads the quarterly bundle). Everything
downstream IS a loop:

- **Staleness detection** — the four tables are registered in
  `routes/infra_growth.py` `_LAYERS` (`periodic`, 150–220d). The daily
  `infra-growth-tracker` snapshots them and raises a stale flag when a table
  hasn't changed past its threshold = "GEM is overdue for a refresh." Nothing
  stagnates silently.
- **Idempotent** — every loader is a full-replace by `source` tag, so re-running
  is always safe (no dupes, no partial state).
- **Resilient serve** — the serve endpoints `to_regclass`-guard every table:
  a missing/empty table degrades to an empty FeatureCollection, never a 500.

## To refresh (when the stale flag fires, ~quarterly)
1. Download the latest GEM bundle from https://globalenergymonitor.org (fill the
   short CC-BY form once) → `gem-data.zip`, and extract it.
2. Set `DATABASE_URL` (from Railway → dchub-backend → Variables, or the Railway MCP
   `list_variables`).
3. Run the five loaders (each is standalone, idempotent, `openpyxl` + `psycopg2`):

```bash
cd <extracted gem-data folder>
DATABASE_URL=… python3 tools/gem/load_gem_power.py            # Global-Integrated-Power-*.xlsx
DATABASE_URL=… python3 tools/gem/load_gem_gas.py             # GEM-GGIT-LNG-*.xlsx
DATABASE_URL=… python3 tools/gem/load_gem_pipelines.py       # GEM-GGIT-Gas-Pipelines-*.geojson (from the sibling .zip)
DATABASE_URL=… GJ=GEM-GOIT-Oil-NGL-Pipelines-*.geojson python3 tools/gem/load_gem_oil_pipelines.py
DATABASE_URL=… GJ="Coal Mine Boundaries…/….geojson" python3 tools/gem/load_gem_coalmines.py
```

4. Verify counts moved: `GET /api/v1/global-power?bbox=…` (`total`), and the next
   `infra-growth` snapshot clears the stale flag automatically.

## Fully-automating the fetch (optional next step)
Host the bundle on R2 (`R2_BUCKET`) — a GitHub workflow *can* reach R2 — and a
weekly `gem-refresh.yml` can download from R2 → run these loaders → the owner's
only step becomes a quarterly drag-drop of the new bundle into R2. The
staleness flag already tells you when that's due.
