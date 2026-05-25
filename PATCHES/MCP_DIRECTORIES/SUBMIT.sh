#!/bin/bash
# Submit DC Hub MCP to 3 directories.
# Manual paste/PR — Smithery + mcp.so don't have public submission APIs.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"

cat <<'EOF'
═══════════════════════════════════════════════════════════════
MCP DIRECTORY SUBMISSION CHECKLIST
═══════════════════════════════════════════════════════════════

1. mcp.so (largest community directory)
   ▸ Open: https://mcp.so/submit
   ▸ Paste contents of: mcp-so-submission.json
   ▸ Expected: live within 24h after manual review

2. Smithery (CLI installer ecosystem)
   ▸ Open: https://smithery.ai/submit
   ▸ Paste contents of: smithery-submission.yaml
   ▸ Requires GitHub auth
   ▸ Once live: users install with `npx -y @smithery/cli install dc-hub-intelligence`

3. Anthropic MCP Registry (official catalog)
   ▸ Fork: https://github.com/modelcontextprotocol/registry
   ▸ Add: registry/dchub-intelligence.json (use anthropic-mcp-registry.json content)
   ▸ Open PR with title: "Add DC Hub Intelligence MCP server"
   ▸ Anthropic team reviews ~1 week

═══════════════════════════════════════════════════════════════
EOF

# Open the 3 submission pages in browser
echo ""
echo "Opening submission pages in your browser..."
open "https://mcp.so/submit"
sleep 1
open "https://smithery.ai/submit"
sleep 1
open "https://github.com/modelcontextprotocol/registry/fork"

echo ""
echo "Submission JSON/YAML files are in: $DIR"
echo "  - mcp-so-submission.json (for mcp.so)"
echo "  - smithery-submission.yaml (for Smithery)"
echo "  - anthropic-mcp-registry.json (for Anthropic PR)"
echo ""
echo "Auto-open the files for easy copy:"
open "$DIR/mcp-so-submission.json"
open "$DIR/smithery-submission.yaml"
open "$DIR/anthropic-mcp-registry.json"
