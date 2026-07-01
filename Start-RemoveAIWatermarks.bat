@echo off
set "BASE=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%BASE%Start-RemoveAIWatermarks.ps1"
