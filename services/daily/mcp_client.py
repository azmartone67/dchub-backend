"""Thin HTTP client for the DC Hub MCP (dchub.cloud).

Uses the REST-equivalent endpoints so this runs outside the MCP runtime
(Railway / GitHub Actions / Replit). For production you plug your Enterprise
API key into the DCHUB_API_KEY env var.

Resolution ladder (tried in order):
  1. DRY_RUN or no API key            → bundled seed, tagged "seed (no API key)"
  2. Per-state /facilities pagination → full live, tagged "DC Hub API · live per-state"
  3. /stats global + seed distribution → hybrid, tagged with live totals in the source line
  4. Bundled seed                      → fallback, tagged "seed (live API failed: <reason>)"

Every step logs what it tried and why it fell through, so Railway deploy
logs make it obvious which ladder rung produced the snapshot.
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

# map API status strings → our bucket keys (case-insensitive lookup key)
STATUS_MAP = {
    "operational": "op",
    "under construction": "uc",
    "expanding": "uc",
    "planned": "ann",
    "announced": "ann",
    "planning": "ann",
    "under development": "ann",
    "approved": "ann",
}

US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC",
    "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
    "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
    "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY",
]

STATE_NAMES = {
    "AL": "ALABAMA", "AK": "ALASKA", "AZ": "ARIZONA", "AR": "ARKANSAS",
    "CA": "CALIFORNIA", "CO": "COLORADO", "CT": "CONNECTICUT", "DE": "DELAWARE",
    "DC": "DC", "FL": "FLORIDA", "GA": "GEORGIA", "HI": "HAWAII", "ID": "IDAHO",
    "IL": "ILLINOIS", "IN": "INDIANA", "IA": "IOWA", "KS": "KANSAS",
    "KY": "KENTUCKY", "LA": "LOUISIANA", "ME": "MAINE", "MD": "MARYLAND",
    "MA": "MASSACHUSETTS", "MI": "MICHIGAN", "MN": "MINNESOTA",
    "MS": "MISSISSIPPI", "MO": "MISSOURI", "MT": "MONTANA", "NE": "NEBRASKA",
    "NV": "NEVADA", "NH": "NEW HAMPSHIRE", "NJ": "NEW JERSEY",
    "NM": "NEW MEXICO", "NY": "NEW YORK", "NC": "NORTH CAROLINA",
    "ND": "NORTH DAKOTA", "OH": "OHIO", "OK": "OKLAHOMA", "OR": "OREGON",
    "PA": "PENNSYLVANIA", "RI": "RHODE ISLAND", "SC": "SOUTH CAROLINA",
    "SD": "SOUTH DAKOTA", "TN": "TENNESSEE", "TX": "TEXAS", "UT": "UTAH",
    "VT": "VERMONT", "VA": "VIRGINIA", "WA": "WASHINGTON",
    "WV": "WEST VIRGINIA", "WI": "WISCONSIN", "WY": "WYOMING",
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
    return httpx.Client(
        base_url=API_BASE,
        timeout=30.0,
        headers=headers,
        follow_redirects=True,
    )


def fetch_stats(client: httpx.Client) -> dict | None:
    """GET /stats — returns global aggregates (by_status, by_source, totals)."""
    try:
        r = client.get("/stats")
        if r.status_code == 200:
            return r.json()
        log.warning("/stats returned %d", r.status_code)
    except httpx.HTTPError as e:
        log.warning("/stats failed: %s", e)
    return None


def _bucket_status(status: str | None, row: StateRow) -> None:
    if not status:
        return
    key = STATUS_MAP.get(status.strip().lower())
    if key:
        setattr(row, key, getattr(row, key) + 1)


def search_facilities(
    client: httpx.Client, state: str, offset: int = 0, limit: int = 100
) -> dict:
    """One page of facilities for a state."""
    params = {"country": "US", "state": state, "limit": limit, "offset": offset}
    for path in ("/facilities", "/facilities/search"):
        r = client.get(path, params=params)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("no facilities endpoint matched")


def fetch_state_counts(state: str, client: httpx.Client | None = None) -> StateRow:
    """Paginate through all facilities in `state` and bucket by status.

    Defensive: we only count facilities whose returned `state` actually
    matches the requested one. The DC Hub API has been observed to ignore
    the `state` filter for unauthenticated / insufficient-tier callers and
    return a global preview instead; without this check those preview rows
    would be counted against every state.
    """
    row = StateRow(name=STATE_NAMES.get(state, state))
    own_client = client is None
    client = client or _client()
    try:
        offset = 0
        while True:
            payload = search_facilities(client, state, offset=offset, limit=100)
            data = payload.get("data", [])
            for f in data:
                if (f.get("state") or "").upper() != state:
                    # filter was ignored — skip cross-state rows
                    continue
                _bucket_status(f.get("status"), row)
            if len(data) < 100:
                break
            offset += 100
            time.sleep(0.1)  # be polite
    finally:
        if own_client:
            client.close()
    return row


def _extract_global_status_counts(stats: dict) -> dict | None:
    """Pull Operational / Under Construction / Announced global totals from /stats.

    The real /stats response nests the counts under `data.by_status` with
    PascalCase keys ("Operational", "Under Construction", "Planned", ...)
    plus a few variants ("announced" lowercase, "Approved", "Expanding",
    "Under Development", "Planning"). We fold them all into three buckets.
    """
    if not isinstance(stats, dict):
        return None
    data = stats.get("data") if isinstance(stats.get("data"), dict) else {}
    by_status = data.get("by_status") or stats.get("by_status")
    if not isinstance(by_status, dict):
        return None
    lc = {str(k).strip().lower(): v for k, v in by_status.items()}

    def _n(*keys: str) -> int:
        return sum(int(lc.get(k, 0) or 0) for k in keys)

    op = _n("operational")
    uc = _n("under construction", "expanding")
    ann = _n(
        "announced", "planned", "planning",
        "under development", "approved",
    )
    if op + uc + ann == 0:
        return None
    return {"op": op, "uc": uc, "ann": ann}


def _seed_snapshot() -> dict:
    return json.loads((Path(__file__).parent / "data.json").read_text())


def _scale_seed_to_global(global_counts: dict) -> list[dict]:
    """Scale the bundled seed per-state distribution to match live globals.

    Preserves the relative per-state shape from Aterio while pinning the
    column sums to whatever `/stats` currently reports.
    """
    seed_rows = _seed_snapshot().get("states", [])
    if not seed_rows:
        return []
    s_op = sum(r.get("op", 0) for r in seed_rows) or 1
    s_uc = sum(r.get("uc", 0) for r in seed_rows) or 1
    s_ann = sum(r.get("ann", 0) for r in seed_rows) or 1
    g_op, g_uc, g_ann = global_counts["op"], global_counts["uc"], global_counts["ann"]
    scaled = [
        {
            "name": r["name"],
            "op": max(0, round(r.get("op", 0) * g_op / s_op)),
            "uc": max(0, round(r.get("uc", 0) * g_uc / s_uc)),
            "ann": max(0, round(r.get("ann", 0) * g_ann / s_ann)),
        }
        for r in seed_rows
    ]
    scaled.sort(key=lambda r: r["op"] + r["uc"] + r["ann"], reverse=True)
    return scaled


def fetch_snapshot() -> dict:
    """Pull every state. Returns the full snapshot dict (renderable by render.py)."""
    import datetime

    if DRY_RUN or not API_KEY:
        log.warning("DRY_RUN or no DCHUB_API_KEY — using bundled seed.")
        snap = _seed_snapshot()
        snap["source"] = snap.get("source", "Aterio") + " · seed (no API key)"
        return snap

    try:
        with _client() as c:
            stats = fetch_stats(c)
            global_counts = _extract_global_status_counts(stats) if stats else None
            if global_counts:
                log.info(
                    "live /stats globals: op=%d uc=%d ann=%d",
                    global_counts["op"], global_counts["uc"], global_counts["ann"],
                )

            # Strategy 1 — per-state /facilities pagination (best path if Enterprise auth works)
            log.info("strategy 1: per-state /facilities pagination")
            rows: list[dict] = []
            for st in US_STATES:
                try:
                    rows.append(fetch_state_counts(st, c).as_dict())
                except (httpx.HTTPStatusError, RuntimeError) as e:
                    log.error("state=%s failed: %s", st, e)
                    rows.append(StateRow(name=STATE_NAMES[st]).as_dict())
            nonzero = sum(1 for r in rows if r["op"] + r["uc"] + r["ann"] > 0)
            total = sum(r["op"] + r["uc"] + r["ann"] for r in rows)
            log.info("strategy 1 result: %d/%d states nonzero, total=%d",
                     nonzero, len(rows), total)

            if nonzero >= 20 and total >= 500:
                rows.sort(key=lambda r: r["op"] + r["uc"] + r["ann"], reverse=True)
                return {
                    "as_of": datetime.date.today().isoformat(),
                    "source": "DC Hub API · live per-state",
                    "generated": datetime.datetime.utcnow().isoformat() + "Z",
                    "states": rows,
                    "unit": "facilities",
                }

            # Strategy 2 — hybrid: live globals from /stats + seed distribution
            if global_counts:
                log.warning(
                    "strategy 1 unusable (nonzero=%d, total=%d); "
                    "using /stats globals + seed per-state distribution",
                    nonzero, total,
                )
                scaled = _scale_seed_to_global(global_counts)
                if scaled:
                    return {
                        "as_of": datetime.date.today().isoformat(),
                        "source": (
                            f"DC Hub /stats (live: {global_counts['op']} op · "
                            f"{global_counts['uc']} uc · {global_counts['ann']} ann) "
                            "+ Aterio per-state distribution"
                        ),
                        "generated": datetime.datetime.utcnow().isoformat() + "Z",
                        "states": scaled,
                        "unit": "facilities",
                    }

            raise RuntimeError(
                f"live strategies failed: per-state nonzero={nonzero} total={total}, "
                f"stats_globals={bool(global_counts)}"
            )

    except Exception as e:  # noqa: BLE001
        log.error("live API fetch failed, falling back to seed: %s", e)
        snap = _seed_snapshot()
        snap["source"] = (
            snap.get("source", "Aterio")
            + f" · seed (live API failed: {str(e)[:80]})"
        )
        snap["generated"] = datetime.datetime.utcnow().isoformat() + "Z"
        return snap


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    snap = fetch_snapshot()
    print(json.dumps(snap, indent=2)[:500], "...")
