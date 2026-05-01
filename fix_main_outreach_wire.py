#!/usr/bin/env python3
"""
fix_main_outreach_wire.py — Surgically un-tangles the broken MCP v2.1 +
outreach-bridge wiring in main.py. Idempotent: detects if already fixed.

Drop into ~/workspace and run:
    python3 fix_main_outreach_wire.py
"""

from pathlib import Path
import shutil
import time

BROKEN = """# ── MCP v2.1 telemetry + key validation ──────────────────────────
_mcp_v21_status = {'registered': False, 'error': None, 'traceback': None}
try:
    from flask_mcp_endpoints import mcp_bp
    app.register_blueprint(mcp_bp)


# ── MCP outreach bridge (digest + nurture + dormant winback) ────────
try:
    from mcp_outreach_bridge import register_mcp_outreach_routes
    register_mcp_outreach_routes(app)
    print('[mcp_outreach_bridge] wired into Flask')
except Exception as _err:
    print(f'[mcp_outreach_bridge] wire failed: {_err}')
    _mcp_v21_status['registered'] = True
    print('[mcp v2.1] blueprint registered: /api/v1/keys/validate, /api/v1/mcp/track, /api/v1/mcp/stats')
except Exception as _mcp_err:
    import traceback as _tb
    _mcp_v21_status['error'] = str(_mcp_err)
    _mcp_v21_status['traceback'] = _tb.format_exc()
    print(f'[mcp v2.1] blueprint registration FAILED: {_mcp_err}')

@app.route('/api/v1/_mcp_status')
def _mcp_status_route():
    from flask import jsonify as _jsonify
    return _jsonify(_mcp_v21_status), 200
"""

FIXED = """# ── MCP v2.1 telemetry + key validation ──────────────────────────
_mcp_v21_status = {'registered': False, 'error': None, 'traceback': None}
try:
    from flask_mcp_endpoints import mcp_bp
    app.register_blueprint(mcp_bp)
    _mcp_v21_status['registered'] = True
    print('[mcp v2.1] blueprint registered: /api/v1/keys/validate, /api/v1/mcp/track, /api/v1/mcp/stats')
except Exception as _mcp_err:
    import traceback as _tb
    _mcp_v21_status['error'] = str(_mcp_err)
    _mcp_v21_status['traceback'] = _tb.format_exc()
    print(f'[mcp v2.1] blueprint registration FAILED: {_mcp_err}')

@app.route('/api/v1/_mcp_status')
def _mcp_status_route():
    from flask import jsonify as _jsonify
    return _jsonify(_mcp_v21_status), 200


# ── MCP outreach bridge (digest + nurture + dormant winback) ────────
try:
    from mcp_outreach_bridge import register_mcp_outreach_routes
    register_mcp_outreach_routes(app)
    print('[mcp_outreach_bridge] wired into Flask')
except Exception as _outreach_err:
    print(f'[mcp_outreach_bridge] wire failed: {_outreach_err}')
"""


def main():
    p = Path("main.py")
    if not p.exists():
        raise SystemExit("FAIL: main.py not found in cwd. Run from ~/workspace.")

    src = p.read_text()

    # Idempotency: if already fixed, exit cleanly
    if FIXED.strip() in src:
        print("✓ already fixed — main.py contains the correct structure")
        # Sanity check it still compiles
        import py_compile
        try:
            py_compile.compile(str(p), doraise=True)
            print("✓ compiles cleanly")
        except py_compile.PyCompileError as e:
            print(f"⚠ but doesn't compile: {e}")
        return

    if BROKEN not in src:
        print("⚠ broken pattern not found exactly. Showing what's at the v2.1 region:")
        idx = src.find("MCP v2.1 telemetry")
        if idx > 0:
            print(src[idx:idx + 1500])
        raise SystemExit(
            "Couldn't auto-match. Open main.py, search for "
            "'MCP v2.1 telemetry', and apply the fix manually using the "
            "FIND/REPLACE blocks at the top of this script."
        )

    # Backup + apply
    backup = p.with_suffix(p.suffix + f".bak.outreach_fix.{int(time.time())}")
    shutil.copy2(p, backup)
    print(f"  backup saved: {backup.name}")

    new_src = src.replace(BROKEN, FIXED, 1)
    p.write_text(new_src)
    print("  patched main.py")

    # Validate
    import py_compile
    try:
        py_compile.compile(str(p), doraise=True)
        print("✓ main.py compiles cleanly")
        print()
        print("Next steps:")
        print("  git add main.py")
        print("  git commit -m 'fix: untangle v2.1 + outreach bridge try/except blocks'")
        print("  git push origin main")
        print("  sleep 90")
        print("  curl -s https://dchub-backend-production.up.railway.app/api/v1/_mcp_status | python3 -m json.tool")
    except py_compile.PyCompileError as e:
        print(f"✗ COMPILE FAILED: {e}")
        print(f"  restoring from {backup.name}")
        shutil.copy2(backup, p)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
