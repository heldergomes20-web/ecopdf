@echo off
chcp 65001 >nul
title EcoPDF - verificar teste
cd /d "%~dp0"
call _preparar.bat || exit /b 1
.venv\Scripts\python.exe assinador.py verificar teste
pause
