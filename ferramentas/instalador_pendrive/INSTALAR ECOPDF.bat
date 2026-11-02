@echo off
chcp 65001 >nul
title Instalar EcoPDF (baixa a versao do GitHub)
cd /d "%~dp0"

rem Pede permissao de administrador sozinho.
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process '%~f0' -Verb RunAs"
  exit /b
)

echo.
echo  Instalador EcoPDF - baixa a versao publicada no GitHub e confere a assinatura.
echo  Precisa de internet. Dados e exames deste PC nao sao apagados.
echo.
echo  Qual versao instalar?
echo    P = Producao (a versao liberada para todas as salas)
echo    T = Teste (a mais nova, so para o PC de teste)
choice /c PT /n /m "  Digite P ou T: "
if errorlevel 2 (set CANAL=teste) else (set CANAL=geral)
echo.

set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY if exist "C:\Program Files\Python313\python.exe" set "PY=C:\Program Files\Python313\python.exe"
if not defined PY (
  echo  Instalando o Python 3.13 ^(pode levar alguns minutos^)...
  winget install -e --id Python.Python.3.13 --scope machine --silent --accept-package-agreements --accept-source-agreements
  if exist "C:\Program Files\Python313\python.exe" set "PY=C:\Program Files\Python313\python.exe"
)
if not defined PY (
  echo  Nao encontrei nem consegui instalar o Python. Instale pelo python.org e rode de novo.
  pause
  exit /b 1
)

set "TMPV=%TEMP%\EcoPDF_instalador"
if not exist "%TMPV%\Scripts\python.exe" (
  echo  Preparando o conferidor de assinatura...
  "%PY%" -m venv "%TMPV%"
)
"%TMPV%\Scripts\python.exe" -m pip install --quiet "cryptography>=42" "truststore>=0.9"
if errorlevel 1 (
  echo  Falhou baixar o componente de criptografia. Verifique a internet e rode de novo.
  pause
  exit /b 1
)

"%TMPV%\Scripts\python.exe" "%~dp0baixar_ecopdf.py" %CANAL% "C:\EcoPDF"
if errorlevel 1 (
  echo.
  pause
  exit /b 1
)

echo.
echo  Rodando o instalar.bat do EcoPDF...
call "C:\EcoPDF\instalar.bat"
