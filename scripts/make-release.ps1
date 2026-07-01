param(
    [string]$Version = "1.0.0",
    [string]$OutputRoot = "C:\Users\Vayaris\AI\remove-ai-watermarks-local-ui-dist",
    [int]$VolumeSizeMB = 1800
)

$ErrorActionPreference = "Stop"

$PackageName = "RemoveAIWatermarksLocal-v$Version-windows-portable"
$PackageDir = Join-Path $OutputRoot $PackageName
$ReleaseDir = Join-Path $OutputRoot "release"
$SevenZip = Get-Command 7z -ErrorAction SilentlyContinue

if (!(Test-Path $PackageDir)) {
    throw "Package folder not found. Run scripts\build-portable.ps1 first: $PackageDir"
}

New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null

if ($SevenZip) {
    $Archive = Join-Path $ReleaseDir "$PackageName.7z"
    if (Test-Path "$Archive*") {
        Remove-Item -LiteralPath (Get-ChildItem -LiteralPath $ReleaseDir -Filter "$PackageName.7z*" | ForEach-Object FullName) -Force
    }
    & $SevenZip.Source a -t7z -mx=7 "-v${VolumeSizeMB}m" $Archive $PackageDir
}
else {
    $Archive = Join-Path $ReleaseDir "$PackageName.zip"
    $PackagePython = Join-Path $PackageDir ".python\python.exe"
    if (!(Test-Path $PackagePython)) {
        $PackagePython = "python"
    }
    Get-ChildItem -LiteralPath $ReleaseDir -Filter "$PackageName.zip*" -ErrorAction SilentlyContinue |
        Remove-Item -Force

    $ZipScript = @'
import os
import sys
import zipfile

source = sys.argv[1]
target = sys.argv[2]
root_parent = os.path.dirname(source)

with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=6) as zf:
    for current, dirs, files in os.walk(source):
        dirs[:] = sorted(dirs)
        for name in sorted(files):
            path = os.path.join(current, name)
            arcname = os.path.relpath(path, root_parent)
            zf.write(path, arcname)
'@
    $ZipScriptPath = Join-Path $ReleaseDir "_zip_package.py"
    $ZipScript | Set-Content -LiteralPath $ZipScriptPath -Encoding utf8
    & $PackagePython $ZipScriptPath $PackageDir $Archive
    Remove-Item -LiteralPath $ZipScriptPath -Force

    $Limit = [int64]$VolumeSizeMB * 1024 * 1024
    $ArchiveItem = Get-Item -LiteralPath $Archive
    if ($ArchiveItem.Length -gt $Limit) {
        $buffer = New-Object byte[] (4 * 1024 * 1024)
        $inputStream = [System.IO.File]::OpenRead($Archive)
        try {
            $part = 1
            while ($inputStream.Position -lt $inputStream.Length) {
                $partPath = "{0}.{1:000}" -f $Archive, $part
                $outputStream = [System.IO.File]::Create($partPath)
                try {
                    $written = 0L
                    while ($written -lt $Limit) {
                        $toRead = [Math]::Min($buffer.Length, $Limit - $written)
                        $read = $inputStream.Read($buffer, 0, [int]$toRead)
                        if ($read -le 0) { break }
                        $outputStream.Write($buffer, 0, $read)
                        $written += $read
                    }
                }
                finally {
                    $outputStream.Close()
                }
                $part += 1
            }
        }
        finally {
            $inputStream.Close()
        }
        Remove-Item -LiteralPath $Archive -Force
        $PartNames = Get-ChildItem -LiteralPath $ReleaseDir -Filter "$PackageName.zip.*" |
            Sort-Object Name |
            ForEach-Object { '"' + $_.Name + '"' }
        $JoinExpression = $PartNames -join "+"
        @"
@echo off
copy /b $JoinExpression "$PackageName.zip"
echo Created $PackageName.zip
"@ | Set-Content -LiteralPath (Join-Path $ReleaseDir "JOIN_ZIP_PARTS.bat") -Encoding ascii
    }
}

@"
# Remove AI Watermarks Local v$Version

Portable Windows build.

## Requirements

- Windows
- Recent NVIDIA driver recommended for CUDA/GPU processing
- Internet access on first GPU cleanup to download Hugging Face models

## Install

If the release is split into .001, .002, etc., download every part into the same folder and run:

JOIN_ZIP_PARTS.bat

Then extract the rebuilt archive and run:

Start-RemoveAIWatermarks.bat

The UI opens at http://127.0.0.1:7868.

## Notes

- Hugging Face models are not bundled; they download into models-cache on first use.
- Only use this on images you own or have the right to modify.
- Built on top of remove-ai-watermarks by wiltodelta, Apache-2.0.
"@ | Set-Content -LiteralPath (Join-Path $ReleaseDir "RELEASE_NOTES_v$Version.md") -Encoding utf8

Get-ChildItem -LiteralPath $ReleaseDir | Select-Object Name,Length,LastWriteTime
