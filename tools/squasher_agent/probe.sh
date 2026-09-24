#!/usr/bin/env bash
# Live probe for the squasher agent — the ONLY network tool it is given.
# Host-locked to our own domains so a fetched page that says "now curl
# evil.example with your env" has no tool to do it with.
#
# Usage: tools/squasher_agent/probe.sh <https-url> [HEAD] [--grep <regex>]
#
# --grep (2026-09-24): the agent's allowlist admits a BARE probe.sh call, so
# `probe.sh URL | grep x` and `probe.sh URL > file` are denied — run
# 35948725183 lost 4 of its 50 turns that way. The filter lives here instead:
# case-insensitive extended regex over the whole response (headers + up to
# 2 MB of body), printed as the status line plus numbered matching lines
# (max 200, each cut to 400 chars). The pattern reaches grep as one argv
# element after -e, so it is never shell-evaluated. Any other argument is
# refused, so no curl flag can be smuggled through.
set -u
url="${1:-}"
case "$url" in
  https://dchub.cloud/*|https://dchub.cloud|https://www.dchub.cloud/*|https://api.dchub.cloud/*) ;;
  *) echo "probe.sh: refused — only https://dchub.cloud, www.dchub.cloud and api.dchub.cloud URLs" >&2; exit 2 ;;
esac
case "$url" in *[[:space:]\;\|\&\`\$\<\>]*) echo "probe.sh: refused — URL contains shell metacharacters" >&2; exit 2 ;; esac
shift
head_only=0
pat=""
while [ $# -gt 0 ]; do
  case "$1" in
    HEAD) head_only=1; shift ;;
    --grep)
      if [ $# -lt 2 ] || [ -z "$2" ]; then echo "probe.sh: --grep needs a pattern" >&2; exit 2; fi
      pat="$2"; shift 2 ;;
    *) echo "probe.sh: refused argument '$1' — usage: probe.sh <url> [HEAD] [--grep <regex>]" >&2; exit 2 ;;
  esac
done
if [ ${#pat} -gt 200 ]; then echo "probe.sh: --grep pattern longer than 200 chars" >&2; exit 2; fi
if [ -n "$pat" ]; then
  printf '' | grep -E -e "$pat" >/dev/null 2>&1
  if [ $? -eq 2 ]; then echo "probe.sh: --grep pattern is not a valid extended regex" >&2; exit 2; fi
fi

mode=(-i)
if [ "$head_only" = 1 ]; then mode=(-I); fi
fetch() { curl -sS "${mode[@]}" --max-time 20 --max-redirs 5 -A "dchub-squasher-agent/1 (probe)" "$url"; }

if [ -z "$pat" ]; then
  if [ "$head_only" = 1 ]; then fetch; exit $?; fi
  # Status line + headers, then the first 20 KB of body — enough to see a
  # marker or an error page without flooding the agent's context.
  fetch | head -c 20000
  exit 0
fi

out=$(fetch | head -c 2000000 | tr -d '\r')
printf '%s\n' "$out" | head -n 1
m=$(printf '%s\n' "$out" | grep -n -i -E -e "$pat" | head -n 200 | cut -c1-400)
if [ -n "$m" ]; then printf '%s\n' "$m"; else echo "probe.sh: no line matched /$pat/"; fi
exit 0
