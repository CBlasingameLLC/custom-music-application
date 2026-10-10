# Music Toolkit

A local-first music player and library manager for Windows. It plays your own
files (MP3, FLAC, M4A, Ogg, Opus, WAV), organizes them, learns your taste from
your listening, and suggests new music, with no account, no subscription and no
cloud. The long-term goal is to replace a streaming subscription with a library
you own.

Desktop app = Electron window + a Python (FastAPI) backend bound to
`127.0.0.1`. Everything lives under `~/.musictoolkit/`.

## Install (Windows)

Download `MusicToolkit-Setup-<version>.exe` from the
[latest release](https://github.com/CBlasingameLLC/custom-music-application/releases/latest)
and run it. No Python, Node or build tools needed. The installer is unsigned, so
SmartScreen shows "unrecognized publisher" once; that is expected.

First launch: **Add music folder**, pick your library, done. The app re-scans your
folders each time it starts.

From 0.3.0 on the app **updates itself**: it checks this project's GitHub releases
shortly after it starts and every few hours, downloads a newer version in the
background, and installs it when you close the app (or right away from
Settings, Updates, "Restart and install"). Switch it off in the same card.
Installs of 0.2.0 or older have no updater: install 0.3.0 once by hand.

## What it does (v0.3.0)

| Area | Features |
|---|---|
| **Playback** | In-app player: queue (drag to reorder, play next, save as playlist), shuffle, repeat all/one, seek, volume, ReplayGain loudness leveling, 10-band equalizer with presets, sleep timer, resume where you left off, keyboard media keys (desktop app) |
| **Browse** | Home shelves, Songs (virtualized; checked against a synthetic 100,000-song library, every screen answers in well under a second), Albums, Artists, Favorites, Recently played, instant search |
| **Filters** | Genre, rating, year, favorites, never played, recently added, plus a rule builder (any/all of: text, number, date and yes/no fields). Save any filter as a **smart playlist** |
| **Playlists** | Manual and smart playlists, drag-to-reorder, import / export `.m3u8` |
| **Library** | Star ratings, favorites, song details, cover art (embedded or `cover.jpg`/`folder.jpg`), lyrics (embedded or `.lrc`, synced highlighting), folders added from Settings |
| **History** | Every listen is logged locally, with top-artist stats |
| **Discover** | New-music suggestions from ListenBrainz / Last.fm, minus what you own. A wishlist, plus links to listen or buy. It never downloads music |
| **Devices** | See connected drives and removable media |
| **Look** | Dark and light themes, responsive down to a narrow window |
| **Library tools** | Everything that changes your files shows a preview first, runs as a job with progress and a Cancel button, and can be undone. See below |
| **Updates** | Checks GitHub releases, downloads in the background, installs when you close the app |

### Library tools (Library tools in the sidebar)

| Tool | What it does |
|---|---|
| **Edit tags** (right-click a song, or select several) | Write title, artist, album artist, album, genre, year and track/disc numbers into MP3, FLAC, M4A, Ogg and Opus files, for one song, a selection or a whole album. Every save is one batch; **Undo** puts the old tags back |
| **Fix missing tags** | Songs with no title, artist or album are looked up on MusicBrainz in the background (about one per second, survives a restart). You review the matches (confidence shown), apply the ones you trust, or "Apply all 90%+". Fills empty tags by default; replacing existing ones is an explicit switch |
| **Organize files** | Pick a folder and a layout (presets or your own, with live examples), preview every move, apply as one batch. Lyrics (`.lrc`) follow the song, folder art is copied so albums keep covers, emptied folders are removed, nothing is overwritten (name clashes are listed), Windows rules are enforced (reserved names, 260-character paths). **Undo** restores files, lyrics and covers |
| **Find duplicates** | Finds the same song by MusicBrainz ID, by artist + title + length, and optionally by file content. You pick the copy to keep (best quality is suggested). The rest move to a `_duplicates_review` folder inside your library folder, hidden from the library; their plays, playlist entries, rating and favorite go to the copy you keep, and come back if you restore. Emptying the review folder sends files to the Recycle Bin after you type DELETE |
| **Missing files** | Songs whose files cannot be found (usually an unplugged drive), grouped by folder. They keep their plays and playlists until you choose to forget them |

Still command-line only for now (they ship with proper screens in 0.4.0):
syncing to a player/SD card, importing Spotify history, scrobbling. See the
table below.

### Keyboard

| Key | Action | Key | Action |
|---|---|---|---|
| `Space` | Play / pause | `M` | Mute |
| `←` `→` | Seek 5 s | `S` / `R` | Shuffle / repeat |
| `Shift` + `←` `→` | Previous / next | `Q` | Queue |
| `Shift` + `↑` `↓` | Volume | `N` | Now playing |
| `/` | Search | `L` | Favorite the current song |

## Command line

The same backend ships as `mtk` for scripting. Every command that changes files
defaults to a dry run; pass `--apply` to execute.

| Command | Purpose |
|---|---|
| `mtk dashboard [--port N]` | Start the UI (what the desktop app runs). Prints a URL that includes the per-launch token |
| `mtk scan <path>` | Scan a folder into the library database |
| `mtk tag <path> [--apply]` | Enrich sparse tags via MusicBrainz (the app's Fix missing tags page does this with review and undo) |
| `mtk organize <path> [--apply]` | Move/rename into the canonical folder scheme (the app's Organize files page adds preview and undo) |
| `mtk dedupe [--apply] [--content-hash]` | Detect and quarantine likely duplicates, keeping the best copy of each |
| `mtk devices` | List removable volumes |
| `mtk import-playlist <path> [--name NAME]` | Import an M3U/M3U8 playlist |
| `mtk sync <target> [--playlist X \| --tag X \| --min-rating N \| --all] [--apply] [--prune]` | Copy a selection onto a device |
| `mtk import-spotify <zip_or_folder> [--submit-listenbrainz]` | Import Spotify's Extended Streaming History |
| `mtk recommend [--source listenbrainz\|lastfm\|both] [--limit N]` | Fetch new-music recommendations |
| `mtk review` | Triage pending recommendations |

## Where things live

`~/.musictoolkit/` (`%USERPROFILE%\.musictoolkit\` on Windows)

- `config.toml`: settings (edited from the app's Settings page)
- `data/library.db`: the library database (SQLite). Settings has a **Back up database** button
- `cache/art/`: cached cover thumbnails (safe to delete)
- `logs/`: `musictoolkit.log` (the app), `backend.log` (startup output) and `updater.log` (update checks)

## Security model

The backend only listens on `127.0.0.1`. Because it can read your files and
write tags, every launch generates a random token: the desktop app passes it to
the backend through the environment and signs the window in with it; `/api/*`
refuses requests without it, requests whose `Host` is not a loopback name are
rejected (DNS rebinding), and a Content-Security-Policy confines the page to its
own origin. The window itself can't navigate away or open other sites; links
open in your default browser.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev,e2e]"
python -m playwright install chromium                    # only for the browser tests
mtk dashboard                                            # prints http://127.0.0.1:<port>/?token=...
```

The UI is plain ES modules in `src/musictoolkit/web/static/` (Preact + htm,
vendored under `vendor/`, no build step, nothing to `npm install`). The API is
in `src/musictoolkit/web/routers/`.

```bash
pytest                       # unit + API tests (~30 s); browser tests skip if Playwright is absent
pytest tests/e2e             # the UI in real Chromium: playback, queue, tools, shortcuts...
python scripts/smoke_frozen.py dist/mtk-backend.exe tests/fixtures   # the frozen backend: first run, scan, organize, duplicates
cd desktop && npm test       # the update logic of the desktop shell
```

Fixtures under `tests/fixtures/` are tiny synthetic MP3s with known tags (plus a
12-second tone for playback tests); no real library or network is needed.
The desktop shell and its checks are described in
[`desktop/README.md`](desktop/README.md).

## Releasing

The version lives in `src/musictoolkit/__init__.py` (`pyproject.toml` reads it)
and `desktop/package.json`; CI fails if they disagree.

```bash
python scripts/bump_version.py 0.3.1   # updates every place the version lives
git commit -am "Release 0.3.1"         # open a PR, merge to main
```

Merging to `main` runs `.github/workflows/build.yml`: unit tests (Windows),
browser tests (Linux), then it builds the frozen backend and the installer,
smoke-tests both (the installer is installed and its window driven), upgrades the
previous release in place and checks the data survived, and, if no `v0.3.1` tag
exists yet, tags the exact commit it built and publishes the installer together
with `latest.yml` and the `.blockmap` (what installed copies read to find and
download the update), with generated release notes. Pull requests run everything
but publish nothing.
