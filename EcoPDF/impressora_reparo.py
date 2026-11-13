"""
Autocorrecao da impressora da sala (2026.11.12).

Problema: quando o cabo USB volta numa porta diferente do PC (ou a impressora e trocada),
o Windows cria uma porta nova (USB002) e uma fila nova ("EPSON L3250 Series (Copia 1)").
A impressora de sempre e a "EcoPDF Foto" ficam apontando para a porta que nao existe mais
(USB001) e nada imprime. As vezes o Windows ainda troca a impressora padrao para a copia.

Regra unica: toda fila de um modelo (driver) que esta numa porta USB MORTA passa para a
porta USB VIVA desse mesmo modelo, se houver exatamente uma. Isso vale para a impressora
de sempre e para a EcoPDF Foto. Alem disso, se a EcoPDF Foto virou a impressora padrao,
a impressora da sala volta a ser a padrao.

Porta viva = o Windows marca o aparelho USB como presente (registro DeviceClasses da porta
de impressao USB, "Linked" = 1). Sem essa informacao: fila nao "offline" na porta = viva.
Portas que nao sao USB (rede, WSD, PDF24...) nunca sao mexidas.

O que o programa NAO resolve (vira aviso com passos na janela Imprimir + botao
"Reconfigurar impressora"):
  - nenhuma porta viva do modelo: impressora desligada ou cabo solto;
  - duas portas vivas do mesmo modelo: nao adivinha qual e a da sala;
  - outro modelo conectado: a EcoPDF Foto precisa ser refeita com papel fotografico e
    qualidade Alta escolhidos na janela do driver (criar_fila_foto.bat, aberto pelo botao).

Trocar a porta de uma fila exige administrador. O EcoPDF roda sem: ele pede a tarefa
agendada "EcoPDF Impressora" (criada pelo instalar.bat, roda com privilegio elevado, sem
janela e sem perguntar), que executa "impressora_reparo.py aplicar". Sem a tarefa: o botao
pede a permissao do Windows (UAC). Sem nenhuma das duas: a fila viva vira a padrao (nao
precisa de administrador), para o laudo pelo menos sair.

Desligar: IMPRESSORA_AUTOCORRIGIR = nao no config.ini.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
FILA_FOTO = "EcoPDF Foto"
TAREFA = "EcoPDF Impressora"
VIRTUAIS = re.compile(r"pdf|xps|onenote|fax|send to|enviar para", re.I)
COPIA = re.compile(r"\((c[oó]pia|copy)\s*\d*\)\s*$", re.I)
RESULTADO = Path(tempfile.gettempdir()) / "ecopdf_impressora.json"
INTERVALO_S = 5 * 60
CACHE_S = 20
JOB_PARADO_MIN = 10
SEM_JANELA = 0x08000000
ADMIN = ("porta", "spooler", "fila", "online")   # acoes que precisam de administrador
PEDIDO = Path(tempfile.gettempdir()) / "ecopdf_impressora_pedido.json"   # botao pede "fila" para a tarefa
# "Deixar o Windows gerenciar a impressora padrao" DESLIGADO: com ele ligado, a padrao vira a ultima
# usada (a Wi-Fi, a PDF24...). Por usuario (HKCU), nao precisa de administrador.
LEGADO_PS = ("Set-ItemProperty -LiteralPath 'HKCU:\\Software\\Microsoft\\Windows NT\\CurrentVersion\\Windows' "
             "-Name LegacyDefaultPrinterMode -Value 1 -Type DWord")

PS_DIAG = r"""
$ErrorActionPreference = 'SilentlyContinue'
$sp = [string](Get-Service Spooler).Status
$agora = Get-Date
$jobs = @(Get-CimInstance Win32_PrintJob | ForEach-Object {
  [pscustomobject]@{ impressora = [string](($_.Name -split ',')[0]); doc = [string]$_.Document;
                     min = [int]($agora - $_.TimeSubmitted).TotalMinutes } })
$imps = @(Get-CimInstance Win32_Printer | ForEach-Object {
  [pscustomobject]@{ nome = [string]$_.Name; driver = [string]$_.DriverName; porta = [string]$_.PortName;
                     padrao = [bool]$_.Default; offline = [bool]($_.WorkOffline -or $_.PrinterStatus -eq 7) } })
$k = 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceClasses\{28d78fad-5a12-11d1-ae5b-0000f803a8c2}'
# dispositivos conectados agora (Win32_PnPEntity so lista os presentes; nao precisa de administrador).
# O "Linked" do registro vem vazio para usuario comum (13/11, sala 6): presenca do aparelho decide.
$pres = @{}
Get-CimInstance Win32_PnPEntity | ForEach-Object { if ($_.DeviceID) { $pres[$_.DeviceID.ToUpper()] = 1 } }
$portas = @(Get-ChildItem -LiteralPath $k | ForEach-Object {
  $inst = [string](Get-ItemProperty -LiteralPath $_.PSPath).DeviceInstance
  $dp = Get-ItemProperty -LiteralPath ($_.PSPath + '\#\Device Parameters')
  if ($dp -and $dp.'Port Number' -ne $null) {
    $base = if ($dp.'Base Name') { [string]$dp.'Base Name' } else { 'USB' }
    $c = Get-ItemProperty -LiteralPath ($_.PSPath + '\#\Control')
    [pscustomobject]@{ porta = ('{0}{1:D3}' -f $base, [int]$dp.'Port Number');
                       viva = $(if ($c -and $c.Linked -ne $null) { [bool]($c.Linked -eq 1) } elseif ($inst) { $pres.ContainsKey($inst.ToUpper()) } else { $null }); descricao = [string]$dp.'Port Description' }
  } })
$leg = (Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Windows').LegacyDefaultPrinterMode
[pscustomobject]@{ impressoras = $imps; portas = $portas; windows_gerencia = ($leg -ne 1); spooler = $sp; trabalhos = $jobs } | ConvertTo-Json -Compress -Depth 4
"""

_trava = threading.Lock()
_cache = {"em": 0.0, "r": None}
ULTIMA = {"quando": "", "o_que": ""}   # ultima correcao feita (coluna do painel)


# --------------------------------------------------------------------------- decisao (sem Windows)
def _lista(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _usb(porta):
    return (porta or "").upper().startswith("USB")


def _real(p):
    return p["nome"].casefold() != FILA_FOTO.casefold() and not VIRTUAIS.search(p["nome"]) \
        and not VIRTUAIS.search(p.get("porta") or "")


def portas_vivas(diag):
    """{porta: True/False} das portas USB conhecidas. Registro manda; sem registro, a fila diz."""
    vivas = {}
    for r in _lista(diag.get("portas")):
        nome = (r.get("porta") or "").upper()
        # viva None = o Windows nao deixou ler o "Linked" (usuario sem administrador, 13/11 sala 6): nao sei
        # -> fica para a situacao da fila, abaixo (antes contava como desligada e dava aviso falso)
        if nome and r.get("viva") is not None:
            vivas[nome] = vivas.get(nome, False) or bool(r.get("viva"))
    for p in _lista(diag.get("impressoras")):
        porta = (p.get("porta") or "").upper()
        if _usb(porta) and porta not in vivas:
            # porta sem registro: so a situacao da fila (offline = aparelho ausente no Windows 10/11)
            vivas[porta] = not p.get("offline")
    return vivas


def _viva(porta, vivas):
    return True if not _usb(porta) else vivas.get(porta.upper(), True)


def _portas_vivas_do_modelo(driver, imps, diag, vivas):
    achadas = {p["porta"].upper() for p in imps
               if p.get("driver") == driver and _usb(p.get("porta")) and _viva(p["porta"], vivas)}
    # porta viva sem fila (a copia foi apagada): descricao do aparelho parecida com o driver
    usadas = {(p.get("porta") or "").upper() for p in imps}
    d = (driver or "").casefold().replace(" series", "")
    for r in _lista(diag.get("portas")):
        porta, desc = (r.get("porta") or "").upper(), (r.get("descricao") or "").casefold().replace(" series", "")
        if r.get("viva") and porta not in usadas and desc and d and (desc in d or d in desc):
            achadas.add(porta)
    return sorted(achadas)


def planejar(diag):
    """Decide o que fazer. Devolve {"estado", "acoes", "impressora", "modelo"}.
    estado: ok | corrigir | desligada | duas | outro_modelo | sem_impressora | virtual
    acoes: [("padrao", nome)] e [("porta", fila, porta_nova)] (porta exige administrador)."""
    # servico de impressao do Windows parado: nada imprime e a lista de impressoras vem vazia
    if diag.get("spooler") and diag["spooler"] != "Running":
        return {"estado": "corrigir", "acoes": [("spooler",)], "impressora": "", "modelo": ""}
    imps = [dict(p, porta=p.get("porta") or "", driver=p.get("driver") or "")
            for p in _lista(diag.get("impressoras")) if p.get("nome")]
    vivas = portas_vivas(diag)
    acoes = []
    padrao = next((p for p in imps if p.get("padrao")), None)
    foto = next((p for p in imps if p["nome"].casefold() == FILA_FOTO.casefold()), None)

    # EcoPDF Foto como padrao: a impressora da sala (mesmo modelo, a original antes da copia) volta
    if padrao and foto and padrao is foto:
        irmas = [p for p in imps if _real(p) and p["driver"] == foto["driver"]]
        irmas.sort(key=lambda p: (not _viva(p["porta"], vivas), bool(COPIA.search(p["nome"])), len(p["nome"])))
        if irmas:
            padrao = irmas[0]
            acoes.append(("padrao", padrao["nome"], "foto"))
    if not padrao:
        return {"estado": "sem_impressora", "acoes": acoes, "impressora": "", "modelo": ""}
    # PDF24 (ou outra virtual) como padrao: a USB da sala volta a ser a padrao (Helder 13/11: "pode retirar
    # o pdf24 como padrao. Ele so e usado pra isso" = o Medware imprime nele escolhendo pelo nome).
    virtual = not _real(padrao)

    # padrao numa fila de REDE (Wi-Fi/WSD/IP) com impressora USB no PC: a USB e a da sala e volta a ser
    # a padrao (Helder 12/11: "e bem frequente a impressora padrao passar para a impressora wifi").
    # So se houver UMA porta USB viva (ou que vai ser corrigida); a fila preferida e a original, antes da
    # "(Copia N)".
    estado_rede = ""
    # Referencia da "impressora da sala" = o modelo da EcoPDF Foto numa porta USB (criada na instalacao a
    # partir dela). Sem EcoPDF Foto USB, nao mexe: a sala pode usar so a Wi-Fi, ou a USB ser outra coisa.
    if (virtual or not _usb(padrao["porta"])) and foto and _usb(foto["porta"]):
        usb = [p for p in imps if _real(p) and _usb(p["porta"]) and p["driver"] == foto["driver"]]
        def porta_efetiva(p):
            if _viva(p["porta"], vivas):
                return p["porta"].upper()
            alvo = _portas_vivas_do_modelo(p["driver"], imps, diag, vivas)
            return alvo[0] if len(alvo) == 1 else ""
        efetivas = {p["nome"]: porta_efetiva(p) for p in usb}
        portas = {v for v in efetivas.values() if v}
        if len(portas) == 1:
            cand = [p for p in usb if efetivas[p["nome"]]]
            cand.sort(key=lambda p: (bool(COPIA.search(p["nome"])), not _viva(p["porta"], vivas), len(p["nome"])))
            de_rede = padrao["nome"]
            padrao = cand[0]
            acoes.append(("padrao", padrao["nome"], "virtual" if virtual else "rede", de_rede))
            virtual = False
        elif usb and not portas and virtual:
            cand = sorted(usb, key=lambda p: (bool(COPIA.search(p["nome"])), len(p["nome"])))
            return {"estado": "desligada", "acoes": acoes, "impressora": cand[0]["nome"], "modelo": cand[0]["driver"]}
        elif usb and not portas and not virtual:
            estado_rede = "rede_sem_usb"     # a USB da sala esta desligada: imprimiria em outra impressora
        elif len(portas) > 1 and not virtual:
            estado_rede = "duas"
    if virtual:
        return {"estado": "virtual", "acoes": acoes, "impressora": padrao["nome"], "modelo": padrao["driver"]}

    # regra unica: fila do modelo em porta USB morta -> a unica porta USB viva do modelo
    estado = "ok"
    modelos = {p["driver"] for p in imps if p["driver"]}
    destino_padrao = padrao["porta"]
    for drv in sorted(modelos):
        mortas = [p for p in imps if p["driver"] == drv and _usb(p["porta"]) and not _viva(p["porta"], vivas)
                  and (_real(p) or p is foto)]
        if not mortas:
            continue
        alvo = _portas_vivas_do_modelo(drv, imps, diag, vivas)
        if len(alvo) == 1:
            for p in mortas:
                acoes.append(("porta", p["nome"], alvo[0]))
                if p is padrao:
                    destino_padrao = alvo[0]
        elif drv == padrao["driver"] and padrao in mortas:
            estado = "desligada" if not alvo else "duas"

    # EcoPDF Foto do mesmo modelo em outra porta (ex.: a padrao ja e a copia na porta nova)
    if foto and foto["driver"] == padrao["driver"] and foto["porta"].upper() != destino_padrao.upper() \
            and _usb(destino_padrao) and not any(a[0] == "porta" and a[1] == foto["nome"] for a in acoes) \
            and _viva(destino_padrao, vivas):
        acoes.append(("porta", foto["nome"], destino_padrao))
    # fila marcada "offline" no Windows (WorkOffline) com a impressora respondendo no USB (registro diz que a
    # porta esta ligada): o Windows nao tira a marca sozinho e nada imprime -> volta para online
    # (13/11, Helder: "clicando em reconfigurar ... ele nao resolve, o aviso nao some")
    if estado == "ok":
        reg = {(r.get("porta") or "").upper() for r in _lista(diag.get("portas")) if r.get("viva")}
        movidas = {a[1] for a in acoes if a[0] == "porta"}
        for p in imps:
            if p.get("offline") and _usb(p["porta"]) and p["porta"].upper() in reg and p["nome"] not in movidas \
                    and p["driver"] == padrao["driver"] and (_real(p) or p is foto):
                acoes.append(("online", p["nome"]))
    if estado == "ok" and estado_rede:
        estado = estado_rede
    if estado == "ok" and foto and foto["driver"] != padrao["driver"]:
        estado = "outro_modelo"
    if diag.get("windows_gerencia") and _real(padrao):
        acoes.insert(0, ("legado",))
    if estado == "ok" and acoes:
        estado = "corrigir"
    return {"estado": estado, "acoes": acoes, "impressora": padrao["nome"], "modelo": padrao["driver"]}


def _desc(a):
    if a[0] == "porta":
        return f"{a[1]} -> porta {a[2]}"
    if a[0] == "padrao":
        return f"{a[1]} como padrao" + (f" (era {a[3]}, {a[2]})" if len(a) > 3 else "")
    if a[0] == "spooler":
        return "servico de impressao do Windows iniciado"
    if a[0] == "fila":
        return "impressoes paradas canceladas e servico reiniciado"
    if a[0] == "online":
        return f"{a[1]} estava marcada offline no Windows -> online"
    return "Windows deixa de escolher a padrao sozinho"


def texto(estado, impressora="", modelo="", corrigido=False, de=""):
    """Aviso para a janela Imprimir: (grave, texto, mostrar_botao)."""
    if estado == "desligada":
        return True, (f"A impressora {impressora} não está respondendo.\n"
                      "1. Confira se ela está ligada.\n"
                      "2. Confira o cabo USB nos dois lados (impressora e computador).\n"
                      "3. Clique em Reconfigurar impressora. Se acabou de ligar o cabo, espere 1 minuto antes "
                      "(o Windows instala a impressora sozinho)."), True
    if estado == "rede_sem_usb":
        return True, (f"A impressora padrão está em {impressora}, pela rede/Wi-Fi: se imprimir agora, pode sair "
                      "em outra impressora. A impressora ligada neste computador pelo cabo USB não está respondendo.\n"
                      "1. Confira se ela está ligada.\n"
                      "2. Confira o cabo USB nos dois lados (impressora e computador).\n"
                      "3. Clique em Reconfigurar impressora."), True
    if estado == "duas":
        return True, (f"Há duas impressoras {modelo} ligadas neste computador e o EcoPDF não sabe qual é a "
                      "da sala. Deixe só a da sala no cabo USB e clique em Reconfigurar impressora."), True
    if estado == "outro_modelo":
        return False, ("Outra impressora foi ligada neste computador. O laudo sai normal; para as fotos "
                       "saírem em papel fotográfico, clique em Reconfigurar impressora e siga a janela que "
                       "abrir."), True
    if estado == "sem_permissao":
        return False, ("O EcoPDF precisa da permissão do Windows para corrigir a impressora.\n"
                       "1. Clique em Reconfigurar impressora.\n"
                       "2. Se aparecer a pergunta do Windows \"Deseja permitir que este aplicativo faça alterações\", "
                       "clique em Sim.\n"
                       "3. Espere a mensagem verde \"pode imprimir\" (até 1 minuto)."), True
    if estado == "virtual":
        return False, (f"A impressora padrão do Windows é a {impressora}, que não é de papel: o EcoPDF abre a "
                       "janela do navegador para imprimir. Para imprimir direto, deixe a impressora da sala como "
                       "padrão no Windows."), False
    if estado == "sem_impressora":
        return True, ("Este computador está sem impressora padrão. Confira se a impressora está ligada e com o "
                      "cabo USB, espere 1 minuto e clique em Reconfigurar impressora."), True
    if corrigido == "virtual":
        return False, (f"A impressora padrão estava na {de}, que não imprime em papel. O EcoPDF voltou para "
                       f"{impressora}, a impressora ligada neste computador: pode imprimir."), False
    if corrigido == "online":
        return False, (f"A impressora {impressora} estava marcada como offline no Windows, mas está ligada. "
                       "O EcoPDF já corrigiu: pode imprimir."), False
    if corrigido == "spooler":
        return False, ("O serviço de impressão do Windows estava parado. O EcoPDF ligou de novo: pode imprimir."), False
    if corrigido == "rede":
        return False, (f"A impressora padrão tinha mudado para {de} (pela rede/Wi-Fi). O EcoPDF voltou para "
                       f"{impressora}, a impressora ligada neste computador: pode imprimir."), False
    if corrigido in ("padrao", "foto"):
        return False, ("A EcoPDF Foto estava como impressora padrão. O EcoPDF voltou a impressora da sala como "
                       "padrão: pode imprimir."), False
    if corrigido:
        return False, "A impressora tinha mudado de porta USB. O EcoPDF já corrigiu: pode imprimir.", False
    return False, "", False


# --------------------------------------------------------------------------- Windows
def _ps(script, timeout=40):
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                       capture_output=True, text=True, timeout=timeout, creationflags=SEM_JANELA)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def diagnosticar():
    _, out, _ = _ps(PS_DIAG)
    return json.loads(out or "{}")


def _q(s):
    return "'" + str(s).replace("'", "''") + "'"


def _script_acoes(acoes, so_padrao=False):
    linhas = ["$ErrorActionPreference = 'Stop'", "$r = @()"]
    for a in acoes:
        if a[0] == "padrao":
            linhas.append(f"try {{ (New-Object -ComObject WScript.Network).SetDefaultPrinter({_q(a[1])}); "
                          f"$r += 'padrao ok' }} catch {{ $r += 'padrao erro: ' + $_ }}")
        elif a[0] == "spooler":
            linhas.append("try { Start-Service Spooler; $r += 'servico de impressao iniciado' } "
                          "catch { $r += 'spooler erro: ' + $_ }")
        elif a[0] == "fila":
            # botao: cancela as impressoes paradas ha mais de 10 min (de qualquer programa) e reinicia o
            # servico de impressao do Windows (destrava fila presa)
            linhas.append(f"try {{ $n = 0; Get-Printer | ForEach-Object {{ Get-PrintJob -PrinterName $_.Name | "
                          f"Where-Object {{ $_.SubmittedTime -lt (Get-Date).AddMinutes(-{JOB_PARADO_MIN}) }} | "
                          "ForEach-Object { Remove-PrintJob -InputObject $_; $n++ } }; $r += \"$n impressoes paradas canceladas\" } "
                          "catch { $r += 'cancelar erro: ' + $_ }")
            linhas.append("try { Restart-Service Spooler -Force; $r += 'servico de impressao reiniciado' } "
                          "catch { $r += 'reiniciar erro: ' + $_ }")
        elif a[0] == "online":
            linhas.append(f"try {{ Get-CimInstance Win32_Printer | Where-Object {{ $_.Name -eq {_q(a[1])} }} | "
                          "Set-CimInstance -Property @{ WorkOffline = $false }; $r += 'online ok' } "
                          "catch { $r += 'online erro: ' + $_ }")
        elif a[0] == "legado":
            linhas.append("try { " + LEGADO_PS + "; $r += 'windows nao escolhe mais a padrao' } "
                          "catch { $r += 'legado erro: ' + $_ }")
        elif a[0] == "porta" and not so_padrao:
            n, p = _q(a[1]), _q(a[2])
            linhas.append(f"try {{ Set-Printer -Name {n} -PortName {p}; $r += 'porta ok' }} "
                          f"catch {{ $r += 'porta erro: ' + $_ }}")
            # envios do EcoPDF presos na porta morta: cancelar, para nao sair tudo de uma vez
            linhas.append(f"try {{ Get-PrintJob -PrinterName {n} | Where-Object {{ $_.DocumentName -like 'EcoPDF -*' "
                          f"-and $_.SubmittedTime -lt (Get-Date).AddMinutes(-{JOB_PARADO_MIN}) }} | Remove-PrintJob }} "
                          "catch {}")
    linhas.append("$r -join ' | '")
    return "\n".join(linhas)


def aplicar(acoes):
    """Executa as acoes neste processo. Devolve o texto do PowerShell."""
    if not acoes:
        return ""
    _, out, err = _ps(_script_acoes(acoes))
    return out or err


def _admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _esperar_resultado(desde, limite):
    fim = time.time() + limite
    while time.time() < fim:
        try:
            if RESULTADO.exists() and RESULTADO.stat().st_mtime >= desde:
                return json.loads(RESULTADO.read_text("utf-8"))
        except (OSError, ValueError):
            pass
        time.sleep(1)
    return None


def _pela_tarefa(log):
    """Roda 'impressora_reparo.py aplicar' pela tarefa elevada. True se ela respondeu."""
    desde = time.time() - 1
    r = subprocess.run(["schtasks", "/run", "/tn", TAREFA], capture_output=True, text=True,
                       creationflags=SEM_JANELA)
    if r.returncode != 0:
        log.info("Impressora: tarefa '%s' indisponivel (%s)", TAREFA, (r.stderr or r.stdout or "").strip()[:120])
        return None
    return _esperar_resultado(desde, 45)


def _pelo_uac(log):
    """Pede a permissao do Windows (so com pessoa na frente: botao)."""
    import ctypes
    desde = time.time() - 1
    exe = sys.executable
    ok = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, f'"{BASE / "impressora_reparo.py"}" aplicar',
                                             str(BASE), 0)
    if ok <= 32:
        log.info("Impressora: permissao de administrador recusada ou indisponivel (%s)", ok)
        return None
    return _esperar_resultado(desde, 90)


def _executar(plano, log, interativo):
    """Aplica o plano com o caminho de privilegio disponivel. Devolve True se as portas foram trocadas."""
    portas = [a for a in plano["acoes"] if a[0] in ADMIN]
    if not portas:
        log.info("Impressora: %s", aplicar(plano["acoes"]))
        return True
    if _admin():
        log.info("Impressora: %s", aplicar(plano["acoes"]))
        return True
    res = _pela_tarefa(log)
    if res is None and interativo:
        res = _pelo_uac(log)
    if res is not None:
        log.info("Impressora (administrador): %s", res.get("saida", ""))
        return True
    # sem administrador: pelo menos o laudo sai pela fila que esta viva
    viva = _fila_viva_do_modelo(plano)
    if viva:
        log.info("Impressora: sem permissao para trocar a porta; %s vira a padrao (%s)", viva,
                 aplicar([("padrao", viva)]))
    return False


def _fila_viva_do_modelo(plano):
    try:
        diag = diagnosticar()
    except Exception:
        return ""
    vivas = portas_vivas(diag)
    for p in _lista(diag.get("impressoras")):
        if p.get("driver") == plano["modelo"] and _real(p) and _usb(p.get("porta")) and _viva(p["porta"], vivas):
            return p["nome"]
    return ""


def reparar(log, cfg=None, interativo=False, forcar=False):
    """Confere e corrige. Devolve {"estado", "corrigido", "grave", "texto", "botao", "impressora"}.
    Guardado por 20 s (a tela chama ao abrir a janela Imprimir e antes de imprimir)."""
    vazio = {"estado": "ok", "corrigido": False, "grave": False, "texto": "", "botao": False, "impressora": ""}
    if os.name != "nt" or (cfg is not None and not cfg.get("impressora_autocorrigir", True)):
        return vazio
    with _trava:
        if not forcar and _cache["r"] is not None and time.time() - _cache["em"] < CACHE_S:
            return _cache["r"]
        try:
            plano = planejar(diagnosticar())
            corrigido = False
            estado = plano["estado"]
            acoes_feitas = list(plano["acoes"])
            if plano["acoes"]:
                log.info("Impressora: corrigindo %s", "; ".join(_desc(a) for a in plano["acoes"]))
                corrigido = _executar(plano, log, interativo)
                if corrigido:
                    feitas = [a for a in plano["acoes"] if a[0] != "legado"]
                    if feitas:
                        ULTIMA.update(quando=time.strftime("%d/%m %H:%M"),
                                      o_que="; ".join(_desc(a) for a in feitas)[:120])
                novo = planejar(diagnosticar())
                estado = novo["estado"]
                if not corrigido or any(a[0] in ADMIN for a in novo["acoes"]):
                    estado, corrigido = ("sem_permissao" if estado in ("ok", "corrigir") else estado), False
                plano = novo
            if estado == "corrigir":
                estado = "ok"
            tipo, de = "", ""
            if corrigido:
                if any(a[0] == "spooler" for a in acoes_feitas):
                    tipo = "spooler"
                elif any(a[0] == "online" for a in acoes_feitas) and not any(a[0] == "porta" for a in acoes_feitas):
                    tipo = "online"
                elif any(a[0] == "porta" for a in acoes_feitas):
                    tipo = "porta"
                else:
                    pad = next((a for a in acoes_feitas if a[0] == "padrao"), None)
                    if pad:
                        tipo, de = (pad[2] if len(pad) > 2 else "foto"), (pad[3] if len(pad) > 3 else "")
            grave, txt, botao = texto(estado, plano["impressora"], plano["modelo"], tipo, de)
            r = {"estado": estado, "corrigido": corrigido, "grave": grave, "texto": txt, "botao": botao,
                 "impressora": plano["impressora"]}
            if estado == "ok" and not corrigido and (cfg is None or cfg.get("impressao_direta", True)):
                falta = _falta_fila_foto()
                if falta:
                    r = {**r, "estado": "sem_fila_foto", "botao": True, "impressora": falta, "texto": TEXTO_SEM_FOTO}
        except Exception:
            log.exception("Impressora: autocorrecao falhou")
            r = vazio
        _cache.update(em=time.time(), r=r)
        return r


def presos(diag, minutos=JOB_PARADO_MIN):
    """Impressoes paradas na fila ha mais de N minutos: [(impressora, documento, minutos)]."""
    return [(j.get("impressora", ""), j.get("doc", ""), int(j.get("min") or 0))
            for j in _lista(diag.get("trabalhos")) if int(j.get("min") or 0) > minutos]


def destravar(log, interativo=True):
    """Cancela impressoes paradas ha mais de 10 min e reinicia o servico de impressao (administrador)."""
    plano = {"acoes": [("fila",)], "modelo": ""}
    if _admin():
        log.info("Impressora: destravar fila: %s", aplicar(plano["acoes"]))
        return True
    try:
        PEDIDO.write_text(json.dumps({"fila": True, "em": time.time()}), "utf-8")
    except OSError:
        pass
    res = _pela_tarefa(log)
    if res is None and interativo:
        res = _pelo_uac(log)
    try:
        PEDIDO.unlink()
    except OSError:
        pass
    if res is None:
        return False
    log.info("Impressora (administrador): destravar fila: %s", res.get("saida", ""))
    return "reiniciado" in str(res.get("saida", ""))


def reconfigurar(log, cfg=None):
    """Botao "Reconfigurar impressora": corrige com permissao (UAC se preciso); outro modelo ou
    EcoPDF Foto faltando = abre o criar_fila_foto.bat (refaz a fila e abre as preferencias)."""
    try:
        parados = presos(diagnosticar())
    except Exception:
        parados = []
    if parados:
        n = len(parados)
        log.info("Impressora: botao Reconfigurar com %d impressao(oes) parada(s): %s", n,
                 "; ".join(f"{a} ({c} min)" for a, _d, c in parados)[:200])
        if destravar(log):
            time.sleep(3)
            r = reparar(log, cfg, interativo=True, forcar=True)
            extra = f" {r['texto']}" if r.get("texto") and r.get("grave") else ""
            return {**r, "texto": (f"Cancelei {n} impressão(ões) parada(s) e reiniciei o serviço de impressão do "
                                   "Windows. Clique em Imprimir de novo." + extra)}
        return {"estado": "sem_permissao", "grave": True, "botao": True, "corrigido": False, "impressora": "",
                "texto": ("Há impressões paradas na fila e o EcoPDF não conseguiu destravar sozinho. Clique de novo "
                          "em Reconfigurar impressora e, se o Windows pedir permissão, clique em Sim.")}
    r = reparar(log, cfg, interativo=True, forcar=True)
    if r["estado"] in ("outro_modelo", "sem_fila_foto") or (r["estado"] == "ok" and not _existe_fila_foto()):
        bat = BASE / "criar_fila_foto.bat"
        try:
            os.startfile(str(bat))
        except OSError as e:
            return {**r, "texto": f"Não consegui abrir o criar_fila_foto.bat ({e})."}
        return {**r, "grave": False, "botao": False,
                "texto": "Abriu uma janela preta: siga o que ela pede (o Windows pede permissão: clique em Sim). "
                         "Na janela da impressora, em Tipo de papel escolha o papel fotográfico brilhante (Glossy) "
                         "e em Qualidade escolha Alta. Clique OK e depois em Imprimir de novo."}
    if r["estado"] == "ok":
        return {**r, "texto": "Impressora conferida: está tudo certo, pode imprimir."}
    return r


TEXTO_SEM_FOTO = (
    "As fotos estão saindo sem a configuração de papel fotográfico (falta preparar a impressora uma vez). "
    "Coloque o papel fotográfico e clique em Reconfigurar impressora:\n"
    "1. Abre uma janela preta: aperte uma tecla quando ela pedir. Se o Windows pedir permissão, clique em Sim.\n"
    "2. Na janela da impressora que abrir, em Tipo de papel escolha o papel fotográfico brilhante "
    "(Glossy, ex.: Epson Premium Glossy / Premium Photo Paper Glossy) e em Qualidade escolha Alta.\n"
    "3. Clique OK. É uma vez só: depois as fotos saem sempre em papel foto.")


def _falta_fila_foto():
    """Nome da impressora padrao se ela e de papel (com papel fotografico no driver) e nao existe a
    EcoPDF Foto; '' se nao falta (ou nao deu para saber: nunca avisa a toa)."""
    try:
        if _existe_fila_foto():
            return ""
        import impressao
        nome = impressao.impressora_padrao()
        if not nome or impressao.VIRTUAIS.search(nome) or nome.casefold() == FILA_FOTO.casefold():
            return ""
        return nome if impressao.modo_foto(nome)[0] is not None else ""
    except Exception:
        return ""


def _existe_fila_foto():
    try:
        return any(p.get("nome", "").casefold() == FILA_FOTO.casefold()
                   for p in _lista(diagnosticar().get("impressoras")))
    except Exception:
        return True


def iniciar(cfg, log, parar):
    """Confere a cada 5 min em segundo plano (vale tambem para impressao pelo Medware)."""
    if os.name != "nt" or not cfg.get("impressora_autocorrigir", True):
        return

    def rodar():
        parar.wait(60)
        while not parar.is_set():
            reparar(log, cfg, forcar=True)
            parar.wait(INTERVALO_S)
    threading.Thread(target=rodar, daemon=True, name="impressora").start()


# --------------------------------------------------------------------------- tarefa elevada
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "aplicar":
    try:
        p = planejar(diagnosticar())
        try:
            pedido = json.loads(PEDIDO.read_text("utf-8")) if PEDIDO.exists() else {}
            PEDIDO.unlink()
        except (OSError, ValueError):
            pedido = {}
        if pedido.get("fila") and time.time() - float(pedido.get("em", 0)) < 120:
            p["acoes"].append(("fila",))
        saida = aplicar(p["acoes"]) if p["acoes"] else "nada a fazer"
    except Exception as e:      # noqa: BLE001 - o resultado vai para quem pediu
        saida = f"erro: {e}"
    RESULTADO.write_text(json.dumps({"saida": saida, "em": time.time()}), "utf-8")
