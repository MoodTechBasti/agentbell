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
# Approval gate: ask before a risky step, wait up to 10 minutes.
# `ask` exits 0 = approved or answered, 1 = denied, 2 = timeout, 3 = error.
#
#   if agentbell ask "Deploy to production?" --timeout 600; then
#       ./deploy.sh
#   fi
