#!/usr/bin/env bash
# Custom agent / script notification example.
# Copy this pattern into any long-running script, CI job, or agent wrapper.
#
# `set -e` aborts the script the moment a command fails, so a `$?` check on the
# next line never runs. The failure branch has to be part of the `if` itself —
# that is the only shape in which both branches actually fire.
set -euo pipefail

run_my_agent() {
    # --- your long-running work here ---------------------------------------
    echo "doing hard work..."
    sleep 2
    # -----------------------------------------------------------------------
}

if run_my_agent; then
    agentbell notify "Job finished successfully" --priority normal --tags done
else
    status=$?
    agentbell notify "Job FAILED with exit code $status" --priority urgent --tags failed
    exit "$status"
fi

# Prefer `watch` when you only need to wrap one command — it sends both events,
# measures the duration, and passes the exit code through:
#
#   agentbell watch -- npm run build
#
# Approval gate for a real deploy. Exit 0 is not enough: a free-text reply
# is also exit 0, and `approved` is false then. `ask && ./deploy.sh` would
# ship on "later" or "nicht jetzt" whenever those words are not in the
# denial list. `--json` prints the verdict. Denied (1), timeout (2) and
# errors (3) abort before the deploy; only an explicit yes has
# `approved: true`.
#
#   answer=$(agentbell ask "Deploy to production?" --timeout 600 --json) || exit $?
#   printf '%s' "$answer" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("approved") is True else 1)'
#   ./deploy.sh
