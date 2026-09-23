#!/usr/bin/env bash
# Run any command and get notified on completion - with exit code and duration.
#
#   ./examples/watch.sh npm run build
#   ./examples/watch.sh sleep 300
#
# Pass the command as separate words, not as one quoted string: agentbell
# runs it directly, without a shell, so "npm run build" in quotes is looked
# up as a program literally named `npm run build` and fails with exit 127.
# For pipes, redirects or `&&`, hand the line to a shell yourself:
#
#   ./examples/watch.sh sh -c 'make && make test'
#
# Priority follows the exit code: success -> normal, failure -> urgent.
# The command's own exit code is passed through, so scripts can react to it.

exec agentbell watch -- "$@"
