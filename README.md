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

## What it does (v0.8.2)

| Area | Features |
|---|---|
| **Playback** | In-app player: queue (drag to reorder, play next, save as playlist), shuffle, repeat all/one, seek, volume, ReplayGain loudness leveling, gapless playback and crossfade (up to 12 s), 10-band equalizer with presets, sleep timer, resume where you left off, keyboard media keys (desktop app) |
| **Browse** | Home shelves, Songs (virtualized; timed against a synthetic library of 100,000 songs and 300,000 plays with `scripts/perf_synthetic.py`: Home, Songs, Albums, Artists, mixes and radio answer in under half a second), Albums, Artists, Favorites, Recently played, instant search |
| **Filters** | Genre, rating, year, favorites, never played, recently added, plus a rule builder (any/all of: text, number, date and yes/no fields). Save any filter as a **smart playlist** |
| **Playlists** | Manual and smart playlists, drag-to-reorder, import / export `.m3u8` |
| **Library** | Star ratings, favorites, song details, cover art (embedded or `cover.jpg`/`folder.jpg`), lyrics (embedded, a `.lrc` file beside the song, or, if you turn it on, lrclib.net; synced highlighting), folders added from Settings |
| **Stats** | Your listening in numbers: hours listened, plays, artists, songs, longest streak, and your most played artist, song, busiest day and hour. Charts of plays by month, by hour and by weekday, a grid of every day for up to a year, and top artists, songs, albums and genres, for the last 30 days, the last 12 months, any year, or all time. Every chart has a table version, and every mark answers the pointer and the arrow keys. The first look at years of history takes a few seconds (300,000 plays: about four); the answer is then kept until the history changes |
| **Mixes** | A "Made for you" shelf on Home, new every day: On repeat, Rediscover, On this day, New in your library, Never played, Your favorites, decades and genres, as many as have enough songs. Open one to play it, shuffle it or save it as a playlist. **Start radio** (a song's menu, or the player bar) queues songs that go with it |
| **History** | Every listen is logged locally, with top-artist stats. **Import your Spotify history** (the extended streaming history ZIP) to start with years of data, and optionally send it to ListenBrainz |
| **Discover** | **New releases** by artists you play (the release radar: albums, EPs and singles from the last two months or announced for the next six weeks, from ListenBrainz or MusicBrainz), and new-music suggestions from ListenBrainz / Last.fm, minus what you own. A wishlist, plus links to listen or buy. It never downloads music |
| **Radio** | Live internet radio through the app: your favorite stations, recently played ones, **Browse** the free Radio Browser directory (search by name, kind of music or country; sort by listeners, votes, name or sound quality) and **Add by address** (a stream, or the playlist file a station publishes). The song a station says is on shows in the player bar, the window title, the tray and the media-key overlay. A stream that drops is joined again; a station that cannot be played says why. Nothing is recorded or downloaded. See below |
| **Podcasts** | Find shows in Apple's free podcast directory or follow any RSS feed. New episodes are looked for by themselves about every six hours (switch it off in Settings) or with a button, and **New episodes** lists what you have not heard. Episodes start a few seconds before where you stopped, skip back 15 s and forward 30 s, play at 0.75 to 2 times speed, and count as heard near the end. See below |
| **Devices** | Copy music to a Walkman, phone, SD card or USB stick, whether it shows up as a drive or not (phones and players in MTP mode, experimental): choose what goes on it, preview, copy only what changed. See below |
| **Discs** | **Rip an audio CD** into the library as FLAC (exactly what is on the disc) or MP3 (320, 256, 192 or 128 kbps): the album is found on MusicBrainz from the disc itself, every song is read twice and compared, and it lands in your library with names, tags, cover and a history you can undo. **Burn** songs onto a music CD (with no gap between the songs if you like, and M4A, AAC and WMA songs too), a data disc (CD, DVD or Blu-ray) or into an ISO file, and erase a rewritable disc. Both are experimental until they have met your drive. See below |
| **Accounts** | ListenBrainz, Last.fm and MusicBrainz, each with a **Test connection** button. Plays from the app are sent to ListenBrainz in the background when you turn that on |
| **Diagnostics** | Settings, Diagnostics checks the library, folders, drives, accounts and media keys and writes a report to copy when something does not work (never includes tokens or keys) |
| **Look** | Dark and light themes, an accent colour (eight presets or any colour you pick; the colours derived from it are always kept readable, at least 4.5:1), comfortable or compact density, responsive down to a narrow window. Settings, Appearance |
| **Accessibility** | Everything can be done from the keyboard (a skip link, dialogs that keep the focus, menus and tables you walk with the arrow keys), screens are labelled for screen readers and say which page you moved to, text meets WCAG AA contrast in both themes, and the app follows Windows' *reduce motion*, *more contrast* and high-contrast themes. Checked automatically with axe on every screen; see below |
| **Library tools** | Everything that changes your files shows a preview first, runs as a job with progress and a Cancel button, and can be undone. See below |
| **Desktop** | A tray icon by the clock with Play/Pause, Next, Previous and Quit; closing the window keeps the music playing from the tray (or quits: your choice in Settings, Window and tray); the window opens where you left it; **Open with** in Explorer, and files or folders dropped on the window, are added to the library and played |
| **Updates** | Checks GitHub releases, downloads in the background, installs when you close the app |

### Library tools (Library tools in the sidebar)

| Tool | What it does |
|---|---|
| **Edit tags** (right-click a song, or select several) | Write title, artist, album artist, album, genre, year and track/disc numbers into MP3, FLAC, M4A, Ogg and Opus files, for one song, a selection or a whole album. Every save is one batch; **Undo** puts the old tags back |
| **Fix missing tags** | Songs with no title, artist or album are looked up on MusicBrainz in the background (about one per second, survives a restart). You review the matches (confidence shown), apply the ones you trust, or "Apply all 90%+". Fills empty tags by default; replacing existing ones is an explicit switch |
| **Organize files** | Pick a folder and a layout (presets or your own, with live examples), preview every move, apply as one batch. Lyrics (`.lrc`) follow the song, folder art is copied so albums keep covers, emptied folders are removed, nothing is overwritten (name clashes are listed), Windows rules are enforced (reserved names, 260-character paths). **Undo** restores files, lyrics and covers |
| **Find duplicates** | Finds the same song by MusicBrainz ID, by artist + title + length, and optionally by file content. You pick the copy to keep (best quality is suggested). The rest move to a `_duplicates_review` folder inside your library folder, hidden from the library; their plays, playlist entries, rating and favorite go to the copy you keep, and come back if you restore. Emptying the review folder sends files to the Recycle Bin after you type DELETE |
| **Missing files** | Songs whose files cannot be found (usually an unplugged drive), grouped by folder. They keep their plays and playlists until you choose to forget them |

### Devices (Devices in the sidebar)

1. **Add a device**: plug it in and pick it from the drives the app finds (a drive with a `Music` folder uses that
   folder), or choose a folder yourself.
2. **Choose what goes on it**: any mix of the whole library, favorites, playlists (manual or smart), a genre,
   songs rated at least N stars, or recently added music. Pick the folder layout, and whether playlists
   (as relative `.m3u8` files) and folder pictures come along.
3. **Preview**: what would be copied (new, changed, moved), what is already there, what is no longer chosen,
   and what is left out and why (FAT32's 4 GB limit, paths too long for the device, name clashes), against
   the free space.
4. **Copy**: only what changed. Each file is written under a temporary name and renamed when complete, so
   unplugging mid-copy never leaves half a song; stop any time and carry on later.
5. Songs no longer chosen are removed **only if you tick the box and confirm the exact number**, and only files
   this app copied. A different drive that has taken over the drive letter is never written to.

#### Phones and players without a drive letter (MTP, experimental)

Most phones and many Walkmans connect in **MTP** mode, which has no drive letter. Add a device, then use the
"Phones and players without a drive letter" part of the dialog: plug the phone in with a USB cable, unlock its
screen and choose **File transfer** in the notification on it, then press *Look again*. Pick the storage
(internal storage or SD card) and the folder the music goes in (`Music` unless you say otherwise). From there it
works exactly like a drive: the same choices, the same preview, the same exact-count confirmation before
anything is removed, progress and Cancel.

What is different on a phone:
- It cannot rename a file, so there is no temporary name: a copy that breaks off is deleted again, and a file that
  still looks too small the next time is copied again.
- Keep the phone unlocked while it copies. If it goes to sleep or the cable is pulled the sync stops and says so;
  everything copied so far is kept and the next sync carries on from there.
- Some players do not list `.m3u8` playlists in their own playlist screen, and a phone's music app may need a
  moment (or a restart) to notice new songs.
- A phone that does not say how much room it has is not stopped early; it simply refuses when it is full.
- A USB stick, an SD card or a player in mass-storage mode has a drive letter, so it is added as a drive, not
  here. Windows also lists such drives among its portable devices; the app leaves them out of this list.
- It is labelled *experimental* because it has been checked against a stand-in for a phone and against Windows'
  own interface, not yet against every real model. If a model misbehaves, Settings, Diagnostics shows whether the
  helper is installed and what Windows reports, and the logs folder has the details.

A small helper program (`mtk-mtp.exe`, installed next to the app) does the talking to Windows Portable Devices.

### Discs (Discs in the sidebar)

**Rip a CD** copies an audio disc into your library. It is labelled *experimental*: the parts that talk to the drive have been checked against a
stand-in drive and against Windows' own interface (a mounted disc image), not yet against every real drive and every real disc. Start with one
disc you know, and listen to it.

Put a disc in and the page notices (it looks at the drives every few seconds while it is open; with several drives, pick one). Then:

1. **Which album is it?** The disc itself says what it is: its table of contents (how long each song is) is turned into a disc ID and looked up on
   MusicBrainz, which lists every album that disc belongs to. You choose between *this very disc* and *similar disc* matches, or *None of these* and
   type it in. This needs the MusicBrainz contact email in Settings, Accounts and services (MusicBrainz asks every app to say who is asking);
   without one the page says so, and typing the album in by hand always works.
2. **Check it.** Album artist, album, year, genre and disc number are filled in and can be changed; so can every song's title (and, for a
   compilation, artist), and a song can be left out.
3. **How to rip it.** FLAC or MP3 (with a bit rate), *Verified* or *Fast* reading, which music folder it goes in, and the folder layout (the same
   fields as Organize files, with a live example), whether to put the cover in, and whether to eject the disc when it is done.
4. **Where the songs will go.** The page shows every song's path, how much room it needs, and what it will leave alone, and the **Rip** button waits
   for that. A name that is already taken is never overwritten: that song is left out and the preview says so.
5. **Rip.** Each song is a step with its own progress and status; **Cancel** stops after the block being read and keeps the songs that are done.
   If the app is closed in the middle, the next start marks the rip *interrupted* and removes what was half written.

What a rip puts in your library: `Artist/Album/01 - Title.flac` (or your own layout), tagged with title, artist, album artist, album, date, track and
disc numbers and totals, genre, the MusicBrainz IDs of the recording, release, release group and artist, barcode and label; the cover picture
embedded in each song and as `cover.jpg` in the folder (unless the folder already has one). Each song is written under a temporary name and renamed
when it is complete, so no half-written song is ever where a player could find it, and it is added to the library at once. If Windows says a file
is in use for a moment (a virus scanner or a sync program can hold a file just after it was written), the app waits and tries again a few times
before it gives up on that one song and goes on with the next.

**How a song is checked (*Verified*).** A drive can hand back wrong data without saying so, and it remembers what it read last, so asking again
straight away may only repeat the mistake. The reader reads the song once, in blocks of 16 sectors, remembering a checksum of each; then it pushes
the drive's memory out by reading somewhere else on the disc, reads the song again and compares. A block whose two readings differ is read again
(each time after pushing the memory out) until some version has been seen twice. A block that never settles is kept as its likeliest version and
**marked** (the song says *Done, with trouble*, and the history says where, as minutes and seconds into the song); a block the drive cannot read at
all becomes silence and is marked; a song with too many such blocks is given up on and the next one is tried. Nothing is passed off as good. *Fast*
reads once. This catches a drive that gives different answers; it cannot catch one that gives the same wrong answer every time (that is what
AccurateRip, which this does not have, is for). A disc with trouble is left in the drive.

**History and undo.** *History* lists every rip with what happened to every song. **Take back** sends the songs of a rip, and the cover it made, to
the Recycle Bin and forgets them in the library; a song you have edited, retagged or replaced since is left alone, as is a cover picture that was in
the folder before. The rip's own record stays, marked *Taken back*.

**What is sent, what is kept.** The disc's table of contents goes to musicbrainz.org (the album lookup) and the album's cover is fetched from the
Cover Art Archive (coverartarchive.org, through the same checks as every other address the app fetches, and re-drawn before it is kept). That
happens when the Rip page shows a disc and the MusicBrainz contact is set, and when you press *Ask MusicBrainz again*; nothing else leaves the
computer. Kept: the history (disc ID, album, and for every song where it went, its size and whether it was read cleanly) in the library database.
The audio of the song being ripped is held in a temporary file under `cache/discs`, deleted when the song is finished or stopped.

**What it does not do.** No AccurateRip, no drive read-offset correction and no C2 error pointers; no hidden tracks (the pre-gap before track 1) and
no data tracks (the data session of an Enhanced CD is ignored); no CD-Text; a copy-protected disc may fail to read; discs are only noticed while the
Discs page is open. It is Windows only: the page says so on another system. Burning has its own tab, described next.

**Burn a disc** (the second tab) puts songs from your library on a disc, or into a disc image file. It is labelled *experimental*, more so than
ripping: no test of ours has a real recorder. The tests use a stand-in recorder that keeps its discs in plain folders, and Windows' own image
builders are checked on a real Windows machine as far as one without a burner allows (for a music CD without gaps, the image of the whole disc is
built and read back: every song is in it word for word, and nothing lies between the songs). **Try a CD-RW first** (it can be erased and used again).
A burn cannot be undone, and one that is stopped or fails once it has begun writing spoils a CD-R for good; the question before every burn says so.

1. **Make a list.** *Add to burn list* is in the menu of a song, on an album and on a playlist. The list is kept while the app is open, can be
   reordered and trimmed, and is burned in the order shown.
2. **Choose what to make.**
   - *Music CD*: plays in any CD player or car stereo. MP3, FLAC, WAV, Ogg, Opus, M4A, AAC and WMA songs can be burned. Windows decodes the last
     three itself (through Media Foundation, into a temporary file that is removed at once); a copy-protected song, or a Windows without the
     decoders (an "N" edition needs Microsoft's Media Feature Pack), is listed as *Cannot be burned* in the words Windows gave. A song at another
     sample rate is converted to 44.1 kHz (with a high-quality resampler), a mono song plays on both speakers, a song with more than two channels is
     refused, and a 24-bit or floating-point song is reduced to 16 bits (with dither). Any other song (Apple Lossless, APE ...) is listed as
     *Cannot be burned* rather than skipped silently. **Between songs** you choose *no gap* (the whole disc is written in one go, so each song
     begins where the one before ends: right for live albums and mixes; it is the default, switchable in Settings, and offered only where the
     recorder can do it) or *two seconds of silence* (the songs written one by one, the way Windows' track-at-once burning does). At most 99 songs,
     and about 80 minutes on a CD-R. Only a blank CD-R or CD-RW can take a music CD. It is **not** read back afterwards: listen to it once.
   - *Data disc*: the song files as they are, for a computer, an MP3 player or a car stereo that reads files, on a CD, DVD or Blu-ray disc. The
     layout is the folders of your library, or one numbered folder (best for car stereos). File and folder names are made safe for every disc
     format (ISO 9660, Joliet, and UDF on discs bigger than a CD), and two files that would end up with the same name are told apart. The disc is
     closed afterwards. Every file is read back from the disc and compared with the original by checksum (switch it off in Settings).
   - *Disc image (ISO file)*: the same as a data disc, in one file, in the folder you choose (Downloads to begin with). It never replaces a file
     that is there. No drive is needed.
3. **Will it fit?** Before anything is written the page shows, for every song or file, what will happen to it, how much of the disc the list
   takes (a bar), and what is wrong if something is (too long, too many songs, not enough room for the temporary copy of the music, a disc that is
   not blank). The **Burn** button waits for that, and the app refuses to start a burn that was not first shown.
4. **Confirm.** The question says what is about to be written, that it cannot be undone, and what stopping does to the disc.
5. **Burn.** Everything that can go wrong without touching the disc happens first: for a music CD every song is converted into a temporary file
   (`cache/discs/burn`), the files of a data disc are checked to be as they were, and the disc is looked at again. Then it writes, with progress.
   **Cancel** is passed on to the recorder at once. The history says what became of the disc and whether it is spoiled; the temporary files are
   always removed.
6. **Erase.** A CD-RW or DVD-RW with something on it can be erased (quickly, or fully, which takes many minutes). An erase cannot be stopped once it
   has begun. The question says that everything on the disc is deleted.

**What it does not do.** No CD-Text (the titles a car stereo may show), no read-back check of a music CD, no adding to a disc that has been written (every burn closes the disc), no disc copying, no DVD-Video. Burning is Windows only,
through Windows' own disc burning service (IMAPI): no drivers and no administrator rights. A recorder that Windows cannot burn with, or a disc it
does not support, is reported in words, not guessed at.

A small helper program (`mtk-disc.exe`, installed next to the app) does the talking to the drive, with Windows' own device controls: no drivers, no
administrator rights. All the decisions (checking, retrying, converting, encoding, tagging, naming) are made in the app, where they are tested
without a drive.

### Radio and podcasts (Radio and Podcasts in the sidebar)

Both play from the internet, **through the app**. The page may only talk to its own backend (see Security model), and the equalizer and the
crossfade need the sound to come from there too, so the app fetches a station or an episode and hands it on. It only ever fetches addresses it
has kept itself (a station you added, a show you followed), never an address the page names.

- **What is sent, and when.** Nothing until you look. The station directory (radio-browser.info) is asked when you open *Browse*, Apple's podcast
  directory when you search, and a station, feed or episode host when you add, play or check it. Directories see the words you type; every host sees
  an ordinary request from your computer with the app's name as its User-Agent. Playing a station from *Browse* also tells Radio Browser that one
  more person listened (it ranks stations by that, and asks apps to say so). No account, no identifier, nothing else.
- **What is kept.** Your stations, the shows you follow and their episodes (title, date, length, where you stopped) are in the library database;
  pictures are re-drawn small and cached in `cache/radio` and `cache/podcasts`. Nothing is recorded, saved as a file or downloaded, and listening to
  radio or podcasts is never added to your music history, statistics, mixes or Year in Music.
- **Stations.** *Favorites* (the heart), *Recent*, *Browse*, and *Add by address*, which tries the address first: one that does not play is not kept,
  and what the stream says about itself fills in the name, kind of music and bit rate. A playlist file (`.pls`, `.m3u`, `.asx`) is followed to the
  stream inside it. The title a station sends is taken out of the audio and shown beside the station's name. A live stream cannot be sought or
  paused for later: *Pause* lets go of it and *Play* joins it again, live. A stream that drops is joined again up to three times, a little later each
  time, and then the player says so. HLS streams (`.m3u8`) cannot be played by a browser engine and are refused with a message, and a few stations
  only play to their own players.
- **Podcasts.** *Follow* reads the feed first, so an address that is not a podcast is not followed and the message says why. The latest three episodes of
  a show you have just followed count as new (its back catalogue does not), and so does everything that comes out afterwards, until you have heard it;
  an episode you have started stays in *New episodes* too. Feeds are asked with the ETag and Last-Modified of the last answer, so an unchanged feed costs
  almost nothing. Seeking works even with hosts that ignore range requests. An episode is *heard* when it is within 30 seconds of the end (a tenth of
  its length for a short one); *Mark as heard* and *Mark all as heard* do it by hand. The place in an episode is kept every ten seconds, when you pause
  or move on, and when you close the window.
- **Safety.** An address that came from a directory, a feed or a pasted link is connected to only if it leads to the public internet: addresses on this
  computer or your home network, and redirects that end up there, are refused, and each connection goes to the address that was checked (so a name
  that changes its answer cannot slip past). A station on your own network is therefore not possible. Feeds are read with a hardened XML reader (no
  entities, no external files), everything fetched has size and time limits, and at most four streams are open at once.

### Online lookups (Settings, Online lookups)

Two features talk to a service on their own account, so both are **off until you turn them on**, and both also work
one press at a time with the switch off:

- **Lyrics** for a song whose file has none: sends the artist, title, album and length of that one song to
  [lrclib.net](https://lrclib.net) (a free, community-run service). Lyrics in the file, or in a `.lrc` file beside it, always
  come first. What comes back is kept in the app's database, never written next to your music: a song LRCLIB did not
  have is asked about again after two weeks, a retagged song at once. Look up one song from its lyrics page (Now Playing).
- **New releases** (Discover, New releases): with your ListenBrainz username it asks ListenBrainz, which knows your
  listens; otherwise it asks MusicBrainz about the artists you play most, one request each, and needs a contact there
  (Settings, Accounts). Live albums, compilations, remixes and soundtracks are left out, and so is anything already in your
  library. Check by hand with the button, or let it look by itself about once a day. Nothing is downloaded: each
  release has links to MusicBrainz, Bandcamp and YouTube.

### Spotify history and ListenBrainz

Listening history, then Import Spotify history walks through it: request the *Extended streaming history* from
Spotify, choose the ZIP, see what is in it (plays, years, most played artists), add it. Importing twice adds
nothing twice and every import can be undone. Sending it to your ListenBrainz account is optional and resumable.
Plays you make in the app are sent to ListenBrainz in the background (Settings, Accounts) when you have saved a
token: they are kept and retried if you are offline, and a rejected token is reported instead of retried forever.

### Keyboard

| Key | Action | Key | Action |
|---|---|---|---|
| `Space` | Play / pause | `M` | Mute |
| `←` `→` | Seek 5 s | `S` / `R` | Shuffle / repeat |
| `Shift` + `←` `→` | Previous / next | `Q` | Queue |
| `Shift` + `↑` `↓` | Volume | `N` | Now playing |
| `/` | Search | `L` | Favorite the current song (or station) |

In a table of songs, Tab reaches the table once (its column headers are the next stops, `Enter` sorts):

| Key | Action |
|---|---|
| `↑` `↓`, `Page Up` `Page Down`, `Home` `End` | Move through the songs; the one you land on is selected |
| `Shift` + those | Select a range |
| `Ctrl` + those | Move without selecting; `Ctrl` + `Space` selects or deselects the song you are on |
| `Ctrl` + `A` / `Esc` | Select all / clear the selection |
| `Enter` | Play the song |
| `Menu` or `Shift` + `F10` | The song's menu (`↑` `↓` `Home` `End` move, `Enter` chooses, `Esc` closes) |
| `0` – `5` | Rate the selected songs (`0` clears the rating) |
| `Alt` + `↑` `↓` | Move the song up or down a playlist; with a song's play button focused in the queue, up or down the queue |
| `Delete` | Remove the selected songs from a playlist |

In dialogs `Tab` stays inside the dialog and `Esc` closes it, putting the focus back where it was. Tabs (such as Lyrics, Up next, Sound)
are one stop: `←` `→` `Home` `End` move between them.

### Accessibility

What is checked, on every build, in a real browser (`tests/e2e/test_accessibility.py`):

- **axe-core** (WCAG 2.0 and 2.1 A and AA, the 2.2 AA rules and axe's best practices) finds nothing on any of about 40 screens
  and dialogs, in both themes: names and roles, headings and landmarks, colour contrast, size of what can be clicked.
- **Keyboard**: the first stop is a skip link; a dialog takes the focus, keeps it and gives it back; menus and the song table are
  driven by the arrow keys; tabs follow the usual pattern; a change of page moves the focus to the page and says its name;
  every stop on the home page shows where the focus is; nothing in the song table is reachable only by mouse (rating, menu,
  sorting, reordering).
- **System settings**: *reduce motion* (no visualizer, no smooth scrolling, no looping animations), *more contrast*, and Windows
  high-contrast themes (selection is shown by an outline, not only by a colour).

What these cannot say is what a screen reader reads aloud, or how the app feels with one. If something is awkward, that is a bug:
please report it.

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
| `mtk sync <target> [--playlist X \| --tag X \| --min-rating N \| --all] [--apply] [--prune]` | Copy a selection onto a device (the app's Devices page adds a preview, progress and confirmation) |
| `mtk import-spotify <zip_or_folder> [--submit-listenbrainz]` | Import Spotify's Extended Streaming History (the app's Import Spotify history page walks through it) |
| `mtk recommend [--source listenbrainz\|lastfm\|both] [--limit N]` | Fetch new-music recommendations |
| `mtk review` | Triage pending recommendations |

## Where things live

`~/.musictoolkit/` (`%USERPROFILE%\.musictoolkit\` on Windows)

- `config.toml`: settings (edited from the app's Settings page)
- `data/library.db`: the library database (SQLite). Settings has a **Back up database** button
- `cache/art/`, `cache/radio/`, `cache/podcasts/`: cached pictures (safe to delete)
- `cache/discs/`: the temporary copy of the song being ripped, and the converted songs of a music CD being burned (empty unless a rip or a burn is running or was cut short)
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
pytest                       # unit + API tests (~1 min); browser tests skip if Playwright is absent
pytest tests/e2e             # the UI in real Chromium: playback, queue, tools, shortcuts, radio and podcasts (against fake stations, feeds and directories on this computer), and the accessibility checks (axe-core, keyboard)
python scripts/smoke_frozen.py dist/mtk-backend.exe tests/fixtures   # the frozen backend: first run, scan, organize, duplicates, a fake station and feed
python scripts/perf_synthetic.py                                    # time every screen on 100,000 invented songs and 300,000 plays
cd desktop && npm test       # the desktop shell's logic: updates, tray menu, close button, window position, Open with arguments
```

The helper for phones and players without a drive letter is a small C# program
([`helpers/mtp`](helpers/mtp), .NET Framework 4.8, built with the .NET SDK; Windows only at run time).
The tests use a stand-in that keeps its "phones" in plain folders (`tests/fake_mtp_helper.py`), so they run
anywhere:

```bash
dotnet build helpers/mtp/MtkMtp.csproj -c Release -o helpers/mtp/out        # Windows: the real helper
python scripts/check_mtp_helper.py helpers/mtp/out/mtk-mtp.exe              # starts it and checks the protocol (Windows)
```

The helper for CDs and DVDs works the same way ([`helpers/disc`](helpers/disc)): the tests use a stand-in drive that keeps its discs in plain folders
(`tests/fake_disc_helper.py`, with bad sectors, a drive cache that repeats itself, a disc taken out half way and so on), and a stand-in
MusicBrainz and Cover Art Archive (`tests/fake_musicbrainz.py`).

```bash
dotnet build helpers/disc/MtkDisc.csproj -c Release -o helpers/disc/out    # Windows: the real helper
python scripts/check_disc_helper.py helpers/disc/out/mtk-disc.exe          # protocol, refusals, stopping, a mounted disc image, and the image builder (Windows)
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
