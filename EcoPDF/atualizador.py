"""Atualizador do EcoPDF (atualizar_agora.bat e voltar.bat).

atualizar_agora.bat          busca a versao mais nova do canal deste PC no GitHub, confere a
                             assinatura (chave publica abaixo), guarda a versao atual em
                             _anterior, instala e confere se o EcoPDF novo iniciou (porta de recepcao aberta).
atualizar_agora.bat teste    idem, e este PC passa a ser de teste (recebe as pre-releases).
atualizar_agora.bat geral    este PC volta ao canal geral (so versoes Latest) e, se estiver numa
                             versao de teste, volta para a Latest (unico caso de rebaixar).
voltar.bat                   devolve a versao guardada em _anterior.

Atualizacao automatica (desde 2026.11.04):
  preparar()                   roda DENTRO do EcoPDF (sem administrador), 3 min depois de ligar e a
                               cada 2 h: se ha versao nova no canal, baixa, confere a assinatura e
                               deixa pronta em log/atualizacao_pronta. Nunca instala.
  atualizador.py auto inicio   tarefa "EcoPDF Atualizar ao ligar" (logon, administrador, sem janela)
  atualizador.py auto 6h       tarefa "EcoPDF Atualizar 6h" (todo dia 06:00; so instala entre 05:30
                               e 07:00 e sem exame recebido nos ultimos 15 min)
                               As duas instalam a versao pronta (ou baixam na hora) e chamam o
                               atualizar_auto.bat, que roda o instalar.bat com este Python fechado e
                               depois confere; se a porta DICOM nao abrir em 60 s, volta sozinho e
                               marca a versao como recusada (nao tenta de novo; o painel avisa).
  canal.ini AUTO = nao         desliga a automatica neste PC (a manual continua).

O instalar.bat roda com este Python FECHADO (codigo de saida 10/11 para o .bat), para o pip
poder trocar componentes que o atualizador usa.

O canal fica em canal.ini (deste PC, fora do zip; sem o arquivo = geral).
Nunca rebaixa (so instala versao MAIOR), exceto pelo comando "geral" acima.
"""
import base64
import configparser
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

try:  # certificados do Windows (sem isso o download do GitHub pode falhar em alguns PCs)
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

BASE = Path(__file__).resolve().parent
REPO = "heldergomes20-web/ecopdf"
CHAVE_PUBLICA = "AMnlb2+BTFjFstkWT3MMldMu7Bnfa6Ab0lUKjNWLhFQ="   # chave_publica.txt do pendrive
VERSAO_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}(\.\d+)?$")
ANTERIOR = BASE / "_anterior"
# nunca copiados para _anterior nem sobrescritos: dados e arquivos locais deste PC
LOCAIS = {"venv", "processados", "dicom", "log", "_anterior", "__pycache__", "painel.ini", "canal.ini"}
WINDOWS = os.name == "nt"
INSTALAR, VOLTOU = 10, 11   # codigos de saida lidos pelo .bat
PRONTA = BASE / "log" / "atualizacao_pronta"     # zip + sig ja baixados e conferidos
ESTADO = BASE / "log" / "atualizacao_auto.json"  # o que a automatica fez (painel e status.bat leem)
TRAVA = BASE / "log" / "atualizando.trava"
JANELA_6H = (5 * 60 + 30, 7 * 60)                 # minutos do dia em que a tarefa das 6h pode instalar
SEM_EXAME_MIN = 15


def dizer(msg=""):
    print("  " + msg if msg else "", flush=True)


def chave(v):
    return tuple(int(x) for x in v.split("."))


def versao_local():
    try:
        return (BASE / "VERSAO.txt").read_text(encoding="utf-8-sig").strip()
    except OSError:
        return "?"


def _canal_ini():
    cfg = configparser.ConfigParser()
    try:
        cfg.read_string("[x]\n" + (BASE / "canal.ini").read_text(encoding="utf-8-sig"))
    except (OSError, configparser.Error):
        pass
    return cfg


def ler_canal():
    c = _canal_ini().get("x", "CANAL", fallback="geral").strip().lower()
    return "teste" if c == "teste" else "geral"


def auto_ligada():
    return _canal_ini().get("x", "AUTO", fallback="sim").strip().lower() not in ("nao", "não", "0", "n")


def gravar_canal(c):
    auto = "" if auto_ligada() else "AUTO = nao\n"
    (BASE / "canal.ini").write_text(
        "; Canal de atualizacao deste PC: geral (so versoes Latest) ou teste (recebe as de teste).\n"
        "; AUTO = nao desliga a atualizacao automatica (6h / ao ligar) so neste PC.\n"
        f"CANAL = {c}\n{auto}", encoding="utf-8")


def baixar(url, api=False):
    cab = {"User-Agent": "EcoPDF-atualizador"}
    if api:
        cab["Accept"] = "application/vnd.github+json"
    with urllib.request.urlopen(urllib.request.Request(url, headers=cab), timeout=90) as r:
        return r.read()


def releases():
    dados = json.loads(baixar(f"https://api.github.com/repos/{REPO}/releases?per_page=30", api=True))
    validos = []
    for r in dados:
        tag = r.get("tag_name", "")
        anexos = {a["name"]: a["browser_download_url"] for a in r.get("assets", [])}
        if r.get("draft") or not VERSAO_RE.match(tag) or not {"EcoPDF.zip", "EcoPDF.zip.sig"} <= anexos.keys():
            continue
        validos.append({"tag": tag, "teste": bool(r.get("prerelease")), "anexos": anexos})
    return validos


def melhor(validos, canal):
    lista = [r for r in validos if canal == "teste" or not r["teste"]]
    return max(lista, key=lambda r: chave(r["tag"])) if lista else None


def conferir(zipb, sigb, tag):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(CHAVE_PUBLICA))
    try:
        pub.verify(base64.b64decode(sigb.strip()), zipb)
    except (InvalidSignature, ValueError):
        raise SystemExit("ASSINATURA NAO CONFERE. Nada foi instalado. Encaminhe uma foto desta tela "
                         "para a administracao da clinica.")
    with zipfile.ZipFile(io.BytesIO(zipb)) as z:
        dentro = z.read("EcoPDF/VERSAO.txt").decode("utf-8-sig").strip()
    if dentro != tag:
        raise SystemExit(f"o zip diz versao {dentro} e o release diz {tag}. Nada foi instalado.")


def _itens_codigo(pasta):
    return [p for p in pasta.iterdir() if p.name not in LOCAIS]


def guardar_anterior():
    if ANTERIOR.exists():
        shutil.rmtree(ANTERIOR)
    ANTERIOR.mkdir()
    for p in _itens_codigo(BASE):
        (shutil.copytree if p.is_dir() else shutil.copy2)(p, ANTERIOR / p.name)


def extrair(zipb):
    with zipfile.ZipFile(io.BytesIO(zipb)) as z:
        for info in z.infolist():
            if not info.filename.startswith("EcoPDF/") or info.is_dir():
                continue
            rel = Path(info.filename[len("EcoPDF/"):])
            if not rel.parts or rel.parts[0] in LOCAIS or ".." in rel.parts:
                continue
            destino = BASE / rel
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(z.read(info))


def restaurar_anterior():
    if not ANTERIOR.exists():
        raise SystemExit("nao ha versao guardada em _anterior para voltar.")
    for p in ANTERIOR.iterdir():
        alvo = BASE / p.name
        if p.is_dir():
            if alvo.exists():
                shutil.rmtree(alvo)
            shutil.copytree(p, alvo)
        else:
            shutil.copy2(p, alvo)


def parar():
    if WINDOWS:
        subprocess.run(["schtasks", "/end", "/tn", "EcoPDF"], capture_output=True)
        time.sleep(3)


def porta():
    try:
        for linha in (BASE / "config.ini").read_text(encoding="utf-8-sig").splitlines():
            m = re.match(r"\s*PORTA\s*=\s*(\d+)", linha)
            if m:
                return int(m.group(1))
    except OSError:
        pass
    return 11112


def funcionando(segundos=60):
    if not WINDOWS:
        return True
    fim = time.time() + segundos
    while time.time() < fim:
        try:
            with socket.create_connection(("127.0.0.1", porta()), timeout=2):
                return True
        except OSError:
            time.sleep(3)
    return False


def atualizar(pedido=None):
    if pedido in ("teste", "geral"):
        gravar_canal(pedido)
    canal = ler_canal()
    local = versao_local()
    dizer(f"Versao instalada: {local}    Canal deste PC: {canal}")
    dizer("Procurando versao nova no GitHub...")
    try:
        validos = releases()
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SystemExit(f"sem acesso ao GitHub ({e}). Confira a internet e tente de novo. "
                         "O EcoPDF continua funcionando na versao atual.")
    alvo = melhor(validos, canal)
    if alvo is None:
        dizer("Nenhuma versao publicada neste canal ainda. Nada a fazer.")
        return
    # "atualizar_agora.bat geral" num PC que estava em teste: volta para a Latest (producao),
    # mesmo sendo menor. Unico caso de rebaixar, e so por esse comando.
    retorno = (pedido == "geral" and VERSAO_RE.match(local) and chave(alvo["tag"]) < chave(local))
    if retorno:
        dizer(f"Este PC volta para a versao de producao (Latest): {alvo['tag']}.")
    elif local != "?" and VERSAO_RE.match(local) and chave(alvo["tag"]) <= chave(local):
        dizer(f"Ja esta na versao mais nova do canal {canal} ({local}).")
        if canal == "geral" and chave(alvo["tag"]) < chave(local):
            dizer(f"Este PC esta numa versao de teste. Para voltar a de producao ({alvo['tag']}): "
                  "atualizar_agora.bat geral")
        teste = melhor(validos, "teste")
        if canal == "geral" and teste and chave(teste["tag"]) > chave(local):
            dizer(f"Ha uma versao de teste ({teste['tag']}). So instale num PC de teste: "
                  "atualizar_agora.bat teste")
        return
    tag = alvo["tag"]
    dizer(f"Baixando {tag}{' (teste)' if alvo['teste'] else ''}...")
    zipb = baixar(alvo["anexos"]["EcoPDF.zip"])
    sigb = baixar(alvo["anexos"]["EcoPDF.zip.sig"])
    conferir(zipb, sigb, tag)
    dizer("Assinatura conferida.")
    if not _travar():
        raise SystemExit("ja ha uma atualizacao em andamento neste PC (automatica?). "
                         "Espere 5 minutos e tente de novo.")
    dizer(f"Guardando a versao {local} em _anterior (voltar.bat devolve)...")
    guardar_anterior()
    parar()
    dizer(f"Instalando {tag}...")
    extrair(zipb)
    (BASE / "log").mkdir(exist_ok=True)
    (BASE / "log" / "atualizacao.json").write_text(json.dumps({"de": local, "para": tag}), "utf-8")
    sys.exit(INSTALAR)   # o .bat roda instalar.bat (com este Python fechado) e depois "conferir"


def conferir_depois():
    """Chamado pelo .bat depois do instalar.bat: se o EcoPDF novo nao voltou, devolve o anterior."""
    try:
        info = json.loads((BASE / "log" / "atualizacao.json").read_text("utf-8"))
    except (OSError, ValueError):
        info = {"de": "?", "para": versao_local()}
    if funcionando():
        dizer("")
        dizer(f"ATUALIZADO: {info['de']} -> {info['para']}. EcoPDF funcionando.")
        _limpar_pronta()
        gravar_estado(resultado="instalada", versao=info["para"], de=info["de"],
                      como=info.get("auto") or "manual", recusada="", erro="", pronta="")
        return
    dizer("")
    dizer("O EcoPDF novo nao iniciou (porta de recepcao DICOM fechada depois de 60 s).")
    dizer("Voltando para a versao anterior...")
    restaurar_anterior()
    _limpar_pronta()
    # a automatica nao tenta de novo esta versao (senao instalaria e voltaria a cada inicio do PC)
    gravar_estado(resultado="voltou", recusada=info["para"], versao=info["de"], pronta="",
                  como=info.get("auto") or "manual",
                  erro=f"{info['para']} nao abriu a porta DICOM; voltou para {info['de']}")
    sys.exit(VOLTOU)   # o .bat roda instalar.bat de novo e mostra o status


def _agora():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def ler_estado():
    try:
        return json.loads(ESTADO.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def gravar_estado(**campos):
    d = ler_estado()
    d.update(campos, quando=_agora())
    ESTADO.parent.mkdir(parents=True, exist_ok=True)
    tmp = ESTADO.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
    os.replace(tmp, ESTADO)


def _limpar_pronta():
    shutil.rmtree(PRONTA, ignore_errors=True)


def _pronta():
    """(tag, zip, sig) da versao ja baixada, ou None."""
    try:
        tag = (PRONTA / "VERSAO.txt").read_text("utf-8").strip()
        return tag, (PRONTA / "EcoPDF.zip").read_bytes(), (PRONTA / "EcoPDF.zip.sig").read_bytes()
    except OSError:
        return None


def _mais_nova(tag, local):
    return local == "?" or not VERSAO_RE.match(local) or chave(tag) > chave(local)


def preparar():
    """Dentro do EcoPDF, a cada 2 h: baixa e confere a versao nova do canal; NAO instala.
    Devolve um texto curto para o log. Nunca levanta excecao."""
    try:
        local, canal = versao_local(), ler_canal()
        alvo = melhor(releases(), canal)
        recusada = ler_estado().get("recusada", "")
        if alvo is None or not _mais_nova(alvo["tag"], local):
            _limpar_pronta()
            gravar_estado(checado=_agora(), pronta="", erro_checagem="", canal=canal)
            return f"em dia ({local})"
        tag = alvo["tag"]
        if tag == recusada:
            gravar_estado(checado=_agora(), pronta="", erro_checagem="", canal=canal)
            return f"{tag} foi recusada neste PC (nao abriu a porta DICOM); espera a proxima"
        p = _pronta()
        if p and p[0] == tag:
            gravar_estado(checado=_agora(), erro_checagem="", canal=canal)
            return f"{tag} ja pronta"
        zipb = baixar(alvo["anexos"]["EcoPDF.zip"])
        sigb = baixar(alvo["anexos"]["EcoPDF.zip.sig"])
        conferir(zipb, sigb, tag)
        _limpar_pronta()
        tmp = PRONTA.with_name(PRONTA.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        (tmp / "EcoPDF.zip").write_bytes(zipb)
        (tmp / "EcoPDF.zip.sig").write_bytes(sigb)
        (tmp / "VERSAO.txt").write_text(tag, "utf-8")
        os.replace(tmp, PRONTA)
        gravar_estado(checado=_agora(), pronta=tag, pronta_desde=_agora(), erro_checagem="", canal=canal)
        return f"{tag} baixada e conferida; instala as 6h ou quando o PC ligar"
    except SystemExit as e:   # conferir() avisa assim (assinatura / versao dentro do zip)
        gravar_estado(checado=_agora(), erro_checagem=str(e.code)[:200])
        return f"ERRO: {e.code}"
    except Exception as e:
        gravar_estado(checado=_agora(), erro_checagem=f"{type(e).__name__}: {e}"[:200])
        return f"sem acesso ao GitHub ou erro ({type(e).__name__})"


def _exame_recente():
    """True se chegou imagem nos ultimos SEM_EXAME_MIN minutos (pasta de cada estudo em dicom/)."""
    limite = time.time() - SEM_EXAME_MIN * 60
    try:
        return any(p.stat().st_mtime > limite for p in (BASE / "dicom").iterdir())
    except OSError:
        return False


def _travar():
    """Impede duas instalacoes ao mesmo tempo (manual x automatica). Trava velha (> 30 min) vence."""
    try:
        if TRAVA.exists() and time.time() - TRAVA.stat().st_mtime < 1800:
            return False
        TRAVA.parent.mkdir(parents=True, exist_ok=True)
        TRAVA.write_text(_agora(), "utf-8")
        return True
    except OSError:
        return False


def soltar():
    try:
        TRAVA.unlink()
    except OSError:
        pass


def auto(momento):
    """Tarefa agendada (administrador, sem janela). Sai com INSTALAR se extraiu a versao nova."""
    if not auto_ligada():
        return
    if momento == "6h":
        t = time.localtime()
        minuto = t.tm_hour * 60 + t.tm_min
        if not JANELA_6H[0] <= minuto < JANELA_6H[1]:
            return   # PC acordou atrasado (ex.: estava dormindo): nunca instala no meio do dia
        if _exame_recente():
            gravar_estado(resultado="adiada", erro="exame recebido nos ultimos 15 min as 6h; "
                                                  "instala quando o PC ligar")
            return
    local, canal = versao_local(), ler_canal()
    p = _pronta()
    if p is None or not _mais_nova(p[0], local):
        # nada pronto: tenta baixar agora (ao ligar a rede pode demorar a subir)
        for tentativa in range(4 if momento == "inicio" else 1):
            if tentativa:
                time.sleep(30)
            texto = preparar()
            p = _pronta()
            if p or not texto.startswith("sem acesso"):
                break
    if p is None or not _mais_nova(p[0], local) or p[0] == ler_estado().get("recusada"):
        return
    if not _travar():
        return
    tag, zipb, sigb = p
    try:
        conferir(zipb, sigb, tag)   # de novo: o arquivo ficou no disco desde o download
    except SystemExit as e:
        _limpar_pronta()
        soltar()
        gravar_estado(resultado="erro", erro=str(e.code)[:200])
        return
    dizer(f"{_agora()} automatica ({momento}): {local} -> {tag} (canal {canal})")
    guardar_anterior()
    parar()
    extrair(zipb)
    (BASE / "log" / "atualizacao.json").write_text(
        json.dumps({"de": local, "para": tag, "auto": momento}), "utf-8")
    gravar_estado(resultado="instalando", versao=tag, de=local, como=momento)
    sys.exit(INSTALAR)


def iniciar_auto(momento):
    """Chamado pela tarefa com pythonw (sem janela). Se ha o que instalar, entrega ao
    atualizar_auto.bat numa janela escondida e sai, para o instalar.bat poder trocar componentes."""
    try:
        auto(momento)
    except SystemExit as e:
        if e.code == INSTALAR and WINDOWS:
            registro = open(BASE / "log" / "atualizacao_auto.log", "a", encoding="utf-8")
            subprocess.Popen(["cmd", "/c", str(BASE / "atualizar_auto.bat")], cwd=str(BASE),
                             stdout=registro, stderr=subprocess.STDOUT,
                             creationflags=0x08000000)   # CREATE_NO_WINDOW
            return
        if e.code == INSTALAR:
            return
        raise
    except Exception as e:
        soltar()
        gravar_estado(resultado="erro", erro=f"{type(e).__name__}: {e}"[:200])


def texto_painel():
    """Coluna 'Atualizacao automatica' do painel: uma linha, ERRO/ATENCAO no comeco quando ha o que olhar."""
    e = ler_estado()
    if not auto_ligada():
        return "desligada neste PC (canal.ini AUTO = nao)"
    partes = []
    if e.get("resultado") == "voltou" and e.get("recusada"):
        return f"ERRO: {e.get('erro', '')}"
    if e.get("erro_checagem"):
        partes.append(f"sem checar ({e['erro_checagem'][:60]})")
    if e.get("pronta"):
        partes.append(f"{e['pronta']} pronta desde {str(e.get('pronta_desde', ''))[5:16].replace('T', ' ')}"
                      " (instala 6h ou ao ligar)")
    elif e.get("checado"):
        partes.append(f"em dia, checado {str(e['checado'])[11:16]}")
    if e.get("resultado") in ("instalada", "adiada", "erro") and e.get("quando"):
        rot = {"instalada": f"última: {e.get('de', '?')} -> {e.get('versao', '?')} ({e.get('como', '')})",
               "adiada": "adiada às 6h (exame chegando)", "erro": f"erro: {e.get('erro', '')[:60]}"}
        partes.append(rot[e["resultado"]] + f" em {str(e['quando'])[5:16].replace('T', ' ')}")
    return " · ".join(partes) or "ainda não checou"


def voltar():
    v = (ANTERIOR / "VERSAO.txt")
    antiga = v.read_text(encoding="utf-8-sig").strip() if v.exists() else "?"
    dizer(f"Voltando de {versao_local()} para {antiga}...")
    parar()
    restaurar_anterior()
    sys.exit(VOLTOU)


def main():
    args = [a.lower() for a in sys.argv[1:]]
    try:
        if args[:1] == ["voltar"]:
            voltar()
        elif args[:1] == ["conferir"]:
            try:
                conferir_depois()
            finally:
                soltar()
        elif args[:1] == ["auto"]:
            iniciar_auto(args[1] if len(args) > 1 else "inicio")
        else:
            atualizar(args[0] if args else None)
    except SystemExit as e:
        if isinstance(e.code, str):
            dizer("")
            dizer("ERRO: " + e.code)
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
