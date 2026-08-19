#!/usr/bin/env bash
# Run any command and get notified on completion - with exit code and duration.
#
#   ./examples/watch.sh "npm run build"
#   ./examples/watch.sh "sleep 300"
#
# Priority follows the exit code: success -> normal, failure -> urgent.
# The command's own exit code is passed through, so scripts can react to it.

exec agentbell watch -- "$@"
