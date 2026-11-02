@echo off
chcp 65001 >nul
title Instalador EcoPDF
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Feche esta janela, clique com o botao DIREITO em instalar.bat
  echo  e escolha "Executar como administrador".
  echo.
  pause
  exit /b 1
)

rem Usa o Python que ja existe (comando py, do Python Install Manager);
rem se nao houver nenhum, instala o 3.13 pelo winget.
set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY if exist "C:\Program Files\Python313\python.exe" set "PY=C:\Program Files\Python313\python.exe"
if not defined PY (
  echo [1/5] Instalando o Python 3.13 ^(pode levar alguns minutos^)...
  winget install -e --id Python.Python.3.13 --scope machine --silent --accept-package-agreements --accept-source-agreements
  if exist "C:\Program Files\Python313\python.exe" set "PY=C:\Program Files\Python313\python.exe"
)
if not defined PY (
  echo.
  echo  Nao encontrei nem consegui instalar o Python.
  echo  Instale pelo site python.org e rode este instalador de novo.
  pause
  exit /b 1
)
echo [1/5] Python encontrado: %PY%

echo [2/5] Preparando o ambiente do EcoPDF...
if not exist venv "%PY%" -m venv venv
if not exist venv\Scripts\pythonw.exe (
  echo  Falhou criar o ambiente Python. Encaminhe uma foto desta tela para a administracao da clinica.
  pause
  exit /b 1
)
venv\Scripts\python.exe -m pip install --quiet --upgrade pip
venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
if errorlevel 1 (
  echo  Falhou baixar os componentes. Verifique a internet e rode de novo.
  pause
  exit /b 1
)

rem O renomeador de laudos agora roda dentro do EcoPDF: para o antigo e tira o atalho
rem dele da inicializacao, para nao haver dois renomeando os mesmos arquivos.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'py*' -and $_.CommandLine -like '*renomear_laudos*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue };" ^
  "Get-ChildItem ([Environment]::GetFolderPath('Startup')) -Filter '*renomead*' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue"

set PORTA=11112
for /f "tokens=1,2 delims== " %%a in ('findstr /b /c:"PORTA" config.ini') do set PORTA=%%b

schtasks /end /tn EcoPDF >nul 2>&1
timeout /t 2 >nul
netstat -an | findstr /r /c:":%PORTA% .*LISTENING" >nul
if not errorlevel 1 (
  echo.
  echo  ATENCAO: outro programa ja usa a porta %PORTA% neste PC.
  echo  Troque PORTA no config.ini ^(ex.: 11113^), use o mesmo numero no Vivid
  echo  e rode este instalador de novo.
  pause
  exit /b 1
)

echo [3/5] Liberando a porta %PORTA% no firewall ^(so para a rede local^)...
netsh advfirewall firewall delete rule name="EcoPDF DICOM" >nul 2>&1
netsh advfirewall firewall add rule name="EcoPDF DICOM" dir=in action=allow protocol=TCP localport=%PORTA% remoteip=localsubnet profile=any >nul

echo [4/5] Impedindo o PC de dormir na tomada ^(senao o Vivid nao consegue enviar^)...
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0

echo [5/5] Configurando para iniciar sozinho quando %USERNAME% entrar no Windows...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a = New-ScheduledTaskAction -Execute '%~dp0venv\Scripts\pythonw.exe' -Argument 'ecopdf.py' -WorkingDirectory '%~dp0';" ^
  "$t = New-ScheduledTaskTrigger -AtLogOn -User '%USERDOMAIN%\%USERNAME%';" ^
  "$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew;" ^
  "Register-ScheduledTask -TaskName 'EcoPDF' -Action $a -Trigger $t -Settings $s -Force | Out-Null;" ^
  "Stop-ScheduledTask -TaskName 'EcoPDF' -ErrorAction SilentlyContinue; Start-ScheduledTask -TaskName 'EcoPDF'"

set TELA_PORTA=8765
for /f "tokens=1,2 delims== " %%a in ('findstr /b /c:"TELA_PORTA" config.ini') do set TELA_PORTA=%%b
echo Criando o icone "EcoPDF" na Area de Trabalho e no menu Iniciar...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$url='http://127.0.0.1:%TELA_PORTA%/'; $ico='%~dp0ecopdf.ico';" ^
  "$edge=@([Environment]::GetFolderPath('ProgramFilesX86'),[Environment]::GetFolderPath('ProgramFiles')) | ForEach-Object { Join-Path $_ 'Microsoft\Edge\Application\msedge.exe' } | Where-Object { Test-Path $_ } | Select-Object -First 1;" ^
  "$d=[Environment]::GetFolderPath('Desktop'); $m=[Environment]::GetFolderPath('Programs');" ^
  "Remove-Item (Join-Path $d 'EcoPDF - Exames.url') -Force -ErrorAction SilentlyContinue;" ^
  "$w=New-Object -ComObject WScript.Shell;" ^
  "foreach ($pasta in @($d,$m)) { $l=$w.CreateShortcut((Join-Path $pasta 'EcoPDF.lnk'));" ^
  "  if ($edge) { $l.TargetPath=$edge; $l.Arguments='--app='+$url } else { $l.TargetPath=(Join-Path $env:WINDIR 'explorer.exe'); $l.Arguments=$url };" ^
  "  $l.IconLocation=$ico+',0'; $l.Description='EcoPDF - exames e imagens'; $l.Save() }"

timeout /t 5 >nul
echo.
call "%~dp0status.bat" nopause
echo.
set AET=ECOPDF
for /f "tokens=1,2 delims== " %%a in ('findstr /b /c:"AE_TITLE" config.ini') do set AET=%%b
set ABRIR=-Abrir
if /i "%~1"=="auto" set ABRIR=
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dados.ps1" -Porta %PORTA% -AE %AET% %ABRIR%
echo.
echo  Instalacao concluida. Os dados para o aparelho e para o roteador abriram no
echo  Bloco de Notas e ficam salvos em "Laudos - Nao Apagar\DADOS DESTE PC.txt".
if not exist "%~dp0painel.ini" (
  echo.
  echo  ATENCAO: falta o arquivo painel.ini nesta pasta. Sem ele este PC nao aparece
  echo  na planilha das salas. Copie o painel.ini do pendrive para %~dp0
  echo  e depois rode reiniciar.bat ^(nao precisa instalar de novo^).
)
if /i not "%~1"=="auto" pause
