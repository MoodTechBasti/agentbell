# Releasing agentbell

This is a maintainer checklist. User setup belongs in `README.md`.

## Release flow

Releases publish to PyPI through `.github/workflows/release.yml` and GitHub Trusted Publishing. Pushing a matching `v*` tag on the current `main` tip starts the workflow; after its build succeeds, PyPI publication runs automatically. There is no manual approval gate. **Do not push a release tag until publication is intended.**

The workflow checks that:

- the version in `agentbell.py`, `pyproject.toml` and the tag matches;
- the tag points at the current `main` tip;
- the full test suite passes;
- wheel and source distribution build, pass `twine check`, and contain only expected files.

## Before the release PR

1. Start from an up-to-date `main` and create `release/v<version>`.
2. Set the same version in `agentbell.py` and `pyproject.toml`.
3. Turn `## Unreleased` into a dated heading in `CHANGELOG.md`. Keep release notes user-facing and remove working notes.
4. Update current-version mentions in `README.md` and the `FIELD_TEST.md` title. Do not rewrite dated test evidence.
5. Ensure `DECISIONS.md` records any lasting design decisions. Record only tests actually run in `FIELD_TEST.md`.
6. Confirm the `v<version>` tag does not already exist.
7. Run the test suite and `python -m py_compile agentbell.py`; review `git diff --check`.

Open a PR against `main`. Merge only after **CI Gate** and **Build and verify distributions** pass. The release workflow's PR build runs when package files or tests change; a release PR should change `agentbell.py` and `pyproject.toml`.

## Tag and publish

After the PR is merged:

1. Update local `main` with `git pull --ff-only` and confirm the merged release commit is its tip.
2. Confirm the tag is still unused and version values still match.
3. Create and push an annotated tag:

   ```bash
   git tag -a v<version> -m "agentbell v<version>"
   git push origin v<version>
   ```

Do not merge another change to `main` until the tag workflow finishes: the release tag must still point at the current tip. Monitor the run with `gh run list --workflow release.yml` and `gh run watch <run-id>`.

After successful PyPI publication, create the GitHub Release from the matching `CHANGELOG.md` section. Check the rendered notes.

## Verify publication

Install the published version from public PyPI in a clean environment, not from the checkout or a local wheel. Check `agentbell --version`, run `doctor` in a temporary home, and test both `pip` and `pipx` where available. Record the date, version and install paths actually checked in `FIELD_TEST.md`; name any path not checked.

## Stop conditions

- If any version, tag, CI, build, archive or publish check fails, stop and diagnose; do not move or recreate a tag to bypass a failure.
- If PyPI upload may have partially succeeded, inspect PyPI before retrying. Published files are immutable and version numbers cannot be reused.
- Do not add a long-lived PyPI token as a workaround; verify Trusted Publishing and the `pypi` GitHub environment instead.
- Keep user credentials and local configuration out of release logs and examples.
