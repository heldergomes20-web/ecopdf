@echo off
chcp 65001 >nul
title EcoPDF - fila de fotos
cd /d "%~dp0"

rem Cria a impressora "EcoPDF Foto": a MESMA impressora padrao, adicionada de novo,
rem para guardar papel fotografico + qualidade Alta nas preferencias do driver (o botao Alta do Imprimir).
rem O EcoPDF manda as fotos para ela e o laudo para a impressora padrao.
rem Rodar com dois cliques (NAO precisa "Executar como administrador": ele pede sozinho).
rem O instalar.bat chama com "instalacao": so cria se a impressora padrao for real
rem (nao PDF24/XPS/OneNote/Fax) e nao para no fim.
set "MODO=%~1"

rem 0 = ja existe e e a mesma impressora da padrao; 2 = existe mas e de outra impressora; 1 = nao existe
powershell -NoProfile -Command "$f = Get-Printer -Name 'EcoPDF Foto' -ErrorAction SilentlyContinue; if (-not $f) { exit 1 }; $p = Get-CimInstance Win32_Printer -Filter 'Default=True'; if ($p -and $p.Name -ne 'EcoPDF Foto' -and $p.Name -notmatch 'pdf|xps|onenote|fax|send to|enviar para' -and ($f.DriverName -ne $p.DriverName -or $f.PortName -ne $p.PortName)) { exit 2 }; exit 0"
if errorlevel 2 goto refazer
if not errorlevel 1 if /i "%MODO%"=="instalacao" exit /b 0
if not errorlevel 1 goto preferencias
goto conferir

:refazer
echo.
echo  A "EcoPDF Foto" atual e de outra impressora ^(a impressora da sala foi trocada^).
echo  Apagando e criando de novo para a impressora padrao ^(o Windows pede permissao^)...
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-Command','Remove-Printer -Name ''EcoPDF Foto'''"

:conferir

powershell -NoProfile -Command "$p = Get-CimInstance Win32_Printer -Filter 'Default=True'; if ($p -and $p.Name -notmatch 'pdf|xps|onenote|fax|send to|enviar para') { exit 0 } else { exit 1 }"
if errorlevel 1 (
  echo.
  echo  A impressora padrao nao e uma impressora de papel ^(ou nao ha impressora padrao^).
  echo  Deixe a impressora da sala como padrao e rode C:\EcoPDF\criar_fila_foto.bat depois.
  if /i not "%MODO%"=="instalacao" pause
  exit /b 0
)

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
  if /i not "%MODO%"=="instalacao" pause
  exit /b 1
)
echo  Criada.

:preferencias
echo.
echo  ============================================================
echo   ULTIMO PASSO: escolher o papel fotografico (uma vez so)
echo  ============================================================
echo.
echo  Quando voce apertar uma tecla, abre a janela "Preferencias de impressao"
echo  da impressora "EcoPDF Foto". Nela:
echo.
echo   1. Na aba "Principal" (ou "Papel/Qualidade"), procure "Tipo de papel".
echo   2. Em "Tipo de papel", escolha o papel fotografico BRILHANTE
echo      (Glossy). Na Epson: "Epson Premium Glossy" ou "Premium Photo Paper Glossy".
echo      NAO escolha "Papel normal" nem "Matte" (fosco).
echo   3. Em "Qualidade", escolha "Alta".
echo   4. Clique em "OK" (embaixo, a direita).
echo.
echo  A impressora padrao continua como esta (papel comum, para o laudo).
echo  Impressora laser ou preto e branco: so clique em "OK".
echo  Se a janela nao aparecer na frente, clique nela na barra de tarefas (embaixo).
echo.
pause
rundll32 printui.dll,PrintUIEntry /e /n "EcoPDF Foto"
echo.
echo  PRONTO. Volte ao EcoPDF e clique em "Imprimir" de novo:
echo  as fotos saem no papel fotografico e o laudo no papel comum.
echo  Para desfazer: apague a impressora "EcoPDF Foto" em Configuracoes ^> Impressoras.
echo.
if /i not "%MODO%"=="instalacao" pause
