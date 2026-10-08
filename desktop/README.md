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
4. Closing the window quits the app and kills the whole backend process tree
   (`taskkill /T` on Windows, because a PyInstaller onefile exe is a bootloader
   plus a child). A single-instance lock stops a second launch from running a
   second backend on the same database.

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

The window cannot navigate away from the app or open new windows; `http(s)`
links go to the default browser instead. Media keys are registered as global
shortcuts, and Chromium's own media-key handling is switched off so the two
cannot both fire.

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

4. mkdir desktop\resources
   copy dist\mtk-backend.exe desktop\resources\

5. cd desktop
   npm ci

6. npm start        (dev mode: runs your venv's Python directly, not the
                      frozen exe, so there is no rebuild loop. MTK_PYTHON
                      overrides which interpreter it uses.)

7. npm run package  -> desktop\release\MusicToolkit-Setup-<version>.exe

8. Install it and drive the installed app's window:
     pip install playwright
     python scripts\smoke_desktop.py --music tests\fixtures --skip-external -- "<install dir>\Music Toolkit.exe"
   (the installer accepts /S /D=<dir> for a silent install). This is what CI
   does on every build. By hand, confirm a Desktop shortcut and Start Menu
   entry appear, no console window shows, and after closing the app
   mtk-backend.exe is not left in Task Manager.
```

`scripts/smoke_desktop.py` works with any command that launches the app, so
the same checks run against the dev shell: `python scripts/smoke_desktop.py
--music tests/fixtures --cwd desktop -- npx electron .` (on Linux CI-style
runs, put `xvfb-run -a` and `--no-sandbox` in front/behind as needed).

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

The window is the whole app: browsing, searching and playing the library,
playlists and filters, listening history, recommendations, folders and settings.
Tag enrichment, organizing, duplicate review, device sync, Spotify import and
scrobbling are still command-line operations (the same bundled
`mtk-backend.exe <command>`, or your dev venv's `mtk`) until they get their own
screens in 0.3.0.
