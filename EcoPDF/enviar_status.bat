@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Enviando agora o status deste PC para o painel das salas...
echo.
venv\Scripts\python.exe painel.py
echo.
pause
