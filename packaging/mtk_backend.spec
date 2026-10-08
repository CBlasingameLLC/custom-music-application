# Build with: pyinstaller packaging/mtk_backend.spec
# (run from the repo root; produces dist/mtk-backend.exe on Windows)
#
# This will very likely need at least one iteration on first build: a
# missing hidden import surfaces as a traceback naming the missing module
# when you actually RUN the frozen exe, not as a spec-time error. That's
# expected — add the named module to hiddenimports below and rebuild.

from pathlib import Path

repo_root = Path(SPECPATH).resolve().parent
migrations_dir = repo_root / "src" / "musictoolkit" / "db" / "migrations"

# Non-Python data files: the migrations are read via importlib.resources at
# runtime, not imported as Python, so PyInstaller's static import analysis
# won't find them on its own — same reasoning for config.example.toml,
# needed for the first-run config bootstrap (see musictoolkit/cli.py's
# _find_example_config, which checks sys._MEIPASS for exactly this file).
datas = [
    (str(migrations_dir / "*.sql"), "musictoolkit/db/migrations"),
    (str(repo_root / "config.example.toml"), "."),
]

# uvicorn picks its protocol/loop implementations dynamically at runtime
# (based on which optional accelerators are installed), and pandas' C
# extensions load the same way — both are well-known PyInstaller gaps that
# static import analysis misses without an explicit hint.
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "pandas._libs.tslibs.base",
]

a = Analysis(
    ["mtk_backend_entrypoint.py"],
    pathex=[str(repo_root / "src")],
    datas=datas,
    hiddenimports=hiddenimports,
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="mtk-backend",
    console=True,  # keep console output for direct CLI use (scan/tag/organize/etc.);
                   # the Electron shell hides it anyway via spawn's windowsHide option
    upx=True,
)
