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
| Claude Code | `~/.claude/settings.json` `hooks` (`UserPromptSubmit`, `Stop`, `StopFailure`, `Notification(agent_needs_input)`, since 1.7.0 also `Notification(permission_prompt)`), `async: true` | Real shell hooks, documented; event names verified against Claude Code 2.1.232. Async so network latency never blocks a turn. `UserPromptSubmit` only writes a start marker (`--silent`) so the completion push can report the turn duration. The `permission_prompt` matcher (runs `hook permission_required`) comes from Claude Code's hooks documentation (checked 2026-09-23); it is wired and covered by tests, but not yet field-tested with a real Claude Code session. Existing installs get it only by re-running `hooks install claude`. |
| Codex | `~/.codex/config.toml` `hooks.UserPromptSubmit` + `hooks.Stop` | Codex supports lifecycle hooks in config.toml (events verified against Codex 0.147.0). We append TOML array-of-tables blocks guarded by markers so user config is never rewritten. Hooks are enabled by default; `features.hooks = true` is written only when it cannot conflict with an existing `[features]` table, and the note now says so instead of claiming hooks are off. `SessionEnd` was considered but dropped: it fires on every exit (including `/clear`) and would spam. |
| Gemini CLI | `~/.gemini/settings.json` `hooks.AfterAgent` | Fires once per turn after the final response — the correct "done" event. No failure event exists; documented. |
| OpenCode | plugin in `~/.config/opencode/plugin/agentbell.js` (**v1.3.0**, was an `AGENTS.md` block) | OpenCode has a real plugin bus (`event` hook). Deterministic beats "please call the CLI": `session.idle` → run_completed, `session.error` → run_failed, `permission.asked` → permission_required. Verified against OpenCode 1.18.18. |
| Kimi Code | `~/.kimi-code/config.toml` `[[hooks]]` (v1.4.0) | Real lifecycle hooks (`UserPromptSubmit`/`Stop`/`StopFailure`), event names re-verified against moonshotai.github.io/kimi-code (2026-08; the kimi-cli docs are being wound down in favor of Kimi Code CLI). Kimi only accepts the four fields `event`/`matcher`/`command`/`timeout` in a `[[hooks]]` table — anything else (e.g. `async`) makes it refuse to load the whole config, so the block is kept to exactly those keys. |
| Qwen Code | `~/.qwen/settings.json` `hooks` (v1.4.0) | Speaks Claude's hooks.json format (`UserPromptSubmit`/`Stop`/`StopFailure`, no matcher on these events). Command hooks support `async: true` (re-verified against qwenlm.github.io/qwen-code-docs, 2026-08), which is used so a notification send never blocks the end of a turn. Hooks are enabled by default; `disableAllHooks: true` disables them. |
| Cursor | `.cursor/rules/agentbell.mdc` (alwaysApply rule) | Cursor had no lifecycle shell hooks when this was decided. *(It has since documented hooks of its own, `~/.cursor/hooks.json`; agentbell does not use them yet and still installs the rule, per project.)* A rule that instructs the agent to call the CLI is the standard, reliable mechanism. This is the one file we own outright, so it is written verbatim — a `.mdc` must begin with its YAML frontmatter, and the marker-block wrapper used for shared files broke it (fixed in v1.3.0). Format re-verified against cursor.com/docs/context/rules (2026-08): `.mdc` + `alwaysApply: true`. |
| Windsurf | `.windsurf/rules/agentbell.md` + legacy `.mdc` (v1.4.0, fixed v1.4.1) | Windsurf/Devin Desktop's rule engine changed: current builds read `.windsurf/rules/*.md` (or `.devin/rules/*.md`, preferred) with a `trigger: always_on` frontmatter (docs.windsurf.com/windsurf/cascade/memories, 2026-08); pre-Devin builds only knew Cursor-style `.mdc`. One install writes **both** files so every build picks the rule up; uninstall removes only files we own. |
| Cline | `.clinerules/agentbell.md` (v1.4.0) | `clinerules/` directory, markdown rules — Cline processes every `.md`/`.txt` in it. Since 1.7.0, a project that has an older single `.clinerules` *file* gets a marked block inside that file instead (Cline still reads it; creating the directory crashed with `FileExistsError`). Uninstall removes only the block and never deletes the user's file. |
| Continue | `.continue/rules/agentbell.md` (v1.4.0) | `rules/` directory inside the repo `.continue/`, markdown. |
| Zed | `.rules` block at repo root (v1.4.0) | Zed reads exactly one file per repo: `.rules` > `.cursorrules` > `.windsurfrules` > `.clinerules` > `.github/copilot-instructions.md` > `AGENT.md` > `AGENTS.md` > `CLAUDE.md` > `GEMINI.md`. Writing `.rules` is the highest-priority single-file option; our marker block is appended so a hand-written `.rules` keeps its own content. |
| Aider | `AGENTS.md` block at repo root (v1.4.0) | Aider auto-reads `AGENTS.md` (since v0.69) — no config change needed. `CONVENTIONS.md` would require a `read:` line in `.aider.conf.yml`, so `AGENTS.md` was chosen. Status is marked by the `--agent aider` command, so a legacy OpenCode `AGENTS.md` block does not count as installed. |

All installers merge JSON (preserving user keys), append marked TOML/markdown blocks, are idempotent, and `uninstall` removes exactly what was added (identified by the `agentbell hook` command marker / comment blocks). Since v1.4.0 they all live in one `AGENT_SPECS` registry (detect / install / status) that drives `find_agents()`, `install_hooks()`, `hooks_status()` and the purge scan. *(Ownership is narrower since 1.7.0: an entry is agentbell's only in the exact shape and place agentbell writes it, §33, and the bytes of the host file around it are kept, §34.)*

**Decision:** hooks call `agentbell hook <event>` (silent, fast, never fails the agent) rather than `notify` — hooks must not print to stdout or block agent turns. Each network attempt has a 5 s timeout. Since 1.7.0 all sending in one hook, retries and queue replay included, fits a 6 s budget, and what does not fit is queued (§32). A hook that fails still exits 0, but leaves a `hook.error` history record and one stderr line instead of vanishing.

**Decision:** the canonical event set is fixed — `run_completed`, `run_failed`, `input_required`, `permission_required`, `started` — with short aliases (`done`, `failed`, `needs-input`, …) kept for convenience. `permission_required` is wired where the host has a permission event of its own: the OpenCode plugin maps `permission.asked` to it (since v1.3.0), and since 1.7.0 the Claude Code hooks map `Notification(permission_prompt)` to it (wired, not yet field-tested). Codex, Gemini CLI, Kimi Code and Qwen Code have no permission event wired. It remains available to the CLI, MCP and custom scripts. *(An earlier version of this paragraph said it was not wired into any agent hook; that was already wrong for OpenCode.)*

## 4. Approval flow: ntfy response-topic roundtrip (no public server)

**Superseded in part by §22 and §30.** The transport below is current. The
answer contract is not: exit codes are 0 approved or answered, 1 denied,
2 timeout and 3 runtime error, and `approved` is true only for an explicit
yes (§22). A typed reply is used only when exactly one question can be on
the phone (§30).

**Decision:** `ask` publishes the question to the main topic with ntfy **action buttons** whose HTTP action POSTs the answer to a dedicated `<topic>-responses` topic. `ask` waits for the first message on that topic using a **hybrid receiver**: a live JSON stream (fast path) plus short `poll=1&since=<ts>` requests every ~4s (fallback), deduplicated by message id. Exit 0/1/2 = approved/denied/timeout; free-text replies are passed through as the answer.

**Why the polling fallback:** ntfy's long-lived streams are the right mechanism, but in practice fan-out to subscribers can stall (observed on ntfy.sh). Polling is immune to stream buffering and adds negligible load for a single-user tool; the dedupe set keeps both paths safe.

**Why not a webhook server as the answer receiver?** It would require a public IP/port-forward/Tailscale and a daemon. ntfy is already the delivery channel — reusing it as the *response channel* means the approval flow works from the same laptop/phone pairing with zero extra infra. The mobile app's "reply" box and action buttons both publish to topics, so users get both buttons and free-text.

**Why a dedicated responses topic instead of the main topic?** The main topic carries other traffic (every hook event); a dedicated topic makes "first message = answer" unambiguous. Cost: user subscribes to two topics in the app (the wizard prints both).

**Security note:** on public ntfy.sh, anyone who guesses the topic name can publish to it (both main and responses). For sensitive approvals, self-host ntfy with auth — publish and subscribe carry the configured `ntfy.auth` header. The action buttons do not: a button's headers are published inside the message, so they carry only `ntfy.action_auth` (a token that may publish to the response topic), and on a protected server without `action_auth` the question goes out without buttons (§12i). Since v1.2 the wizard suggests a 128-bit random topic by default (plus the username prefix), which makes guessing impractical; the README still recommends self-hosted ntfy + auth for sensitive approvals.

### 4b. Request-ID model (v1.2)

**Superseded in part by §30.** The request id and the id-bound buttons are
current. The free-text rule is not: a typed reply no longer goes to the
newest open ask. It is used only when exactly one ask can still be on the
phone, and refused with a notice otherwise.

**Decision:** every `ask` carries a 64-bit random approval id (`secrets.token_hex(8)`). ntfy button bodies and Telegram `callback_data` embed it (`APPROVED <id>` / `agentbell|<id>|approved`). Both answer paths bind answers to that id: button answers with a non-matching or unknown id are ignored and logged (`stale_answer` history events; Telegram answers expired callbacks politely). Free-text replies cannot carry an id on either platform, so they are attributed **deterministically to the newest open ask**: each waiting `ask` registers itself in `<state>/ntfy-pending`/`tg-pending` before publishing and unregisters when done; a waiter only accepts free text if it is the newest unexpired entry.

**Why:** parallel asks on the shared `<topic>-responses` topic previously risked cross-talk (both waiters saw every message). ID-bound buttons + newest-wins free text make every outcome unambiguous without changing the single-ask UX.

**Trade-off:** free text arriving after a newer ask opened goes to the newer ask even if the user meant the older one — the same rule Telegram flows use, documented in README. Sequential asks are unaffected (only one pending entry exists).

**Trade-off:** ntfy cannot carry the Telegram flow without a long-running bot daemon (callbacks are server-side). Telegram approvals are implemented since v1.1 as an opt-in long-polling daemon — see §9.

## 5. Quiet hours & priority semantics

**Decision:** quiet windows are time ranges (`22:00-07:30`, overnight wrap supported); during them, notifications below `quiet_hours_min_priority` (default `normal`) are handled per `quiet_hours_mode`: **`suppress`** (default, drop + history) or **`defer`** (store in `<state>/deferred/` and deliver after the window ends). `--force` bypasses both modes; `--defer` on `notify` defers a single call even in suppress mode; `ask` always publishes at high priority and is **never** deferred or suppressed — approvals must not be swallowed. `quiet_hours_min_priority` takes 1–5 or the names `min` … `urgent` (names since 1.7.0; a hand-edited name used to make every send fail).

**Defer delivery:** deferred items are delivered by the next notification activity after the window ends (`notify`/hook/`watch`/webhook), by `agentbell queue flush`, or immediately by the bot daemon if it runs. More than 3 due items are **bundled into one summary notification** ("N deferred notifications") to avoid an inbox flood at 07:30; up to 3 are replayed individually. If quiet hours are still active at flush time (e.g. the window moved), items are re-deferred instead of delivered. A transient delivery failure moves the item into the offline queue instead of dropping it. The deferred store is capped at 200 items (drop-oldest + history).

**Why defer over "always drop"?** v1 dropped + logged, which silently lost low-priority events the user wanted to see in the morning. Defer keeps the promise of the notification while respecting "opt-in, not auto-spam": delivery only ever happens outside quiet hours and is bundle-limited. Suppress remains the default because deferral adds state and surprise; users opt in explicitly (wizard question, `--quiet-hours-mode defer`).

## 5b. Retry & offline queue

**Decision:** publish failures are classified (`TransientError`: connection errors, timeouts, 5xx/408/429; `PermanentError`: 4xx, bad config, premium gate). A transient failure is tried up to 3 times in total (`RETRY_ATTEMPTS`), with 1s/2s backoff between the tries, inside the same call. A hook stops earlier when its 6 s send budget runs out (§32), and the bot's drain stops at its 20 s budget per cycle. If the channel is still down, the notification is **queued** in `<state>/queue/` (one JSON file per item) and delivered later — the v1.1 failure mode (silent loss) is gone.

**Queue limits:** max 100 items (drop-oldest + `queue_overflow` history), max age 24 h (`queue_expired`), permanent failures on replay are dropped + logged (`queue_dropped`), transient ones stay with an attempt counter (`kept`). Queued `ask` questions are pointless (the answer window closes), so `ask` never queues — it retries and then fails loudly (parallel-channel asks still let the healthy channel decide).

**Drain triggers (no hidden daemon):** (1) `agentbell queue flush` (also flushes deferred items), (2) automatically after any successful send — bounded to 2 queue items so a normal `notify` stays fast, (3) the opt-in bot daemon drains the whole queue + deferred store every poll cycle. Items are claimed atomically (`.sending` rename) so concurrent processes never double-deliver.

**Quiet hours apply to the queue too (1.7.0).** A queued item retried during quiet hours is moved to the deferred store (`queue_deferred`) instead of being pushed, unless its priority is at or above `quiet_hours_min_priority` or it was sent with `--force`. The 24 h age still counts from the first queueing. When one channel of a multi-channel item fails permanently while another delivered or kept it, the dropped channel is recorded (`queue_dropped` / `deferred_dropped`).

**Semantics:** `notify` exits 0 when a message is queued (it is not lost — just delayed) and prints a stderr warning; history records `queued`/`queued_delivered`, which share a `queue_id` since 1.7.0 so `verify` counts a queued hook push as delivered once the queue has delivered it. Permanent failures still exit 3 as in v1.1.

## 6. One tool, three surfaces: CLI / MCP / webhook

**Decision:** one codebase, three entry points sharing the same send/ask core.

- **CLI** — human + scripts + agent hooks.
- **MCP** (stdio, JSON-RPC 2.0, `notify` + `ask_approval`) — agents that prefer tool calls over shell (`mcp add <agent>` registers it in each agent's config).
- **Webhook** (`agentbell server`, 127.0.0.1 by default, optional bearer token) — remote scripts, CI, VPS without the CLI. `/ask` blocks server-side until approval.

**Why:** each surface maps to one real user pain; the shared core keeps the whole thing in one file (~5400 lines when this was written, ~10,300 at 1.7.0).

## 7. Deliberately not implemented (yet)

1. **Windows-native hooks.** The CLI runs on Windows; agent hook configs are generated for the agents' own shells. First-class Windows support is possible but wasn't the focus. *(Superseded in part: since 1.7.0 the hook command is written for the shell each host uses on Windows — a plain path unquoted with forward slashes, which runs in cmd, PowerShell and Git Bash; otherwise `& '…'` for PowerShell hosts and double quotes for Claude Code and Kimi Code; Qwen Code hooks carry `"shell": "powershell"`. The host shells come from the hosts' source and docs as of 2026-09. The forms were run through cmd, PowerShell 5.1 and Git Bash, not inside the real host apps. A path that needs quoting still fails under Codex's `cmd /C` fallback, under Qwen builds that ignore the `shell` key, and in Claude Code without Git Bash. The Windows toast is §24; `watch` on Windows is §28 and §37.)*
2. **Auto-update / prebuilt binaries / custom sounds** — plausible premium extras later, deliberately not built now.
3. **Multi-user / team topics, dashboards, rate limiting** — out of scope for a personal tool.
4. **Cursor global rules.** Cursor stores global rules in its own DB; we install project-level rules (documented). 
5. **Per-event channel routing, reply-to on plain notifications** — explicitly excluded by this iteration's goals.
6. **Agent remote control via chat** (arbitrary Telegram commands, reply-to as a command system) — v1.2's request IDs answer *approvals*, they deliberately do not become a general command surface. See §10.
7. **A background auto-drainer for free users.** Queue/defer delivery is opportunistic (next notify/hook/`queue flush`), never a hidden daemon. The known opt-in `bot` daemon drains when it runs.
8. **Retry delivery guarantees beyond best-effort.** Retries are bounded and the queue is local; there is no at-most-once guarantee (a timed-out POST may still have been delivered, so a rare duplicate is possible on retry — the request id stays the same, so approval answers are deduplicated).

## 7b. Single-file architecture: current state and migration strategy

**Current state (1.7.0):** `agentbell.py` (~10,300 lines, ~455 KB) plus 21 test modules under `tests/` (~15,700 lines, ~725 KB; `test_agentbell.py` and the `test_audit*` files from the 2026-09 audit). When this section was first written the numbers were ~5900 lines / ~243 KB and one ~3300-line test file. The build step is `py_compile`, nothing else. Single-file distribution is the product identity — a user copies one file and runs it.

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

**Historical (written for v1.5.0).** v1.6.3 is the release on PyPI (§19)
and 1.7.0 is being prepared. Item 3 exists in part: Windows onboarding
(`py -m agentbell`), the PowerShell toast (§24), Windows hook commands (§7
item 1) and `watch` on Windows (§37). There is still no Windows installer
and no Windows service for the bot. The field test (item 1) continues in
`FIELD_TEST.md`.

v1.5.0 is the first public release; the 2-week field test (started on v1.3.1) continues against it. In order:

1. **Finish the field test** — `FIELD_TEST.md` is the checklist. Whatever it surfaces gets fixed first; that is the gate, not a feature count.
2. **End-to-end hook smoke tests** against the real agents' configs (CI matrix). §15 re-verified all twelve integrations by hand against the vendors' live docs — worth doing once, too expensive to repeat every release.
3. **Windows installer + PowerShell notification support**.
4. **Template engine** for notification bodies (turn path, duration, last-line summary from agent stdin JSON).
5. **Shell completions** for bash/zsh/fish.

## 9. Telegram interactive approval: answer daemon + file handoff (v1.1)

**Superseded in part.** Read with these sections:
- single poller: the pid lockfile is now a kernel file lock (§31);
- free-text replies and the parallel-channel free-text rule: §30 (a typed
  reply is used only when exactly one question can be on the phone);
- `agentbell watch`: §28 (no `subprocess.run`; the command keeps the
  terminal and gets each signal once);
- run durations: the start marker is per session, not per agent (§26).

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

**Superseded in part by §31.** Liveness is no longer "heartbeat age + pid
liveness", and there is no stale lock to reclaim: `bot status` probes the
kernel lock on `bot.lock` and reads the heartbeat. The heartbeat, the
`last_error` fields and the drain duty below are current.

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

**Superseded in part by §36.** Since 1.7.0 the purge also stops, disables
and deletes the bot service first; a directory set by
`AGENTBELL_CONFIG_DIR` / `AGENTBELL_STATE_DIR` loses only agentbell's own
entries; a file set by `AGENTBELL_CONFIG` is listed and removed; the Windows
`Scripts\agentbell.exe` launcher of a `pip install --user` install is
included. Marked blocks are removed only when they run an agentbell
command, and hook entries only in the shape agentbell writes (§33).

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

**Decision:** `mcp add` takes explicit client names and, without one, registers every client of this list that is installed on the machine (the others are listed as skipped): `claude`, `claude-desktop`, `chatgpt-desktop`, `codex`, `gemini`, `qwen-code`, `kimi`, `cursor`, `opencode`, `vscode`. Registration is **global** wherever the client supports it, so every repo is covered without per-project setup.

**Why ChatGPT Desktop works:** per OpenAI's docs the ChatGPT desktop app, the Codex CLI and the IDE extension *share* MCP configuration in `~/.codex/config.toml`, and the desktop app supports local stdio servers. So the Codex registration covers ChatGPT Desktop; `chatgpt-desktop` is an alias that says so out loud. ChatGPT **web** accepts remote MCP servers only — documented, not worked around (a tunnel would contradict "no public server").

**`--print`** emits the raw snippets for clients we do not write (Windsurf, Zed, LM Studio, …) rather than growing a writer per client. It prints the standard `mcpServers` JSON, the VS Code `servers` form, the Codex TOML table and, since 1.7.0, Zed's own form (`context_servers` in Zed's `settings.json`). Before that, Zed users were pointed at the `mcpServers` shape, which Zed does not read.

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

Three of them changed how the product behaves, not just how it is implemented. *(The answer contract in the first one was changed again: free text is exit 0 but `approved: false` (§22), exit 3 is a runtime error, and typed-reply routing is §30.)*

**Free-text answers are no longer collapsed.** `_parse_answer` matched a keyword *prefix*, so "yes, but use staging" became a bare `approved` and the instruction was lost. Now: an affirmation approves only when it stands alone; a negation denies even with a reason after it (fail-closed is the right direction for an approval gate, and the reason is kept); everything else is free text. The documented exit-code contract (0 approved/answered, 1 denied, 2 timeout) is unchanged — a verifier correctly refuted the related "fails open" claim as documented, tested behavior, so the README now just states the caveat for anyone chaining `ask && <command>`.

**Config-derived Telegram degrades instead of failing.** With `channels: ["ntfy","telegram"]` and no license, `notify` exited 3 and hid the fact that ntfy had delivered. The premium gate stays a permanent error inside `_publish_item_channels` (the queue relies on it to drop unlicensed items instead of retrying forever), but `send_notification` now drops telegram from *config-derived* channel lists — mirroring what `resolve_ask_channels` already did for `ask`. An explicit `--channel telegram` still fails loudly.

**The license env var no longer overrides a real build.** `check_license_key` honored `AGENTBELL_LICENSE_SECRET`, which let anyone pick the verifier's own secret and sign a key with it — a complete bypass of the paid tier in one env var. The fallback was narrowed to an unbuilt checkout (which validated no real key anyway), so the author/test workflow was untouched and shipped builds were not bypassable this way. The move to Ed25519 (§2b) later removed the underlying problem instead of narrowing it: verification uses the hardcoded public key only, and `AGENTBELL_LICENSE_SECRET` is now nothing but a *signing* seed for the author's minting tool — it cannot make an invalid key verify, which is the property `TestAuditRegressions.test_the_environment_cannot_make_an_invalid_key_verify` pins down.

Two findings are documented rather than fixed:

- **ntfy action buttons carry a credential.** A button definition travels inside the published message, so on a protected self-hosted ntfy the `Authorization` header is visible to every subscriber to the topic. Removing it would break button answers entirely. `ntfy.action_auth` now lets you give the buttons a scoped publish-only token instead of the account password, and the README says so.
- **Free-text attribution is per machine.** Pending markers live in the local state dir, so two machines sharing one topic cannot see each other's open questions. Button answers carry the request ID and are unaffected. Out of scope for a single-user tool (§10: no multi-device sync). *(Still true after §30: the one-candidate rule counts only the questions this machine knows about.)*

### 12j. `--min-duration`: the anti-spam rule

**Problem:** "finished" hooks fire per *turn*, not per *task*. With hooks installed globally, an interactive Claude Code session produces a push every time the assistant stops talking — dozens per hour. That is exactly the automatic spam the product direction rules out, and it is the fastest way to get a notifier uninstalled.

**Decision:** the installed Claude Code and Codex "finished" hooks carry `--min-duration 60`. A turn whose measured duration is under the threshold is skipped and logged as `hook.skipped_short`. Two deliberate exceptions: **failures always notify** (a failure matters however fast it happened), and an **unknown duration always notifies** (agents without a start marker — Gemini, OpenCode, custom scripts — keep v1.2 behavior; fail-open, never silently swallow).

**Why 60 s:** below a minute you were almost certainly still watching. Above it you probably switched tasks — which is the entire premise of the tool.

**Why a flag baked into the hook command and not a config key:** it is visible where the behavior lives (`grep min-duration ~/.claude/settings.json`), editable without a new CLI verb, and per-agent — Codex and Claude Code can differ without a config schema for it. The trade-off is that changing it means editing the hook. Until 1.7.0, re-running `hooks install` (also `init` and `hooks install all`) quietly reset an edited value to 60. Since 1.7.0 a reinstall keeps the value found in agentbell's own `run_completed` hook for Claude Code, Qwen Code, Codex and Kimi Code (§33). A value of 60 cannot be told apart from the old default, so if the default ever changes, installs that hold 60 keep 60.

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

*(Superseded in part by §31: the unit runs `agentbell_command()` — the
interpreter plus `agentbell.py` from a checkout, because that file is not
executable — with `Type=exec`, pins `AGENTBELL_CONFIG` and
`AGENTBELL_STATE_DIR`, and uses `enable` plus `restart`. A service that did
not start, including the no-systemd case, now exits 1.)*

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
- *(1.7.0)* An MCP `notify` call carrying `agent` is reported as such
  ("N of them MCP notify call(s)", `notify_calls` in `--json`). It still
  verifies an MCP-only host, but an agent with an installed hook
  integration needs a real lifecycle event.
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
were installable but invisible to removal and repair. *(Superseded in part by
§33: since 1.7.0 the stem check is only the first step, and an entry is
agentbell's only in the exact shape and place agentbell writes it.)* The
matcher now parses the command and compares the first token's stem — and deliberately leaves
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
of the start marker because the marker was per agent then, and parallel OpenCode
sessions would overwrite each other's start. (Markers are per session since
§26; the plugin still passes `--duration`. In 1.7.0 it also ignores the
prompt OpenCode re-sends after a turn, which had made some short turns
look long.) Unknown duration (plugin loaded
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

## 18. v1.6.3: disposition of the 2026-08-22 review (2026-09-03)

Every finding was checked again against v1.6.2 before edits. Twelve remained
fully present. The Windows parser finding was partial: current quoted commands
parsed correctly, but a legacy bare Windows path lost its backslashes and was
therefore invisible to ownership/self-heal logic. It is hardened as well.

| # | Verified result | Decision |
|---|---|---|
| 1 | two processes could claim one reply | atomic cross-process lock; failed claims are history + doctor WARN |
| 2 | outdated Aider was `installed: false` plus repair notice | existing stale wiring is installed and update-needed |
| 3 | Windows config permissions always read OK | WARN that ACL is unverified; show `icacls` inspection |
| 4 | insecure-ask warning was process-global once-only | warn for every sensitive ask |
| 5 | deploying/deleting/publishing were missed | extend the narrow grammar |
| 6 | quoted Windows paths worked; bare paths did not | preserve backslashes with a non-POSIX parse fallback |
| 7 | `--project` still aggregated global history | record normalized hook project and filter observations |
| 8 | wrapper read installed but install added four owned hooks | label/warn wrapper and refuse the second mechanism |
| 9 | manifest omitted ask exit 3 | add the runtime-error exit |
| 10 | two verify tests reused suite-global `claude` history | mock empty history in both tests |
| 11–12 | two ntfy readers expressed the 90 s margin separately | one named constant |
| 13 | verify parsed `AGENTS.md` three times | one status read reused by the repair notice |

### 18a. A reply claim is correctness state, not best-effort bookkeeping

The answer claim differs from hook-push dedupe in §17a. A duplicate push is
noise; one approval reply authorizing two concurrent asks can trigger two
actions. `mkdir` is the portable stdlib atomic primitive used as a short-lived
per-log lock. A stale empty lock can be reclaimed after 30 seconds. The
existing bounded append log remains the durable record, so the change is
small and compatible with existing state.

If locking, appending or trimming fails, that reply is not offered to the
waiting ask. The waiter records `answer_claim_failed`, retains an in-process
answer-channel error for stderr, and `doctor` reports recent failures. This is
the required visibility rule: fault tolerance must not turn an uncertain
claim into an approval.

### 18b. Project history is explicit and conservative

Hook records from v1.6.3 carry a normalized absolute `project` derived from
`--cwd` or the hook process working directory. A project report includes that
directory and descendants. Legacy history has no unambiguous structured
project field; parsing the human message would couple diagnostics to display
text and could misattribute records. Therefore `verify --project` excludes
legacy unscoped records, while a verify without `--project` remains global.

### 18c. Wrappers remain user-owned, but no longer invisible

Section 16j's ownership boundary stands: `bash -c '... agentbell hook ...'`
is not an entry agentbell may rewrite or remove. Treating it as simply
installed hid an important distinction, while treating it as absent made
self-heal add a second mechanism. The new `user wrapper` state is installed
for truthfulness, WARNed in status/doctor/verify, and causes `hooks install`
to stop with a note until the user removes or updates it. No shell wrapper is
parsed deeply or claimed as agentbell-owned. *(§33 widens "user-owned" to
every command that is not in agentbell's exact generated shape, such as a
hook with flags agentbell does not write.)*

### 18d. Windows config ACLs are not inferred from POSIX mode bits

Python's portable `st_mode` view does not prove which Windows principals can
read a file. A universal OK was therefore a false security claim. Adding a
platform-specific ACL implementation would exceed the stdlib-only thin CLI
for this patch; `doctor` now emits a visible WARN and an `icacls` inspection
command. This is deliberately an honest unknown, not a claim that the ACL is
unsafe or safe.

## 19. v1.6.3: PyPI distribution and trusted release (2026-09-03)

**Decision:** `pipx install agentbell` is the primary end-user installation
path; `pip install agentbell` is supported for an existing virtual
environment. `install.sh` remains the checkout/development path and retains
its fallbacks for machines without pipx. The runtime remains one stdlib-only
module with one console entry point; packaging tools are release-time tools,
not runtime dependencies.

Releases are built on a `v*` tag in GitHub Actions and published from a
separate job through PyPI Trusted Publishing. The job uses the GitHub
environment named `pypi` and `id-token: write`; no long-lived PyPI token is
stored in the repository. The build job checks that the tag equals the
`pyproject.toml` version, runs the standard package build and `twine check`,
checks the artifact contents, then hands those exact artifacts to the publish
job. A tag/version mismatch or an archive containing tests, internal files,
the signing secret, extra Python runtime files or a missing console entry
point fails before publication. `MANIFEST.in` excludes the tests that
setuptools otherwise adds to a source distribution by default.

**Evidence boundary:** preparing a workflow and successfully installing a
locally built wheel does not prove that PyPI serves the package. Until a fresh
environment can run `pipx install agentbell`, report the released version and
execute `agentbell doctor`, the README and field-test checklist label the
PyPI path as in preparation. Publication proof is recorded after the tag
workflow succeeds, not inferred from configuration.

*(Status: the "in preparation" label was removed in 4a92ae3 on the
strength of a `pip install agentbell` from public PyPI into a fresh venv
(`FIELD_TEST.md`, 2026-09-05). `pipx install agentbell` from public PyPI was
not re-run in that pass; the only pipx evidence is the isolated local-wheel
test from 2026-09-03. The pipx half of this boundary is still open.)*

The existing purge ownership model remains unchanged. A pipx installation is
detected with `pipx list` and removed by calling `pipx uninstall agentbell`;
agentbell does not unlink pipx's launcher or edit its managed environment by
hand. Hooks continue to store `agentbell_binary()`'s absolute launcher path,
so GUI clients do not depend on inheriting the shell `PATH`.

## 20. Approval replies fail closed (2026-09-22)

**Superseded in part by §22, §27 and §30.** This section is the first pass.
Two statements below are not the current contract. Free text is not
`approved: true` (§22). Telegram does not use a two-second clock window
(§22); a free-text reply counts only when its message id is newer than
the question. The denial list was extended again in §27, and an exact
yes-button label is checked before that list. Read §22 and §27 for what
the code does now.

**What was wrong.** Three separate holes turned a non-approval into a yes:

- `_parse_answer` only knew a short English list. The no button's own label
  (`Abort`, the word the hint tells the user to type), `Nein`, `not yet`,
  `don't`, and 👎 came back as free text, and `run_ask` reported
  `approved: true` (exit 0). `ask_approval` returns that same object, so a
  model gating a destructive action saw an approval.
- `ApprovalWaiter._prime` swallowed one failed poll and then waited with an
  empty seen set. The next poll, a few seconds later, delivered a `yes`
  that was already on the response topic and attributed it to the new
  question. That breaks the rule in §12b.
- The Telegram backlog guard subtracted 60 seconds. A restarted bot replays
  updates, and a `yes` from half a minute before the new question was
  accepted. The v1.3 changelog already said replies that predate the
  question are rejected; the code did not do that.

**Decision.** Negations and the no button's label deny, including a reason
after them (`Abort, tests are red`). The yes button's label approves only
when it is the whole reply, same rule as bare `yes`. A failed prime is
retried three times and then `ask` raises (exit 3) instead of waiting.
Telegram allows `TG_REPLY_CLOCK_SKEW_SECONDS` (2) for whole-second dates
and a clock that is a moment ahead, and rejects anything older.

**Left unchanged on purpose.** *(Superseded by §22. Do not restore this.)*
Free text that is not a negation is still an answer: exit 0,
`approved: true`, the text in `answer`. That is the "Which environment?" /
`staging` contract from §12i and the README. A strict gate still has to
read `approved` together with `answer`; exit 0 alone has never meant
"bare approval". Collapsing every free-text reply to `approved: false`
would break that contract, so it was not done. §22 did set `approved`
false for free text. Exit 0 for `staging` stayed. The exit code was the
contract this paragraph was protecting, not the boolean.

## 21. Host config files are not ours to trim (2026-09-22)

**What was wrong.** Three writers treated a file we only partly own as if
we owned all of it.

- `_replace_toml_block` replaced everything between the agentbell markers.
  Codex stores `[hooks.state]`, `[tui]`, `[notice.*]` and `[plugins.*]`
  and writes them between those markers. `hooks install` and `uninstall`
  deleted them. A hook command that is not ours, sitting in the same
  region, went with them.
- `_codex_insert_features_flag` concatenated `features.hooks = true` onto
  the last line when the file had no trailing newline. Codex then refused
  the file, and uninstall's line-anchored removal could not see the glued
  flag, so it could not repair it.
- `_write_text_atomic` opened `path + ".tmp"` with a plain `open`. A repo
  that ships `AGENTS.md.tmp` as a symlink to `~/.bashrc` was written
  through. The same rewrite also created the temp file at the umask mode,
  so a Codex or Kimi `config.toml` that was `0600` came back `0644`.

**Decision.** Foreign TOML tables between the markers are moved to after
the end marker on install, and left in the file on uninstall. A table is
foreign when its command is not our hook; a header-only parent we also
emit stays with the table that follows it, so a user's `[[hooks.Stop]]`
is not beheaded. The features flag is always its own line, and both
install and uninstall recognize a copy glued onto the previous line.
The temp file is created with `O_EXCL` and `O_NOFOLLOW`; an existing
symlink at that name is unlinked (the link, not its target) and a real
file is created. The replacement keeps the mode the destination already
had. A Codex block with no end marker is left untouched instead of
raising. *(§27 changed the symlink rule for home-directory configs, and §34
states the general rule for bytes agentbell does not own.)*

## 22. Review of the 2026-09-22 fixes (2026-09-22)

**Superseded in part by §30.** The `approved` contract below is current.
Two details are not: `wait` (and `warte`, `später`, `hang on` and other
postponements) now deny as a leading phrase, so `wait for CI, then ship`
exits 1; only `not` and `nicht` still deny only as the whole reply. And a
typed Telegram reply is no longer placed by message id alone: it is used
only when exactly one question can be on the phone, and the message id
only decides whether it predates that question.

The first pass (§20, §21) was re-checked against the real behavior. Five
corrections:

**`approved` means an explicit yes.** §20 left free text as
`approved: true` so that `staging` would stay an answer. That made the
heading "fail closed" false: replies such as `Stopp`, `noch nicht`,
`nö`, `not now`, `please don't`, `absolutely not`, `wait`, ❌ and 🛑
still came back as approvals, and a word list cannot cover the rest.
`cmd_ask` does not look at `approved`. It exits 0 unless the reply was
denied or timed out. So `staging` can stay exit 0 with the text on
stdout while `approved` is false. `yes, but use staging` is the same:
the instruction is kept, and it is not a bare approval. Denial phrases
still exit 1, because `ask && deploy` only sees the exit code. `not`
and `wait` deny only when they are the whole reply; as a prefix they
would swallow real answers.

**A dead ntfy channel must not cancel Telegram.** The prime check from
§20 raised before any other channel was started, so an unreachable
response topic turned a two-channel ask into exit 3 and never sent the
Telegram question. Prime now runs beside the Telegram send. If it
fails, ntfy is not published and not waited on. The ask fails only when
nothing else can carry it. ntfy alone still refuses to wait.

**Telegram replay is ordered by message id.** The two-second clock skew
in §20 rejects a live reply when the local clock is ahead of Telegram
and accepts an old one when the clock is behind. That repeats the
mistake §16i already fixed for ntfy. The question's Telegram message id
is stored when the send succeeds. A free-text reply counts only when
its id is greater. Buttons were already bound to the approval id.

**One chunk of lookahead is not enough.** §21 keeps a header-only
`[[hooks.Stop]]` with the table that follows it. Our own hook is that
next table, so a user's `[[hooks.Stop.hooks]]` further down the same
group lost its parent on uninstall and `hooks.Stop` became a table.
The parent stays if any later child of that header is not our hook.

**The MCP remover was a second temp-file writer.** §21 closed the
symlink hole for rule files. `_remove_mcp_server_key` still opened
`path.tmp` with a plain `open`, so `mcp.json.tmp` pointing at another
file was followed, and the rewrite also widened `0600` to `0644`. It
now uses `write_json_atomic`, which already creates with `O_EXCL` and
keeps the existing mode.

**Kimi without markers is not the Kimi case §21 fixed.** The marker
rules apply only while the markers are present. A config that still
contains the hook commands but no longer the comments is reported as
installed, and install refuses to append a second block. Uninstall
does not guess which lines to delete. *(§27 revises the last sentence:
a command that matches `_is_our_hook_command` exactly is not a guess,
and uninstall removes those tables.)*

## 23. A dropped connection is a retry, not a crash (2026-09-22)

**What was wrong.** `http_request` caught `URLError` and `socket.timeout`.
urllib wraps some socket errors in `URLError` and does not wrap others.
`getresponse()` sits outside that wrapper, and `resp.read()` is ours.
`RemoteDisconnected`, `ConnectionResetError` and `IncompleteRead`
therefore left the function as themselves. They are not
`TransientError`, so nothing retried them and nothing queued them.
`cmd_hook` then swallowed the crash to protect the agent's turn, which
deleted the push with no history line. `run_watch` never got back to
the command's exit code. `doctor` and the bot loop only catch
`RuntimeError`, so they died. The approval stream reader caught
`OSError` but not `IncompleteRead`, so one short read ended that thread.

**Decision.** Both the open and the body read treat `OSError` and
`http.client.HTTPException` as `TransientError`. A 4xx (except 408 and
429) stays permanent, including when the error page itself cannot be
read. Subscribe raises `RuntimeError` for the same failures so the
stream loop reconnects; that loop also catches a short read on the
body. Callers that already retry or queue a `TransientError` need no
new policy.

*(Since 1.7.0 a hook failure that still gets through is not swallowed
silently either: `cmd_hook` exits 0 but writes a `hook.error` record and one
stderr line, §32.)*

## 24. Windows toast text is data, not script (2026-09-22)

**What was wrong.** The Windows notification built a PowerShell `-Command`
and wrapped the title and message in single quotes, doubling only the
ASCII apostrophe. Windows PowerShell 5.1 treats the typographic
apostrophes U+2018–U+201B as quotes. A message containing `’` ended the
string. The toast failed, and text after that character was executed.
Shown on PowerShell 5.1 with a crafted message.

**Decision.** The script is a constant. Title and message are placed in
`AGENTBELL_OS_TITLE` and `AGENTBELL_OS_MESSAGE` on the child environment,
and the script reads those. A NUL byte is stripped first, because an
environment block cannot hold one. No quoting of the notification text
remains.

## 25. Re-init keeps the ntfy server you already have (2026-09-22)

**What was wrong.** `init` is the documented way to add Telegram later.
The server prompt defaulted to `https://ntfy.sh`, and a non-interactive
run applied that default without asking. The topic was rolled again.
`ntfy.auth` was left as it was. The test push at the end of `init` then
sent the self-hosted password to ntfy.sh.

**Decision.** The server and topic already stored are the defaults.
Pressing Enter, or running `init --non-interactive` without `--server`,
does not move them. If the server does change and this run did not pass
`--ntfy-auth`, the saved password is not reused: an interactive run asks
for the new server's credential, and a non-interactive run clears it
before anything is published.

## 26. Start markers are per session (2026-09-22)

**What was wrong.** `hook started` wrote `<state>/runs/<agent>.json` and
`run_completed` consumed that one file. Two Claude sessions running at
once share the agent name. The second start overwrote the first. The
long turn then measured itself from the short turn's start, fell under
`--min-duration`, and was dropped. The short turn found no marker and
notified, because an unknown duration always notifies.

**Decision.** The file is keyed by `session_id` (or `sessionId`) from the
JSON the host passes on stdin. A session id wins over the directory, so
two sessions in one repo stay apart. With no session id, the working
directory (`--cwd` or the payload's `cwd`) is the key. With neither, the
old per-agent file remains, so a hand-run `hook started` / `hook
run_completed` pair still matches. The stdin read never blocks: a
terminal is ignored, and a pipe is read only when data is already
waiting.

## 27. The second review of the 2026-09-22 fixes (2026-09-23)

The fixes through §26 were re-checked. Eight gaps remained. None of
them change the exit-code contract from §22: free text that is not a
denial is still exit 0, and `approved` is still false for that reply.

**The denial list had holes the exit code falls through.** `ask &&
deploy` only sees the exit code. `not now` denied and `nicht jetzt`
did not; `Not` denied and `Nicht` did not; 🚫 denied and ⛔ did not.
`nicht jetzt`, `bloß nicht`, `bloss nicht`, `halt`, `hold on`,
`later` and `moment` are leading denials. `nicht` denies only as the
whole reply, same as `not`, so `nicht staging` stays an answer. ✋ and
⛔ are leading marks. The list is still not complete. `examples/custom-agent.sh`
no longer shows a production deploy gated on exit 0; it reads
`approved` from `--json`. A `--strict` exit code for every free-text
reply was not added: §22 already decided that reply stays exit 0.

**An exact yes label wins over the denial list.** `--yes-label "Stop
it"` tells the user to reply `Stop it`. That reply matched the leading
denial `stop` and exited 1. The label alone is checked first. `Stop it
now` is not the label, so it still denies.

**Kimi uninstall lied when the markers were gone.** Status saw the
commands, the removal plan said it would remove them, and the result
said they were already gone. The three `[[hooks]]` tables stayed, and
Kimi kept calling a binary that was no longer there. Uninstall now
removes a `[[hooks]]` table only when every command is exactly ours
(`_is_our_hook_command`). A wrapper that merely mentions agentbell is
left, and the note says so. Install still refuses to append a second
copy. The machine this was written on already has its markers back;
this is for the next time Kimi strips them.

**`ntfy.action_auth` is the password's neighbor.** §25 drops `ntfy.auth`
when the server changes. The button token was left behind and the next
`ask` published it on the new server. `init` now clears it on every server
change, including when `--ntfy-auth` supplies a new password. The same
server keeps it. *(As written here this held for `init` only:
`config set ntfy.server` still kept both credentials. §35 makes it one rule
for both paths and defines "the same server".)*

**A `#` inside quotes is not a TOML comment.**
`[projects."/home/u/C#/app"]` was cut at `#`, not seen as a header, and
deleted with the hook table above it. Comment stripping now tracks
basic and literal strings. A `#` outside quotes is still a comment.

**A config symlink is the user's file.** `_write_text_atomic` replaced
the symlink with a regular file, so uninstall updated a copy and the
dotfiles repo kept the hooks. The rewrite now replaces the file the
link points at. A symlink at the temp name is still unlinked rather
than followed. Rule files in a repository still refuse to write through
a symlink; that threat is a repo choosing the destination, not a
home-directory config the user linked on purpose.

**Start markers older than the read window are deleted.** §26 keys one
file per session. A session whose last turn never hits Stop left that
file forever. Files older than the one-day read window are removed when
a marker is written or read. `runs/last-sent.json` is not a start
marker and stays. A turn already longer than that window was not going
to get a duration; the read ignored it.

**`approved: false` is a behavior change, not a patch note.** JSON, MCP
and the webhook changed for every free-text reply. The changelog
records it under Changed. The version string stays 1.6.3 until a real
release, and that release should be a minor version. *(It is being
prepared as 1.7.0.)*

## 28. `watch` keeps the terminal and passes each signal on once (2026-09-23)

*(Rewritten for 1.7.0. The first version of this section described a
design that was replaced in the same release cycle; see "What was tried
first".)*

**What was wrong.** `run_watch` used `subprocess.run`. On Ctrl-C that
helper waits 0.25s and then sends SIGKILL. A migration that traps
SIGINT to roll back or commit the current step was killed instead.
The completion push never ran, because the exception left `run_watch`
before `send_notification`. The same happened for SIGTERM: the process
died and the child was left without a push.

**What was tried first.** The command was started in its own session,
and `watch` forwarded SIGINT and SIGTERM to that process group. That
fixed Ctrl-C and broke everything interactive: the command no longer
owned the terminal, so sudo, ssh and gpg password prompts failed and
Ctrl-Z did nothing. Ctrl-\ and closing the terminal still killed
`watch` before the push. It was replaced by the design below.

**Decision.** The command runs in `watch`'s own process group, which is
the terminal's foreground job, so it keeps the terminal: prompts work
and Ctrl-Z suspends it. `watch` catches SIGINT, SIGQUIT, SIGHUP and
SIGTERM (and SIGBREAK on Windows) and waits for the command however long
it takes. There is no 0.25s kill. The push is sent afterwards with the
command's status. A death by signal becomes 128+N, which is what a shell
reports; a command that handles the signal and exits 0 is a success. A
signal that was ignored when `watch` started (nohup, a background job of
a script) stays ignored, so the command inherits that.

The rule is that the command gets each signal once. A second interrupt
is how tools such as Terraform abandon a graceful shutdown. The terminal
already sends Ctrl-C, Ctrl-\ and the hangup to the whole foreground job,
command included, so `watch` must not send those again. A signal meant
for `watch` alone has to be passed on. Only the command's pid is
signaled, never the group, which can hold the rest of a pipeline.

- **Linux.** `watch` blocks these signals while the command runs and
  takes them with `sigtimedwait`, which names the sender.
  - Raised by the kernel for a key or a hangup: not passed on, the
    command got it too. A hangup is passed on when `watch` is the session
    leader (`ssh -t host agentbell watch …`, `docker run -it`) or had no
    terminal when it started (cron, CI, supervisord).
  - Sent by a process in `watch`'s own group: that process signaled the
    group (`timeout` without `--foreground`, the command's own
    `kill 0`), so it is not passed on. The exception is `watch`'s parent
    when it is not a group-killing `timeout`: `uv run`, `uvx`, a nested
    `watch`, a wrapper script or `timeout --foreground` relays to its
    child alone, so SIGTERM is passed on, and SIGINT/SIGQUIT are passed on
    unless `watch` is the terminal's foreground job.
  - Any other sender is taken to have signaled `watch` alone and is
    passed on once: `kill -INT <watch pid>` from an IDE stop button or
    pexpect reaches the command even while `watch` is in the foreground.
    SIGHUP keeps the hangup rule above whoever sends it.
- **macOS and other non-Linux POSIX systems.** No `sigtimedwait` sender
  (and different `si_code` values), so a heuristic: SIGINT and SIGQUIT
  count as key presses while `watch` is the terminal's foreground job,
  SIGTERM is always passed on, and the hangup rule is the same as on
  Linux.
- **Windows.** Nothing is passed on. Every process on the console gets
  Ctrl-C and Ctrl-Break, and no other signal reaches `watch` from
  outside. `watch` survives them and sends the push.

Once the command has ended, SIGINT, SIGQUIT or SIGTERM during a push that
hangs stops the send: the push goes to the offline queue, `watch` exits
with the command's code and says so on stderr. A hangup does not stop the
send. On Windows nothing changes during the send, because the Ctrl-C that
ended the command may reach `watch` only then. A push that fails never
changes the exit code; it costs one stderr line. With an unreadable
config the command still runs, without a push, and its exit code is
returned.

**Known residual cases.** None of these has a portable fix; they are
documented instead.

- A group kill sent from **outside** `watch`'s group reaches the command
  twice: once directly and once passed on. Examples: bash `kill %1`,
  `kill -- -PGID`, systemd `KillMode=control-group`. The sender info
  cannot tell it from a kill of `watch` alone; sudo has the same rule. A
  command that treats a second SIGTERM as "force quit" (Terraform) skips
  its graceful shutdown then. A SIGTERM sent to `watch` alone reaches the
  command once (tested).
- A parent shell script whose trap runs `kill 0` looks like a relaying
  parent, so the command gets that SIGTERM twice.
- `timeout --foreground` is recognized only when `timeout` is `watch`'s
  direct parent and the option is written on its own (`-f`,
  `--foreground` or an unambiguous prefix). A bundled short option such
  as `-vf` is read as a group-killing `timeout`, so the signal, which
  only reached `watch`, is not passed on and the command keeps running.
- A SIGINT or SIGQUIT that a relaying parent passes on while `watch` is
  the terminal's foreground job is taken for a key press and dropped:
  `kill -INT <wrapper pid>` from another terminal does not reach the
  command.
- macOS: `kill -INT <watch pid>` while `watch` is in the foreground is
  not passed on, and a group SIGTERM (including a plain `timeout`)
  reaches the command twice.
- Ctrl-C pressed exactly while the command is being started may not
  reach it. `watch` survives and pushes; pressing Ctrl-C again works.
- Linux: if the parent left SIGCHLD ignored, `watch` notices the exit
  only at the one-second `sigtimedwait` timeout.

**Evidence boundary.** The pty tests (Ctrl-C, Ctrl-\, Ctrl-Z, a
`/dev/tty` prompt, SIGTERM, `timeout`, relaying parents) were run on
Linux and WSL2; macOS runs them only in CI. Real interactive use — a sudo
prompt, closing a terminal window — is not field-tested yet and has rows
in `FIELD_TEST.md`. Windows argument handling for `.bat`/`.cmd` tools is
§37.

## 29. Five smaller holes from the same audit (2026-09-23)

**23:59 is inside a window that ends at 23:59.** End times are exclusive:
`13:00-14:00` is quiet through 13:59 and loud at 14:00. `23:59` is the
last minute the parser accepts, so that exclusive rule dropped the last
minute of `00:00-23:59`. A window ending at 23:59 runs until midnight.
Nothing else changed.

**An HTTP hook is not a crash.** Install compares hook entries by putting
them in a set. `headers` is an object and `args` can be a list, and
neither is hashable. The comparison now freezes JSON values into tuples.
The user's hook is left in place.

**The bot lock has to name one process, not a pid.** *(Superseded by §31:
the start-time field was replaced by a kernel file lock. The SIGTERM part
stands, and a clean stop now exits 0.)* SIGTERM killed the
daemon before the lock was removed. The next start treated a live pid as
"the bot is running" even after that pid had been given to another
program. The lock stores the Linux start-time field, and a mismatch is a
stale lock. SIGTERM now unwinds the daemon so the lock is removed. A host
without `/proc` still has only the pid, which is what it had before.

**Secrets stay out of `config show` and out of token errors.**
`ntfy.action_auth` is redacted like the other credentials. A Telegram
token is stripped of surrounding whitespace, so a pasted newline still
works. Whitespace or a control character inside it raises `invalid bot
token` without the text that was pasted. The URL scrubber redacts from
`/bot<id>:` up to the next slash, so a space or CR in the middle cannot
leave a tail.

**The flaky Telegram tests counted too late.** They sampled the mock's
request list after the ask thread had started, so a fast `sendMessage`
was treated as "already there" and the test waited 20 seconds. The count
is taken before the thread starts.

## 30. A typed reply answers one question or none (2026-09-23)

**What was wrong.** A typed reply carries no question id. It went to the
newest open ask (§4b, §9), later to the newest question sent before the
reply, ordered by Telegram message id or ntfy server time (§22). Both
rules approved the wrong ask in reproducible cases. A newer question
whose send failed, was retried or was still in flight could sit anywhere
on the phone. A question that had just timed out was still on the screen,
and a "yes" typed under it went to an older ask. Marking uncertain sends
and same-second ties only moved the problem.

**Decision.** A misrouted approval is worse than a lost reply: a lost one
costs a tap on a button, a misrouted one runs the wrong command. A typed
reply is used only when exactly one question can still be on the phone.

- An ask is open exactly while its process holds a kernel lock on its
  pending marker (one per channel), the same lock as the bot's (§31):
  `flock` on POSIX, `msvcrt.locking` on Windows. Readers probe it without
  waiting and drop the probe at once. No clock decides whether an ask is
  open. A clock that jumps (a laptop that slept, a publish slower than
  the old 60 s slack) used to delete a waiting ask's marker: its button
  said "expired" and a typed "yes" went to a newer ask. A killed or
  crashed ask loses the lock with its process, so its marker cannot stay
  open.
- The candidates are every open ask plus every ended ask whose question
  may still be on that phone. When an ask ends, each marker becomes a
  tombstone (`closed`, `answered`, `expires`) before the lock is
  dropped. `answered` is true only on the channel that carried the
  answer: an ask answered on Telegram still has its question and
  buttons on ntfy. An answered tombstone does not count. Any other
  tombstone (no answer, a failed send, a kill, an answer on the other
  channel) counts for `PENDING_TOMBSTONE_GRACE_SECONDS` (60 s) after the
  ask ended, then it is deleted. A marker whose ask died without closing
  it is closed by the first reader that finds its lock free, and its 60 s
  start then. `ask` turns SIGTERM and SIGHUP into a normal exit
  (128 + signal), so an agent's tool timeout closes the question at once.
  The wall clock only prunes tombstones nobody holds.
- A question the server refused (every attempt answered with an HTTP
  error status other than a gateway's 502/504, or Telegram `ok: false`)
  never reached the phone: its marker on that channel is deleted. A
  timeout or a dropped connection proves nothing, since the server may
  have stored the question, so that marker stays with an unknown place.
- With exactly one candidate, the reply is used when that ask is still
  open and its question is known to be out before the reply: a greater
  Telegram message id, or an ntfy server time that is not earlier
  (whole seconds, so the same second counts). A reply older than every
  candidate's question is a replay and stays stale.
- In every other case the reply is refused and recorded as
  `stale_answer` with the channel, the reason and whether the notice
  went out. The person who typed it is told: the Telegram bot answers
  the message with `Your reply "…" was not used: <reason>. Please tap a
  button, or answer with Reply on the question.`, and on ntfy the
  waiting ask publishes a "Reply not used" notification (priority high,
  up to 60 characters of the reply) to the main topic. A starting bot
  first reads the chat backlog without waiting, until a poll comes back
  empty. Nobody waits on an answer to that backlog, so a reply refused
  there is only recorded. After it the bot sends at most one notice per
  reason a minute (`BOT_NOTICE_INTERVAL_SECONDS`); a reply it does not
  announce is recorded with `notice: "not sent: …"`.
- Explicit routes are unchanged and always work: the buttons, a typed
  `APPROVED <id>` / `DENIED <id>` with the full id (any letter case),
  and Telegram's Reply on the question. A reply that names a question
  that has ended is not used. `approve 2` is not an id; it is free text.
- A marker that cannot be read is read once more after 50 ms (it may be
  caught mid-rewrite). If it still cannot be read it counts as a
  question of unknown place: while its lock is held, and for 60 s after
  its last write once nobody holds it; then it is deleted. On ntfy a
  typed reply waits while the one candidate is still publishing its
  question; the publish ends with a time, an unknown place or a deleted
  marker.
- A server that sends no message times (real ntfy does) gets no typed
  replies at all. Buttons still work.

The reply text is read more strictly in the same release. Smart
punctuation is mapped to ASCII first, so `yes… wait` and `ok — later`
read like `yes... wait` and `ok - later`. Postponements deny, also with a
reason after them: `wait`, `warte`, `später`, `hang on`, `one moment`,
⏸ ⏳ ⌛. A yes followed by a denial or a postponement denies (`yes, but
wait`, `ok, later`, `👍⏳`), which also catches benign `yes, no problem`;
that errs on the side of not running the gated command.

**Cost.** Fewer typed replies are accepted. With two asks open, or for
60 s after an ask ended without an answer on that channel, typed text is
refused with a notice. A "yes" typed under an ended question more than
60 s after it ended can reach a later single open ask; so can a
duplicate "yes" meant for an ask that was already answered on that
channel. Excluding them is the price of not blocking every reply after
every ask. A marker needs a filesystem with `flock` (not some network
mounts): where it cannot be locked, `ask` fails with that error, like
the bot. An ask started by an earlier version holds no lock and counts
as ended once a new reader sees it; its markers still carry `expires`
for a pre-1.7 bot that has not been restarted. Markers are local, so
two machines sharing one topic still cannot see each other's asks
(§12i).

## 31. The bot lock is a kernel lock (2026-09-23)

**What was wrong.** `bot.lock` held a pid, later a pid plus the Linux
start time (§29). A killed bot left the file behind, a reused pid read as
a running bot (macOS has no start time), two bots started at the same
moment could both take the lock, and a stopping bot could delete a lock
another bot had just taken. The service had its own problems: SIGTERM
ended the bot with 143, so `systemctl --user stop` left the unit failed
and `Restart=on-failure` started it again; from a checkout the unit ran
the non-executable `agentbell.py`; and `uninstall` left the enabled unit
restarting a deleted binary every 10 seconds.

**Decision.** The bot holds a kernel lock on `bot.lock` for its whole
life: `flock` on POSIX, `msvcrt.locking` on Windows (on a byte far past
the pid record, so the file stays readable). The kernel drops it however
the process ends, SIGKILL and power loss included, so it never goes
stale, and a pid that now belongs to another program cannot hold it. The
pid in the file is only for people. `bot status`, `doctor`, `uninstall`
and the button decision in `ask` probe with a non-blocking shared lock
(Windows has none: an exclusive probe, tried once more after 50 ms). A
starting bot waits up to one second for a probe to finish. A clean stop
empties the file and leaves it; deleting it would let a start that had
just opened it lock a file nobody else can see.

SIGTERM is handled like Ctrl-C: release the lock, exit 0. A stopped
service stays stopped; a crash is still restarted. The launchd job uses
`KeepAlive` with `SuccessfulExit=false` for the same reason.

`bot install-service` writes a unit that runs `agentbell_command()`
(interpreter plus script from a checkout) with `Type=exec`, so a command
that cannot start fails the install instead of looping. It pins
`AGENTBELL_CONFIG` and `AGENTBELL_STATE_DIR`, because a service does not
see the shell's variables, and refuses a license key that exists only in
`AGENTBELL_LICENSE`. It uses `enable` plus `restart`, so a running bot
picks up the new unit. A service that did not start (including a machine
without systemd, where it prints a `nohup` line) exits 1. `uninstall`
stops, disables and deletes the service first (§36).

**Cost.** A filesystem without `flock` (some network mounts) makes the
bot refuse to start, with the error shown. A bot started by an earlier
version holds no kernel lock and is invisible to the new CLI until it is
restarted. `Type=exec` needs systemd 240; older systemd treats it as
`simple`. Neither service manager was run for real: the unit was checked
with `systemd-analyze verify` and a mocked `systemctl`, the plist with
`plistlib` and a mocked `launchctl`. The field test has rows for it.

## 32. A hook has a 6-second send budget (2026-09-23)

**What was wrong.** A hook send was up to three tries of 5 s each plus 1 s
and 2 s of backoff, about 18 s in the worst case. Kimi Code kills a hook
after 10 s (agentbell writes `timeout = 10`), Gemini CLI after 15 s. A
push that was still retrying died with the hook, unrecorded. A socket
timeout does not bound a stalled DNS lookup or a server that sends one
byte at a time, and a hook that crashed was swallowed without a trace.

**Decision.** All sending in one hook — retries, the queue drain and the
deferred flush after it — shares a wall-clock budget of
`HOOK_SEND_BUDGET_SECONDS = 6`, below Kimi's 10 s with room for the
interpreter to start. A backoff pause that would cross the deadline is
skipped. Each try runs in a helper thread and is given up 0.5 s after its
own timeout, so the worst case is about 7 s. What the budget cannot
deliver goes to the offline queue (history `queued`) and is delivered by
the next successful send, `queue flush` or the bot. The OS notification
helper is capped by the remaining budget as well. A hook still always
exits 0; a failure writes a `hook.error` record (agent, event, error,
project) and one stderr line, and `verify` counts it as an event that
reached no channel.

**Cost.** On hosts whose hooks run async with long timeouts (Claude Code,
Codex, Qwen Code), a flaky network now queues the push after about 6 s
instead of retrying for up to 18 s. A try that was given up can still
land late; if the server accepted it, the queued retry duplicates the
push. That duplicate already existed for any socket timeout after the
request was sent (§7 item 8).

## 33. A hook entry is agentbell's only in the exact shape it writes (2026-09-23)

**What was wrong.** Install, reinstall and uninstall treated every
command whose first word was agentbell's binary as agentbell's own
(§16j). Hooks the user wrote were rewritten or deleted: a `Notification`
hook with matcher `permission_prompt` that called `agentbell hook`, a
wrapper such as `afplay …; agentbell hook …`, a hook with an extra
`--priority high`. A tuned `--min-duration` went back to 60 on every
reinstall (§12j).

**Decision.** An entry belongs to agentbell only when both hold:

1. its command is exactly the generated shape — an agentbell binary at
   any path (or any interpreter followed by `agentbell.py`), `hook
   <event> --agent <slug>`, and only the flags agentbell writes
   (`--silent`, `--min-duration N`);
2. it sits where agentbell writes that command: the same event and
   matcher in a JSON settings file, the same table, event and matcher in
   a Codex or Kimi `config.toml`.

Everything else is the user's. Reinstall and uninstall leave it.
`hooks uninstall` says how many such commands it kept, and `uninstall
--yes` reports them as kept and does not print "Done" while they remain.
`hooks status` shows `user wrapper` and `doctor` warns. With a user's
hook and no agentbell entry in the file, install adds nothing and says
why. A reinstall keeps the `--min-duration` found in agentbell's own
`run_completed` hook. An install that changes nothing does not rewrite
the file. A Codex or Kimi table without markers that is exactly what
agentbell writes counts as agentbell's: status reads installed, install
does not add a second copy, uninstall removes it, and a path that no
longer exists is repaired. The same test applies to Claude Code, Gemini
CLI, Qwen Code, Codex and Kimi Code.

**Cost.** Editing a generated hook by hand makes it the user's. If
agentbell's other entries are still in the file, a reinstall writes the
standard entry for that slot as well, so that event can push twice until
one is removed; it shows as `user wrapper`. A user's own hook that is
byte-for-byte agentbell's shape is removed by uninstall. The new Claude
`permission_prompt` hook reaches existing installs only through a
reinstall, and status does not flag it as missing, because that would also
warn people who removed it on purpose. A user who already had their own
`permission_prompt` hook calling agentbell gets two pushes per dialog.

## 34. Bytes agentbell does not own stay as they were (2026-09-23)

**What was wrong.** The config writers normalised what they did not own:
CRLF files came back LF (and LF files CRLF on Windows), comments and
foreign tables near agentbell's block were dropped, a non-UTF-8
`AGENTS.md` crashed `status`, `doctor` and `verify`, and inline TOML
hook forms turned into invalid TOML.

**Decision.** agentbell edits only its own lines and keeps the rest.

- **Codex and Kimi `config.toml`:** read with the file's own line
  ending and written back with it; final newlines, trailing blank lines
  and comments stay. Uninstall removes only agentbell's hook tables and
  its `[mcp_servers.agentbell]` table with its sub-tables, even when a
  sub-table sits elsewhere. Install touches only its own marked
  `features.hooks = true` line (and a pre-1.3 bare line directly above
  agentbell's block), so a flag under `[profiles.x]` is left alone. A file that is not UTF-8, or that defines hooks inline
  (`hooks = {…}`, `hooks.Stop = …`, a plain `[hooks.Stop]` table), is
  refused: nothing is written, a note names the entry, and `hooks
  install` exits 1.
- **Rule files** (`AGENTS.md`, `.rules`, `.clinerules`, `.continue`
  rules): edited byte for byte, including non-UTF-8 text (cp1252,
  latin-1) and the file's own line endings. A UTF-16 file is left
  unchanged with a note. A marker counts only on a line of its own; a
  marker quoted in running text is user text. A marker without its
  partner blocks every write and removal, with a note.
- **JSON settings and MCP files:** a file with comments or trailing
  commas, or not UTF-8, is refused byte-for-byte unchanged; the refusal
  prints the entries to add by hand, the other agents still install, and
  the exit code is 1. A real change keeps every key and value, but the
  file is re-serialized with two-space indentation. A symlinked config is
  updated where it points; a link into a read-only store (Nix,
  home-manager) is refused with a message. The existing mode is kept,
  and a new file follows the umask.

**Cost.** A TOML file that mixes CRLF and LF comes back in its majority
line ending. After install and uninstall, a rule file that had no final
newline has one. JSON formatting is not preserved on a real change; the
content is.

## 35. One credential rule for a server change (2026-09-23)

**What was wrong.** `init` cleared `ntfy.auth` and `ntfy.action_auth` on a
server change (§25, §27). `agentbell config set ntfy.server <other>` did
not, so the next push or `ask` sent the self-hosted password and the
button token to the new server. `ntfy.action_auth` could not be set with
`config set` at all.

**Decision.** `config set ntfy.server` and `init` use one function. Two
values are the same server when scheme, host, port and path match; letter
case, a trailing slash and an explicit default port do not count, and an
empty stored server means the default `https://ntfy.sh`. A stored value
that could never be used (a port like `8o80`) is compared by scheme and
host only, so correcting the typo on the same host keeps the credentials.
On a real change both credentials are cleared, with one stderr line each
saying how to set them again. `init --ntfy-auth` supplies the new password
and still clears the button token; an interactive `init` asks for the new
server's credential. `config set ntfy.action_auth <token>` exists: it is
redacted in output, `none` clears it, and it refuses the value stored in
`ntfy.auth`. `config set ntfy.server` refuses a URL that cannot be used
(no host, a bad port, `?`, `#`, spaces, credentials in the URL, a
mistyped scheme).

**Cost.** Moving to another host always means entering the credentials
again, even when they are the same.

## 36. Purge deletes only what agentbell owns (2026-09-23)

**What was wrong.** `uninstall --yes` deleted the whole directory named by
`AGENTBELL_CONFIG_DIR` or `AGENTBELL_STATE_DIR`, which could be
`~/.config`. It deleted metadata of other `pip --user` packages whose
name starts with `agentbell`, and a user's own `.clinerules` file once it
was empty. It left an `AGENTBELL_CONFIG` file, the Windows
`Scripts\agentbell.exe` launcher and the enabled bot service behind, and
reported "already fully removed" when it could not read a directory or
`pipx list` failed.

**Decision.** The default `~/.config/agentbell` and
`~/.local/state/agentbell` are still removed whole. A directory set by an
env var loses only agentbell's own entries (`config.json`;
`history.jsonl`, `queue`, `deferred`, `runs`, `bot.json`, `bot.lock`,
`tg-answers`, `tg-pending`, `ntfy-pending`, `ntfy-consumed`,
`.doctor-probe`), and inside those folders only agentbell's own item
files. The directory goes only when nothing else is left; everything else
is listed as kept. A directory that cannot be read is listed, and `--yes`
fails with the reason. A symlinked config file loses only the link, with
a warning that the target still holds the license key and tokens; a
symlinked config or state directory is kept. A file set by
`AGENTBELL_CONFIG` is listed and removed. `pip --user` removal touches
only agentbell's own files. pipx detection reads both output streams and
says so when pipx cannot be asked. On Windows the `Scripts\agentbell.exe`
launcher is included; it is locked while it runs, so the dry run points
to `py -m agentbell uninstall --yes`. The bot service is stopped,
disabled and deleted first. Marked blocks are removed only when they run
an agentbell `--agent` command, a user's `.clinerules` file is never
deleted, and hook entries follow §33. The dry run stays the default.

**Cost.** A hand-edited block without an agentbell `--agent` line
survives the purge and is not listed. `STATE_DIR_NAMES` has to grow with
every new state file; a test fails when a writer adds a name it does not
list.

## 37. Windows batch files get their arguments unchanged or not at all (2026-09-23)

**What was wrong.** `agentbell watch -- npm test` failed on Windows:
CreateProcess finds `.exe` files, and npm, yarn and pnpm are `.cmd`
files. Once `watch` found them, a second problem showed: CreateProcess
runs a `.bat` or `.cmd` through `cmd.exe`, which parses the arguments
again. `^` disappeared (`lodash@^4.17.0` became an exact version), `|`
and `<` broke the command, `&` ran a second one, and `%NAME%` expanded.
This is the BatBadBut class (CVE-2024-24576).

**Decision.** CreateProcess's own search runs first, so everything it
found before still runs, even when the current directory holds a script
of the same name. If it finds nothing, `watch` looks on `PATH` only,
never in the current directory, for a `.com`, `.exe`, `.bat` or `.cmd`.
A batch file is started with a command line `watch` builds itself:
`cmd.exe /d /v:off /s /c ""script" args"`. Every argument that holds
anything other than letters, digits and `#$*+-./:?@\_` is double-quoted
(the set Rust uses), and trailing backslashes are doubled so they cannot
escape the closing quote. `/d` skips the `cmd` AutoRun registry command
and `/v:off` turns delayed expansion off. `%`, `"`, CR and LF cannot be
passed through `cmd.exe` safely, so an argument holding one is refused:
exit 127, one stderr line, and a push saying the command could not be
started. Rust escapes `%` and `"` instead; refusing is the safe default.

**Cost.** Arguments with `%` or `"` (JSON on the command line,
`--define=100%`) cannot reach a `.bat`/`.cmd` tool through `watch`. A bare
name no longer finds a `.cmd` in the current directory (`watch --
.\build` does). The AutoRun command no longer runs before a batch file.
The Windows test suite covers this; a real PowerShell session running
`watch -- npm …` is not field-tested yet.
