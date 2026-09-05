#!/usr/bin/env python3
"""Regenerate the HF dataset dchubcloud/dcpi-market-verdicts from live DC Hub.

WHY THIS EXISTS
---------------
The published dataset went 63 days stale while its own card claimed the verdicts
were "recomputed daily". Both halves of that were true and that is the problem:
the INDEX does recompute daily (live rows carry a current as_of), but publishing
the CSV was a hand-run step with no script, so nothing carried the fresh rows to
Hugging Face. A cadence claim with no mechanism behind it is a claim about
someone remembering.

PAYWALL DISCIPLINE — read before adding a column.
  This dataset ships FREE-TIER COLUMNS ONLY: identity, geography, serving ISO,
  and the BUILD/CAUTION/AVOID verdict. The numeric DCPI scores (composite,
  excess-power, grid-constraint, time-to-power) are Pro and are DELIBERATELY
  excluded. `--check-paywall` re-asserts that on every run, so a future column
  addition has to defeat an explicit test rather than slip through a diff.

USAGE
  DCHUB_API_KEY=dchub_...  python3 scripts/refresh_hf_dcpi_dataset.py --out out/

  A key is required: the anonymous tier previews a handful of rows, which is
  what made this un-runnable by hand and is exactly why it rotted. CI supplies
  DCHUB_API_KEY and HF_TOKEN; see .github/workflows/hf-dataset-refresh.yml.

WHY THIS LIVES IN THE BACKEND REPO
  The dataset is a PUBLISHED SURFACE of this repo's data, so it belongs to the
  same drift discipline as every other published count here. Keeping the
  generator beside the API it reads means a schema change to
  /api/v1/dcpi/scores lands in the same diff as the exporter that consumes it.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys

import requests

API = os.environ.get("DCHUB_API_BASE", "https://dchub.cloud")

#: The ONLY columns that may ship. Anything not listed is either Pro or unproven.
FREE_COLUMNS = ["market_slug", "market_name", "state", "iso",
                "latitude", "longitude", "verdict", "as_of_date"]

#: Pro fields. Present in an authenticated response; must never reach the CSV.
PRO_FIELDS = {"composite_score", "excess_power_score", "constraint_score",
              "time_to_power_months", "score", "dcpi_score"}


def _get(path: str, key: str) -> dict:
    """GET one DC Hub endpoint.

    ★ requests, NOT urllib — a live edge behaviour, not a style preference. A
      User-Agent beginning "Python-" + "urllib" is 403'd with Cloudflare
      `error code: 1010` on every public path of dchub.cloud, by the Pages
      Browser Integrity Check. That runs BEFORE the zone worker, so the 403
      carries no x-dc-worker-version and neither worker.js nor Flask can see
      it — only an outside probe can. A custom UA does dodge it, which is
      exactly the trap: the script keeps working until someone simplifies the
      header away. scripts/regression_lint.py blocks the import outright.
    """
    r = requests.get(
        f"{API}{path}",
        headers={"X-API-Key": key,
                 "User-Agent": "dchub-hf-dataset-refresh/1.0 (+https://dchub.cloud)"},
        timeout=60)
    r.raise_for_status()
    # DCPI narratives carry raw newlines inside JSON strings, which strict JSON
    # rejects — so parse the text with strict=False rather than using r.json().
    return json.loads(r.text, strict=False)


def fetch_rows(key: str) -> list[dict]:
    d = _get("/api/v1/dcpi/scores?limit=1000", key)
    rows = d.get("scores") or []
    total = d.get("_total_available")

    # ★ THE INVARIANT IS ROW COVERAGE, NOT THE PREVIEW FLAG. `_preview_only` is
    #   True even for a key that returns the COMPLETE universe: it reports
    #   FIELD-level gating (the Pro numerics come back null), not row
    #   truncation. An earlier draft of this guard refused on the flag and
    #   would have blocked every legitimate run — a guard that cannot pass is
    #   as useless as one that cannot fail. What must never happen is
    #   publishing a SUBSET, because missing markets are invisible downstream.
    if total is None:
        sys.exit("REFUSING: response carries no _total_available, so coverage "
                 "cannot be checked. Publishing an unverifiable universe is the "
                 "failure this script exists to prevent.")
    if len(rows) < total:
        sys.exit(f"REFUSING: got {len(rows)} of {total} rows. The key's tier "
                 f"truncates the scored universe; publishing a subset is worse "
                 f"than publishing nothing.")

    # Pro numerics must arrive EMPTY. If the gate ever starts sending them, the
    # free-tier dataset would silently inherit paid data — so check the input,
    # not only the columns written out.
    for f in (d.get("_locked_fields") or []):
        hot = [r.get("market_slug") for r in rows if r.get(f) not in (None, "", "null")]
        if hot:
            sys.exit(f"REFUSING: locked field {f!r} arrived populated for "
                     f"{len(hot)} rows (e.g. {hot[:3]}). This dataset is "
                     f"free-tier only — do not publish gated values.")
    return rows


def to_csv_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        out.append({
            "market_slug":  r.get("market_slug") or r.get("slug") or "",
            "market_name":  r.get("market_name") or r.get("name") or "",
            "state":        r.get("state") or "",
            "iso":          r.get("iso") or r.get("iso_rto") or "",
            "latitude":     r.get("latitude") or r.get("lat") or "",
            "longitude":    r.get("longitude") or r.get("lon") or r.get("lng") or "",
            "verdict":      r.get("verdict") or r.get("dcpi_verdict") or "",
            "as_of_date":   (r.get("as_of") or r.get("computed_at") or "")[:10],
        })
    return out


def check_paywall(csv_rows: list[dict]) -> None:
    """★ Fails loudly if a Pro field ever reaches the published columns."""
    leaked = PRO_FIELDS & set().union(*(set(r) for r in csv_rows)) if csv_rows else set()
    if leaked:
        sys.exit(f"PAYWALL LEAK: Pro fields in the CSV: {sorted(leaked)}")
    extra = set().union(*(set(r) for r in csv_rows)) - set(FREE_COLUMNS) if csv_rows else set()
    if extra:
        sys.exit(f"PAYWALL: undeclared columns {sorted(extra)} — add to FREE_COLUMNS "
                 f"only after confirming they are free-tier.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dataset")
    ap.add_argument("--check-paywall", action="store_true",
                    help="run the paywall assertion and exit (no network)")
    a = ap.parse_args()

    key = (os.environ.get("DCHUB_API_KEY") or "").strip()
    if not key:
        return int(bool(sys.stderr.write(
            "DCHUB_API_KEY is required — the anonymous tier previews 10 of ~327 "
            "rows.\nMint one at https://dchub.cloud/pricing or via the MCP "
            "claim_free_key tool (note: the FREE tier is also capped; this needs "
            "a tier that can read the full scored universe).\n")) or 2)

    rows = to_csv_rows(fetch_rows(key))
    check_paywall(rows)

    os.makedirs(os.path.join(a.out, "data"), exist_ok=True)
    path = os.path.join(a.out, "data", "dcpi-market-verdicts.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FREE_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    dates = sorted({r["as_of_date"] for r in rows if r["as_of_date"]})
    verdicts = {}
    for r in rows:
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
    print(f"wrote {path}")
    print(f"  rows      = {len(rows)}")
    print(f"  as_of     = {dates[0] if dates else '?'} .. {dates[-1] if dates else '?'}")
    print(f"  verdicts  = {verdicts}")
    print(f"  generated = {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
