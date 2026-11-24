# Music Toolkit as a server (Raspberry Pi OS, Debian, Ubuntu)

Runs the same app as the Windows desktop one, without a window, so that every device at home (and, over a private network, away from
home) uses the library that lives on this computer. It is written for a Raspberry Pi 5 with an external drive, but any Linux computer
with Python 3.11 or newer will do (x86-64 and arm64 are both tested in CI).

**What it is not yet**: there are no accounts. One token signs a device in, and anyone who has it can use everything in the app,
including moving and deleting files. Reach it only from your own network or a private network such as Tailscale, and **never forward a
port from the internet to it**. Accounts and roles are the next round (0.11).

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
git clone --depth 1 --branch v0.10.1 https://github.com/CBlasingameLLC/custom-music-application.git
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

Open that link once on each device; it keeps the device signed in for `login_days` (90 by default) and leaves the address without the
token in it, so a bookmark of the page is just the page. A device that has only the address (or whose cookie ended) is shown a page with a
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

1. Update the program to the release (`git fetch --tags && git checkout v0.10.1` in the folder it was installed from, then `pip install --upgrade .` with the service's
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
| `401 unauthorized`, or the page with the box for the token | The device is not signed in: open the link from `mtk token --url ...`, or paste the token (`mtk token`) into the box |
| "Too many wrong tokens. Try again in N seconds." (`429`) | Eight wrong tokens came in within a minute (a typo, a program with an old token). Wait; devices that are signed in are not affected |
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
