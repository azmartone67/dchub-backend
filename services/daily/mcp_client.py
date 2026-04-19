"""Thin HTTP client for the DC Hub MCP (dchub.cloud).

Uses the REST-equivalent endpoints so this runs outside the MCP runtime
(Railway / GitHub Actions / Replit). For production you plug your
Enterprise API key into the DCHUB_API_KEY env var.

If the DC Hub API is unreachable or DRY_RUN=1, we fall back to the last
cached snapshot in Neon (or the bundled data.json seed).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

API_BASE = os.environ.get("DCHUB_API_BASE", "https://dchub.cloud/api/v1")
API_KEY = os.environ.get("DCHUB_API_KEY", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

# map API status strings → our bucket keys
STATUS_MAP = {
    "Operational": "op",
    "Under Construction": "uc",
    "Planned": "ann",
    "Announced": "ann",
}

US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL",
    "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME",
    "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]

STATE_NAMES = {
    "AL": "ALABAMA", "AK": "ALASKA", "AZ": "ARIZONA", "AR": "ARKANSAS",
    "CA": "CALIFORNIA", "CO": "COLORADO", "CT": "CONNECTICUT",
    "DE": "DELAWARE", "DC": "DC", "FL": "FLORIDA", "GA": "GEORGIA",
    "HI": "HAWAII", "ID": "IDAHO", "IL": "ILLINOIS", "IN": "INDIANA",
    "IA": "IOWA", "KS": "KANSAS", "KY": "KENTUCKY", "LA": "LOUISIANA",
    "ME": "MAINE", "MD": "MARYLAND", "MA": "MASSACHUSETTS",
    "MI": "MICHIGAN", "MN": "MINNESOTA", "MS": "MISSISSIPPI",
    "MO": "MISSOURI", "MT": "MONTANA", "NE": "NEBRASKA", "NV": "NEVADA",
    "NH": "NEW HAMPSHIRE", "NJ": "NEW JERSEY", "NM": "NEW MEXICO",
    "NY": "NEW YORK", "NC": "NORTH CAROLINA", "ND": "NORTH DAKOTA",
    "OH": "OHIO", "OK": "OKLAHOMA", "OR": "OREGON", "PA": "PENNSYLVANIA",
    "RI": "RHODE ISLAND", "SC": "SOUTH CAROLINA", "SD": "SOUTH DAKOTA",
    "TN": "TENNESSEE", "TX": "TEXAS", "UT": "UTAH", "VT": "VERMONT",
    "VA": "VIRGINIA", "WA": "WASHINGTON", "WV": "WEST VIRGINIA",
    "WI": "WISCONSIN", "WY": "WYOMING",
}


@dataclass
class StateRow:
    name: str
    op: int = 0
    uc: int = 0
    ann: int = 0

    def as_dict(self) -> dict:
        return {"name": self.name, "op": self.op, "uc": self.uc, "ann": self.ann}


def _client() -> httpx.Client:
    headers = {"User-Agent": "dchub-daily/1.0"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    return httpx.Client(base_url=API_BASE, timeout=30.0, headers=headers,
                        follow_redirects=True)


def fetch_stats(client: httpx.Client) -> dict | None:
    """Try the /stats endpoint first — may return per-state rollups in one call."""
    try:
        r = client.get("/stats")
        if r.status_code == 200:
            return r.json()
    except httpx.HTTPError as e:
        log.warning("/stats failed: %s", e)
    return None


def search_facilities(client: httpx.Client, state: str, offset: int = 0,
                      limit: int = 100) -> dict:
    """One page of facilities for a state."""
    # Try the documented /facilities endpoint; if DC Hub uses /facilities/search
    # instead, it'll 404 once and we'll retry with the alt path.
    params = {"country": "US", "state": state, "limit": limit, "offset": offset}
    for path in ("/facilities", "/facilities/search"):
        r = client.get(path, params=params)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("no facilities endpoint matched")


def fetch_state_counts(state: str, client: httpx.Client | None = None) -> StateRow:
    """Paginate through all facilities in `state` and bucket by status."""
    row = StateRow(name=STATE_NAMES.get(state, state))
    own_client = client is None
    client = client or _client()
    try:
        offset = 0
        while True:
            payload = search_facilities(client, state, offset=offset, limit=100)
            data = payload.get("data", [])
            for f in data:
                key = STATUS_MAP.get(f.get("status"), None)
                if key:
                    setattr(row, key, getattr(row, key) + 1)
            if len(data) < 100:
                break
            offset += 100
            time.sleep(0.1)  # be polite
    finally:
        if own_client:
            client.close()
    return row


def _stats_to_rows(stats: dict) -> list[dict] | None:
    """Best-effort parse of /stats into our per-state shape.

    DC Hub's /stats response format varies; we look for a by_state / states /
    per_state block with op/uc/ann (or operational/under_construction/announced).
    If we can't find one, return None so the caller falls back to pagination.
    """
    candidates = (
        stats.get("by_state"), stats.get("states"), stats.get("per_state"),
        stats.get("us", {}).get("by_state") if isinstance(stats.get("us"), dict) else None,
    )
    raw = next((c for c in candidates if c), None)
    if not raw:
        return None

    def _pick(obj: dict, *keys: str) -> int:
        for k in keys:
            if k in obj and obj[k] is not None:
                return int(obj[k])
        return 0

    rows = []
    items = raw.items() if isinstance(raw, dict) else [(r.get("state", r.get("code", "")), r) for r in raw]
    for code, vals in items:
        if not isinstance(vals, dict):
            continue
        name = STATE_NAMES.get((code or "").upper()) or (vals.get("name") or code or "").upper()
        rows.append({
            "name": name,
            "op":  _pick(vals, "op",  "operational", "Operational"),
            "uc":  _pick(vals, "uc",  "under_construction", "Under Construction"),
            "ann": _pick(vals, "ann", "announced", "planned", "Announced", "Planned"),
        })
    return rows or None


def fetch_snapshot() -> dict:
    """Pull every state. Returns the full snapshot dict (renderable by render.py)."""
    if DRY_RUN or not API_KEY:
        log.warning("DRY_RUN or no DCHUB_API_KEY — using bundled seed data.")
        return json.loads((Path(__file__).parent / "data.json").read_text())

    import datetime
    rows: list[dict] | None = None
    with _client() as c:
        stats = fetch_stats(c)
        if stats:
            rows = _stats_to_rows(stats)
            if rows:
                log.info("got %d rows from /stats", len(rows))

        if not rows:
            log.info("falling back to per-state /facilities pagination")
            rows = []
            for st in US_STATES:
                try:
                    rows.append(fetch_state_counts(st, c).as_dict())
                except httpx.HTTPStatusError as e:
                    log.error("state=%s failed: %s", st, e)
                    rows.append(StateRow(name=STATE_NAMES[st]).as_dict())

    rows.sort(key=lambda r: r["op"] + r["uc"] + r["ann"], reverse=True)
    return {
        "as_of": datetime.date.today().isoformat(),
        "source": "Aterio (via DC Hub MCP, Enterprise)",
        "generated": datetime.datetime.utcnow().isoformat() + "Z",
        "states": rows,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    snap = fetch_snapshot()
    print(json.dumps(snap, indent=2)[:500], "...")
