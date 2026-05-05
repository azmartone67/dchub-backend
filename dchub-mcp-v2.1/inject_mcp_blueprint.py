#!/usr/bin/env python3
"""
inject_mcp_blueprint.py — Add the MCP v2.1 blueprint registration to main.py.

Idempotent: detects if the import is already there and exits cleanly.
Always writes a backup at main.py.bak.v21.<timestamp> before modifying.

Run from ~/workspace:
    python3 dchub-mcp-v2.1/inject_mcp_blueprint.py
"""

import re
import shutil
import sys
import time
from pathlib import Path


MAIN_PY = Path("main.py")
SNIPPET = """

# ── MCP v2.1 telemetry + key validation ──────────────────────────
try:
    from flask_mcp_endpoints import mcp_bp
    app.register_blueprint(mcp_bp)
    print('[mcp v2.1] blueprint registered: /api/v1/keys/validate, /api/v1/mcp/track, /api/v1/mcp/stats')
except Exception as _mcp_err:
    print(f'[mcp v2.1] blueprint registration FAILED: {_mcp_err}')

"""


def main():
    if not MAIN_PY.exists():
        sys.exit(f"FAIL: {MAIN_PY.resolve()} not found. Run from ~/workspace.")

    src = MAIN_PY.read_text(encoding="utf-8")

    if "from flask_mcp_endpoints import mcp_bp" in src:
        print(f"OK — already registered in {MAIN_PY}. Nothing to do.")
        return

    # Find `app = Flask(...)` — match across reasonable variants:
    #   app = Flask(__name__)
    #   app = Flask(__name__, static_folder='...')
    #   app=Flask(__name__)
    pat = re.compile(r"^(\s*app\s*=\s*Flask\s*\([^)]*\)[^\n]*)$", re.MULTILINE)
    m = pat.search(src)
    if not m:
        sys.exit(
            "FAIL: could not find a line matching 'app = Flask(...)'.\n"
            "Open main.py in the Replit editor, search for 'app = Flask', and\n"
            "paste these two lines on the next blank line below it:\n\n"
            "    from flask_mcp_endpoints import mcp_bp\n"
            "    app.register_blueprint(mcp_bp)\n"
        )

    insert_at = m.end()  # position right after the match
    # advance past the newline so we insert AFTER the line
    nl = src.find("\n", insert_at)
    if nl < 0:
        nl = len(src)
    insert_at = nl + 1

    # Backup
    backup = Path(f"main.py.bak.v21.{int(time.time())}")
    shutil.copy2(MAIN_PY, backup)

    new_src = src[:insert_at] + SNIPPET + src[insert_at:]
    MAIN_PY.write_text(new_src, encoding="utf-8")

    print(f"OK — patched {MAIN_PY}")
    print(f"   inserted snippet right after line: {m.group(1).strip()}")
    print(f"   backup: {backup}")
    print()
    print("Now restart Flask (Stop + Run in Replit), then watch logs for:")
    print("   [mcp v2.1] blueprint registered: /api/v1/keys/validate, /api/v1/mcp/track, /api/v1/mcp/stats")


if __name__ == "__main__":
    main()
