@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PORTA=11112
for /f "tokens=1,2 delims== " %%a in ('findstr /b /c:"PORTA" config.ini') do set PORTA=%%b
for /f "tokens=1,2 delims== " %%a in ('findstr /b /c:"AE_TITLE" config.ini') do set AET=%%b

set VERSAO=?
if exist VERSAO.txt set /p VERSAO=<VERSAO.txt
echo ================= EcoPDF =================
echo  Versao: %VERSAO%
netstat -an | findstr /r /c:":%PORTA% .*LISTENING" >nul
if errorlevel 1 (
  echo  SITUACAO: PARADO  ^(nao esta recebendo imagens^)
) else (
  echo  SITUACAO: FUNCIONANDO
)
echo.
echo  Dados para cadastrar no Vivid:
echo    AE Title: %AET%
echo    Porta:    %PORTA%
for /f "tokens=2 delims=:" %%i in ('ipconfig ^| findstr /c:"IPv4"') do echo    IP:      %%i
echo.
echo  Ultimas linhas do registro:
if exist log\ecopdf.log powershell -NoProfile -Command "Get-Content -Tail 15 -Encoding UTF8 'log\ecopdf.log'"
echo ==========================================
if not "%1"=="nopause" pause
