# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for freezing the connector into a standalone onedir
build (see docs/ARCHITECTURE.md's Phase 5e section in the main
forex-scalper-bot project for the full design/rationale). Onedir chosen
over onefile deliberately: onefile self-extracts to a temp directory on
every launch, a pattern that frequently trips antivirus heuristics and
adds startup latency; onedir (PyInstaller's own default) ships a static
folder instead.

IMPORTANT -- PyInstaller does NOT cross-compile. This spec can only
produce a real Windows .exe when run ON Windows with `MetaTrader5`
actually installed. Running `pyinstaller` against this spec on
Linux/macOS produces a binary for THAT platform instead -- useful only
as a structural smoke test of the non-MT5-specific parts of this build
(see packaging/README.md for what that does and doesn't prove).
"""
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

hidden_imports = [
    # MetaTrader5's plain top-level `import MetaTrader5` inside
    # _import_real_mt5() is a real, statically-visible import (not a
    # dynamic one), so PyInstaller's default Analysis should already
    # find it without this -- no community pyinstaller-hooks-contrib
    # hook exists for MetaTrader5 as of this writing, so this entry is
    # redundant insurance, not a confirmed requirement. Verify on the
    # real Windows build; remove if unneeded.
    "MetaTrader5",
    # Guards against a documented lazy-import discovery gotcha in
    # websockets (python-websockets/websockets#956) affecting its
    # `legacy` submodules. This project only uses websockets.asyncio,
    # never .legacy, so this may also turn out unnecessary -- same
    # "verify and possibly remove" status as above.
    "websockets.legacy.client",
]

# httpx's HTTPS requests go through certifi's CA bundle, which certifi
# ships as a non-Python data file -- a classic PyInstaller gotcha for
# any package doing TLS (the default Analysis won't pick up data files
# on its own).
datas = collect_data_files("certifi")

a = Analysis(
    # NOT ../src/fx_connector/main.py directly -- see run_connector.py's
    # docstring for why (main.py's package-relative imports break when
    # PyInstaller runs it as a bare __main__ script; this confirmed the
    # hard way by actually running an early build during Phase 5e).
    ["run_connector.py"],
    pathex=["../src"],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="fx-connector",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="fx-connector",
)
