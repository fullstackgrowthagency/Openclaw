# Build script for Windows -- run from connector/ with an activated venv
# that has this project's [dev] extras installed (which includes
# pyinstaller and, via the sys_platform marker in pyproject.toml, the
# real MetaTrader5 package -- this script only works on Windows).
#
# See packaging/README.md for the full pre-build checklist and what to
# do if the build fails or the frozen .exe can't find a module.
#
# Usage:
#   .\packaging\build.ps1          # normal build
#   .\packaging\build.ps1 -Clean   # wipe build/ and dist/ first

param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

if ($Clean) {
    Write-Host "Removing build/ and dist/ ..."
    Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
}

pyinstaller packaging\connector.spec --noconfirm

Write-Host ""
Write-Host "Build complete. The frozen app is at dist\fx-connector\fx-connector.exe"
Write-Host "Run it directly (not via python) to verify it starts correctly."
