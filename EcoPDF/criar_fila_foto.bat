@echo off
chcp 65001 >nul
title EcoPDF - fila de fotos
cd /d "%~dp0"

rem Cria a impressora "EcoPDF Foto": a MESMA impressora padrao, adicionada de novo,
rem para guardar papel fotografico + qualidade alta nas preferencias do driver.
rem O EcoPDF manda as fotos para ela e o laudo para a impressora padrao.
rem Rodar com dois cliques (NAO precisa "Executar como administrador": ele pede sozinho).

powershell -NoProfile -Command "if (Get-Printer -Name 'EcoPDF Foto' -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if not errorlevel 1 goto preferencias

echo.
echo  Criando a impressora "EcoPDF Foto" a partir da impressora padrao...
echo  (o Windows vai pedir permissao: clique Sim)
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-Command','$p = Get-CimInstance Win32_Printer -Filter ''Default=True''; if (-not $p) { exit 2 }; Add-Printer -Name ''EcoPDF Foto'' -DriverName $p.DriverName -PortName $p.PortName'"

powershell -NoProfile -Command "if (Get-Printer -Name 'EcoPDF Foto' -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if errorlevel 1 (
  echo.
  echo  NAO consegui criar a "EcoPDF Foto".
  echo  Confira se a impressora padrao do Windows e a impressora real ^(nao a PDF24^)
  echo  e rode de novo. Sem a "EcoPDF Foto" o EcoPDF continua imprimindo normalmente.
  echo.
  pause
  exit /b 1
)
echo  Criada.

:preferencias
echo.
echo  Vai abrir as PREFERENCIAS da "EcoPDF Foto". Escolha:
echo     Tipo de papel:  Papel fotografico brilhante  ^(o papel que se usa nas fotos^)
echo     Qualidade:      Alta
echo  e clique OK. So isso: a impressora padrao continua como esta ^(papel comum^).
echo.
pause
rundll32 printui.dll,PrintUIEntry /e /n "EcoPDF Foto"
echo.
echo  Pronto. Na proxima impressao pelo EcoPDF as fotos saem pela "EcoPDF Foto".
echo  Para desfazer: apague a impressora "EcoPDF Foto" em Configuracoes ^> Impressoras.
echo.
pause
