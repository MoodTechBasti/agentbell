#!/usr/bin/env bash
# CI / VPS / remote-box example: notify via the webhook server.
# Start the server on the box once:  agentbell server
# Auth: set a token there with  agentbell config set webhook.token <random>
# and export the same value here as WEBHOOK_TOKEN. The server refuses to
# listen on a non-loopback address without a token.
set -euo pipefail

WEBHOOK="${WEBHOOK_URL:-http://127.0.0.1:8756}"
auth=()
if [ -n "${WEBHOOK_TOKEN:-}" ]; then
    auth=(-H "Authorization: Bearer $WEBHOOK_TOKEN")
fi

curl -fsS -X POST "$WEBHOOK/notify" ${auth[@]+"${auth[@]}"} \
    -H "Content-Type: application/json" \
    -d '{"message":"CI pipeline finished","title":"CI: my-project","priority":"normal","tags":"ci"}'

# Blocking approval from CI (agentbell server keeps the request open until you
# answer). /ask returns HTTP 200 for every answer - denied, timed out or free
# text alike - so `curl -f ... && deploy` is not a gate. Read the JSON and
# deploy only on "approved": true (curl -f still stops on a 4xx/5xx error):
#
# answer=$(curl -fsS -X POST "$WEBHOOK/ask" ${auth[@]+"${auth[@]}"} \
#     -H "Content-Type: application/json" \
#     -d '{"message":"Release to prod?","timeout_seconds":600}')
# if printf '%s' "$answer" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("approved") is True else 1)'; then
#     ./deploy.sh
# else
#     echo "release not approved: $answer" >&2
#     exit 1
# fi
