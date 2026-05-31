"""Thin HTTP client for the DC Hub MCP (dchub.cloud).

Uses the REST-equivalent endpoints so this runs outside the MCP runtime
(Railway / GitHub Actions / Replit). For production you plug your Enterprise
API key into the DCHUB_API_KEY env var.

Resolution ladder (tried in order):
  1. DRY_RUN or no API key            → bundled seed, "seed (no API key)"
  2. /stats + seed distribution       → hybrid, live globals + Aterio shape
  3. Bundled seed                      → fallback, "seed (live API failed: <reason>)"

We deliberately make AT MOST ONE external call per refresh to stay under
DC Hub's Free-tier rate limits (~50 calls/day). The previous per-state
pagination burned the entire daily quota on a single refresh. If/when
Enterprise auth is wired up with a more generous quota, the per-state
helpers (still in this file) can be re-enabled.
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
    """GET /stats with retries on rate-limit and transient server errors.

    Uses Retry-After when present, else exponential backoff capped at 10s per attempt.
    Up to 3 attempts total. Returns None only if every attempt fails.
    """
    for attempt in range(3):
        try:
            r = client.get("/stats")
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429 or r.status_code >= 500:
                ra = r.headers.get("retry-after")
                try:
                    wait = float(ra) if ra else 2.0 * (attempt + 1)
                except (ValueError, TypeError):
                    wait = 2.0 * (attempt + 1)
                wait = min(wait, 10.0)
                log.warning(
                    "/stats %d (attempt %d/3), retrying after %.1fs",
                    r.status_code, attempt + 1, wait,
                )
                time.sleep(wait)
                continue
            log.warning("/stats %d (giving up)", r.status_code)
            return None
        except httpx.HTTPError as e:
            log.warning("/stats error (attempt %d/3): %s", attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))
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
    """One page of facilities for a state. (Kept for future use; NOT called in the
    default refresh path because DC Hub Free tier caps at ~50 calls/day.)"""
    params = {"country": "US", "state": state, "limit": limit, "offset": offset}
    for path in ("/facilities", "/facilities/search"):
        r = client.get(path, params=params)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("no facilities endpoint matched")


def fetch_state_counts(state: str, client: httpx.Client | None = None) -> StateRow:
    """Paginated per-state bucket counts. Unused in default refresh (rate-limit);
    callers should only invoke this with an Enterprise-tier key + sufficient quota."""
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
                    continue
                _bucket_status(f.get("status"), row)
            if len(data) < 100:
                break
            offset += 100
            time.sleep(0.1)
    finally:
        if own_client:
            client.close()
    return row


def _extract_global_status_counts(stats: dict) -> dict | None:
    """Pull Operational / Under Construction / Announced global totals from /stats.

    Real /stats response nests counts under `data.by_status` with PascalCase
    keys plus a few variants. We fold them all into three buckets (case-insensitive).
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
    """Scale the bundled seed per-state distribution to match live globals."""
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
    """Pull a LIVE per-state facility snapshot from the DC Hub backend.

    v5 (2026-05-14): genuinely dynamic. Until now this scaled a static
    2026-03-31 seed to live global totals — the per-state SHAPE never
    moved, so /daily looked frozen no matter how many new sites landed.
    The backend now exposes /api/v1/facilities/state-status-counts: real
    per-US-state operational / under-construction / announced counts in
    one public GROUP BY — no API key, no rate limit. Pull that directly.
    The bundled seed remains ONLY as a last-resort fallback.
    """
    import datetime

    # r37 (2026-05-31): REMOVED the `if DRY_RUN: return seed` short-circuit. A
    # stale DRY_RUN=1 env var on the heroic-reprieve service was forcing the
    # bundled 2026-03-31 seed on every render — so /daily was frozen at that
    # date and count for 2 months, even though /api/v1/facilities/state-status-
    # counts returns live, GROWING per-state totals (verified: 200, as_of=today,
    # op 2212 / uc 126 / ann 135). Always try live now; the bundled seed remains
    # ONLY as the genuine-failure fallback in the except block below (which also
    # covers a no-network local dev). DRY_RUN no longer suppresses live data.
    try:
        url = API_BASE.rstrip("/") + "/facilities/state-status-counts"
        headers = {"User-Agent": "dchub-daily/5.0"}
        if API_KEY:
            headers["X-API-Key"] = API_KEY  # optional — endpoint is public
        r = httpx.get(url, headers=headers, timeout=30.0)
        r.raise_for_status()
        d = r.json()
        states = d.get("states") or []
        total = sum((s.get("op", 0) or 0) + (s.get("uc", 0) or 0)
                    + (s.get("ann", 0) or 0) for s in states)
        if states and total > 0:
            log.info("live state-status-counts: %d states, %d facilities",
                     len(states), total)
            return {
                "as_of": d.get("as_of") or datetime.date.today().isoformat(),
                "source": d.get("source", "DC Hub live facilities DB"),
                "generated": datetime.datetime.utcnow().isoformat() + "Z",
                "states": states,
                "unit": "facilities",
            }
        raise RuntimeError(
            f"state-status-counts returned no usable rows "
            f"(states={len(states)} total={total})")

    except Exception as e:  # noqa: BLE001
        log.error("live state-status-counts failed, falling back to seed: %s", e)
        snap = _seed_snapshot()
        snap["source"] = (
            snap.get("source", "Aterio")
            + f" · seed (live failed: {str(e)[:80]})"
        )
        snap["generated"] = datetime.datetime.utcnow().isoformat() + "Z"
        return snap


def fetch_gdci() -> dict | None:
    """GET dchub.cloud/api/gdci — returns composite index snapshot."""
    headers = {"User-Agent": "dchub-daily/1.0"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    try:
        r = httpx.get("https://dchub.cloud/api/gdci", headers=headers, timeout=30.0)
        if r.status_code == 200:
            return r.json()
        log.warning("fetch_gdci: %d", r.status_code)
    except httpx.HTTPError as e:
        log.warning("fetch_gdci: %s", e)
    return None


def fetch_grid() -> dict | None:
    """GET dchub.cloud/api/v1/grid-intelligence — returns 5 ISO regions snapshot."""
    headers = {"User-Agent": "dchub-daily/1.0"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    try:
        r = httpx.get("https://dchub.cloud/api/v1/grid-intelligence", headers=headers, timeout=30.0)
        if r.status_code == 200:
            return r.json()
        log.warning("fetch_grid: %d", r.status_code)
    except httpx.HTTPError as e:
        log.warning("fetch_grid: %s", e)
    return None



if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    snap = fetch_snapshot()
    print(json.dumps(snap, indent=2)[:500], "...")
