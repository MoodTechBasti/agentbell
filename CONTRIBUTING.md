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
