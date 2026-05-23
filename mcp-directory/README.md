# DC Hub MCP — Directory Submissions

Source of truth for every external MCP-server directory we want DC Hub
listed in. Each subdirectory or file in here is a submission artifact
that can be applied verbatim to the upstream PR / form / API.

## Status

| Directory                       | File                       | State            |
|---------------------------------|----------------------------|------------------|
| awesome-mcp-servers (GitHub)    | `awesome-mcp-entry.md`     | Ready for PR     |
| glama.ai registry               | `../glama.json`            | Live, refreshed  |
| smithery.ai                     | `../github-repo/smithery.yaml` | Live, refreshed |
| mcp.so directory                | `mcp-so-submission.yaml`   | Ready to submit  |
| Anthropic MCP examples (issue)  | `anthropic-mcp-issue.md`   | Ready to open    |

## Why this matters

Each directory listing drives a measurable conversion bucket:
- **awesome-mcp-servers**: GitHub stargazers → Claude / Cursor users adopting our key
- **glama.ai**: ~12k MAU of MCP-evaluators / agent builders
- **smithery.ai**: surfaces to Smithery CLI installs (`npx @smithery/cli install`)
- **mcp.so**: SEO-heavy directory, ranks for "data center mcp" queries
- **Anthropic examples**: highest authority signal — agents trust Anthropic's list

## Submitting

For each ready entry, the steps are documented in-file. Most are 1-2 minute
PRs. Once shipped, update the State column in this README so we don't
double-submit.
