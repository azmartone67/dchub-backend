"""map_layer_probe — Land & Power map layer health, on a schedule that runs.

★ WHY THIS EXISTS, WHEN check_land_power_map_health ALREADY DID

On 2026-09-04 the map's hazard layer had been serving 503 worldwide and the
transmission / transformer / grid layers rendered empty over Germany. The loop
reported nothing. `routes/brain_security_detectors.check_land_power_map_health`
was supposed to catch exactly this. It could not, for FOUR independent reasons —
fixing any one alone would still have missed the outage:

  1. NOT SCHEDULED. It is excluded from surveillance_sweep's
     `_SWEEP_SECURITY_DETECTORS`, and the tuple that does contain it
     (brain_security_detectors.SECURITY_DETECTORS) is only iterated when
     DCHUB_SECURITY_RADAR_ENABLED=1, which is not set in production.
  2. NO CALLER. Its documented entry point, POST
     /api/v1/admin/brain/security-scan, has no caller anywhere — not
     cron_heartbeat._DISPATCH, not .github/workflows, not Railway cron. The only
     hit in the tree is a manual curl example in STEP_BY_STEP.md.
  3. NEVER PERSISTED. It RETURNS findings from an HTTP handler. Nothing writes
     them to brain_findings, so even a firing detector left zero rows and reached
     no human surface.
  4. VACUOUS PROBES. Its status ladder is
         if status in (200,304,400): continue
         if status == 0: ...        elif (401,403): ...      elif >= 500: ...
     404 / 402 / 3xx match NOTHING and produce no finding — a DELETED route reads
     as healthy. Measured 2026-09-04, 2 of its 8 targets were already 404
     (/api/v1/infrastructure/transmission has no handler in the repo;
     /api/v1/connectivity/ixps is swallowed by /api/v1/connectivity/<fac_id>).
     And every probe used one US coordinate, so no geography gap was visible.

★ THE INVERSION: AN ALLOW-SET, NOT A FAILURE ENUMERATION

A guard that lists the ways things break can only catch the breaks someone
thought of; everything else falls through to "healthy". This module asserts what
GOOD looks like and treats every deviation as a finding. A route that 404s, a
paywall that starts 402ing a public layer, a redirect loop — all fire, because
none of them are in the allow-set. Nobody has to have predicted them.

★ STATUS IS NOT ENOUGH — ROWS ARE THE POINT

The user-visible symptom was never a status code: it was an EMPTY LAYER under a
badge that still advertised "300k". So each probe also asserts a row count, and
does it per-geography:

  coverage="global"  — must return rows at BOTH canaries. A global dataset with
                       zero rows anywhere is broken. (global-hazards, the layer
                       that actually went dark, is one of these.)
  coverage="us"      — must return rows at the US canary. OUTSIDE the US it may
                       legitimately return zero, but it must still answer 200:
                       a US-only endpoint that ERRORS abroad is a defect, and
                       measurably one — /api/v1/energy/power-plants/nearby
                       returns 400 "Could not determine state from coordinates"
                       at Frankfurt instead of an empty result.

The coverage column is deliberately data, not prose: it is the same fact the
map's layer badges need in order to stop printing "0" where they mean "this
dataset does not cover what you are looking at".

★ CANARIES

US      Ashburn VA 39.0438,-77.4874 — the densest US data-center market. Chosen
        over the old Tonopah NV canary precisely BECAUSE it is dense: a remote
        desert coordinate cannot distinguish "layer broken" from "nothing here".
NON-US  Frankfurt DE 50.1109,8.6821 — FLAP-6 metro, dense European market, and
        the geography the 2026-09-04 report came from.

All statuses/row-counts in the table below were measured live on 2026-09-04
against https://dchub.cloud. They are the baseline this probe defends.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from flask import Blueprint, jsonify, request

map_layer_probe_bp = Blueprint("map_layer_probe", __name__)

# Loopback on Railway (same shape as cron_heartbeat.BASE) so the probe measures
# the origin rather than whatever the CF edge happens to have cached.
_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else "https://dchub.cloud"
)

US = (39.0438, -77.4874)      # Ashburn VA
NON_US = (50.1109, 8.6821)    # Frankfurt DE


def _rows(field: str) -> Callable:
    """Row-count extractor for `{... "<field>": [...]}`. Returns None when the
    field is missing or not a list — that is a SHAPE failure, distinct from a
    genuine zero, and is reported as such."""
    def get(doc):
        if not isinstance(doc, dict):
            return None
        v = doc.get(field)
        return len(v) if isinstance(v, list) else None
    return get


def _rows_line_count(doc):
    """transmission-proximity reports a scalar `line_count` plus a `top` list."""
    if not isinstance(doc, dict):
        return None
    n = doc.get("line_count")
    if isinstance(n, bool) or not isinstance(n, int):
        return None
    return n


@dataclass(frozen=True)
class Probe:
    key: str            # stable id; becomes the finding's `url`
    path: str           # may contain {lat} / {lng}
    layers: str         # which map layer(s) this backs, for the human detail
    coverage: str       # "global" | "us"
    rows: Callable      # doc -> row count, or None if the shape is wrong
    allow: frozenset = frozenset({200})  # acceptable statuses IN coverage


# ── the probe table ───────────────────────────────────────────────────
#
# Measured 2026-09-04 (Ashburn / Frankfurt):
#   global-hazards          503 / 503   ← THE OUTAGE. Not probed by the old detector.
#   global-ixps             881 / 881   global
#   facilities                1 /   1   global
#   transmission-proximity    8 /   0   us
#   hifld/substations       500 /   0   us
#   gas-pipelines            20 /   0   us
#   power-plants/nearby      50 / HTTP 400  us  ← errors abroad instead of empty
_PROBES: tuple[Probe, ...] = (
    Probe(
        key="global_hazards",
        path="/api/v1/infrastructure/global-hazards",
        layers="Hazards (GDACS multi-hazard overlay)",
        coverage="global",
        rows=_rows("features"),
    ),
    Probe(
        key="global_ixps",
        path="/api/v1/infrastructure/global-ixps",
        layers="PeeringDB IX",
        coverage="global",
        rows=_rows("features"),
    ),
    Probe(
        key="facilities",
        path="/api/facilities?limit=1",
        layers="Facility base layer",
        coverage="global",
        rows=_rows("data"),
    ),
    Probe(
        key="transmission_proximity",
        path="/api/v1/grid/transmission-proximity?lat={lat}&lng={lng}&radius_km=40",
        layers="Transmission / HIFLD Trans",
        coverage="us",
        rows=_rows_line_count,
    ),
    Probe(
        key="hifld_substations",
        path="/api/v2/infrastructure/hifld/substations"
             "?lat={lat}&lng={lng}&radius=100&min_kv=138&limit=500",
        layers="Transformers / HV substations",
        coverage="us",
        rows=_rows("substations"),
    ),
    Probe(
        key="gas_pipelines",
        path="/api/v1/gas-pipelines?lat={lat}&lng={lng}&radius=50&limit=20",
        layers="Gas Pipelines",
        coverage="us",
        rows=_rows("pipelines"),
    ),
    Probe(
        key="power_plants_nearby",
        path="/api/v1/energy/power-plants/nearby?lat={lat}&lng={lng}&radius=50",
        layers="Power Plants",
        coverage="us",
        rows=_rows("plants"),
    ),
)


# The hazard layer alone is ~2.5 MB (522 GDACS features), so a small read cap
# TRUNCATES a valid body and the JSON parse then fails — reporting a healthy
# endpoint as malformed. Caught on the first live run against production, which
# the stubbed unit tests structurally could not catch. The cap stays (an
# unbounded read off a misbehaving upstream is its own hazard) but truncation is
# now reported as truncation, never as "not JSON".
_READ_CAP = 32 * 1024 * 1024


def _fetch(path: str, timeout: float = 20.0) -> tuple[int, str]:
    """(status, body). status 0 means the request never completed."""
    url = _BASE + path
    req = urllib.request.Request(url, method="GET")
    # UA must not look internal: this probe audits what the map's own browser
    # session sees, and an internal-looking UA can bypass the tier gate — the
    # false-positive trap documented in brain_security_detectors._probe.
    req.add_header("User-Agent", "dc-map-layer-probe/1.0")
    req.add_header("Accept", "application/json")
    req.add_header("Referer", "https://dchub.cloud/land-power-map")
    req.add_header("Origin", "https://dchub.cloud")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(_READ_CAP).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(20_000).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        return 0, f"{type(e).__name__}: {str(e)[:200]}"


def _check_one(p: Probe, lat: float, lng: float, where: str) -> dict | None:
    """Run one probe at one canary. Returns a finding dict, or None if healthy.

    The ONLY healthy path is: status in p.allow, body parses, shape is right,
    and the row count clears the bar for this geography. Everything else is a
    finding — including statuses nobody enumerated.
    """
    path = p.path.format(lat=lat, lng=lng)
    status, body = _fetch(path)
    in_coverage = (p.coverage == "global") or (where == "us")
    tag = f"{p.key}@{where}"

    if status == 0:
        return {
            "issue": "map_layer_unreachable",
            "url": tag,
            "count": 1,
            "detail": (f"Map layer '{p.layers}' ({path}) did not complete from "
                       f"inside the container: {body[:200]}"),
        }

    if status not in p.allow:
        # ALLOW-SET, not a failure enumeration: 404 / 402 / 3xx land here too.
        if not in_coverage:
            return {
                "issue": "map_layer_outside_coverage_error",
                "url": tag,
                "count": status,
                "detail": (
                    f"Map layer '{p.layers}' ({path}) returned HTTP {status} at "
                    f"the non-US canary. This is a US-only dataset, so ZERO rows "
                    f"would be correct — but it must still answer "
                    f"{sorted(p.allow)} with an empty result rather than error. "
                    f"A layer that errors abroad cannot be told apart from a "
                    f"layer that is broken. Body: {body[:180]!r}"),
            }
        return {
            "issue": "map_layer_bad_status",
            "url": tag,
            "count": status,
            "detail": (f"Map layer '{p.layers}' ({path}) returned HTTP {status}; "
                       f"allowed: {sorted(p.allow)}. The layer renders empty for "
                       f"users. Body: {body[:180]!r}"),
        }

    try:
        doc = json.loads(body)
    except Exception:
        truncated = len(body) >= _READ_CAP
        return {
            "issue": "map_layer_bad_shape",
            "url": tag,
            "count": 1,
            "detail": (
                (f"Map layer '{p.layers}' ({path}) returned HTTP {status} and a "
                 f"body of {len(body)} bytes that hit this probe's {_READ_CAP}-byte "
                 f"read cap, so it could not be parsed. This is a PROBE limit, not "
                 f"proof the endpoint is broken — raise _READ_CAP."
                 if truncated else
                 f"Map layer '{p.layers}' ({path}) returned HTTP {status} "
                 f"but the body is not JSON. Body: {body[:180]!r}")),
        }

    n = p.rows(doc)
    if n is None:
        # A 200 carrying an error/upgrade envelope instead of the expected list
        # lands here. Status alone would have called this healthy.
        keys = list(doc.keys())[:10] if isinstance(doc, dict) else type(doc).__name__
        return {
            "issue": "map_layer_bad_shape",
            "url": tag,
            "count": 1,
            "detail": (f"Map layer '{p.layers}' ({path}) returned HTTP {status} "
                       f"but not the expected shape — the row field is missing "
                       f"or not a list. Top-level keys: {keys}. This is how a "
                       f"200-with-an-upgrade-gate reads as success. "
                       f"Body: {body[:180]!r}"),
        }

    if n == 0 and in_coverage:
        return {
            "issue": "map_layer_empty_in_coverage",
            "url": tag,
            "count": 1,
            "detail": (
                f"Map layer '{p.layers}' ({path}) returned HTTP {status} with "
                f"ZERO rows at the {where.upper()} canary, which is INSIDE its "
                f"declared '{p.coverage}' coverage. A 200 is not a working "
                f"layer: the user sees an empty map under a badge that still "
                f"advertises data."),
        }

    return None


def run_probe() -> dict:
    """Run every probe at both canaries. Pure — returns findings, writes nothing."""
    findings: list[dict] = []
    coverage: list[dict] = []

    for p in _PROBES:
        for where, (lat, lng) in (("us", US), ("non_us", NON_US)):
            f = _check_one(p, lat, lng, where)
            if f:
                findings.append(f)
            coverage.append({
                "layer": p.layers, "key": p.key,
                "canary": where, "declared_coverage": p.coverage,
                "healthy": f is None,
                "issue": f["issue"] if f else None,
            })

    return {
        "findings": findings,
        "coverage": coverage,
        "probes": len(_PROBES),
        "checks": len(_PROBES) * 2,
    }


def _persist(findings: list[dict]) -> dict:
    """Write findings to brain_findings. Returning them from a handler is NOT
    persistence — that is exactly why the predecessor detector left zero rows
    in 15 months."""
    if not findings:
        return {"persisted": 0, "note": "no findings to write"}
    try:
        import psycopg2
        from routes.brain_findings_writer import upsert_brain_finding
    except Exception as e:
        return {"persisted": 0, "error": f"import failed: {type(e).__name__}: {e}"}

    dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""
    if not dsn:
        return {"persisted": 0, "error": "no DATABASE_URL"}

    conn = None
    try:
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()
        outcomes = {}
        for f in findings:
            r = upsert_brain_finding(
                cur, issue=f["issue"], url=f["url"], count=f.get("count", 1),
                detail=f.get("detail", ""), detector="map_layer_probe")
            outcomes[r] = outcomes.get(r, 0) + 1
        conn.commit()
        return {"persisted": len(findings), "outcomes": outcomes}
    except Exception as e:
        return {"persisted": 0, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@map_layer_probe_bp.route("/api/v1/jobs/map-layer-probe", methods=["GET", "POST"])
def map_layer_probe():
    """Probe every Land & Power map layer at both canaries; persist findings.

    ★ The kill-switch below deliberately does NOT return ok=True.
    cron_heartbeat._classify records "skipped" only when `ok is not True`
    (routes/cron_heartbeat.py) — that guard exists because
    {"ok":True,"skipped":...} wrote ~450 false-success rows a day. Returning
    ok=True here would make a DISABLED probe report success forever: the
    disarmed-verifier failure this whole module exists to prevent.
    """
    if os.environ.get("MAP_LAYER_PROBE_DISABLE") == "1":
        return jsonify(skipped="MAP_LAYER_PROBE_DISABLE=1"), 200

    result = run_probe()
    dry = (request.args.get("dry") == "1")
    result["persist"] = ({"persisted": 0, "note": "dry run"} if dry
                         else _persist(result["findings"]))

    n = len(result["findings"])
    if n:
        # Non-empty `error` so the heartbeat's failure row carries a real detail:
        # _classify builds detail from error/skipped/reason, and an empty string
        # produces a blank row nobody can action.
        result["ok"] = False
        result["error"] = (
            f"{n} map-layer finding(s): "
            + "; ".join(f"{f['issue']}@{f['url']}" for f in result["findings"][:6])
        )[:300]
    else:
        result["ok"] = True
    return jsonify(result), 200


def register_map_layer_probe(app):
    app.register_blueprint(map_layer_probe_bp)
    return True
