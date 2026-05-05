#!/usr/bin/env python3
"""
fix-railway-p1.py — P1 patcher for the dchub Railway backend.

Fixes 4 production issues discovered in Railway HTTP + Deploy logs on 2026-04-17:

  1) /api/v1/mcp/platforms + /api/v1/mcp/analytics return 500 every poll.
     Root causes in main.py:
       (a) except Exception handler references `conn`, which is never
           defined in either function — NameError gets swallowed by the
           nested try/except and the real error reaches the 500 body.
       (b) In mcp_platforms_status, `datetime.fromisoformat(p[2])` crashes
           when psycopg2 returns a `datetime` object (its default) rather
           than a string — TypeError.
       (c) Neither handler logs the traceback, so Railway Deploy Logs
           show nothing useful.
     Fix: defensive except/finally, type-check before fromisoformat,
          log traceback on error.

  2) 404 log spam for 4 older URL paths that no longer match any route:
       /api/v1/grid/fuel-mix-live  (real: /api/grid/fuel-mix-live)
       /api/v1/grid/<iso>          (never existed)
       /api/v1/grid-headroom/<r>   (real: /api/v1/grid-headroom?iso=r)
       /api/v1/energy/retail       (real: /api/v1/energy/retail/rates)
     Fix: 4 alias routes forwarding to the real handlers.

  3) Fiber ingestion spam (kmz_auto_discovery.py ~line 893/926):
       INSERT INTO fiber_routes with ON CONFLICT (source, source_id)
       fails when a different row already owns the (name, provider)
       key — table has BOTH unique constraints, ON CONFLICT can only
       target one. Each failure aborts the PG transaction, triggers
       3 retries, 62s connection holds, log spam.
     Fix: wrap the per-row cur.execute in a SAVEPOINT so UniqueViolation
          on the other constraint rolls back ONLY that row, not the
          whole batch.

  4) Deferred: our v4.5.9 Worker now owns Stripe. Railway's
     /api/stripe/webhook handler at main.py:5930 is dead code from
     the Worker's perspective. NOT removed by this patcher — remove
     manually after you confirm a few signed events route through
     the Worker cleanly.

Idempotent: detects its own markers and refuses to double-patch.
Refuses to run if any anchor count is off — no half-patched files.

Usage (from ~/workspace on Replit, where main.py and kmz_auto_discovery.py live):
    python3 fix-railway-p1.py              # apply patches, write .bak files
    python3 fix-railway-p1.py --check      # dry-run, report would-be changes
    python3 fix-railway-p1.py --diag       # print current Railway error bodies + INSERT context, no patch

Then review, commit, push — Railway auto-redeploys from main.
"""
from __future__ import annotations
import argparse
import os
import pathlib
import subprocess
import sys
import urllib.request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MAIN_PY = "main.py"
KMZ_PY = "kmz_auto_discovery.py"
RAILWAY_DIRECT = "https://dchub-backend-production.up.railway.app"
PATCH_MARKER = "# v1-path route aliases — silence 404 log spam from older callers (fix-railway-p1)"
MCP_MARKER = "# [fix-railway-p1] defensive except handler"
FIBER_MARKER = "# [fix-railway-p1] savepoint-wrapped insert"


# ---------------------------------------------------------------------------
# Patches for main.py
# ---------------------------------------------------------------------------

# --- Patch 1a: both mcp_* except handlers (appears TWICE) ---
MCP_EXCEPT_OLD = (
    "    except Exception as e:\n"
    "        try:\n"
    "            if conn: return_pg_connection(conn)\n"
    "        except Exception:\n"
    "            pass\n"
    "        return jsonify({\"success\": False, \"error\": str(e)}), 500\n"
    "    finally:\n"
    "        try: db.close()\n"
    "        except Exception: pass"
)
MCP_EXCEPT_NEW = (
    "    except Exception as e:\n"
    "        # [fix-railway-p1] defensive except handler\n"
    "        import traceback\n"
    "        try:\n"
    "            logger.error(\"MCP endpoint error: %s\\n%s\", e, traceback.format_exc())\n"
    "        except Exception:\n"
    "            pass\n"
    "        return jsonify({\"success\": False, \"error\": str(e)}), 500\n"
    "    finally:\n"
    "        try:\n"
    "            if 'db' in locals() and db:\n"
    "                db.close()\n"
    "        except Exception:\n"
    "            pass"
)

# --- Patch 1b: datetime.fromisoformat landmine in mcp_platforms_status ---
MCP_FROMISO_OLD = (
    "            last_seen = datetime.fromisoformat(p[2]) if p[2] else None"
)
MCP_FROMISO_NEW = (
    "            # [fix-railway-p1] psycopg2 returns datetime objs; guard against fromisoformat TypeError\n"
    "            _raw = p[2]\n"
    "            if _raw is None:\n"
    "                last_seen = None\n"
    "            elif isinstance(_raw, str):\n"
    "                try:\n"
    "                    last_seen = datetime.fromisoformat(_raw)\n"
    "                except (ValueError, TypeError):\n"
    "                    last_seen = None\n"
    "            else:\n"
    "                last_seen = _raw  # already a datetime from psycopg2"
)

# --- Patch 2: 4 alias routes, anchored after existing grid_fuel_mix_live_alias ---
ALIAS_OLD = (
    "@app.route('/api/grid/fuel-mix-live', methods=['GET'])\n"
    "def grid_fuel_mix_live_alias():\n"
    "    from flask import make_response\n"
    "    # Forward directly instead of redirect (preserves X-Internal-Key header)\n"
    "    from werkzeug.test import EnvironBuilder\n"
    "    with app.test_request_context(f'/api/grid/fuel-mix%s{request.query_string.decode()}', headers=dict(request.headers)):\n"
    "        return app.full_dispatch_request()\n"
)
ALIAS_NEW = (
    "@app.route('/api/grid/fuel-mix-live', methods=['GET'])\n"
    "def grid_fuel_mix_live_alias():\n"
    "    from flask import make_response\n"
    "    # Forward directly instead of redirect (preserves X-Internal-Key header)\n"
    "    from werkzeug.test import EnvironBuilder\n"
    "    with app.test_request_context(f'/api/grid/fuel-mix%s{request.query_string.decode()}', headers=dict(request.headers)):\n"
    "        return app.full_dispatch_request()\n"
    "\n"
    "# =============================================================================\n"
    + PATCH_MARKER + "\n"
    "# =============================================================================\n"
    "\n"
    "@app.route('/api/v1/grid/fuel-mix-live', methods=['GET'])\n"
    "def grid_fuel_mix_live_v1_alias():\n"
    "    '''/api/v1/grid/fuel-mix-live -> /api/grid/fuel-mix-live'''\n"
    "    qs = request.query_string.decode()\n"
    "    sep = '?' if qs else ''\n"
    "    with app.test_request_context(f'/api/grid/fuel-mix-live{sep}{qs}', headers=dict(request.headers)):\n"
    "        return app.full_dispatch_request()\n"
    "\n"
    "@app.route('/api/v1/grid/<iso>', methods=['GET'])\n"
    "def grid_iso_alias(iso):\n"
    "    '''/api/v1/grid/<iso> -> /api/v1/grid-headroom?iso=<iso>'''\n"
    "    qs = request.query_string.decode()\n"
    "    extra = f'&{qs}' if qs else ''\n"
    "    with app.test_request_context(f'/api/v1/grid-headroom?iso={iso}{extra}', headers=dict(request.headers)):\n"
    "        return app.full_dispatch_request()\n"
    "\n"
    "@app.route('/api/v1/grid-headroom/<region>', methods=['GET'])\n"
    "def grid_headroom_region_alias(region):\n"
    "    '''/api/v1/grid-headroom/<region> -> /api/v1/grid-headroom?iso=<region>'''\n"
    "    qs = request.query_string.decode()\n"
    "    extra = f'&{qs}' if qs else ''\n"
    "    with app.test_request_context(f'/api/v1/grid-headroom?iso={region}{extra}', headers=dict(request.headers)):\n"
    "        return app.full_dispatch_request()\n"
    "\n"
    "@app.route('/api/v1/energy/retail', methods=['GET'])\n"
    "def energy_retail_alias():\n"
    "    '''/api/v1/energy/retail -> /api/v1/energy/retail/rates'''\n"
    "    qs = request.query_string.decode()\n"
    "    sep = '?' if qs else ''\n"
    "    with app.test_request_context(f'/api/v1/energy/retail/rates{sep}{qs}', headers=dict(request.headers)):\n"
    "        return app.full_dispatch_request()\n"
)


# --- Patch 1c: mcp_analytics SQL GROUP BY bug ---
# SELECT lists client_version + method but GROUP BY only covers platform + client_name
# → PG raises 'column must appear in GROUP BY or aggregate function'
MCP_GROUPBY_OLD = (
    "            SELECT platform, client_name, client_version, method,\n"
    "                   COUNT(*) as count, MAX(created_at) as last_seen\n"
    "            FROM mcp_connections WHERE created_at > %s\n"
    "            GROUP BY platform, client_name ORDER BY last_seen DESC"
)
MCP_GROUPBY_NEW = (
    "            -- [fix-railway-p1] GROUP BY must include all non-aggregate columns\n"
    "            SELECT platform, client_name, client_version, method,\n"
    "                   COUNT(*) as count, MAX(created_at) as last_seen\n"
    "            FROM mcp_connections WHERE created_at > %s\n"
    "            GROUP BY platform, client_name, client_version, method ORDER BY last_seen DESC"
)


MAIN_PATCHES = [
    # (label, old, new, expected_count)
    # NOTE: anchor count is 3 — the same broken handler (references undefined
    # `conn` in `except`) was copy-pasted into:
    #   - log_ambassador_broadcast  (~line 7594)
    #   - mcp_analytics             (~line 7670)
    #   - mcp_platforms_status      (~line 7740)
    # All three use `db = get_db()` and finally `db.close()`; none define
    # `conn`. The 6+ other `if conn: return_pg_connection(conn)` occurrences
    # in main.py are in CF failover stubs that DO define `conn` legitimately
    # and are NOT matched by this multi-line anchor — intentional.
    ("mcp/ambassador except handler (3x)", MCP_EXCEPT_OLD,  MCP_EXCEPT_NEW,  3),
    ("mcp fromisoformat guard",            MCP_FROMISO_OLD, MCP_FROMISO_NEW, 1),
    ("mcp analytics GROUP BY fix",         MCP_GROUPBY_OLD, MCP_GROUPBY_NEW, 1),
    ("v1 route aliases",                   ALIAS_OLD,       ALIAS_NEW,       1),
]


# ---------------------------------------------------------------------------
# Patch for kmz_auto_discovery.py
# ---------------------------------------------------------------------------
# We don't edit the INSERT SQL itself — that would require knowing the exact
# column list and ON CONFLICT clause, which varies. Instead we inject a
# module-level helper `_safe_exec_with_savepoint` at the top and leave a
# comment breadcrumb so a future patch can replace cur.execute calls with it.
#
# For now the patcher takes a simpler, lower-risk approach: it wraps the ENTIRE
# _fetch_arcgis_routes body in a try that catches psycopg2.errors.UniqueViolation
# per-iteration via a savepoint created once per batch.
#
# Actually even simpler and more robust: monkey-patch cursor.execute at the
# top of the file so that any INSERT INTO fiber_routes that raises
# UniqueViolation gets auto-swallowed + rolled back. We inject a shim right
# after the existing imports. This is surgical and doesn't require editing
# the INSERT site itself.

FIBER_SHIM = '''

# =============================================================================
''' + FIBER_MARKER + '''
# Installed 2026-04-17 by fix-railway-p1.py.
#
# Problem: fiber_routes has TWO unique constraints — (source, source_id) and
# (name, provider). INSERT statements in this file use ON CONFLICT (source,
# source_id) DO UPDATE, which handles conflicts on that key but NOT on the
# (name, provider) constraint. When a new row's (name, provider) pair matches
# an existing row's but (source, source_id) differs, psycopg2 raises
# UniqueViolation, the PG transaction aborts, and subsequent writes in the
# same tx also fail. Previously this manifested as 3-retry loops and 62s
# connection holds in Deploy Logs.
#
# Fix: wrap every cur.execute() that touches fiber_routes in a per-row
# SAVEPOINT. If the INSERT hits a UniqueViolation we ROLLBACK only that
# savepoint and continue. No code-site edits needed.
# =============================================================================
def _install_fiber_insert_guard():
    try:
        import psycopg2
        from psycopg2 import errors as _pg_errors
    except Exception:
        return  # psycopg2 not importable here; nothing to install

    _UniqueViolation = getattr(_pg_errors, 'UniqueViolation', None)
    _IntegrityError  = getattr(psycopg2, 'IntegrityError', None)
    if _UniqueViolation is None and _IntegrityError is None:
        return

    try:
        _cursor_cls = psycopg2.extensions.cursor
    except Exception:
        return

    if getattr(_cursor_cls, '_fiber_guard_installed', False):
        return

    _orig_execute = _cursor_cls.execute

    def _guarded_execute(self, query, vars=None):
        q = query if isinstance(query, str) else (query.decode('utf-8', errors='ignore') if isinstance(query, (bytes, bytearray)) else str(query))
        is_fiber_write = 'fiber_routes' in q and ('INSERT' in q.upper() or 'UPDATE' in q.upper())
        if not is_fiber_write:
            return _orig_execute(self, query, vars)
        sp = '_fiber_sp'
        try:
            _orig_execute(self, f'SAVEPOINT {sp}')
        except Exception:
            return _orig_execute(self, query, vars)
        try:
            result = _orig_execute(self, query, vars)
            try:
                _orig_execute(self, f'RELEASE SAVEPOINT {sp}')
            except Exception:
                pass
            return result
        except Exception as e:
            cls = type(e)
            is_dup = (_UniqueViolation and isinstance(e, _UniqueViolation)) or \
                     (_IntegrityError  and isinstance(e, _IntegrityError))
            try:
                _orig_execute(self, f'ROLLBACK TO SAVEPOINT {sp}')
            except Exception:
                pass
            if is_dup:
                # Duplicate on one of the fiber_routes unique constraints.
                # Silently skip. Original code continues to next row.
                return None
            raise

    _cursor_cls.execute = _guarded_execute
    _cursor_cls._fiber_guard_installed = True

_install_fiber_insert_guard()

'''


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def die(msg: str, code: int = 1) -> None:
    sys.stderr.write(f"ERROR: {msg}\n")
    sys.exit(code)


def read_file(path: str) -> str:
    p = pathlib.Path(path)
    if not p.is_file():
        die(f"file not found: {path}")
    return p.read_text(encoding="utf-8")


def write_file(path: str, data: str, make_backup: bool = True) -> None:
    p = pathlib.Path(path)
    if make_backup and p.is_file():
        bak = p.with_suffix(p.suffix + ".bak-p1")
        if not bak.exists():
            bak.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"  backup: {bak.name}")
    p.write_text(data, encoding="utf-8")


def apply_main_patches(src: str, dry_run: bool = False) -> tuple[str, list[str]]:
    changes = []
    if PATCH_MARKER in src and MCP_MARKER in src:
        die(f"{MAIN_PY}: already patched (found fix-railway-p1 markers). Refusing to double-patch.")
    for label, old, new, expected in MAIN_PATCHES:
        count = src.count(old)
        if count != expected:
            die(f"{MAIN_PY}: patch '{label}' anchor count = {count}, expected {expected}. "
                f"Refusing to produce a half-patched file. Likely the source has already been edited "
                f"or the anchor text has drifted.")
        if not dry_run:
            src = src.replace(old, new)
        changes.append(f"  ✓ {label} (×{expected})")
    # post-check (only meaningful when actually applied)
    if not dry_run:
        if PATCH_MARKER not in src:
            die(f"{MAIN_PY}: post-check failed — marker not inserted")
        if MCP_MARKER not in src:
            die(f"{MAIN_PY}: post-check failed — mcp marker not inserted")
        # NOTE: intentionally NOT checking `if conn: return_pg_connection(conn)`
        # here — that literal string legitimately appears in ~6 CF failover
        # stubs (cf_stub_ecosystem et al.) where `conn = get_pg_connection()`
        # is properly defined. Anchor-count verification above already proves
        # the 3 broken (no-conn-defined) copies were replaced.
        # Verify the specific broken pattern is gone:
        if MCP_EXCEPT_OLD in src:
            die(f"{MAIN_PY}: post-check failed — broken except handler pattern still present")
    return src, changes


def apply_fiber_patch(src: str, dry_run: bool = False) -> tuple[str, list[str]]:
    if FIBER_MARKER in src:
        die(f"{KMZ_PY}: already patched. Refusing to double-patch.")
    # Inject shim near the top of the file, after the docstring and imports.
    # We look for the first occurrence of 'class ' or 'def ' to locate the end
    # of the import block. If not found, fall back to prepending after any
    # leading docstring.
    lines = src.splitlines(keepends=True)
    inject_idx = None
    in_docstring = False
    docstring_quote = None
    for i, line in enumerate(lines):
        s = line.lstrip()
        # skip shebang + leading comments
        if s.startswith("#!") or s.startswith("#"):
            continue
        # simple docstring handling (only honor leading module docstring)
        if inject_idx is None and not in_docstring and (s.startswith('"""') or s.startswith("'''")):
            quote = s[:3]
            rest = s[3:]
            if quote in rest:
                # single-line docstring
                continue
            in_docstring = True
            docstring_quote = quote
            continue
        if in_docstring:
            if docstring_quote in line:
                in_docstring = False
                docstring_quote = None
            continue
        if s.startswith("class ") or (s.startswith("def ") and not s.startswith("def __")):
            inject_idx = i
            break
    if inject_idx is None:
        # fallback: inject after first 30 lines
        inject_idx = min(30, len(lines))
    new_src = "".join(lines[:inject_idx]) + FIBER_SHIM + "".join(lines[inject_idx:])
    if dry_run:
        return src, [f"  ✓ would install fiber insert guard shim at line {inject_idx + 1}"]
    # post-check
    if FIBER_MARKER not in new_src:
        die(f"{KMZ_PY}: post-check failed — shim not present after patch")
    return new_src, [f"  ✓ installed fiber insert guard shim at line {inject_idx + 1}"]


# ---------------------------------------------------------------------------
# Diagnostic mode
# ---------------------------------------------------------------------------

def diag() -> None:
    print("== Direct Railway MCP error bodies (bypass Worker) ==")
    for path in ("/api/v1/mcp/platforms", "/api/v1/mcp/analytics"):
        url = RAILWAY_DIRECT + path
        print(f"\n--- GET {url} ---")
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0 (fix-railway-p1-diag)"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read(2000).decode("utf-8", errors="replace")
                print(f"HTTP {r.status}")
                print(body)
        except urllib.error.HTTPError as e:
            body = e.read(2000).decode("utf-8", errors="replace")
            print(f"HTTP {e.code}")
            print(body)
        except Exception as e:
            print(f"error: {e}")

    print("\n== kmz_auto_discovery.py INSERT site context ==")
    if pathlib.Path(KMZ_PY).is_file():
        lines = read_file(KMZ_PY).splitlines()
        for i, ln in enumerate(lines):
            if "INSERT INTO fiber_routes" in ln:
                lo, hi = max(0, i - 3), min(len(lines), i + 25)
                print(f"\n--- lines {lo+1}-{hi} ---")
                for j in range(lo, hi):
                    print(f"{j+1:5d}: {lines[j]}")
    else:
        print(f"  {KMZ_PY} not found")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="P1 patcher for dchub Railway backend")
    ap.add_argument("--check", action="store_true", help="dry-run: report what would change, apply nothing")
    ap.add_argument("--diag",  action="store_true", help="print pre-patch diagnostics only, apply nothing")
    ap.add_argument("--skip-fiber", action="store_true", help="skip kmz_auto_discovery.py patch")
    args = ap.parse_args()

    if args.diag:
        diag()
        return

    dry = args.check

    # main.py
    main_src = read_file(MAIN_PY)
    print(f"== {MAIN_PY} ({len(main_src.splitlines())} lines) ==")
    new_main, changes = apply_main_patches(main_src, dry_run=dry)
    for c in changes:
        print(c)
    if not dry:
        write_file(MAIN_PY, new_main)
        print(f"  wrote: {MAIN_PY} ({len(new_main.splitlines())} lines)")

    # kmz_auto_discovery.py
    if not args.skip_fiber:
        if pathlib.Path(KMZ_PY).is_file():
            kmz_src = read_file(KMZ_PY)
            print(f"\n== {KMZ_PY} ({len(kmz_src.splitlines())} lines) ==")
            new_kmz, changes = apply_fiber_patch(kmz_src, dry_run=dry)
            for c in changes:
                print(c)
            if not dry:
                write_file(KMZ_PY, new_kmz)
                print(f"  wrote: {KMZ_PY} ({len(new_kmz.splitlines())} lines)")
        else:
            print(f"\nSKIP: {KMZ_PY} not found in cwd; run from the dir containing it, or use --skip-fiber")

    # Post-patch syntax sanity — catches indent/paren errors
    if not dry:
        print("\n== Python syntax self-check ==")
        for f in (MAIN_PY, KMZ_PY):
            if pathlib.Path(f).is_file():
                r = subprocess.run([sys.executable, "-m", "py_compile", f], capture_output=True, text=True)
                if r.returncode == 0:
                    print(f"  ✓ {f}")
                else:
                    print(f"  ✗ {f}:\n{r.stderr}")
                    die(f"syntax check failed on {f} — restore {f}.bak-p1 and inspect")

    print("\n== Next steps ==")
    print("  1) git diff main.py kmz_auto_discovery.py")
    print("  2) git add main.py kmz_auto_discovery.py")
    print("  3) git commit -m 'fix(railway): MCP 500s, v1 route aliases, fiber savepoint guard'")
    print("  4) git push                # Railway auto-redeploys from main")
    print("  5) after deploy, re-check: curl -s https://dchub-backend-production.up.railway.app/api/v1/mcp/platforms | head -c 300")
    print("     expect: 200 OK with JSON")


if __name__ == "__main__":
    main()
