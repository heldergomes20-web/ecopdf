@echo off
chcp 65001 >nul
title EcoPDF - atualizar agora
cd /d "%~dp0"
rem Pede permissao de administrador (o Windows pergunta "Sim/Nao").
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
echo  Nao use durante um exame: o EcoPDF fica parado por 1 a 3 minutos.
echo.
if not exist venv\Scripts\python.exe (
  echo  EcoPDF nao instalado nesta pasta. Rode o instalar.bat primeiro.
  pause
  exit /b 1
)
venv\Scripts\python.exe atualizador.py %1
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
