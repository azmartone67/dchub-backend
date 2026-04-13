"""
Auto-patcher for dchub_mcp_server.py
=====================================
Adds gatekeeper calls (gate + finalize) to every @mcp.tool handler
and wires up the ASGI middleware + DB init.

Usage:
    python patch_mcp_server.py                          # patches in place
    python patch_mcp_server.py --dry-run                # preview only
    python patch_mcp_server.py --input server.py --output server_patched.py
"""

import re
import sys
import argparse
import shutil
from datetime import datetime


def patch_server(source: str) -> tuple:
    """
    Patch the MCP server source code.
    Returns (patched_source, list_of_changes).
    """
    changes = []
    lines = source.split("\n")
    output_lines = []

    # ═══════════════════════════════════════════════════════════
    # State machine: track which tool we're inside
    # ═══════════════════════════════════════════════════════════
    pending_tool_name = None   # set when we see @mcp.tool(name="X")
    current_tool_name = None   # set when we're inside a tool function body
    in_func_sig = False        # True while inside multi-line async def(...)
    in_docstring = False       # tracks multi-line docstrings
    docstring_done = False     # flag: just finished docstring, insert gate
    gate_inserted = False      # did we already insert gate for this tool?
    finalize_count = 0
    gate_count = 0

    # ═══════════════════════════════════════════════════════════
    # 1. Add import (prepend to output)
    # ═══════════════════════════════════════════════════════════
    import_line = "from mcp_gatekeeper import gate, finalize, GatekeeperMiddleware, init_db, _load_keys_from_db"
    import_added = "mcp_gatekeeper" in source

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()

        # ── Insert import after `from mcp.server.fastmcp import FastMCP` ──
        # Must start at column 0 to avoid matching inside docstrings
        if not import_added and line.startswith("from mcp.server.fastmcp import FastMCP"):
            output_lines.append(line)
            output_lines.append("")
            output_lines.append("# ═══ Gatekeeper (auth + rate limiting + tier gating) ═══")
            output_lines.append(import_line)
            import_added = True
            changes.append("Added gatekeeper import after FastMCP import")
            i += 1
            continue

        # ── Detect @mcp.tool(name="XXX") on the decorator line ──
        if "@mcp.tool(" in stripped:
            nm = re.search(r'name="([^"]+)"', line)
            if nm:
                pending_tool_name = nm.group(1)
                gate_inserted = False

        # ── Detect the name on a SEPARATE line inside the decorator ──
        if pending_tool_name is None and stripped.startswith('name="'):
            nm = re.search(r'name="([^"]+)"', stripped)
            if nm:
                pending_tool_name = nm.group(1)
                gate_inserted = False

        # ── Detect async def start ──
        if pending_tool_name and stripped.startswith("async def "):
            in_func_sig = True

        # ── Track end of function signature: line ending with -> str: ──
        if in_func_sig and "-> str:" in stripped:
            # Function signature complete on this line
            in_func_sig = False
            current_tool_name = pending_tool_name
            pending_tool_name = None
            in_docstring = False
            docstring_done = False
            output_lines.append(line)
            i += 1
            continue

        # ── If still in func signature, just pass through ──
        if in_func_sig:
            output_lines.append(line)
            i += 1
            continue

        # ── Track docstring boundaries (only when inside tool body, gate not yet inserted) ──
        if current_tool_name and not gate_inserted:
            if '"""' in stripped:
                tq_count = stripped.count('"""')
                if tq_count >= 2:
                    # Single-line docstring or closing line with two sets of quotes
                    if not in_docstring:
                        docstring_done = True
                    else:
                        in_docstring = False
                        docstring_done = True
                elif tq_count == 1:
                    if in_docstring:
                        in_docstring = False
                        docstring_done = True
                    else:
                        in_docstring = True

            # ── Insert gate after docstring closes ──
            if docstring_done and not in_docstring and not gate_inserted:
                output_lines.append(line)  # emit the docstring closing line

                # Detect indent from next non-blank line
                indent = "    "
                for j in range(i + 1, min(i + 6, len(lines))):
                    ns = lines[j].lstrip()
                    if ns and not ns.startswith("#"):
                        indent = lines[j][:len(lines[j]) - len(ns)]
                        break

                # Check if gate already exists
                lookahead = "\n".join(lines[i+1:i+5])
                if "gate(" not in lookahead:
                    output_lines.append(f"{indent}# ── Auth gate ──")
                    output_lines.append(f'{indent}_block = gate("{current_tool_name}")')
                    output_lines.append(f"{indent}if _block: return _block")
                    output_lines.append("")
                    gate_inserted = True
                    gate_count += 1
                    changes.append(f"Added gate() to {current_tool_name}")
                else:
                    gate_inserted = True

                docstring_done = False
                i += 1
                continue

        # ── Wrap return json.dumps(result...) with finalize() ──
        if current_tool_name and gate_inserted and "return json.dumps(" in stripped:
            if "finalize(" not in stripped:
                if "error" not in stripped and ("result" in stripped or "results" in stripped):
                    match = re.match(r'^(\s*)return (json\.dumps\(.+\))\s*$', line)
                    if match:
                        indent = match.group(1)
                        dumps_call = match.group(2)
                        line = f'{indent}return finalize({dumps_call}, "{current_tool_name}")'
                        finalize_count += 1

        # ── Detect new top-level block (end of current tool) ──
        if (current_tool_name and gate_inserted and stripped
                and not stripped.startswith("#") and not in_docstring):
            current_indent = len(line) - len(stripped)
            if current_indent == 0 and (stripped.startswith("@") or stripped.startswith("def ")
                                         or stripped.startswith("class ")
                                         or stripped.startswith("async def ")
                                         or stripped.startswith("# ===")):
                current_tool_name = None

        output_lines.append(line)
        i += 1

    if finalize_count:
        changes.append(f"Wrapped {finalize_count} return statements with finalize()")

    source = "\n".join(output_lines)

    # ═══════════════════════════════════════════════════════════
    # 2. Add GatekeeperMiddleware to the ASGI app
    # ═══════════════════════════════════════════════════════════
    if "GatekeeperMiddleware(app)" not in source:
        # Must go AFTER CORSMiddleware is added, right before uvicorn.run()
        source = re.sub(
            r'^(\s*)(uvicorn\.run\(app,)',
            r'\1# ═══ Gatekeeper ASGI middleware (extracts x-api-key from headers) ═══\n'
            r'\1app = GatekeeperMiddleware(app)\n\n'
            r'\1\2',
            source,
            count=1,
            flags=re.MULTILINE,
        )
        changes.append("Added GatekeeperMiddleware to ASGI app")

    # ═══════════════════════════════════════════════════════════
    # 3. Add DB init on startup
    # ═══════════════════════════════════════════════════════════
    if "init_db()" not in source:
        source = source.replace(
            '    port = MCP_PORT',
            '    # Initialize gatekeeper DB + keys\n'
            '    try:\n'
            '        init_db()\n'
            '        _load_keys_from_db()\n'
            '    except Exception as _gk_err:\n'
            '        logger.warning(f"⚠️ Gatekeeper init: {_gk_err}")\n'
            '\n'
            '    port = MCP_PORT',
            1  # only replace first occurrence
        )
        changes.append("Added gatekeeper DB init on startup")

    # ═══════════════════════════════════════════════════════════
    # 4. Update version
    # ═══════════════════════════════════════════════════════════
    source = source.replace(
        "DC Hub Nexus — MCP Server (Production) v2.2.1",
        "DC Hub Nexus — MCP Server (Production) v2.3.0"
    )
    source = source.replace(
        "  Tools: 24 | Resources: 6 | Prompts: 4",
        "  Tools: 24 | Resources: 6 | Prompts: 4 | Gatekeeper: active"
    )
    changes.append("Updated version to v2.3.0")

    # Summary
    changes.append(f"TOTAL: {gate_count} tools gated, {finalize_count} returns wrapped")

    return source, changes


def main():
    parser = argparse.ArgumentParser(description="Patch dchub_mcp_server.py with gatekeeper")
    parser.add_argument("--input", default="dchub_mcp_server.py", help="Input file")
    parser.add_argument("--output", default=None, help="Output file (default: overwrite input)")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes only")
    args = parser.parse_args()

    output = args.output or args.input

    try:
        with open(args.input, "r") as f:
            source = f.read()
    except FileNotFoundError:
        print(f"❌ File not found: {args.input}")
        print("Make sure you're in the right directory.")
        sys.exit(1)

    # Create backup
    if not args.dry_run:
        backup = f"{args.input}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(args.input, backup)
        print(f"📋 Backup saved: {backup}")

    patched, changes = patch_server(source)

    print(f"\n{'DRY RUN — ' if args.dry_run else ''}Changes:")
    for c in changes:
        print(f"  ✅ {c}")

    if not args.dry_run:
        with open(output, "w") as f:
            f.write(patched)
        print(f"\n💾 Patched file saved: {output}")
        print(f"\n⚡ Restart your MCP server to activate gatekeeper")
    else:
        print(f"\nRun without --dry-run to apply changes.")


if __name__ == "__main__":
    main()
