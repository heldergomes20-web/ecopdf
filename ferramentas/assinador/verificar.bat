@echo off
chcp 65001 >nul
title EcoPDF - verificar
cd /d "%~dp0"
call _preparar.bat || exit /b 1
.venv\Scripts\python.exe assinador.py verificar
pause
