@echo off
rem Atualizacao automatica: chamado (sem janela) pelo atualizador.py quando a tarefa
rem "EcoPDF Atualizar 6h" ou "EcoPDF Atualizar ao ligar" ja extraiu a versao nova.
rem Nao abra este arquivo a mao: para atualizar agora use atualizar_agora.bat.
rem
rem Roda a partir de uma COPIA em log\ porque a atualizacao troca este proprio arquivo
rem (o Windows le o .bat aos poucos enquanto executa).
if /i not "%~1"=="copia" (
  copy /y "%~f0" "%~dp0log\_atualizar_auto_execucao.bat" >nul
  "%~dp0log\_atualizar_auto_execucao.bat" copia "%~dp0."
)
cd /d "%~2"
set ECOPDF_AUTO=1
rem espera o pythonw que chamou este arquivo fechar (o pip pode precisar trocar componentes dele)
ping -n 6 127.0.0.1 >nul
echo %date% %time% instalando...
call "%~2\instalar.bat" auto
venv\Scripts\python.exe atualizador.py conferir
if errorlevel 11 goto voltou
echo %date% %time% fim
exit /b 0
:voltou
echo %date% %time% voltou para a versao anterior; reinstalando
call "%~2\instalar.bat" auto
exit /b 0
