# Changelog

## 1.6.2 — 2026-09-03 — removal is owner-scoped

### Fixed

- **`agentbell hooks install opencode` no longer wipes a project's Aider
  block.** The install migrated away from the v1.3rc OpenCode `AGENTS.md`
  block by removing *every* agentbell marker block in that file, and deleted
  the file when nothing else remained — in a project wired for Aider that
  was the current Aider block (found within minutes of v1.6.1, on the
  maintainer's own checkout). Block removal is now owner-scoped like block
  repair has been since b1aa21e: a block is only removed when it names the
  removing agent (`--agent opencode` for the migration, `--agent aider` for
  `hooks uninstall aider`); a block that names another agent, or none, is
  left untouched. `agentbell uninstall` (full reset) still removes every
  agentbell block, and its report line says so. Rationale: `DECISIONS.md`
  §17d.

## 1.6.1 — 2026-09-03 — one buzz per event

Patch release from twelve days of real use on the maintainer's machine
(1,339 history records across Claude Code, Codex, OpenCode, Kimi Code and a
self-integrated agent). Rationale: `DECISIONS.md` §17.

### Fixed

- **OpenCode no longer pushes twice for one turn, and short turns stay
  silent.** In ~6% of turns OpenCode 1.18.26 reported the same session idle
  twice within a second (24 same-second doubles in 415 turns); the plugin
  now reports one turn end per session per 10 s. It also measures the turn
  from the user's prompt (`message.updated` with role `user`) and passes
  `--duration` together with `--min-duration 60`, so OpenCode follows the
  same "no push for a turn you watched" rule as every other hook agent
  (before: 0 of 415 turns were skipped as short). Re-run `agentbell hooks
  install opencode` to update the plugin file. Until the first prompt after
  the plugin loads the duration is unknown, and an unknown duration still
  notifies. An older plugin file now shows as `update needed` in
  `hooks status` and as a WARN in `doctor`, each with that command as the
  fix — status observes, only install rewrites.
- **An identical hook push within 5 s is suppressed.** One API outage made
  six parallel Claude Code sessions fire `StopFailure` within seven seconds:
  six identical "run_failed" pushes for one piece of news (29 of 43 failure
  pushes in the sample were such repeats). The hook now claims each push
  (agent + event + text) in a small state file; a repeat inside
  `HOOK_DEDUPE_WINDOW_SECONDS` (5 s) is recorded as `hook.skipped_duplicate`
  — with the text and the original event — and not sent. `--force` bypasses
  it; a push with a different text (another project, another duration) is
  never a repeat. Two processes claiming the same push in the same instant
  can both win (no lock): the window collapses bursts, it does not promise
  exactly-once.
- **`verify` no longer calls a failure burst a "possible double
  integration".** `run_failed` left the near-duplicate set for the reason
  `permission_required` did in 1.6.0: it is driven from outside (one outage,
  many sessions), and `run_completed` alone exposes a double integration.
  Suppressed repeats now show in the report ("N suppressed (identical
  push)") and a suppressed `run_completed` repeat still raises the
  double-integration WARN, since a delivered pair can no longer occur.
- **Aider block repair writes atomically and only replaces its own block**
  (shipped on `main` after the v1.6.0 tag as b1aa21e): the `replace_stale`
  path of `_install_block_file()` used a truncating write and did not check
  that the marker block it replaced was agentbell's.

### Changed

- **Outdated Aider blocks are visible instead of silently left in place.**
  Older `AGENTS.md` blocks told every agent that reads the shared file to
  emit hooks attributed as `aider`. `hooks status` and text-mode `verify`
  now show a bordered ACTION REQUIRED banner; `verify --json` reports the
  same condition in `repair_notices`. Running `agentbell hooks install
  aider` manually replaces only the marker-owned block with an Aider-only
  instruction and preserves every user section outside it.
  (Shipped on `main` after the v1.6.0 tag as 0f77fbd and listed under 1.6.0
  in the previous changelog; the v1.6.0 release archive does not contain
  it.)

### Field-verified

- Claude Code, Codex, OpenCode and Kimi Code hooks are ticked in
  `FIELD_TEST.md` from twelve days of real turns: durations, short-turn
  skips, failures and a needs-input event, all delivered to ntfy and
  Telegram. OpenCode's "exactly one push per turn" failed in that sample
  (the doubles above) and is fixed here; the fixed plugin awaits its own
  real turns.

## 1.6.0 — 2026-08-21 — the universal agent contract: `integrate` + `verify`

Twelve maintained integrations answered "does it work with mine?" twelve
times; this release answers it once, for every agent. agentbell no longer
needs to know an agent to work with it: it **publishes a contract** and
**observes the results**. Rationale: `DECISIONS.md` §16.

### Added

- **`agentbell integrate`** — prints a self-integration guide for any agent:
  known-agents short-circuit (native installer + stop), a mechanism ladder
  (shell lifecycle hooks > MCP for deliberate actions > rules block as best
  effort — exactly ONE lifecycle mechanism), slug rules with a reserved
  list, the runtime contract (absolute binary path, the 5 events,
  `started --silent` + `--min-duration 60` coupled, `hook` always exits 0,
  `ask` fails closed), a notification policy, binding safety rails (own
  configs only, slug-scoped markers, diff + explicit OK outside the project,
  repo-initiated tasks require asking the user, never read agentbell's
  config/state), a two-step verification protocol and a report template.
  `--json` prints the same contract as a machine-readable manifest
  (`contract_version: 1`). The command changes nothing and never reads the
  config, so no credential can appear in its output (test-enforced).
- **`agentbell verify`** — read-only observation report from history: per
  agent the delivered / held (quiet hours or queued) / skipped-short /
  forced buckets, event counts, last-seen age; WARNs for near-duplicate
  events (possible double integration — never a FAIL), delivered `started`
  events (wire `--silent`), unknown event names (with the valid list), and
  installed-but-silent integrations; offline delivery basics (config
  present, topic format, binary on PATH). Sends nothing, and never prints
  the topic, server or a path (test-enforced) — safe to hand to an agent.
  `--json` for machines. Exit 0 = a real (non-forced) agent event observed
  and no FAIL; forced smoke tests (`--force`) alone still exit 1 ("smoke
  test only, wiring still unproven").
- **History attribution** — hook-driven records now carry the firing
  `agent`, `forced: true` when `--force` pushed them through, and
  `source_event` preserving the original hook event when quiet hours or
  queueing rewrote it. Unknown agent slugs now appear on the phone as the
  slug itself instead of a generic "Agent".
- **MCP `notify` accepts an optional `agent` argument** for attribution
  (sanitized; a bad value drops the attribution, never kills the server).
  Tool descriptions now state the notification policy and that a timeout
  is not an approval.

### Changed

- **`hook` tolerates unknown event names**: exit 0, nothing sent, a
  `hook.unknown_event` history record with the requested name — `verify`
  surfaces it with the valid event list. A hallucinated event name must
  never fail an agent's turn (`--agent` validation stays strict: exit 2).
- `hook`'s help line no longer claims to be internal — self-integrating
  agents are a supported caller since the contract exists.
- `doctor` mentions self-integrated agents seen in history on its "agent
  hooks" line and cross-links `verify`; `uninstall` lists self-integrated
  wiring under "not removed automatically".

### Fixed (found by the Tier-1 field test, see below)

- **`agentbell test` no longer reports "NOT delivered" for delivered
  messages.** The confirmation poll used an epoch cursor from the *local*
  clock; with the local clock ahead of the server's (WSL2 clock drift), the
  server-side `since` filter hid the delivered message, and poll errors were
  silently swallowed. The poll now uses a server-relative duration window
  (`since=90s`), the last poll error is reported as the failure reason, and
  the output separates three honest states: "NOT delivered" (publish
  failed), "sent, but NOT confirmed" (server accepted the message, read-back
  failed — still exit 1, unconfirmed is not proven), and "delivered and
  confirmed" (published *and* read back from the server; only your phone's
  subscription proves the final hop). `doctor --send` reports the middle
  state as a WARN instead of a false "did NOT arrive" FAIL. The same
  local-clock bug class was fixed in the `ask` receiver: its prime/stream/
  poll replay windows are now server-relative duration strings (deduplicated
  by message id), so clock drift can no longer blind the poll fallback that
  exists precisely for servers with unreliable streams.
- **`verify` no longer flags rapid real permission prompts as a "possible
  double integration".** The near-duplicate heuristic counted *any*
  same-label events ≤5 s apart; GitHub Copilot CLI legitimately raised
  several `permission_required` prompts within one second. Duplicate
  detection now covers per-turn lifecycle events only (`started`,
  `run_completed`, `run_failed`), tracks per event label (an interleaved
  interaction event no longer hides a real turn duplicate — detection got
  *stronger* there), and skips `--force` smoke tests (a re-run command is a
  human, not a second integration).

### Fixed (found by CI)

- **A published contract can no longer carry the calling context as its
  executable.** `agentbell_binary()` fell back to `sys.argv[0]` verbatim;
  under `python -m unittest` the stdlib rewrites argv[0] to the literal
  string `"python -m unittest"`, so with agentbell not on PATH the contract
  advertised `<cwd>/python -m unittest hook …` as a runnable command (every
  CI job; same class for any embedder with a foreign argv[0]). argv[0] is
  now only trusted when it names a real agentbell entry point on disk
  (launcher, `agentbell.py`, `agentbell.exe` — case-insensitive stem);
  otherwise the fallback is the module file itself. A relative argv[0] after
  a `chdir` is rejected by the same existence check.
- **Uninstall, self-heal and `hooks status` now recognize every binary
  shape.** The "is this hook ours?" test was the substring `agentbell hook`,
  which only matches the bare-launcher shape — hooks installed from a
  checkout (`…/agentbell.py hook …`) or on Windows (`'…\agentbell.exe'
  hook …`, always quoted) were invisible to uninstall, repair and status.
  The matcher now parses the command and compares the first token's
  basename stem against `agentbell`; a wrapped command (`bash -c '…'`) is
  deliberately not touched — it is the user's, not ours.
- **Contract commands now embed the binary shell-quoted.** The manifest and
  guide built commands as `f"{binary} hook …"` with the raw path; a Windows
  path (backslashes) or any path with spaces did not survive the shell
  split the host applies before executing — the same quoting the native
  hook installers already used everywhere else.
- **The Windows test jobs were red before this branch and are repaired
  with it:** on Windows `os.path.expanduser` ignores `HOME` and reads
  `USERPROFILE`, so tests that only moved `HOME` read and wrote the real
  runner profile (state leaked between tests; installs landed where
  assertions never looked). Test homes now move both variables; the bot
  service test is skipped on Windows (no installer there by design); the
  remaining assertions are binary-shape-independent.

### Field-verified

- **Tier 1 passed (2026-08-21): GitHub Copilot CLI 1.0.80** self-integrated
  against the printed contract alone — chose its own slug and native hooks,
  wired all 5 events with the paired anti-spam flags, produced a real
  (non-forced) `run_completed` after an ~8-minute turn, passed
  `verify --agent github-copilot-cli --since 10m`, was idempotent on a
  second `integrate` run, and removed itself cleanly. Details in
  `FIELD_TEST.md`.

## 1.5.0 — 2026-08-19 — first public release

### Changed

- **The project was renamed from `agent-notify` to `agentbell`** — binary,
  module, config and state dirs, env vars, block markers and MCP server name
  all changed with it; the entries below are written in the new names even
  where the older, never-published builds used the old one.
- **Licensing moved from HMAC to Ed25519 signatures.** Keys now look like
  `AB1-…` and carry an Ed25519 signature over their payload. `agentbell.py`
  contains only the matching **public** key, so a key cannot be forged from
  anything that ships, and the private seed never leaves the author's machine.
  RFC 8032 is implemented in stdlib Python (SHA-512 + integer math) and covered
  by the RFC's own §7.1 test vectors. Rationale: `DECISIONS.md` §2b.
- **The build step is gone.** `tools/build.py` used to inject a symmetric
  signing secret into the installed copy — a secret that a single-file release
  on PyPI would have handed to anyone with `pip download` and `grep`. There is
  nothing to inject any more: `install.sh` installs the source file directly
  (pipx → `pip --user` → plain copy, unchanged fallback chain).
- **"This build cannot verify keys" is gone**, because that state can no longer
  exist: every copy verifies keys with the embedded public key. `doctor`, the
  init wizard and `license activate` dropped their branches for it — a key that
  does not check out is now simply reported as invalid, with a support hint.

### Security

- Pre-release hardening pass: the webhook rejects browser-originated requests
  and oversized bodies, config and hook writes refuse to follow symlinks, the
  HTTP opener never follows redirects (credentials are not replayed to another
  host), licensing fails closed on anything it cannot verify, and files holding
  credentials are written with stricter modes.

## 1.4.1 — 2026-08-16 — every integration re-verified against the live vendor docs

Every agent path and config format was re-checked against the vendors' current
documentation (2026-08-16) before trusting other people's machines to them.
Vendors move; this pass found two of them.

### Fixed

- **Windsurf changed its rule engine** (Windsurf → Devin Desktop). Current
  builds read `.windsurf/rules/*.md` (or `.devin/rules/*.md`, preferred) with
  `trigger: always_on` frontmatter — the Cursor-style `.mdc` written since
  v1.4.0 is no longer the documented format. Install now writes **both** files
  (`.md` for current builds, `.mdc` for pre-Devin ones); uninstall removes
  only files it owns. Detection also recognizes a `.devin` directory.
- **Qwen Code hooks no longer block the end of every turn.** Qwen's command
  hooks support `async: true` per current docs; v1.4.0 ran them synchronously.
  All three hooks are now async, matching the Claude/Codex wiring.
- **Install now repairs stale hook configs, not just detects them.** The JSON
  merger compares whole hook entries (previously only the command string), so
  a Qwen hook written by 1.4.0 without `async` is upgraded on the next
  `hooks install`; the Kimi and Codex TOML blocks are replaced when the binary
  path or flags changed (a stale path pointed at a binary that no longer
  exists).
- **Continue detection** also checks for the `continue` binary — the CLI is
  `continue` (or `cn` on some installs), not only `cn`.
- **`doctor` now reports the Qwen Code MCP registration** (it was written by
  `mcp add` but missing from the health check's client list).

### Added

- **Qwen Code is a first-class MCP client**: `mcp add` registers in
  `~/.qwen/settings.json` (global; `--project` → `.qwen/settings.json`),
  `uninstall` and `doctor` scan it.

### Verified unchanged (documented, not touched)

Kimi Code hooks + MCP paths, Gemini CLI `AfterAgent`, Cursor `.mdc` rules,
Cline `.clinerules/`, Continue `.continue/rules/`, Zed `.rules`, OpenCode
plugin dirs + event names — see DECISIONS.md §15.

## 1.4.0 — 2026-08-16 — more agents

Five supported agents became twelve. Rationale — including why six of them are
wired by rule file rather than by hook — is in `DECISIONS.md` §14.

### Added

- **Seven more hook targets** (5 before, 12 supported agents now): **Kimi Code**
  (`~/.kimi-code/config.toml` `[[hooks]]`, real events → finished/failed with
  duration), **Qwen Code** (`~/.qwen/settings.json`, Claude-style JSON hooks),
  **Windsurf** (`.windsurf/rules/agentbell.mdc`, same MDC engine as Cursor),
  **Cline** (`.clinerules/agentbell.md`), **Continue**
  (`.continue/rules/agentbell.md`) and **Zed** (`.rules` block, the one file
  Zed actually reads). Seventh, **Aider** gets an `AGENTS.md` block (auto-read
  since v0.69), so the plain `nano` edit that worked for AGENTS.md users now
  works for Aider too.
- The agent code became a registry (`AGENT_SPECS`: detect / install / status
  per agent). `find_agents()`, `install_hooks()`, `hooks_status()` and the
  `uninstall` scan all run off one table, so a new agent is one entry instead
  of five branches.
- `hooks status` and `uninstall` list the new agents; `init` offers to wire
  whichever of them it detects on your machine.
- **Kimi Code is a first-class MCP client** for `mcp add`: it registers in
  `~/.kimi-code/mcp.json` (global; `--project` → `<proj>/.kimi-code/mcp.json`).
  Kimi exposes the tools as `mcp__agentbell__notify` and
  `mcp__agentbell__ask_approval`; new sessions only, then `/mcp`.

## 1.3.1 — 2026-08-14 — first-setup fixes

Found by running the real setup on a clean machine (2026-08-14). Every item
below cost the user something during that run.

### Fixed

- **A network timeout was reported as "invalid bot token".** During `init`,
  `getMe` timing out sent the user back to @BotFather to create replacement
  bots — twice — for a token that was never the problem. Transient errors now
  pass through as what they are, and the wizard offers to keep the unverified
  token and carry on.
- **A bad bot token aborted the whole wizard** (`SystemExit(3)`), throwing away
  the ntfy topic and the license key already entered. The token prompt now
  retries, and giving up only skips Telegram — everything else stays configured.
  `find_chat_id` failing no longer crashes `init` either.

### Added

- **`agentbell config set <key> <value>`** — change one setting without
  re-running the wizard (`ntfy.topic`, `ntfy.server`, `ntfy.auth`,
  `telegram.chat_id`, `channels`, `quiet_hours`, `quiet_hours_mode`,
  `quiet_hours_min_priority`, `approval_timeout`). Values are validated:
  unlike the tolerant config reader, a malformed quiet-hours window is
  rejected rather than silently dropped. `doctor`'s short-topic warning now
  fixes itself with one pasteable line instead of "run init again".
- **`agentbell bot install-service`** — installs the Telegram answer daemon
  as a systemd user unit (or a launchd agent on macOS) with the absolute
  binary path, so the approval buttons no longer depend on a terminal staying
  open. Detects a missing systemd (WSL, containers) and prints a `nohup`
  fallback instead of leaving a unit file that never runs. Replaces the old
  "copy `examples/agentbell-bot.service`" advice, which only worked from a
  git checkout.

- **OpenCode MCP no longer needs hand-editing.** `mcp add` refused any
  `opencode.jsonc` on the assumption that it carried comments — but the check
  was the file *extension*, and the stock OpenCode config has none. It now
  looks for real comments (a scan that ignores strings, so `"https://…"` in
  the default `$schema` line no longer counts) and writes the file when there
  is nothing to lose. `opencode_config_path()` also resolves to whichever file
  actually exists, so `mcp add`, `doctor` and `uninstall` finally agree on one
  path instead of writing a `.json` that OpenCode ignores.

### Changed

- Installing Codex hooks that are already present no longer prints a `note:`
  restating the line above it.
- The `NEXT STEPS` block no longer lists commands *after* the blocking
  `agentbell bot`: pasting the whole block fed the following lines into the
  daemon's stdin, so the suggested `agentbell doctor` silently never ran.

## 1.3.0 — 2026-08-14 — field-test release

Versions 1.0–1.2 were unreleased development builds; see DECISIONS.md for their
design history.

Hardening pass before the 2-week field test: an adversarial multi-agent audit
(83 findings, 37 confirmed after verification) plus end-to-end runs against the
real Claude Code 2.1.232, Codex 0.147.0, OpenCode 1.18.18 and ntfy.sh.
Rationale for every decision is in `DECISIONS.md` §12.

### Fixed — things that were simply broken

- **`agentbell mcp` crashed** with an `AttributeError` — and that is exactly the
  command every `mcp add` registration invokes. MCP integration never worked in
  any client. The bare subcommand now *is* the stdio server, and a test asserts it.
- **Codex hooks were never enabled.** `features.hooks = true` was appended at the
  end of `config.toml`, where TOML makes it a key of the *last table* instead of a
  top-level one. It is now written above the first table header. (Any config
  containing a `[table]` — including the `[mcp_servers.agentbell]` block this
  tool writes itself — hit this.)
- **A new `ask` could inherit the previous ask's answer.** ntfy's `since` cursor is
  second-granular, so an older answer could still fall inside the new window.
  Reproduced end to end; fixed by priming the waiter with everything already on
  the response topic.
- **`agentbell test` always exited 0** and printed nothing, even when nothing was
  delivered. It now reports delivery, exits 1 on failure, and names the next step.
- **Partial delivery lost a channel:** a queued/deferred item delivered on one
  channel was deleted even when the other channel still failed. It is now
  re-queued with exactly the channels that failed — in the queue, the deferred
  store and the bundle path.
- **Ctrl-C during a queue flush destroyed the in-flight notification.** The claimed
  item is handed back instead of consumed.
- **A crashed sender stranded items forever** as invisible `.sending` files; they
  are now reclaimed after 15 minutes.
- **`hooks install` appended a second copy of every hook** when the binary path
  changed (pipx → copy), so every turn notified twice. Stale copies of our own
  hooks are now replaced.
- **`uninstall` left a pip install fully working** — only the metadata directory
  was deleted, not the module or the launcher script.
- **The Cursor rule was invalid**: comment markers above the YAML frontmatter and
  an unquoted colon inside it. The `.mdc` file is now written verbatim.
- **A dead answer daemon still got approval buttons** for up to 60 s (heartbeat age
  was checked, process liveness was not).
- **A restarted Telegram daemon replayed up to 24 h of backlog** and could answer a
  brand-new question with an old message. Replies that predate the question are
  now rejected.
- **The queue drain blocked the daemon** for minutes on a long backlog; it is now
  capped at 20 s per cycle so approvals keep flowing.
- **`notify` exited 3 and hid a successful ntfy delivery** when the config listed
  Telegram without a valid license. Config-derived channels degrade; an explicit
  `--channel telegram` still fails loudly.
- **The approval poller and `test` ignored ntfy auth**, so both silently failed
  against a protected self-hosted ntfy.
- **The live approval stream died after ~46 s** (read timeout equal to ntfy's
  keepalive) and never reconnected, silently degrading to polling.
- **A title or tag containing a newline crashed** the CLI and the webhook, and an
  untitled notification was literally titled "None".
- **A hand-edited `quiet_hours` value crashed every send**; values are now
  normalized on load and validated in the wizard.
- **A topic of 55–64 characters broke every `ask`** (the derived `-responses`
  topic exceeded ntfy's limit) — rejected at setup with an explanatory message.
- **`agentbell hooks` with no subcommand** crashed like `mcp` did.
- **Windows notifications** loaded a type and reported success without notifying.
- Ctrl-C/Ctrl-D anywhere printed a traceback; `hooks uninstall` left an empty
  `"hooks": {}`; deferred bundles were listed in random order; the bot daemon
  left its lock file behind.

### Security

- **The paid tier was unlockable by anyone**: `AGENTBELL_LICENSE_SECRET` let a
  user choose the *verifier's* secret and sign their own key. A build with the
  real secret injected now ignores the environment entirely.
- **The Telegram bot token leaked** into `history.jsonl`, `bot.json`, queue files
  and stderr — every error message carried the full API URL. Scrubbed at the
  single choke point.
- **`_pid_alive` terminated processes on Windows**: `os.kill(pid, 0)` maps to
  `TerminateProcess` there. It now queries the exit code instead.
- `config.json` is written **0600** (license key, Telegram token, ntfy password)
  and every config write is atomic.
- `config show` redacts **all four** credentials — it previously printed the
  self-hosted ntfy password and the webhook token in clear.
- The webhook server **refuses to listen on a non-loopback address without a
  token**, and rejects a malformed `timeout_seconds` with 400 instead of dying.
- macOS notifications escape the message instead of interpolating it into
  AppleScript; `notify-send` gets `--`.
- The webhook bearer token is compared with `hmac.compare_digest`.
- Telegram approval buttons are only accepted from the configured chat.
- Approval buttons can carry a scoped `ntfy.action_auth` credential instead of
  the account password (a button definition is visible to every subscriber).
- `history.jsonl` is rotated at 2 MB instead of growing forever.
- Agent configs are never overwritten when they contain invalid JSON.

### Added

- **`agentbell doctor`** — checks install, PATH, config, file mode, topic,
  server reachability, quiet hours, license, Telegram daemon, agent hooks, MCP
  registrations, queue backlog and state dir, and prints a **copy-paste fix
  command** for everything that is wrong. `--send` adds a real delivery test.
- **Desktop apps via MCP**: `mcp add` targets `claude`, `claude-desktop`,
  `chatgpt-desktop`, `codex`, `gemini`, `cursor`, `opencode`, `vscode` —
  globally, and only for clients actually installed — plus `--print` for
  anything else. (ChatGPT Desktop shares Codex's MCP config; ChatGPT web is
  remote-MCP-only.)
- **A real OpenCode plugin** (`~/.config/opencode/plugin/agentbell.js`) instead
  of an `AGENTS.md` request the model could ignore: `session.idle`,
  `session.error` and `permission.asked`, with subagent sessions filtered out.
  Installing migrates away from the old `AGENTS.md` block automatically.
- **Turn durations for Claude Code and Codex** ("finished in 4m12s") via a silent
  `UserPromptSubmit` start marker.
- **`--min-duration` (default 60 s on Claude Code and Codex)** — "finished" fires
  after every turn, so short turns you watched happen now stay silent (logged as
  `hook.skipped_short`). Failures and unknown durations always notify.
- `init` and `install.sh` end with a copy-paste **NEXT STEPS** block; the wizard
  walks you through BotFather for Telegram.
- `license activate` and `doctor` detect a build installed without the signing
  secret, instead of blaming your key.
- A denial can carry a reason ("no, not before the release") instead of losing it.

### Changed

- **Free-text answers keep their text.** "yes, but use staging" is an instruction,
  not a bare approval. A leading negation still denies (fail-closed), a bare
  "yes"/"ok" still approves.
- Hooks and MCP are registered **globally** by default (`--project` forces
  project scope) — you wire up once, not per repo.
- MCP `ask_approval` defaults to a 120 s timeout (capped at 600 s) so desktop
  clients do not cancel the call.
- The queue is drained oldest-first, as documented.
- Deferred items are bundled **per channel set**, so a channel-restricted message
  is never republished everywhere.
- An unreadable response topic is reported on stderr instead of looking like
  "nobody answered".

### Internal

- One atomic JSON writer, one ntfy poll helper, one Telegram API call helper
  (was five copies), one bot-state updater (was two), one quiet-hours
  normalizer, and a rewritten `_merge_json_hooks`.
- Test suite: 91 → 128 tests, with a regression test per confirmed finding.
