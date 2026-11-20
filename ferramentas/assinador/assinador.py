"""Assinador do EcoPDF: gera o par de chaves, assina cada versao publicada
no GitHub (Releases) e confere o que esta publicado.

Uso (pelos .bat desta pasta):
  gerar_chaves.bat              cria chave_privada.pem (com senha) e chave_publica.txt
  assinar.bat [EcoPDF.zip]      cria EcoPDF.zip.sig ao lado do zip (para anexar ao release)
  verificar.bat / verificar_teste.bat  confere o release geral (Latest) ou de teste
  reverter.bat <antiga> <nova>  refaz o zip de uma versao antiga com numero novo, ja assinado
  CONFIGURAR PUBLICACAO.bat     uma vez: guarda a chave de PUBLICAR do GitHub, cifrada com a
                                senha da assinatura (chave_publicar.enc, so no pendrive)
  PUBLICAR VERSAO.bat           em um clique: acha o rascunho novo no GitHub, mostra as novidades,
                                pede a senha, assina, anexa o .sig, publica como Latest e verifica

Repositorio privado: a consulta ao GitHub usa a chave de leitura. Ela vem de TOKEN em
assinador.ini, ou do painel.ini do pendrive (GITHUB_TOKEN, ou pedida a planilha do painel com
PAINEL_URL + PAINEL_CHAVE). O painel.ini e procurado nesta pasta, na pasta de cima e em
"Instalador EcoPDF" ao lado.
"""
import base64
import configparser
import getpass
import hashlib
import io
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

try:  # usa os certificados do Windows (evita CERTIFICATE_VERIFY_FAILED ao baixar do GitHub)
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

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
CHAVE_PUBLICAR = PASTA / "chave_publicar.enc"   # chave do GitHub que publica, cifrada com a senha
SENHA = None                                    # senha digitada uma vez (publicar)

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
    r = cfg.get("github", "REPO", fallback="").strip().strip("/") or "heldergomes20-web/ecopdf"
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
    senha = SENHA or getpass.getpass("  Senha da chave (nao aparece ao digitar): ").encode()
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


def assinar(caminho_zip=None, ao_lado=True, publicando=False):
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

    try:  # confere tambem no GitHub, para o pendrive reserva (sem o registro local) ter a mesma trava
        import json
        st, corpo = baixar(f"https://api.github.com/repos/{repo()}/releases?per_page=30", api=True)
        tags = [x.get("tag_name", "") for x in json.loads(corpo) if not x.get("draft")] if st == 200 else []
        if st != 200:
            print(f"  (o GitHub respondeu {st}: confira a chave de leitura; segui com o registro do pendrive)")
        tags = [t for t in tags if VERSAO_RE.match(t)]
        ultima_gh = max(tags, key=chave_versao) if tags else None
    except Exception:
        ultima_gh = None
        print("  (nao consegui conferir a ultima versao no GitHub; segui com o registro do pendrive)")
    if ultima_gh and chave_versao(versao) < chave_versao(ultima_gh):
        falha(f"a versao {versao} e MENOR que a ultima publicada no GitHub ({ultima_gh}). "
              "Para voltar a uma versao antiga use reverter.bat.")

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
    if publicando:
        print(f"  Assinado ({versao}). Copia guardada em {destino}")
        return versao, sig_txt
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


def _painel_ini(k):
    for pasta in (PASTA, PASTA.parent, PASTA.parent / "Instalador EcoPDF"):
        try:
            texto = (pasta / "painel.ini").read_text("utf-8-sig")
        except OSError:
            continue
        for linha in texto.splitlines():
            a, sep, v = linha.partition("=")
            if sep and a.strip().upper() == k:
                return v.strip()
        return ""
    return ""


def _pedir_chave():
    """Chave de leitura do GitHub ('' = sem): assinador.ini TOKEN > painel.ini GITHUB_TOKEN > planilha."""
    cfg = configparser.ConfigParser()
    cfg.read(PASTA / "assinador.ini", encoding="utf-8")
    t = cfg.get("github", "TOKEN", fallback="").strip() or _painel_ini("GITHUB_TOKEN")
    if t:
        return t
    url, chave_painel = _painel_ini("PAINEL_URL"), _painel_ini("PAINEL_CHAVE")
    if not url or not chave_painel:
        return ""
    corpo = json.dumps({"chave": chave_painel, "pc": socket.gethostname(), "pedido": "github"}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=corpo, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read(20000).decode("utf-8", "replace"))
        return str(d.get("github") or "").strip() if d.get("ok") else ""
    except Exception:
        return ""


CHAVE_GH = None


class _Redirecionar(urllib.request.HTTPRedirectHandler):
    """Anexo do release redireciona para o armazenamento: segue sem levar a chave para outro endereco."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        novo = super().redirect_request(req, fp, code, msg, headers, newurl)
        if novo is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            novo.remove_header("Authorization")
        return novo


_ABRIR = urllib.request.build_opener(_Redirecionar())


def baixar(url, api=False, anexo=False):
    global CHAVE_GH
    if CHAVE_GH is None:
        CHAVE_GH = _pedir_chave()
    cab = {"User-Agent": "EcoPDF-verificador"}
    if api:
        cab["Accept"] = "application/vnd.github+json"
    if anexo:
        cab["Accept"] = "application/octet-stream"
    if CHAVE_GH:
        cab["Authorization"] = "Bearer " + CHAVE_GH
    req = urllib.request.Request(url, headers=cab)
    try:
        with _ABRIR.open(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def release_do_canal(r, canal):
    """geral = Latest (nao pre-release); teste = a maior versao publicada (pre-release ou nao)."""
    import json
    st, corpo = baixar(f"https://api.github.com/repos/{r}/releases?per_page=30", api=True)
    if st != 200:
        falha(f"o GitHub respondeu {st} ao listar os releases de {r}. "
              + ("A chave de leitura foi recusada: confira a chave na planilha do painel "
                 "(menu EcoPDF > Chave de leitura do GitHub)." if CHAVE_GH else
                 "Nao achei a chave de leitura (repositorio privado precisa dela): confira o painel.ini "
                 "no pendrive ou TOKEN em assinador.ini."))
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
    anexos = {a["name"]: a["url"] for a in rel.get("assets", [])}   # endereco da API: vale com repo privado
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
    st_z, zipb = baixar(anexos["EcoPDF.zip"], anexo=True)
    st_s, sigb = baixar(anexos["EcoPDF.zip.sig"], anexo=True)
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


# ---------------------------------------------------------------- publicar em um clique

def _derivar(senha, sal):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=sal, length=32, n=2 ** 15, r=8, p=1).derive(senha)


def _cifrar_chave(token, senha):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    sal, nonce = os.urandom(16), os.urandom(12)
    ct = AESGCM(_derivar(senha, sal)).encrypt(nonce, token.encode(), b"ecopdf-publicar")
    CHAVE_PUBLICAR.write_text(base64.b64encode(b"EP1" + sal + nonce + ct).decode() + "\n", encoding="ascii")


def _decifrar_chave(senha):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        raw = base64.b64decode(CHAVE_PUBLICAR.read_text(encoding="ascii").strip())
        if raw[:3] != b"EP1":
            raise ValueError
        return AESGCM(_derivar(senha, raw[3:19])).decrypt(raw[19:31], raw[31:], b"ecopdf-publicar").decode()
    except (OSError, ValueError, InvalidTag):
        falha("nao consegui abrir a chave de publicar com esta senha. Rode CONFIGURAR PUBLICACAO.bat de novo.")


def _api(metodo, url, corpo=None, tipo="application/json"):
    """Pedido ao GitHub com a chave de publicar. Devolve (status, json ou bytes)."""
    cab = {"User-Agent": "EcoPDF-publicar", "Accept": "application/vnd.github+json",
           "Authorization": "Bearer " + CHAVE_GH}
    dados = None
    if corpo is not None:
        dados = json.dumps(corpo).encode() if tipo == "application/json" else corpo
        cab["Content-Type"] = tipo
    req = urllib.request.Request(url, data=dados, method=metodo, headers=cab)
    try:
        with _ABRIR.open(req, timeout=120) as r:
            st, b = r.status, r.read()
    except urllib.error.HTTPError as e:
        st, b = e.code, e.read()
    try:
        return st, json.loads(b) if b else None
    except ValueError:
        return st, b


def _pedir_senha():
    global SENHA
    SENHA = getpass.getpass("  Senha da assinatura (nao aparece ao digitar): ").encode()
    carregar_privada()   # confere a senha antes de seguir
    return SENHA


def _conferir_pasta():
    if not CHAVE_PRIVADA.exists():
        falha("nao achei chave_privada.pem nesta pasta. Rode este .bat de dentro da pasta do pendrive "
              "onde esta a sua chave de assinatura (chave_privada.pem e chave_publica.txt).")


def configurar_publicar():
    global CHAVE_GH
    _conferir_pasta()
    r = repo()
    print("\n  CONFIGURAR A PUBLICACAO EM UM CLIQUE (uma vez so)\n")
    print("  1. No github.com: sua foto > Settings > Developer settings > Personal access tokens")
    print("     > Fine-grained tokens > Generate new token.")
    print("  2. Token name: EcoPDF publicar.  Expiration: Custom, daqui a 1 ano.")
    print(f"  3. Repository access: Only select repositories > {r.split('/')[1]}.")
    print("  4. Permissions > Repository permissions > Contents: Read and write. Nada mais.")
    print("  5. Generate token e copie a chave (comeca com github_pat_).\n")
    token = getpass.getpass("  Cole a chave aqui e aperte Enter (nao aparece ao colar): ").strip()
    if not re.match(r"^(github_pat_|ghp_)\w+$", token):
        falha("isso nao parece uma chave do GitHub (comeca com github_pat_). Nada foi guardado.")
    print(f"  Chave recebida (termina em {token[-4:]}). Conferindo no GitHub...")
    CHAVE_GH = token
    st, lista = _api("GET", f"https://api.github.com/repos/{r}/releases?per_page=30")
    if st != 200:
        falha(f"o GitHub recusou esta chave (codigo {st}). Confira os passos 3 e 4 e gere outra.")
    if not any(x.get("draft") for x in lista):
        print("  Aviso: a chave le o repositorio, mas nao vi rascunhos. Confira se Contents esta em")
        print("  Read and write; se nao estiver, a publicacao vai falhar e e so configurar de novo.")
    print("\n  Agora a senha da assinatura: a chave de publicar fica cifrada com ela.")
    senha = _pedir_senha()
    _cifrar_chave(token, senha)
    print(f"\n  PRONTO. Chave de publicar guardada (cifrada) em {CHAVE_PUBLICAR.name}.")
    print("  Copie este arquivo tambem para o pendrive reserva. A partir de agora: PUBLICAR VERSAO.bat\n")


def publicar():
    global CHAVE_GH
    _conferir_pasta()
    r = repo()
    if not CHAVE_PUBLICAR.exists():
        falha("falta configurar a publicacao. Rode CONFIGURAR PUBLICACAO.bat uma vez.")
    print("\n  PUBLICAR VERSAO NOVA DO ECOPDF\n")
    senha = _pedir_senha()
    CHAVE_GH = _decifrar_chave(senha)
    st, lista = _api("GET", f"https://api.github.com/repos/{r}/releases?per_page=50")
    if st != 200:
        falha(f"o GitHub respondeu {st} ao listar as versoes. A chave de publicar venceu? "
              "Gere outra e rode CONFIGURAR PUBLICACAO.bat.")
    publicadas = [x["tag_name"] for x in lista if not x.get("draft") and VERSAO_RE.match(x.get("tag_name", ""))]
    ultima = max(publicadas, key=chave_versao) if publicadas else "0.0.0"
    novos = [x for x in lista if x.get("draft") and VERSAO_RE.match(x.get("tag_name", ""))
             and chave_versao(x["tag_name"]) > chave_versao(ultima)]
    if not novos:
        print(f"  Nenhum rascunho novo para publicar (ultima publicada: {ultima}).\n")
        sys.exit(0)
    rel = max(novos, key=lambda x: chave_versao(x["tag_name"]))
    tag = rel["tag_name"]
    anexos = {a["name"]: a for a in rel.get("assets", [])}
    if "EcoPDF.zip" not in anexos:
        falha(f"o rascunho {tag} nao tem o EcoPDF.zip. Peca para gerar o rascunho de novo.")
    print(f"  Versao em rascunho: {tag}   (ultima publicada: {ultima})\n")
    notas = (rel.get("body") or "").split("\n---")[0].strip().splitlines()
    for linha in notas[:40]:
        print("    " + linha)
    if len(notas) > 40:
        print("    (...)")
    if input(f"\n  Publicar a {tag} para TODAS as salas? Digite S e aperte Enter: ").strip().upper() != "S":
        print("  Nada foi publicado.\n")
        sys.exit(0)
    st, zipb = baixar(anexos["EcoPDF.zip"]["url"], anexo=True)
    if st != 200:
        falha(f"nao consegui baixar o EcoPDF.zip do rascunho (codigo {st}).")
    temp = PASTA / f"_rascunho_{tag}.zip"
    temp.write_bytes(zipb)
    try:
        versao, sig_txt = assinar(str(temp), ao_lado=False, publicando=True)
    finally:
        temp.unlink(missing_ok=True)
    if versao != tag:
        falha(f"o zip diz versao {versao} e o rascunho diz {tag}. Nada foi publicado.")
    if "EcoPDF.zip.sig" in anexos:   # sobra de uma tentativa anterior
        _api("DELETE", anexos["EcoPDF.zip.sig"]["url"])
    up = rel["upload_url"].split("{")[0] + "?name=EcoPDF.zip.sig"
    st, _ = _api("POST", up, sig_txt.encode("ascii"), tipo="application/octet-stream")
    if st not in (200, 201):
        falha(f"o GitHub recusou anexar a assinatura (codigo {st}). A chave tem Contents: Read and write?")
    ok("assinatura anexada ao rascunho")
    st, _ = _api("PATCH", rel["url"], {"draft": False, "prerelease": False, "make_latest": "true"})
    if st != 200:
        falha(f"o GitHub recusou publicar (codigo {st}). O rascunho continua com a assinatura; "
              "publique pela pagina do GitHub como Latest.")
    ok(f"{tag} publicada como Latest")
    import time
    time.sleep(3)
    verificar("geral")


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "gerar-chaves":
        gerar_chaves()
    elif cmd == "assinar":
        assinar(args[1] if len(args) > 1 else None)
    elif cmd == "verificar":
        verificar(args[1] if len(args) > 1 else "geral")
    elif cmd == "configurar-publicar":
        configurar_publicar()
    elif cmd == "publicar":
        publicar()
    elif cmd == "reverter" and len(args) == 3:
        reverter(args[1], args[2])
    else:
        print(__doc__)
        sys.exit(2)
