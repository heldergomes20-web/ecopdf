@echo off
chcp 65001 >nul
title EcoPDF - reverter
cd /d "%~dp0"
call _preparar.bat || exit /b 1
echo.
echo  Versoes ja publicadas nesta pasta:
dir /b /ad publicados 2>nul
echo.
set /p ANTIGA= Versao cujo codigo voce quer de volta (ex.: 2026.10.30): 
set /p NOVA= Numero novo para publicar (maior que a ultima, ex.: 2026.11.05.2): 
.venv\Scripts\python.exe assinador.py reverter %ANTIGA% %NOVA%
pause
