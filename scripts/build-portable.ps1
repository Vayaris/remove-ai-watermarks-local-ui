param(
    [string]$Version = "1.0.0",
    [string]$PythonSource = "C:\Users\Vayaris\AI\StabilityMatrix\Assets\Python\cpython-3.12.12-windows-x86_64-none",
    [string]$OutputRoot = "C:\Users\Vayaris\AI\remove-ai-watermarks-local-ui-dist"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$PackageName = "RemoveAIWatermarksLocal-v$Version-windows-portable"
$PackageDir = Join-Path $OutputRoot $PackageName
$PythonDir = Join-Path $PackageDir ".python"
$VenvDir = Join-Path $PackageDir ".venv"

if (!(Test-Path $PythonSource)) {
    throw "PythonSource not found: $PythonSource"
}

if (Test-Path $PackageDir) {
    Remove-Item -LiteralPath $PackageDir -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $PackageDir | Out-Null
New-Item -ItemType Directory -Force -Path `
    (Join-Path $PackageDir "input"), `
    (Join-Path $PackageDir "output"), `
    (Join-Path $PackageDir "logs"), `
    (Join-Path $PackageDir "tmp"), `
    (Join-Path $PackageDir "models-cache") | Out-Null

Copy-Item -LiteralPath (Join-Path $RepoRoot "app.py") -Destination $PackageDir -Force
Copy-Item -LiteralPath (Join-Path $RepoRoot "Start-RemoveAIWatermarks.bat") -Destination $PackageDir -Force
Copy-Item -LiteralPath (Join-Path $RepoRoot "Start-RemoveAIWatermarks.ps1") -Destination $PackageDir -Force
Copy-Item -LiteralPath (Join-Path $RepoRoot "README.md") -Destination $PackageDir -Force
Copy-Item -LiteralPath (Join-Path $RepoRoot "LICENSE") -Destination $PackageDir -Force

Copy-Item -LiteralPath $PythonSource -Destination $PythonDir -Recurse -Force

$PythonExe = Join-Path $PythonDir "python.exe"
& $PythonExe -m venv $VenvDir

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$PipPython = Join-Path $VenvDir "Scripts\python.exe"
& $PipPython -m pip install --upgrade pip setuptools wheel
& $PipPython -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
& $PipPython -m pip install "remove-ai-watermarks[gpu,detect,trustmark,lama]==0.12.1" fastapi "uvicorn[standard]" python-multipart

$Pyvenv = Join-Path $VenvDir "pyvenv.cfg"
(Get-Content -LiteralPath $Pyvenv) |
    ForEach-Object {
        if ($_ -match "^home = ") { "home = $PythonDir" } else { $_ }
    } |
    Set-Content -LiteralPath $Pyvenv -Encoding utf8

@"
Remove AI Watermarks Local v$Version

Run:
  Start-RemoveAIWatermarks.bat

First GPU cleanup may download Hugging Face models into models-cache.
"@ | Set-Content -LiteralPath (Join-Path $PackageDir "PORTABLE-README.txt") -Encoding utf8

Write-Host "Portable package ready: $PackageDir"
