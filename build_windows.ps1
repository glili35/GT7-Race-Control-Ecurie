param(
    [switch]$SkipDependencies,
    [switch]$SmokeOnly,
    [string]$Executable = "",
    [string]$SmokeDirectory = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Test-ApplicationStartup {
    param([Parameter(Mandatory)][string]$ApplicationPath)

    if (!(Test-Path -LiteralPath $ApplicationPath)) {
        throw "Application introuvable : $ApplicationPath"
    }
    if (!$SmokeDirectory) {
        $SmokeDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("gt7-smoke-" + [guid]::NewGuid().ToString("N"))
    }
    New-Item -ItemType Directory -Path $SmokeDirectory -Force | Out-Null
    $reportPath = Join-Path $SmokeDirectory "startup.json"
    Remove-Item -LiteralPath $reportPath -ErrorAction SilentlyContinue

    # Le smoke exerce les vues pilote et ingénieur avec des données synthétiques.
    # Il ne doit ni lire la configuration d'un pilote ni contacter sa vraie PS5.
    $previousDataDirectory = $env:GT7RC_DATA_DIR
    $env:GT7RC_DATA_DIR = Join-Path $SmokeDirectory "profile"
    try {
        $arguments = "--smoke-test --smoke-report `"$reportPath`""
        $process = Start-Process -FilePath $ApplicationPath -ArgumentList $arguments -PassThru
        if (!$process.WaitForExit(45000)) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            throw "Le test de démarrage ne s'est pas terminé en 45 secondes."
        }
        $process.Refresh()
        if ($process.ExitCode -ne 0) {
            throw "Le test de démarrage a échoué (code $($process.ExitCode))."
        }
        if (!(Test-Path -LiteralPath $reportPath)) {
            throw "Le test n'a pas produit son rapport : $reportPath"
        }
        $report = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($report.ok -ne $true -or $report.version -ne "0.7.0") {
            throw "Le rapport ne confirme pas le démarrage de la version 0.7.0."
        }
        Write-Host "Démarrage de l'interface vérifié : $ApplicationPath"
        Write-Host "Rapport : $reportPath"
    }
    finally {
        $env:GT7RC_DATA_DIR = $previousDataDirectory
    }
}

if ($SmokeOnly) {
    if (!$Executable) { throw "-Executable est nécessaire avec -SmokeOnly." }
    Test-ApplicationStartup -ApplicationPath $Executable
    exit 0
}

if (!$SkipDependencies) {
    python -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

# Les imports historiques restent disponibles depuis le nouveau lanceur.
# Le catalogue est une ressource embarquée, jamais un fichier de session privée.
python -m PyInstaller --noconfirm --clean --windowed --onedir `
    --name "GT7-Race-Control-Ecurie" --paths . `
    --hidden-import race_control_live_V4_INTERFACE `
    --hidden-import strategie_carburant_V2 `
    --collect-submodules racecontrol `
    --add-data "circuits_gt7/index.json;circuits_gt7" `
    racecontrol_app.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$application = Join-Path $PSScriptRoot "dist\GT7-Race-Control-Ecurie\GT7-Race-Control-Ecurie.exe"
Test-ApplicationStartup -ApplicationPath $application
Write-Host "EXE : $application"
