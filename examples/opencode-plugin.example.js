// What `agentbell hooks install opencode` writes to
//   ~/.config/opencode/plugin/agentbell.js   (global — applies in every repo)
// or .opencode/plugin/agentbell.js with --project.

// agentbell: phone notifications for OpenCode.
// Installed by `agentbell hooks install opencode`.
// Remove with   `agentbell hooks uninstall opencode`.
const BIN = "/home/you/.local/bin/agentbell"
const childSessions = new Set()
let lastPermission = 0

export const AgentBell = async ({ $ }) => {
  const fire = async (...args) => {
    // never let a notification failure break or slow down the session
    try {
      await $`${BIN} ${args}`.quiet().nothrow()
    } catch (_) {}
  }
  return {
    event: async ({ event }) => {
      const props = (event && event.properties) || {}
      // subagent sessions go idle too - they would notify twice
      if (event && event.type === "session.created" && props.info && props.info.parentID) {
        childSessions.add(props.info.id)
        return
      }
      if (props.sessionID && childSessions.has(props.sessionID)) return
      if (!event) return
      if (event.type === "session.idle") {
        await fire("hook", "run_completed", "--agent", "opencode")
      } else if (event.type === "session.error") {
        await fire("hook", "run_failed", "--agent", "opencode")
      } else if (event.type === "permission.asked" || event.type === "permission.updated") {
        // both names exist across versions; collapse them into one ping
        const now = Date.now()
        if (now - lastPermission < 3000) return
        lastPermission = now
        await fire("hook", "permission_required", "--agent", "opencode")
      }
    },
  }
}
