# FIELD TEST — 2-week checklist (v1.5.0)

Goal: use the tool like a real user for two weeks and tick off every path below.
Budget: ~20 minutes for the first pass, then just use it.

## Prerequisites and evidence

- Record the OS, Python version (`python3 --version` or `py --version`), AgentBell version, ntfy server, and agent/editor version before starting.
- Use a real ntfy app subscription for the configured main and `-responses` topics; automated tests do not prove phone delivery.
- Treat Telegram, MCP desktop clients, agent hooks, and network recovery as separate integration tests. Do not mark one as passed because another worked.
- For every completed row, retain the command, exit code, relevant `agentbell history --limit 10` record, and either the received phone notification or an app/editor screenshot. Redact topics, tokens, and message content before sharing evidence.

**Start here:**

```bash
./install.sh
agentbell init            # wizard: topic, quiet hours, agent hooks, test push
agentbell doctor          # should be all OK
agentbell test            # real delivery check
```

On Windows PowerShell, replace `./install.sh` with `py -m pip install --user .` from the checkout, and use `py -m ...` for Python commands.

If anything is ever unclear: **`agentbell doctor`** prints the problem *and* the command that fixes it.

## Core checklist

| # | What | Command / setup | Expected |
|---|------|-----------------|----------|
| 1 | Install + init | `./install.sh && agentbell init` | Wizard completes, config saved, test push arrives, "NEXT STEPS" block printed |
| 2 | doctor | `agentbell doctor` | All checks OK; exit 0 |
| 3 | Basic notify | `agentbell notify "hello" --priority high --tags test` | Push with title/priority/tags |
| 4 | ask via ntfy (buttons) | `agentbell ask "Deploy to prod?"` | Question + Approve/Deny on the phone; CLI exits 0/1 |
| 5 | ask free text | `agentbell ask "Which env?" --no-buttons` | Typed answer is printed by the CLI, exit 0 |
| 6 | ask timeout | `agentbell ask "Anyone there?" --timeout 15` | Exit 2 after ~15 s, "timeout" |
| 7 | two asks in a row | run #4, answer it, immediately run #6 | The second ask does **not** inherit the first answer |
| 8 | watch | `agentbell watch -- sleep 5` / `agentbell watch -- false` | ✅ exit 0 + duration / 🔴 exit 1; exit code passed through |
| 9 | Claude Code hooks | `agentbell hooks install claude`, finish a real turn | `run_completed` push **with duration**; nothing printed into the session |
| 9b | No spam on short turns | ask Claude something trivial (< 60 s) | **No push**; `agentbell history` shows `hook.skipped_short` |
| 10 | Codex hooks | `agentbell hooks install codex`, finish a turn | `run_completed` push; check `/hooks` inside Codex if not |
| 11 | OpenCode plugin | `agentbell hooks install opencode`, finish a turn in **any** repo | `run_completed` push (plugin is global); exactly one push per turn |
| 12 | MCP in a desktop app | `agentbell mcp add claude-desktop chatgpt-desktop`, restart the app | The app lists `notify` / `ask_approval` and can call them |
| 13 | Telegram (premium) | `agentbell license activate <key>`, `agentbell init`, `agentbell bot install-service` | Question with Telegram buttons; a press answers the ask; `bot status` healthy after closing the terminal |
| 14 | Parallel channels | with ntfy + Telegram: `agentbell ask "Deploy?"` | Both get it; first answer wins; `channel` in `--json` says which |
| 15 | Quiet hours / defer | set quiet hours to "now", mode `defer`, send a low-prio notify | No push now; after the window / `queue flush` it arrives (3+ → one summary) |
| 16 | Offline queue | point the server at a dead port, `agentbell notify "offline"` | Exit 0 + stderr warning; `queue list` shows it; after fixing, `queue flush` delivers |
| 17 | history | `agentbell history --limit 20` | sent / suppressed / deferred / queued / ask results all visible |
| 18 | secrets | `agentbell config show` | license, bot token, ntfy password, webhook token all redacted; `ls -l` on config.json shows `-rw-------` |
| 19 | Full purge + re-init | `agentbell uninstall`, then `--yes`, then reinstall (`./install.sh` on macOS/Linux; `py -m pip install --user .` on Windows) and `agentbell init` | Dry run lists everything; purge removes binary/config/state/hooks/MCP and keeps foreign config |
| 20 | License status | `agentbell license status` | Correct premium state |
| 21 | Change one setting | `agentbell config set ntfy.topic <long-random>` | Written + re-subscribe hint; `doctor` turns the topic WARN into OK; no wizard needed |
| 22 | Setup survives a hiccup | in `init`, paste a bot token while offline | Says "could not reach Telegram", offers to keep it — the license key and topic entered before are **not** lost |

## Agent wiring — all 12

Rows 9–11 above cover the three agents in daily use here. These are all twelve,
so the gaps are visible instead of implied. **Everything unticked is untested,
not "known good".**

Install with `agentbell hooks install <agent>` (or `hooks install all`), then
finish one real turn in the agent and see whether the push arrives.

**Status legend:**
- `[x]` = manually tested with a real agent end-to-end
- `[ci]` = covered by automated tests (hooks install/uninstall, plugin parsing)
- `[ ]` = not yet tested in either form

Reliability class (shown by `agentbell hooks status`):
- **hook** = deterministic lifecycle hook/plugin (Claude, Codex, Gemini, Kimi, Qwen, OpenCode)
- **rule** = instruction in a rule file the agent is asked to follow — best-effort by construction (Cursor, Windsurf, Cline, Continue, Zed, Aider)

| Agent | Mechanism | Scope | Expected when a turn ends | Status |
|---|---|---|---|---|
| Claude Code | `~/.claude/settings.json` hooks | global | finished (with duration), failed, needs-input | `[ ]` run #9, #9b |
| Codex | `~/.codex/config.toml` `[[hooks.…]]` | global | finished (with duration) | `[ ]` run #10 |
| OpenCode | plugin in `~/.config/opencode/plugin/` | global | finished, failed, permission asked — exactly one push per turn | `[ ]` run #11 |
| Gemini CLI | `~/.gemini/settings.json` `AfterAgent` | global | finished only — Gemini exposes no failure event | [ ] |
| Kimi Code | `~/.kimi-code/config.toml` `[[hooks]]` | global | finished (with duration), failed | [ ] |
| Qwen Code | `~/.qwen/settings.json` hooks | global | finished (with duration), failed; hooks are async, so the end of a turn never waits on the network | [ ] |
| Cursor | `.cursor/rules/agentbell.mdc` | project | best-effort: the agent calls the CLI on finish / needs-input / failure | [ ] |
| Windsurf | `.windsurf/rules/agentbell.md` + legacy `.mdc` | project | best-effort; on a current (Devin) build the `.md` is the file that fires | [ ] |
| Cline | `.clinerules/agentbell.md` | project | best-effort | [ ] |
| Continue | `.continue/rules/agentbell.md` | project | best-effort | [ ] |
| Zed | `.rules` block | project | best-effort; if the repo already had a `.rules`, check its own content survived | [ ] |
| Aider | `AGENTS.md` block | project | best-effort; check a legacy OpenCode `AGENTS.md` block is not counted as Aider's | [ ] |

For the six rule-file agents, **"it didn't notify" is a result worth writing
down, not a bug to hunt.** They are prompt-driven; the useful number is how
*often* the agent follows the rule over two weeks, not whether it can once.

Also worth checking once, on any agent: `agentbell hooks status` lists it as
installed, and `agentbell hooks uninstall <agent>` leaves the rest of the
config file — including your own rules — intact.

## Things worth trying on purpose

- Kill the network mid-`watch` and see the queue catch it.
- Run two agents in two repos at once and check the notifications are attributable.
- Stop the Telegram bot and ask again — the question should arrive **without** buttons and say so.
- Run `agentbell uninstall` (dry run) in a repo that has other hooks/MCP servers and verify they are not listed.

## Known limitations (expected, not bugs)

- **Possible duplicate on retry**: a timed-out POST may have been delivered anyway, so a rare duplicate push can occur (approval answers are deduplicated by request ID).
- **Free text → newest ask**: with two asks open, a free-text reply answers the newest one (ntfy and Telegram alike). Button answers are matched by ID.
- **Deferred delivery needs activity**: without the `bot` daemon, deferred items go out with the next notification/`queue flush` after the window — not exactly at window end.
- **Queue/defer are local**: no multi-device sync; they live in the state dir on this machine.
- **Caps**: 200 deferred items, 100 queued items / 24 h age, history rotated at 2 MB.
- **Six agents are wired by rule file, not by hook**: Cursor, Windsurf, Cline, Continue, Zed and Aider have no lifecycle hooks, so their wiring is an instruction the agent is asked to follow. Best-effort by construction — the model can skip it, and sometimes will. The other six (Claude Code, Codex, Gemini CLI, OpenCode, Kimi Code, Qwen Code) have real hook systems and are deterministic.
- **ChatGPT web** cannot use a local MCP server (desktop app can).
- **Premium**: Telegram channel, parallel delivery and Telegram buttons need a lifetime key.

## When something surprises you

```bash
agentbell doctor              # what is broken + the fix
agentbell history --limit 10  # what the tool thought it did
agentbell queue list          # what is still waiting
```

Paste those three outputs plus the command you ran — that is everything needed to diagnose it.
