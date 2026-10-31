# Changelog

All notable changes to Chronolist are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- The current year (in January also the previous one) is kept up to date: on every run new uploads are appended. It stays selected when done. New channels now select the current year too.
- Years with more than 5,000 videos are split automatically into "Part 1", "Part 2", … (YouTube's playlist limit). Checking, repairing and thumbnails work per part.
- Year cards show what is happening right now with progress (reading the channel page, adding, sorting, checking the playlist), on the card of the year it concerns, e.g. the pre-scan of the next year.
- With FALLBACK on, video lists come from the channel's videos page sorted by oldest (in the Edge window). This is complete, needs no API quota and replaces the gappy API date search for old years. Unscanned years can now be filled entirely without quota.
- Years found earlier through the date search are checked against the channel page; missing videos are added to the playlist and moved to the right position. The whole order is checked each time, so an interrupted repair is completed on the next run.
- After an in-app update, the changelog of the new version is shown once in the log.

### Changed
- The playlist check during a run happens every 1000 videos (was 750).
- Browser fallback: the long break comes after every 1000 videos (was 750) and lasts about 5 minutes (was about 10).
- Browser fallback: short cooldown of about 30 seconds every 200 videos (was about 1 minute every 150).
- Browser fallback: 25 videos per batch (was 20), 5 to 15 seconds between batches (was 10 to 25).
- A new release is shown as a green UPDATE button next to HELP instead of a small text tag. In narrow windows HELP shrinks to "?" so the button always fits.
- Larger, brighter text in the update dialog.

## [2.10.0] - 2026-10-31

### Added
- Year order by drag & drop. Selected years are processed from top to bottom in that order.
- The icon of the running year is a pause/resume button.
- Year cards show their state by color: cyan when running right now, yellow when started and waiting, blue-gray when not started, dimmed green with a check mark when done.
- Years that can only continue after the daily API reset are marked "NEEDS API" in red, with the reset time.

### Changed
- Selected years are simply worked through one after another. Chronolist no longer adds years on its own; SKIP AHEAD only picks from the selected years.
- For a new channel all completed years are selected by default.
- Finished years are deselected automatically.
- Browser fallback: 20 videos per batch (was 15), 10 to 25 seconds between batches (was 15 to 30).

### Fixed
- SKIP AHEAD now continues with the next selected year that is ready or in progress, instead of jumping to a new year behind it.

## [2.9.0] - 2026-10-31

### Added
- Updates inside the app (exe only): click the green update tag in the header, confirm, and Chronolist downloads the new release, replaces itself and restarts. Settings, sign-in and progress are kept. Not possible during a run.

### Changed
- The playlist check during a run now happens every 750 videos instead of every 100. Checks on start, after STOP and at the end of a year stay.

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

[Unreleased]: https://github.com/LooneyN/chronolist/compare/v2.10.0...HEAD
[2.10.0]: https://github.com/LooneyN/chronolist/compare/v2.9.0...v2.10.0
[2.9.0]: https://github.com/LooneyN/chronolist/compare/v2.8.0...v2.9.0
[2.8.0]: https://github.com/LooneyN/chronolist/compare/v2.7.0...v2.8.0
[2.7.0]: https://github.com/LooneyN/chronolist/releases/tag/v2.7.0
