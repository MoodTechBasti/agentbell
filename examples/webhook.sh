#!/usr/bin/env bash
# CI / VPS / remote-box example: notify via the webhook server.
# Start the server on the box once:  agentbell server
# (add "token" to webhook config and use -H "Authorization: Bearer <token>" for auth)
set -euo pipefail

WEBHOOK="${WEBHOOK_URL:-http://127.0.0.1:8756}"

curl -fsS -X POST "$WEBHOOK/notify" \
    -H "Content-Type: application/json" \
    -d '{"message":"CI pipeline finished","title":"CI: my-project","priority":"normal","tags":"ci"}'

# Blocking approval from CI (agentbell server keeps the request open until you answer):
# curl -fsS -X POST "$WEBHOOK/ask" \
#     -H "Content-Type: application/json" \
#     -d '{"message":"Release to prod?","timeout_seconds":600}'
