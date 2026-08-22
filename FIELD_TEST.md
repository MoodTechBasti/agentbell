# FIELD TEST — 2-week checklist (v1.6.0)

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

### Current-checkout installation result (2026-08-22)

The post-merge Linux/WSL2 installation was refreshed from `main` at commit
`b1aa21e`. `install.sh` selected its standalone-copy fallback and installed
to the user `PATH`. The checkout's `agentbell.py` and the installed executable
had the same SHA-256 digest, proving that the command on `PATH` used the exact
reviewed checkout rather than an older copy carrying the same `1.6.0` version
number.

- `agentbell hooks install aider` migrated the project block; the following
  `agentbell hooks status` reported Aider as installed with no repair banner.
- `agentbell doctor` returned all checks OK for delivery configuration, ntfy,
  parallel ntfy/Telegram channels, the Telegram answer daemon, MCP
  registrations, and the state directory. Its agent-hooks check listed all 12
  native integrations as installed; this records detected installation status,
  not manual end-to-end proof for every agent in the table below.
- `agentbell test` published the notification and read it back from the ntfy
  server successfully.
- `agentbell ask "Does this reach my phone?" --timeout 60` was answered within
  the timeout and returned `approved` through the parallel ntfy/Telegram flow.

Topic names and other credential-like values are deliberately omitted from
this retained evidence.

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
| 23 | Self-integration (unknown agent) | hand `agentbell integrate` output to an agent NOT in the table (e.g. GitHub Copilot CLI), let it wire itself, finish one real turn | `verify --agent <slug> --since 10m` exit 0, event shown as real (not forced); push arrived with the slug as its label — **passed 2026-08-21** with GitHub Copilot CLI 1.0.80, see Tier-1 result below |
| 24 | Double integration is detected | wire the same agent via hooks AND the Appendix A rules block on purpose, finish a turn **of at least 60 s** (a shorter turn hits `--min-duration` on the hook side, leaves only one `run_completed` record and defeats the check) | Two pushes; `agentbell verify` WARNs "possible double integration"; after removing one mechanism, a fresh window is clean (`verify --since 10m`) — **passed 2026-08-21** with GitHub Copilot CLI 1.0.80; the default 7-day window keeps warning until the old duplicate records age out |
| 25 | Old Aider block migration | put a pre-scope agentbell block containing `--agent aider` in an `AGENTS.md` that also has user sections; run `agentbell hooks status`, `agentbell verify --json`, then `agentbell hooks install aider` | status/text verify show the bordered ACTION REQUIRED banner; JSON has `repair_notices[0].code == "aider_agents_block_outdated"`; reinstall preserves user sections and the next status says `installed` with no banner |

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

## Self-integration field test (rows 23-24, expanded)

The strong "any agent" claim stays gated until Tier 1 passes at least once
(see README "Any other agent" and DECISIONS §16g). Candidates were checked
against live vendor docs on 2026-08-21 — re-check before testing, vendors
drift. Evidence to keep per tier: exit codes, `verify --agent <slug> --json`,
`history --json` excerpt, the agent's own section-8 report, phone screenshot.

- **Tier 1 — unknown agent with real lifecycle hooks (highest value).**
  **PASSED 2026-08-21 with GitHub Copilot CLI 1.0.80** — see result block
  below. Further candidates, best first: Factory Droid
  (`~/.factory/hooks.json`), Auggie CLI (`~/.augment/settings.json`),
  Goose (hooks shipped 2026-05). Flow: install → run `agentbell integrate`
  → hand the output to the agent → let it wire itself → end the session →
  one real turn → `agentbell verify --agent <slug> --since 10m` (expect
  exit 0, event NOT marked forced) → re-run its integration (idempotent?)→
  `agentbell verify` (no duplicate WARN) → have it remove the wiring.

### Tier-1 result: GitHub Copilot CLI 1.0.80 (2026-08-21, WSL2, v1.6.0 branch)

The agent received **only** the `agentbell integrate` output as
agentbell-specific knowledge (no repo, source, README, config, state,
history, topic, token or credential access). Observed, with evidence kept
per the rules above:

- Detected its own native lifecycle hooks, chose the unreserved slug
  `github-copilot-cli`, proposed exactly ONE deterministic hook integration
  (`~/.copilot/hooks/agentbell-github-copilot-cli.json`) and waited for
  explicit user approval before writing outside the project.
- Wired all five events; used the paired anti-spam flags
  (`started --silent`, `run_completed --min-duration 60`).
- Its forced smoke test was correctly NOT counted as wiring proof; real
  `permission_required` events from real Copilot prompts and a real
  non-forced `run_completed` after an ~8-minute turn arrived on the phone.
- `agentbell verify --agent github-copilot-cli --since 10m` exit 0 (row 23
  **passed**).
- A second `integrate` run recognized the integration as complete and
  changed nothing — no second hook file, no second lifecycle mechanism.
- Removal via its own documented steps left no agentbell wiring behind.

Row 24 was exercised separately with an isolated Copilot user hook plus a
slug-scoped `AGENTS.md` block. One real turn produced two delivered
`run_completed` records two seconds apart; `verify --since 10m` stayed exit 0
and emitted exactly one "possible double integration" warning. A new turn
with an empty `COPILOT_HOME` (rules block only) produced one record and a
clean verify result. No persistent Copilot configuration was changed.

The same run exposed two false alarms in agentbell's own diagnostics, both
root-caused and fixed on this branch (DECISIONS §16i, CHANGELOG 1.6.0
"Fixed"): `agentbell test` reported "NOT delivered" for delivered messages
(local-clock poll cursor vs. server-side `since` filter under WSL2 clock
drift, poll errors swallowed), and `verify` warned "possible double
integration" for several real `permission_required` prompts inside one
second (the near-duplicate heuristic now only covers per-turn events).
- **Tier 2 — MCP-only mechanism: PASSED 2026-08-21.** GitHub Copilot CLI
  1.0.80 received an ephemeral stdio MCP config with custom instructions,
  built-in MCPs and shell use excluded from the test. It called AgentBell's
  `notify` tool once with `agent: "github-copilot-mcp"`; history recorded one
  delivered, attributed event and `verify` exited 0. This proves the MCP-only
  mechanism on a real host, not yet one of the MCP-only products originally
  proposed (Crush, Amp or Warp Agent CLI).
- **Tier 3 — rules-only mechanism: PASSED 2026-08-21 (3/3 turns).** A
  committed slug-scoped `AGENTS.md` was the only AgentBell integration in
  three independent GitHub Copilot CLI sessions. All three produced exactly
  one delivered, attributed event; `verify` exited 0 with no duplicates.
  This measures rules-only behavior on a hook-capable host, not yet a product
  whose only integration surface is a rules file.
- **Tier 4 — failure modes: PASSED 2026-08-21.** (a) with AgentBell absent
  from `PATH`, the JSON guide used the absolute checkout path and returned a
  concrete `path_fix`; (b) all-day quiet hours recorded one event as held and
  `verify` exited 0; (c) two concurrent same-slug sessions produced two held
  records plus a duplicate WARN while verify stayed exit 0; (d) an empty
  state for a forgotten rule returned exit 1, "nothing known", and the
  `agentbell integrate --agent tier4-forgotten` fix. All Tier-4 checks used
  isolated config/state directories.

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
- **A manual smoke test without `--force` is indistinguishable from a real event**: `verify` marks `--force` events as smoke tests, but an agent running the plain hook command by hand looks like real wiring. The trust anchor is procedural — end the session, do one real turn, then `verify`.
- **MCP attribution only works when the client passes `agent`**: the `notify` tool's `agent` argument is optional; calls without it appear in history without attribution.

## When something surprises you

```bash
agentbell doctor              # what is broken + the fix
agentbell history --limit 10  # what the tool thought it did
agentbell queue list          # what is still waiting
```

Paste those three outputs plus the command you ran — that is everything needed to diagnose it.
