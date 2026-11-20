@echo off
chcp 65001 >nul
title EcoPDF - configurar publicacao
cd /d "%~dp0"
call _preparar.bat || exit /b 1
.venv\Scripts\python.exe assinador.py configurar-publicar
pause
