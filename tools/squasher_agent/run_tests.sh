#!/usr/bin/env bash
# pytest for the squasher agent, with the environment EMPTIED and the
# repo's no-network harness on. The agent's own process holds
# ANTHROPIC_API_KEY; a test it writes must not be able to read or send it.
# Usage: tools/squasher_agent/run_tests.sh tests/test_x.py [more pytest args]
set -u
root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root" || exit 2
for a in "$@"; do
  case "$a" in
    -p|--pyargs|--rootdir*|--confcutdir*|-c|--override-ini*) echo "run_tests.sh: refused flag $a" >&2; exit 2 ;;
  esac
done
exec env -i PATH="$PATH" HOME="${RUNNER_TEMP:-/tmp}/agent-home" LANG=C.UTF-8 \
  PYTHONPATH="$root/tests/_no_network" DCHUB_NO_NETWORK_LOG="${RUNNER_TEMP:-/tmp}/agent-no-network.jsonl" \
  python3 -m pytest -q --tb=short -p no:cacheprovider "$@"
