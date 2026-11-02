@echo off
chcp 65001 >nul
title EcoPDF - voltar versao
cd /d "%~dp0"
rem Devolve a versao guardada em _anterior pelo atualizar_agora.bat.
net session >nul 2>&1
if errorlevel 1 (
  if "%~1"=="" (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  ) else (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%~1' -Verb RunAs"
  )
  exit /b
)
echo.
venv\Scripts\python.exe atualizador.py voltar
if errorlevel 11 goto voltou
if errorlevel 10 goto instalar
goto fim
:instalar
call "%~dp0instalar.bat" auto
venv\Scripts\python.exe atualizador.py conferir
if errorlevel 11 goto voltou
goto fim
:voltou
call "%~dp0instalar.bat" auto
echo.
call "%~dp0status.bat" nopause
:fim
echo.
pause
