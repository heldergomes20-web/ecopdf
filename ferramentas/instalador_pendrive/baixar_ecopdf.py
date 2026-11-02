"""Instalador do pendrive: baixa o EcoPDF do GitHub, confere a assinatura e poe em C:\\EcoPDF.

Uso (pelo "INSTALAR ECOPDF.bat"): python baixar_ecopdf.py geral|teste [pasta_destino]
  geral = versao de producao (Latest); teste = a mais nova publicada (pre-release ou Latest).
Nada e extraido se a assinatura nao conferir. Dados do PC (processados, dicom, log, venv,
painel.ini) nunca sao tocados. O painel.ini ao lado deste arquivo (no pendrive) e copiado.
Codigo de saida: 0 = pronto para rodar o instalar.bat; 1 = erro (nada mudou).
"""
import base64
import io
import json
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

REPO = "heldergomes20-web/ecopdf"
CHAVE_PUBLICA = "AMnlb2+BTFjFstkWT3MMldMu7Bnfa6Ab0lUKjNWLhFQ="   # a mesma do atualizador.py
VERSAO_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}(\.\d+)?$")
LOCAIS = {"venv", "processados", "dicom", "log", "_anterior", "__pycache__", "painel.ini", "canal.ini"}
AQUI = Path(__file__).resolve().parent


def dizer(m=""):
    print("  " + m if m else "", flush=True)


def baixar(url, api=False):
    cab = {"User-Agent": "EcoPDF-instalador"}
    if api:
        cab["Accept"] = "application/vnd.github+json"
    with urllib.request.urlopen(urllib.request.Request(url, headers=cab), timeout=120) as r:
        return r.read()


def chave(v):
    return tuple(int(x) for x in v.split("."))


def escolher(canal):
    dados = json.loads(baixar(f"https://api.github.com/repos/{REPO}/releases?per_page=30", api=True))
    validos = []
    for r in dados:
        tag = r.get("tag_name", "")
        anexos = {a["name"]: a["browser_download_url"] for a in r.get("assets", [])}
        if r.get("draft") or not VERSAO_RE.match(tag) or not {"EcoPDF.zip", "EcoPDF.zip.sig"} <= anexos.keys():
            continue
        if canal == "geral" and r.get("prerelease"):
            continue
        validos.append((tag, bool(r.get("prerelease")), anexos))
    return max(validos, key=lambda x: chave(x[0])) if validos else None


def conferir(zipb, sigb, tag):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(CHAVE_PUBLICA)).verify(
            base64.b64decode(sigb.strip()), zipb)
    except (InvalidSignature, ValueError):
        sys.exit("ASSINATURA NAO CONFERE. Nada foi instalado. Encaminhe uma foto desta tela "
                 "para a administracao da clinica.")
    with zipfile.ZipFile(io.BytesIO(zipb)) as z:
        dentro = z.read("EcoPDF/VERSAO.txt").decode("utf-8-sig").strip()
    if dentro != tag:
        sys.exit(f"o zip diz versao {dentro} e o release diz {tag}. Nada foi instalado.")


def main():
    canal = (sys.argv[1] if len(sys.argv) > 1 else "geral").lower()
    destino = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(r"C:\EcoPDF")
    dizer(f"Procurando a versao {'de TESTE' if canal == 'teste' else 'de PRODUCAO'} no GitHub...")
    try:
        alvo = escolher(canal)
    except OSError as e:
        sys.exit(f"sem acesso ao GitHub ({e}). Confira a internet e tente de novo.")
    if alvo is None:
        sys.exit("nenhuma versao de producao (Latest) publicada ainda. Rode de novo e escolha T (teste).")
    tag, teste, anexos = alvo
    dizer(f"Baixando {tag}{' (teste)' if teste else ''}...")
    zipb, sigb = baixar(anexos["EcoPDF.zip"]), baixar(anexos["EcoPDF.zip.sig"])
    conferir(zipb, sigb, tag)
    dizer("Assinatura conferida.")

    destino.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        subprocess.run(["schtasks", "/end", "/tn", "EcoPDF"], capture_output=True)
    if (destino / "VERSAO.txt").exists():   # ja tinha EcoPDF: guarda a versao atual (voltar.bat)
        anterior = destino / "_anterior"
        shutil.rmtree(anterior, ignore_errors=True)
        anterior.mkdir()
        for p in destino.iterdir():
            if p.name not in LOCAIS:
                (shutil.copytree if p.is_dir() else shutil.copy2)(p, anterior / p.name)
        dizer(f"Versao anterior guardada em {anterior}.")
    with zipfile.ZipFile(io.BytesIO(zipb)) as z:
        for info in z.infolist():
            if not info.filename.startswith("EcoPDF/") or info.is_dir():
                continue
            rel = Path(info.filename[len("EcoPDF/"):])
            if not rel.parts or rel.parts[0] in LOCAIS or ".." in rel.parts:
                continue
            (destino / rel).parent.mkdir(parents=True, exist_ok=True)
            (destino / rel).write_bytes(z.read(info))
    (destino / "canal.ini").write_text(
        "; Canal de atualizacao deste PC: geral (so versoes Latest) ou teste (recebe as de teste).\n"
        f"CANAL = {canal}\n", encoding="utf-8")
    if (AQUI / "painel.ini").exists():
        shutil.copy2(AQUI / "painel.ini", destino / "painel.ini")
        dizer("painel.ini copiado do pendrive.")
    else:
        dizer("AVISO: nao achei painel.ini ao lado deste instalador; o painel das salas fica desligado.")
    dizer(f"EcoPDF {tag} copiado para {destino} (canal {canal}).")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if isinstance(e.code, str):
            dizer("")
            dizer("ERRO: " + e.code)
            sys.exit(1)
        raise
