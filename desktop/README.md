# Music Toolkit: desktop shell

A small Electron shell around the Python backend. There is no React/Vite
renderer here: the window is pointed at the web UI the backend already serves
(`src/musictoolkit/web/static/`, plain ES modules, no build step), so the only
JavaScript to build is the main process itself (`src/main/`).

## How it runs

1. `backend.js` asks the OS for a free port, generates a random per-launch
   **token**, and spawns the backend (in dev, your venv's
   `python -m musictoolkit.cli dashboard`; packaged, the bundled
   `mtk-backend.exe`) with the token in its environment (`MTK_TOKEN`, never on
   the command line).
2. It polls `/?token=...` until the backend answers HTTP 200: server up, UI
   files found, token accepted. If the process exits first, or never becomes
   healthy, an error dialog names the log file instead of showing a blank window.
3. `index.js` opens the window on that URL, which signs it in (an HttpOnly,
   same-site cookie). If the backend later dies, a dialog says so and the app
   quits.
4. Quitting the app kills the whole backend process tree (`taskkill /T` on
   Windows, because a PyInstaller onefile exe is a bootloader plus a child).
   What the window's close button does is a setting (see *The tray and the close
   button*): by default it quits, unless music is playing. A single-instance
   lock stops a second launch from running a second backend on the same
   database; a second launch that names songs hands them to the first.

Backend output goes to `%USERPROFILE%\.musictoolkit\logs\backend.log`, next to
the app's own `musictoolkit.log`.

### The bridge (`preload.js`)

The page sees one object, `window.mtk`, and nothing else from Node/Electron
(`contextIsolation`, `sandbox`, no `nodeIntegration`). Every call is validated
in the main process and ignored unless it comes from the backend's own origin.

| Call | Does |
|---|---|
| `selectFolder()` / `selectFile({filters})` | Native pickers; resolve to a path or `null` |
| `showItemInFolder(path)` | Reveal a file in Explorer |
| `openPath(dir)` | Open a **folder** (never a file: the page cannot launch programs) |
| `openExternal(url)` | Open an `http(s)` link in the default browser |
| `onMediaKey(fn)` | Play/pause, next, previous, stop from the keyboard's media keys; returns an unsubscribe function |
| `setNowPlaying({title, artist, playing})` | Tell the shell what is playing: the tray's tooltip and menu, and whether closing the window keeps the music going |
| `setCloseAction(action)` | `quit`, `tray_if_playing` or `tray`; keeps the shell in step with the `app.close_action` setting |
| `takeOpenFiles()` / `onOpenFiles(fn)` | Paths of songs and folders the app was opened with: the ones that arrived before the page was ready, then each new batch |
| `pathForFile(file)` | The path of a file dropped on the window (Electron's `webUtils`), or `null` |
| `desktopInfo()` | `{tray, closeAction, hidden}`: whether a tray icon exists, for Diagnostics and the tests |
| `quit()` | Quit the app (the Quit buttons in the tray menu and in Settings) |
| `update.status()` / `update.check()` | The updater's state (`idle`, `checking`, `up-to-date`, `downloading`, `ready`, `error`, `disabled`) and a manual check |
| `update.install()` / `update.setAuto(on)` | Restart into a downloaded update; switch the automatic checks on or off |
| `update.onStatus(fn)` | Be told whenever the updater's state changes; returns an unsubscribe function |

The window cannot navigate away from the app or open new windows; `http(s)`
links go to the default browser instead. `quit()` and the other calls that act
on the app are answered only for the backend's own page: a foreign page that
somehow got the bridge cannot quit the app (`smoke_desktop.py` checks this). Media keys are registered as global
shortcuts, and Chromium's own media-key handling is switched off so the two
cannot both fire.

## The tray and the close button

The app puts an icon by the clock (`tray.png`, with `tray@2x.png` for sharp
screens, drawn from the same design as `build/icon.png`). Clicking it shows or
hides the window; its menu has Show/Hide, Play/Pause, Next, Previous and Quit,
and its tooltip names what is playing. A computer with no notification area simply
has no tray: the icon is created inside a `try`, and without it the close button always quits, so the
window can never be hidden with no way back.

What the close button does is `app.close_action` in the backend's config
(Settings, Window and tray): `quit`; `tray_if_playing` (the default: music keeps playing from the tray, and
closing with nothing playing quits); or `tray`. The main process keeps a copy
(read at start-up, then updated by `setCloseAction`) because it has to decide while the window is
closing. The first time closing leaves the app running, a one-time notice says so. The window
is created with `backgroundThrottling: false`, so the gapless/crossfade clock keeps running while it is hidden.

The pure parts are in `close-action.js` and `tray-menu.js` and are tested by `npm test`.

## The window remembers where it was

`window-state.js` saves the size, place and maximized state to `window-state.json` in the app's user-data
folder (shortly after the last move or resize, and when the window closes; written to a temporary file first
so a crash cannot leave half a file). On the next start the saved place is used if enough of the window
would be on a display that still exists; if the monitor it was on has been unplugged only the size is kept and
the system places the window. A saved place that hangs over a screen edge is moved back inside. The logic takes
the displays as plain rectangles and is tested without a screen.

## Songs opened with the app

The installer registers Music Toolkit for mp3, flac, m4a, aac, ogg, oga, opus and wav files (the formats the
player decodes), so it appears in Explorer's *Open with* list; Windows keeps your current default
player unless you choose this one. A song named on the command line (or dragged onto the program, or opened with
the app while it is running: a second launch hands its arguments to the first and exits) is collected by
`launch.js` (`filesFromArgv`: switches skipped, the app folder skipped when run as `electron .`, relative paths
resolved, only audio files and folders kept) and passed to the page. The page sends the paths to
`POST /api/library/open-files`, which adds the songs the library does not have and answers them as tracks, and
plays them. Files and folders dropped on the window go the same way (`pathForFile`).

## Updates

`updater.js` wraps `electron-updater` with the GitHub provider (the repository is
public, so no token is involved). Shortly after start and every six hours it asks
GitHub for the latest release's `latest.yml`; a newer version is downloaded in
the background (only the changed blocks when the `.blockmap` allows) and installed
silently when the app quits, or at once on `update.install()`. The switch for
automatic checks is the `auto_update` setting of the Python backend, which the
main process reads at start-up. Only a packaged build updates; `npm start` reports
`disabled`. The logic is plain Node with the updater and timers injected, tested by
`npm test` (`test/updater.test.js`). Update activity is logged to
`~/.musictoolkit/logs/updater.log`.

Because the installer is unsigned there is nothing to verify beyond the checksum in
`latest.yml`, which electron-updater checks against the downloaded file.

## Building the Windows installer

This must run on Windows: PyInstaller produces platform-native binaries and
electron-builder's NSIS target needs Windows. From the repo root:

```
1. python -m venv .venv
   .venv\Scripts\activate
   pip install -e ".[dev,packaging]"

2. pyinstaller packaging\mtk_backend.spec
   -> dist\mtk-backend.exe

3. Smoke-test the frozen exe alone BEFORE involving Electron. This isolates
   PyInstaller problems from Electron ones:
     python scripts\smoke_frozen.py dist\mtk-backend.exe tests\fixtures
   It starts the exe on a throwaway profile and checks the bundled UI, the
   token gate, a scan, cover art, Range streaming and a backup. Give it a few
   seconds: onefile builds self-extract on every launch.

4. The helpers (they need the .NET SDK): one for phones and players without a
   drive letter, one for CDs and DVDs:
     dotnet build helpers\mtp\MtkMtp.csproj -c Release -o helpers\mtp\out
     python scripts\check_mtp_helper.py helpers\mtp\out\mtk-mtp.exe
     dotnet build helpers\disc\MtkDisc.csproj -c Release -o helpers\disc\out
     python scripts\check_disc_helper.py helpers\disc\out\mtk-disc.exe
   Then stage them next to the backend:
     mkdir desktop\resources
     copy dist\mtk-backend.exe desktop\resources\
     xcopy /E /I helpers\mtp\out desktop\resources\mtk-mtp
     xcopy /E /I helpers\disc\out desktop\resources\mtk-disc

5. cd desktop
   npm ci

6. npm start        (dev mode: runs your venv's Python directly, not the
                      frozen exe, so there is no rebuild loop. MTK_PYTHON
                      overrides which interpreter it uses.)

7. npm run package  -> desktop\release\MusicToolkit-Setup-<version>.exe

8. Install it and drive the installed app's window:
     pip install playwright
     python scripts\smoke_desktop.py --music tests\fixtures --skip-external --expect-tray --expect-mtp-helper -- "<install dir>\Music Toolkit.exe"
   (the installer accepts /S /D=<dir> for a silent install). This is what CI
   does on every build. By hand, confirm a Desktop shortcut and Start Menu
   entry appear, no console window shows, and after closing the app
   mtk-backend.exe is not left in Task Manager.
```

`scripts/smoke_desktop.py` works with any command that launches the app, so
the same checks run against the dev shell: `python scripts/smoke_desktop.py
--music tests/fixtures --cwd desktop -- npx electron .` (on Linux CI-style
runs, put `xvfb-run -a` and `--no-sandbox` in front/behind as needed). Besides the
window, the bridge and playback it checks that the window opens where it was left, that a second launch
hands a song to the running app, that closing the window while music plays leaves the app in
the tray until it is told to quit (the close button is pressed for real: PowerShell's `CloseMainWindow` on Windows,
`python-xlib` under X; without either that part is skipped), that quitting takes the backend down, and that a
song named on the command line plays.

**Expect to iterate at step 3.** A missing PyInstaller hidden import shows up
as a traceback naming the module when you run the frozen exe, not as a build
error. Add it to `hiddenimports` in `packaging/mtk_backend.spec` and rebuild.
Three warnings during the PyInstaller build are expected and harmless:
`Hidden import "jinja2" not found` (FastAPI's optional template support, which
this project does not use) and `Library user32/msvcrt required via ctypes not
found` if you build on a non-Windows machine (the device-detection code
references Windows DLLs, which only resolve on a real Windows build).

Windows SmartScreen will flag both the exe and the installer as "unrecognized
publisher" on first run: neither is code-signed. That is expected for an
unsigned personal build, not a sign that anything is broken.

## Scope

The window is the whole app: browsing, searching and playing the library (gapless, with crossfade),
playlists and filters, listening statistics and mixes, listening history, recommendations and new
releases, tag enrichment, organizing and duplicate review, device sync, Spotify import and scrobbling, folders and settings.
The command line (`mtk`) still exists for scripting, but nothing needs it.
