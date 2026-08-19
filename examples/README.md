# Examples

Reference copies of what `agentbell` writes for you, plus patterns for
wiring your own scripts. **You rarely need to copy any of this by hand** —
`agentbell init`, `agentbell hooks install <agent>` and
`agentbell mcp add` write the real thing, with your real binary path, and
merge into existing config instead of overwriting it.

The paths below all say `/home/you/.local/bin/agentbell`. Yours comes from
`command -v agentbell`; `agentbell mcp add --print` prints the snippets
with it already filled in.

---

## Agent hooks

### `claude-settings.example.json`

What `agentbell hooks install claude` merges into `~/.claude/settings.json`.

`UserPromptSubmit` records a start marker only (`--silent`: no notification, no
output, no network), so the "finished" push can report the turn duration.
`--min-duration 60` on the `Stop` hook keeps turns shorter than a minute
silent — failures and turns of unknown duration always send a notification.

### `codex-config.example.toml`

What `agentbell hooks install codex` appends to `~/.codex/config.toml`.
Same start-marker and `--min-duration` pattern as Claude Code. The
`agentbell:start` / `agentbell:end` comments delimit the block, so
`agentbell hooks uninstall codex` removes exactly this and nothing else.

`features.hooks = true` is written *above* the first `[table]` header — in TOML
a bare dotted key after a table header belongs to that table, not to the root.

### `gemini-settings.example.json`

What `agentbell hooks install gemini` merges into `~/.gemini/settings.json`.
Gemini CLI's `AfterAgent` fires once per turn, after the final response. It has
no failure event, so Gemini gets "finished" only.

### `opencode-plugin.example.js`

What `agentbell hooks install opencode` writes to
`~/.config/opencode/plugin/agentbell.js` (global — applies in every repo),
or to `.opencode/plugin/agentbell.js` with `--project`.

A real plugin rather than a rule file: it listens on `session.idle`,
`session.error` and `permission.asked`, and filters out subagent sessions so
one turn is one notification.

---

## MCP registration

`agentbell mcp add` registers a stdio MCP server exposing `notify` and
`ask_approval`. Three config shapes cover every client — pick the one your
client uses, or just run `agentbell mcp add --print`. Restart the client
afterwards.

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

### Anything else

Windsurf, Zed, LM Studio and friends: `agentbell mcp add --print` gives you
the raw snippet to paste. ChatGPT on the **web** accepts remote MCP servers
only, so it cannot use a local stdio server at all.

---

## Scripts and services

### `custom-agent.sh`

The pattern for a long-running script, CI job or agent wrapper of your own:
run the work, notify on success, notify *and* pass the exit code through on
failure. The failure branch is part of the `if` on purpose — under
`set -e` a `$?` check on the following line would never be reached.

### `watch.sh`

A one-line wrapper around `agentbell watch`, which does all of the above for
you: exit code, duration, priority by outcome, exit code passed through.

```bash
./examples/watch.sh "npm run build"
```

### `webhook.sh`

Notifying from a box that has no `agentbell` install — CI, a VPS, a
container. Start `agentbell server` once on the machine that *does* have it,
then POST JSON to `/notify` or (blocking until you answer) `/ask`.

Set `webhook.token` and send `Authorization: Bearer <token>`. The server
refuses to listen on a non-loopback address without one.

### `agentbell-bot.service`

A systemd user unit for the premium Telegram answer daemon.

**Prefer `agentbell bot install-service`** — it writes the unit with the
absolute path of your actual install, and falls back to a `nohup` line where
systemd isn't running (WSL without systemd, containers). The `%h/.local/bin`
path in this reference copy is only correct for the plain-copy and
`pip --user` install paths; a pipx or venv install lives elsewhere.
