#!/bin/bash
# Rotate the leaked enterprise API key from earlier QA paste.
# RUN THIS LOCALLY — needs railway login + DCHUB_ADMIN_KEY env var.
set -e

LEAKED_KEY="dchub_live_08f4fb4d9ade02fceb3e63acd2691730d8a95dc2ed02122d"
NEW_KEY=""  # will be set after mint

echo "=== Step 1: Revoke leaked key ==="
railway run python3 gen_dev_key.py revoke --api-key "$LEAKED_KEY" || {
    echo "  revoke failed — trying alternate command"
    railway run python3 -c "
from gen_dev_key import revoke_key
revoke_key('$LEAKED_KEY')
print('revoked via library call')
"
}

echo ""
echo "=== Step 2: Mint fresh enterprise key ==="
OUT=$(railway run python3 gen_dev_key.py mint \
    --email azmartone@gmail.com \
    --tier enterprise \
    --developer-id admin002)
echo "$OUT"
NEW_KEY=$(echo "$OUT" | grep -oE "dchub_live_[a-f0-9]+" | head -1)

if [ -n "$NEW_KEY" ]; then
    echo ""
    echo "=== Step 3: Update local MCP config (~/.claude.json) ==="
    if [ -f ~/.claude.json ]; then
        cp ~/.claude.json ~/.claude.json.bak.$(date +%s)
        python3 -c "
import json
with open('$HOME/.claude.json') as f: cfg = json.load(f)
# Walk to find dchub MCP and update X-API-Key
def patch(obj):
    if isinstance(obj, dict):
        if 'X-API-Key' in obj and obj.get('X-API-Key') == '$LEAKED_KEY':
            obj['X-API-Key'] = '$NEW_KEY'
            return True
        for v in obj.values():
            if patch(v): return True
    elif isinstance(obj, list):
        for v in obj:
            if patch(v): return True
    return False
patched = patch(cfg)
if patched:
    with open('$HOME/.claude.json','w') as f: json.dump(cfg, f, indent=2)
    print('  ✓ patched ~/.claude.json — backed up to .bak.*')
else:
    print('  ⚠ leaked key not found in ~/.claude.json — may already be different')
"
    fi
    echo ""
    echo "=== Done. New key (KEEP SECRET): ==="
    echo "  $NEW_KEY"
    echo ""
    echo "Other places to update if used:"
    echo "  - Cursor/Cline MCP config"
    echo "  - Any deployed scripts that hit MCP with X-API-Key"
    echo "  - Railway env vars (if any service uses it)"
else
    echo "  ✗ couldn't extract new key from output"
fi
