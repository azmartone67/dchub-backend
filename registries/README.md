# MCP Registry Submissions

Submission-ready JSON manifests for the three major MCP server registries.
Each file targets one registry's specific submission schema.

## How to submit

### 1. modelcontextprotocol.io (official Anthropic registry)
- Fork: <https://github.com/modelcontextprotocol/registry>
- Add `modelcontextprotocol.io.json` to `servers/dchub.json` (or whatever path
  the registry's CONTRIBUTING.md says)
- Open a PR

### 2. mcp.run (community registry)
- Submit via: <https://mcp.run/submit>
- Paste the contents of `mcp.run.json` into their form

### 3. anthropic-quickstarts (Anthropic's example list)
- Fork: <https://github.com/anthropics/anthropic-quickstarts>
- Open an issue referencing `anthropic-quickstarts.json` content
- Or PR an entry into their `mcp-servers/` directory

## Keep these files current

Anytime the MCP tool surface changes (new tool, tier change, transport change),
update `/.well-known/mcp.json` first (canonical) and then re-export these
manifests with the same data. The registries don't auto-refresh, so each
substantive change needs a follow-up registry update.

Last sync: 2026-05-15 (Phase RR)
