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

// OpenCode 1.x sends { type, properties }, 2.x sends { type, data }. Reading
// both here keeps one handler for either host.
const payloadOf = (event) => event.data || event.properties || {}

const handleEvent = async (event, fire) => {
  if (!event) return
  const payload = payloadOf(event)
  const info = payload.info || {}
  const sid = payload.sessionID || info.sessionID
  // subagent sessions go idle too - they would notify twice
  if (event.type === "session.created") {
    // 2.x carries parentID on the payload, 1.x under info; the new session's
    // id is the payload/session id in either shape, and only the 1.x form
    // needs info.id as a fallback (in message.updated info.id is the message).
    if (payload.parentID || info.parentID) {
      const child = sid || info.id
      if (child) childSessions.add(child)
      return
    }
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
  if (event.type === "session.execution.succeeded" || event.type === "session.idle") {
    const now = Date.now()
    const started = sid ? turnStarted.get(sid) : undefined
    if (sid) {
      turnStarted.delete(sid)
      if (turnEnded.size > 500) turnEnded.clear()
      turnEnded.set(sid, now)
    }
    // the same session can report the end of its turn twice - one push, not two
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
  } else if (event.type === "session.execution.failed" || event.type === "session.error") {
    // the turn is over either way; remembering when keeps a prompt OpenCode
    // re-sends afterwards from being read as the start of the next turn
    if (sid) {
      turnStarted.delete(sid)
      if (turnEnded.size > 500) turnEnded.clear()
      turnEnded.set(sid, Date.now())
    }
    await fire("hook", "run_failed", "--agent", "opencode")
  } else if (event.type === "session.execution.interrupted") {
    // the turn was cut short - by the user, or by the host (shutdown,
    // superseded by a new prompt, inactivity). Nothing is pushed for it; the
    // timestamp only keeps the next turn from inheriting this start time.
    if (sid) {
      turnStarted.delete(sid)
      if (turnEnded.size > 500) turnEnded.clear()
      turnEnded.set(sid, Date.now())
    }
  } else if (event.type === "permission.asked" || event.type === "permission.updated") {
    // 2.x has permission.asked only; 1.x also sent permission.updated
    const now = Date.now()
    if (now - lastPermission < 3000) return
    lastPermission = now
    await fire("hook", "permission_required", "--agent", "opencode")
  }
}

export const AgentBell = async ({ $ }) => {
  const fire = async (...args) => {
    // never let a notification failure break or slow down the session
    try {
      await $`${BIN} ${args}`.quiet().nothrow()
    } catch (_) {}
  }
  return { event: async ({ event }) => handleEvent(event, fire) }
}

export default {
  id: "agentbell",
  // 1.x reads exactly one of these from `default` and returns before it ever
  // looks at the other exports; its reader treats an object that has `id` but
  // no `server` as a broken module and throws. 2.x decodes the same object and
  // ignores every key except `id` and `setup`.
  server: AgentBell,
  setup(ctx) {
    // 2.x hands the plugin a context with an event stream; a host that calls
    // setup without one is a no-op rather than a failed plugin load
    if (!ctx || !ctx.event || typeof ctx.event.subscribe !== "function") return
    const controller = new AbortController()
    const fire = async (...args) => {
      try {
        const command = Array.isArray(BIN) ? BIN : [BIN]
        const child = Bun.spawn([...command, ...args], { stdout: "ignore", stderr: "ignore" })
        await child.exited
      } catch (_) {}
    }
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          await handleEvent(event, fire)
        }
      } catch (_) {}
    })()
    return () => controller.abort()
  },
}
