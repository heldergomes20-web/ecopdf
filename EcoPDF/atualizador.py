"""Atualizador do EcoPDF (atualizar_agora.bat e voltar.bat).

atualizar_agora.bat          busca a versao mais nova do canal deste PC no GitHub, confere a
                             assinatura (chave publica abaixo), guarda a versao atual em
                             _anterior, instala e confere se o EcoPDF voltou a funcionar.
atualizar_agora.bat teste    idem, e este PC passa a ser de teste (recebe as pre-releases).
atualizar_agora.bat geral    este PC volta ao canal geral (so versoes Latest) e, se estiver numa
                             versao de teste, volta para a Latest (unico caso de rebaixar).
voltar.bat                   devolve a versao guardada em _anterior.

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


def dizer(msg=""):
    print("  " + msg if msg else "", flush=True)


def chave(v):
    return tuple(int(x) for x in v.split("."))


def versao_local():
    try:
        return (BASE / "VERSAO.txt").read_text(encoding="utf-8-sig").strip()
    except OSError:
        return "?"


def ler_canal():
    cfg = configparser.ConfigParser()
    try:
        cfg.read_string("[x]\n" + (BASE / "canal.ini").read_text(encoding="utf-8-sig"))
        c = cfg.get("x", "CANAL", fallback="geral").strip().lower()
    except OSError:
        c = "geral"
    return "teste" if c == "teste" else "geral"


def gravar_canal(c):
    (BASE / "canal.ini").write_text(
        "; Canal de atualizacao deste PC: geral (so versoes Latest) ou teste (recebe as de teste).\n"
        f"CANAL = {c}\n", encoding="utf-8")


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
        return
    dizer("")
    dizer("A versao nova nao voltou a receber imagens. Voltando para a anterior...")
    restaurar_anterior()
    sys.exit(VOLTOU)   # o .bat roda instalar.bat de novo e mostra o status


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
            conferir_depois()
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
