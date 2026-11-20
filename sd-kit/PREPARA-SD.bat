@echo off
title MeshWatch - preparazione scheda SD
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0prepara-sd.ps1"
echo.
pause
