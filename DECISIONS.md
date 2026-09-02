# Design Decisions & Rationale

## 1. Language & packaging: Python stdlib, single file

**Decision:** Python 3.9+, one file, zero third-party dependencies.

**Why:**
- Must install with one command on laptop *and* VPS. A stdlib-only single file works with pipx, `pip --user`, or a bare copy to `~/.local/bin` — no build, no venv, no lockfiles.
- Python 3.9+ is preinstalled on virtually every Linux/macOS box (unlike Go toolchains or Node).
- The feature set is small by design (HTTP POST + JSON + a webhook server); stdlib `urllib`/`http.server` covers it without pulling `requests`.

**Trade-off:** no async/HTTP2 niceties. Irrelevant at this scale.

## 2. Channel order: ntfy first, Telegram premium, OS fallback

**Decision:** ntfy is the default free channel; OS notifications are a free fallback; Telegram is a **premium feature** gated by a lifetime license key.

**Why:**
- ntfy: no account, works on iOS + Android, self-hostable, action buttons, free-text replies — it uniquely supports the *approval flow without a public server* (see §4). Telegram cannot do this without a bot daemon.
- Opt-in matters more than reach: the tool never spams a channel the user didn't configure.
- The product direction is fixed on this point: the free core has to be completely usable without paying anything. Telegram and parallel delivery are a separable layer on top, not a piece cut out of the core.

## 2b. Premium licensing: offline Ed25519 lifetime keys

**Decision:** license keys of the form `AB1-<base32(payload)>-<base32(signature)>`, payload `agentbell|customer|expiry`, signature = Ed25519 over the payload bytes. Offline-checkable, no phone-home, no subscription. `tools/make-license.py` mints keys (author-side only, **not in the public repo**); `agentbell license activate <key>` verifies and stores. Premium gate = `send_notification` refuses the `telegram` channel without a valid key (clear error message), plus the init wizard's license flow.

**Key security:** the split is asymmetric on purpose. `agentbell.py` contains `LICENSE_PUBLIC_KEY` — 64 public hex characters — and nothing else; the 32-byte private seed lives only in the git-ignored `.license-secret` (mode 0600) on my machine. Verification reads that constant and nothing else: there is deliberately no env var, config entry or file that can point it at another key, so nobody can announce their own key pair to the process and unlock the paid tier (§12i, tested).

**Why it replaced HMAC** (the v1.0–v1.4 scheme, symmetric, with the secret injected into the shipped copy by `tools/build.py`): the plan is a single-file `.py` on PyPI, and a symmetric secret inside a shipped file is a secret in public — `pip download` plus `grep` and anyone could mint keys for everyone. Ed25519 removes the secret from every artifact, and with it the entire build step. Three things fell out of that, all improvements:

- **Every copy verifies keys.** Under HMAC, a checkout run straight from `python3 agentbell.py` had no secret and rejected every real key, so `doctor`, `license activate` and the init wizard each needed a branch explaining "this build cannot verify keys". All gone: one code path, one message.
- **`install.sh` installs the source file.** No staging directory, no injected copy — pipx / `pip --user` / plain copy, all from the checkout.
- **Keys are unforgeable.** Under HMAC, extracting the secret from an author-built binary produced a working key generator. Forging an Ed25519 key means breaking Ed25519.

**Implementation:** RFC 8032 in ~150 lines of stdlib Python (`hashlib.sha512` + integer arithmetic, extended coordinates) inside `agentbell.py`, covered by the RFC's own §7.1 test vectors. Signing lives in the same file: it is inert without the private seed, and the tests and the minting tool import it from there. Verification is a few milliseconds of big-int math but `premium_enabled()` runs on every send, so results are memoized per process.

**Trade-off (unchanged and accepted):** the free-vs-paid split lives in the same MIT codebase, and a fork can delete the gate — it is deliberately one line deep so it stays trivial to audit (see `premium_enabled`/`send_notification`). What changed is the other half of the threat model: the *keys* can no longer be forged, and no shipped artifact contains anything that helps. No license server = zero ops and zero privacy concerns.

## 3. Hook strategy per agent

**Decision:** use each agent's native hook system where one exists; use prompt/rule injection where it doesn't.

| Agent | Mechanism | Why |
|-------|-----------|-----|
| Claude Code | `~/.claude/settings.json` `hooks` (`UserPromptSubmit`, `Stop`, `StopFailure`, `Notification(agent_needs_input)`), `async: true` | Real shell hooks, documented; event names verified against Claude Code 2.1.232. Async so network latency never blocks a turn. `UserPromptSubmit` only writes a start marker (`--silent`) so the completion push can report the turn duration. |
| Codex | `~/.codex/config.toml` `hooks.UserPromptSubmit` + `hooks.Stop` | Codex supports lifecycle hooks in config.toml (events verified against Codex 0.147.0). We append TOML array-of-tables blocks guarded by markers so user config is never rewritten. Hooks are enabled by default; `features.hooks = true` is written only when it cannot conflict with an existing `[features]` table, and the note now says so instead of claiming hooks are off. `SessionEnd` was considered but dropped: it fires on every exit (including `/clear`) and would spam. |
| Gemini CLI | `~/.gemini/settings.json` `hooks.AfterAgent` | Fires once per turn after the final response — the correct "done" event. No failure event exists; documented. |
| OpenCode | plugin in `~/.config/opencode/plugin/agentbell.js` (**v1.3.0**, was an `AGENTS.md` block) | OpenCode has a real plugin bus (`event` hook). Deterministic beats "please call the CLI": `session.idle` → run_completed, `session.error` → run_failed, `permission.asked` → permission_required. Verified against OpenCode 1.18.18. |
| Kimi Code | `~/.kimi-code/config.toml` `[[hooks]]` (v1.4.0) | Real lifecycle hooks (`UserPromptSubmit`/`Stop`/`StopFailure`), event names re-verified against moonshotai.github.io/kimi-code (2026-08; the kimi-cli docs are being wound down in favor of Kimi Code CLI). Kimi only accepts the four fields `event`/`matcher`/`command`/`timeout` in a `[[hooks]]` table — anything else (e.g. `async`) makes it refuse to load the whole config, so the block is kept to exactly those keys. |
| Qwen Code | `~/.qwen/settings.json` `hooks` (v1.4.0) | Speaks Claude's hooks.json format (`UserPromptSubmit`/`Stop`/`StopFailure`, no matcher on these events). Command hooks support `async: true` (re-verified against qwenlm.github.io/qwen-code-docs, 2026-08), which is used so a notification send never blocks the end of a turn. Hooks are enabled by default; `disableAllHooks: true` disables them. |
| Cursor | `.cursor/rules/agentbell.mdc` (alwaysApply rule) | Cursor has no lifecycle shell hooks. A rule that instructs the agent to call the CLI is the standard, reliable mechanism. This is the one file we own outright, so it is written verbatim — a `.mdc` must begin with its YAML frontmatter, and the marker-block wrapper used for shared files broke it (fixed in v1.3.0). Format re-verified against cursor.com/docs/context/rules (2026-08): `.mdc` + `alwaysApply: true`. |
| Windsurf | `.windsurf/rules/agentbell.md` + legacy `.mdc` (v1.4.0, fixed v1.4.1) | Windsurf/Devin Desktop's rule engine changed: current builds read `.windsurf/rules/*.md` (or `.devin/rules/*.md`, preferred) with a `trigger: always_on` frontmatter (docs.windsurf.com/windsurf/cascade/memories, 2026-08); pre-Devin builds only knew Cursor-style `.mdc`. One install writes **both** files so every build picks the rule up; uninstall removes only files we own. |
| Cline | `.clinerules/agentbell.md` (v1.4.0) | `clinerules/` directory, markdown rules — Cline processes every `.md`/`.txt` in it. |
| Continue | `.continue/rules/agentbell.md` (v1.4.0) | `rules/` directory inside the repo `.continue/`, markdown. |
| Zed | `.rules` block at repo root (v1.4.0) | Zed reads exactly one file per repo: `.rules` > `.cursorrules` > `.windsurfrules` > `.clinerules` > `.github/copilot-instructions.md` > `AGENT.md` > `AGENTS.md` > `CLAUDE.md` > `GEMINI.md`. Writing `.rules` is the highest-priority single-file option; our marker block is appended so a hand-written `.rules` keeps its own content. |
| Aider | `AGENTS.md` block at repo root (v1.4.0) | Aider auto-reads `AGENTS.md` (since v0.69) — no config change needed. `CONVENTIONS.md` would require a `read:` line in `.aider.conf.yml`, so `AGENTS.md` was chosen. Status is marked by the `--agent aider` command, so a legacy OpenCode `AGENTS.md` block does not count as installed. |

All installers merge JSON (preserving user keys), append marked TOML/markdown blocks, are idempotent, and `uninstall` removes exactly what was added (identified by the `agentbell hook` command marker / comment blocks). Since v1.4.0 they all live in one `AGENT_SPECS` registry (detect / install / status) that drives `find_agents()`, `install_hooks()`, `hooks_status()` and the purge scan.

**Decision:** hooks call `agentbell hook <event>` (silent, fast, 5s network timeout, never fails the agent) rather than `notify` — hooks must not print to stdout or block agent turns.

**Decision:** the canonical event set is fixed — `run_completed`, `run_failed`, `input_required`, `permission_required`, `started` — with short aliases (`done`, `failed`, `needs-input`, …) kept for convenience. `permission_required` exists as an event (CLI + MCP + custom scripts) but is *not* wired into agent hooks by default: per-turn permission prompts would spam the phone (opt-in principle).

## 4. Approval flow: ntfy response-topic roundtrip (no public server)

**Decision:** `ask` publishes the question to the main topic with ntfy **action buttons** whose HTTP action POSTs the answer to a dedicated `<topic>-responses` topic. `ask` waits for the first message on that topic using a **hybrid receiver**: a live JSON stream (fast path) plus short `poll=1&since=<ts>` requests every ~4s (fallback), deduplicated by message id. Exit 0/1/2 = approved/denied/timeout; free-text replies are passed through as the answer.

**Why the polling fallback:** ntfy's long-lived streams are the right mechanism, but in practice fan-out to subscribers can stall (observed on ntfy.sh). Polling is immune to stream buffering and adds negligible load for a single-user tool; the dedupe set keeps both paths safe.

**Why not a webhook server as the answer receiver?** It would require a public IP/port-forward/Tailscale and a daemon. ntfy is already the delivery channel — reusing it as the *response channel* means the approval flow works from the same laptop/phone pairing with zero extra infra. The mobile app's "reply" box and action buttons both publish to topics, so users get both buttons and free-text.

**Why a dedicated responses topic instead of the main topic?** The main topic carries other traffic (every hook event); a dedicated topic makes "first message = answer" unambiguous. Cost: user subscribes to two topics in the app (the wizard prints both).

**Security note:** on public ntfy.sh, anyone who guesses the topic name can publish to it (both main and responses). For sensitive approvals, self-host ntfy with auth — publish/subscribe/actions all carry the configured Basic auth header. Since v1.2 the wizard suggests a 128-bit random topic by default (plus the username prefix), which makes guessing impractical; the README still recommends self-hosted ntfy + auth for sensitive approvals.

### 4b. Request-ID model (v1.2)

**Decision:** every `ask` carries a 64-bit random approval id (`secrets.token_hex(8)`). ntfy button bodies and Telegram `callback_data` embed it (`APPROVED <id>` / `agentbell|<id>|approved`). Both answer paths bind answers to that id: button answers with a non-matching or unknown id are ignored and logged (`stale_answer` history events; Telegram answers expired callbacks politely). Free-text replies cannot carry an id on either platform, so they are attributed **deterministically to the newest open ask**: each waiting `ask` registers itself in `<state>/ntfy-pending`/`tg-pending` before publishing and unregisters when done; a waiter only accepts free text if it is the newest unexpired entry.

**Why:** parallel asks on the shared `<topic>-responses` topic previously risked cross-talk (both waiters saw every message). ID-bound buttons + newest-wins free text make every outcome unambiguous without changing the single-ask UX.

**Trade-off:** free text arriving after a newer ask opened goes to the newer ask even if the user meant the older one — the same rule Telegram flows use, documented in README. Sequential asks are unaffected (only one pending entry exists).

**Trade-off:** ntfy cannot carry the Telegram flow without a long-running bot daemon (callbacks are server-side). Telegram approvals are implemented since v1.1 as an opt-in long-polling daemon — see §9.

## 5. Quiet hours & priority semantics

**Decision:** quiet windows are time ranges (`22:00-07:30`, overnight wrap supported); during them, notifications below `quiet_hours_min_priority` (default `normal`) are handled per `quiet_hours_mode`: **`suppress`** (default, drop + history) or **`defer`** (store in `<state>/deferred/` and deliver after the window ends). `--force` bypasses both modes; `--defer` on `notify` defers a single call even in suppress mode; `ask` always publishes at high priority and is **never** deferred or suppressed — approvals must not be swallowed.

**Defer delivery:** deferred items are delivered by the next notification activity after the window ends (`notify`/hook/`watch`/webhook), by `agentbell queue flush`, or immediately by the bot daemon if it runs. More than 3 due items are **bundled into one summary notification** ("N deferred notifications") to avoid an inbox flood at 07:30; up to 3 are replayed individually. If quiet hours are still active at flush time (e.g. the window moved), items are re-deferred instead of delivered. A transient delivery failure moves the item into the offline queue instead of dropping it. The deferred store is capped at 200 items (drop-oldest + history).

**Why defer over "always drop"?** v1 dropped + logged, which silently lost low-priority events the user wanted to see in the morning. Defer keeps the promise of the notification while respecting "opt-in, not auto-spam": delivery only ever happens outside quiet hours and is bundle-limited. Suppress remains the default because deferral adds state and surprise; users opt in explicitly (wizard question, `--quiet-hours-mode defer`).

## 5b. Retry & offline queue

**Decision:** publish failures are classified (`TransientError`: connection errors, timeouts, 5xx/408/429; `PermanentError`: 4xx, bad config, premium gate). Transient failures are retried 3 times with 1s/2s backoff inside the same call. If the channel is still down, the notification is **queued** in `<state>/queue/` (one JSON file per item) and delivered later — the v1.1 failure mode (silent loss) is gone.

**Queue limits:** max 100 items (drop-oldest + `queue_overflow` history), max age 24 h (`queue_expired`), permanent failures on replay are dropped + logged (`queue_dropped`), transient ones stay with an attempt counter (`kept`). Queued `ask` questions are pointless (the answer window closes), so `ask` never queues — it retries and then fails loudly (parallel-channel asks still let the healthy channel decide).

**Drain triggers (no hidden daemon):** (1) `agentbell queue flush` (also flushes deferred items), (2) automatically after any successful send — bounded to 2 queue items so a normal `notify` stays fast, (3) the opt-in bot daemon drains the whole queue + deferred store every poll cycle. Items are claimed atomically (`.sending` rename) so concurrent processes never double-deliver.

**Semantics:** `notify` exits 0 when a message is queued (it is not lost — just delayed) and prints a stderr warning; history records `queued`/`queued_delivered`. Permanent failures still exit 3 as in v1.1.

## 6. One tool, three surfaces: CLI / MCP / webhook

**Decision:** one codebase, three entry points sharing the same send/ask core.

- **CLI** — human + scripts + agent hooks.
- **MCP** (stdio, JSON-RPC 2.0, `notify` + `ask_approval`) — agents that prefer tool calls over shell (`mcp add <agent>` registers it in each agent's config).
- **Webhook** (`agentbell server`, 127.0.0.1 by default, optional bearer token) — remote scripts, CI, VPS without the CLI. `/ask` blocks server-side until approval.

**Why:** each surface maps to one real user pain; the shared core keeps the whole thing in one ~5400-line file.

## 7. Deliberately not implemented (yet)

1. **Windows-native hooks.** The CLI runs on Windows; agent hook configs are generated for the agents' own shells. First-class Windows support is possible but wasn't the focus.
2. **Auto-update / prebuilt binaries / custom sounds** — plausible premium extras later, deliberately not built now.
3. **Multi-user / team topics, dashboards, rate limiting** — out of scope for a personal tool.
4. **Cursor global rules.** Cursor stores global rules in its own DB; we install project-level rules (documented). 
5. **Per-event channel routing, reply-to on plain notifications** — explicitly excluded by this iteration's goals.
6. **Agent remote control via chat** (arbitrary Telegram commands, reply-to as a command system) — v1.2's request IDs answer *approvals*, they deliberately do not become a general command surface. See §10.
7. **A background auto-drainer for free users.** Queue/defer delivery is opportunistic (next notify/hook/`queue flush`), never a hidden daemon. The known opt-in `bot` daemon drains when it runs.
8. **Retry delivery guarantees beyond best-effort.** Retries are bounded and the queue is local; there is no at-most-once guarantee (a timed-out POST may still have been delivered, so a rare duplicate is possible on retry — the request id stays the same, so approval answers are deduplicated).

## 7b. Single-file architecture: current state and migration strategy

**Current state:** `agentbell.py` (~5900 lines, ~243 KB) plus `tests/test_agentbell.py` (~3300 lines, ~149 KB). The build step is `py_compile`, nothing else. Single-file distribution is the product identity — a user copies one file and runs it.

**When to split:** when the file size actively harms development velocity: navigation takes multiple searches, parallel work collides on the same file, code review diffs are unbounded by component, or new contributors spend more time scrolling than reasoning.

**Migration strategy (documented, not implemented):**

1. **Source layout** — move components to `src/agentbell/` modules matching logical boundaries already visible in the code (config, ntfy, telegram, approvals, hooks, mcp, queue, doctor, cli), keeping `agentbell.py` as a thin re-export shim during the transition.

2. **Build step** — a deterministic concatenator (`tools/build.py` or equivalent, stdlib-only) produces the single-file distribution from the module sources. The concatenator runs as part of the release workflow; the distributed artifact is byte-for-byte identical to what CI validates.

3. **Compat guarantee** — the single-file output must be drop-in compatible with every existing hook command, MCP registration, `install.sh` path, `pipx install`, and standalone copy. The module structure is a development convenience, never a runtime requirement.

4. **When to trigger:** not now. The file is maintainable today. Trigger when:
   - 2+ contributors collide on the same file in every sprint
   - Navigation/search overhead demonstrably slows bug fixes
   - A new integration (agent #13+) would add >200 lines of hook logic

5. **Rollback:** the concatenator is the safety net — if module structure proves more overhead than benefit, delete `src/` and keep the concatenated file as the canonical source. No data loss.

## 8. What I'd do next (prioritized)

v1.5.0 is the first public release; the 2-week field test (started on v1.3.1) continues against it. In order:

1. **Finish the field test** — `FIELD_TEST.md` is the checklist. Whatever it surfaces gets fixed first; that is the gate, not a feature count.
2. **End-to-end hook smoke tests** against the real agents' configs (CI matrix). §15 re-verified all twelve integrations by hand against the vendors' live docs — worth doing once, too expensive to repeat every release.
3. **Windows installer + PowerShell notification support**.
4. **Template engine** for notification bodies (turn path, duration, last-line summary from agent stdin JSON).
5. **Shell completions** for bash/zsh/fish.

## 9. Telegram interactive approval: answer daemon + file handoff (v1.1)

**Decision:** Telegram approvals are a **premium** feature implemented as an opt-in long-polling daemon (`agentbell bot`), stdlib-only. `ask` publishes the question with an inline keyboard (Approve/Deny); the daemon picks up the `callback_query` via `getUpdates`, writes the answer to `<state>/tg-answers/<approval-id>.json`, and `ask` polls that file. A pid lockfile (`<state>/bot.lock`) guarantees a single poller.

**Why long polling instead of a webhook daemon?** Telegram's webhook model requires a publicly reachable HTTPS endpoint (tunnel/port-forward/VPS) — exactly the infra the product avoids (see §4). Long polling needs only outbound HTTPS, runs on the same laptop/VPS as the CLI, and keeps the daemon stdlib-only (`urllib` + `json`). The free ntfy flow is untouched; the daemon is opt-in and the systemd unit example lives in `examples/`.

**Why file-based answer handoff (not ports/sockets)?** The daemon and the waiting `ask` process are separate processes. Files in the state dir need no port management, survive daemon restarts, are trivially debuggable, and are atomic enough for a single-user tool at this scale.

**Button availability:** `ask` attaches the inline keyboard only when the daemon's heartbeat (`<state>/bot.json`, refreshed every poll cycle) is fresh (≤60s). Otherwise the question goes out as plain text with a hint — no dead buttons. A button pressed with the daemon down still reaches it if it starts within Telegram's ~24h update retention.

**Free-text replies:** attributed to the *newest* unexpired pending ask (pending markers written by `ask` before publishing, removed on completion). Callback buttons are unambiguous (approval id in `callback_data`).

**Parallel channel semantics (ntfy + Telegram):** both channels receive the question at once; the **first answer wins** and the others are stopped; the timeout is shared. The deciding channel is recorded in the JSON output and history. A publish failure on one channel never aborts the ask as long as another channel could still decide (single-channel asks still fail hard on publish errors). If the daemon is down, ntfy still decides. On the ntfy side, button answers carry the approval id and stale ones (from an earlier ask) are filtered out by the waiter; free-text is accepted as before (v1.0 semantics).

**Premium gate:** `ask --channel telegram`, the derived-config telegram path, and `agentbell bot` all require a valid license; without one, derived ask silently degrades to ntfy (warning on stderr), explicit `--channel telegram` fails with an error. The gate stays the same one-liner pattern as the delivery channel (§2b).

**`agentbell watch`:** runs the command via `subprocess.run`, notifies on completion with exit code + duration (`format_duration`: `12s`, `4m12s`, `1h05m`), success → `normal`, failure → `urgent` (overridable via `--priority`/`--fail-priority`). The command's exit code is passed through (127 if it cannot be started); notification failures are printed to stderr but never change the exit code.

**Run durations in hook events:** `hook started` writes a start marker (`<state>/runs/<agent>.json`); `run_completed`/`run_failed` consume it and append the duration. `--silent` on `started` records the marker without sending a notification, so agents can be wired with zero noise. `--duration <seconds>` allows explicit values from scripts that measure themselves. Markers expire after 24h; without a marker no duration is appended (no regression).

### 9b. Bot robustness (v1.2)

**Decision:** `<state>/bot.json` now carries pid, heartbeat, `started_at`, and `last_error`/`last_error_ts` (set on poll failures, cleared on success). `bot status` derives: running/stale/dead from heartbeat age + pid liveness, lock state (`held`/`stale` with the owning pid — a stale lock after a crash is reclaimable, as before), the last known error cause (e.g. "webhook active" on getUpdates 409), open approval count, and queue/deferred counts. The heartbeat is refreshed before **and** after every poll cycle so a long `getUpdates` call can never look dead; the 60s freshness window that decides button availability is unchanged (no dead-button regression). The daemon also drains the offline queue and deferred store each cycle — it is the only always-on drain trigger, and it remains strictly opt-in.

**Why:** "buttons dead without explanation" was the v1.1 pain; status visibility + last-error surfacing + dual heartbeat fixes the diagnosability without new dependencies or a second daemon.

## 10. v1.2 scope: deliberately not built

- **No chat remote control.** Request IDs exist to bind *answers* to *questions*, not to route arbitrary commands. No agent command language over Telegram/ntfy.
- **No per-event channel routing matrix** and no reply-to on plain notifications.
- **No background daemon for free users.** Defer/queue delivery rides on activity + `queue flush`; only the known premium `bot` drains continuously.
- **No multi-device sync / cloud backend / CRDT** — the queue is local, single-user, bounded.
- **Bundle format is simple**: one summary message with timestamps; no digests, collapsing by event type, or per-day grouping. Good enough at 3-item granularity.

## 11. v1.3 RC: field-test readiness (queue list + uninstall)

**Decision:** v1.3.0rc1 is the field-test build line. Two additions, no new feature scope:

### 11a. `queue list`

**Decision:** `agentbell queue list` renders the queue and the deferred store as two tables (age, priority, channels, retry count / due-in, message), oldest first, plus `--json`. `queue status` stays as the short count view; `queue flush` unchanged.

**Why:** before this, queued/deferred data lived only as files in the state dir — a daily-use visibility gap (§8 formerly listed this). The table answers "what is waiting and how old is it" without building a dashboard. Formatting uses a new `format_age` (45s/12m/3h/2d) to stay compact.

### 11b. `uninstall` (purge)

**Decision:** one command, `agentbell uninstall`, is the complete removal path:

- **Default is a dry run**: it lists every found artifact (kind, path, what would happen) and deletes nothing. Deletion requires the explicit `--yes` flag. No hidden destructive behavior.
- **Scope** (grown with the agent list; current as of v1.4.1): the CLI entry (pipx package, pip --user script + dist-info, or standalone copy — detected per install path, files verified to be ours before deletion), the config dir (incl. license key), the state dir (history, queue, deferred, bot.json, bot.lock, run markers, pending-ask dirs, tg-answers), the hooks of **all twelve supported agents** — the global configs of Claude Code, Codex, Gemini CLI, Kimi Code, Qwen Code and OpenCode, plus the project rule files of Cursor, Windsurf, Cline, Continue, Zed and Aider (`--project`, default `.`) — and the MCP registrations this tool wrote, scanning **nine client config families** (Claude Code, Claude Desktop, Gemini, Qwen Code, Kimi Code, Cursor, VS Code, OpenCode, Codex; global *and* project paths where a client has both). That covers all ten `mcp add` client names: ChatGPT Desktop shares Codex's `~/.codex/config.toml`, so removing the Codex entry removes it too.
- **Own-markers-only rule**: hooks/MCP removal operates exactly on the markers this tool wrote (`agentbell hook` commands, the TOML comment block, `<!-- agentbell:start -->`, the `agentbell` MCP keys). User hooks, foreign MCP servers and unrelated config keys are never touched; files are only deleted when they consist solely of our block.
- **Explicitly not removed** (printed after every run): the ntfy app subscription on the phone, a Telegram bot at BotFather, `AGENTBELL_*` env vars in shell rc files. A *running* bot daemon gets a warning instead of a kill; it would recreate state files.
- **Env overrides are honored** (paths come from `AGENTBELL_CONFIG_DIR`/`AGENTBELL_CONFIG`/`AGENTBELL_STATE_DIR` when set), with a warning that the env vars themselves are not unset.

**Why one command with one flag:** five hidden scripts or a `purge`/`reset`/`uninstall` zoo would violate "thin". A dry-run-first single command makes the 2-week test's reset step safe and re-runnable; after `--yes` + re-install, `init` behaves exactly like a new user (verified in tests and a scripted purge→re-init smoke test).

**Trade-off:** detection of pipx/pip-user installs is best-effort (subprocess-based); a user-installed copy at an unusual location is listed in the dry run before anything happens, and the report is the safety net.

## 12. v1.3.0: field-test hardening

Everything here came out of an adversarial audit of the RC plus end-to-end runs against the *real* Claude Code 2.1.232, Codex 0.147.0 and OpenCode 1.18.18 on a test machine.

### 12a. `agentbell mcp` was dead on arrival (critical)

**Bug:** `mcp add` writes registrations that launch `<binary> mcp`, but the bare `mcp` subcommand had no `func` and crashed with an `AttributeError`. Every MCP registration this tool ever wrote pointed at a command that could not start.

**Decision:** the bare subcommand *is* the server (`p_mcp.set_defaults(func=cmd_mcp, sub=None)`), and a test asserts that the exact command written by `mcp add` dispatches. `main()` now reports "needs a subcommand" instead of raising for any parser without a `func`.

### 12b. Stale answers could decide a new question

**Bug:** ntfy's `since` cursor has 1-second granularity, so an answer published for a *previous* ask could still be inside the window a new ask opens — reproduced end to end: an `ask` returned the previous ask's free-text answer instantly instead of waiting.

**Decision:** `ApprovalWaiter.start()` primes its `seen` set with every message already on the response topic (one `poll=1` request), so only messages that arrive *after* the question can answer it. Exact (id-based), one extra request, no behavior change for the normal path.

### 12c. Which MCP clients we register (desktop market)

**Decision:** `mcp add` takes explicit client names and defaults to all of: `claude`, `claude-desktop`, `chatgpt-desktop`, `codex`, `gemini`, `qwen-code`, `kimi`, `cursor`, `opencode`, `vscode`. Registration is **global** wherever the client supports it, so every repo is covered without per-project setup.

**Why ChatGPT Desktop works:** per OpenAI's docs the ChatGPT desktop app, the Codex CLI and the IDE extension *share* MCP configuration in `~/.codex/config.toml`, and the desktop app supports local stdio servers. So the Codex registration covers ChatGPT Desktop; `chatgpt-desktop` is an alias that says so out loud. ChatGPT **web** accepts remote MCP servers only — documented, not worked around (a tunnel would contradict "no public server").

**`--print`** emits the raw snippet for clients we do not write (Windsurf, Zed, LM Studio, …) rather than growing a writer per client.

**Not done:** an HTTP/SSE MCP transport. It would require a reachable endpoint and an auth story — the exact infrastructure this product avoids (§4).

### 12d. `doctor`

**Decision:** one command answers "why is this not working?": install + PATH, config presence and file mode, topic quality, server reachability (with auth), active quiet hours, license validity, Telegram + daemon state, wired agents, MCP registrations, queue backlog, state-dir writability, optionally a real delivery test (`--send`). Every non-OK check carries a **copy-pasteable fix command**; exit code 1 if anything failed.

**Why a new command and not more flags:** the failure modes are known and finite, and a 2-week field test needs one thing to run when something is odd — not a decision tree in the README. (It also detected a build installed *without* the license secret, where every valid key looked invalid — a state the Ed25519 switch in §2b removed entirely: every copy verifies keys now, so `doctor` only ever reports the key itself as valid or not.)

### 12e. Security pass

| Issue | Fix |
|---|---|
| `config.json` (license key, bot token, ntfy password) was mode 644 | written 0600 via a shared atomic `write_json_atomic` |
| `config show` printed `ntfy.auth` and the webhook token in clear | all four credentials redacted (`redacted_config`, covered by a test that greps for each secret) |
| macOS notifications interpolated the message into AppleScript | `_applescript_string` escaping; Windows got a *working* toast (the old branch only loaded a type and reported success); `notify-send` gets `--` |
| Webhook bearer token compared with `==` | `hmac.compare_digest` |
| Any Telegram user could answer an approval by pressing a button | callbacks are accepted only from the configured chat; foreign presses are logged and politely rejected |
| `history.jsonl` grew unbounded | rotated at 2 MB, keeping the newest 2000 entries |
| Config paths written to `~/.claude.json` etc. non-atomically | all JSON configs go through `write_json_atomic`; invalid JSON is refused, not overwritten |

The premium gate is unchanged and still one line deep. Licensing was HMAC at the time, so `doctor` and `license activate` learned to *say* when a build could not verify keys instead of blaming the key — both of those branches are gone since §2b moved to Ed25519 (no build carries a secret, every build verifies).

### 12f. Partial delivery no longer loses a channel

**Bug:** when a queued or deferred item had two channels and only one succeeded, the item was deleted — the still-failing channel's notification was lost.

**Decision:** the item is re-queued with exactly the channels that still failed. Same rule in `drain_queue` and `flush_deferred`, matching what `send_notification` already did.

### 12g. Durations for Claude Code

**Decision:** `hooks install claude` also wires `UserPromptSubmit` → `hook started --silent`, so `Stop` can report the real per-turn duration ("Claude Code finished in 4m12s"). `--silent` writes only the start marker: no notification, no output, no network.

### 12h. Simplifications shipped with it

- `_merge_json_hooks`: the remove branch had a nested loop that shadowed its own loop variable and left `"hooks": {}` behind; rewritten as one pass that removes only our commands and drops emptied events.
- Five copies of "POST to the Telegram API, parse JSON, check `ok`" collapsed into `TelegramChannel._call`.
- Two hand-rolled read-modify-write helpers for `bot.json` collapsed into `_update_bot_state`.
- Every atomic JSON write goes through one helper; `NtfyChannel.poll` replaced three inline poll loops (and fixed the missing auth header, which broke the approval poller and `test` on authenticated self-hosted ntfy).

### 12i. Audit outcome (37 confirmed findings)

The RC was put through a multi-agent adversarial audit: six independent readers over six failure dimensions (approval/concurrency, queue/defer, hooks/purge, security, CLI/portability, Telegram/licensing), every finding then verified by a separate skeptic that had to reproduce it against the real module. 83 raw findings, 37 confirmed, 11 explicitly refuted (documented behavior, stale line numbers, or already fixed mid-run). Each confirmed finding now has a regression test.

Three of them changed how the product behaves, not just how it is implemented:

**Free-text answers are no longer collapsed.** `_parse_answer` matched a keyword *prefix*, so "yes, but use staging" became a bare `approved` and the instruction was lost. Now: an affirmation approves only when it stands alone; a negation denies even with a reason after it (fail-closed is the right direction for an approval gate, and the reason is kept); everything else is free text. The documented exit-code contract (0 approved/answered, 1 denied, 2 timeout) is unchanged — a verifier correctly refuted the related "fails open" claim as documented, tested behavior, so the README now just states the caveat for anyone chaining `ask && <command>`.

**Config-derived Telegram degrades instead of failing.** With `channels: ["ntfy","telegram"]` and no license, `notify` exited 3 and hid the fact that ntfy had delivered. The premium gate stays a permanent error inside `_publish_item_channels` (the queue relies on it to drop unlicensed items instead of retrying forever), but `send_notification` now drops telegram from *config-derived* channel lists — mirroring what `resolve_ask_channels` already did for `ask`. An explicit `--channel telegram` still fails loudly.

**The license env var no longer overrides a real build.** `check_license_key` honored `AGENTBELL_LICENSE_SECRET`, which let anyone pick the verifier's own secret and sign a key with it — a complete bypass of the paid tier in one env var. The fallback was narrowed to an unbuilt checkout (which validated no real key anyway), so the author/test workflow was untouched and shipped builds were not bypassable this way. The move to Ed25519 (§2b) later removed the underlying problem instead of narrowing it: verification uses the hardcoded public key only, and `AGENTBELL_LICENSE_SECRET` is now nothing but a *signing* seed for the author's minting tool — it cannot make an invalid key verify, which is the property `TestAuditRegressions.test_the_environment_cannot_make_an_invalid_key_verify` pins down.

Two findings are documented rather than fixed:

- **ntfy action buttons carry a credential.** A button definition travels inside the published message, so on a protected self-hosted ntfy the `Authorization` header is visible to every subscriber to the topic. Removing it would break button answers entirely. `ntfy.action_auth` now lets you give the buttons a scoped publish-only token instead of the account password, and the README says so.
- **Free-text attribution is per machine.** Pending markers live in the local state dir, so two machines sharing one topic cannot see each other's open questions. Button answers carry the request ID and are unaffected. Out of scope for a single-user tool (§10: no multi-device sync).

### 12j. `--min-duration`: the anti-spam rule

**Problem:** "finished" hooks fire per *turn*, not per *task*. With hooks installed globally, an interactive Claude Code session produces a push every time the assistant stops talking — dozens per hour. That is exactly the automatic spam the product direction rules out, and it is the fastest way to get a notifier uninstalled.

**Decision:** the installed Claude Code and Codex "finished" hooks carry `--min-duration 60`. A turn whose measured duration is under the threshold is skipped and logged as `hook.skipped_short`. Two deliberate exceptions: **failures always notify** (a failure matters however fast it happened), and an **unknown duration always notifies** (agents without a start marker — Gemini, OpenCode, custom scripts — keep v1.2 behavior; fail-open, never silently swallow).

**Why 60 s:** below a minute you were almost certainly still watching. Above it you probably switched tasks — which is the entire premise of the tool.

**Why a flag baked into the hook command and not a config key:** it is visible where the behavior lives (`grep min-duration ~/.claude/settings.json`), editable without a new CLI verb, and per-agent — Codex and Claude Code can differ without a config schema for it. The trade-off is that changing it means editing the hook or re-running `hooks install`.

---

## 13. v1.3.1: what the first real setup broke

§12 was an audit of code. This is the first run of the wizard by a human on a
clean machine — a different failure class, and the more expensive one.

### 13a. Never accuse the credential when the network failed

**Bug:** `validate_token` wrapped *every* exception as `invalid bot token`. A
`getMe` timeout therefore read as "your token is wrong", and the user did the
rational thing: went back to @BotFather and created a new bot. Twice. Both new
tokens were as valid as the first; the API was simply unreachable.

**Decision:** `TransientError` propagates unchanged — only a genuine rejection
becomes `invalid bot token`. The wizard says explicitly that the token was *not
checked*, and offers to keep it (default yes), because an unverified token that
is probably right beats a verified detour through bot creation.

**Rule:** an error message may only blame what was actually tested. The
transient/permanent split already existed for the queue; the diagnostic layer
just wasn't using it.

### 13b. A wizard must never discard what it already has

**Bug:** a token the wizard disliked ended in `SystemExit(3)` — losing the ntfy
topic, the quiet hours and the **license key** entered moments earlier. The
user re-typed the key three times in a row.

**Decision:** `prompt_bot_token` retries, and giving up returns `None` instead
of exiting. Telegram is skipped; everything else is saved. A failure in an
*optional* step may never destroy the mandatory steps that preceded it.

### 13c. `config set`: a fix must be one line

**Problem:** `doctor` flagged the short, guessable topic correctly, but its fix
was "run `agentbell init`" — the whole wizard, including re-entering the
license key, to change one string. That contradicts the copy-paste promise.

**Decision:** `config set <dotted.key> <value>` with an **allowlist** of keys,
each with its own validator, and `doctor` emits the complete command with a
freshly generated topic. Not free-form JSON editing: a typo in a nested key
would create a setting nothing reads. Values are validated more strictly than
the config *reader* — `_load` tolerantly drops an unparseable quiet-hours
window (right, at send time), but accepting one here would silently mean "no
quiet hours" and the user would learn that at 3am.

### 13d. The premium feature depended on an open terminal

**Problem:** Telegram Approve/Deny buttons only exist while the answer daemon
is running, and the documented way to keep it running was
`cp examples/agentbell-bot.service …` — which only works from a git
checkout. Anyone who installed via `install.sh` and moved on could not do it.
So the headline paid feature quietly degraded to buttonless questions.

**Decision:** `bot install-service` writes the unit itself (systemd user unit,
or a launchd agent on macOS) with the **absolute** binary path from
`agentbell_binary()` — `%h/.local/bin` was wrong for pipx and venv installs.
Where systemd isn't running (WSL without it, containers) it says so and prints
a `nohup` line, instead of leaving a unit file that will never start.

### 13e. Copy-paste blocks are executed, not read

**Bug:** the `NEXT STEPS` block listed `agentbell bot` (blocking, never
exits) and then more commands. Pasting the block fed those lines into the
daemon's **stdin**; the suggested `agentbell doctor` never ran and the user
had no idea. Fixed by ending the block with the non-blocking
`bot install-service`, with a test asserting no bare `agentbell bot` line
survives in the block.

**Rule:** anything printed as a copy-paste block is a script. It must survive
being pasted as one — no blocking command with lines after it.

### 13f. Refuse for the real reason, not the file extension

**Bug:** `mcp add` skipped any `opencode.jsonc` with "comments would be lost",
so OpenCode was the one client that needed hand-editing. The stock OpenCode
config contains no comments at all — the check was the *extension*, never the
content. (A naive `"//" in text` would have been no better: the default config
line is `"$schema": "https://opencode.ai/config.json"`.)

**Decision:** `jsonc_has_comments()` scans for `//` and `/*` outside string
literals, and the file is rewritten whenever there is genuinely nothing to
lose. The snippet fallback stays for configs that really do use comments.

**Related:** `opencode_config_path()` now returns whichever of
`opencode.json` / `opencode.jsonc` exists. Returning only the `.json` name
meant a registration could be written to a file OpenCode never reads, while
`doctor` and `uninstall` inspected the other one — three call sites disagreeing
about one path. One resolver fixes all of them.

---

## 14. v1.4.0: from five agents to twelve

Five agents was enough to prove the idea and too few to be the answer to "does
it work with mine?". v1.4.0 answers that question with **seven more
integrations in one batch** — and, more importantly, with a structure that
makes the eighth cheap.

### 14a. The agent list became a table, not a pile of branches

**Problem:** every agent was five separate code paths — detect, install,
status, uninstall scan, and the `init` wizard's offer. Adding one agent meant
touching five functions and remembering all five. That does not scale to
twelve, and the parts that get forgotten are the boring ones (a new agent that
`uninstall` does not know about leaves junk behind forever).

**Decision:** one `AGENT_SPECS` registry — per agent: how to detect it, where
its config lives, how to install, how to report status. `find_agents()`,
`install_hooks()`, `hooks_status()` and the purge scan are thin wrappers over
that table. A new agent is one entry, and it is automatically detected, wired,
reported and removable.

**Why it matters beyond tidiness:** the uninstall promise ("removes exactly
what it added") only holds if install and uninstall cannot drift apart. Driving
both from one table is what makes that structural instead of a discipline
problem.

### 14b. The seven, and why each got the mechanism it got

Two of them have real lifecycle hooks, so they get deterministic wiring:

- **Kimi Code** — `~/.kimi-code/config.toml` `[[hooks]]`, real
  `UserPromptSubmit`/`Stop`/`StopFailure` events, so it gets finished-with-duration
  and failed, exactly like Claude Code and Codex. Kimi accepts only the four
  fields `event`/`matcher`/`command`/`timeout` in a hook table — anything else
  makes it refuse the whole config — so the block carries nothing more.
- **Qwen Code** — `~/.qwen/settings.json`, which speaks Claude's hooks.json
  dialect, so the same three events wire up the same way.

The other five have no lifecycle hooks at all. They get a clearly marked rule
file that tells the agent to call the CLI — best-effort by construction, and
labeled as such everywhere it is offered:

- **Windsurf** — `.windsurf/rules/agentbell.mdc`, the same MDC engine as
  Cursor. (This is the one that moved under us; see §15.)
- **Cline** — `.clinerules/agentbell.md`; Cline reads every `.md`/`.txt` in
  that directory.
- **Continue** — `.continue/rules/agentbell.md`.
- **Zed** — a marked block in `.rules`. Zed reads exactly one instruction file
  per repo and `.rules` is the highest-priority name, so it is the only correct
  target; the block is appended so a hand-written `.rules` keeps its content.
- **Aider** — a marked block in `AGENTS.md`, which Aider has auto-read since
  v0.69. `CONVENTIONS.md` would have needed a `read:` line in
  `.aider.conf.yml`; requiring a config edit to install a convenience tool is
  the wrong trade. Aider's block is tagged `--agent aider`, so a legacy
  OpenCode `AGENTS.md` block is not mistaken for it.

**The honest part of this section:** six of the twelve agents are wired by
prompt, not by hook. A rule file is an instruction the model can ignore, and
sometimes does. It is documented as best-effort in the README, in
`FIELD_TEST.md`, and in the wizard — the alternative was to not support them,
which helps nobody.

### 14c. Kimi Code as an MCP client

`mcp add` gained `~/.kimi-code/mcp.json` (global; `--project` →
`<proj>/.kimi-code/mcp.json`), standard `mcpServers` stdio format. Kimi surfaces
the tools as `mcp__agentbell__notify` and `mcp__agentbell__ask_approval`,
in new sessions only.

**Trade-off accepted here:** seven integrations shipped in one release is a lot
of surface added at once, and each one is a claim about somebody else's product
that could already be stale. That is exactly what §15 was written to check —
and it found two of the seven had moved.

---

## 15. v1.4.1: every integration re-verified against the live docs

v1.4.0 shipped seven new integrations in one batch. Before trusting other
people's machines to them, every path and format was re-checked against each
vendor's current documentation (2026-08-16) — the same failure class as §13:
what breaks is not what the code does, but what it assumes about the agent.

Confirmed correct as shipped:

- **Kimi Code** (moonshotai.github.io/kimi-code): `~/.kimi-code/config.toml`
  `[[hooks]]` with exactly `event`/`matcher`/`command`/`timeout`, events
  `UserPromptSubmit`/`Stop`/`StopFailure`; MCP in `~/.kimi-code/mcp.json`
  (`mcpServers.command/args`, project-local `.kimi-code/mcp.json`);
  `KIMI_CODE_HOME` override. Note: Moonshot is winding the old kimi-cli down in
  favor of Kimi Code CLI — the integration targets the successor, and the
  config paths are the successor's.
- **Gemini CLI**: `~/.gemini/settings.json` `hooks.AfterAgent` with
  `matcher: "*"`, `timeout` in ms.
- **Cursor**: `.cursor/rules/*.mdc` with `alwaysApply: true` frontmatter.
- **Cline**: `.clinerules/` directory (all `.md`/`.txt` inside are read).
- **Continue**: `.continue/rules/` directory.
- **Zed**: `.rules` still the highest-priority single project-instruction file
  (zed.dev/docs: `.rules` > `.cursorrules` > `.windsurfrules` > `.clinerules`
  > `.github/copilot-instructions.md` > `AGENT.md` > `AGENTS.md` …).
- **OpenCode**: `{plugin,plugins}` directories both scanned (verified in the
  opencode source); plugin event names `session.idle`/`session.error`/
  `permission.asked` current. Confirmed live: the installed plugin fires on
  this very setup.

Changed because the vendor moved:

- **Windsurf** changed its rule engine (Windsurf → Devin Desktop). Current
  builds read `.windsurf/rules/*.md` (or `.devin/rules/*.md`, preferred) with
  `trigger: always_on` frontmatter; the Cursor-style `.mdc` this tool wrote is
  no longer the documented format. Install now writes **both** (`.md` for
  current builds, `.mdc` for pre-Devin ones); uninstall removes only files we
  own. Detect also looks for a `.devin` directory.
- **Qwen Code** command hooks support `async: true` per current docs; the
  v1.4.0 hooks ran synchronously and could block the end of every turn. They
  are now async, matching the Claude/Codex wiring. The `hooksConfig` note is
  gone (hooks are on by default; `disableAllHooks` turns them off).

Install is now self-healing as well as idempotent: the JSON merger compares
whole hook entries instead of just the command string, and the Kimi/Codex
TOML blocks are replaced when their content is stale (binary path or flags
changed) — a config written by an older release is repaired by the next
`hooks install`, instead of being treated as "already present".

Added:

- **Qwen Code is an MCP client**: `mcp add` registers `mcpServers` in
  `~/.qwen/settings.json` (project scope: `.qwen/settings.json`), and
  `uninstall`/`doctor` scan it. Stdio format `command`/`args` confirmed against
  the Qwen Code MCP reference.
- **Continue detection** also looks for the `continue` binary (was `cn` only).

---

## 16. v1.6.0: the universal agent contract — `integrate` + `verify`

§15 named the problem without meaning to: twelve integrations are twelve
claims about other people's products, and two of seven were already stale
after three months. Agent #13 was never going to fix that — it would have
been claim #13. This release answers the treadmill differently.

### 16a. The inversion: publish a contract, observe the results

**Decision:** agentbell does not integrate unknown agents. `agentbell
integrate` prints a versioned, platform- and state-aware self-integration
guide (`--json`: the same contract as a machine-readable manifest); the agent
performs the integration **in its own config files, with its own
permissions**; `agentbell verify` reports what actually happened, from
history records alone.

**Why not write foreign configs ourselves:** an installer for an agent we
don't know means writing a config format we cannot validate, cannot repair
and cannot cleanly uninstall — §15's treadmill with worse failure modes, plus
a trust-boundary violation (agentbell would need write access to arbitrary
config surfaces). The inversion gives agentbell **no new write surface at
all**: both new commands are read-only (the guide's smoke test sends one
notification, run by the agent, through the existing `hook` path).

**The honest limitation:** a printed contract is followed by a model, not
enforced by code. Self-integration is probabilistic where our native hook
installers are deterministic — which is why the three classes (native /
self-integrated / rules-based) are labeled everywhere, and why "verified"
is a per-agent observation, not a product claim.

### 16b. Why the runtime already made this possible

The runtime layer was agent-agnostic before this release: `hook` accepts any
`--agent` matching `[A-Za-z0-9_-]{1,32}`, `notify`/`ask`/MCP/webhook don't
care who calls them, `ask` fails closed everywhere. What was missing was
discoverability (nothing printed the contract), attribution (history records
carried no `agent` field, unknown slugs showed as "Agent" on the phone),
observability (suppressed/deferred/queued rewrote the event name and
destroyed the evidence), and honesty (no way to say what "supported" means
per class). Those four gaps are what v1.6.0 actually built.

### 16c. `verify` is observation, not certification

A `verify` that "certifies" an integration would be circular: the agent can
fire a manual smoke event that is indistinguishable from a real lifecycle
event. So:

- `--force` events are recorded with `forced: true` and reported separately
  ("N forced smoke tests") — they prove the delivery path, never the wiring.
- The trust anchor is procedural: one **real agent turn** after the
  integration, then `verify --agent <slug> --since 10m`.
- Quiet hours and queueing rewrite a record's event to
  `suppressed`/`deferred`/`queued`; the new `source_event` field preserves
  the original hook event. Without it, "arrived but held" reads as "never
  fired" and users would double-install during quiet hours.
- Duplicates (same slug, same **turn** event — `started`/`run_completed`/
  `run_failed` — ≤5 s apart, `--force` records excluded) are a **WARN,
  never a FAIL**: two parallel sessions are legitimate; `history`
  disambiguates. Interaction events (`permission_required`,
  `input_required`) never count: a turn starts and ends once, but an agent
  legitimately raises several permission prompts within seconds (§16i).
  A runtime dedupe was rejected — it could swallow wanted notifications.
- **`verify` never prints the topic, server or any path** (test-enforced).
  It is the one status command designed to be handed to an agent; the guide
  points agents at it and never at `doctor`/`config show`. `doctor` keeps
  printing the topic — it is a human command with an explicit warning, and
  degrading its UX to defend against an agent running it uninvited is the
  wrong trade (documented residual risk in the README trust model).
- doctor vs. verify: doctor = "is agentbell healthy", verify = "did agent
  integrations actually fire". doctor's agent-hooks line mentions
  self-integrated slugs it has seen; the real assessment lives in verify.

### 16d. Attribution via an `agent` field, not a new log

History already records every delivery decision; a second log would need its
own rotation, its own consumers and a sync story. `send_notification` gained
an optional `agent` kwarg (the only hot-path change), `run_hook` passes it
through, and `verify` filters on it. Records without the field (plain
`notify`, webhook) simply don't participate. MCP `notify` accepts an
optional `agent` argument, sanitized by `safe_agent_name()` — invalid values
drop the attribution instead of killing the MCP server (contrast
`validate_agent_name`, whose SystemExit(2) is correct for the CLI where the
name becomes a state-file path).

### 16e. Unknown hook events: tolerant for events, strict for --agent

`hook <event>` no longer argparse-rejects unknown event names: a
self-integrating agent that invents `task_done` would otherwise die with
exit 2 — violating "a hook must never fail the agent's turn". Unknown events
now exit 0, send nothing, and write a `hook.unknown_event` record with the
requested name; `verify` turns those into a WARN with the valid event list.
Never-fail-a-turn beats clean-error **only for the event name**: `--agent`
stays strict (exit 2) because it is interpolated into a state-file path —
that validation is a security boundary, not ergonomics.

### 16f. Slug-scoped rule markers

The guide's Appendix A wraps the standard instructions block in
`<!-- agentbell:<slug>:start/end -->` instead of the generic
`<!-- agentbell:start -->`. The generic markers belong to
`hooks install`/`uninstall`/purge (substring checks in
`_install_block_file`); a self-integrated block using them would be
mangled by `hooks uninstall zed` in the same file. The scoped markers are
invisible to those substring checks — collision-free by construction
(test-enforced: the generic marker does not appear in the guide).

### 16g. Relationship to the field-test gate (§8.1)

This feature is read-only and **adds** field-test rows (self-integration,
double-integration detection) — it does not substitute for the open gate.
The README's "any agent" claim is deliberately gated: it is phrased as a
contract statement with a visible verification status ("field-verified so
far with: —") until at least one genuinely unknown agent has been integrated
end-to-end by a real user.

### 16h. Deliberately not built (and what would change that)

- **A registry of self-integrations (`agents.d`)** — deferred, not rejected.
  History-based visibility covers doctor/verify today; a registry earns its
  state cost when several self-integrations are active at once and need
  names, uninstall hints or per-agent settings.
- **verify --send** — `test`/`doctor --send` exist; "read-only, offline,
  safe" is the property that makes verify handable to agents.
- **A third MCP tool serving the guide** — an MCP-capable agent can run the
  CLI or be handed the text; a tool would duplicate the contract surface.
- **A webhook `agent` field** — webhook callers are scripts/CI, which
  already choose their own message text; attribution solves an agent
  problem the webhook doesn't have. Add it when a real consumer appears.
- **Auto-repair of foreign wiring** — agentbell cannot know whether a
  changed foreign config is drift or intent. The guide requires agents to
  document removal steps instead.
- **Changing the rule template of the six existing rule agents** (e.g. to
  absolute paths or richer policy): block installs don't self-heal
  (`_install_block_file` only adds/removes), so a text change would diverge
  across already-installed copies. Separate decision, taken deliberately
  later; the guide embeds the template verbatim so there is exactly one
  text to evolve.

### 16i. What the Tier-1 field test taught (GitHub Copilot CLI, 2026-08-21)

The gate from §16g closed: GitHub Copilot CLI 1.0.80, given only the
`integrate` output, wired its own hooks, produced real attributed lifecycle
events, passed `verify`, was idempotent on a second run and removed itself
cleanly (evidence rows in `FIELD_TEST.md`). The same run exposed two false
alarms in agentbell's own diagnostics — both were the tool making a claim
its evidence did not support:

- **`test` said "NOT delivered" for messages that were on the phone.** Root
  cause: the confirmation poll's `since` cursor came from the *local* clock,
  but ntfy filters by *server* time — a local clock running ahead (WSL2
  clock drift) hid the delivered message, and poll errors were swallowed, so
  the failure had no visible reason. Decision: the poll window is now a
  server-relative duration (`since=90s`), the last poll error becomes the
  reported reason, and the output states exactly what was proven: publish
  failure ("NOT delivered"), accepted-but-unread ("sent, but NOT confirmed",
  still exit 1 — fail-closed, unconfirmed is not proven), or read back from
  the server ("delivered and confirmed"). "Confirmed" deliberately claims
  the server, not the phone: only the subscription proves the final hop.
  `doctor --send` reports accepted-but-unread as a WARN (exit 0): doctor
  diagnoses health, and that state includes structurally healthy configs
  (cache-disabled servers, write-only publish tokens) where a FAIL would be
  permanently wrong — `test` stays the delivery *proof* command with its
  strict exit code. The same local-vs-server-clock class was also fixed in
  the ask receiver: prime/stream/poll windows are monotonic-elapsed
  duration strings now, deduplicated by message id.
- **`verify` warned "possible double integration" on real permission
  prompts.** Root cause: the near-duplicate heuristic treated *any*
  same-label pair ≤5 s apart as suspicious; Copilot raised several distinct
  `permission_required` prompts within one second. Hook messages are
  templates (same agent + cwd ⇒ identical text), so content cannot
  disambiguate — the event *class* can: a turn starts and ends once, so
  duplicate detection now covers `started`/`run_completed`/`run_failed`
  only, tracks per event label (an interleaved permission prompt no longer
  resets the pair detection — strictly stronger on turn events), and skips
  `--force` records. The residual gap — a double integration that only
  wires interaction events — also double-fires turn events in practice,
  which is where the detection now looks.

### 16j. The advertised binary is a claim about execution, not a mirror of argv (CI, 2026-08-21)

CI failed on every matrix job with the contract advertising
`<workspace>/python -m unittest hook run_completed …` as a runnable command.
Root cause: `agentbell_binary()` fell back to `os.path.abspath(sys.argv[0])`,
and the stdlib's `unittest/__main__.py` rewrites `sys.argv[0]` to the literal
string `"python -m unittest"` for nicer help text. Locally the bug was
invisible because an installed launcher on PATH short-circuited the fallback
— which is exactly why the fallback path needs its own tests.

Decision: the binary in a published contract is a *claim* — "this single
token executes agentbell" — so every candidate must be checked against that
claim, not taken from context. Order: (1) `shutil.which("agentbell")`;
(2) `sys.argv[0]`, but only when it names a real agentbell entry point on
disk (basename stem `agentbell`, case-insensitive — covers the launcher,
`agentbell.py`, `agentbell.exe`); (3) the module file itself. Test runners,
embedders, and a relative argv[0] invalidated by `chdir` all fail check (2)
and land on (3), which is always agentbell by construction.

The same claim-vs-shape confusion existed on the read side: uninstall,
self-heal and `hooks status` matched the substring `agentbell hook`, which
only the bare-launcher shape produces. Checkout (`…/agentbell.py hook`) and
Windows (`'…\agentbell.exe' hook`, always quoted by `shlex.quote`) hooks
were installable but invisible to removal and repair. The matcher now parses
the command and compares the first token's stem — and deliberately leaves
wrapped commands (`bash -c '…'`) alone: a wrapper is the user's construction,
and "only entries whose command is ours are ever touched" outranks
completeness of removal.

Follow-up from the same CI pass: the advertised *commands* must embed the
binary shell-quoted (`shlex.quote`) — a Windows path or a path with spaces
otherwise dies at the host's shell split; the manifest's `binary` field
stays the raw path, and the two are reconciled by the contract test
(`shlex.split(command)[0] == binary`). The Windows CI jobs had been red all
along for a test-environment reason worth recording: Windows `expanduser`
reads `USERPROFILE` and ignores `HOME`, so tests that only moved `HOME`
operated on the real runner profile — cross-test contamination that looked
like product bugs (broken idempotence, purge misses). Test homes move both
variables now.

### 16k. Shared `AGENTS.md` needs an Aider-only scope and explicit migration

The Aider rules integration writes a marker-owned block to project
`AGENTS.md`. That filename is shared across agent ecosystems: Codex and other
agents read it too. The pre-scope block contained literal `--agent aider`
commands without saying that only Aider may follow them, so another agent
could send a duplicate notification falsely attributed to Aider.

Silent repair during `status` or `verify` was rejected: both are observation
commands, and rewriting a user-visible instruction file while diagnosing it
would violate their contract. They now classify a single, bounded legacy
Aider block as `update needed`. Human-readable output puts a bordered ACTION
REQUIRED banner before the normal report; JSON stays machine-readable and
uses a structured `repair_notices` entry.

Repair is explicit: `agentbell hooks install aider`. Only this Aider install
path may replace stale content between agentbell's existing start/end markers.
Text before and after the markers is retained byte-for-byte, ambiguous marker
layouts are never guessed at, and a second install is idempotent. The current
block begins with an Aider-only scope instruction so other `AGENTS.md` readers
are told to ignore the whole block.

## 17. v1.6.1: one buzz per event (2026-09-03)

Twelve days of real use (1,339 history records) exposed two noise sources
the v1.6.0 field test could not: OpenCode reporting one turn end twice, and
an API outage turning six parallel Claude Code sessions into six identical
failure pushes within seven seconds.

### 17a. Suppress the identical push, keep the record

The push text is the identity: agent, event and message - and the message
carries the project path and the duration. Two pushes with the same text
within 5 s are one piece of news. The suppression happens in `run_hook`,
before delivery, and writes `hook.skipped_duplicate` with the text and the
original event: history stays complete, `verify` counts it ("N suppressed
(identical push)"), nothing is silent. A different text is never a repeat -
two projects or two durations are two pushes. `--force` bypasses it, like
every other rail.

Rejected: keying on agent + event only (would swallow a second project's
finish); a lock (portable file locking is not worth exactly-once for a
notification - check-then-write collapses the sequential bursts observed,
and the OpenCode plugin handles its own same-instant double in memory); a
longer window (a genuine second turn of the same length in the same project
within a minute is plausible, within five seconds it is not).

### 17b. `run_failed` is not double-integration evidence

Same argument as §16i for permission prompts: failures are driven from
outside. One outage, many sessions, many `run_failed` within seconds - all
real. A double integration doubles `run_completed` too, so removing
`run_failed` from the near-duplicate set loses nothing. Because suppressed
repeats can no longer produce a delivered pair, a suppressed `run_completed`
repeat counts as the near-duplicate instead (`suppressed: true` in
`verify --json`).

### 17c. OpenCode: measure the turn, dedupe the idle

The plugin records the user's prompt (`message.updated` with role `user`)
per session and passes `--duration` to the hook. Explicit duration instead
of the start marker because the marker is per agent, and parallel OpenCode
sessions would overwrite each other's start. Unknown duration (plugin loaded
mid-turn) still notifies - agentbell's rule, unchanged. The idle double is
collapsed per session in memory (10 s) because both events arrive within the
same second from one plugin instance - exactly the case the file-based
window cannot promise to catch. Root cause inside OpenCode was not
established; the fix is robust to either a doubled event or a second
listener and is exercised under node by the test suite.

### 17d. Removal is owner-scoped too (v1.6.2)

Minutes after the v1.6.1 install, `hooks install opencode` deleted the
checkout's `AGENTS.md`: its v1.3rc migration removed every agentbell marker
block in that file, and the only block there was Aider's. b1aa21e had made
block *repair* check ownership; removal had kept the pre-shared-file
assumption that any block between our markers is the caller's. The rule from
§16j applies on the way out as well: only content that names the removing
agent (`--agent <slug>`) is touched. A block that names no owner is left
alone rather than guessed at - the cost is a stale, harmless block; the
alternative was a deleted user file.
