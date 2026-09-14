"""Site-score REFERENCE DISTRIBUTION — the "global percentile" layer both Gemini
and Grok converged on as the next boundary after rank_sites absolute mode.

rank_sites relative-mode compares within a batch; absolute-mode is a fixed 0-100
scale. Neither answers "is 78 GOOD relative to the whole viable population?".
This module maintains a percentile distribution of analyze_site (/api/site-score)
metrics across a sample of viable geocoded queue survivors, so rank_sites can
score a site as "better than X% of viable 1GW+ sites" — stable across runs and
comparable across regions.

Cost-bounded: the tick samples a capped set and calls /api/site-score at the
Railway ORIGIN (X-Internal-Key), not the CF edge (avoids the self-traffic loop).
"""
import os
import json
import urllib.request

import psycopg2
import psycopg2.extras

# flat metric name -> (path into the /api/site-score JSON, higher_is_better)
_METRICS = {
    "overall_score":        (("overall_score",), True),
    "risk_resilience":      (("scores", "risk_resilience"), True),
    "fiber_connectivity":   (("scores", "fiber_connectivity"), True),
    "power_infrastructure": (("scores", "power_infrastructure"), True),
    "market_conditions":    (("scores", "market_conditions"), True),
    "gas_pipeline_access":  (("scores", "gas_pipeline_access"), True),
    "fiber_km":             (("fiber", "nearest_carrier_km"), False),
    "power_cost":           (("power_cost", "industrial_cents_kwh"), False),
}

_ORIGIN = (os.environ.get("RAILWAY_BACKEND_URL")
           or os.environ.get("DCHUB_API_BASE")
           or "https://dchub-backend-production.up.railway.app")


def _dsn():
    return os.environ.get("DATABASE_URL", "")


def _conn():
    # direct connect (never a GC-closed contextmanager handle — see the
    # contextmanager-conn GC trap); caller closes.
    return psycopg2.connect(_dsn())


def ensure_baseline_table():
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS site_score_baseline (
                metric text PRIMARY KEY,
                p10 numeric, p25 numeric, p50 numeric, p75 numeric, p90 numeric,
                min_v numeric, max_v numeric,
                sample_size integer,
                higher_is_better boolean DEFAULT true,
                computed_at timestamptz DEFAULT now()
            )
        """)
        c.commit()
    finally:
        c.close()


def _dig(obj, path):
    cur = obj
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    if isinstance(cur, bool) or not isinstance(cur, (int, float)):
        return None
    return float(cur)


def _site_score(lat, lon, state, capacity, internal_key):
    url = (f"{_ORIGIN}/api/site-score?lat={lat}&lon={lon}"
           f"&state={state or ''}&capacity={int(capacity or 0)}")
    req = urllib.request.Request(url, headers={
        "X-Internal-Key": internal_key,
        # dchub- UA => backend server-to-server bypass returns ungated data
        "User-Agent": "dchub-mcp-server/1.0",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def _pct_breaks(vals):
    """p10/25/50/75/90 + min/max of a numeric list (nearest-rank)."""
    s = sorted(vals)
    n = len(s)
    def q(p):
        if n == 1:
            return s[0]
        i = min(n - 1, max(0, int(round(p * (n - 1)))))
        return s[i]
    return {
        "p10": round(q(0.10), 3), "p25": round(q(0.25), 3), "p50": round(q(0.50), 3),
        "p75": round(q(0.75), 3), "p90": round(q(0.90), 3),
        "min_v": round(s[0], 3), "max_v": round(s[-1], 3), "sample_size": n,
    }


def run_site_baseline_tick(sample_n=40):
    """Sample viable geocoded survivors, score each via /api/site-score, and
    recompute the per-metric percentile breakpoints. Returns a summary dict."""
    internal_key = os.environ.get("DCHUB_INTERNAL_KEY", "")
    summary = {"scored": 0, "failed": 0, "metrics_updated": 0, "errors": []}
    if not internal_key or not _dsn():
        summary["errors"].append("missing DCHUB_INTERNAL_KEY or DATABASE_URL")
        return summary
    ensure_baseline_table()

    # sample viable survivors (geocoded, >=500MW) spread by random
    try:
        c = _conn()
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT state, lat, lng, capacity_mw FROM interconnect_queue
            WHERE lat IS NOT NULL AND lng IS NOT NULL AND capacity_mw >= 500
            ORDER BY random() LIMIT %s
        """, (int(sample_n),))
        rows = cur.fetchall()
        c.close()
    except Exception as e:
        summary["errors"].append(f"sample:{str(e)[:100]}")
        return summary

    collected = {m: [] for m in _METRICS}
    for row in rows:
        try:
            d = _site_score(row["lat"], row["lng"], row.get("state"),
                            row.get("capacity_mw"), internal_key)
            if not d or not d.get("success", True):
                summary["failed"] += 1
                continue
            got = False
            for m, (path, _hib) in _METRICS.items():
                v = _dig(d, path)
                if v is not None:
                    collected[m].append(v)
                    got = True
            summary["scored"] += 1 if got else 0
        except Exception as e:
            summary["failed"] += 1
            if len(summary["errors"]) < 3:
                summary["errors"].append(str(e)[:80])

    # upsert breakpoints per metric
    try:
        c = _conn()
        cur = c.cursor()
        for m, vals in collected.items():
            if len(vals) < 5:
                continue
            b = _pct_breaks(vals)
            hib = _METRICS[m][1]
            cur.execute("""
                INSERT INTO site_score_baseline
                    (metric, p10, p25, p50, p75, p90, min_v, max_v, sample_size, higher_is_better, computed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now() ON CONFLICT DO NOTHING)
                ON CONFLICT (metric) DO UPDATE SET
                    p10=EXCLUDED.p10, p25=EXCLUDED.p25, p50=EXCLUDED.p50,
                    p75=EXCLUDED.p75, p90=EXCLUDED.p90, min_v=EXCLUDED.min_v,
                    max_v=EXCLUDED.max_v, sample_size=EXCLUDED.sample_size,
                    higher_is_better=EXCLUDED.higher_is_better, computed_at=now()
            """, (m, b["p10"], b["p25"], b["p50"], b["p75"], b["p90"],
                  b["min_v"], b["max_v"], b["sample_size"], hib))
            summary["metrics_updated"] += 1
        c.commit()
        c.close()
    except Exception as e:
        summary["errors"].append(f"upsert:{str(e)[:100]}")

    # After the reference distribution refreshes, evaluate drift alerts on saved
    # shortlists and notify on breach (the daily "wake me when it matters" loop).
    try:
        from routes.shortlists import evaluate_shortlist_alerts
        summary["alerts"] = evaluate_shortlist_alerts()
    except Exception as e:
        summary["errors"].append(f"alerts:{str(e)[:80]}")
    return summary


_BASELINE_CACHE = {"rows": None, "at": None}


def load_baseline(force=False):
    """Return {metric: {p10..p90, min_v, max_v, higher_is_better}} from the table."""
    if _BASELINE_CACHE["rows"] is not None and not force:
        return _BASELINE_CACHE["rows"]
    out = {}
    try:
        c = _conn()
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM site_score_baseline")
        for r in cur.fetchall():
            out[r["metric"]] = {k: (float(r[k]) if r[k] is not None else None)
                                for k in ("p10", "p25", "p50", "p75", "p90", "min_v", "max_v")}
            out[r["metric"]]["higher_is_better"] = bool(r["higher_is_better"])
        c.close()
    except Exception:
        pass
    _BASELINE_CACHE["rows"] = out
    return out


def baseline_meta():
    """Freshness metadata for the current baseline: {computed_at, age_hours,
    metrics, min_sample_size}. Lets rank_sites tell an agent how fresh the
    reference distribution is (reproducibility awareness — Phase 4)."""
    meta = {"computed_at": None, "age_hours": None, "metrics": 0, "min_sample_size": None}
    try:
        c = _conn()
        cur = c.cursor()
        cur.execute("SELECT max(computed_at), count(*), min(sample_size), "
                    "EXTRACT(EPOCH FROM (now() - max(computed_at)))/3600.0 "
                    "FROM site_score_baseline")
        row = cur.fetchone()
        c.close()
        if row and row[0] is not None:
            meta["computed_at"] = row[0].isoformat()
            meta["metrics"] = int(row[1] or 0)
            meta["min_sample_size"] = int(row[2]) if row[2] is not None else None
            meta["age_hours"] = round(float(row[3]), 1) if row[3] is not None else None
    except Exception:
        pass
    return meta


def score_site(metrics, objectives, baseline=None):
    """Weighted-percentile objective_score for ONE site's metric dict, using the
    population baseline — mirrors rank_sites percentile mode for a single site so
    saved shortlist entries can be re-scored against the current distribution
    (Phase 5). Returns (objective_score, per_field_normalized_dict)."""
    bl = baseline if baseline is not None else load_baseline()
    if not objectives:
        return None, {}
    wsum = 0.0
    total = 0.0
    per = {}
    for f, w in objectives.items():
        try:
            w = float(w)
        except Exception:
            continue
        wsum += abs(w)
        pct = percentile_of(f, metrics.get(f), bl)
        if pct is None:
            v = metrics.get(f)
            try:
                pct = max(0.0, min(100.0, float(v)))
            except Exception:
                continue
        directed = pct if w >= 0 else (100.0 - pct)
        per[f] = round(directed, 1)
        total += abs(w) * directed
    if wsum <= 0:
        return None, per
    return round(total / wsum, 1), per


def percentile_of(metric, value, baseline=None):
    """Map a raw metric value to its 0-100 percentile in the population (0 = at/below
    the lowest sampled value, 100 = at/above the highest). Direction (maximize vs
    minimize) is NOT applied here — the caller applies it via the objective's signed
    weight, so all rank_sites modes handle direction identically. Piecewise-linear
    over the stored breakpoints; None if no baseline for this metric."""
    b = (baseline or load_baseline()).get(metric)
    if b is None or value is None:
        return None
    pts = [(b["min_v"], 0.0), (b["p10"], 10.0), (b["p25"], 25.0), (b["p50"], 50.0),
           (b["p75"], 75.0), (b["p90"], 90.0), (b["max_v"], 100.0)]
    pts = [p for p in pts if p[0] is not None]
    pts.sort(key=lambda x: x[0])
    v = float(value)
    if v <= pts[0][0]:
        return round(pts[0][1], 1)
    if v >= pts[-1][0]:
        return round(pts[-1][1], 1)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= v <= x1:
            span = (x1 - x0) or 1.0
            return round(y0 + (v - x0) / span * (y1 - y0), 1)
    return None
