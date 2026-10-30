$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m PyInstaller --noconfirm --clean --windowed --onedir --name "GT7-Race-Control-Ecurie" --paths . --add-data "circuits_gt7/index.json;circuits_gt7" race_control_live_V4_INTERFACE.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "EXE: dist\GT7-Race-Control-Ecurie\GT7-Race-Control-Ecurie.exe"
