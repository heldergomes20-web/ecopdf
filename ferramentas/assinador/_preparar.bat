@echo off
rem Cria o ambiente Python desta pasta na primeira vez (precisa de internet so nesse momento).
if exist ".venv\Scripts\python.exe" .venv\Scripts\python.exe -c "import cryptography, truststore" >nul 2>&1 && goto :eof
if exist ".venv" rmdir /s /q ".venv"
set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  echo.
  echo  Este PC nao tem Python. Instale pelo python.org e rode de novo.
  pause
  exit /b 1
)
echo  Preparando o assinador pela primeira vez...
"%PY%" -m venv .venv
.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.venv\Scripts\python.exe -m pip install --quiet "cryptography>=42" "truststore>=0.9"
if errorlevel 1 (
  echo  Falhou baixar o componente de criptografia. Verifique a internet.
  pause
  exit /b 1
)
