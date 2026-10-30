"""Assinador do EcoPDF: gera o par de chaves, assina cada versao publicada
no GitHub (Releases) e confere o que esta publicado.

Uso (pelos .bat desta pasta):
  gerar_chaves.bat              cria chave_privada.pem (com senha) e chave_publica.txt
  assinar.bat [EcoPDF.zip]      cria EcoPDF.zip.sig ao lado do zip (para anexar ao release)
  verificar.bat / verificar_teste.bat  confere o release geral (Latest) ou de teste
  reverter.bat <antiga> <nova>  refaz o zip de uma versao antiga com numero novo, ja assinado
"""
import base64
import configparser
import getpass
import hashlib
import io
import re
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

PASTA = Path(__file__).resolve().parent
CHAVE_PRIVADA = PASTA / "chave_privada.pem"
CHAVE_PUBLICA = PASTA / "chave_publica.txt"
PUBLICADOS = PASTA / "publicados"
ULTIMA = PUBLICADOS / "ultima_versao.txt"

VERSAO_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}(\.\d+)?$")
OBRIGATORIOS = ["EcoPDF/VERSAO.txt", "EcoPDF/ecopdf.py", "EcoPDF/instalar.bat",
                "EcoPDF/config.ini", "EcoPDF/requirements.txt"]


def falha(msg):
    print("\n  ERRO: " + msg + "\n")
    sys.exit(1)


def ok(msg):
    print("  [OK]    " + msg)


def ruim(msg):
    print("  [FALHA] " + msg)


def chave_versao(v):
    return tuple(int(p) for p in v.split("."))


def repo():
    cfg = configparser.ConfigParser()
    cfg.read(PASTA / "assinador.ini", encoding="utf-8")
    r = cfg.get("github", "REPO", fallback="").strip().strip("/")
    if r.count("/") != 1:
        falha("preencha REPO em assinador.ini (ex.: seuusuario/ecopdf)")
    return r


def carregar_publica():
    if not CHAVE_PUBLICA.exists():
        falha("nao achei chave_publica.txt. Rode gerar_chaves.bat primeiro.")
    raw = base64.b64decode(CHAVE_PUBLICA.read_text(encoding="utf-8").strip())
    return Ed25519PublicKey.from_public_bytes(raw)


def carregar_privada():
    if not CHAVE_PRIVADA.exists():
        falha("nao achei chave_privada.pem nesta pasta (o pendrive esta conectado?).")
    senha = getpass.getpass("  Senha da chave (nao aparece ao digitar): ").encode()
    try:
        return serialization.load_pem_private_key(CHAVE_PRIVADA.read_bytes(), password=senha)
    except (ValueError, TypeError):
        falha("senha errada ou arquivo de chave danificado.")


def versao_do_zip(dados):
    try:
        z = zipfile.ZipFile(io.BytesIO(dados))
    except zipfile.BadZipFile:
        falha("o arquivo nao e um zip valido.")
    nomes = set(z.namelist())
    faltando = [n for n in OBRIGATORIOS if n not in nomes]
    if faltando:
        falha("o zip nao tem a estrutura do EcoPDF. Faltam: " + ", ".join(faltando))
    versao = z.read("EcoPDF/VERSAO.txt").decode("utf-8-sig").strip()
    if not VERSAO_RE.match(versao):
        falha(f"VERSAO.txt dentro do zip tem formato invalido: '{versao}' (use AAAA.MM.DD ou AAAA.MM.DD.2)")
    return versao


# ---------------------------------------------------------------- comandos

def gerar_chaves():
    if CHAVE_PRIVADA.exists():
        falha("ja existe chave_privada.pem nesta pasta. Nao gerei outra para nao "
              "invalidar as maquinas que ja confiam na chave atual.")
    print("\n  Escolha uma senha para proteger a chave (minimo 10 caracteres).")
    print("  Guarde-a no seu gerenciador de senhas: sem ela nao da para publicar.\n")
    while True:
        s1 = getpass.getpass("  Senha: ")
        s2 = getpass.getpass("  Repita a senha: ")
        if s1 != s2:
            print("  As senhas nao conferem. Tente de novo.\n")
        elif len(s1) < 10:
            print("  Senha curta demais. Tente de novo.\n")
        else:
            break
    privada = Ed25519PrivateKey.generate()
    CHAVE_PRIVADA.write_bytes(privada.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(s1.encode()),
    ))
    publica = privada.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    CHAVE_PUBLICA.write_text(base64.b64encode(publica).decode() + "\n", encoding="utf-8")
    print("\n  Chaves criadas:")
    print("   - chave_privada.pem  SECRETA. Fica so no pendrive + 1 copia de seguranca.")
    print("   - chave_publica.txt  pode ser compartilhada; vai embutida no EcoPDF.")
    print("\n  Chave publica: " + base64.b64encode(publica).decode() + "\n")


def assinar(caminho_zip=None, ao_lado=True):
    caminho = Path(caminho_zip.strip('"')) if caminho_zip else PASTA / "EcoPDF.zip"
    if not caminho.exists():
        falha(f"nao achei {caminho}. Arraste o EcoPDF.zip para cima do assinar.bat.")
    dados = caminho.read_bytes()
    versao = versao_do_zip(dados)
    sha = hashlib.sha256(dados).hexdigest()
    print(f"\n  Versao dentro do zip: {versao}")

    if ULTIMA.exists():
        ultima = ULTIMA.read_text(encoding="utf-8").strip()
        if chave_versao(versao) < chave_versao(ultima):
            falha(f"a versao {versao} e MENOR que a ultima publicada ({ultima}). "
                  "Para voltar a uma versao antiga use reverter.bat.")
        if versao == ultima:
            antigo = PUBLICADOS / versao / "EcoPDF.zip"
            if antigo.exists() and hashlib.sha256(antigo.read_bytes()).hexdigest() != sha:
                falha(f"a versao {versao} ja foi publicada com outro conteudo. "
                      "Aumente o numero em VERSAO.txt (ex.: " + versao + ".2) e gere o zip de novo.")

    privada = carregar_privada()
    assinatura = privada.sign(dados)
    try:
        carregar_publica().verify(assinatura, dados)
    except InvalidSignature:
        falha("a chave privada nao corresponde a chave_publica.txt desta pasta.")

    destino = PUBLICADOS / versao
    destino.mkdir(parents=True, exist_ok=True)
    sig_txt = base64.b64encode(assinatura).decode() + "\n"
    (destino / "EcoPDF.zip").write_bytes(dados)
    (destino / "EcoPDF.zip.sig").write_text(sig_txt, encoding="ascii")
    ULTIMA.write_text(versao + "\n", encoding="utf-8")
    if ao_lado:
        sig_ao_lado = caminho.with_name("EcoPDF.zip.sig")
        sig_ao_lado.write_text(sig_txt, encoding="ascii")
        onde = sig_ao_lado
    else:
        onde = destino / "EcoPDF.zip.sig"
    print(f"\n  Assinado. Arquivo da assinatura:\n  {onde}\n")
    print(f"  Anexe-o ao rascunho do release {versao} no GitHub (o nome tem de ser EcoPDF.zip.sig).")
    print(f"  Copia guardada em {destino}\n")


def reverter(antiga, nova):
    if not VERSAO_RE.match(nova):
        falha(f"versao nova invalida: '{nova}' (use AAAA.MM.DD ou AAAA.MM.DD.2)")
    origem = PUBLICADOS / antiga / "EcoPDF.zip"
    if not origem.exists():
        falha(f"nao achei {origem}.")
    entrada = zipfile.ZipFile(origem)
    saida_bytes = io.BytesIO()
    with zipfile.ZipFile(saida_bytes, "w", zipfile.ZIP_DEFLATED) as saida:
        for item in entrada.infolist():
            conteudo = entrada.read(item.filename)
            if item.filename == "EcoPDF/VERSAO.txt":
                conteudo = (nova + "\n").encode()
            elif item.filename == "EcoPDF/NOVIDADES.txt":
                texto = conteudo.decode("utf-8-sig")
                linhas = texto.split("\n")
                pos = next((i for i, l in enumerate(linhas) if VERSAO_RE.match(l.strip())), len(linhas))
                linhas[pos:pos] = [nova, f"- Reversao: volta ao codigo da versao {antiga}.", ""]
                conteudo = ("﻿" + "\n".join(linhas)).encode("utf-8")
            saida.writestr(item, conteudo)
    temp = PASTA / f"EcoPDF_reversao_{nova}.zip"
    temp.write_bytes(saida_bytes.getvalue())
    print(f"\n  Pacote de reversao criado com o codigo da {antiga} e numero {nova}.")
    assinar(str(temp), ao_lado=False)
    temp.unlink()
    print(f"  Crie um release com a tag {nova} e anexe os dois arquivos de")
    print(f"  {PUBLICADOS / nova}  (EcoPDF.zip e EcoPDF.zip.sig).\n")


def baixar(url, api=False):
    cab = {"User-Agent": "EcoPDF-verificador"}
    if api:
        cab["Accept"] = "application/vnd.github+json"
    req = urllib.request.Request(url, headers=cab)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def release_do_canal(r, canal):
    """geral = Latest (nao pre-release); teste = a maior versao publicada (pre-release ou nao)."""
    import json
    st, corpo = baixar(f"https://api.github.com/repos/{r}/releases?per_page=30", api=True)
    if st != 200:
        falha(f"o GitHub respondeu {st} ao listar os releases de {r}. O repositorio e publico?")
    publicados = [x for x in json.loads(corpo) if not x.get("draft")]
    if canal == "geral":
        publicados = [x for x in publicados if not x.get("prerelease")]
    validos = [x for x in publicados if VERSAO_RE.match(x.get("tag_name", ""))]
    if not validos:
        return None
    return max(validos, key=lambda x: chave_versao(x["tag_name"]))


def verificar(canal="geral"):
    r = repo()
    print(f"\n  Conferindo o canal {canal} em github.com/{r}/releases\n")
    rel = release_do_canal(r, canal)
    if rel is None:
        print("  Nenhum release publicado neste canal ainda.\n")
        sys.exit(0)
    tag = rel["tag_name"]
    print(f"  Versao que as maquinas deste canal vao receber: {tag}"
          + ("  (pre-release)" if rel.get("prerelease") else "  (Latest)"))
    anexos = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    erros = 0
    for nome in ("EcoPDF.zip", "EcoPDF.zip.sig"):
        if nome in anexos:
            ok(f"anexo {nome} presente")
        else:
            ruim(f"falta o anexo {nome} (o nome tem de ser exatamente este)")
            erros += 1
    if erros:
        print(f"\n  Resultado: {erros} problema(s) - as maquinas ignorariam este release\n")
        sys.exit(1)
    st_z, zipb = baixar(anexos["EcoPDF.zip"])
    st_s, sigb = baixar(anexos["EcoPDF.zip.sig"])
    if st_z != 200 or st_s != 200:
        falha(f"nao consegui baixar os anexos (respostas {st_z}/{st_s})")
    try:
        carregar_publica().verify(base64.b64decode(sigb.strip()), zipb)
        ok("assinatura confere com a chave publica")
    except (InvalidSignature, ValueError):
        ruim("ASSINATURA NAO CONFERE (sig de outra versao ou zip trocado). As maquinas recusariam.")
        erros += 1
    versao_zip = versao_do_zip(zipb)
    if versao_zip == tag:
        ok(f"versao dentro do zip ({versao_zip}) = tag do release")
    else:
        ruim(f"versao dentro do zip ({versao_zip}) diferente da tag do release ({tag})")
        erros += 1
    print(f"\n  Resultado: {'TUDO CERTO' if not erros else str(erros) + ' problema(s) - veja acima'}\n")
    sys.exit(1 if erros else 0)


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "gerar-chaves":
        gerar_chaves()
    elif cmd == "assinar":
        assinar(args[1] if len(args) > 1 else None)
    elif cmd == "verificar":
        verificar(args[1] if len(args) > 1 else "geral")
    elif cmd == "reverter" and len(args) == 3:
        reverter(args[1], args[2])
    else:
        print(__doc__)
        sys.exit(2)
