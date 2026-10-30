@echo off
chcp 65001 >nul
title EcoPDF - gerar_chaves
cd /d "%~dp0"
call _preparar.bat || exit /b 1
.venv\Scripts\python.exe assinador.py gerar-chaves
pause
