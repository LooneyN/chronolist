# Chronolist – notes for working on this repo

## Rules
- Chronolist is a general tool for any YouTube channel. Never add personal data, channel-specific defaults, names, credentials or tokens to code, examples, docs or commits.
- Personal files (`credentials.json`, `token.json`, `settings.json`, `quota.json`, `data/`, `browser_profile/`) stay out of the repo (see `.gitignore`).

## Commits
- Commit messages must not contain links to Claude sessions (no `Claude-Session:` line). A `Co-Authored-By` line is fine.

## Branches and versions
- Day-to-day work happens on `dev`. Commit every change there; this creates no release and no update notice for users.
- On `dev`, `VERSION` in `chronolist.py` carries a `-dev` suffix (e.g. `2.8.0-dev`); only `BUILD` is incremented per change.
- Collect user-facing changes under `## [Unreleased]` in `CHANGELOG.md`.
- `main` only receives finished releases.

## Releasing (only when the maintainer asks for it)
1. Merge `dev` into `main`.
2. Set `VERSION` to the final number (no `-dev`), move the `[Unreleased]` entries to a new version section in `CHANGELOG.md` and update the compare links.
3. Commit, then push the tag `vX.Y.Z`. The `Release` workflow checks that the tag matches `VERSION`, builds `Chronolist.exe` and publishes the GitHub release with the changelog notes.
4. Afterwards bump `dev` to the next `-dev` version.

Version numbers: bug fixes only -> patch (2.7.1); new features -> minor (2.8.0).
