$ErrorActionPreference = "Stop"

$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Base ".venv\Scripts\python.exe"
$EmbeddedPython = Join-Path $Base ".python"
$Pyvenv = Join-Path $Base ".venv\pyvenv.cfg"
$LogDir = Join-Path $Base "logs"
$StartupLog = Join-Path $LogDir "server-startup.log"
$ServerOut = Join-Path $LogDir "server-out.log"
$ServerErr = Join-Path $LogDir "server-error.log"
$Url = "http://127.0.0.1:7868"
$Health = "$Url/api/health"

New-Item -ItemType Directory -Force -Path `
    $LogDir, `
    (Join-Path $Base "input"), `
    (Join-Path $Base "output"), `
    (Join-Path $Base "tmp"), `
    (Join-Path $Base "models-cache") | Out-Null

if ((Test-Path $EmbeddedPython) -and (Test-Path $Pyvenv)) {
    (Get-Content -LiteralPath $Pyvenv) |
        ForEach-Object {
            if ($_ -match "^home = ") { "home = $EmbeddedPython" } else { $_ }
        } |
        Set-Content -LiteralPath $Pyvenv -Encoding utf8
}

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:HF_HOME = Join-Path $Base "models-cache"
$env:HF_HUB_CACHE = Join-Path $Base "models-cache\hub"
$env:HUGGINGFACE_HUB_CACHE = Join-Path $Base "models-cache\hub"
$env:TRANSFORMERS_CACHE = Join-Path $Base "models-cache\transformers"
$env:DIFFUSERS_CACHE = Join-Path $Base "models-cache\diffusers"
$env:TEMP = Join-Path $Base "tmp"
$env:TMP = Join-Path $Base "tmp"

function Test-Server {
    try {
        Invoke-RestMethod -Uri $Health -TimeoutSec 2 | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

"Starting at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Out-File -FilePath $StartupLog -Encoding utf8

if (-not (Test-Server)) {
    $Args = @("-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "7868", "--no-access-log")
    $Process = Start-Process `
        -FilePath $Python `
        -ArgumentList $Args `
        -WorkingDirectory $Base `
        -WindowStyle Hidden `
        -RedirectStandardOutput $ServerOut `
        -RedirectStandardError $ServerErr `
        -PassThru
    "Server PID: $($Process.Id)" | Out-File -FilePath $StartupLog -Encoding utf8 -Append

    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Server) { break }
    }
}

Start-Process $Url | Out-Null
