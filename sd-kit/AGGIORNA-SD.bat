@echo off
title MeshWatch - aggiornamento scheda SD
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0aggiorna-sd.ps1"
echo.
pause
