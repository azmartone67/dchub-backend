# MCP Directory Submissions — Round 38

Three directories to submit to. Each is a separate manual action; the
JSON/YAML files in this folder are ready to paste.

## 1. mcp.so (largest community directory)
- URL: https://mcp.so/submit
- File: `mcp-so-submission.json`
- Paste the JSON or fill the web form with these fields
- Expected: live within 24h after manual review

## 2. Smithery (CLI installer ecosystem)
- URL: https://smithery.ai/submit
- File: `smithery-submission.yaml`
- Requires GitHub auth
- Once live, users install with: `npx -y @smithery/cli install dc-hub-intelligence`

## 3. Anthropic MCP Registry (official catalog)
- URL: https://github.com/modelcontextprotocol/registry (PR to repo)
- File: `anthropic-mcp-registry.json`
- Open PR adding this file under `registry/dchub-intelligence.json`
- Requires repo signature verification — Anthropic team reviews

## Expected outcomes
- mcp.so: 100s of monthly impressions, ~5-10 weekly new clients
- Smithery: ~50/week installs from CLI-savvy devs
- Anthropic: featured if approved; massive credibility boost in Claude

## Tracking
Once live, watch `https://api.dchub.cloud/api/v1/cited-by` for new
`platform` entries — each directory's traffic shows up as a distinct
user-agent pattern.
