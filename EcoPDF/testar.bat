@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Enviando um exame de TESTE para o EcoPDF deste PC...
venv\Scripts\python.exe teste.py
echo.
echo Em cerca de 3 minutos deve aparecer na pasta "aguardando fotos":
echo   ..._Paciente_Teste_Ecocardiograma.pdf
echo Depois de conferir, apague esse PDF de teste.
pause
