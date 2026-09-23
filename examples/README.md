# Examples

Reference copies of what `agentbell` writes for you, plus patterns for
wiring your own scripts. **You rarely need to copy any of this by hand** —
`agentbell init`, `agentbell hooks install <agent>` and
`agentbell mcp add` write the real thing, with your real binary path, and
merge into existing config instead of overwriting it.

The paths below all say `/home/you/.local/bin/agentbell` (`/Users/you/...`
in the macOS plist). Yours comes from `command -v agentbell`;
`agentbell mcp add --print` prints the snippets with it already filled in.
From a git checkout, `agentbell.py` is not executable, so every command
starts with the Python interpreter and then the path to `agentbell.py`.

Covered here: the Claude Code, Codex, Gemini CLI, Kimi Code, Qwen Code and
OpenCode hook configs, the MCP registrations, and the bot service files.
Not copied here: the rule-file blocks for Cursor, Windsurf, Cline, Continue,
Zed and Aider. `agentbell hooks install <agent>` prints the file it wrote,
and the block sits between `agentbell:start` / `agentbell:end` markers.

Using an agent that has no example here? `agentbell integrate` prints a
self-integration guide any agent can follow (and `--json` a machine-readable
manifest); `agentbell verify` then shows whether its events actually arrive.

---

## Agent hooks

### `claude-settings.example.json`

What `agentbell hooks install claude` merges into `~/.claude/settings.json`.

`UserPromptSubmit` records a start marker only (`--silent`: no notification, no
output, no network), so the "finished" push can report the turn duration.
`--min-duration 60` on the `Stop` hook keeps turns shorter than a minute
silent — failures and turns of unknown duration always send a notification.
The two `Notification` hooks cover Claude Code waiting for your input
(`agent_needs_input` → `input_required`) and showing a permission dialog
(`permission_prompt` → `permission_required`).

### `codex-config.example.toml`

What `agentbell hooks install codex` appends to `~/.codex/config.toml`.
Same start-marker and `--min-duration` pattern as Claude Code. The
`agentbell:start` / `agentbell:end` comments delimit the block, so
`agentbell hooks uninstall codex` removes exactly this and nothing else.

`features.hooks = true  # added by agentbell` is written *above* the first
`[table]` header — in TOML a bare dotted key after a table header belongs to
that table, not to the root. The trailing comment is how
`agentbell hooks uninstall codex` tells this line from an identical one you
wrote yourself: it removes only the marked line.

### `gemini-settings.example.json`

What `agentbell hooks install gemini` merges into `~/.gemini/settings.json`.
Gemini CLI's `AfterAgent` fires once per turn, after the final response. It has
no failure event, so Gemini gets "finished" only.

### `kimi-config.example.toml`

What `agentbell hooks install kimi` appends to `~/.kimi-code/config.toml`
(`$KIMI_CODE_HOME/config.toml` when that is set). Same start-marker,
`--min-duration` and failure hooks as Claude Code, as `[[hooks]]` tables.
Kimi rejects the whole config for any key other than `event`, `matcher`,
`command` and `timeout`, so these hooks have no `async` flag and a
10-second timeout. The `agentbell:start` / `agentbell:end` comments delimit
the block for `agentbell hooks uninstall kimi`.

### `qwen-settings.example.json`

What `agentbell hooks install qwen-code` merges into `~/.qwen/settings.json`
(`$QWEN_HOME/settings.json` when that is set). Qwen Code reads Claude Code's
hook format: the same `UserPromptSubmit`, `Stop` and `StopFailure` hooks.
On Windows each hook also carries `"shell": "powershell"`.

### `opencode-plugin.example.js`

What `agentbell hooks install opencode` writes to
`~/.config/opencode/plugin/agentbell.js` (global — applies in every repo),
or to `.opencode/plugin/agentbell.js` with `--project`.

A real plugin rather than a rule file: it listens on `session.idle`,
`session.error` and `permission.asked` / `permission.updated`, and filters out
subagent sessions so one turn is one notification. A user message
(`message.updated`) marks the start of a turn, so the "finished" push carries
the turn duration and `--min-duration 60` keeps turns shorter than a minute
silent, as for Claude Code. A second idle event for the same session within
10 seconds is dropped, and permission events within 3 seconds collapse into
one push.

---

## MCP registration

`agentbell mcp add` registers a stdio MCP server exposing `notify` and
`ask_approval`. Clients differ in the config shape they read — pick the one
below that your client uses, or just run `agentbell mcp add --print`.
Restart the client afterwards.

### `mcp-mcpservers.example.json` — the common shape

Used by Claude Desktop, Cursor, Gemini CLI, Qwen Code and Kimi Code:

| Client | File |
|---|---|
| Claude Code | `claude mcp add --scope user agentbell -- agentbell mcp` |
| Claude Desktop (macOS) | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Claude Desktop (Windows) | `%APPDATA%\Claude\claude_desktop_config.json` |
| Claude Desktop (Linux) | `~/.config/Claude/claude_desktop_config.json` |
| Cursor | `~/.cursor/mcp.json` (global) |
| Gemini CLI | `~/.gemini/settings.json` |
| Qwen Code | `~/.qwen/settings.json` (global; `--project` → `.qwen/settings.json`) |
| Kimi Code | `~/.kimi-code/mcp.json` (global; `--project` → `.kimi-code/mcp.json`) |

### `mcp-vscode.example.json` — VS Code / Copilot

Goes in the user `mcp.json` or `.vscode/mcp.json`. Note the key is `servers`,
not `mcpServers`, and each entry carries an explicit `"type": "stdio"`.

### `mcp-opencode.example.json` — OpenCode

Goes in `~/.config/opencode/opencode.json` (or `opencode.jsonc`). OpenCode uses
a `mcp` key, a `command` **array**, and an explicit `enabled` flag.

### Codex CLI and ChatGPT Desktop

These two share `~/.codex/config.toml`, so the snippet is TOML rather than
JSON:

```toml
[mcp_servers.agentbell]
command = "/home/you/.local/bin/agentbell"
args = ["mcp"]
```

### Zed

Zed reads MCP servers from `context_servers` in its `settings.json`
(command palette: `zed: open settings file`), not from `mcpServers`.
`agentbell mcp add --print` prints this block:

```json
{
  "context_servers": {
    "agentbell": {
      "command": "/home/you/.local/bin/agentbell",
      "args": ["mcp"],
      "env": {}
    }
  }
}
```

### Anything else

Windsurf, LM Studio and friends: `agentbell mcp add --print` gives you
the raw snippet to paste. ChatGPT on the **web** accepts remote MCP servers
only, so it cannot use a local stdio server at all.

---

## Scripts and services

### `custom-agent.sh`

The pattern for a long-running script, CI job or agent wrapper of your own:
run the work, notify on success, notify on failure, and exit with the work's
own exit code either way. The work runs in a subshell with `set -e`, and the
script reads `$?` afterwards. It is deliberately not called as an `if`
condition: bash ignores `set -e` there, so a failing step would not stop the
work and the job would report success. A notification that cannot be sent
is reported on stderr and does not change the exit code.

The commented approval gate does not use `ask && deploy`. `ask` exits 0 for
every reply it does not read as a denial, typed free text included, and
typed free text never approves. The gate reads `approved` from `--json` in an
explicit `if`, so it stops the deploy with or without `set -e`.

### `watch.sh`

A one-line wrapper around `agentbell watch`, which does all of the above for
you: exit code, duration, priority by outcome, exit code passed through.

```bash
./examples/watch.sh npm run build
```

Pass the command as separate words. `agentbell watch` runs it without a
shell, so a single quoted string such as `"npm run build"` is taken as the
name of one program and fails with exit 127. For pipes, redirects or `&&`,
wrap the line yourself: `./examples/watch.sh sh -c 'make && make test'`.

### `webhook.sh`

Notifying from a box that has no `agentbell` install — CI, a VPS, a
container. Start `agentbell server` once on the machine that *does* have it,
then POST JSON to `/notify` or (blocking until you answer) `/ask`.

Set a token with `agentbell config set webhook.token <random>` and send
`Authorization: Bearer <token>`; the script adds that header when
`WEBHOOK_TOKEN` is set. The server refuses to listen on a non-loopback
address without a token.

`/ask` answers HTTP 200 for every outcome — approved, denied, timed out or
free text — so `curl -f ... && deploy` is not a gate. The commented example
deploys only when the JSON response has `"approved": true`.

### `agentbell-bot.service`

A systemd user unit for the premium Telegram answer daemon.

**Prefer `agentbell bot install-service`** — it writes the unit with the
absolute path of your actual install, and falls back to a `nohup` line where
systemd isn't running (WSL without systemd, containers). The `%h/.local/bin`
path in this reference copy is only correct for the plain-copy and
`pip --user` install paths; a pipx or venv install lives elsewhere, and from
a git checkout `ExecStart` is `"/usr/bin/python3" "/path/to/agentbell.py" bot run`.

`Type=exec` makes `systemctl start` fail when `ExecStart` cannot be run. The
two `Environment=` lines pin the config file and state directory, because a
service does not see your shell's `AGENTBELL_*` or `XDG_*` variables; the
command fills in the ones your shell uses.

### `com.agentbell.bot.example.plist`

The macOS counterpart: what `agentbell bot install-service` writes to
`~/Library/LaunchAgents/com.agentbell.bot.plist` and loads with `launchctl`.
Same program arguments and environment as the systemd unit. `KeepAlive`
restarts the bot only when it exits with an error.
