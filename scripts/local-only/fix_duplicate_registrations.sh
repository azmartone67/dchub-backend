#!/usr/bin/env bash
# Remove the duplicate LinkedIn/digest registration block in main.py.
# The block at ~14108-14125 is a stale fallback; primary reg lives at 4298-4310.
set -u
cd ~/workspace || exit 1
cp main.py main.py.bak_duplicate_reg

echo "=== BEFORE: context around the duplicate block ==="
sed -n '14100,14135p' main.py
echo ""
echo "=== patching... ==="

python3 <<'PY'
import pathlib, re, sys
p = pathlib.Path('main.py')
src = p.read_text()
lines = src.splitlines()

# Find the start of the duplicate block: comment "# Try to register LinkedIn Auto-Posting"
start_linkedin = None
end_block = None
for i, ln in enumerate(lines):
    if 'Try to register LinkedIn Auto-Posting' in ln and start_linkedin is None:
        start_linkedin = i
        break

if start_linkedin is None:
    print("ABORT: LinkedIn 'Try to register' comment not found - block may already be removed", file=sys.stderr)
    sys.exit(2)

# Walk forward to find end of AI Weekly Digest except block
# Pattern: try/except blocks, look for the except that follows register_digest_routes
i = start_linkedin
in_digest_block = False
end_line = None
while i < len(lines) and i < start_linkedin + 60:
    if 'Try to register AI Weekly Digest' in lines[i]:
        in_digest_block = True
    if in_digest_block:
        # find the except and its body (usually pass or logger.warning)
        if re.match(r'^\s*except\b', lines[i]):
            # include next 1-3 lines of except body
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].startswith((' ', '\t'))):
                if lines[j].strip() and not lines[j].lstrip().startswith(('except', 'try', '#')):
                    # we're inside except body
                    if len(lines[j]) - len(lines[j].lstrip()) <= len(lines[i]) - len(lines[i].lstrip()):
                        break
                j += 1
                if j - i > 5:
                    break
            end_line = j
            break
    i += 1

if end_line is None:
    print(f"ABORT: couldn't find end of duplicate block starting at line {start_linkedin+1}", file=sys.stderr)
    sys.exit(2)

print(f"Removing lines {start_linkedin+1}..{end_line} ({end_line - start_linkedin} lines)")
# Show what we're about to remove
print("--- block to remove ---")
for n, ln in enumerate(lines[start_linkedin:end_line], start=start_linkedin+1):
    print(f"{n}: {ln}")
print("--- end block ---")

# Replace the block with a single comment marker
new_lines = lines[:start_linkedin] + [
    "    # [v3 cleanup] duplicate register_linkedin_routes/register_digest_routes",
    "    # block removed — primary registration lives at lines ~4298-4310."
] + lines[end_line:]

pathlib.Path('main.py').write_text('\n'.join(new_lines) + ('\n' if src.endswith('\n') else ''))
print(f"\nOK: removed {end_line - start_linkedin} lines, inserted 2-line marker")
PY

if [ $? -ne 0 ]; then
  echo "Python failed - restoring backup"
  cp main.py.bak_duplicate_reg main.py
  exit 1
fi

echo ""
echo "=== AFTER: context around the patch site ==="
# The patch site shifted down; find it by the marker
MARKER_LINE=$(grep -n "v3 cleanup.*duplicate register_linkedin" main.py | head -1 | cut -d: -f1)
if [ -n "$MARKER_LINE" ]; then
  START=$((MARKER_LINE - 5))
  END=$((MARKER_LINE + 10))
  sed -n "${START},${END}p" main.py
fi

echo ""
echo "=== Python syntax check ==="
python3 -m py_compile main.py && echo "py_compile OK" || {
  echo "SYNTAX ERROR - restoring backup"
  cp main.py.bak_duplicate_reg main.py
  exit 3
}

echo ""
echo "=== verify primary registrations still intact ==="
grep -n "register_linkedin_routes(app)\|register_digest_routes(app)" main.py

rm -f main.py.bak_duplicate_reg
git add main.py
git commit -m "cleanup: remove duplicate register_linkedin_routes / register_digest_routes

The 'Try to register' fallback block at main.py:~14108 was calling
register_linkedin_routes(app) and register_digest_routes(app) a second
time, producing Flask duplicate-endpoint warnings on every boot.

Primary registration at lines ~4298-4310 handles both. Removed the
duplicate block, left a 2-line marker comment in its place."

git push origin main
echo ""
echo "=== DONE. Railway will redeploy; watch for clean boot logs ==="
