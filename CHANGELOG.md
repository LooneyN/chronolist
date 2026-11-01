# Changelog

All notable changes to Chronolist are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- TEXT SIZE in the PROGRAM settings (90, 95, 100, 105 or 110 %). The default 100 % is larger than before, so text is easier to read on large screens; dialogs never grow larger than the screen. The list editor now uses two columns so it stays compact.

### Changed
- Custom list editor: the keyword fields are now ANY OF / ALL OF / NONE OF under one sentence ("a video is included if its title contains ..."), each with a short hint, and a line below sums up what your input means (e.g. "Matches: (minecraft OR zelda) AND season 1, but NOT live").

## [3.0.0] - 2026-11-01

### Highlights
- Custom lists: collect videos by title keywords into your own playlists (a series, a season, a topic), always in upload order and kept up to date on every run.
- Suggestions: Chronolist finds series in your video titles and prepares a list for each one.
- A real editor for lists: live preview, search, multi-select, exclude / include single videos, year range, own name and description, and templates from existing lists.
- New dashboard: years and lists side by side as TARGETS, large control tiles, HIDE DONE to keep the view short, and links in the log that copy on click.
- Titles are stored while scanning, at no extra quota, which is what makes all of the above possible.
- Thumbnail upload removed: YouTube's API only accepts square playlist images, so set thumbnails in YouTube Studio.

### Added
- SUGGESTIONS button: finds series in the stored titles (same beginning, then an episode number such as "#12", "Folge 3", "Teil 7") and opens the list editor with name and keyword filled in. Series that an existing list already covers are left out. A search box narrows the suggestions by keyword.
- Custom lists can have their own DESCRIPTION (optional). Empty = the description template from the PLAYLISTS settings.
- New list editor offers a TEMPLATE selection when lists already exist (name and filter are copied, not single videos).
- HIDE DONE / SHOW DONE in the TARGETS bar collapses completed cards that are not ticked. Ticked ones (current year, lists) stay visible because they are synced on every start.
- Links in the log copy to the clipboard on click (with a short "link copied" hint).
- Video titles are now stored while scanning (API and channel page, no extra quota). This is the basis for keyword lists.
- Keyword lists: CUSTOM LIST under the years opens an editor with live preview (hit count and titles). Optionally limit a list to a range of years. Exclude single videos or add videos by link/ID. Lists are cards like years (right-click edits) and run after the years.
- Keyword lists (details): a list collects videos by title (any / all / exclude terms, plus single videos added or skipped) and is filled like a year in upload order. Missing titles are loaded from your own playlists (API) or via oEmbed. 

### Changed
- The name of a custom list is now the whole playlist title; the title template from the PLAYLISTS settings only applies to years. Lists that already have a playlist keep their current title.
- Custom list editor: FILTER RESULTS / ALL CHANNEL VIDEOS view, a title search, separate LIST SETTINGS (name) and FILTER (keywords, years) sections, and separate EXCLUDE and INCLUDE buttons. The FILTER always searches all videos (several terms separated by commas), so videos that do not match your keywords can be found and included from either view.
- In the custom list editor you can select several videos (Ctrl/Shift+click, Ctrl+A) and exclude or restore them at once.
- The year list is now called TARGETS (lists and years sit side by side); the button is CUSTOM LIST. Loading missing titles shows count and percent on the list card.
- Cleaner dashboard: only FALLBACK, PROGRAM and PLAYLISTS remain under options. AUTO-RUN and BOOT moved to the program settings.
- Aligned layout: both columns share one grid. START / PAUSE / STOP / CHECK are large tiles as tall as the channel card, all list and option cards have the same height, and right edges line up.
- Skipping ahead to the next ready year is now always on; the SKIP AHEAD switch is gone.

### Removed
- The optional playlist thumbnail upload. YouTube's API only accepts square playlist images (the Studio crops them itself), so the feature was dropped. Set thumbnails in YouTube Studio. Already uploaded thumbnails stay as they are; the old `thumbs` folder in your data folder can be deleted.
- BATCH LIMIT (videos per year and run).

### Fixed
- The progress display no longer freezes (log, PAUSE/STOP stuck) when a custom list reports progress.
- A card no longer says COMPLETE while the run is still working on it (e.g. repairing a playlist). It shows SYNCING, and the repair shows what it is doing from the first moment.
- Window position is now remembered reliably on Windows (also on a second monitor and when maximized). Before, often only the size was restored.

## [2.11.0] - 2026-11-01

### Added
- ALL / NONE next to the year list selects or deselects every year with one click.
- Overall progress of all years (percent and bar) next to the year list.
- Overview of left-out videos (shorts) with titles and links: click the log line "… REMOVED // SHOW ›" or LEFT OUT in the playlist settings.
- Chronolist remembers the window size and position (and whether it was maximized), as long as that monitor is still connected.
- Shorts are left out (detected through the channel's shorts list via the API); livestreams stay in, since some channels used them for regular content such as extra perspectives. After the update every playlist is checked and repaired once.
- The current year (in January also the previous one) is kept up to date: on every run new uploads are appended. It stays selected when done. New channels now select the current year too.
- Years with more than 5,000 videos are split automatically into "Part 1", "Part 2", … (YouTube's playlist limit). Checking and repairing work per part.
- Year cards show what is happening right now with progress (reading the channel page, adding, sorting, checking the playlist), on the card of the year it concerns, e.g. the pre-scan of the next year.
- With FALLBACK on, video lists come from the channel's videos page sorted by oldest (in the Edge window). This is complete, needs no API quota and replaces the gappy API date search for old years. Unscanned years can now be filled entirely without quota.
- Years found earlier through the date search are checked against the channel page; missing videos are added to the playlist and moved to the right position. The whole order is checked each time, so an interrupted repair is completed on the next run.
- After an in-app update, the changelog of the new version is shown once in the log.

### Changed
- The running year's glow pulses more visibly.
- The help button just says HELP.
- PROGRAM and PLAYLISTS settings can be opened during a run. Playlist changes apply to playlists created from then on; a language change restarts Chronolist only after the run.
- Much faster duplicate check: Chronolist remembers the batch in flight and after STOP or on the next start checks only that batch instead of reading the whole playlist. The full check runs at the end of a year and with CHECK; the interval check during a run is gone. Reading a playlist is also faster.
- Browser fallback is faster: 30 videos per batch (was 20), 4 to 12 seconds between batches (was 10 to 25), a short break of about 20 seconds every 250 videos (was about 1 minute every 150) and a long break of about 4 minutes every 1500 videos (was about 10 minutes every 750).
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

[Unreleased]: https://github.com/LooneyN/chronolist/compare/v3.0.0...HEAD
[3.0.0]: https://github.com/LooneyN/chronolist/compare/v2.11.0...v3.0.0
[2.11.0]: https://github.com/LooneyN/chronolist/compare/v2.10.0...v2.11.0
[2.10.0]: https://github.com/LooneyN/chronolist/compare/v2.9.0...v2.10.0
[2.9.0]: https://github.com/LooneyN/chronolist/compare/v2.8.0...v2.9.0
[2.8.0]: https://github.com/LooneyN/chronolist/compare/v2.7.0...v2.8.0
[2.7.0]: https://github.com/LooneyN/chronolist/releases/tag/v2.7.0
