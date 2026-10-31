# Changelog

All notable changes to Chronolist are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Updates inside the app (exe only): click the green update tag in the header, confirm, and Chronolist downloads the new release, replaces itself and restarts. Settings, sign-in and progress are kept. Not possible during a run.

### Fixed
- Restarting the exe (language change) now starts a fully independent new instance.

## [2.8.0] - 2026-10-31

### Added
- Playlist settings in the app (no more editing `settings.json`): title and optional description with placeholders `{channel}`, `{handle}`, `{year}` and `{count}`, insert buttons and a live preview.
- Playlist visibility: public, unlisted or private.
- Optional thumbnail per year (JPG/PNG, max 2 MB). Can be picked before the playlist exists and is uploaded via the API as soon as the playlist exists (experimental).
- Program settings, separate from the playlist settings: language (English / German) and update check on/off. A language change switches interface, tooltips, help and log completely and restarts Chronolist. The default follows the system language.

### Changed
- All action buttons are yellow and, like the panels, have a single cut corner at the bottom right.
- The title in the header is plain red without the glitch effect.
- Default playlist title and description follow the selected language. Existing installations keep their previous templates.
- Development builds are marked with a `-dev` suffix (e.g. `2.8.0-dev`). The update check treats them as newer than every earlier release but older than the final release with the same number.

## [2.7.0] - 2026-10-31

First public release.

### Added
- Update check: on launch, Chronolist asks GitHub once for the latest release and shows a green `UPDATE` tag in the header when a newer version exists. Can be turned off with `"check_updates": false` in `settings.json`.
- Configurable playlist title and description via `playlist_template` and `description_template` in `settings.json` (placeholders `{channel}` and `{year}`).
- "Updates" and "About" sections in the in-app help, including license, repository link and a note that the tool was written entirely with Claude (Anthropic).
- Windows icon (`assets/chronolist.ico`) and automated Windows builds via GitHub Actions.

### Changed
- Neutral UI texts and examples; no personal names or channel-specific defaults.

### Removed
- Migration code for the private predecessor (old file layout and old autostart entry).

## Earlier versions

Versions before 2.7.0 were developed privately and are not published.

[Unreleased]: https://github.com/LooneyN/chronolist/compare/v2.8.0...HEAD
[2.8.0]: https://github.com/LooneyN/chronolist/compare/v2.7.0...v2.8.0
[2.7.0]: https://github.com/LooneyN/chronolist/releases/tag/v2.7.0
