# agentbell

**One place for all your AI agent notifications — phone push + Approve/Deny from your pocket.**

[![CI](https://github.com/MoodTechBasti/agentbell/actions/workflows/ci.yml/badge.svg)](https://github.com/MoodTechBasti/agentbell/actions) [![License](https://img.shields.io/github/license/MoodTechBasti/agentbell)](LICENSE) ![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg) ![Dependencies: 0](https://img.shields.io/badge/dependencies-0-brightgreen.svg)

[Setup](#60-second-setup) · [Approval flow](#approval-flow-human-in-the-loop) · [Agents](#agents-what-gets-wired-up) · [Free vs. premium](#free-vs-premium) · [Commands](#quick-reference) · [MCP](#desktop-apps-and-editors-mcp) · [Trust model](#trust-model) · [Troubleshooting](#troubleshooting) · [FAQ](#faq)

---

Be honest — how many times have you already checked your screen today while your AI agent still wasn’t done?

You jump between ChatGPT, Claude, Gemini, Cursor, DeepSeek… Desktop apps, CLI, browser windows. Always checking. Always a bit on edge.

`agentbell` is the single place that tells you when something actually needs you.

- **Push notification** straight to your phone the moment an agent finishes, fails, or waits for input
- When it really needs a decision, you tap **Approve** or **Deny** from your phone — no running back to the keyboard
- One stdlib-only Python file, zero dependencies, free, no account, no server

Works with **Claude Code, Codex, OpenCode, Cursor, Gemini CLI, Kimi Code, Qwen Code, Windsurf, Cline, Continue, Zed, Aider**, the **ChatGPT and Claude desktop apps** (via MCP), CI jobs, and any shell script.

> **Status:** first public release (v1.5.0) — feedback wanted.

<!-- demo: docs/demo.gif — 20s: watch build → phone push → ask blocks → phone Approve → terminal continues -->

*(demo GIF coming — record with VHS)*

**Requirements:** Python 3.9+ · the free [ntfy](https://ntfy.sh) app (iOS/Android) · no account and no server of your own for the free core.

---

## 60-second setup

**No dev experience needed.** On macOS or Linux, open a terminal and run:

```bash
git clone https://github.com/MoodTechBasti/agentbell && cd agentbell
./install.sh       # picks pipx, pip --user or a plain copy — whichever works
agentbell init     # wizard: topic name, quiet hours, agent hooks, test push
```

That's it. `agentbell init` prints the next steps; `agentbell doctor` tells you exactly what's wrong and how to fix it at any point.

**Windows (PowerShell):** install from the same checkout without `install.sh`:

```powershell
git clone https://github.com/MoodTechBasti/agentbell
Set-Location agentbell
py -m pip install --user .
py -m agentbell init  # works even before the Scripts folder is on PATH
```

To make `agentbell` available to future terminals, hooks, and desktop MCP clients, run this once in PowerShell, then close and reopen PowerShell:

```powershell
$scripts = py -c "import sysconfig; print(sysconfig.get_path('scripts', scheme='nt_user'))"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
[Environment]::SetEnvironmentVariable("Path", "$userPath;$scripts", "User")
```

After reopening PowerShell, `agentbell doctor` confirms the installation.

> **New to the terminal?** You need Python 3.9+. On macOS: `brew install python3`. On Debian/Ubuntu: `sudo apt install python3`. On Windows, install Python from [python.org](https://www.python.org/downloads/) and use `py --version`. If `agentbell` is not found after installation, run `py -m agentbell doctor` for a copy-pasteable PATH fix.

### Developer path — full reference below

If you know your way around hooks, MCP and config files, jump straight to [Agents: what gets wired up](#agents-what-gets-wired-up), the [Quick reference](#quick-reference), or [MCP](#desktop-apps-and-editors-mcp).

---

## Approval flow (human in the loop)

This is the part no notifier gives you: the agent doesn't just tell you it's blocked, it **waits for your answer** — and you give it from your phone.

```
agent ── ask "Deploy to prod?" ──►  phone: 🔴 Approval requested
                                    [Approve] [Deny]   (or type any answer)
agent ◄── approved / denied / your text / timeout ◄──  phone
```

The answer travels **through ntfy itself** — you don't expose an endpoint:

- The question goes to your main topic with action buttons.
- Buttons and app replies publish to a dedicated `<topic>-responses` topic.
- `ask` waits on a live stream **plus** a polling fallback (robust when a server buffers streams).

**Button answers never get crossed.** Every question carries a 64-bit request ID, and button answers are matched to exactly that ID. Free-text replies carry no ID, so they always go to the newest open question — with two asks in flight, answer with the buttons. Answers that were already on the topic when a question is asked are ignored, so a new `ask` can never inherit an old answer.

**How your reply is read:**

| You reply | Result | Exit |
|---|---|---|
| tap **Approve**, or type `yes` / `ok` / `y` | approved | 0 |
| tap **Deny**, or type `no` / `deny` / `stop` | denied | 1 |
| `no, not before the release` | denied, reason kept | 1 |
| `use the staging cluster` | answered (text on stdout) | 0 |
| `yes, but use staging` | answered, **not** a bare approval | 0 |
| nothing | timeout | 2 |

Exit 0 means "approved **or** answered" — so if you chain `agentbell ask "Deploy?" && deploy`, a free-text reply also proceeds. For a strict gate, read the `--json` output and check `approved` together with `answer`.

> **Before you gate anything real on this:** the answer path is only as private as your topic name, and on public ntfy.sh anyone who knows that name can approve your questions. Read the [trust model](#trust-model) — for sensitive approvals, self-host ntfy with auth.

---

## Agents: what gets wired up

`agentbell hooks install <agent>` does the wiring for you. Nothing is written into your agent session, nothing blocks a turn.

| Agent | Mechanism | Scope | Events |
|---|---|---|---|
| **Claude Code** | `~/.claude/settings.json` hooks | global | finished (with duration), failed, needs-input |
| **Codex** | `~/.codex/config.toml` `[[hooks.…]]` | global | finished (with duration) |
| **OpenCode** | real plugin in `~/.config/opencode/plugin/` | global | finished, failed, permission asked |
| **Gemini CLI** | `~/.gemini/settings.json` `AfterAgent` | global | finished |
| **Kimi Code** | `~/.kimi-code/config.toml` `[[hooks]]` | global | finished (with duration), failed |
| **Qwen Code** | `~/.qwen/settings.json` hooks | global | finished (with duration), failed |
| **Cursor** | `.cursor/rules/agentbell.mdc` (`alwaysApply`) | project | finished, needs-input, failed |
| **Windsurf** | `.windsurf/rules/agentbell.md` (`trigger: always_on`) + legacy `.mdc` for pre-Devin builds | project | finished, needs-input, failed |
| **Cline** | `.clinerules/agentbell.md` | project | finished, needs-input, failed |
| **Continue** | `.continue/rules/agentbell.md` | project | finished, needs-input, failed |
| **Zed** | `.rules` block | project | finished, needs-input, failed |
| **Aider** | `AGENTS.md` block | project | finished, needs-input, failed |

Claude Code, Codex, OpenCode, Gemini CLI, Kimi Code and Qwen Code have real hook/plugin systems — the wiring is exact and deterministic. The editors (Cursor, Windsurf, Cline, Continue, Zed, Aider) have no lifecycle hooks, so they get a clearly marked rule file that tells the agent when to call the CLI. That's best-effort by construction: it's an instruction the model can skip. Existing configs are merged, never overwritten; `uninstall` removes only what was added.

`agentbell init` lists every agent it detects on your system (CLI on `PATH` or config dir) and offers to wire it up; `agentbell hooks install all` wires every supported agent at once.

### No spam while you're watching

"Finished" fires after *every* turn — a 20-second answer you watched happen isn't worth a push. So for Claude Code and Codex the installed hook carries `--min-duration 60`: turns shorter than a minute stay silent (logged as `hook.skipped_short` in `history`, so it's never a mystery). **Failures always send a notification**, and so does any turn whose duration is unknown.

Want a different threshold? Change the number in the hook command, or re-install:

```bash
agentbell hooks install claude          # default: 60 s
# then edit the "--min-duration 60" in ~/.claude/settings.json to taste (0 = every turn)
```

Custom agents and scripts just call the CLI:

```bash
long_job && agentbell notify "done" || agentbell notify "FAILED" --priority urgent
agentbell watch -- long_job          # …or let watch do both
```

---

## What it fixes

| The annoying part | What agentbell does |
|---|---|
| Alt-tabbing every two minutes: "is it done yet?" | Push on finish / fail — with exit code and duration (`✅ npm run build succeeded in 4m12s`) |
| The agent silently waits for a permission you never saw | `input_required` push the second it blocks |
| You must sit at the keyboard to say "yes, deploy" | `agentbell ask` → **Approve / Deny buttons on your phone**; the agent blocks until you answer |
| Notifier tools want an account, a hosted server, or a subscription | Free core. No account, no server, no subscription. Telegram extras: **€4.99 once** |
| Wiring notifications into every agent, by hand, per repo | `agentbell init` detects your agents and installs their hooks — globally, so every repo is covered |
| It breaks at 3 a.m. and you have no clue why | `agentbell doctor` names the problem **and prints the command that fixes it** |
| "Notifications" that spam you all night | Quiet hours: drop *or* hold-and-bundle. Urgent always gets through |
| A wifi blip silently eats the notification | Retry, then a persistent queue that gets replayed |
| Uninstalling leaves junk in five config files | `agentbell uninstall` — dry run first, removes only its own markers |

---

## Free vs. premium

**Free and open source — the complete core:**
ntfy push · native OS notifications · agent hooks for 12 agents · the full approval flow (buttons + free text) · `watch` · webhook server · MCP server · priorities · quiet hours (with defer) · history · retry + offline queue · `doctor` · clean uninstall.

That list is the whole product for most people. Nothing above nags, expires, or asks for a key.

> **Premium — €4.99 one-time, lifetime. No subscription, no account, no phone-home.**
> - **Approve/Deny buttons that are authenticated to you.** A Telegram chat is tied to your account — unlike an ntfy topic, which anyone who learns its name could answer. (Via the `agentbell bot` daemon.)
> - Plus **parallel delivery**: ntfy *and* Telegram at once, first answer wins.
>
> [**Buy a lifetime key — €4.99**](https://buy.polar.sh/polar_cl_MAAwIuriOXF45xu9Fm0dbgr9iTIJFqsKM) — you get an `AB1-…` key by email within 24 hours (usually much faster), then: `agentbell license activate AB1-...`
> The key never expires and isn't tied to a machine — use it on every computer you work on. It's verified offline; nothing about you is ever sent anywhere. VAT is included and the payment provider sends your invoice. Not what you expected? Reply to the purchase email within 14 days and you get a refund, no questions asked.
>
> **The honest part:** the paywall is one `if` in a file you can read, and the project is MIT — a fork that deletes it is legal. €4.99 is priced as "less than the five minutes that would take." If it isn't worth that to you, the free core is complete and I'd rather you use it. What the key *is*: an Ed25519 signature over your customer id, checked against a public key that sits in plain sight in `agentbell.py`. It can't be forged, it's verified entirely on your machine (no network, ever), and there's no secret hidden in the install for anyone to dig out. See `DECISIONS.md` §2b for the full scheme and what it deliberately doesn't protect against.

---

## Telegram approvals (premium)

Real Approve/Deny buttons in Telegram, powered by a small opt-in long-polling daemon — no public endpoint, still zero dependencies.

```bash
agentbell license activate <key>
agentbell init                 # enter bot token + chat id
agentbell bot install-service  # answer daemon in the background
agentbell ask "Deploy to production?"
```

- Buttons are attached only while the daemon's heartbeat is fresh — never dead buttons.
- Free-text replies in the bot chat count as answers; only your configured chat can answer.
- With ntfy **and** Telegram configured, both get the question — first answer wins.
- `agentbell bot status` shows daemon state, lock, last error, open questions, queue counts.

---

## Quick reference

```bash
# notify
agentbell notify "Build finished" --priority high --tags build

# run something and get told how it went (exit code is passed through)
agentbell watch -- npm run build

# ask and wait for the answer   (exit 0=approved/answered, 1=denied, 2=timeout, 3=error)
agentbell ask "Deploy to production?" --timeout 600
agentbell ask "Which environment?" --no-buttons        # free-text answer
agentbell ask "Deploy?" --json

# wire up agents (global — applies in every repo)
agentbell hooks install claude codex opencode gemini cursor
agentbell hooks install all             # every supported agent, detected or not
agentbell hooks status
agentbell hooks uninstall all

# expose as an MCP tool (desktop apps + editors)
agentbell mcp add                     # all clients it can detect
agentbell mcp add claude-desktop      # just one
agentbell mcp add --print             # print the snippet, change nothing

# health check with copy-paste fixes
agentbell doctor
agentbell doctor --send               # …and send a real test notification

# premium: Telegram answer daemon
agentbell bot install-service         # run in the background (systemd/launchd) — recommended
agentbell bot                         # or in the foreground, for debugging
agentbell bot status

# reliability + inspection
agentbell queue list                  # what is waiting and why
agentbell queue flush                 # deliver it now
agentbell history --limit 20
agentbell config show                 # secrets redacted
agentbell config set ntfy.topic <new> # change one setting, no wizard

# webhook for CI / a VPS without the CLI
agentbell server                      # POST /notify, POST /ask, GET /healthz

# complete removal
agentbell uninstall                   # dry run, deletes nothing
agentbell uninstall --yes
```

---

## Desktop apps and editors (MCP)

`agentbell mcp add` registers a stdio MCP server exposing two tools:
`notify(message, title, priority, tags)` and `ask_approval(message, timeout_seconds)`.

| Client | Where it's registered | Works |
|---|---|---|
| **ChatGPT Desktop** | `~/.codex/config.toml` (shared with Codex CLI) | yes — local stdio |
| **Claude Desktop** | `claude_desktop_config.json` | yes — local stdio |
| Claude Code | `claude mcp add --scope user` | yes |
| Codex CLI | `~/.codex/config.toml` | yes |
| Cursor | `~/.cursor/mcp.json` (global) | yes |
| VS Code / Copilot | user `mcp.json` | yes |
| Gemini CLI, OpenCode | their settings files | yes |
| **Qwen Code** | `~/.qwen/settings.json` (global; `--project` → `.qwen/settings.json`) | yes |
| **Kimi Code** | `~/.kimi-code/mcp.json` (global; `--project` → `.kimi-code/mcp.json`) | yes |
| ChatGPT **web** | — | no: web accepts remote MCP servers only |

Restart the client afterwards. `agentbell mcp add --print` gives you the raw snippet for anything not in that list (Windsurf, Zed, LM Studio, …); `examples/README.md` has the same snippets to copy. Kimi Code exposes the tools as `mcp__agentbell__notify` and `mcp__agentbell__ask_approval`.

---

## Events, priorities, quiet hours

| Event | Priority | Emoji |
|---|---|---|
| `run_completed` | normal (3) | ✅ |
| `run_failed` | urgent (5) | 🔴 |
| `input_required` | high (4) | 🔵 |
| `permission_required` | high (4) | 🔐 |
| `started` | low (2) | ▶️ |

Quiet hours (e.g. `22:00-07:30`) hold back everything below `normal`:

| `quiet_hours_mode` | Behavior |
|---|---|
| `suppress` (default) | dropped, logged to history |
| `defer` | delivered after the window; more than 3 are bundled into one summary |

`--force` bypasses quiet hours, `--defer` defers a single message. **Approval questions are never suppressed or deferred.**

---

## Reliability

Transient failures (network down, timeout, 5xx) are retried with backoff, then the notification goes into a persistent queue instead of being lost:

- replayed after your next successful send, by `queue flush`, or by the bot daemon
- `queue list` shows exactly what's waiting, how old it is and why
- bounded: 100 items / 24 h, oldest dropped first — everything logged to history
- `notify` exits 0 when queued (not lost, just delayed); `ask` is never queued

This is best-effort delivery, not a guarantee — see [trust model](#trust-model) for what ntfy.sh's free tier does and doesn't promise.

---

## Configuration

`~/.config/agentbell/config.json` (mode 600 — it holds your license key, bot token and ntfy password):

| Key | Meaning |
|---|---|
| `ntfy.server` / `ntfy.topic` / `ntfy.auth` | channel + optional `user:pass` (or an access token) for self-hosted ntfy |
| `ntfy.action_auth` | optional scoped credential for approval buttons (see the [trust model](#trust-model)) |
| `telegram.bot_token` / `telegram.chat_id` | Telegram channel (premium) |
| `license` | premium key |
| `channels` | `["ntfy"]`, `["ntfy","os"]`, `["ntfy","telegram"]` |
| `quiet_hours` / `quiet_hours_min_priority` / `quiet_hours_mode` | see above |
| `approval_timeout` | default seconds for `ask` (300) |
| `webhook.listen` / `webhook.port` / `webhook.token` | `agentbell server` (token = bearer auth; required for any non-loopback `listen`) |

State (history, queue, deferred, bot state): `~/.local/state/agentbell/`.
Env overrides: `AGENTBELL_CONFIG_DIR`, `AGENTBELL_CONFIG`, `AGENTBELL_STATE_DIR`, `AGENTBELL_LICENSE`.

---

## Trust model

What this tool actually protects, and what it doesn't. Read this before you gate a production deploy on it.

**Your topic name is the only credential in the free setup.** Anyone who learns it can read every notification, publish fake ones, and approve or deny any open `ask`. Treat it like a password — that's ntfy's own wording in their terms. `init` generates a long random topic for exactly this reason, so don't shorten it, and **don't paste `doctor` or `config show` output into public issues.**

**Sensitive approvals get a runtime warning when ntfy authentication is absent.** AgentBell recognizes only a narrow set of high-impact requests (for example, production deployments, production database deletion, credential rotation, money transfers, and firewall changes). This is a reminder, not a security decision: it cannot understand every action's real impact. Use self-hosted ntfy with auth before relying on phone approval for a sensitive action.

**ntfy.sh is a third-party relay.** Your message text passes through servers you don't control and can be read there — including the working directory that hook notifications carry. If that matters for your work, self-host ntfy and point `ntfy.server` at it.

**Action buttons carry their credential inside the message.** On a protected server, every subscriber to the topic can see a button's `Authorization` header. Set `ntfy.action_auth` to a token that may only publish to the `-responses` topic instead of reusing your account password.

**Free-text replies go to the newest open question.** Button answers are matched by request ID and are unambiguous. With two asks in flight, answer with the buttons.

**The webhook server trusts its loopback.** On loopback it accepts requests from any local process. Set `webhook.token` even locally; browser-originated requests are rejected. A non-loopback `listen` without a token is refused outright.

**ntfy.sh free-tier limits that matter:** 250 messages/day · at most 3 action buttons per notification · 4 KB message size · **no delivery guarantee** — it's best-effort. Self-hosting or ntfy's paid tiers are the reliability path.

**Not a security boundary:** agentbell doesn't authenticate who publishes to your topic and doesn't encrypt message bodies end-to-end.

---

## Troubleshooting

**Start here: `agentbell doctor`** — it checks the installation, PATH, config, server reachability, quiet hours, license, hooks, MCP, queue and state dir, and prints a fix command for everything that's wrong.

| Symptom | Usually |
|---|---|
| Nothing arrives | you haven't subscribed to the topic in the app, or quiet hours are active → `agentbell doctor` |
| Nothing arrives, no error | `agentbell history --limit 10` shows `suppressed` / `queued` / `deferred` |
| Telegram buttons missing | the answer daemon isn't running → `agentbell bot install-service` |
| Topic too guessable | `agentbell config set ntfy.topic <long-random>`, then re-subscribe in the app |
| "webhook is active" | another process holds a Telegram webhook: `curl -s "https://api.telegram.org/bot<TOKEN>/deleteWebhook"`, then restart the bot |
| Hooks don't fire | `agentbell hooks status`; for Codex check `/hooks` inside Codex |
| Start over | macOS/Linux: `agentbell uninstall` → `--yes` → `./install.sh && agentbell init`; Windows: `py -m pip install --user .` → `agentbell init` |

---

## FAQ

**Is my data private?**
Nothing goes to me — there's no account, no telemetry, and no phone-home, and the license check is offline. But your notification text does pass through whichever ntfy server you use, and on the default `ntfy.sh` that's a third-party relay. Self-host ntfy if your message text is sensitive; see the [trust model](#trust-model).

**Does it work self-hosted?**
Yes, and that's the recommended setup for anything sensitive. Point `ntfy.server` at your own instance, put credentials in `ntfy.auth`, and give the approval buttons a scoped publish-only token via `ntfy.action_auth`. Everything else — hooks, approvals, quiet hours, queue — behaves identically.

**Can I use it without Telegram?**
Yes. Telegram is the premium add-on; the free core is complete without it, approval flow included. ntfy alone gives you push, Approve/Deny buttons and free-text replies.

**Why is Telegram paid if it's MIT?**
Because the code being open and the work being worth paying for aren't in conflict. The gate is one `if` you can read, and deleting it in a fork is legal — €4.99 once is priced below the effort of doing that. It funds the maintenance; it isn't a moat, and it isn't pretending to be one.

**Found a bug, or stuck on something?**
[Open an issue](https://github.com/MoodTechBasti/agentbell/issues) — the bug template asks for `agentbell doctor` output (redact your topic names first).

---

## Removal

```bash
agentbell uninstall        # dry run: lists everything, deletes nothing
agentbell uninstall --yes  # binary, config, state, hooks, MCP entries
```

Only its own markers are removed — your other hooks and MCP servers stay. Not removed automatically: the ntfy subscription on your phone, a Telegram bot at BotFather, `AGENTBELL_*` env vars in your shell rc.

---

## Development

```bash
python3 -m unittest discover -s tests -v   # macOS/Linux, no external deps
py -m unittest discover -s tests -v        # Windows
```

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to propose a change, and what gets merged
- [`SECURITY.md`](SECURITY.md) — how to report a vulnerability
- [`DECISIONS.md`](DECISIONS.md) — why the tool is built the way it is, including what was deliberately left out
- [`CHANGELOG.md`](CHANGELOG.md) — what changed per version
- [`FIELD_TEST.md`](FIELD_TEST.md) — the 2-week field-test checklist this build is running against
- [`examples/`](examples/) — reference copies of every config it writes, plus script patterns

---

## License

MIT — see [`LICENSE`](LICENSE).
