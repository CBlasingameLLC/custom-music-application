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
| `mtk import-spotify <zip_or_folder>` | Import Spotify's Extended Streaming History | Phase 3 |
| `mtk recommend` | Fetch new-music recommendations | Phase 4 |
| `mtk review` | Triage pending recommendations | Phase 4 |
| `mtk dashboard` | Optional local-only web dashboard (127.0.0.1 only) | Phase 5 |

Every destructive operation (tag writes, file moves, device sync) defaults to
a dry-run; pass `--apply` to actually execute.

## Development

```bash
pytest
```

Test fixtures under `tests/fixtures/` are tiny synthetic MP3s with known
baked-in tags — no real library or network access required to run the suite.
