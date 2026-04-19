#!/usr/bin/env python3
"""
KMZ Fiber Map Builder
=====================
Automatically pulls fiber route + power infrastructure data from all sites
and packages them as KMZ files ready for import into your Land & Power tool.

Sources supported:
  1. DC Hub / REST API  — paginate all facilities, fetch fiber intel + power infra
  2. Local folder scan  — collect any existing .kmz / .kml files on disk
  3. Remote URLs / FTP  — download KMZ files from a list of endpoints

Usage:
  python kmz_fiber_builder.py                  # run everything with defaults
  python kmz_fiber_builder.py --api-only        # API source only
  python kmz_fiber_builder.py --local-only      # local folder scan only
  python kmz_fiber_builder.py --sites VA,TX     # filter by state codes

Requirements:
  pip install simplekml requests tqdm
"""

import os
import sys
import json
import zipfile
import logging
import argparse
import ftplib
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from datetime import datetime

import requests
import simplekml
from tqdm import tqdm

# ─────────────────────────────────────────────
#  CONFIGURATION  (edit here or use env vars)
# ─────────────────────────────────────────────

CONFIG = {
    # --- DC Hub / API settings ---
    "API_BASE_URL": os.getenv("DCHUB_API_URL", "https://dchub.cloud/api/v1"),
    "API_KEY":      os.getenv("DCHUB_API_KEY", "YOUR_API_KEY_HERE"),

    # Fiber intel filters (leave blank for all)
    "FIBER_CARRIER": os.getenv("FIBER_CARRIER", ""),       # e.g. "Zayo"
    "FIBER_ROUTE_TYPE": os.getenv("FIBER_ROUTE_TYPE", ""), # long_haul | metro | subsea

    # Power infrastructure radius per site (km)
    "INFRA_RADIUS_KM": int(os.getenv("INFRA_RADIUS_KM", "50")),
    "INFRA_LAYER":     os.getenv("INFRA_LAYER", "all"),    # substations | transmission | all

    # Pagination
    "PAGE_SIZE": 100,   # facilities per API page (max 100)

    # --- Local folder scan ---
    "LOCAL_SCAN_DIR": os.getenv("LOCAL_KMZ_DIR", "./kmz_source"),

    # --- Remote URL list (one URL per line in this file, or add inline) ---
    "REMOTE_URL_FILE": os.getenv("REMOTE_URL_FILE", "./remote_kmz_urls.txt"),
    "REMOTE_URLS": [
        # Add direct KMZ URLs here, e.g.:
        # "https://example.com/fiber_route_east.kmz",
        # "ftp://files.example.com/maps/west_coast.kmz",
    ],

    # --- Output ---
    "OUTPUT_DIR": os.getenv("KMZ_OUTPUT_DIR", "./kmz_output"),
    "BUNDLE_ALL":  True,   # also create a single merged KMZ with all sites
}

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("kmz_builder.log", mode="w"),
    ],
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def api_get(path: str, params: dict = None) -> dict:
    """Call the DC Hub REST API and return parsed JSON."""
    url = CONFIG["API_BASE_URL"].rstrip("/") + "/" + path.lstrip("/")
    headers = {"Authorization": f"Bearer {CONFIG['API_KEY']}", "Accept": "application/json"}
    resp = requests.get(url, headers=headers, params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def safe_name(text: str) -> str:
    """Strip characters unsafe for filenames."""
    return "".join(c if c.isalnum() or c in "-_ " else "_" for c in str(text)).strip()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# ─────────────────────────────────────────────
#  1. API: FETCH ALL FACILITIES
# ─────────────────────────────────────────────

def fetch_all_facilities(state_filter: list[str] = None) -> list[dict]:
    """
    Paginate through every facility in the DC Hub API.
    Optionally filter by a list of US state codes (e.g. ['VA', 'TX']).
    """
    log.info("Fetching facility list from API …")
    facilities = []
    offset = 0
    page_size = CONFIG["PAGE_SIZE"]

    while True:
        params = {"limit": page_size, "offset": offset}
        if state_filter:
            params["state"] = ",".join(state_filter)
        try:
            page = api_get("/facilities", params)
        except requests.HTTPError as e:
            if hasattr(e,"response") and e.response is not None and e.response.status_code==403:
                log.warning("  /facilities requires Pro -- switching to export mode")
                return []
            log.error(f"API error fetching facilities: {e}")
            break

        items = page if isinstance(page, list) else page.get("results", page.get("data", []))
        if not items:
            break
        facilities.extend(items)
        log.info(f"  … fetched {len(facilities)} facilities so far (page offset {offset})")
        if len(items) < page_size:
            break
        offset += page_size

    log.info(f"Total facilities found: {len(facilities)}")
    return facilities


# ─────────────────────────────────────────────
#  2. API: FIBER INTEL → KMZ per site
# ─────────────────────────────────────────────

FIBER_STYLE_MAP = {
    "long_haul": {"color": simplekml.Color.blue,   "width": 4},
    "metro":     {"color": simplekml.Color.green,  "width": 3},
    "subsea":    {"color": simplekml.Color.cyan,   "width": 5},
    "default":   {"color": simplekml.Color.orange, "width": 3},
}

INFRA_ICON_MAP = {
    "substations":    "http://maps.google.com/mapfiles/kml/shapes/bolt.png",
    "transmission":   "http://maps.google.com/mapfiles/kml/shapes/electric.png",
    "gas_pipelines":  "http://maps.google.com/mapfiles/kml/shapes/gas_stations.png",
    "power_plants":   "http://maps.google.com/mapfiles/kml/shapes/industries.png",
}


def geojson_to_kml_lines(kml_doc: simplekml.Kml, geojson_features: list, folder_name: str):
    """Add GeoJSON LineString/MultiLineString features as KML line strings."""
    folder = kml_doc.newfolder(name=folder_name)
    for feat in geojson_features:
        props = feat.get("properties", {})
        geom  = feat.get("geometry", {})
        gtype = geom.get("type", "")
        carrier    = props.get("carrier", "Unknown carrier")
        route_type = props.get("route_type", "default")
        style_cfg  = FIBER_STYLE_MAP.get(route_type, FIBER_STYLE_MAP["default"])

        def add_line(coords):
            ls = folder.newlinestring(
                name=f"{carrier} — {route_type}",
                description=(
                    f"Carrier: {carrier}<br/>"
                    f"Route type: {route_type}<br/>"
                    f"Distance: {props.get('distance_km', 'N/A')} km<br/>"
                    f"Endpoints: {props.get('endpoint_a', '')} → {props.get('endpoint_b', '')}"
                ),
                coords=[(c[0], c[1]) for c in coords],
            )
            ls.style.linestyle.color = style_cfg["color"]
            ls.style.linestyle.width = style_cfg["width"]

        if gtype == "LineString":
            add_line(geom.get("coordinates", []))
        elif gtype == "MultiLineString":
            for segment in geom.get("coordinates", []):
                add_line(segment)


def infra_to_kml_points(kml_doc: simplekml.Kml, infra_data: dict):
    """Add power infrastructure items as KML placemarks."""
    for layer_name, items in infra_data.items():
        if not isinstance(items, list) or not items:
            continue
        icon_url = INFRA_ICON_MAP.get(layer_name, "http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png")
        folder = kml_doc.newfolder(name=layer_name.replace("_", " ").title())
        for item in items:
            lat = item.get("lat") or item.get("latitude")
            lon = item.get("lon") or item.get("longitude")
            if lat is None or lon is None:
                continue
            pm = folder.newpoint(
                name=item.get("name", layer_name),
                description=(
                    f"Voltage: {item.get('voltage_kv', 'N/A')} kV<br/>"
                    f"Capacity: {item.get('capacity_mw', 'N/A')} MW<br/>"
                    f"Distance: {item.get('distance_km', 'N/A')} km from site<br/>"
                    f"Operator: {item.get('operator', 'N/A')}"
                ),
                coords=[(lon, lat)],
            )
            pm.style.iconstyle.icon.href = icon_url
            pm.style.iconstyle.scale = 1.2


def build_site_kmz(facility: dict, output_dir: Path) -> Optional[Path]:
    """
    For one facility: fetch fiber intel + power infra via API,
    build a KMZ, and return its path. Returns None on failure.
    """
    fid   = facility.get("id", "unknown")
    fname = facility.get("name", fid)
    lat   = facility.get("lat") or facility.get("latitude")
    lon   = facility.get("lon") or facility.get("longitude")

    if lat is None or lon is None:
        log.warning(f"  Skipping {fname} — no coordinates")
        return None

    kml = simplekml.Kml(name=fname)

    # ── Fiber intel ──────────────────────────────────────────────
    try:
        fiber_params = {
            "lat": lat, "lon": lon,
            "carrier":    CONFIG["FIBER_CARRIER"],
            "route_type": CONFIG["FIBER_ROUTE_TYPE"],
        }
        fiber_resp = api_get("/fiber-intel", fiber_params)
        features = (
            fiber_resp.get("features") or
            fiber_resp.get("routes") or
            (fiber_resp if isinstance(fiber_resp, list) else [])
        )
        if features:
            geojson_to_kml_lines(kml, features, "Fiber Routes")
            log.info(f"    ✓ {len(features)} fiber routes")
        else:
            log.info(f"    – no fiber routes returned")
    except Exception as e:
        log.warning(f"    Fiber intel error for {fname}: {e}")

    # ── Power infrastructure ─────────────────────────────────────
    try:
        infra_params = {
            "lat": lat, "lon": lon,
            "radius_km": CONFIG["INFRA_RADIUS_KM"],
            "layer":     CONFIG["INFRA_LAYER"],
        }
        infra_resp = api_get("/infrastructure", infra_params)
        # Strip non-layer keys
        infra_layers = {
            k: v for k, v in infra_resp.items()
            if isinstance(v, list)
        }
        if infra_layers:
            infra_to_kml_points(kml, infra_layers)
            total = sum(len(v) for v in infra_layers.values())
            log.info(f"    ✓ {total} infrastructure points")
        else:
            log.info(f"    – no infrastructure data returned")
    except Exception as e:
        log.warning(f"    Infrastructure error for {fname}: {e}")

    # ── Write KMZ ────────────────────────────────────────────────
    kmz_path = output_dir / f"{safe_name(fname)}_{fid}.kmz"
    kml.savekmz(str(kmz_path))
    return kmz_path


import re as _re
def run_export_source(output_dir, state_filter=None):
    import re; base = re.sub(r"/api/v\d+$|/v\d+$|/api$", "", CONFIG["API_BASE_URL"].rstrip("/"))
    url = base + "/api/energy-discovery/export/kmz"
    log.info(f"Export mode: {url}")
    out = ensure_dir(output_dir / "export")
    paths = []
    for etype, fname in [("power-plants","dchub_power_plants"),("transmission-lines","dchub_transmission_lines"),("pipelines","dchub_pipelines")]:
        dest = out / f"{fname}.kmz"
        log.info(f"  Downloading {etype}...")
        try:
            download_http(f"{url}?type={etype}", dest)
            log.info(f"  checkmark {fname}.kmz ({dest.stat().st_size//1024:,} KB)")
            paths.append(dest)
        except Exception as e:
            log.error(f"  x {etype}: {e}")
    return paths

def run_api_source(output_dir: Path, state_filter: list[str] = None) -> list[Path]:
    """Fetch all sites from API, falls back to export bundles if Pro-gated."""
    facilities = fetch_all_facilities(state_filter)
    if not facilities:
        log.info("Falling back to export bundles...")
        return run_export_source(output_dir, state_filter)

    log.info(f"\nBuilding KMZ files for {len(facilities)} sites …")
    site_dir = ensure_dir(output_dir / "per_site")
    paths = []

    for fac in tqdm(facilities, desc="Sites", unit="site"):
        fname = fac.get("name", fac.get("id", "?"))
        log.info(f"  Processing: {fname}")
        try:
            p = build_site_kmz(fac, site_dir)
            if p:
                paths.append(p)
        except Exception as e:
            log.error(f"  Failed for {fname}: {e}")

    return paths


# ─────────────────────────────────────────────
#  3. LOCAL FOLDER SCAN
# ─────────────────────────────────────────────

def run_local_source(output_dir: Path) -> list[Path]:
    """Collect all .kmz and .kml files from the configured local directory."""
    scan_dir = Path(CONFIG["LOCAL_SCAN_DIR"])
    if not scan_dir.exists():
        log.info(f"Local scan dir not found: {scan_dir} — skipping")
        return []

    found = list(scan_dir.rglob("*.kmz")) + list(scan_dir.rglob("*.kml"))
    log.info(f"Local scan found {len(found)} files in {scan_dir}")

    local_out = ensure_dir(output_dir / "local")
    paths = []
    for src in found:
        dest = local_out / src.name
        shutil.copy2(src, dest)
        log.info(f"  Copied: {src.name}")
        paths.append(dest)
    return paths


# ─────────────────────────────────────────────
#  4. REMOTE URL / FTP DOWNLOADS
# ─────────────────────────────────────────────

def download_http(url: str, dest: Path):
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)


def download_ftp(url: str, dest: Path):
    parsed = urlparse(url)
    host, path = parsed.hostname, parsed.path
    user = parsed.username or "anonymous"
    pwd  = parsed.password or "anonymous@"
    with ftplib.FTP(host) as ftp:
        ftp.login(user, pwd)
        with open(dest, "wb") as f:
            ftp.retrbinary(f"RETR {path}", f.write)


def run_remote_source(output_dir: Path) -> list[Path]:
    """Download KMZ files from a list of HTTP/FTP URLs."""
    urls = list(CONFIG["REMOTE_URLS"])

    # Also load from file if it exists
    url_file = Path(CONFIG["REMOTE_URL_FILE"])
    if url_file.exists():
        extra = [ln.strip() for ln in url_file.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
        urls.extend(extra)

    if not urls:
        log.info("No remote URLs configured — skipping")
        return []

    log.info(f"Downloading {len(urls)} remote KMZ files …")
    remote_out = ensure_dir(output_dir / "remote")
    paths = []

    for url in urls:
        fname = Path(urlparse(url).path).name or "download.kmz"
        dest  = remote_out / fname
        try:
            if url.startswith("ftp://"):
                download_ftp(url, dest)
            else:
                download_http(url, dest)
            log.info(f"  ✓ Downloaded: {fname}")
            paths.append(dest)
        except Exception as e:
            log.error(f"  ✗ Failed {url}: {e}")

    return paths


# ─────────────────────────────────────────────
#  5. BUNDLE: MERGE ALL KMZ INTO ONE
# ─────────────────────────────────────────────

def bundle_all_kmz(all_paths: list[Path], output_dir: Path) -> Path:
    """
    Merge every KMZ into a single master KMZ file so the Land & Power tool
    can load the full fiber map in one import.
    """
    log.info(f"\nBundling {len(all_paths)} KMZ files into master KMZ …")
    master_kml = simplekml.Kml(name="All Sites — Fiber & Power Map")
    ts = datetime.now().strftime("%Y%m%d_%H%M")

    for kmz_path in all_paths:
        folder_name = kmz_path.stem
        try:
            # KMZ is a zip; extract the inner doc.kml
            with zipfile.ZipFile(kmz_path, "r") as zf:
                kml_names = [n for n in zf.namelist() if n.endswith(".kml")]
                if not kml_names:
                    continue
                kml_text = zf.read(kml_names[0]).decode("utf-8", errors="replace")

            # Embed as a NetworkLink (keeps file size manageable)
            nl = master_kml.newnetworklink(name=folder_name)
            nl.link.href = str(kmz_path.resolve())
            nl.link.refreshmode = simplekml.RefreshMode.onchange

        except Exception as e:
            log.warning(f"  Could not embed {kmz_path.name}: {e}")

    bundle_path = output_dir / f"ALL_SITES_fiber_map_{ts}.kmz"
    master_kml.savekmz(str(bundle_path))
    log.info(f"  Master KMZ saved: {bundle_path.name}")
    return bundle_path


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="KMZ Fiber Map Builder for Land & Power Tool")
    p.add_argument("--api-only",   action="store_true", help="Only pull from API")
    p.add_argument("--local-only", action="store_true", help="Only scan local folder")
    p.add_argument("--remote-only",action="store_true", help="Only download remote URLs")
    p.add_argument("--sites",      default="",          help="Comma-separated US state codes to filter, e.g. VA,TX")
    p.add_argument("--output-dir", default=CONFIG["OUTPUT_DIR"], help="Where to save KMZ output")
    p.add_argument("--no-bundle",  action="store_true", help="Skip creating the master merged KMZ")
    return p.parse_args()


def main():
    args    = parse_args()
    out_dir = ensure_dir(Path(args.output_dir))
    states  = [s.strip().upper() for s in args.sites.split(",") if s.strip()] if args.sites else None
    all_run_all = not any([args.api_only, args.local_only, args.remote_only])

    log.info("=" * 60)
    log.info("  KMZ Fiber Map Builder")
    log.info(f"  Output dir : {out_dir.resolve()}")
    log.info(f"  State filter: {states or 'ALL'}")
    log.info("=" * 60)

    collected: list[Path] = []

    # 1. API source
    if args.api_only or all_run_all:
        collected += run_api_source(out_dir, states)

    # 2. Local folder scan
    if args.local_only or all_run_all:
        collected += run_local_source(out_dir)

    # 3. Remote downloads
    if args.remote_only or all_run_all:
        collected += run_remote_source(out_dir)

    log.info(f"\nTotal KMZ files collected: {len(collected)}")

    # 4. Bundle into one master KMZ
    if collected and CONFIG["BUNDLE_ALL"] and not args.no_bundle:
        bundle_all_kmz(collected, out_dir)

    log.info("\nDone! Import the KMZ files from:")
    log.info(f"  {out_dir.resolve()}")
    log.info("into your Land & Power tool to view the full fiber map.\n")


if __name__ == "__main__":
    main()
