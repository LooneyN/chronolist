<p align="center">
  <img src="assets/logo.png" width="112" alt="Chronolist logo">
</p>

<h1 align="center">Chronolist</h1>

<p align="center">
  Builds chronological YouTube playlists of a channel's videos in upload order: one per year, plus your own keyword lists such as a series.
</p>

<p align="center">
  <a href="https://github.com/LooneyN/chronolist/releases/latest"><img src="https://img.shields.io/github/v/release/LooneyN/chronolist?label=release" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/LooneyN/chronolist" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/platform-Windows-blue" alt="Windows">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+">
</p>

---

<p align="center">
  <img src="assets/screenshots/main.png" width="860" alt="Chronolist main window with year and custom list cards">
</p>

YouTube only lets you sort a channel's uploads by "latest" or "popular". Chronolist fixes that: for any channel you choose, it creates playlists like **"ChannelName 2012 in upload order"** (the title is configurable) and fills them with all videos from that year, oldest first. On top of that you can build your own lists, for example one per series or season, from keywords in the video titles. Perfect for watching a long-running channel from the very beginning.

## Features

- **Any channel**: enter a `@handle`, channel URL or channel ID; name and start year are detected automatically.
- **One playlist per year**, filled in exact upload order, with progress saved per channel and year.
- **Custom lists**: collect videos by title keywords (contains any / all / none of them, optionally limited to a range of years) into a playlist with your own name and description, always in upload order. The editor shows the matches live, lets you exclude or add single videos and start from a copy of an existing list.
- **Suggestions**: finds series in your video titles (e.g. "Season 04 #12") and prepares a list for each one.
- **Stays up to date**: the current year and all custom lists are re-synced on every run, so new uploads are appended, new matches are added and excluded videos are removed.
- **Quota-aware**: knows YouTube's daily API limits, remembers when they are used up and shows when they reset (in your local time).
- **Archive scan**: reads the whole upload list once and prepares all years at very low quota cost; older years beyond YouTube's 20,000-upload cap are found via a month-by-month date search, or, with the browser fallback, completely and without quota via the channel's videos page sorted by oldest.
- **Self-checking**: remembers the batch in flight and checks only that one after an interruption; a full duplicate check runs at the end of each year and with CHECK.
- **Videos only**: shorts and livestreams are left out.
- **Pause, resume, stop**; if a year can't be searched right now, Chronolist continues with the next ready one. **HIDE DONE** collapses finished cards to keep the list short.
- **More than 5,000 videos per year**: split automatically into "Part 1", "Part 2", … because a YouTube playlist holds at most 5,000 videos.
- **Auto-run** on launch and optional **start with Windows** (in the program settings).
- **Optional browser fallback** (off by default, see the warning below).
- **Updates in the app**: a green UPDATE button in the header tells you when a new release is available; one click downloads it, replaces the exe and restarts. Your settings and progress stay.

<p align="center">
  <img src="assets/screenshots/list-editor.png" width="400" alt="Custom list editor with keyword filter and live preview">
  &nbsp;
  <img src="assets/screenshots/suggestions.png" width="400" alt="Series suggestions found in the video titles">
</p>

## Download

Grab `Chronolist-vX.Y.Z-windows.zip` from the [latest release](https://github.com/LooneyN/chronolist/releases/latest), unzip it into its own folder and start `Chronolist.exe`.

All settings and progress are stored in that folder, so keep it in a place where you have write access (not `C:\Program Files`).

> Windows SmartScreen may warn about an unknown publisher because the exe is not code-signed. Choose **More info → Run anyway**, or run from source instead.

## Set up your own Google API access

Chronolist uses the official YouTube Data API v3 with **your own** credentials. Nothing is shared with the author. Usage is free; Google just limits how much you can do per day.

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create a new project.
2. Under **APIs & Services → Library**, enable **YouTube Data API v3**.
3. Under **APIs & Services → OAuth consent screen**, set it up as **External** and add your own Google account as a **test user**.
4. Under **APIs & Services → Credentials**, choose **Create credentials → OAuth client ID** with the application type **TVs and Limited Input devices**.
5. Copy the **Client ID** and **Client secret** into Chronolist via the **API** button in the header.
6. On the first START, Chronolist shows a link and a code. Open the link, enter the code and sign in with the YouTube account that should own the playlists.

## Usage

1. Click **EDIT** on the *Target Channel* card and enter the channel.
2. All completed years are selected. Deselect the ones you don't want and drag the cards to change the order; selected cards are processed from top to bottom.
3. Optional: **+ CUSTOM LIST** creates a list of your own, **SUGGESTIONS** finds series in your titles, **HIDE DONE** collapses finished cards.
4. Press **START**.

The in-app **HELP** (English or German, following the program language) explains every button, status and pause in detail.

### Daily limits

| Action | Cost |
| --- | --- |
| Archive scan (upload list) | 1 unit per 50 videos |
| Date search for old years | 100 units per 50 videos, plus ~100 searches per day limit |
| Create a playlist | 50 units |
| Add one video | 50 units |
| Check a playlist | 1 unit per 50 videos |
| Remove a duplicate | 50 units |

The default quota is 10,000 units per day, roughly 200 added videos. It resets at midnight Pacific Time.

### Browser fallback ⚠️

When the daily quota is used up, the optional **FALLBACK** continues in a separate Microsoft Edge window where you are signed in to YouTube, using the same requests the website itself sends when you save a video to a playlist. It works in batches with deliberate pauses.

**This is automated use of the YouTube website and is not allowed by YouTube's Terms of Service. In the worst case your account can be restricted. Use it at your own risk.** That is why it is off by default.

## Configuration

Under **PROGRAM** in the app you can set:

- The **language**: English or German (interface, tooltips, help and log)
- The **text size** of the whole interface: 90, 95, 100, 105, 110, 115 or 135 % (default 100 %)
- The **update check** on launch

Under **PLAYLISTS** you can set:

- **Title** and optional **description** of new playlists, with placeholders `{channel}`, `{handle}`, `{year}` and `{count}` (number of videos in that year) and a live preview
- **Visibility**: public, unlisted or private

The name of a custom list is its whole playlist title; the title template only applies to years.

Playlist changes apply to playlists created from then on. Everything is stored in `settings.json` next to the program.

## Your data

Everything stays in the program folder and is never uploaded anywhere except to Google/YouTube itself:

| File / folder | Content |
| --- | --- |
| `credentials.json` | Your Client ID and secret |
| `token.json` | Your Google sign-in |
| `settings.json` | Settings and selected channel |
| `quota.json` | Remembered daily limit |
| `data/<channel>/` | Video lists, titles, custom lists and progress |
| `browser_profile/` | Sign-in of the fallback Edge window |

Never share these files. They are excluded in `.gitignore`.

## Run from source

Requires Windows and Python 3.10 or newer.

```bash
git clone https://github.com/LooneyN/chronolist.git
cd chronolist
pip install -r requirements.txt
pythonw chronolist.py
```

The browser fallback uses Microsoft Edge, which comes with Windows; no extra browser download is needed.

Build the exe yourself:

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name Chronolist --icon assets/chronolist.ico chronolist.py
```

## Releases

Releases are built automatically by GitHub Actions. Day-to-day work happens on the `dev` branch; `main` only receives finished releases. To publish a new version:

1. Merge `dev` into `main`.
2. Set `VERSION` in `chronolist.py` to the final number (no `-dev` suffix) and move the `[Unreleased]` entries in `CHANGELOG.md` to a new version section.
3. Push `main`, then either push the tag (`git tag v3.0.0` and `git push origin v3.0.0`) or start **Actions → Release → Run workflow** on `main`, which creates the tag itself.

The workflow checks that the tag matches `VERSION`, builds `Chronolist.exe` and publishes the release with the changelog entry as notes.

## About

Chronolist was written entirely with [Claude](https://claude.ai), an AI by Anthropic.

It is an independent project and is not affiliated with, endorsed by or sponsored by YouTube or Google. You use your own API credentials and your own account and are responsible for complying with YouTube's Terms of Service.

Bug reports and ideas are welcome as [issues](https://github.com/LooneyN/chronolist/issues).

## License

[MIT](LICENSE)
