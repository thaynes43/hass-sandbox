#!/bin/bash
# Run one of the voice-bench scripts INSIDE the Home Assistant pod.
#   scripts/voice-bench/run.sh bench.py "MODE=voice REPS=3 PIPELINE=<pipeline id>"
#   scripts/voice-bench/run.sh bench.py "MODE=conv AGENT=conversation.bedroom_assist QUERIES='Is the fan on?|Why is the sky blue?'"
#   scripts/voice-bench/run.sh bench.py "MODE=pipe REPS=1 PIPELINE=<id> DEVICE_ID=<satellite device id> QUERIES='Play some jazz'"
#       # text AS that satellite (the agent is told its area); prints tool calls with their arguments. It really acts.
#   scripts/voice-bench/run.sh debug_runs.py "N=4 PIPELINE=<pipeline id>"   # real runs from a satellite
#   scripts/voice-bench/run.sh openai_agents.py                             # agent subentry settings + account model ids
#   scripts/voice-bench/run.sh persona_check.py                            # text-only persona + spoken-rules check, all four room agents
#   scripts/voice-bench/run.sh door_status_check.py                        # truth vs what the agents SAY about locks/garage (read-only)
#   scripts/voice-bench/run.sh mcp_oauth_setup.py "MCP_URL=<https://host/mcp> NAME=<name>"   # register HA as an OAuth client of an MCP server (no secrets printed)
# The HA token comes from the AppDaemon pod's env and travels over stdin: it never
# appears in argv, in the transcript, or on disk.
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
SCRIPT=${1:?script name}
ENVS=${2:-}
NS=home-automation
HA_TOKEN="$(kubectl exec -n $NS deploy/appdaemon -c app -- sh -c 'printf %s "$TOKEN"' 2>/dev/null)" || true
[ -n "$HA_TOKEN" ] || { echo "could not read the HA token from the appdaemon pod (is kubectl pointed at the right cluster, is the pod up?)" >&2; exit 1; }
{ printf '%s\n' "$HA_TOKEN"; cat "$DIR/$SCRIPT"; } | kubectl exec -i -n $NS deploy/home-assistant -c app -- \
  sh -c "read -r HA_TOKEN; export HA_TOKEN; cat > /tmp/vb.py; env $ENVS python3 /tmp/vb.py; rc=\$?; rm -f /tmp/vb.py; exit \$rc"
