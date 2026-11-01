# Chronolist – notes for working on this repo

## Rules
- Chronolist is a general tool for any YouTube channel. Never add personal data, channel-specific defaults, names, credentials or tokens to code, examples, docs or commits.
- Personal files (`credentials.json`, `token.json`, `settings.json`, `quota.json`, `data/`, `browser_profile/`) stay out of the repo (see `.gitignore`).

## Screenshots
- `assets/screenshots/` holds the README screenshots (main window, list editor, suggestions). They are made with invented demo data (fake channel name "Example Channel", invented titles), never with a real channel.
- Only renew them when the UI changes noticeably, not on every change.

## Commits
- Commit messages must not contain links to Claude sessions (no `Claude-Session:` line). A `Co-Authored-By` line is fine.

## Branches and versions
- Day-to-day work happens on `dev`. Commit every change there; this creates no release and no update notice for users.
- On `dev`, `VERSION` in `chronolist.py` carries a `-dev` suffix (e.g. `2.8.0-dev`); only `BUILD` is incremented per change.
- Collect user-facing changes under `## [Unreleased]` in `CHANGELOG.md`.
- Major releases (X.0.0) get a short `### Highlights` section at the top of their changelog entry: 4-6 punchy bullets in plain language, then the detailed lists. The in-app update note and the GitHub release text are taken from this entry, so keep the bullets short (no emoji, no bold, since the in-app note shows the text as is).
- `main` only receives finished releases.

## Releasing (only when the maintainer asks for it)
1. Merge `dev` into `main`.
2. Set `VERSION` to the final number (no `-dev`), move the `[Unreleased]` entries to a new version section in `CHANGELOG.md` and update the compare links.
3. Commit and push `main`. Then either push the tag `vX.Y.Z` or (if tag pushes are blocked) have the maintainer start Actions -> Release -> Run workflow on `main`, which creates the tag itself. The `Release` workflow checks that the tag matches `VERSION`, builds `Chronolist.exe` and publishes the GitHub release with the changelog notes.
4. Afterwards bump `dev` to the next `-dev` version.

Version numbers: bug fixes only -> patch (2.7.1); new features -> minor (2.8.0). Bigger reworks or a clearly new scope -> major (3.0.0).

## Suggesting releases and versions
- The maintainer finds it hard to judge when a release or a major version is due. Proactively suggest it when it fits:
  - a new release, once `[Unreleased]` holds a meaningful, tested set of changes (or an important fix);
  - a major version (e.g. 3.0.0), when the changes add up to a clearly bigger step for users.
- Only suggest; release only when the maintainer says so.
