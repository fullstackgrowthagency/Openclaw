# Packaging the connector

This directory holds the PyInstaller configuration for freezing
`fx_connector` into a standalone Windows build so end users don't need
Python installed. See the main forex-scalper-bot project's
`docs/ARCHITECTURE.md` (Phase 5e) for the full design rationale.

## Two hard constraints, read this first

1. **PyInstaller does not cross-compile.** A real Windows `.exe` can
   only be produced by running PyInstaller *on Windows* (a real machine,
   or a Windows CI runner such as GitHub Actions' `windows-latest`).
   Running it anywhere else (Linux, macOS) produces a binary for *that*
   platform instead -- useless for distribution, though still valuable
   as a structural smoke test of everything except the `MetaTrader5`-
   specific parts (see "What was actually verified" below).
2. **Code signing is a separate step requiring real money, a verified
   identity, and Windows-native tooling** -- it is not part of `build.ps1`
   and can't be prototyped or faked anywhere. See "Code signing" below.

## Building on a real Windows machine

```powershell
cd connector
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e "..\relay_protocol[dev]"
pip install -e ".[dev]"
.\packaging\build.ps1
```

Then run `dist\fx-connector\fx-connector.exe` **directly** (double-click
or from a terminal -- not `python dist\fx-connector\fx-connector.exe`)
and confirm it starts and reaches the pairing prompt.

## Checklist for that first real Windows build

Nothing below was guessed at randomly -- each item is grounded in real
research (PyInstaller's own docs/issue tracker, `MetaTrader5`'s official
docs) done during Phase 5e, but none of it could be *verified* without a
real Windows machine + a real MT5 terminal, which this development
environment doesn't have. Work through these in order:

1. **Run the build.** If it fails outright, read the error -- it's
   almost always a missing package in the venv, not a PyInstaller issue.
2. **Launch the frozen `.exe` directly.** If it crashes with
   `ModuleNotFoundError: No module named 'MetaTrader5'` (or a DLL load
   error for it), the `hiddenimports=["MetaTrader5"]` entry in
   `connector.spec` wasn't enough on its own -- add an explicit
   `binaries=[...]` entry pointing PyInstaller at the installed
   `MetaTrader5` package's compiled files in site-packages (or try
   `collect_dynamic_libs("MetaTrader5")` from
   `PyInstaller.utils.hooks`). No community
   `pyinstaller-hooks-contrib` hook for `MetaTrader5` was found to exist
   as of this writing -- check again first, one may have been added
   since.
3. **If `websockets` fails to import inside the frozen build**, that's
   the documented `websockets.legacy` lazy-import gotcha
   (`python-websockets/websockets#956`) -- the spec already includes a
   defensive `hiddenimports=["websockets.legacy.client"]` for this, but
   confirm it's actually needed (this project only uses
   `websockets.asyncio`, never `.legacy`) and remove it if the build
   works fine without it.
4. **If `httpx`'s pairing HTTP call fails with an SSL/certificate
   error**, the `datas=collect_data_files("certifi")` entry should have
   bundled certifi's CA bundle already -- if it's still missing, that's
   the fix to double check first.
5. **Confirm the connector can actually reach a running MT5 terminal**
   on the same machine -- this is the first point in this whole project
   where real MT5 hardware gets exercised, and it's out of this phase's
   scope to do anything more here than flag it (see Phase 5g in the main
   project's `docs/ARCHITECTURE.md`).
6. **Decide onedir vs. an installer wrapper.** `build.ps1` produces a
   `dist\fx-connector\` folder (onedir, not onefile -- see the spec
   file's own docstring for why). For end-user distribution, wrapping
   that folder in an actual installer (Inno Setup or NSIS are the
   well-trusted options) is the natural next step, but building that
   installer is a separate task from getting a working frozen build.

## What was actually verified in this (Linux) development environment

`pyinstaller` itself is a pure-Python tool and runs fine here -- it just
can't target Windows from Linux. Running it against this same spec on
Linux produces a Linux binary, which is a real (if partial) smoke test:
it proves `Analysis` completes without error, the `hiddenimports`/`datas`
settings are syntactically and semantically valid, and everything
platform-independent (config loading, relay client wiring, pairing HTTP
logic, argument/env handling) actually freezes and runs. The frozen
Linux binary gets exactly as far as `_import_real_mt5()` and fails there
with a clean `ModuleNotFoundError` -- which is the *correct* behavior on
a non-Windows build (`MetaTrader5` is correctly never installed there,
per its `sys_platform == 'win32'` marker), and proves the platform guard
works inside a frozen build, not just under `python main.py`. See the
Phase 5e section of `docs/ARCHITECTURE.md` for the exact command run and
its output.

## Code signing (not attempted here, not attemptable here)

An unsigned `.exe` will trigger Windows SmartScreen warnings and is
correctly treated with suspicion by users and antivirus software.
Signing it for real distribution requires, confirmed via direct research
during Phase 5e:

- **A purchased Authenticode certificate** from a trusted CA (DigiCert,
  Sectigo, SSL.com, etc.) -- roughly $200-$400/year for OV, $270-$580/year
  for EV as of 2026 pricing. This requires real organizational/identity
  validation by the CA; it cannot be self-issued or faked.
- **Hardware-backed private key custody.** Since mid-2023, CA/Browser
  Forum baseline requirements mandate the signing key live on a
  FIPS-140-2-Level-2-or-better hardware token or a CA-hosted cloud HSM
  (e.g. DigiCert KeyLocker, SSL.com eSigner) -- a plain `.pfx` file on
  disk is no longer sufficient for a newly issued certificate.
- **`signtool.exe`**, part of the Windows SDK, run on Windows (or a
  cloud-HSM-integrated CI signing step, which is how EV signing is
  commonly done in CI today).

This is a budgeted, human decision (which CA, OV vs. EV, whose business
identity gets validated) explicitly out of scope for Phase 5e -- get an
unsigned build working and verified first.
