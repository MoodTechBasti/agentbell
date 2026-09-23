# Releasing agentbell to PyPI

This runbook is for maintainers. End users should follow `README.md`.

## Release model

agentbell publishes from `.github/workflows/release.yml` through PyPI Trusted Publishing.

- No PyPI API token is stored in GitHub.
- `main` only changes through pull requests. The required status check is **CI Gate** (`.github/workflows/ci.yml`), which passes only when the full test matrix (Linux, macOS, Windows) passed. The branch must be up to date with `main` before it can merge.
- The build job has read-only repository permissions.
- The publish job receives `id-token: write` only after the build job succeeds.
- The publish job uses the GitHub environment `pypi`, which only accepts deployments from `v*` tags. No manual approval step is configured, so a pushed tag that passes the build job is published.
- Publication happens only for a `v*` tag whose version matches both `pyproject.toml` and `agentbell.py`.
- The tagged commit must equal the current `main` tip at the time the workflow runs.
- Pull requests run the build job (**Build and verify distributions**) but never the publish job, and only when they touch a file in the workflow's `paths` list: `.github/workflows/release.yml`, `MANIFEST.in`, `pyproject.toml`, `agentbell.py`, `README.md`, `LICENSE` or `tests/**`. A PR that changes none of these (for example `CHANGELOG.md` alone) does not run the build job. A release PR always runs it, because it changes `agentbell.py` and `pyproject.toml`.

In the commands below, replace `<version>` with the new version, for example `1.7.0`.

## 1. Prepare the release branch

Start from an up-to-date `main`:

```bash
git switch main
git pull --ff-only
git switch -c release/v<version>
```

1. **Bump the version in both places.** `VERSION = "<version>"` in `agentbell.py` and `version = "<version>"` in `pyproject.toml`. The build job fails if they differ.
2. **Turn `## Unreleased` in `CHANGELOG.md` into a dated heading**, in the existing style: `## <version> — <YYYY-MM-DD> — <short title>`. Read the section once as release notes: it becomes the GitHub Release text, so remove internal notes and working remarks.
3. **Update version mentions.** `grep -n "<old version>" README.md FIELD_TEST.md` finds them. Update the README status line and any install text that names the current version, and the `FIELD_TEST.md` title (`(v<version>)`). Leave dated evidence entries in `FIELD_TEST.md` alone: they record the version that was tested.
4. **Run the tests locally:**

   ```bash
   python3 -m unittest discover -s tests -v   # macOS/Linux
   py -m unittest discover -s tests -v        # Windows
   ```

5. `DECISIONS.md` has an entry for every design change in the release.
6. No `v<version>` tag exists yet (`git ls-remote --tags origin v<version>` prints nothing).

## 2. Pull request and merge

Push the branch and open a pull request against `main`. Before merging:

- **CI Gate** is green.
- **Build and verify distributions** (from `release.yml`) is green. It checks version consistency, runs the test suite, builds wheel and sdist, runs `twine check` and checks the archive contents.

Merge the PR. Do not tag the release branch: the workflow rejects a tag that does not point at the current `main` tip.

## 3. Tag and publish

Create an annotated tag on the merged release commit and push it:

```bash
git switch main
git pull --ff-only
git log -1 --oneline            # the merged release commit must be the tip
git tag -a v<version> -m "agentbell v<version>"
git push origin v<version>
```

Do not merge anything else into `main` until the workflow has finished. A newer `main` tip fails the "tag points at current main" check.

The tag starts `.github/workflows/release.yml`. Follow it with `gh run list --workflow release.yml` and `gh run watch <run-id>`. The build job must pass these checks before the publish job may run:

- runtime/package version consistency
- tag/version consistency
- tag points at current `main`
- full unit-test suite
- wheel and sdist build
- `twine check`
- archive-content checks (`agentbell.py` is the only Python file; the console entry point is present; `tests/`, `internal/` and `.license-secret` are absent)

The publish job downloads only the files that build job produced and uploads them to PyPI.

## 4. GitHub Release

Create the GitHub Release from the tag, with the version's `CHANGELOG.md` section as the notes:

```bash
awk -v v="<version>" 'index($0, "## " v " ") == 1 {p=1; next} p && /^## / {exit} p' \
  CHANGELOG.md > /tmp/agentbell-release-notes.md
gh release create v<version> --verify-tag \
  --title "agentbell v<version>" \
  --notes-file /tmp/agentbell-release-notes.md
```

Check the rendered notes on the release page.

## 5. Verify the published package

Install from public PyPI in a clean environment. Do not use the checkout, a local wheel or another package index. Run `doctor` with a temporary `HOME` and without the config/state overrides, so the check does not read your real agentbell config (whose `doctor` output contains your topic):

```bash
tmp=$(mktemp -d)
fresh() { env -u XDG_CONFIG_HOME -u XDG_STATE_HOME -u AGENTBELL_CONFIG \
  -u AGENTBELL_CONFIG_DIR -u AGENTBELL_STATE_DIR HOME="$tmp" "$@"; }
python3 -m venv "$tmp/venv"
"$tmp/venv/bin/pip" install --no-cache-dir "agentbell==<version>"
"$tmp/venv/bin/agentbell" --version
fresh "$tmp/venv/bin/agentbell" doctor
```

Then the same with pipx, in the same shell and isolated from your own pipx installs:

```bash
PIPX_HOME="$tmp/pipx" PIPX_BIN_DIR="$tmp/bin" pipx install "agentbell==<version>"
"$tmp/bin/agentbell" --version
fresh "$tmp/bin/agentbell" doctor
```

Expected:

- Both installs resolve `agentbell` from public PyPI.
- `agentbell --version` reports the released version.
- `agentbell doctor` runs normally. In the fresh home it reports that no config exists yet and points to `agentbell init`; that is correct.
- The PyPI project page (https://pypi.org/p/agentbell) shows the new version, its files and the repository links.

Record the result in `FIELD_TEST.md`: the date, which install paths (`pip`, `pipx`) you checked and the version they reported. Name any path you did not check.

## Failure rules

- **Build job fails:** fix on a branch; do not recreate or move the release tag to bypass the failure.
- **Tag check fails because `main` moved:** delete the tag (`git push origin :refs/tags/v<version>` and `git tag -d v<version>`) only if the publish job did not run. Then tag the new tip, provided it is the commit you want to release.
- **Trusted Publishing/OIDC fails:** verify the PyPI publisher identity and the GitHub environment first (see the appendix). Do not add a long-lived PyPI token as a shortcut.
- **Wrong version/tag:** fix the version on a branch, merge, then create the correct unused tag.
- **Upload partially succeeds:** inspect PyPI before retrying. PyPI release files are immutable, and a version number cannot be uploaded twice; never assume a failed workflow means nothing was uploaded.

## Appendix: one-time PyPI setup (historical, already done)

This setup was completed before the first PyPI release, v1.6.3 (published 2026-09-03). It is kept here for reference and for repairing the configuration if it is ever lost.

The PyPI project was created through a **pending Trusted Publisher**, without a bootstrap upload using a long-lived token. The publisher uses these exact values:

| Field | Value |
|---|---|
| PyPI project name | `agentbell` |
| GitHub owner | `MoodTechBasti` |
| Repository | `agentbell` |
| Workflow filename | `release.yml` |
| Environment | `pypi` |

The GitHub environment `pypi` holds no secrets. Its deployment policy allows only `v*` tags.

A pending publisher did not reserve the project name. If the name had been claimed by someone else before the first upload, the rule was to stop and reassess the distribution name before changing metadata or publishing elsewhere.
