@echo off
cd /d "%~dp0"
schtasks /end /tn EcoPDF >nul 2>&1
timeout /t 2 >nul
schtasks /run /tn EcoPDF
timeout /t 4 >nul
call "%~dp0status.bat"
