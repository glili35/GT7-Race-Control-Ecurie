$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
py -3 -m pip install -r requirements-build.txt
py -3 -m PyInstaller --noconfirm --clean --windowed --onedir --name "GT7-Race-Control-Ecurie" --paths . race_control_live_V4_INTERFACE.py
Write-Host "EXE: dist\GT7-Race-Control-Ecurie\GT7-Race-Control-Ecurie.exe"
