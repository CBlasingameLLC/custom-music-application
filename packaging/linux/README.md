# Music Toolkit as a server (Raspberry Pi OS, Debian, Ubuntu)

Runs the same app as the Windows desktop one, without a window, so that every device at home (and, over a private network, away from
home) uses the library that lives on this computer. It is written for a Raspberry Pi 5 with an external drive, but any Linux computer
with Python 3.11 or newer will do (x86-64 and arm64 are both tested in CI).

**What it is not yet**: there are no accounts. One token signs a device in, and anyone who has it can use everything in the app,
including moving and deleting files. Reach it only from your own network or a private network such as Tailscale, and **never forward a
port from the internet to it** (the server refuses a request that came through Tailscale Funnel, which is that). There are no accounts of its
own: whoever is signed in can do everything. A device is signed in by the token, or, behind `tailscale serve`, because Tailscale says it belongs to an account
you listed (see *Sign in with Tailscale*).

## What goes where

| What | Where |
|---|---|
| The program | `/opt/musictoolkit/venv` |
| Its data (config, database, logs, caches, the token file) | `/var/lib/musictoolkit` (owner `mtk`). Keep it on the system disk, not on the music drive |
| The music drive | mounted at `/srv/music`; the library is `/srv/music/library`; copy new music into `/srv/music/inbox` and let Organize files put it in place; conversions go to `/srv/music/exports` (never inside the library) |
| The account it runs as | `mtk` (no login), in the group `music`, which the share that copies music over also uses |

## Install

```bash
sudo apt install -y python3-venv git
git clone --depth 1 --branch v0.13.0 https://github.com/CBlasingameLLC/custom-music-application.git
cd custom-music-application
sudo groupadd -f music
sudo useradd --system --home-dir /var/lib/musictoolkit --shell /usr/sbin/nologin --groups music mtk
sudo install -d -o mtk -g mtk -m 0750 /var/lib/musictoolkit
sudo python3 -m venv /opt/musictoolkit/venv
sudo /opt/musictoolkit/venv/bin/pip install .
```

On a Raspberry Pi every dependency installs from a ready-made arm64 wheel (nothing is compiled), on Python 3.11 (Raspberry Pi OS
"Bookworm") and 3.13 ("Trixie").

The drive: format it ext4 (`sudo mkfs.ext4 -L music -m 0 /dev/sdX1`, which **erases it**), and mount it by label at boot:

```
LABEL=music /srv/music ext4 defaults,noatime,nofail,x-systemd.device-timeout=30 0 2
```

Make the mount's top folder `root:music` with mode `2775`, and create `library`, `inbox` and `exports` in it, group `music`, mode `2775`.
(The first one is so that the program can make the per-drive trash folder the Linux "move to trash" uses; the others so that both the
program and whoever copies music over can write.)

## Configure

Copy `packaging/linux/config.example.toml` to `/var/lib/musictoolkit/config.toml` (`sudo install -o mtk -g mtk -m 0640 ...`) and edit it. The part that matters:

```toml
[server]
host = "127.0.0.1"
port = 4533
allowed_hosts = ["musicpi.local"]
```

- `host`: `"127.0.0.1"` when a program on this computer (Tailscale Serve) passes requests on to the app, `"0.0.0.0"` to listen for other
  devices directly. The server **refuses to start** on any address but this computer's own without `allowed_hosts`.
- `allowed_hosts`: every name the devices reach it by, written exactly: `musicpi.local`, the Tailscale name from `tailscale status`
  (`musicpi.tail1234.ts.net`), an IP address. No wildcards, no ports. A request that names anything else gets `403 forbidden host`
  (this is what stops a web page on another site from using the app through your browser).
- `rescan_hours`: look for new songs by itself this often (0: only when the server starts, and when you press Refresh).
- `login_days`: how long a device that opened the sign-in link stays signed in (90).
- `tailscale_logins`: the Tailscale accounts that are signed in without the token (see *Sign in with Tailscale*). Empty: only the token signs a device in. It needs
  `host = "127.0.0.1"`, and the server refuses to start with it on any other address.

## Run it as a service

```bash
sudo cp packaging/linux/musictoolkit.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now musictoolkit
curl -s http://127.0.0.1:4533/healthz        # {"status":"ok","version":"…","library":{"folders":1,"available":1}}
journalctl -u musictoolkit -f                # what it says; the app's own log is /var/lib/musictoolkit/logs/musictoolkit.log
```

`/healthz` needs no token and tells nothing but the version and whether the library folders are there (`"status":"degraded"` when the
drive is not mounted): point a monitor at it.

**The token.** At its first start the server makes a random token and keeps it in `/var/lib/musictoolkit/server.token` (readable by `mtk`
only); it is never printed in a log. Show the link that signs a device in:

```bash
sudo -u mtk /opt/musictoolkit/venv/bin/mtk --config /var/lib/musictoolkit/config.toml token --url https://musicpi.tail1234.ts.net
```

Open that link once on each device; it keeps the device signed in for `login_days` (90 by default), and the page then takes the
token out of the address, so a bookmark of the page is just the page. A device that has only the address (or whose cookie ended) is shown a page with a
box for the token: paste what `mtk token` prints there. Anyone who has the link or the token has the app, so do not post either anywhere.
Eight wrong tokens in a minute make the server refuse every token for the rest of the minute (a device that is already signed in is not
affected). If you would rather choose the token yourself, put `MTK_TOKEN=<openssl rand -hex 32>` in
`/etc/musictoolkit.env` (mode 0600, owned by root): it wins over the file, and the link is then
`https://NAME/?token=<that value>` (`mtk token` shows only the token file's).

**If the token was ever shown where others could read it** (a log, a screen shared in a call, a chat), treat it as spent. Make a new one:

```bash
openssl rand -hex 32                                   # a new token: copy it
sudoedit /etc/musictoolkit.env                         # MTK_TOKEN=<the new token>   (or, without MTK_TOKEN: sudo rm /var/lib/musictoolkit/server.token)
sudo systemctl restart musictoolkit                    # every device is signed out; open the new link on each
sudo journalctl --rotate && sudo journalctl --vacuum-time=1s   # forget the old token in the journal (this clears the older journal of the whole computer, not only this service's)
```

A monitor should ask `/healthz` (it needs no token); `/` answers `401` to a device that is not signed in, `HEAD` requests are answered like `GET`.

## A server that runs `mtk dashboard` (0.9.0 and earlier)

`mtk dashboard` is the desktop app's server: it listens on this computer only, makes the token it is given in `MTK_TOKEN`, and (up to 0.10.0) **printed the
link that signs in, with the token in it, every time it started**, so a service that ran it wrote the token into the journal. `mtk serve` never
prints it, and from 0.10.1 `dashboard` prints the link only when its output is a terminal. To move such a service over:

1. Update the program to the release (`git fetch --tags && git checkout v0.13.0` in the folder it was installed from, then `pip install --upgrade .` with the service's
   virtual environment).
2. In the unit file (`systemctl cat musictoolkit`), end the command with `serve` instead of `dashboard --port 4533`, keep `EnvironmentFile=` (the `MTK_TOKEN` in it is
   used as it is), and put in `config.toml` the address and the names the devices reach it by, written exactly:
   `[server]` `host = "127.0.0.1"`, `port = 4533`, `allowed_hosts = ["musicpi.tail1234.ts.net"]`.
3. `sudo systemctl daemon-reload && sudo systemctl restart musictoolkit`, and make a new token and clear the journal as above: the old token is in it.

## From outside the house: Tailscale

[Tailscale](https://tailscale.com) makes a private network between your devices (the free plan is for personal use). On the server:

```bash
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
sudo tailscale serve --bg 4533        # the syntax differs between versions: `tailscale serve --help`
```

Turn on *HTTPS certificates* and *MagicDNS* in the Tailscale admin page, add the machine's name to `allowed_hosts`, keep `host =
"127.0.0.1"`, and restart the service. The page is then `https://musicpi.<your tailnet>.ts.net`, with a real certificate (a browser
needs one to install the app on a phone's home screen). Tailscale publishes the machine's name, not anything it serves, in public
certificate logs. The cookie that keeps a device signed in is marked Secure when the proxy says (`X-Forwarded-Proto: https`) that the page came over https; check it
in the browser's developer tools if you care.

## Sign in with Tailscale (no token on any of your devices)

With `tailscale serve` in front of the server, Tailscale tells it which of your accounts each request came from, in the header `Tailscale-User-Login` (it deletes any
such header that a client sent, and writes none for a request from a tagged device or from Funnel). A server that is told to believe it signs that account in with
no token: your phone and your Mac open the app and it is simply there.

1. Have the HTTPS address working (the section above) and `host = "127.0.0.1"` in `[server]`, with the Tailscale name in `allowed_hosts`.
2. On a device of yours, open the server's `https://NAME.<your tailnet>.ts.net` address in a browser. The page that asks for the token now also says
   *Tailscale says this device belongs to you@example.com* and shows the line to add. (Without that page: the account is the one you sign in to Tailscale with.)
3. Add the line to the `[server]` section of `config.toml`, and restart the service:

   ```toml
   tailscale_logins = ["you@example.com"]
   ```
   ```bash
   sudo systemctl restart musictoolkit
   ```
4. Open the address again: no token is asked for. Settings, *How this device signs in*, says *Tailscale signs this device in as you@example.com*. Do the same on every
   device (nothing to type on them: each is in as soon as it is on your tailnet).

What to know:

- **Every listed account can do everything**, like the token. List your own accounts only. A person you *share* the Pi with in Tailscale is another account and is not signed
  in unless you list it (and is then signed in fully).
- **Taking an account off the list signs its devices out** on their next request, and so does changing the token (the devices that Tailscale still vouches for sign in again by
  themselves, with a new cookie). The cookie a device gets is made from the token and the account, not the token itself.
- **A tagged device** (a server of yours that has a Tailscale tag, not a person) carries no account: it signs in with the token. So does anything that reaches the server
  by another address than the `tailscale serve` one (its local name, its IP address): there Tailscale says nothing.
- **A page of another site cannot use it.** A browser says (`Sec-Fetch-Site`) whether the app's own page or another site's made a request, and only the app's own
  page, or you typing or tapping the address, is signed in. A browser too old to say (Safari before 16.4) is not signed in this way; it uses the token. A program that
  is not a browser (a phone app that speaks the Subsonic protocol, later) is signed in by the account.
- **The server must listen on this computer only** (`host = "127.0.0.1"`), because whatever can reach it directly could write the header and be anyone. For the same
  reason keep other people's programs and accounts off the Pi: anything on it that can open `127.0.0.1:4533` can say who it is.
- **Funnel is refused**: a request that came through `tailscale funnel` (the public internet) gets `403 this server is not available through Tailscale Funnel`, whatever it
  asks for. If you see that, Funnel is on for this port: turn it off (`sudo tailscale serve reset`, then `sudo tailscale serve --bg 4533` again).
- **Use `tailscale serve` as the proxy, and no other** in front of the app on this computer for this. A proxy that passes on every header a client sends (most do) would
  pass on a `Tailscale-User-Login` that the client wrote, and the client would be whoever it said. Tailscale deletes such headers; another proxy has to be told to.
- The log says how many accounts are signed in this way, never which.

## On the phone and on the Mac

The app is a web page that can be installed: it opens from an icon of its own, with no browser around it. It needs the `https` address above (a phone
gives a Home Screen app its own worker only over `https`; over plain `http` the page can still be put on the Home Screen, but it opens like a bookmark).

**iPhone or iPad**

1. Put the Tailscale app on the phone and sign in to the account the server is on. Leave it connected: it uses next to nothing when nothing is sent.
2. In Safari open `https://musicpi.<your tailnet>.ts.net`. If you did *Sign in with Tailscale*, it opens signed in. Otherwise the page asks for the token, once
   (`mtk token` shows it; copy it on a Mac or another iPhone and paste it, the clipboard is shared), or open the link
   `mtk token --url https://musicpi.<your tailnet>.ts.net` prints.
3. Press Share, then **Add to Home Screen**. From iOS 17 Safari copies the site's cookies to the new app, so it opens signed in; if it asks for the token anyway,
   paste it once more (the app keeps its cookies apart from Safari's). It then stays signed in for `login_days`. With *Sign in with Tailscale* the app signs
   itself in whenever it is opened (there is nothing to copy, and no `login_days` to run out).
4. Open **Music Kit** from the Home Screen.

What an iPhone does and does not do:

- The songs play with the screen locked. The lock screen shows the song, its cover and the buttons for play and pause, previous and next (an episode has skip back 15 seconds and
  forward 30 instead). The page plays through the plain audio element, not through Web Audio, which Safari stops when the screen locks.
- The volume is the phone's own buttons, and the equalizer, the loudness levelling (ReplayGain), the crossfade and the gapless join are not there: they are made of Web Audio. The
  next song is loaded when a song ends, so there is a short gap (a fraction of a second on a good connection).
- What cannot be promised from here: how long iOS lets a Home Screen app keep running with the screen locked. If the music stops between two songs after the phone has
  been locked for a long time, that is iOS stopping the page, and an iPhone app that speaks the Subsonic protocol (next section) is the way to be sure.
- The phone is away from the server, so everything is streamed: on mobile data a song is its whole file (transcoding to a smaller one is planned).

**Mac.** Safari (macOS Sonoma or later): *File, Add to Dock*. Chrome and Edge: the menu, then *Install*. It opens in a window of its own and answers the media keys.

## An app that speaks Subsonic (iPhone, iPad, Mac)

The server also answers the Subsonic / OpenSubsonic protocol at `/rest`, which a whole family of music apps speaks: on an iPhone, for example, Amperfy, play:Sub,
Substreamer and Narjo; on a Mac, Supersonic and Feishin. A native app is the sure way to have the music go on with the screen locked, and some bring CarPlay and
downloads for offline listening. **None of these apps has been tried against this server yet** (the tests use a client library of the protocol and the schemas of the
OpenSubsonic project): if one of them does not work, the app and what it says is what is wanted.

In the app's settings for a server:

| Box | What to write |
|---|---|
| Server address | `https://musicpi.<your tailnet>.ts.net` (the same address as for the web page: with `https`, with no port and nothing after the name). The phone needs Tailscale on |
| User name | Anything you like (`me`). The server has no users; it is shown as the name of the person who listens |
| Password | The token (`mtk token` shows it). Some apps have an *API key* box instead: the token goes there, and then the user name is left empty |
| Salted password, "legacy" or "token" authentication | Either works. Leave the app's own choice alone |

With *Sign in with Tailscale* (above), a phone that is on the tailnet is let in without the token: the app still wants something in the password box, and anything will do. A
wrong password is only counted when the server needs the password.

What an app can do: browse by artist, album, genre and year and by the lists (newest, most played, recently played, random, starred); search; play a song (the original file,
and seeking inside it); show covers (in the size it asks for) and lyrics (from the file, a `.lrc` beside it, or lrclib.net if you turned that on in Settings); star and
rate songs (a star is a favorite, in the web page as well); count a listen (it goes into the history, and to ListenBrainz if that is on, once even if the app sends it twice with the same time on it);
make, change and delete playlists (a smart playlist can be played, and not changed from an app); list the radio stations kept in the app; and ask for a scan.

What an app cannot: ask for a smaller file (`maxBitRate` and `format` are ignored, so on mobile data a song is the whole file); keep a play queue that follows you from one device to
another, bookmarks, podcasts, sharing; rate an album (a rating belongs to a song); read an artist's biography (there is none). A Subsonic player that is a web page of another site cannot use the server
(it answers no other site: that is what keeps a page you visit from using your library).

The ids an app shows are the server's own and mean nothing outside it.

## Move the library over from the Windows PC

1. Copy the music with its dates kept (the next scan then finds every song unchanged and reads no tags). The folder is wherever the Windows app has
   its library: often a folder of the user's own (`C:\Users\you\Music`), sometimes another drive (`D:\Music`); Settings, Music folders, shows it:
   `robocopy C:\Users\you\Music \\musicpi\music\library /E /COPY:DAT /DCOPY:DAT /R:2 /W:5 /MT:8`
2. Stop the Windows app, copy `%USERPROFILE%\.musictoolkit\data\library.db` to `/var/lib/musictoolkit/data/library.db` (owner `mtk`),
   and stop the service on the server.
3. Tell the database where the music is now (a dry run first; it only says what it would do):

   ```bash
   sudo -u mtk /opt/musictoolkit/venv/bin/mtk --config /var/lib/musictoolkit/config.toml library reroot --from 'C:\Users\you\Music' --to /srv/music/library
   sudo -u mtk /opt/musictoolkit/venv/bin/mtk --config /var/lib/musictoolkit/config.toml library reroot --from 'C:\Users\you\Music' --to /srv/music/library --apply --update-config
   ```

   `--from` has to be the folder as the database has it. Capitals and slashes do not matter, but the folder does: if no song is in it the command says so and prints
   `the database has its songs in: ...` with the folders that do hold them. Every song keeps its number, rating, plays and playlists; a copy of the database as it was is
   kept next to it.
4. Start the service. The first scan reports the songs as *Unchanged* when their size and time were kept; a song whose time was not is
   read again and keeps everything else.

## When something is wrong

| You see | It means |
|---|---|
| `403 forbidden host` | The name in the address is not in `allowed_hosts`. Add it (exactly, without the port), restart |
| `403 this server is not available through Tailscale Funnel` | The request came through Funnel (the public internet). Turn Funnel off for this port: `sudo tailscale serve reset`, then `sudo tailscale serve --bg 4533` |
| `Cannot start the server: [server] tailscale_logins needs host = "127.0.0.1"` | The list is set and the server would listen on another address, where a program on the network could write the header and be anyone. Listen on this computer only and reach it through `tailscale serve`, or empty the list |
| The page asks for the token although the account is listed | Tailscale is not naming the account in this visit. Check: the address is the `https://....ts.net` one (not the local name or an IP); the device is not a tagged one; the page was not opened from a link on another site (open the address itself); the browser is not older than Safari 16.4; the service was restarted after the line was added. The page itself says *Tailscale says this device belongs to ...* when it sees an account, and nothing when it does not |
| The sign-in page says *Tailscale says this device belongs to X. This server does not sign in that account* | X is not on `tailscale_logins`. Add it exactly as shown (capitals do not matter), restart |
| `401 unauthorized`, or the page with the box for the token | The device is not signed in: open the link from `mtk token --url ...`, or paste the token (`mtk token`) into the box |
| "Too many wrong tokens. Try again in N seconds." (`429`) | Eight wrong tokens came in within a minute (a typo, a program with an old token). Wait; devices that are signed in are not affected |
| A page that says **Can't reach your music server**, with a *Try again* button | The phone or Mac got no answer from the server within eight seconds (the installed app shows this instead of the browser's own error). Tailscale is off on the phone, the phone is on a different account, or the server is off: check each, then press *Try again* |
| The app on the Home Screen asks for the token | Its cookies are not Safari's: before iOS 17 they are not copied when the app is added, and a cookie that was cleared is gone. Paste the token once; the app then stays signed in for `login_days` |
| A Subsonic app says *Wrong username or password* | The password is the token (`mtk token`), or the API key box is the token with the user name empty. Not the Tailscale login |
| A Subsonic app says *Too many wrong passwords. Try again in N seconds.* | Eight wrong ones came in within a minute (an app trying an old token again and again). The same limit as for the web page's sign-in. Wait, then fix the app's password |
| A Subsonic app says *Server must upgrade* or *Client must upgrade* | The app speaks a version of the protocol that is not 1.x, or newer than 1.16. Tell me which app it is |
| A Subsonic app shows no covers or no artists | Covers come from the pictures in the files and from `cover.jpg`/`folder.jpg` beside them; run a scan (Settings, *Scan for new music*, or the app's own *scan* button) so the library knows the files. A file that is only tags is left out of every list |
| A song with a **No audio** mark | The file is only tags (an MP3 with no audio frames, or an empty file): nothing to play. It is left out of queues, mixes and what is copied to a device. Filter on "Has tags but no audio" to list them; the scan counts them apart from errors |
| `Could not read the audio in ...` in the log | The file has more than tags but its audio could not be read (damage). One line, no traceback; the song stays in the library |
| `Cannot start the server: it would listen on 0.0.0.0 ... allowed_hosts is empty` | Listening beyond this computer needs the names; see Configure |
| `/healthz` says `degraded`; "The library's drive is not available right now" | The music drive is not mounted. Songs are not forgotten: they play again when it is back |
| A scan reports `1 folder(s) not found` | The same thing; the scan skips a folder that is not there or is empty while the library has songs in it |

## Update

```bash
cd custom-music-application && git fetch --tags && git checkout vX.Y.Z      # the version you want
sudo systemctl stop musictoolkit
sudo /opt/musictoolkit/venv/bin/pip install --upgrade .
sudo systemctl start musictoolkit
```

The database is brought up to date by the new version when it starts. Back it up first from the app (Settings, Back up database).
