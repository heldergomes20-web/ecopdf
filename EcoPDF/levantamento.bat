@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Levantamento do Medware e das medidas dos aparelhos (so estrutura e contagens,
echo nunca nome ou valor de paciente). Use fora do horario de exames.
echo.
venv\Scripts\python.exe levantamento.py
echo.
echo O resultado esta na aba "Levantamento" da planilha do painel.
pause
