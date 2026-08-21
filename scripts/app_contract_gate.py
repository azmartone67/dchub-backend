#!/usr/bin/env python3
"""BEHAVIOR GATE — boot the real app and check what it actually serves.

Why this exists
---------------
This suite has ~8,800 tests and they are almost all static: 290 test files read
source as TEXT and assert strings are present (1,021 such assertions), 240
AST-extract functions and run them against stubs, and only 64 exercise the real
application. CI was 351/354 green over the week of 2026-08-13 while 104 "fix"
PRs landed. Green CI plus a hundred fixes a week is the signature of gates that
cannot see what breaks.

The stated reason nothing tested behavior was that main.py cannot be imported —
"it opens DB pools, starts keepalive threads and registers ~200 blueprints."
That is true of a real DB. With psycopg2 stubbed it boots in ~2 seconds and
yields a complete URL map: 3,393 rules across 741 blueprints. The premise that
forced the whole suite into grep-testing was ~40 lines of stub away from false.

What it asserts
---------------
1. THE APP BOOTS. On its own this is a gate the suite never had: an import
   error, a bad decorator or a broken blueprint anywhere in ~200 modules fails
   here, where 8,800 string assertions would all still pass.

2. ROUTE + BLUEPRINT FLOORS. Registration failures in this codebase are
   swallowed and logged, not raised — a blueprint that dies during import just
   silently stops serving. A floor turns "the route count collapsed" into a red
   build instead of a 404 a user finds first. Same principle as
   tests/_scan_floors.py: a gate must prove it observed something.

3. NO NEW SHADOWED ROUTES. When two handlers register the same rule+method,
   Flask serves whichever matched first and the other is dead code that still
   looks live in source. 18 exist today. This is a RATCHET: the count may fall,
   never rise. Fixing all 18 at once would be its own risky patch wave; making
   them un-addable stops the bleeding now and lets them be retired in ones.

4. THE CONTRACT ROUTES ACTUALLY SERVE. Each pinned path is fetched through the
   test client and must not 404 or 5xx. "Registered" is not "routable" and
   "routable" is not "delivers" — this checks the last one.

Run:  python3 scripts/app_contract_gate.py [--update-baseline]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(ROOT, "tests", "app_contract.json")


# ---------------------------------------------------------------- DB stubbing
class _Cur:
    description = None
    rowcount = 0

    def execute(self, *a, **k):
        return None

    def executemany(self, *a, **k):
        return None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, *a):
        return []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter([])


class _Conn:
    closed = 0
    autocommit = True

    def cursor(self, *a, **k):
        return _Cur()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def set_session(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stub_db() -> None:
    """Replace the DB layer so import-time queries return empty, not raise.

    Deliberately stubs at the psycopg2 boundary rather than at each call site:
    that is the one seam every DB path in this repo goes through, so nothing
    can route around it.
    """
    import psycopg2

    psycopg2.connect = lambda *a, **k: _Conn()
    try:
        import psycopg2.pool as _pool

        class _P:
            def __init__(self, *a, **k):
                pass

            def getconn(self, *a, **k):
                return _Conn()

            def putconn(self, *a, **k):
                pass

            def closeall(self):
                pass

        _pool.SimpleConnectionPool = _P
        _pool.ThreadedConnectionPool = _P
    except Exception:
        pass


def boot():
    """Import the real app with the DB stubbed. Returns (app, seconds)."""
    sys.path.insert(0, ROOT)
    os.environ.setdefault("DATABASE_URL", "postgresql://stub:stub@127.0.0.1:1/stub")
    # Import-time hard requirements. Values are inert test placeholders — the
    # gate never talks to a real service.
    os.environ.setdefault("JWT_SECRET", "contract-gate-placeholder-not-a-secret")
    os.environ.setdefault("DCHUB_ADMIN_KEY", "contract-gate-placeholder")
    _stub_db()
    t = time.time()
    import main  # noqa: F401

    return main.app, time.time() - t


def shadowed(app) -> dict:
    """rule+method pairs served by more than one handler."""
    seen = collections.defaultdict(list)
    for r in app.url_map.iter_rules():
        for m in (r.methods or set()) - {"HEAD", "OPTIONS"}:
            seen[f"{m} {r}"].append(r.endpoint)
    return {k: sorted(v) for k, v in seen.items() if len(set(v)) > 1}


def load_baseline() -> dict:
    with open(BASELINE, encoding="utf-8") as fh:
        return json.load(fh)


def main_() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args()

    try:
        app, secs = boot()
    except Exception as e:  # a boot failure IS the finding
        print("FAIL: the app did not boot.")
        print(f"  {type(e).__name__}: {e}")
        print("\nEvery static test in this suite would still pass. That is the")
        print("gap this gate exists to close — fix the import error above.")
        return 1

    rules = list(app.url_map.iter_rules())
    n_rules, n_bps = len(rules), len(app.blueprints)
    shadows = shadowed(app)

    if args.update_baseline:
        base = load_baseline() if os.path.exists(BASELINE) else {}
        base.update({
            "_comment": (
                "Behavior-gate baseline. route/blueprint floors are COLLAPSE "
                "detectors (~10% under true). max_shadowed_routes is a RATCHET "
                "— it may fall, never rise. Regenerate with "
                "scripts/app_contract_gate.py --update-baseline only when the "
                "change is intended; never to make a red build green."
            ),
            "min_routes": int(n_rules * 0.9),
            "min_blueprints": int(n_bps * 0.9),
            "max_shadowed_routes": len(shadows),
            "contract_routes": base.get("contract_routes") or [
                "/api/v1/ops/deadman",
                "/api/v1/health",
                "/llms.txt",
                "/api/v1/ai-agents.json",
                "/robots.txt",
                "/AGENTS.md",
            ],
        })
        with open(BASELINE, "w", encoding="utf-8") as fh:
            json.dump(base, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"baseline written: routes={n_rules} blueprints={n_bps} "
              f"shadowed={len(shadows)}")
        return 0

    base = load_baseline()
    failures: list[str] = []
    print(f"booted in {secs:.1f}s — {n_rules} routes, {n_bps} blueprints, "
          f"{len(shadows)} shadowed")

    if n_rules < base["min_routes"]:
        failures.append(
            f"ROUTE COLLAPSE: {n_rules} routes registered, floor is "
            f"{base['min_routes']}. Blueprint registration failures in this "
            f"codebase are logged and swallowed, not raised — a module that "
            f"died on import just stops serving. Check the boot log above for "
            f"a blueprint that failed to register."
        )
    if n_bps < base["min_blueprints"]:
        failures.append(
            f"BLUEPRINT COLLAPSE: {n_bps} registered, floor is "
            f"{base['min_blueprints']}."
        )

    if len(shadows) > base["max_shadowed_routes"]:
        new = len(shadows) - base["max_shadowed_routes"]
        listing = "\n".join(f"    {k} -> {v}" for k, v in sorted(shadows.items()))
        failures.append(
            f"NEW SHADOWED ROUTE(S): {len(shadows)} now, baseline allows "
            f"{base['max_shadowed_routes']} ({new} added).\n"
            f"  Two handlers claim the same rule+method; Flask serves whichever "
            f"matched first and the other is dead code that still reads as live "
            f"in source. Remove the duplicate registration — do not raise the "
            f"baseline.\n{listing}"
        )

    client = app.test_client()
    for path in base["contract_routes"]:
        try:
            rv = client.get(path)
        except Exception as e:
            failures.append(f"CONTRACT ROUTE RAISED: {path} -> "
                            f"{type(e).__name__}: {str(e)[:200]}")
            continue
        if rv.status_code >= 500 or rv.status_code == 404:
            failures.append(
                f"CONTRACT ROUTE BROKEN: {path} -> {rv.status_code}. This path "
                f"is part of the published agent-facing contract; it must "
                f"serve."
            )

    # 5. THE EVIDENCE-STATUS CONVENTION IS ON THE WIRE.
    #
    # Seven AI partners agreed this on 2026-08-17 and it reached nothing for
    # four days, because it lived in a handoff document instead of a payload.
    # The check belongs HERE and not in the unit-test suite: that job installs
    # light dependencies and cannot import main at all (ModuleNotFoundError:
    # dotenv), which is why every other test there stubs sys.modules['main'].
    # A test that cannot boot the app cannot prove the app serves anything.
    _EV_ROUTE = "/api/v1/mcp/handoff-funnel"
    try:
        _ev_rv = client.get(_EV_ROUTE)
        _ev_payload = _ev_rv.get_json() or {}
    except Exception as e:
        _ev_payload = {}
        failures.append(f"EVIDENCE STATUS UNREADABLE: {_EV_ROUTE} raised "
                        f"{type(e).__name__}: {str(e)[:160]}")
    _ev = (_ev_payload or {}).get("evidence_status") or {}
    _ev_states = set((_ev.get("states") or {}))
    if _ev_states != {"observed", "hypothesis", "verified"}:
        failures.append(
            f"EVIDENCE STATUS MISSING/CHANGED on {_EV_ROUTE}: published states "
            f"are {sorted(_ev_states) or 'ABSENT'}, expected exactly "
            f"['hypothesis', 'observed', 'verified']. Seven AI partners consume "
            f"these literally; dropping or renaming the block breaks a contract "
            f"agreed 2026-08-17, and its absence is how the convention silently "
            f"failed to ship the first time."
        )

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"\n  - {f}")
        return 1

    print("OK: app boots, floors hold, no new shadowed routes, contract serves")
    return 0


if __name__ == "__main__":
    rc = main_()
    # ★ os._exit, not sys.exit. Importing main.py starts the app's background
    # schedulers, and any non-daemon thread among them blocks interpreter
    # shutdown — a normal exit HANGS here, which in CI is a job that burns its
    # full timeout and reports failure with no usable output. The gate's work
    # is finished and its result is already printed, so tear the process down
    # unconditionally.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
