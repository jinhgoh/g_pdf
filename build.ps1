[CmdletBinding()]
param(
    [switch]$InstallDependencies,
    [switch]$OneFile,
    [switch]$Console,
    [string]$GoogleOAuthClient = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "Build the Windows executable on Windows."
}

if ([string]::IsNullOrWhiteSpace($Python)) {
    $localPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $localPython) {
        $pythonExe = $localPython
    } else {
        $pythonExe = (Get-Command python -ErrorAction Stop).Source
    }
} else {
    $pythonExe = (Get-Command $Python -ErrorAction Stop).Source
}

Push-Location -LiteralPath $projectRoot
try {
    & $pythonExe -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.11 or later is required."
    }

    if ($InstallDependencies) {
        & $pythonExe -m pip install -r (Join-Path $projectRoot "requirements.txt") "pyinstaller>=6.11,<7"
        if ($LASTEXITCODE -ne 0) {
            throw "Dependency installation failed."
        }
    }

    & $pythonExe -c "import PyInstaller, PyQt6, pymupdf, requests"
    if ($LASTEXITCODE -ne 0) {
        throw "Build dependencies are missing. Rerun with -InstallDependencies."
    }

    # Desktop client configuration identifies this app; no user tokens belong
    # in a build. Stage only validated client fields under a fixed resource name.
    $oauthSource = $GoogleOAuthClient
    if ([string]::IsNullOrWhiteSpace($oauthSource)) {
        $oauthSource = Join-Path $projectRoot "gpdf\google_oauth_client.json"
    }
    $oauthData = @()
    if (Test-Path -LiteralPath $oauthSource -PathType Leaf) {
        $oauthSource = (Resolve-Path -LiteralPath $oauthSource).Path
        $oauthStage = Join-Path $projectRoot ".build\oauth\google_oauth_client.json"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $oauthStage) | Out-Null
        & $pythonExe -c 'import json, sys; from pathlib import Path; from gpdf.drive_auth import OAuthConfig; config = OAuthConfig.from_file(sys.argv[1]); Path(sys.argv[2]).write_bytes(json.dumps(dict(installed=dict(client_id=config.client_id, client_secret=config.client_secret))).encode())' $oauthSource $oauthStage
        if ($LASTEXITCODE -ne 0) {
            throw "Invalid Google Desktop OAuth client configuration."
        }
        $oauthData = @("--add-data", "$oauthStage;gpdf")
    } elseif (-not [string]::IsNullOrWhiteSpace($GoogleOAuthClient)) {
        throw "The specified Google OAuth client file does not exist."
    } else {
        Write-Warning "Building without Google sign-in configuration; local PDFs will still work."
    }

    $bundleMode = if ($OneFile) { "--onefile" } else { "--onedir" }
    $windowMode = if ($Console) { "--console" } else { "--windowed" }
    $buildArguments = @(
        "-m", "PyInstaller",
        "--noconfirm", "--clean", $windowMode,
        $bundleMode,
        "--name", "Gpdf",
        "--distpath", (Join-Path $projectRoot "dist"),
        "--workpath", (Join-Path $projectRoot ".build\pyinstaller"),
        "--specpath", (Join-Path $projectRoot ".build"),
        "--collect-binaries", "pymupdf",
        "--copy-metadata", "PyMuPDF",
        # These optional PyMuPDF integrations are unused by the viewer. Excluding
        # them keeps a build from inheriting ML/data libraries in a Conda install.
        "--exclude-module", "pymupdf.layout",
        "--exclude-module", "pandas",
        "--exclude-module", "PIL",
        (Join-Path $projectRoot "main.py")
    )
    & $pythonExe @buildArguments @oauthData
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed. See the output above."
    }

    if ($OneFile) {
        Write-Host "Built: $(Join-Path $projectRoot 'dist\Gpdf.exe')"
    } else {
        Write-Host "Built: $(Join-Path $projectRoot 'dist\Gpdf\Gpdf.exe')"
        Write-Host "Distribute the entire dist\Gpdf folder."
    }
} finally {
    Pop-Location
}
