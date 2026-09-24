#!/usr/bin/env bash
# Custom agent / script notification example.
# Copy this pattern into any long-running script, CI job, or agent wrapper.
#
# The work runs in its own subshell with `set -e`, and the script reads its
# exit status afterwards. Do not call it as `if run_my_agent; then` or
# `run_my_agent || ...`: bash ignores `set -e` inside a command that is an
# `if` condition or the left side of `||` / `&&`, so a failing step would not
# stop the work and the job would report success. That is also why this
# script itself does not use `set -e`.
set -uo pipefail

run_my_agent() (
    set -e
    # --- your long-running work here ---------------------------------------
    echo "doing hard work..."
    sleep 2
    # -----------------------------------------------------------------------
)

run_my_agent
status=$?

# A failed notification (exit 3: it could not be sent) must not change the
# job's own result, so its exit code is only reported, never passed on.
if [ "$status" -eq 0 ]; then
    agentbell notify "Job finished successfully" --priority normal --tags "done" ||
        echo "agentbell notify failed (exit $?)" >&2
else
    agentbell notify "Job FAILED with exit code $status" --priority urgent --tags failed ||
        echo "agentbell notify failed (exit $?)" >&2
fi

# Prefer `watch` when you only need to wrap one command - it sends both events,
# measures the duration, and passes the exit code through:
#
#   agentbell watch -- npm run build
#
# Approval gate for a real deploy. Exit 0 is not enough: `ask` exits 0 for
# every reply it does not read as a denial, including typed free text such
# as "yes, but use staging". Typed free text never approves - `approved` is
# false then - and the denial list cannot know every way of saying no. So
# `ask && ./deploy.sh` is not a gate. Denied (1), timeout (2) and errors (3)
# stop at the first line; after that, only `approved: true` in the `--json`
# output lets the deploy run. The check is an explicit `if`, so it holds
# with or without `set -e`.
#
#   answer=$(agentbell ask "Deploy to production?" --timeout 600 --json) || exit $?
#   if printf '%s' "$answer" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("approved") is True else 1)'; then
#       ./deploy.sh
#   else
#       echo "deploy not approved: $answer" >&2
#       exit 1
#   fi

exit "$status"
