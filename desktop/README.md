# Music Toolkit — desktop

A minimal Electron shell around the existing Python backend. Unlike a
typical Electron app, there's no React/Vite renderer here — the "renderer"
is just this window pointed at the dashboard the backend already serves
(`src/musictoolkit/dashboard/app.py`), so there's nothing to build on the
JS side beyond the main process itself.

On launch, the main process spawns the Python backend (in dev, your own
venv's `python -m musictoolkit.cli dashboard`; in a packaged build, the
bundled `mtk-backend.exe`) on a free localhost port chosen by the OS, waits
for it to answer successfully, then opens a window pointed at it. On quit,
the whole backend process tree is killed (on Windows via `taskkill /T`,
since a PyInstaller onefile exe is a bootloader plus a child process). A
single-instance lock prevents a second launch from running a second backend
against the same database.

The backend's stdout/stderr go to `%USERPROFILE%\.musictoolkit\logs\backend.log`
(uvicorn tracebacks, startup errors), next to the app's own
`musictoolkit.log`. If the backend exits or never becomes healthy, the app
shows an error dialog naming that file instead of an empty window.

## Building the Windows installer

This must run on Windows — PyInstaller produces platform-native binaries
and electron-builder's NSIS target needs Windows to build. From the repo
root:

```
1. python -m venv .venv
   .venv\Scripts\activate
   pip install -e ".[dev,packaging]"

2. pyinstaller packaging\mtk_backend.spec
   -> dist\mtk-backend.exe

3. Smoke-test the frozen exe alone BEFORE involving Electron — this
   isolates PyInstaller issues from Electron ones:
     dist\mtk-backend.exe --help
     dist\mtk-backend.exe dashboard --port 4533
     -> open http://127.0.0.1:4533 in a normal browser, confirm the
        dashboard loads. Give it a few seconds — PyInstaller onefile
        builds self-extract on every launch (~4s cold-start observed
        during development is normal, not a hang).

4. mkdir desktop\resources
   copy dist\mtk-backend.exe desktop\resources\

5. cd desktop
   npm install

6. npm start        (dev mode — runs your venv's Python directly, not
                      the frozen exe, so there's no rebuild loop while
                      iterating)

7. npm run package  -> desktop\release\Music Toolkit Setup <version>.exe

8. Run that installer. Confirm: a Desktop shortcut and Start Menu entry
   appear, launching shows no visible console window, and the dashboard
   loads in the app window.

9. Close the app, check Task Manager — mtk-backend.exe should not still
   be running.
```

**Expect to iterate at step 3.** A missing PyInstaller hidden-import
surfaces as a traceback naming the missing module when you actually run
the frozen exe — not as a build-time error. Add the named module to
`hiddenimports` in `packaging/mtk_backend.spec` and rebuild. Two warnings
during the PyInstaller build itself are expected and harmless, not bugs:
`Hidden import "jinja2" not found` (FastAPI's optional Jinja2Templates
integration, which this project doesn't use — the dashboard renders plain
f-string HTML on purpose) and `Library user32/msvcrt required via ctypes
not found` if you ever build on non-Windows first (the device-detection
code references these Windows DLLs via `ctypes`, which only resolve on an
actual Windows build).

Windows SmartScreen will likely flag both the frozen exe and the installer
as "unrecognized publisher" on first run, since neither is code-signed.
That's expected for an unsigned personal build, not a sign anything is
broken.

## Scope

This packages what the dashboard does: browsing/searching the library,
scanning a folder into it (the **Scan** page; read-only, it never changes
your files), and triaging recommendations (accept/owned/dismiss). The
commands that move or write files or call outside services (tag, organize,
dedupe, sync, import-spotify, recommend) stay CLI-only, reachable via a
terminal running the same bundled `mtk-backend.exe <command>` (or your dev
venv's `mtk` command). Expanding the dashboard to cover those is future
work.
