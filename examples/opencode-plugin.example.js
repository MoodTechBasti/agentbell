// What `agentbell hooks install opencode` writes to
//   ~/.config/opencode/plugin/agentbell.js   (global — applies in every repo)
// or .opencode/plugin/agentbell.js with --project.

// agentbell: phone notifications for OpenCode.
// Installed by `agentbell hooks install opencode`.
// Remove with   `agentbell hooks uninstall opencode`.
const BIN = "/home/you/.local/bin/agentbell"
const MIN_DURATION = 60   // seconds; shorter turns stay silent
const IDLE_DEDUPE_MS = 10000            // one turn end per session per 10 s
const childSessions = new Set()
const turnStarted = new Map()           // sessionID -> ms of the prompt that began the turn
const turnEnded = new Map()             // sessionID -> ms of the last idle, reported or not
const lastIdle = new Map()              // sessionID -> ms of the last turn end reported
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
      if (!event) return
      const props = event.properties || {}
      const info = props.info || {}
      const sid = props.sessionID || info.sessionID
      // subagent sessions go idle too - they would notify twice
      if (event.type === "session.created" && info.parentID) {
        childSessions.add(info.id)
        return
      }
      if (sid && childSessions.has(sid)) return
      if (event.type === "message.updated") {
        // the user's prompt starts the turn; assistant updates stream all turn long.
        // A prompt created before the last idle is the previous turn's, re-sent.
        const created = info.time && info.time.created
        const resent = typeof created === "number" && created <= (turnEnded.get(sid) || 0)
        if (info.role === "user" && sid && !resent && !turnStarted.has(sid)) {
          turnStarted.set(sid, Date.now())
        }
        return
      }
      if (event.type === "session.idle") {
        const now = Date.now()
        const started = sid ? turnStarted.get(sid) : undefined
        if (sid) {
          turnStarted.delete(sid)
          if (turnEnded.size > 500) turnEnded.clear()
          turnEnded.set(sid, now)
        }
        // the same session can report idle twice for one turn - one push, not two
        if (sid && now - (lastIdle.get(sid) || 0) < IDLE_DEDUPE_MS) return
        if (sid) {
          if (lastIdle.size > 500) lastIdle.clear()
          lastIdle.set(sid, now)
        }
        const args = ["hook", "run_completed", "--agent", "opencode"]
        if (started) {
          args.push("--duration", String(Math.round((now - started) / 1000)),
                    "--min-duration", String(MIN_DURATION))
        }
        await fire(...args)
      } else if (event.type === "session.error") {
        if (sid) turnStarted.delete(sid)
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
