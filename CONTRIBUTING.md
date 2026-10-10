# Contributing to agentbell

agentbell is a solo-maintained, single-file project. Small, focused PRs
are welcome — bug fixes, new agent integrations, and doc improvements most
of all.

## Running the tests

```
python3 -m unittest discover -s tests -v   # macOS/Linux
py -m unittest discover -s tests -v        # Windows
```

Please run this before opening a PR. The suite runs in a temporary home
directory and state/config directories, so it does not read or change your
real agentbell config or your agents' configs.

## Hard constraint: stdlib-only

No new dependencies, ever. That's the whole point of the project — a
single file you can drop anywhere and run. PRs that add a runtime
dependency will be closed.

## Where changes go

Code changes belong in `agentbell.py`, with tests under `tests/`. This
project deliberately stays a single file, not a package — please don't
propose a package split, a new module, or a new top-level file unless it's
been discussed in an issue first.

Design rationale lives in [DECISIONS.md](DECISIONS.md) — read it before
proposing architecture changes, it likely already covers the tradeoff.

## Rules that hold regardless of the change

These are the durable ones; each is a one-line summary of a `DECISIONS.md`
section that explains the cost of getting it wrong.

- **Ownership.** An entry in someone's config is agentbell's only in the
  exact shape and place agentbell writes it; anything else is theirs, even
  when it calls agentbell. Bytes agentbell does not own stay as they were —
  line endings, comments, surrounding tables, existing mode (§33, §34).
  Status is observation: `hooks status`, `doctor` and `verify` only report;
  `hooks install`, `hooks uninstall` and `uninstall` rewrite.
- **Sending is budgeted.** A hook is silent on stdout, always exits 0, and
  shares one ~6 s wall-clock budget across retries, queue drain and deferred
  flush; what does not fit is queued with a history record (§32). Quiet hours
  default to suppress; optional defer stores notifications for later.
  Priority threshold and `--force` can bypass quiet hours; `ask` is never
  suppressed or deferred (§5). Duplicate pushes are suppressed and recorded
  within the dedupe window (§17a) — that is the core, text-keyed guard, not
  "one turn, one push".
- **Approvals.** Only an explicit yes approves. Explicit denial/postponement
  denies; other text is an answer, not approval. ID-bearing replies must
  match their request; stale IDs are ignored. Unbound typed replies are
  accepted only when exactly one question can still be on the phone (§20,
  §22, §30).
- **Integrations are version-specific.** Host event names and payload shapes
  move without notice — OpenCode 1.x and 2.x differ in the export contract,
  the payload key and the turn-end event for the same turn (§38). Establish
  a schema from a versioned primary source (an installed binary, versioned
  typings, the vendor's docs), cover each supported generation with its own
  offline fixture, and re-verify rather than trusting an earlier claim.
- **No secrets, no `shell=True`.** Credentials never go into commits.
  Credentials and private data stay out of output — `config show` redacts the
  fields it treats as sensitive — but that is field-by-field redaction, not a
  blanket guarantee.
  Do not infer Windows ACL safety from POSIX mode bits (§18d).
- **`integrate` / `verify` are observation, not certification.** `integrate`
  publishes the contract an agent can call; `verify` reports what arrived.
  Neither certifies an agent end to end (§16).
- **Dirty trees.** Uncommitted edits here are usually deliberate. Read the
  diff before editing around it, build on it, and never reset, stash or
  discard work you did not make. If a change in the tree makes the task
  impossible, say so instead of overwriting it.

`docs/agent-work/<task>/` holds transient plans, checkpoints and reports for
larger changes; durable rules belong in this file or in `DECISIONS.md`.

A PR that changes behavior should also update:

- [CHANGELOG.md](CHANGELOG.md) — an entry under `## Unreleased` describing
  the user-visible change.
- [DECISIONS.md](DECISIONS.md) — when the change makes or reverses a design
  decision.
- [README.md](README.md) — when a command, option, config key or documented
  behavior changes.

## Pull requests and CI

`main` only changes through pull requests. The required check is
**CI Gate**: it passes only when the full test matrix passed (Linux and
macOS on Python 3.9–3.13, Windows on Python 3.11 and 3.13). A PR can merge
when CI Gate is green and the branch is up to date with `main`.

## Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).
Report unacceptable behavior to basti@moodtechsolutions.com. Security
issues go through private vulnerability reporting instead; see
[SECURITY.md](SECURITY.md).
