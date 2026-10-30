@echo off
net session >nul 2>&1
if errorlevel 1 (
  echo Execute como administrador.
  pause
  exit /b 1
)
schtasks /end /tn EcoPDF >nul 2>&1
schtasks /delete /tn EcoPDF /f
netsh advfirewall firewall delete rule name="EcoPDF DICOM"
echo EcoPDF removido do inicio automatico e do firewall.
echo Suas pastas de laudos e PDFs nao foram tocadas.
pause
