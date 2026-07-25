#!/usr/bin/env python3
"""gemini_contract_eval.py — the DC Hub × Gemini partnership contract
regression harness (eval-readiness proof).

WHY THIS EXISTS
    Gemini told us it will "hit the live envelope during the monthly
    evaluation cycle." We cannot run Gemini's evaluator, but we CAN run the
    exact contract assertions Gemini's routing state machine depends on,
    against LIVE production (https://dchub.cloud), and prove we would PASS.
    This script exercises every surface the partnership co-designed, asserts
    the contract properties byte-for-byte, and prints a GREEN/RED report.

WHAT IT CHECKS (each row is one PASS / FAIL / ERROR with the observed value)
    A. PROVENANCE v1        — /api/v1/facilities collection block + per-record v
    B. DARK FIBER           — inferred dark-availability (metro + point screen)
    C. CLUSTER TOOL #73     — physics-bounded latency clustering over MCP
    D. ERROR error_version:1 — normative grounding + STRICT-SUBSET binding
    E. ERROR REGISTRY       — the published 10-code taxonomy + drift guard
    F. RUNTIME as_of        — envelopes are stamped with the live clock, not
                              a frozen literal

SAFETY
    Read-only. Every request is a GET or a JSON-RPC read (initialize +
    tools/call on tools that only fetch). It performs NO writes, mutates NO
    data, mints NO keys, and creates NO synthetic records. It is safe to run
    anytime — before a deploy, after a deploy, or on a schedule — as a
    contract regression gate. It uses a benign, self-identifying client name
    on the MCP handshake and issues a minimal number of calls.

USAGE
    python3 scripts/gemini_contract_eval.py

    Exit 0 = every check GREEN (eval-ready). Exit 1 = at least one RED/ERROR.

    Dependencies: requests (stdlib otherwise). No Flask / DB / repo imports —
    this is a black-box client that only knows the public contract, exactly
    like Gemini's evaluator.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time

try:
    import requests
except Exception as _e:  # pragma: no cover
    print("FATAL: this harness requires the `requests` package "
          "(pip install requests).", file=sys.stderr)
    raise

# ── configuration ────────────────────────────────────────────────────────
BASE = "https://dchub.cloud"
MCP_URL = BASE + "/mcp"
MCP_HEALTH_URL = ("https://dchub-mcp-server-production-4d2e.up.railway.app"
                  "/health")

# A key so the connectivity-score endpoint returns the FULL read (the
# dark_screen block is keyed-or-internal only). Read-only usage.
# 2026-07-25 security sweep: this was HARDCODED (and split across two string
# literals, which defeats naive secret scanners) in a PUBLIC repo. Now
# env-only — the eval skips the keyed read rather than ship a credential.
CONNECTIVITY_KEY = (os.environ.get("DCHUB_API_KEY")
                    or os.environ.get("DCHUB_SMOKE_ENTERPRISE_KEY") or "")

HTTP_TIMEOUT = 45  # seconds; the live envelope can cold-start a worker

# The tools' DECLARED parameter names — the strict-subset allow-list any
# error_version:1 suggested_params must validate against. Embedded here (not
# imported) so the harness stays a black-box client of the public contract.
GET_GRID_DATA_PARAMS = {"iso", "metric", "detail"}
RANK_MARKETS_PARAMS = {"criteria", "region", "limit", "min_capacity_mw"}

# The published taxonomy (check E). The registry must carry EXACTLY these.
EXPECTED_ERROR_CODES = {
    "invalid_iso", "invalid_sites_param", "invalid_criteria",
    "max_latency_us_below_physics_floor", "database_unavailable",
    "rate_limit_exceeded", "tool_execution_failed", "webmcp_call_failed",
    "envelope_error", "unknown_error",
}
VALID_SEVERITIES = {"parameter_adjustment", "transient_backoff", "fatal"}

DARK_HONESTY_PHRASE = "NOT confirmed strand availability"


# ── result recording ─────────────────────────────────────────────────────
PASS, FAIL, ERROR = "PASS", "FAIL", "ERROR"
_results = []            # list of dict(id, desc, status, observed)
# every error_code observed in checks A-D, fed to the E drift guard.
_observed_error_codes = set()
# runtime as_of values captured off error envelopes, fed to check F.
_observed_as_of = []


def record(cid, desc, status, observed):
    _results.append({"id": cid, "desc": desc, "status": status,
                     "observed": str(observed)})


def check(cid, desc, ok, observed):
    """Record a boolean assertion as PASS/FAIL with its observed value."""
    record(cid, desc, PASS if ok else FAIL, observed)


def group_error(rows, exc):
    """Mark a whole check group ERROR (a network hiccup on one group must
    never abort the rest of the harness)."""
    msg = f"{type(exc).__name__}: {str(exc)[:160]}"
    for cid, desc in rows:
        record(cid, desc, ERROR, msg)


# ── HTTP helpers ─────────────────────────────────────────────────────────
def get_json(path, headers=None):
    """GET a JSON body from the live envelope. Returns (status_code, obj).
    Raises on network failure so the caller's group is marked ERROR."""
    url = path if path.startswith("http") else BASE + path
    r = requests.get(url, headers=headers or {}, timeout=HTTP_TIMEOUT)
    try:
        obj = r.json()
    except Exception:
        obj = None
    return r.status_code, obj


class MCPSession:
    """Minimal MCP Streamable-HTTP client: does the initialize → session-id →
    initialized → tools/call dance and parses the SSE `data:` frame. Only the
    read path the contract needs; never raises on a well-formed server."""

    def __init__(self, url):
        self.url = url
        self.session_id = None
        self._id = 0
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

    def _next_id(self):
        self._id += 1
        return self._id

    @staticmethod
    def _parse(resp):
        """Parse a JSON-RPC message out of either a plain-JSON or an
        SSE (text/event-stream) response. Returns the message that carries a
        `result`/`error`, else the first parseable frame."""
        ct = resp.headers.get("content-type", "")
        if "text/event-stream" in ct:
            frames = []
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    try:
                        frames.append(json.loads(line[5:].strip()))
                    except Exception:
                        pass
            for f in frames:
                if isinstance(f, dict) and ("result" in f or "error" in f):
                    return f
            return frames[-1] if frames else None
        try:
            return resp.json()
        except Exception:
            return None

    def initialize(self):
        body = {
            "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "dchub-gemini-contract-eval",
                               "version": "1.0"},
            },
        }
        r = requests.post(self.url, headers=self._headers, json=body,
                          timeout=HTTP_TIMEOUT)
        self.session_id = r.headers.get("mcp-session-id")
        # best-effort initialized notification (some servers require it)
        h = dict(self._headers)
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        try:
            requests.post(self.url, headers=h,
                          json={"jsonrpc": "2.0",
                                "method": "notifications/initialized"},
                          timeout=HTTP_TIMEOUT)
        except Exception:
            pass
        return self

    def call_tool(self, name, arguments):
        """tools/call → the tool result's structuredContent dict (or {})."""
        h = dict(self._headers)
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        body = {
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        r = requests.post(self.url, headers=h, json=body, timeout=HTTP_TIMEOUT)
        msg = self._parse(r)
        result = (msg or {}).get("result") or {}
        sc = result.get("structuredContent")
        return sc if isinstance(sc, dict) else {}


def _today_window():
    """The set of date strings that legitimately count as 'today' for a
    runtime-stamped as_of — today in UTC and local, padded ±1 day so a
    harness run straddling midnight never false-REDs. A frozen/stale literal
    (e.g. a hardcoded 2026-07-11 lock date weeks in the past) still fails."""
    out = set()
    for base in (datetime.datetime.now(datetime.timezone.utc),
                 datetime.datetime.now()):
        d = base.date()
        for delta in (-1, 0, 1):
            out.add((d + datetime.timedelta(days=delta)).isoformat())
    return out


# ═══════════════════════════════════════════════════════════════════════
# A. PROVENANCE v1 — the citation-confidence envelope
# ═══════════════════════════════════════════════════════════════════════
def check_a_provenance():
    rows = [
        ("A1", "facilities provenance.provenance_version == 1"),
        ("A2", "facilities provenance.default_v present"),
        ("A3", "facilities provenance.fallback_url present"),
        ("A4", "facilities provenance.cite_url_template present"),
        ("A5", "facilities provenance.license == CC-BY-4.0"),
        ("A6", "facilities verification_counts has int verified+tracked"),
        ("A7", "every facility record carries v in {verified,tracked}"),
    ]
    try:
        status, d = get_json("/api/v1/facilities?limit=2")
        prov = (d or {}).get("provenance") or {}
        records = (d or {}).get("data") or []

        check("A1", rows[0][1], prov.get("provenance_version") == 1,
              f"provenance_version={prov.get('provenance_version')!r}")
        check("A2", rows[1][1], prov.get("default_v") is not None,
              f"default_v={prov.get('default_v')!r}")
        check("A3", rows[2][1], bool(prov.get("fallback_url")),
              f"fallback_url={prov.get('fallback_url')!r}")
        check("A4", rows[3][1], bool(prov.get("cite_url_template")),
              f"cite_url_template={prov.get('cite_url_template')!r}")
        check("A5", rows[4][1], prov.get("license") == "CC-BY-4.0",
              f"license={prov.get('license')!r}")

        vc = prov.get("verification_counts") or {}
        vc_ok = (isinstance(vc.get("verified"), int)
                 and isinstance(vc.get("tracked"), int))
        check("A6", rows[5][1], vc_ok,
              f"verification_counts={vc}")

        vs = [r.get("v") for r in records]
        v_ok = bool(records) and all(v in ("verified", "tracked") for v in vs)
        check("A7", rows[6][1], v_ok,
              f"n={len(records)} v_values={sorted(set(map(str, vs)))}")
    except Exception as e:
        group_error(rows, e)


# ═══════════════════════════════════════════════════════════════════════
# B. DARK FIBER — inferred availability, honesty-railed
# ═══════════════════════════════════════════════════════════════════════
def check_b_dark_fiber():
    metro_rows = [
        ("B1", "fiber/metro/northern-virginia provenance_version == 1"),
        ("B2", "fiber/metro dark_availability_zones has level + v==inferred"),
    ]
    try:
        _, d = get_json("/api/v1/fiber/metro/northern-virginia")
        pv = ((d or {}).get("provenance") or {}).get("provenance_version")
        check("B1", metro_rows[0][1], pv == 1, f"provenance_version={pv!r}")
        dz = (d or {}).get("dark_availability_zones") or {}
        b2_ok = dz.get("level") is not None and dz.get("v") == "inferred"
        check("B2", metro_rows[1][1], b2_ok,
              f"level={dz.get('level')!r} v={dz.get('v')!r}")
    except Exception as e:
        group_error(metro_rows, e)

    point_rows = [
        ("B3", "connectivity/score dark_screen.level present"),
        ("B4", "connectivity/score dark_screen.v == inferred"),
        ("B5", f"connectivity/score method says '{DARK_HONESTY_PHRASE}'"),
    ]
    try:
        _, d = get_json(
            "/api/infrastructure/connectivity/score?lat=39.02&lon=-77.46",
            headers={"X-API-Key": CONNECTIVITY_KEY})
        ds = (d or {}).get("dark_screen") or {}
        check("B3", point_rows[0][1], ds.get("level") is not None,
              f"level={ds.get('level')!r}")
        check("B4", point_rows[1][1], ds.get("v") == "inferred",
              f"v={ds.get('v')!r}")
        method = str(ds.get("method") or "")
        check("B5", point_rows[2][1], DARK_HONESTY_PHRASE in method,
              f"method={'…' + DARK_HONESTY_PHRASE + '…' if DARK_HONESTY_PHRASE in method else method[:80]!r}")
    except Exception as e:
        group_error(point_rows, e)


# ═══════════════════════════════════════════════════════════════════════
# C. CLUSTER TOOL #73 — physics-bounded latency clustering (MCP)
# ═══════════════════════════════════════════════════════════════════════
def check_c_cluster(mcp):
    rows = [
        ("C1", "cluster_sites_by_latency: LA pair physics_impossible "
               "& est_rtt_us > 1200"),
        ("C2", "all-impossible → _error_mitigation "
               "max_latency_us_below_physics_floor, suggested budget > "
               "requested"),
    ]
    try:
        if mcp is None:
            raise RuntimeError("MCP session unavailable (initialize failed)")
        sc = mcp.call_tool("cluster_sites_by_latency",
                           {"sites": "39.02,-77.46;34.05,-118.24",
                            "max_latency_us": 1200})
        pairs = sc.get("pairs") or []
        # the single NoVA↔LA pair must be pruned by physics
        c1_ok = bool(pairs) and all(
            p.get("physics_impossible") is True
            and isinstance(p.get("est_rtt_us"), (int, float))
            and p.get("est_rtt_us") > 1200
            for p in pairs)
        est = [p.get("est_rtt_us") for p in pairs]
        imp = [p.get("physics_impossible") for p in pairs]
        check("C1", rows[0][1], c1_ok,
              f"physics_impossible={imp} est_rtt_us={est}")

        mit = sc.get("_error_mitigation") or {}
        code = mit.get("error_code")
        sugg = (mit.get("suggested_params") or {}).get("max_latency_us")
        if code:
            _observed_error_codes.add(code)
        c2_ok = (code == "max_latency_us_below_physics_floor"
                 and isinstance(sugg, (int, float)) and sugg > 1200)
        check("C2", rows[1][1], c2_ok,
              f"error_code={code!r} suggested_max_latency_us={sugg!r} "
              f"(requested 1200)")
    except Exception as e:
        group_error(rows, e)

    health_rows = [("C3", "mcp-server /health tools==73 & version 2.5.0")]
    try:
        _, d = get_json(MCP_HEALTH_URL)
        d = d or {}
        c3_ok = d.get("tools") == 73 and d.get("version") == "2.5.0"
        check("C3", health_rows[0][1], c3_ok,
              f"tools={d.get('tools')!r} version={d.get('version')!r}")
    except Exception as e:
        group_error(health_rows, e)


# ═══════════════════════════════════════════════════════════════════════
# D. ERROR CONTRACT error_version:1 — grounding + STRICT-SUBSET binding
# ═══════════════════════════════════════════════════════════════════════
def check_d_error_contract(mcp):
    # D1 — MCP get_grid_data with an unknown ISO
    d1_rows = [("D1", "MCP get_grid_data iso=PJMM → error_version:1, "
                      "invalid_iso, suggested_params=={'iso':'PJM'} (strict)")]
    try:
        if mcp is None:
            raise RuntimeError("MCP session unavailable (initialize failed)")
        sc = mcp.call_tool("get_grid_data", {"iso": "PJMM"})
        mit = sc.get("_error_mitigation") or {}
        code = mit.get("error_code")
        sugg = mit.get("suggested_params") or {}
        if code:
            _observed_error_codes.add(code)
        ao = (sc.get("provenance") or {}).get("as_of")
        if ao:
            _observed_as_of.append(("MCP get_grid_data", ao))
        strict = (set(sugg.keys()) == {"iso"}
                  and "iso" in GET_GRID_DATA_PARAMS
                  and sugg.get("iso") == "PJM")
        d1_ok = (sc.get("error_version") == 1
                 and mit.get("severity") == "parameter_adjustment"
                 and code == "invalid_iso" and strict)
        check("D1", d1_rows[0][1], d1_ok,
              f"error_version={sc.get('error_version')!r} "
              f"severity={mit.get('severity')!r} error_code={code!r} "
              f"suggested_params={sugg!r}")
    except Exception as e:
        group_error(d1_rows, e)

    # D2 — REST rank_markets with an unknown criteria enum
    d2_rows = [("D2", "REST rank_markets criteria=cheap → error_version:1, "
                      "invalid_criteria, suggested enum valid & in-contract")]
    try:
        _, d = get_json("/api/v1/mcp/tools/rank_markets?criteria=cheap&limit=1")
        d = d or {}
        mit = d.get("_error_mitigation") or {}
        code = mit.get("error_code")
        sugg = mit.get("suggested_params") or {}
        valid_options = d.get("valid_options") or []
        if code:
            _observed_error_codes.add(code)
        ao = (d.get("provenance") or {}).get("as_of")
        if ao:
            _observed_as_of.append(("REST rank_markets", ao))
        enum_ok = sugg.get("criteria") in valid_options
        subset_ok = set(sugg.keys()).issubset(RANK_MARKETS_PARAMS)
        d2_ok = (d.get("error_version") == 1 and code == "invalid_criteria"
                 and enum_ok and subset_ok)
        check("D2", d2_rows[0][1], d2_ok,
              f"error_version={d.get('error_version')!r} error_code={code!r} "
              f"suggested_params={sugg!r} subset_of_params={subset_ok}")
    except Exception as e:
        group_error(d2_rows, e)

    # D3 — REST cluster-latency with an unparseable sites value
    d3_rows = [("D3", "REST cluster-latency sites=garbage → error_version:1, "
                      "invalid_sites_param, severity parameter_adjustment")]
    try:
        _, d = get_json("/api/v1/fiber/cluster-latency?sites=garbage")
        d = d or {}
        mit = d.get("_error_mitigation") or {}
        code = mit.get("error_code")
        if code:
            _observed_error_codes.add(code)
        ao = (d.get("provenance") or {}).get("as_of")
        if ao:
            _observed_as_of.append(("REST cluster-latency", ao))
        d3_ok = (d.get("error_version") == 1
                 and code == "invalid_sites_param"
                 and mit.get("severity") == "parameter_adjustment")
        check("D3", d3_rows[0][1], d3_ok,
              f"error_version={d.get('error_version')!r} error_code={code!r} "
              f"severity={mit.get('severity')!r}")
    except Exception as e:
        group_error(d3_rows, e)


# ═══════════════════════════════════════════════════════════════════════
# E. ERROR REGISTRY — the published taxonomy + drift guard
# ═══════════════════════════════════════════════════════════════════════
def check_e_registry():
    rows = [
        ("E1", "errors/registry registry_version == 1"),
        ("E2", "registry carries EXACTLY the 10 published error_codes"),
        ("E3", "every registry code has a severity in the 3-value set"),
        ("E4", "DRIFT GUARD: every error_code seen in A-D is registered"),
    ]
    try:
        _, d = get_json("/api/v1/errors/registry")
        d = d or {}
        codes = d.get("codes") or {}
        code_set = set(codes.keys())

        check("E1", rows[0][1], d.get("registry_version") == 1,
              f"registry_version={d.get('registry_version')!r}")

        e2_ok = code_set == EXPECTED_ERROR_CODES
        missing = EXPECTED_ERROR_CODES - code_set
        extra = code_set - EXPECTED_ERROR_CODES
        check("E2", rows[1][1], e2_ok,
              f"count={len(code_set)} missing={sorted(missing)} "
              f"extra={sorted(extra)}")

        bad_sev = {k: (v or {}).get("severity") for k, v in codes.items()
                   if (v or {}).get("severity") not in VALID_SEVERITIES}
        check("E3", rows[2][1], not bad_sev,
              f"offending={bad_sev}" if bad_sev
              else f"all {len(codes)} severities valid")

        # the normative-grounding guarantee — anything an agent actually
        # received in A-D must be a code it can bind its state machine to.
        undrifted = _observed_error_codes - code_set
        check("E4", rows[3][1],
              bool(_observed_error_codes) and not undrifted,
              f"observed={sorted(_observed_error_codes)} "
              f"unregistered={sorted(undrifted)}")
    except Exception as e:
        group_error(rows, e)


# ═══════════════════════════════════════════════════════════════════════
# F. RUNTIME as_of — envelopes stamped with the live clock, not a literal
# ═══════════════════════════════════════════════════════════════════════
def check_f_runtime_as_of():
    rows = [
        ("F1", "error-envelope as_of == today's runtime date (not frozen)"),
        ("F2", "errors/registry generated_at date == today (not frozen)"),
    ]
    today = _today_window()
    try:
        # F1: every error envelope observed in D must carry today's date.
        if not _observed_as_of:
            record("F1", rows[0][1], ERROR,
                   "no error-envelope as_of captured (checks D did not run)")
        else:
            stale = [(src, val) for src, val in _observed_as_of
                     if val not in today]
            check("F1", rows[0][1], not stale,
                  f"observed={_observed_as_of} today~{sorted(today)[len(today)//2]} "
                  f"stale={stale}")
    except Exception as e:
        group_error([rows[0]], e)

    try:
        # F2: the registry stamps generated_at at request time.
        _, d = get_json("/api/v1/errors/registry")
        gen = str((d or {}).get("generated_at") or "")
        gen_date = gen[:10]
        check("F2", rows[1][1], gen_date in today,
              f"generated_at={gen!r} (date {gen_date})")
    except Exception as e:
        group_error([rows[1]], e)


# ── runner + report ──────────────────────────────────────────────────────
def _print_report(elapsed):
    width_id = 4
    width_status = 6
    # column for observed wraps naturally in a terminal; keep desc readable.
    print()
    print("=" * 100)
    print("  DC HUB × GEMINI — CONTRACT EVAL-READINESS HARNESS")
    print(f"  target: {BASE}   run: "
          f"{datetime.datetime.now(datetime.timezone.utc).isoformat()}")
    print("=" * 100)
    print(f"  {'ID':<{width_id}}{'STATUS':<{width_status}}CHECK")
    print("-" * 100)
    for r in _results:
        mark = {PASS: "PASS", FAIL: "FAIL", ERROR: "ERR "}[r["status"]]
        print(f"  {r['id']:<{width_id}}{mark:<{width_status}}{r['desc']}")
        print(f"  {'':<{width_id}}{'':<{width_status}}└─ observed: {r['observed']}")
    print("-" * 100)

    npass = sum(1 for r in _results if r["status"] == PASS)
    nfail = sum(1 for r in _results if r["status"] == FAIL)
    nerr = sum(1 for r in _results if r["status"] == ERROR)
    total = len(_results)
    verdict = "GREEN — EVAL-READY" if (nfail == 0 and nerr == 0) else "RED"
    print(f"  RESULT: {npass}/{total} PASS   {nfail} FAIL   {nerr} ERROR   "
          f"({elapsed:.1f}s)")
    print(f"  VERDICT: {verdict}")
    if nfail or nerr:
        print()
        print("  NOT GREEN — the following rows need attention:")
        for r in _results:
            if r["status"] in (FAIL, ERROR):
                print(f"    [{r['status']}] {r['id']} {r['desc']}")
                print(f"           observed: {r['observed']}")
    print("=" * 100)
    print()


def main():
    t0 = time.time()
    # One MCP session shared by the C and D MCP checks; if the handshake
    # fails, those checks degrade to ERROR (the harness still runs the rest).
    mcp = None
    try:
        mcp = MCPSession(MCP_URL).initialize()
    except Exception as e:
        print(f"WARNING: MCP initialize failed ({type(e).__name__}: "
              f"{str(e)[:120]}); MCP-dependent checks will report ERROR.",
              file=sys.stderr)

    check_a_provenance()
    check_b_dark_fiber()
    check_c_cluster(mcp)
    check_d_error_contract(mcp)
    check_e_registry()       # after A-D so the drift guard sees observed codes
    check_f_runtime_as_of()  # after D so it sees the captured as_of values

    _print_report(time.time() - t0)

    all_green = all(r["status"] == PASS for r in _results)
    return 0 if all_green else 1


if __name__ == "__main__":
    sys.exit(main())
