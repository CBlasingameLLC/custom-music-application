# Personal Music Toolkit

A local-only, $0-cost toolkit for organizing a personal MP3 library, importing
Spotify listening history, generating new-music recommendations via
ListenBrainz/Last.fm, and syncing curated selections onto MP3 players/DAPs via
microSD.

This is **not** a streaming server and **not** a player. It operates on files
and metadata only, and hands off actual playback to whatever desktop player is
already in use (Strawberry, MusicBee, foobar2000, VLC, etc.). Nothing here
ever binds a network port except the optional local dashboard, which is
strictly bound to `127.0.0.1`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp config.example.toml config.toml
# edit config.toml: set library.roots, your MusicBrainz contact email,
# and your ListenBrainz user token (from listenbrainz.org/profile/)
```

## Usage

```bash
mtk --help
```

Commands are introduced incrementally as each build phase lands:

| Command | Purpose | Status |
|---|---|---|
| `mtk scan <path>` | Scan a library root into the database | Done |
| `mtk tag <path> [--apply]` | Enrich sparse tags via MusicBrainz | Done |
| `mtk organize <path> [--apply]` | Move/rename into the canonical folder scheme | Done |
| `mtk dedupe [--apply] [--content-hash]` | Detect and quarantine likely duplicates | Done |
| `mtk devices` | List detected removable volumes | Done |
| `mtk import-playlist <path> [--name NAME]` | Import an M3U/M3U8 playlist | Done |
| `mtk sync <target> [--playlist X \| --tag X \| --min-rating N \| --all] [--apply] [--prune]` | Sync a selection onto a device | Done |
| `mtk import-spotify <zip_or_folder> [--submit-listenbrainz]` | Import Spotify's Extended Streaming History | Done |
| `mtk recommend [--source listenbrainz\|lastfm\|both] [--limit N]` | Fetch new-music recommendations | Done |
| `mtk review` | Triage pending recommendations | Done |
| `mtk dashboard [--port N]` | Optional local-only web dashboard (127.0.0.1 only) | Done |

Every destructive operation (tag writes, file moves, device sync) defaults to
a dry-run; pass `--apply` to actually execute.

## Desktop app (Windows)

Download the latest `MusicToolkit-Setup-<version>.exe` from the
[Releases page](https://github.com/CBlasingameLLC/custom-music-application/releases/latest).
No Python, Node, or build tools required to use it.

To build it yourself instead: a minimal Electron shell spawns this same
backend as a bundled executable and displays its dashboard in a native
window. See [`desktop/README.md`](desktop/README.md) for the build steps
(must be built on Windows).

Either way, the app keeps its config, library database, and logs under
`~/.musictoolkit/` (`%USERPROFILE%\.musictoolkit\` on Windows). It can browse
the library, scan a music folder (read-only), and triage recommendations;
tag/organize/dedupe/sync/import-spotify/recommend remain CLI-only for now.

## Development

```bash
pytest
```

Test fixtures under `tests/fixtures/` are tiny synthetic MP3s with known
baked-in tags — no real library or network access required to run the suite.

## Releasing

The version lives in `src/musictoolkit/__init__.py` (`pyproject.toml` reads it)
and `desktop/package.json`; CI fails if they disagree. To ship a release:

```bash
python scripts/bump_version.py 0.2.0   # updates every place the version lives
git commit -am "Release 0.2.0"         # open a PR, merge to main
```

Merging to `main` runs `.github/workflows/build.yml`: if no `v0.2.0` tag exists
yet, it builds and tests on Windows, tags the exact commit it built, and
publishes the installer with generated release notes. Pull requests build and
test everything but publish nothing.
