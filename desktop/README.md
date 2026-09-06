# Music Toolkit — desktop

A minimal Electron shell around the existing Python backend. Unlike a
typical Electron app, there's no React/Vite renderer here — the "renderer"
is just this window pointed at the dashboard the backend already serves
(`src/musictoolkit/dashboard/app.py`), so there's nothing to build on the
JS side beyond the main process itself.

On launch, the main process spawns the Python backend (in dev, your own
venv's `python -m musictoolkit.cli dashboard`; in a packaged build, the
bundled `mtk-backend.exe`), waits for it to respond on `127.0.0.1:4533`,
then opens a window pointed at that URL. On quit, the backend process is
killed. A single-instance lock prevents a second launch from spawning a
second backend fighting over the same port.

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

This packages what the dashboard already does: browsing/searching the
library and triaging recommendations (accept/owned/dismiss). It does
**not** add a GUI for scan/tag/organize/dedupe/sync/import-spotify/recommend
— those stay CLI-only, reachable via a terminal running the same bundled
`mtk-backend.exe <command>` (or your dev venv's `mtk` command). Expanding
the dashboard to cover those is future work, not part of this pass.
