# Releasing AgentBell to PyPI

This runbook is for maintainers. End users should follow `README.md`.

## Release model

AgentBell publishes from `.github/workflows/release.yml` through PyPI Trusted Publishing.

- No PyPI API token is stored in GitHub.
- The build job has read-only repository permissions.
- The publish job receives `id-token: write` only after the build gate succeeds.
- The publish job uses the protected GitHub environment `pypi`.
- Publication happens only for a `v*` tag whose version matches both `pyproject.toml` and `agentbell.py`.
- The tagged commit must equal the current `main` tip.
- Pull requests run the build/distribution gate but never the publish job.

## One-time setup before the first PyPI release

The `agentbell` project does not need a bootstrap upload with a long-lived token. Configure a **pending Trusted Publisher** in the PyPI account that will own the project.

Use these exact values:

| Field | Value |
|---|---|
| PyPI project name | `agentbell` |
| GitHub owner | `MoodTechBasti` |
| Repository | `agentbell` |
| Workflow filename | `release.yml` |
| Environment | `pypi` |

A pending publisher does not reserve the project name. Do not leave a long gap between configuring it and making the first release.

In GitHub, create an environment named `pypi`. Keep it secret-free. Add environment protection appropriate for the repository; at minimum, restrict deployment to the intended release tags and require maintainer approval when available.

## Pre-release gate

Before tagging:

1. `main` contains the intended release commit.
2. The release PR CI is green, including **Build and verify distributions**.
3. `pyproject.toml` and `agentbell.py` report the same version.
4. `CHANGELOG.md` contains the release notes.
5. `FIELD_TEST.md` distinguishes locally verified packaging from public-PyPI verification.
6. The PyPI pending/normal Trusted Publisher matches the exact owner, repository, workflow filename and environment above.
7. No `v<version>` tag already exists.

Do not tag a branch commit before it is merged. The workflow intentionally rejects a tag that does not point at the current `main` tip.

## Publish

After the release commit is on `main`, create and push the version tag, for example:

```bash
git switch main
git pull --ff-only
git tag -a v1.6.3 -m "AgentBell v1.6.3"
git push origin v1.6.3
```

The tag starts `.github/workflows/release.yml`.

The workflow must complete these gates before the OIDC publish job is allowed to run:

- runtime/package version consistency
- tag/version consistency
- tag points at current `main`
- full unit-test suite
- wheel and sdist build
- `twine check`
- archive-content checks

Only the artifacts produced by that build job are downloaded by the publish job and sent to PyPI.

## Post-publication verification

Use a fresh environment. Do not use the checkout, a local wheel or an alternate package index:

```bash
pipx install agentbell
agentbell --version
agentbell doctor
```

Expected first-release evidence:

- `pipx install agentbell` resolves from public PyPI.
- `agentbell --version` reports the released version.
- `agentbell doctor` runs normally; an unconfigured fresh home may correctly ask for `agentbell init`.
- The PyPI project page shows the expected repository metadata and release files.

Record the result in `FIELD_TEST.md`, then remove the README wording that says PyPI publishing is still in preparation.

## Failure rules

- **Build gate fails:** fix on a branch; do not recreate or move the release tag to bypass the failure.
- **Trusted Publishing/OIDC fails:** verify the PyPI publisher identity and GitHub environment first. Do not add a long-lived PyPI token as a shortcut.
- **Wrong version/tag:** fix the version on a branch, merge, then create the correct unused tag.
- **Package name was claimed before first publication:** stop. A pending publisher does not reserve the name; reassess the distribution name before changing metadata or publishing elsewhere.
- **Upload partially succeeds:** inspect PyPI before retrying. PyPI release files are immutable; never assume a failed workflow means nothing was uploaded.
