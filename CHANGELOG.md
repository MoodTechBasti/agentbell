# Changelog

## 1.7.0 — 2026-09-24 — audit hardening

Fixes from the 2026-09-22 audit (69 findings) and three review rounds of
those fixes. Several changes are visible to scripts that call agentbell:
read Changed before upgrading. Rationale: `DECISIONS.md` §20–§37.

After upgrading, re-run `agentbell hooks install <agent>` for each wired
agent (Claude Code gets its permission hook, checkout installs and Windows
get runnable hook commands, the OpenCode plugin is updated), re-run
`agentbell bot install-service` if you use the service, and restart a bot
that is still running from an earlier version. Until it restarts, `ask`,
`doctor` and `bot status` do not see it, and `ask` sends no buttons.

### Changed

- **`approved: true` means an explicit yes.** `ask --json`, the MCP tool
  `ask_approval` and the webhook set it only for the Approve button, a
  typed `APPROVED <id>`, a bare `approve` / `approved` / `yes` / `yep` /
  `yeah` / `ok` / `okay` / `y` / `ja` / 👍, or the yes button's label alone.
  Any other free text, such as `staging` or `yes, but use staging`, still
  exits 0 with the text on stdout, and `approved` is false. A caller that
  took `approved` to mean "the user answered" has to read `answer` as well.
- **Refusals and postponements deny (exit 1).** Besides `no`, `Nein`,
  `Stopp`, `not now`, the no button's label, 👎 ❌ 🛑 ✋ ⛔ and similar,
  a postponement is a denial too: `wait`, `warte`, `später`, `later`,
  `hold on`, `hang on`, `not so fast`, `pause`, `Moment`, ⏸ ⏳ ⌛, also with
  a reason after it (`wait, tests are red`). A yes followed by a denial or
  postponement denies as well (`yes, but wait`, `ok, later`, `ja, aber
  später`, `👍⏳`), and so does a benign one such as `yes, no problem`.
  Smart punctuation from a phone keyboard (`yes… wait`, `ok — later`) is
  read like its ASCII form. `not staging` and `nicht staging` stay
  answers. Typing the yes button's label approves even when it starts with
  a no-word (`--yes-label "Stop it"`).
- **A typed reply is used only when exactly one question can be on the
  phone.** A reply that names no question (anything but a button, a typed
  `APPROVED <id>` / `DENIED <id>`, or Telegram's Reply on the question)
  goes to an ask only when exactly one approval question can still be
  open. An ask counts while its process waits for the answer. After it
  ends without an answer on that channel (timed out, failed to send,
  killed, or answered on the other channel), its question keeps counting
  for 60 seconds. A question the server rejected (an HTTP 4xx other than
  408, Telegram `ok: false`) never reached the phone and does not count;
  after a 5xx, a timeout or a dropped connection it may have, and it
  counts. The questions are those that could be on the phone when the
  reply was sent, not when agentbell reads it: a reply that reaches
  agentbell more than 30 seconds after it was sent (after a lost
  connection, or from a Telegram bot that was down) is not used. Otherwise
  the reply is refused, recorded as `stale_answer`, and a "Reply not used"
  notice on the same channel says to tap a button, use Reply (Telegram),
  or send `APPROVED <id>` / `DENIED <id>` (ntfy). A reply written before
  the question went out is stale; replies are ordered by Telegram message
  id or by the ntfy server's time, not by the local clock, and their age
  is measured so that a clock difference between this machine and the
  server cannot make a late reply look fresh.
- **`watch` keeps the terminal and survives signals until the push is
  sent.** The command runs in `watch`'s own process group, the terminal's
  foreground job, so sudo, ssh and gpg password prompts work and Ctrl-Z
  suspends it. Ctrl-C, Ctrl-\\, a closing terminal and SIGTERM no longer
  end `watch` early: the command gets the signal once, may finish its
  cleanup, and the push goes out. `watch` exits with the command's code; a
  signal death is 128+signal. Signals that were ignored when `watch`
  started (nohup, a background job in a script) stay ignored for the
  command.
- **How `watch` passes signals on.** On Linux it reads the sender of each
  signal (sigtimedwait). A key press or a signal from a process in its own
  group (timeout(1), the command's `kill 0`) is not passed on, because the
  command already got it. `kill -INT <watch pid>` (an IDE stop button,
  pexpect) and a signal relayed to `watch` alone by its parent (uv run,
  uvx, a nested `watch`, a wrapper script) reach the command. A hangup
  reaches the command when `watch` runs without a terminal (cron, CI).
  macOS and other non-Linux systems use a heuristic instead: while `watch`
  is the terminal's foreground job, Ctrl-C and Ctrl-\\ count as keys, so a
  `kill -INT <watch pid>` sent from elsewhere is not passed on.
  timeout(1)'s options are read the way its getopt reads them
  (`-vs TERM --foreground`, `--sig TERM`, `--fore`). An option it cannot
  read counts as a relay. Known residual on every POSIX system: a signal
  sent to the whole group from outside it (`kill %1`, `kill -- -PGID`,
  systemd `KillMode=control-group`) reaches the command twice, as with
  sudo. On Linux it arrives three times when a relaying parent sits in
  between (`uv run`, a nested `watch`). A parent that signals its own
  group (a script's `trap 'kill 0' TERM`) cannot be told apart from a
  relay, so the command gets that signal twice. A command that treats a
  second SIGTERM as "force quit" then skips its graceful shutdown.
  Dropping a signal that might be a repeat was rejected: a command that
  never stops, while the push says it succeeded, is worse.
- **Ctrl-C while the push hangs ends `watch`.** Once the command has ended,
  Ctrl-C or SIGTERM during a stalled send queues the push, says so on
  stderr, and exits with the command's code. Before, these signals were
  ignored for about 33 seconds. On Windows the wait stays as it was.
- **Hooks send within a 6-second budget.** All sending in a hook, retries
  and queue replay included, fits in 6 seconds, below Kimi Code's 10 s and
  Gemini CLI's 15 s hook timeouts, even when a DNS lookup stalls or a
  server answers byte by byte. What does not fit is queued (history
  `queued`, `queue list`) and delivered later. On hosts with long hook
  timeouts, a flaky network now queues the push after about 6 seconds
  instead of retrying for up to 18.
- **A failing hook leaves a trace.** A hook still exits 0, including with
  a broken `config.json` (it used to exit 1), but it records `hook.error`
  in `history` with the agent and the reason, and prints one line on
  stderr. `verify` counts it as an event that reached no channel.
- **Refused configs exit 1.** `hooks install` exits 1 when it refuses a
  config and nothing is installed: a settings.json with comments or
  trailing commas, clashing TOML hooks, a non-UTF-8 config, a broken
  AGENTS.md block, a symlinked rule file. `hooks uninstall` exits 1 when
  agentbell's own block stays in place (an unpaired marker in a rule file
  or the Kimi config, a symlinked AGENTS.md) and says "left in place for
  <agent>". A hook you wrote
  yourself that calls agentbell is kept with a note and exit 0, so
  `hooks uninstall all` can be run again. `uninstall --yes` exits 1 when a
  step fails and does not print "Done" while your own hooks still run
  agentbell. `mcp add` exits 1 when a client row says FAILED.
- **`bot install-service` exits 1 when the service could not be set up**
  (no systemd, as on WSL; `systemctl` or `launchctl` failing or missing;
  an unwritable service directory). It used to report success. The
  `nohup` fallback instructions now go to stderr.
- **`notify --quiet` no longer hides failures.** A failed notification
  prints its error on stderr and exits 3; `--quiet` silences only success
  output. Without `--quiet`, the error line moved from stdout to stderr.
- **Hook ownership is exact.** `hooks install` and `hooks uninstall`
  replace or remove only entries agentbell generated: its own command
  (any binary path, only the flags it writes) under the event and matcher
  it writes to. A wrapper (`afplay …; agentbell hook …`), a command with
  extra flags, or a hook under another matcher is yours and stays; this
  also holds for Codex and Kimi Code. A generated hook you edit becomes
  yours too; if other agentbell hooks remain in that file, reinstall adds
  the standard hook next to it, and that event pushes twice until you
  remove one (`hooks status` shows `user wrapper`).
- **`init` takes the Telegram chat ID only from a private chat** with the
  bot. Messages in groups and channels are ignored, and `init` says so.
- **Windows `watch` refuses batch-file arguments cmd.exe would change.**
  An argument to a .bat or .cmd tool (npm, yarn, pnpm) that holds `%`, a
  double quote or a line break makes `watch` exit 127 with the reason on
  stderr and a push saying the command could not start. JSON arguments
  and `--define=100%` for such tools are affected. See Security.
- **Server URLs are validated.** `config set ntfy.server` refuses a URL
  that cannot be opened: a port like `8o80`, no host, a mistyped scheme
  (`https//host`), a query or fragment, spaces, or credentials in the URL
  (put those in `ntfy.auth`). A stored server like that now fails each
  send with a clear error instead of queueing every push as "unreachable",
  and `init` stops with an error.
- **The fresh-start hint** after `uninstall --yes` reads `pipx install
  agentbell && agentbell init` (or `./install.sh` from a checkout).

### Added

- **Claude Code permission prompts notify you.** `hooks install claude`
  adds a `Notification` hook with matcher `permission_prompt` that sends
  `permission_required` when Claude shows a permission dialog. Re-run
  `agentbell hooks install claude` to add it to an existing install;
  `hooks status` does not flag an install without it. If you already
  wired your own `permission_prompt` hook that calls agentbell, both run.
  Wired from the Claude Code hooks documentation; not yet field-tested.
- **`config set ntfy.action_auth <token>`.** The approval-button token is
  redacted in the output, `none` clears it, and your `ntfy.auth`
  credential is refused. `config show` redacts it.
- **Priority names for `quiet_hours_min_priority`** (`high`), in the
  config file and in `config set`. A hand-edited name no longer makes
  every notify and hook fail.
- **Quiet hours in `init`**: several windows separated by commas; a typo
  asks again instead of exiting; Enter keeps the current windows on a
  re-run, and `none` (or `--quiet-hours none`) clears them.
- **The webhook server listens on the IPv6 loopback** (`webhook.listen`
  `::1` or `[::1]`, no token needed).
- **`doctor` warns when a registered MCP server command cannot be
  started** and names the `agentbell mcp add <client>` fix.
- **`mcp add --print` includes a Zed snippet** (`context_servers` in
  Zed's settings.json).
- **`integrate`** documents `ask` exit 3 and that free text exits 0
  without approving (check `approved` with `--json`). The manifest gains
  `command_prefix` and `powershell_command_prefix`.
- **More history records.** A channel that fails permanently for a queued
  or deferred item delivered elsewhere is recorded (`queue_dropped` /
  `deferred_dropped`); `queue flush` reports how many items it deferred;
  `verify` marks MCP notify calls.
- **Examples** for Kimi Code (`kimi-config.example.toml`), Qwen Code
  (`qwen-settings.example.json`) and the macOS launchd job
  (`com.agentbell.bot.example.plist`). The Claude, Codex and OpenCode
  examples and the systemd unit match what the installers write;
  `custom-agent.sh` and `webhook.sh` gate on `approved`, not on exit 0;
  `watch.sh` passes its arguments as words.
- **Project docs.** `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1);
  `RELEASING.md` is the current release runbook; the bug report template
  asks for `--version` and `verify` output instead of `doctor` (which
  prints the topic).

### Fixed

#### Approvals

- A restarted Telegram bot no longer accepts a `yes` sent before the
  question: a typed reply counts only when its message id is newer than
  the question's, independent of the local clock.
- Telegram's Reply on a question answers that question, even when a newer
  one is open. A reply to a closed question is logged as stale and not
  given to another.
- Typed `APPROVED <id>` / `DENIED <id>` answers the question with that id
  on Telegram, and is accepted in capitals on ntfy. On Telegram, `approve
  2` is free text unless the id is the question's.
- On ntfy, typed `approve 2`, `deny 2` or `deny bad idea` reach the
  question as an answer or a denial instead of being dropped until the
  timeout.
- A pending-question file caught mid-write is read again. One that stays
  unreadable counts as a question whose place is unknown, instead of
  being ignored: while its ask waits, and for 60 seconds after.
- A waiting ask no longer disappears after the laptop sleeps or a publish
  is slow. Its question used to expire by the wall clock: its button said
  "expired", and a typed `yes` could approve a newer ask. An ask now
  holds a kernel lock on its pending-question file while it waits, the
  same kind as the bot lock, and the question is open exactly as long as
  that lock is held.
- A killed `ask` (SIGKILL, a crash) ends with its process. Its button says
  "This question has expired." instead of accepting an answer nobody
  reads and editing the message to "Answered". On SIGTERM and SIGHUP (an
  agent's tool timeout, a closed terminal) `ask` ends its question as
  unanswered before it exits with 128 + the signal number; a SIGHUP
  ignored by `nohup` stays ignored.
- A failed or killed ask no longer blocks typed replies for its whole
  timeout (up to an hour for a webhook ask); see Changed for the
  60-second rule.
- An ask answered on Telegram still counts on ntfy for 60 seconds, and
  the other way round: its question and buttons are still on that
  phone, so a typed `yes` there no longer approves the next ask.
- A restarted Telegram bot no longer answers every message of the
  replayed chat backlog with "Reply not used". It first reads the backlog
  without waiting and only records refused replies in it. After that it
  sends at most one notice per reason a minute; the others are recorded
  as `stale_answer` with the reason they were not sent.
- A pending-question file that cannot be locked (a state directory on a
  filesystem without file locks) makes `ask` fail with one error line and
  exit 3, also when Telegram is configured. Before, the ntfy half ended in
  a thread traceback and the ask went on with Telegram alone.
- An HTTP 500 no longer counts as proof that a question never reached the
  phone. ntfy delivers a message to its subscribers before it writes its
  cache, and answers 500 when that write fails; each retry delivered
  another copy. The question's marker was deleted, and a `yes` typed under
  those copies approved another open ask. Only a 4xx other than 408, or
  Telegram `ok: false`, deletes it now.
- A typed reply that reached agentbell late was judged against the
  questions open at that moment. After a lost connection, or while the
  Telegram bot was down, a `yes` typed while two questions were open went
  to the one still open once the other had ended, or had been answered
  with its button in the meantime. It is now refused when it arrives more
  than 30 seconds after it was sent, and an ended question counts for
  every reply that may have been sent while it counted.

#### Queue and quiet hours

- A quiet window ending at 23:59 includes that minute: `00:00-23:59` runs
  until midnight. Other end minutes stay exclusive (14:00 is not quiet in
  `13:00-14:00`).
- Queued notifications retried during quiet hours are deferred until the
  window ends, unless they are at or above the quiet-hours priority or
  were sent with `--force`.
- Items the quiet-hours drain holds overnight keep their original queue
  time, so the 24-hour expiry still applies while a channel stays down.
- Queued notifications keep their order when several land in the same
  second, also on a clock that advances in coarse steps (Windows before
  Python 3.13), so a full queue drops the oldest item.
- A numeric priority (`4` from the webhook or MCP, or stored by an older
  version) is used as that priority (`high`). It used to go out as
  `normal`, ignore the quiet-hours threshold and crash `history`,
  `queue list` and the queue drain.

#### Delivery and hooks

- A dropped connection (`RemoteDisconnected`, `ConnectionResetError`,
  `IncompleteRead`) is retried and then queued instead of crashing hooks,
  `watch`, `doctor` and the Telegram bot. The approval stream reconnects.
- A desktop notification cut short by a hook's budget is queued, not
  reported as unavailable. The OS notification helper is capped by the
  send timeout (the Windows toast gets at most 10 s instead of 15 s).
- `config.json` with a UTF-8 BOM is accepted. A config that is valid JSON
  but not an object is reported as a config error instead of crashing.
- Paths and arguments with undecodable bytes are delivered with U+FFFD in
  place of the bad bytes; history keeps the original. A NUL character is
  dropped on every channel, and one channel failing unexpectedly no
  longer stops the others.
- ntfy titles and tags with emoji, CJK or umlauts arrive intact (RFC 2047
  encoded-words, decoded by ntfy 2.4.0 and later).
- Consoles that cannot show emoji (a Windows pipe using cp1252) print `?`
  instead of crashing. `--json` output is unchanged.
- Very long messages are trimmed in linear time; a 400 KB message used to
  take minutes.
- Parallel sessions no longer share one start time. The start marker is
  per `session_id` from the hook payload, or per working directory when
  the host sends none. Markers older than a day are deleted.
- `init` no longer crashes in containers without a passwd entry or `USER`.
- `verify` counts a queued hook push as delivered once the queue has
  delivered it.

#### `watch`

- `watch` keeps the command's exit code when the push fails in an
  unexpected way (a config value of the wrong kind, an unwritable state
  directory). It prints one stderr line, pointing to `agentbell doctor`
  when a push could not be delivered, and no longer says "notification
  not sent" when one channel delivered it.
- Windows: Ctrl-C or Ctrl-Break while `watch` starts or runs the command
  no longer kills agentbell or the starting command, and `watch` sends no
  console control events of its own.
- Windows: `watch -- npm test` (also yarn, pnpm and any .bat or .cmd on
  PATH) runs. A command CreateProcess finds runs as before; otherwise
  `watch` looks it up on PATH as a .com, .exe, .bat or .cmd file, not in
  the current directory. Batch-file arguments pass exactly as typed
  (`lodash@^4.17.0` keeps its `^`).

#### Telegram bot and service

- The bot lock is a kernel file lock held while the bot runs. A killed or
  crashed bot never blocks the next start, a reused process ID no longer
  looks like a running bot to `bot status`, `doctor`, `ask` or
  `uninstall`, and two bots started at once cannot both run. After a
  clean stop `bot.lock` stays, empty; `bot status` says `stale` only for
  a bot that died. A filesystem without file locks makes the bot refuse
  to start with an error.
- SIGTERM (`systemctl --user stop`, `launchctl unload`, `kill`) is a clean
  stop: the bot releases its lock and exits 0, so systemd no longer marks
  the unit failed or restarts a bot stopped on purpose. The launchd job
  uses `KeepAlive` with `SuccessfulExit=false`.
- `bot install-service` writes a service that starts from a checkout or
  `python3 -m agentbell` (`python agentbell.py bot run`), and the systemd
  unit uses `Type=exec`, so a command that cannot start fails the install.
  The unit or plist carries the installing shell's `AGENTBELL_CONFIG` and
  `AGENTBELL_STATE_DIR`; a license key held only in `AGENTBELL_LICENSE` is
  refused with a pointer to `agentbell license activate`. Re-running it
  restarts a running bot. Checked with `systemd-analyze verify`, plistlib
  and mocked `systemctl` / `launchctl`, not yet under a real service
  manager.
- `agentbell uninstall` stops, disables and deletes the bot service
  (systemd user unit and its `default.target.wants` link, or the launchd
  plist) first. The enabled unit used to restart a deleted binary every
  10 seconds.
- Two status probes at the same moment (parallel asks, `bot status`
  during an ask) no longer report a stopped bot as running.
- The bot tells Telegram's two 409 conflicts apart (an active webhook,
  another program polling the same token) and no longer calls unrelated
  errors containing "409" a webhook problem.
- Bot token errors are shown once and say what is wrong. BOM and
  zero-width characters around a pasted token are removed; one inside it
  is reported as an invalid token instead of a traceback.
- The 20-second drain budget per bot cycle also covers deferred
  notifications, so a hung ntfy server no longer stalls the bot for about
  100 seconds and removes the approval buttons.
- Windows: a heartbeat write that collides with a reader of `bot.json` is
  reported on stderr instead of ending the bot. macOS: `install-service`
  without a runnable `launchctl` reports an error instead of a traceback.

#### Claude Code, Gemini CLI and Qwen Code settings

- `hooks install` no longer crashes on an HTTP hook with `headers`, on a
  matcher that is a list, or on a non-UTF-8 settings.json. A settings.json
  with comments or trailing commas stays byte-for-byte unchanged; the
  refusal names the file and prints the hooks to add by hand, and the
  other agents in the same command are still processed.
- Re-running `hooks install` (or `init`) keeps a `--min-duration` you
  changed in agentbell's own hook for Claude Code, Qwen Code, Codex and
  Kimi Code, instead of resetting it to 60.
- Symlinked JSON configs are updated where they point, with their mode
  kept (`~/.claude/settings.json -> ~/dotfiles/claude.json`). A link into a
  read-only location (Nix/home-manager) gives `cannot write <link> (a
  symlink to <target>)` and exit 1 instead of a traceback.
- Hooks installed from a checkout run: they start `agentbell.py` with the
  Python interpreter that installed them (Claude Code, Gemini CLI, Qwen
  Code, Codex, Kimi Code, OpenCode plugin). Hooks installed under PyPy or
  free-threaded Python are recognized as agentbell's on reinstall.
- Windows hook commands run in the shell each host uses. A plain path,
  including non-ASCII letters, is written unquoted with forward slashes;
  other paths are quoted per host, and Qwen Code hooks carry `"shell":
  "powershell"`. Checked with cmd, PowerShell 5.1 and Git Bash command
  lines built like the hosts', not inside the host apps.
- An install that changes nothing no longer rewrites the file, and new
  agent config files follow your umask instead of 0644.

#### Codex and Kimi Code config.toml

- Codex's own config is no longer deleted. Tables Codex writes between the
  agentbell markers (`[tui]`, `[plugins.*]`, `[notice.*]`,
  `[hooks.state]`, a hook of yours) are moved out of the block and kept;
  the same applies to Kimi Code.
- Everything outside agentbell's part stays byte-for-byte: CRLF or LF line
  endings, final newlines, trailing blank lines and comments, the comment
  lines that introduce the next table, a `#` inside a quoted key, and the
  file mode. A file that mixes CRLF and LF gets its majority line ending.
- A symlinked config stays a symlink; the file it points at is updated.
- A config without a trailing newline stays valid: `features.hooks = true`
  goes on its own line, and a copy glued to the last line is removed.
  Only the `features.hooks = true` line with agentbell's marker comment is
  agentbell's. A flag without it is kept wherever it stands, under
  `[profiles.x]` or directly above agentbell's block. A bare flag that a
  1.3.0rc1 or older install appended there now stays as well, and install
  adds the top-level one.
- A config with inline hooks (`hooks = {...}`, `Stop = [...]`, dotted
  `hooks.Stop = ...`, a plain `[hooks.Stop]` table) is not turned into
  invalid TOML: nothing is written, and a note names the entry and how to
  convert it. A non-UTF-8 config.toml is refused with one line.
- Uninstall keeps the `[[hooks.Stop]]` parent when a later hook in that
  group is not ours, so `hooks.Stop` stays an array.
- Hooks without markers: Kimi Code deletes the marker comments, and
  uninstall now removes tables whose command is exactly ours; a wrapper
  is left, with a note. A Codex lifecycle hook written by hand counts as
  installed (no second copy; uninstall removes it, including one you wrote
  that is identical to agentbell's). A Kimi hook of yours under another
  event is not taken for ours, and a commented-out hook does not count.
- A hook without markers that starts agentbell from a path that no longer
  exists shows as `update needed` in `hooks status` and `doctor`, and
  `hooks install` rewrites just that path.

#### MCP

- The MCP server no longer exits on a JSON-RPC batch, a non-object
  message, unparsable input or an unreadable `config.json`. Batches get
  batched responses, invalid input gets the JSON-RPC error (-32700,
  -32600, -32602), a broken config fails only the tool call, and
  notifications (no `id`) get no response.
- MCP registrations made from a checkout (or the pip module without the
  launcher on PATH) run `python agentbell.py mcp`, and so does the
  canonical entry `integrate` prints.
- `mcp add codex` (and `chatgpt-desktop`) repairs a stale `command` and
  `args` together instead of reporting "already present", keeps the
  file's line endings, and leaves a registration it did not write (uvx,
  pipx run) unchanged with a hint.
- Uninstall removes only the `[mcp_servers.agentbell]` table and its
  sub-tables, not the comment lines after it; a quoted
  `[mcp_servers."agentbell"]` header is found, and a comment mentioning
  the table is not counted as an entry.
- `mcp add` lists every parameter of the two tools (`agent` for `notify`,
  `yes_label` and `no_label` for `ask_approval`), read from the tool
  schemas the server offers.

#### Rule files and OpenCode

- A non-UTF-8 AGENTS.md (cp1252, latin-1) no longer crashes `hooks
  status`, `doctor`, `verify` or Aider and OpenCode installs; a UTF-16
  file is left unchanged with a note.
- Adding, repairing or removing the block in AGENTS.md, `.rules`,
  `.clinerules` and `.continue` rule files leaves every other byte as it
  was (line endings, blank lines, indentation). A `.clinerules` you
  created is not deleted when it ends up empty; an older single
  `.clinerules` file gets the block instead of a crash.
- A marker mentioned in running text (in backticks in AGENTS.md) is your
  text, and the text between it and agentbell's block is no longer
  deleted. A marker line without its partner leaves the file unchanged
  with a note. `uninstall --yes` purges only blocks that run an agentbell
  command.
- `hooks install` and `init` say "NOT installed" with the reason instead
  of "already installed" when a block was skipped. `init` reports a
  refused config on stderr and no longer claims that rule-file agents
  work in every repo.
- A symlinked AGENTS.md is no longer reported as "already gone" on
  uninstall; the file that still holds the block is named.
- The OpenCode plugin no longer measures a turn from the previous turn's
  prompt, so short turns stay silent. Re-run `agentbell hooks install
  opencode`; `hooks status` shows `update needed` until then.

#### Uninstall

- With `AGENTBELL_CONFIG_DIR` or `AGENTBELL_STATE_DIR` set, uninstall
  removes only agentbell's own files there (also inside `runs`, `queue`,
  `deferred`), removes the directory only when it is empty, and lists
  what it kept. The default directories are still removed whole. A
  symlinked directory is reported as kept.
- A config file set with `AGENTBELL_CONFIG` is listed and removed. When
  the config file is a symlink, uninstall says that only the link goes
  and the target still holds the license key and tokens.
- A pipx install is found even when `pipx list` exits 1 over a broken
  venv; if pipx cannot be asked, the plan says so. Other pip `--user`
  packages whose name starts with `agentbell` are left alone.
- Windows: the `Scripts\agentbell.exe` launcher of a `pip install --user`
  is removed; when uninstall runs through that launcher, the dry run
  points to `py -m agentbell uninstall --yes`.
- An unreadable `AGENTBELL_*_DIR` is listed and makes `--yes` fail, instead
  of "already fully removed". Hooks you wrote that still call agentbell
  are reported as kept, without "Done".
- The running-bot warning says the service is stopped first; only a bot
  started by hand has to be stopped by you.
- `uninstall --yes` removes the `.cursor/rules`, `.windsurf/rules`,
  `.continue/rules` and `.clinerules` folders (and `.cursor`, `.windsurf`,
  `.continue`) when agentbell's rule file was the last thing in them, and
  says which folders it removed. A folder with anything else in it, or a
  symlinked one, stays.

#### doctor, verify, test, history, config

- `doctor` no longer crashes on an invalid ntfy server and no longer
  suggests ntfy.sh for one; `verify` fails its delivery check for it.
- One topic rule for `doctor`, `verify`, `init` and `config set ntfy.topic`:
  longer than 54 characters fails (`ask` adds `-responses`, and ntfy
  allows 64), a trailing newline is refused, shorter than 16 characters
  warns. Sending still accepts up to ntfy's 64, so a longer topic from an
  older config keeps notifying; `ask` refuses it with that reason, and
  `doctor` and `verify` say that notifications still go out. The send-time
  error names both limits instead of "max 64 chars".
- A damaged history line no longer crashes `history` or `verify`: bytes
  that are not UTF-8 show as U+FFFD, lines that are not a record are
  skipped and counted. An unreadable history is a warning in `verify`
  (`--json` still prints JSON) and a clear error in `history`; `doctor`
  reports it. A record with a non-text project no longer crashes
  `verify --project`.
- A Telegram-only setup is not blamed on ntfy: `test` needs no ntfy topic
  and names the channel that failed, and `doctor` and `verify` report
  ntfy as not used.
- An MCP `notify` call no longer verifies an installed hook; an agent
  with hooks needs a real lifecycle event. MCP-only hosts are still
  verified by their first notify call.
- A Codex or Kimi `config.toml` that is not UTF-8 is named by `doctor`
  (and reported by `verify`, without the path). Before, `doctor` said
  "not registered" and "not wired up" and suggested `mcp add` or `hooks
  install`, which refuse the same file.
- The webhook server reports a port already in use as an error instead of
  a traceback. It no longer looks up its own address in DNS at start,
  which could delay the start by several seconds (macOS, `::1`).
- The webhook server reads the request body before it refuses a request
  (401, 403, 413; up to 1 MiB of a body over the cap). Before, a
  client on Windows got a connection reset instead of the answer.
- `config set ntfy.auth none` (and `ntfy.action_auth none`) no longer
  warns that a credential travels over plain http while clearing it, and
  shows the cleared value as `null` instead of `"<redacted>"`.
- `license activate` with an invalid key, `init` and the premium refusal
  no longer point to a purchase e-mail or a price: there is no online
  checkout, and they say to e-mail basti@moodtechsolutions.com or open a
  GitHub issue for a key, as the README does.

### Security

- **Credentials no longer follow a server change.** Re-running `init` keeps
  the stored server and topic as defaults, so a self-hosted ntfy password
  is no longer posted to ntfy.sh. `init` and `config set ntfy.server`
  clear `ntfy.auth` and `ntfy.action_auth` (and say so) whenever the
  server really changes, including from an empty one. Before, the old
  button token was published in the button headers on the new server.
  Letter case, a trailing slash, the default port, or fixing a broken port
  on the same host keep them.
- **Approvals fail closed.** If the ntfy response topic cannot be checked
  for old replies and ntfy is the only channel, `ask` stops with exit 3
  after one retry instead of letting a previous question's `yes` approve
  the new one; with Telegram also configured, only ntfy is dropped. A
  typed reply is never handed to an older question while a newer
  question's delivery is uncertain, or because of a marker left by a
  killed ask or an older version (see Changed for the one-question rule).
- **Windows batch-file injection (BatBadBut, CVE-2024-24576).** `watch`
  ran .bat and .cmd tools through CreateProcess's implicit `cmd /c`, which
  parsed the arguments again: `&` ran a second command, `|` and `<` broke
  it, `^` disappeared. `watch` now builds the `cmd.exe /d /v:off /s /c`
  line itself, quotes every argument, and refuses `%`, `"`, CR and LF.
  `/d` also means cmd's AutoRun command no longer runs first.
- **No write-through of planted symlinks.** A symlink at the temp name
  (`AGENTS.md.tmp`, `mcp.json.tmp`, a Codex or Kimi temp file) is not
  followed; the temp file is created with `O_EXCL`. `mcp add --project`
  refuses a project config that resolves outside the project, and rule
  files inside a repository still refuse a symlink destination.
- **Windows toast text is no longer PowerShell source.** A typographic
  apostrophe closed the quoted string on Windows PowerShell 5.1, and the
  rest of the message ran as code. Title and message now reach the script
  through the child's environment.
- **A bot token with a space or carriage return is not copied into error
  messages**; a trailing newline is still accepted.

### Internal

- The test suite no longer touches the developer's real agent configs,
  pip `--user` or pipx install, whatever `KIMI_CODE_HOME`, `QWEN_HOME`,
  `XDG_*`, `APPDATA`, `PYTHONUSERBASE`, `PIPX_*` or `AGENTBELL_CONFIG` are
  set to.
- The test suite removes its temp dirs at exit. Every temp dir a test
  makes lives under one root, and a part that cannot be removed is
  reported on stderr.
- HTTP error responses are closed once their body is read, in agentbell
  and in the tests' own HTTP clients, so Python 3.14 prints no
  `ResourceWarning`. One Telegram test no longer reaches the real Bot API.

## 1.6.3 — 2026-09-03 — review hardening

The thirteen findings from the 2026-08-22 review were re-verified against
v1.6.2 before changing code. All confirmed or partially confirmed cases are
fixed here; the disposition and rejected alternatives are in
`DECISIONS.md` §18.

### Added

- **PyPI release preparation.** Tags matching `v*` now build a wheel and
  source distribution, check their metadata, transfer the exact artifacts to
  a separate publish job and upload through PyPI Trusted Publishing. The
  workflow uses the protected `pypi` GitHub environment and OIDC; no upload
  token is stored in the repository. v1.6.3 was published to PyPI through
  this workflow on 2026-09-03, and `pipx install agentbell` is the primary
  install path. The release gate also asserts that the only Python runtime
  file in wheel and sdist is `agentbell.py`, that the console
  entry point is present, and that tests, `internal/` and `.license-secret`
  are absent. Rationale: `DECISIONS.md` §19.

### Verified

- `agentbell uninstall` recognizes a pipx-managed package and delegates its
  removal to `pipx uninstall agentbell`; regression tests cover both pipx
  detection and the purge action. A real isolated pipx artifact test is
  recorded in `FIELD_TEST.md`. After publication, `pip install agentbell`
  from public PyPI was verified on 2026-09-05 (`FIELD_TEST.md`); a `pipx
  install` from public PyPI is not recorded there.

### Fixed

- **Concurrent `ask` processes cannot consume the same ntfy reply.** The
  consumed-answer log now has a cross-process atomic lock in addition to its
  thread lock. A claim-storage failure rejects the reply, writes an
  `answer_claim_failed` history record and appears as an `approval answers`
  WARN in `doctor`; it is never silently accepted.
- **Sensitive unauthenticated ntfy asks warn every time**, including repeated
  MCP calls in one server process. The narrow heuristic now recognizes
  deploying, deleting and publishing forms as well as the previous base
  forms.
- **`verify` is internally consistent and project-scoped.** An outdated Aider
  block is reported as installed wiring that needs repair, not simultaneously
  absent and repairable. New hook records carry their normalized project;
  `verify --project` counts only that directory (including descendants), and
  deliberately does not guess a project for legacy records that lack the
  field.
- **Windows and wrapped hooks stay visible.** `doctor` now WARNs that portable
  stdlib checks cannot verify a Windows config ACL and points to `icacls`.
  The hook parser preserves bare Windows-path backslashes. A user-owned shell
  wrapper is labeled `user wrapper` by status/doctor/verify, is never removed,
  and blocks automatic installation of a second lifecycle hook.
- **The self-integration manifest documents `ask` exit 3** for configuration,
  publication and answer-channel errors.
- **One ntfy lookback constant feeds both read paths.** Ask streaming/polling
  and `agentbell test` now share the same 90-second server-relative margin.
- **`verify` reads `AGENTS.md` once per report**, reusing the Aider state for
  both status and the structured repair notice.
- **The order-dependent verify tests no longer share `claude` observations.**
  Their history input is isolated; the full 316-test suite passed in normal
  order and in two randomized class orders locally.

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

### Field-verified

- The v1.6.1 OpenCode plugin passed its real turns on 2026-09-03 (OpenCode
  1.18.26): a 62 s turn produced exactly one push with its duration, turns
  of 9 s and 51 s stayed silent as `hook.skipped_short`, no duplicates.
  `FIELD_TEST.md` row 11.

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
- **Windows onboarding.** The README documents a PowerShell setup from a
  checkout (`py -m pip install --user .`, then `py -m agentbell init`, which
  works before the Python Scripts folder is on `PATH`). When `agentbell` is
  not on `PATH`, `doctor` on Windows now prints a PowerShell command that
  adds the user Scripts folder to the user `PATH`, instead of the POSIX
  `export PATH=…` line. The config file's POSIX mode check (`chmod 600`) is
  skipped on Windows, where it does not apply.
- **Warning for sensitive approvals without ntfy authentication.** When an
  `ask` goes out over ntfy without `ntfy.auth` and the question matches a
  narrow set of high-impact patterns (production deploys; deleting a
  database, cluster, bucket or production resource; rotating, revoking or
  exposing credentials; money transfers; firewall or access-control
  changes), agentbell writes a warning to stderr: anyone who knows the
  topic can answer the question. The warning is a reminder, not a block, and
  it cannot judge every action's real impact. In this release it fired once
  per process; 1.6.3 made it fire for every matching ask.
- **`hooks status` shows how reliable each integration is.** A new column
  reads `hook` for deterministic lifecycle hooks and plugins, and `~ rule`
  for rule-file instructions the agent is asked to follow (best effort by
  construction).
- **CI runs on Windows.** The test matrix gained Windows jobs (Python 3.11
  and 3.13), and one job per OS installs the package and runs
  `agentbell --help` as a packaging smoke test.

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

- **A free-text reply can no longer answer two parallel asks.** On ntfy
  every open `ask` polls the same response topic. When the newest ask took a
  free-text reply and removed its pending marker, an older ask polling the
  same topic could find the marker gone, promote itself to newest and
  consume the same reply. The claim is now recorded durably in the state
  directory (`ntfy-consumed`, the last 200 message ids) before the marker is
  removed, and every poller skips claimed ids. Found on a slow CI runner.
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
  `quiet_hours_min_priority`, `approval_timeout`; 1.5.0 and later also
  accept `webhook.token`). Values are validated:
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
