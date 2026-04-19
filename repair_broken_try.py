import re, os, sys

broken_files = [
    'ai_interconnection.py', 'alert_system_v2.py', 'api_fixes.py',
    'api_monetization.py', 'discovery_pipeline.py', 'ecosystem_routes.py',
    'email_service.py', 'google_search_console.py', 'h3_scoring.py',
    'index_api.py', 'main.py', 'mcp_server.py', 'news_to_pipeline.py'
]

fixed_count = 0
for fname in broken_files:
    if not os.path.exists(fname):
        print(f"  SKIP {fname} (not found)")
        continue
    with open(fname, 'r') as f:
        lines = f.readlines()
    
    new_lines = []
    i = 0
    fixes = 0
    while i < len(lines):
        # Detect: line with get_db() followed by a rogue "try:" at LOWER indent
        if i + 1 < len(lines) and 'get_db()' in lines[i]:
            get_db_indent = len(lines[i]) - len(lines[i].lstrip())
            next_stripped = lines[i+1].strip()
            next_indent = len(lines[i+1]) - len(lines[i+1].lstrip())
            if next_stripped == 'try:' and next_indent <= get_db_indent:
                # Found the broken pattern - skip the rogue try:
                new_lines.append(lines[i])  # keep get_db() line
                i += 1  # skip rogue try:
                # Now un-indent everything that was over-indented until we hit
                # a finally: block at the same indent as the rogue try
                rogue_indent = next_indent
                extra = '    '  # 4 spaces the fixer added
                i += 1
                while i < len(lines):
                    line = lines[i]
                    stripped = line.strip()
                    curr_indent = len(line) - len(line.lstrip())
                    # Check for fixer's finally: block at rogue indent level
                    if stripped == 'finally:' and curr_indent == rogue_indent:
                        # Check if next line is conn.close() - remove both
                        if i + 1 < len(lines) and '.close()' in lines[i+1]:
                            i += 2  # skip finally: and conn.close()
                            fixes += 1
                            break
                        else:
                            i += 1
                            fixes += 1
                            break
                    # Un-indent lines that have extra indentation
                    if line.startswith(' ' * (rogue_indent + 8)) and stripped:
                        new_lines.append(' ' * (rogue_indent + 4) + line.lstrip())
                    elif stripped == '':
                        new_lines.append(line)
                    else:
                        new_lines.append(line)
                    i += 1
                fixes += 1
                continue
        new_lines.append(lines[i])
        i += 1
    
    if fixes > 0:
        with open(fname, 'w') as f:
            f.writelines(new_lines)
        print(f"  FIXED {fname} ({fixes} broken try block(s))")
        fixed_count += 1
    else:
        print(f"  NO FIX NEEDED {fname}")

print(f"\nRepaired {fixed_count} files")
