@echo off
chcp 65001 >nul
title EcoPDF - assinar
cd /d "%~dp0"
call _preparar.bat || exit /b 1
.venv\Scripts\python.exe assinador.py assinar %1
pause
