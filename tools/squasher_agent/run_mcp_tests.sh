#!/usr/bin/env bash
# vitest for dchub-mcp-server (checked out at ./mcp beside dchub-backend), with
# the environment EMPTIED like run_tests.sh: the agent's process holds
# ANTHROPIC_API_KEY and a test it writes must not be able to read it.
# Only test FILE paths are accepted (mcp/test/x.test.mjs or test/x.test.mjs);
# every flag is refused, so no vitest/node option can be smuggled through.
# Usage: tools/squasher_agent/run_mcp_tests.sh mcp/test/x.test.mjs [...]
set -u
root="$(cd "$(dirname "$0")/../.." && pwd)"
# Arguments are validated BEFORE anything else, so a refusal is never masked
# by a later, unrelated exit (a missing checkout, a missing vitest).
[ $# -gt 0 ] || { echo "run_mcp_tests.sh: name at least one test file" >&2; exit 2; }
files=()
for a in "$@"; do
  case "$a" in
    -*) echo "run_mcp_tests.sh: refused flag $a" >&2; exit 2 ;;
    *..*|/*) echo "run_mcp_tests.sh: refused path $a" >&2; exit 2 ;;
  esac
  files+=("${a#mcp/}")
done
[ -d "$root/mcp" ] || { echo "run_mcp_tests.sh: no mcp/ checkout beside the backend" >&2; exit 2; }
cd "$root/mcp" || exit 2
exec env -i PATH="$PATH" HOME="${RUNNER_TEMP:-/tmp}/agent-home" LANG=C.UTF-8 CI=1 \
  npx --no-install vitest run "${files[@]}"
