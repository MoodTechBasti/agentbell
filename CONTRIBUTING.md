# Contributing to agentbell

agentbell is a solo-maintained, single-file project. Small, focused PRs
are welcome — bug fixes, new agent integrations, and doc improvements most
of all.

## Running the tests

```
python3 -m unittest discover -s tests -v
```

Please run this before opening a PR.

## Hard constraint: stdlib-only

No new dependencies, ever. That's the whole point of the project — a
single file you can drop anywhere and run. PRs that add a runtime
dependency will be closed.

## Where changes go

Changes belong in `agentbell.py` and `tests/test_agentbell.py`. This
project deliberately stays a single file, not a package — please don't
propose a package split, a new module, or a new top-level file unless it's
been discussed in an issue first.

Design rationale lives in [DECISIONS.md](DECISIONS.md) — read it before
proposing architecture changes, it likely already covers the tradeoff.

## Conduct

Be respectful. Bad-faith issues or PRs get closed.
