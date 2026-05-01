#!/usr/bin/env python3
"""
fix_merged_line.py — Splits the merged line in main.py where
`app.register_blueprint(mcp_bp)` and `_mcp_v21_status['registered'] = True`
got fused into one line, which is invalid Python.

Run from ~/workspace:
    python3 fix_merged_line.py
"""
import re
import shutil
import time
from pathlib import Path

BAD_PATTERNS = [
    # Most common shape — multiple spaces between the two statements
    (
        re.compile(r"(    app\.register_blueprint\(mcp_bp\))\s+(_mcp_v21_status\[\'registered\'\] = True)"),
        r"\1\n    \2",
    ),
    # Same statement could also appear with single space variants
    (
        re.compile(r"(app\.register_blueprint\(mcp_bp\))(\s*_mcp_v21_status\[\'registered\'\])"),
        r"\1\n    \2",
    ),
]


def main():
    p = Path("main.py")
    if not p.exists():
        raise SystemExit("FAIL: main.py not found in cwd. Run from ~/workspace.")

    src = p.read_text()
    fixes_applied = 0
    new_src = src
    for pattern, repl in BAD_PATTERNS:
        new_src, n = pattern.subn(repl, new_src)
        fixes_applied += n
        if n:
            print(f"  fixed {n} occurrence(s) of {pattern.pattern[:60]}...")

    if fixes_applied == 0:
        # Already fixed? Verify by compiling.
        import py_compile
        try:
            py_compile.compile(str(p), doraise=True)
            print("✓ main.py compiles cleanly — nothing to fix")
            return
        except py_compile.PyCompileError as e:
            print(f"⚠ main.py doesn't compile but no merged-line pattern found:")
            print(f"  {e}")
            # Show the v2.1 region for diagnosis
            idx = src.find("MCP v2.1 telemetry")
            if idx > 0:
                print()
                print("--- current v2.1 region (lines around it): ---")
                print(src[idx:idx + 1200])
            raise SystemExit("Manual inspection needed.")

    # Backup + write
    backup = p.with_suffix(p.suffix + f".bak.merged_line_fix.{int(time.time())}")
    shutil.copy2(p, backup)
    print(f"  backup saved: {backup.name}")
    p.write_text(new_src)
    print(f"  patched main.py ({fixes_applied} total fixes)")

    # Validate
    import py_compile
    try:
        py_compile.compile(str(p), doraise=True)
        print("✓ main.py compiles cleanly")
        print()
        print("Next:")
        print("  git add main.py")
        print("  git commit -m 'fix: split merged line app.register_blueprint(mcp_bp) and _mcp_v21_status assignment'")
        print("  git push origin main")
        print("  sleep 90")
        print("  curl -s https://dchub-backend-production.up.railway.app/api/v1/_mcp_status | python3 -m json.tool")
    except py_compile.PyCompileError as e:
        print(f"✗ STILL DOESN'T COMPILE after split: {e}")
        print(f"  restoring from {backup.name}")
        shutil.copy2(backup, p)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
