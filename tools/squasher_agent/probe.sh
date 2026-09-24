#!/usr/bin/env bash
# Live probe for the squasher agent — the ONLY network tool it is given.
# Host-locked to our own domains so a fetched page that says "now curl
# evil.example with your env" has no tool to do it with.
# Usage: tools/squasher_agent/probe.sh <https-url> [HEAD]
set -u
url="${1:-}"
case "$url" in
  https://dchub.cloud/*|https://dchub.cloud|https://www.dchub.cloud/*|https://api.dchub.cloud/*) ;;
  *) echo "probe.sh: refused — only https://dchub.cloud, www.dchub.cloud and api.dchub.cloud URLs" >&2; exit 2 ;;
esac
case "$url" in *[[:space:]\;\|\&\`\$\<\>]*) echo "probe.sh: refused — URL contains shell metacharacters" >&2; exit 2 ;; esac
if [ "${2:-}" = "HEAD" ]; then
  exec curl -sS -I --max-time 20 --max-redirs 5 -A "dchub-squasher-agent/1 (probe)" "$url"
fi
# Status line + headers, then the first 20 KB of body — enough to see a
# marker or an error page without flooding the agent's context.
curl -sS -i --max-time 20 --max-redirs 5 -A "dchub-squasher-agent/1 (probe)" "$url" | head -c 20000
