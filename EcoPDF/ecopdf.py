"""
EcoPDF - recebe as imagens do aparelho por DICOM, monta um PDF de imagens
por exame e junta esse PDF ao laudo.

Fluxo:
  aparelho (P1 / enviar selecionadas -> DICOM storage) --> este programa
  1. cada imagem recebida e gravada em PASTA_DICOM/<estudo>/<imagem>.dcm
  2. quando um estudo fica ESPERA_SEGUNDOS sem receber imagem nova, gera um
     PDF por tipo de exame (a sonda separa eco de carotidas) em
     PASTA_AGUARDANDO_FOTOS, com nome no padrao dos laudos:
       2026_10_29_Larissa_Braga_Fernandes_Ecocardiograma.pdf
  3. laudo em PASTA_AGUARDANDO_IMAGENS + PDF de imagens do mesmo dia,
     paciente e tipo -> laudo+imagens em PASTA_FINALIZADOS (nome do laudo);
     os dois originais vao para PASTA_PROCESSADOS (nada e apagado)
  4. na duvida nao junta: lista em PENDENTES.txt. Para resolver, basta
     renomear o PDF de imagens em "aguardando fotos" (ou o laudo).

O estado fica todo em disco, entao reiniciar o PC nao perde nada.
Configuracao: config.ini na mesma pasta deste arquivo.
"""

import collections
import configparser
import io
import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import pydicom
from PIL import Image
from pydicom.pixels import apply_voi_lut, convert_color_space
from pydicom.uid import JPEG2000Lossless
from pynetdicom import AE, AllStoragePresentationContexts, evt
from pynetdicom.sop_class import Verification
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

BASE = Path(__file__).resolve().parent


def versao():
    """Numero da versao instalada (VERSAO.txt, o mesmo que o status.bat mostra)."""
    try:
        return (BASE / "VERSAO.txt").read_text(encoding="utf-8").strip() or "?"
    except OSError:
        return "?"

# Sintaxes de transferencia aceitas (sem compressao + JPEG/RLE/JPEG2000).
TRANSFER_SYNTAXES = [
    "1.2.840.10008.1.2",        # Implicit VR Little Endian
    "1.2.840.10008.1.2.1",      # Explicit VR Little Endian
    "1.2.840.10008.1.2.2",      # Explicit VR Big Endian
    "1.2.840.10008.1.2.4.50",   # JPEG Baseline (o Vivid usa este com "JPEG")
    "1.2.840.10008.1.2.4.51",   # JPEG Extended
    "1.2.840.10008.1.2.4.57",   # JPEG Lossless
    "1.2.840.10008.1.2.4.70",   # JPEG Lossless SV1
    "1.2.840.10008.1.2.4.80",   # JPEG-LS Lossless
    "1.2.840.10008.1.2.4.81",   # JPEG-LS Near-lossless
    "1.2.840.10008.1.2.4.90",   # JPEG 2000 Lossless
    "1.2.840.10008.1.2.4.91",   # JPEG 2000
    "1.2.840.10008.1.2.5",      # RLE Lossless
]

# Campos DICOM usados para descobrir o tipo de exame de cada imagem.
CAMPOS_TIPO = ("TransducerData", "StudyDescription", "SeriesDescription",
               "ProtocolName", "BodyPartExamined")

GERADO = ".gerado"
GRUPOS = "grupos.json"
OCULTAS = "ocultas.json"   # {tipo: [ids fora da folha]}
ORDEM = "ordem.json"       # {tipo: [ids na ordem da folha]} (definida na tela)
DESCARTADOS = "descartados.json"  # {tipo: quando} exames descartados na tela (nunca terao laudo)
# vigia (thread) e tela (servidor web) mexem nos mesmos arquivos: um de cada vez
TRAVA = threading.RLock()
# pedido antigo pelo nome do arquivo ("..._Ecocardiograma tirar 3 5"): hoje so gera aviso (use a tela)
PEDIDO = re.compile(r"^(?P<base>\d{4}_\d{2}_\d{2}_.+?)\s+(?P<acao>tirar|tira|retirar|remover|voltar|volta|repor)"
                    r"(?:\s+(?P<lista>.*))?$", re.I)  # so com espaco: nomes gerados usam "_"
# "..._Ecocardiograma sem imagens" na pasta 1: liberar o laudo sozinho
SEM_IMG = re.compile(r"^(?P<base>\d{4}_\d{2}_\d{2}_.+?)\s+sem\s+image(?:m|ns)$", re.I)
PALAVRAS_VAZIAS = {"de", "da", "do", "das", "dos", "e"}

log = logging.getLogger("ecopdf")


# --------------------------------------------------------------------------
# Configuracao
# --------------------------------------------------------------------------

def area_de_trabalho():
    """Area de Trabalho do usuario logado, mesmo quando o OneDrive a moveu.
    Permite o mesmo config.ini em todos os PCs (%DESKTOP% nos caminhos)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            return Path(os.path.expandvars(winreg.QueryValueEx(k, "Desktop")[0]))
    except (ImportError, OSError):
        return Path.home() / "Desktop"


def _modo_abrir(v):
    """TELA_ABRIR: ao_receber (abre quando chega imagem ou laudo) | sim (ao iniciar) | nao."""
    v = (v or "").strip().lower()
    if v in ("nao", "não", "n", "0", "no", "false"):
        return "nao"
    if v in ("sim", "s", "1", "yes", "true", "inicio", "ao_iniciar"):
        return "inicio"
    return "ao_receber"


TEMPOS_FOLHA = collections.deque(maxlen=300)  # (quando, segundos) para gerar a folha: painel
TELA = None  # modulo da tela, quando ela esta ligada (para abrir ao receber)


def _avisar_tela(cfg):
    if TELA is not None:
        try:
            TELA.abrir_se_fechada(cfg)
        except Exception:
            log.exception("Tela: nao consegui abrir o navegador")


def _painel_ini(chave):
    """Valor do painel.ini ao lado do ecopdf.py ('' se nao houver). Formato: CHAVE = valor."""
    try:
        for linha in (BASE / "painel.ini").read_text("utf-8-sig").splitlines():
            k, sep, v = linha.partition("=")
            if sep and k.strip().upper() == chave:
                return v.strip()
    except OSError:
        pass
    return ""


def carregar_config(caminho):
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # preserva maiusculas dos tipos de exame
    if not cp.read(caminho, encoding="utf-8"):
        sys.exit(f"config.ini nao encontrado em {caminho}")
    c = cp["ecopdf"]
    for chave in ("PASTA_AGUARDANDO_FOTOS", "PASTA_AGUARDANDO_IMAGENS", "PASTA_FINALIZADOS"):
        if not c.get(chave, "").strip():
            sys.exit(f"config.ini: preencha {chave}")

    def pasta(chave, padrao=""):
        v = c.get(chave, padrao).strip()
        if not v:
            return None
        p = Path(os.path.expandvars(v.replace("%DESKTOP%", str(area_de_trabalho()))))
        return p if p.is_absolute() else BASE / p

    tipos = {}
    if cp.has_section("tipos"):
        for nome, regex in cp["tipos"].items():
            if regex.strip():
                tipos[nome.strip()] = re.compile(regex.strip(), re.IGNORECASE)

    return {
        "ae_title": c.get("AE_TITLE", "ECOPDF").strip()[:16],
        "porta": c.getint("PORTA", 11112),
        "pasta_dicom": pasta("PASTA_DICOM", "dicom"),
        "pasta_log": pasta("PASTA_LOG", "log"),
        "pasta_processados": pasta("PASTA_PROCESSADOS", "processados"),
        "pasta_imagens": pasta("PASTA_AGUARDANDO_FOTOS"),
        "pasta_laudos": pasta("PASTA_AGUARDANDO_IMAGENS"),
        "pasta_final": pasta("PASTA_FINALIZADOS"),
        "espera": c.getint("ESPERA_SEGUNDOS", 150),
        "colunas": c.getint("COLUNAS", 2),
        "linhas": c.getint("LINHAS", 3),
        "quadro_loop": c.get("QUADRO_LOOP", "ignorar").strip().lower(),
        "simbolo": pasta("SIMBOLO", "simbolo.png"),
        "rodape": [l.strip() for l in c.get("RODAPE", "").split("|") if l.strip()],
        "manter_dicom_dias": c.getint("MANTER_DICOM_DIAS", 30),
        "semelhanca": c.getfloat("SEMELHANCA_NOME", 0.85),
        "renomeador": c.get("RENOMEADOR", "sim").strip().lower() in ("sim", "s", "1", "yes", "true"),
        "sufixo_refeito": c.get("SUFIXO_REFEITO", "_retificado").strip(),
        "pendentes_minutos": c.getint("PENDENTES_MINUTOS", 30),
        "qualidade_jpeg": min(95, max(50, c.getint("QUALIDADE_JPEG", 80))),
        "largura_max": max(0, c.getint("LARGURA_MAX_IMAGEM", 1000)),
        "porta_tela": c.getint("TELA_PORTA", 8765),
        # endereco e chave da planilha ficam no painel.ini (copiado a mao em cada PC da clinica,
        # fora do zip publico); o config.ini so serve de reserva
        "painel_url": _painel_ini("PAINEL_URL") or c.get("PAINEL_URL", "").strip(),
        "painel_chave": _painel_ini("PAINEL_CHAVE") or c.get("PAINEL_CHAVE", "").strip(),
        "painel_horarios": [h.strip() for h in c.get("PAINEL_HORARIOS", "09:00, 15:00").split(",") if h.strip()],
        "abrir_tela": _modo_abrir(c.get("TELA_ABRIR", "ao_receber")),
        "ocultar_pastas": c.get("OCULTAR_PASTAS", "sim").strip().lower() not in ("nao", "não", "0", "n"),
        "tipos": tipos,
    }


def configurar_log(pasta):
    pasta.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    arq = logging.handlers.RotatingFileHandler(
        pasta / "ecopdf.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    arq.setFormatter(fmt)
    log.addHandler(arq)
    if sys.stdout is not None:  # pythonw.exe nao tem console
        tela = logging.StreamHandler()
        tela.setFormatter(fmt)
        log.addHandler(tela)
    log.setLevel(logging.INFO)
    log.propagate = False  # o renomeador usa o logger raiz (log/renomeador.log)
    logging.getLogger("pynetdicom").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# Utilitarios de texto
# --------------------------------------------------------------------------

def nome_arquivo_paciente(nome):
    """'CÉLIA MARIA DA SILVA' -> 'Celia_Maria_da_Silva' (padrao dos laudos)."""
    s = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    partes = re.sub(r"[^A-Za-z0-9 ]+", " ", s).split()
    return "_".join(p.lower() if p.lower() in PALAVRAS_VAZIAS else p.capitalize() for p in partes)


def nome_paciente(pn):
    """'SILVA^MARIA^DA' -> 'MARIA DA SILVA'. Sem '^', devolve como veio."""
    s = str(pn or "").strip()
    if "^" not in s:
        return " ".join(s.split())
    sobrenome, nome, meio = ([p.strip() for p in s.split("^")] + ["", "", ""])[:3]
    return " ".join(x for x in (nome, meio, sobrenome) if x)


def limpar_arquivo(s, limite=80):
    """Remove caracteres proibidos no Windows e espacos repetidos."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s)
    s = " ".join(s.split()).strip(" .")
    return s[:limite] or "SEM_NOME"


def normalizar(s):
    """Minusculas, sem acento, so letras/numeros separados por espaco."""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


def _som(t):
    """Grafias que soam igual: Raphael/Rafael, Luiz/Luis, Thais/Tais, Mariah/Maria."""
    t = t.replace("ph", "f").replace("th", "t").replace("y", "i")
    t = re.sub(r"(.)\1", r"\1", t)       # letra dobrada: Mello/Melo
    t = re.sub(r"z$", "s", t)
    return re.sub(r"(?<=.)h$", "", t)     # h final mudo: Mariah, Sarah


def tokens(s):
    return [_som(t) for t in normalizar(s).split() if t not in PALAVRAS_VAZIAS]


def cobertura(a, b):
    """Media, para cada palavra de a, da melhor semelhanca com uma palavra de b."""
    if not a or not b:
        return 0.0
    return sum(max(SequenceMatcher(None, x, y).ratio() for y in b) for x in a) / len(a)


def data_br(d):
    d = str(d or "")
    return f"{d[6:8]}/{d[4:6]}/{d[0:4]}" if len(d) == 8 else d


def data_iso(d):
    d = str(d or "")
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else "sem-data"


def gravar_atomico(destino, escrever):
    """Escreve em .tmp e renomeia: o arquivo nunca aparece pela metade."""
    tmp = destino.with_name(destino.name + ".tmp")
    escrever(tmp)
    os.replace(tmp, destino)


# --------------------------------------------------------------------------
# Recepcao DICOM
# --------------------------------------------------------------------------

def tipo_da_imagem(ds, tipos):
    """Campo a campo, na ordem de CAMPOS_TIPO: a sonda decide antes da descricao."""
    for campo in CAMPOS_TIPO:
        texto = str(ds.get(campo, "") or "")
        if not texto:
            continue
        for nome, regex in tipos.items():
            if regex.search(texto):
                return nome
    return ""


def on_echo(event):
    log.info("Teste de conexao (C-ECHO) de %s", event.assoc.requestor.address)
    _anotar_conexao(event, "teste (Verify/Echo)")
    return 0x0000


def _anotar_conexao(event, tipo):
    """Ultimo aparelho que falou com este PC (IP, AE Title, quando), para o painel das
    salas separar 'o aparelho nem conectou' de 'conectou e nao mandou imagem'."""
    try:
        req = event.assoc.requestor
        dados = {"ip": str(req.address), "ae": str(req.ae_title).strip(), "tipo": tipo,
                 "em": datetime.now().isoformat(timespec="seconds")}
        arq = BASE_CONEXAO[0]
        if arq:
            gravar_atomico(arq, lambda p: p.write_text(json.dumps(dados), "utf-8"))
    except Exception:
        pass  # so informativo


BASE_CONEXAO = [None]  # log/conexao.json (definido no main)


def on_conectou(event, cfg):
    _anotar_conexao(event, "conexao")


def on_recusou(event, cfg):
    _anotar_conexao(event, "recusada (AE Title/configuracao)")


def _sem_perda(ds):
    """Imagem que chegou SEM compressao: guarda em JPEG 2000 sem perda (pixels identicos,
    conferidos). Ja comprimida pelo aparelho: guarda exatamente como veio (None)."""
    try:
        if "PixelData" not in ds or ds.file_meta.TransferSyntaxUID.is_compressed:
            return None
        original = ds.pixel_array.copy()
        ds.compress(JPEG2000Lossless, encoding_plugin="pylibjpeg")
        if not np.array_equal(ds.pixel_array, original):
            raise ValueError("pixels diferentes apos compressao")
        buf = io.BytesIO()
        ds.save_as(buf, enforce_file_format=True)
        return buf.getvalue()
    except Exception:
        log.exception("Nao consegui comprimir sem perda; guardando como veio")
        return None


def on_store(event, cfg):
    try:
        ds = event.dataset
        quadros = int(ds.get("NumberOfFrames", 1) or 1)
        if quadros > 1 and cfg["quadro_loop"] == "ignorar":
            # loop (video): aceito para o aparelho nao reenviar, mas nao guardo nem uso
            log.info("Loop ignorado de %s: %s | quadros=%s",
                     event.assoc.requestor.ae_title, nome_paciente(ds.get("PatientName")), quadros)
            return 0x0000
        estudo = str(ds.get("StudyInstanceUID", "")).strip() or "sem-estudo"
        sop = str(ds.get("SOPInstanceUID", "")).strip() or str(time.time_ns())
        pasta = cfg["pasta_dicom"] / limpar_arquivo(estudo, 64)
        pasta.mkdir(parents=True, exist_ok=True)
        ds.file_meta = event.file_meta
        dados = _sem_perda(ds) or event.encoded_dataset()
        # reenvio da mesma imagem sobrescreve o mesmo arquivo
        gravar_atomico(pasta / (limpar_arquivo(sop, 64) + ".dcm"),
                       lambda p: p.write_bytes(dados))
        _anotar_conexao(event, "imagem")
        _avisar_tela(cfg)
        log.info("Recebida de %s: %s | %s | quadros=%s | aparelho: %s | campos de tipo: %s",
                 event.assoc.requestor.ae_title, nome_paciente(ds.get("PatientName")),
                 ds.get("Modality", "?"), ds.get("NumberOfFrames", 1), aparelho(ds, cfg),
                 {k: str(ds.get(k)) for k in CAMPOS_TIPO if ds.get(k)})
        dt = str(ds.get("StudyDate", "") or "")
        if len(dt) == 8 and (dt > time.strftime("%Y%m%d") or dt < "20100101"):
            log.warning("Data do aparelho errada? imagem com data %s (PC: %s): acertar data e hora "
                        "do aparelho", data_br(dt), time.strftime("%d/%m/%Y"))
        return 0x0000
    except Exception:
        log.exception("Falha ao gravar imagem recebida")
        return 0xA700  # o aparelho mantem na fila e pode reenviar


# --------------------------------------------------------------------------
# DICOM -> imagem
# --------------------------------------------------------------------------

def dicom_para_imagens(ds, quadro_loop):
    """Lista de PIL.Image RGB: 1 por imagem parada, 1 quadro escolhido por loop."""
    n = int(ds.get("NumberOfFrames", 1) or 1)
    if n <= 1:
        indices = [None]
    elif quadro_loop == "ignorar":
        return []
    elif quadro_loop == "primeiro":
        indices = [0]
    elif quadro_loop == "ultimo":
        indices = [n - 1]
    else:
        indices = [n // 2]

    foto = str(ds.get("PhotometricInterpretation", "")).upper()
    comprimido = ds.file_meta.TransferSyntaxUID.is_compressed
    imagens = []
    for idx in indices:
        arr = ds.pixel_array if idx is None else pydicom.pixels.pixel_array(ds, index=idx)
        if arr.ndim == 3 and arr.shape[-1] == 3:
            # pydicom 3 ja converte YBR->RGB quando descomprime; sem compressao, nao
            if foto.startswith("YBR") and not comprimido:
                arr = convert_color_space(arr, foto, "RGB")
            img = Image.fromarray(arr.astype(np.uint8), "RGB")
        elif foto == "PALETTE COLOR":
            rgb = pydicom.pixels.apply_color_lut(arr, ds)
            if rgb.dtype != np.uint8:
                rgb = (rgb / 256).astype(np.uint8)
            img = Image.fromarray(rgb, "RGB")
        else:
            if arr.dtype != np.uint8 or "WindowCenter" in ds:
                arr = apply_voi_lut(arr, ds).astype(np.float64)
                lo, hi = float(arr.min()), float(arr.max())
                arr = (arr - lo) / (hi - lo or 1) * 255
            arr = arr.astype(np.uint8)
            if foto == "MONOCHROME1":
                arr = 255 - arr
            img = Image.fromarray(arr, "L").convert("RGB")
        imagens.append(img)
    return imagens


def momento(ds, arquivo):
    """Ordenacao: horario de aquisicao, numero da imagem, hora de chegada."""
    d = str(ds.get("ContentDate") or ds.get("AcquisitionDate") or "")
    t = str(ds.get("ContentTime") or ds.get("AcquisitionTime") or "").split(".")[0]
    try:
        num = int(ds.get("InstanceNumber") or 0)
    except (TypeError, ValueError):
        num = 0
    return (d + t.ljust(6, "0") if d and t else "", num, arquivo.stat().st_mtime)


# --------------------------------------------------------------------------
# PDF de imagens (um por estudo e tipo de exame)
# --------------------------------------------------------------------------

def _ler_estudo(pasta_estudo, cfg):
    """(dataset de referencia, {tipo: [(id, imagem) em ordem]}). id = 'arquivo#k',
    estavel mesmo se chegarem imagens novas (usado para tirar/voltar imagem)."""
    grupos = {}  # tipo -> lista de (chave, id, imagem)
    ref = None
    for arq in sorted(pasta_estudo.glob("*.dcm")):
        try:
            ds = pydicom.dcmread(arq)
        except Exception:
            log.exception("Arquivo DICOM ilegivel: %s", arq.name)
            continue
        ref = ref or ds
        if "PixelData" not in ds:
            continue  # relatorio de medidas (SR), sem imagem
        try:
            imgs = dicom_para_imagens(ds, cfg["quadro_loop"])
        except Exception:
            log.exception("Nao consegui decodificar %s", arq.name)
            continue
        chave = momento(ds, arq)
        grupos.setdefault(tipo_da_imagem(ds, cfg["tipos"]), []).extend(
            (chave, f"{arq.stem}#{k}", i) for k, i in enumerate(imgs))
    for itens in grupos.values():
        itens.sort(key=lambda x: x[:2])
    return ref, {t: [(n, i) for _, n, i in itens] for t, itens in grupos.items()}


def _ler_json(arq, padrao):
    try:
        return json.loads(arq.read_text("utf-8"))
    except (OSError, ValueError):
        return padrao


def _ler_ocultas(pasta_estudo):
    return _ler_json(pasta_estudo / OCULTAS, {})


def _ler_ordem(pasta_estudo):
    return _ler_json(pasta_estudo / ORDEM, {})


def ordenar(ids, ordem, fora):
    """ids na ordem da folha: primeiro os da ordem salva na tela, depois os que
    chegaram depois (na ordem de chegada). Sem os que estao fora."""
    pos = {i: k for k, i in enumerate(ordem or [])}
    seq = sorted(enumerate(ids), key=lambda e: (pos.get(e[1], len(pos)), e[0]))
    fora = set(fora or [])
    return [i for _, i in seq if i not in fora]


def _numeradas(pasta_estudo, tipo, itens, ordem=None, fora=None):
    """[(numero, imagem)] na ordem da folha, sem as tiradas; numero = posicao na folha."""
    t = tipo or ""
    if ordem is None:
        ordem = _ler_ordem(pasta_estudo).get(t, [])
    if fora is None:
        fora = _ler_ocultas(pasta_estudo).get(t, [])
    por_id = dict(itens)
    return [(n, por_id[i]) for n, i in enumerate(ordenar([i for i, _ in itens], ordem, fora), 1)]


def gerar_pdfs_do_estudo(pasta_estudo, cfg):
    ref, grupos = _ler_estudo(pasta_estudo, cfg)
    if ref is None or not grupos:
        log.warning("Estudo %s sem imagens aproveitaveis", pasta_estudo.name)
        return

    # nome do aparelho: provisorio; na juncao a folha e refeita com o nome do laudo
    paciente = nome_paciente(ref.get("PatientName")) or "SEM NOME"
    data = str(ref.get("StudyDate", "") or "")

    # PDFs que este estudo ja gerou: reaproveita o nome enquanto ainda estiver
    # aguardando; se o tipo sumiu (config mudou), remove o antigo
    antes = _ler_grupos(pasta_estudo)
    anterior = {g["tipo"]: Path(g["pdf"]) for g in antes}
    qtd_antes = {g["tipo"]: g.get("n") for g in antes}
    for tipo, p in anterior.items():
        if tipo not in grupos and p.exists():
            p.unlink()

    registro = []
    cfg["pasta_imagens"].mkdir(parents=True, exist_ok=True)
    for tipo, itens in sorted(grupos.items()):
        imagens = _numeradas(pasta_estudo, tipo, itens)
        destino = anterior.get(tipo)
        if destino is not None and not destino.exists():
            if qtd_antes.get(tipo) == len(itens) or _atualizar_final(destino, data, tipo, cfg):
                # mesma quantidade: imagem nova era do outro tipo (ex.: so Carotidas), nada a refazer
                registro.append({"pdf": str(destino), "data": data, "paciente": paciente,
                                 "tipo": tipo, "n": len(itens)})
                continue
            log.warning("Imagens novas de exame ja finalizado (%s): gerando outro PDF", destino.name)
        if destino is None or not destino.exists():
            data_nome = f"{data[:4]}_{data[4:6]}_{data[6:8]}" if len(data) == 8 else "sem_data"
            base = limpar_arquivo(f"{data_nome}_{nome_arquivo_paciente(paciente)}_{tipo or 'Imagens'}", 150)
            destino = cfg["pasta_imagens"] / f"{base}.pdf"
            n = 2
            while destino.exists():  # outro estudo do mesmo paciente/dia/tipo
                destino = cfg["pasta_imagens"] / f"{base}_{n}.pdf"
                n += 1
        gravar_atomico(destino, lambda p: _desenhar(p, imagens, cfg, dados_do_dicom(ref, cfg)))
        log.info("PDF de imagens: %s (%d imagens, %.1f MB)", destino.name, len(imagens),
                 destino.stat().st_size / 1e6)
        registro.append({"pdf": str(destino), "data": data, "paciente": paciente,
                         "tipo": tipo, "n": len(itens)})  # total recebido (com as tiradas)
    gravar_atomico(pasta_estudo / GRUPOS,
                   lambda p: p.write_text(json.dumps(registro, ensure_ascii=False, indent=1), "utf-8"))


def _ler_grupos(pasta_estudo):
    try:
        return json.loads((pasta_estudo / GRUPOS).read_text("utf-8"))
    except (OSError, ValueError):
        return []


CINZA = (0.69, 0.69, 0.69)


def _recortar(img, limiar=12, margem=4):
    """Tira so a borda preta vazia em volta da tela do aparelho (nada com conteudo)."""
    a = np.asarray(img.convert("L")) > limiar
    linhas, colunas = np.where(a.any(axis=1))[0], np.where(a.any(axis=0))[0]
    if not len(linhas) or not len(colunas):
        return img
    x0, x1 = max(colunas[0] - margem, 0), min(colunas[-1] + margem + 1, img.width)
    y0, y1 = max(linhas[0] - margem, 0), min(linhas[-1] + margem + 1, img.height)
    return img.crop((x0, y0, x1, y1))


def _desenhar(caminho, imagens, cfg, dados, primeira_pagina=1):
    """Folha de imagens com o mesmo visual do laudo do Medware: simbolo e
    'Clinica Ecocardio', barra cinza com o nome do paciente, moldura com as
    imagens, barra 'Pagina: N' e rodape com os enderecos."""
    larg, alt = A4
    x0, x1 = 41, larg - 41
    cols, lins = max(1, cfg["colunas"]), max(1, cfg["linhas"])
    por_pagina = cols * lins
    total = (len(imagens) + por_pagina - 1) // por_pagina
    paciente = str(dados.get("paciente") or "")
    simbolo = cfg["simbolo"] if cfg["simbolo"] and cfg["simbolo"].exists() else None

    def barra(y, h, texto, fonte, negrito):
        c.setFillColorRGB(*CINZA)
        c.rect(x0, y, x1 - x0, h, fill=1, stroke=1)
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold" if negrito else "Helvetica", fonte)
        c.drawCentredString(larg / 2, y + h / 2 - fonte * 0.35, texto)

    c = canvas.Canvas(str(caminho), pagesize=A4)
    c.setTitle(f"Imagens - {paciente}")
    c.setCreator(f"EcoPDF {versao()}")
    c.setLineWidth(0.6)
    for p in range(total):
        if simbolo:
            c.drawImage(str(simbolo), 47, alt - 110, width=54, height=50,
                        preserveAspectRatio=True, mask="auto")
        c.setFont("Times-Bold", 24)
        c.drawString(137, alt - 86, "Clínica Ecocardio")

        barra(alt - 132, 18, paciente, 8.5, True)
        topo, base = alt - 136, 74
        c.rect(x0, base, x1 - x0, topo - base, fill=0, stroke=1)
        pad, esp = 8, 6
        cel_w = (x1 - x0 - 2 * pad - (cols - 1) * esp) / cols
        cel_h = (topo - base - 2 * pad - (lins - 1) * esp) / lins
        for i, (num, img) in enumerate(imagens[p * por_pagina:(p + 1) * por_pagina]):
            col, lin = i % cols, i // cols
            cx = x0 + pad + col * (cel_w + esp)
            cy = topo - pad - (lin + 1) * cel_h - lin * esp
            esc = min(cel_w / img.width, cel_h / img.height)
            w, h = img.width * esc, img.height * esc
            lim = cfg.get("largura_max", 0)
            if lim and img.width > lim:   # so reduz; ~360 dpi na celula de 7 cm
                img = img.resize((lim, max(1, round(img.height * lim / img.width))), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=cfg.get("qualidade_jpeg", 80))
            buf.seek(0)
            ix, iy = cx + (cel_w - w) / 2, cy + (cel_h - h) / 2
            c.drawImage(ImageReader(buf), ix, iy, w, h)
            # numero discreto no canto: posicao da imagem na folha (o mesmo da tela)
            c.setFillColorRGB(0.85, 0.85, 0.85)
            c.setFont("Helvetica", 7)
            c.drawRightString(ix + w - 3, iy + 3, str(num))

        barra(52, 18, f"Página: {primeira_pagina + p}", 7, False)
        if dados.get("sigla"):
            # escondido: final da serie do aparelho, na ponta direita da barra cinza,
            # em cinza pouco mais escuro que ela (para achar o exame no aparelho depois)
            c.setFillColorRGB(0.56, 0.56, 0.56)
            c.setFont("Helvetica", 5)
            c.drawRightString(x1 - 4, 52 + 7, str(dados["sigla"]))
            c.setFillColorRGB(0, 0, 0)
        c.setFont("Times-Bold", 8.5)
        for k, linha in enumerate(cfg["rodape"]):
            c.drawCentredString(larg / 2, 38 - k * 11, linha)
        # marca invisivel (branco, 1 pt): se esta folha voltar pela PDF24 (final impresso
        # na impressora errada), o EcoPDF reconhece e nao junta as imagens de novo
        c.setFillColorRGB(1, 1, 1)
        c.setFont("Helvetica", 1)
        c.drawString(x0, 6, MARCA_FOLHA)
        c.setFillColorRGB(0, 0, 0)
        c.showPage()
    c.save()


def sigla(ds, cfg=None):
    """Final (4 ultimos caracteres) do numero de serie de fabrica que o proprio
    aparelho manda no DICOM. Identifica o aparelho onde quer que ele esteja,
    sem tabela para manter. '' se o aparelho nao mandar a serie."""
    if ds is None:
        return ""
    serie = re.sub(r"\s", "", str(ds.get("DeviceSerialNumber", "") or ""))
    return serie[-4:].upper()


def aparelho(ds, cfg):
    """'Vivid T8 · serie 123456': de onde vieram as imagens (so no log)."""
    if ds is None:
        return ""
    serie = str(ds.get("DeviceSerialNumber", "") or "").strip()
    estacao = str(ds.get("StationName", "") or "").strip()
    modelo = str(ds.get("ManufacturerModelName", "") or "").strip()
    partes = [modelo or estacao]
    if serie:
        partes.append(f"série {serie}")
    return " · ".join(p for p in partes if p)


def dados_do_dicom(ref, cfg=None):
    idade = str(ref.get("PatientAge", "") or "")
    m = re.match(r"0*(\d+)Y", idade)
    return {
        "paciente": (nome_paciente(ref.get("PatientName")) or "SEM NOME").upper(),
        "nasc": data_br(ref.get("PatientBirthDate", "")),
        "sexo": str(ref.get("PatientSex", "") or ""),
        "idade": f"{m.group(1)} anos" if m else "",
        "data": data_br(ref.get("StudyDate", "")),
        "sigla": sigla(ref, cfg),
    }


# --------------------------------------------------------------------------
# Juncao laudo + imagens
# --------------------------------------------------------------------------

def ler_nome(arq, tipos):
    """'2026_10_29_Larissa_Braga_Fernandes_Ecocardiograma.pdf' (ou ..._2.pdf)
       -> ('20261029', 'larissa braga fernandes', 'Ecocardiograma').
       Vale para laudos e para os PDFs de imagens. Tipo nao reconhecido -> ''."""
    m = re.match(r"(\d{4})_(\d{2})_(\d{2})_(.+?)(?:_\d+)?$", arq.stem)
    if not m:
        return None
    data, resto = m.group(1) + m.group(2) + m.group(3), normalizar(m.group(4))
    # tipo mais longo primeiro, para 'Duplex_Scan_Vascular_Carotidas' vencer 'Duplex'
    for tipo in sorted(list(tipos) + ["Imagens"], key=len, reverse=True):
        t = normalizar(tipo)
        if resto.endswith(" " + t):
            return data, resto[: -len(t) - 1], "" if tipo == "Imagens" else tipo
    return data, resto, ""


def _listar(pasta, cfg, agora):
    itens = []
    for arq in pasta.glob("*.pdf"):
        try:
            if agora - arq.stat().st_mtime < 20:
                continue  # acabou de ser salvo (ou sincronizado); espera estabilizar
        except OSError:
            continue
        if PEDIDO.match(arq.stem) or SEM_IMG.match(arq.stem):
            continue  # pedido de tirar/voltar imagem: tratado em processar_pedidos
        info = ler_nome(arq, cfg["tipos"])
        if info:
            itens.append((arq, *info))
    return itens


def _mesmo_paciente(a, b, cfg):
    """Primeiro nome igual e TODAS as palavras do nome mais curto presentes no longo.
    Aceita 'Larissa Fernandes' (Vivid) x 'Larissa Braga Fernandes' (laudo),
    erro de digitacao ('Lustos' x 'Lustosa') e iniciais ('Maria F G Luckwu')."""
    def cheio(t):
        return [x for x in t if len(x) > 1]
    curto, longo = sorted((tokens(a), tokens(b)), key=lambda t: (len(cheio(t)), len(t)))
    if len(cheio(curto)) < 2:
        return False  # so o primeiro nome (ou iniciais) nao basta para identificar ninguem
    primeiro = SequenceMatcher(None, curto[0], longo[0]).ratio()

    def nota(x):  # inicial: precisa de um nome com essa letra; palavra: tolera 1-2 letras
        if len(x) == 1:
            return 1.0 if any(y.startswith(x) for y in cheio(longo)) else 0.0
        return max(SequenceMatcher(None, x, y).ratio() for y in longo)
    cada = min(nota(x) for x in curto)
    return primeiro >= cfg["semelhanca"] and cada >= 0.8


def _casar(laudo, laudos, fotos, cfg):
    """(foto, None) ou (None, motivo). Na duvida, nao casa."""
    _, data, nome, tipo = laudo
    do_dia = [f for f in fotos if f[1] == data and _mesmo_paciente(f[2], nome, cfg)]
    irmaos = [l for l in laudos if l[1] == data and _mesmo_paciente(l[2], nome, cfg)]
    if not do_dia:
        return None, "sem PDF de imagens do mesmo dia com nome parecido"
    if tipo:
        if sum(1 for l in irmaos if l[3] == tipo) > 1:
            return None, f"mais de um laudo de {tipo} deste paciente no dia (duplicado?)"
        mesmo = [f for f in do_dia if f[3] == tipo]
        if len(mesmo) == 1:
            return mesmo[0], None
        if len(mesmo) > 1:
            return None, f"{len(mesmo)} PDFs de imagens de {tipo} deste paciente no dia"
    sem_tipo = [f for f in do_dia if not f[3]]
    if len(irmaos) == 1 and len(do_dia) == 1 and sem_tipo:
        return sem_tipo[0], None  # um laudo e um PDF de imagens no dia: sem ambiguidade
    return None, "tipo do exame nao identificado e mais de um exame/PDF do paciente no dia"


def _livre(arq):
    """No Windows, PDF aberto num leitor nao pode ser movido. Testa antes de juntar,
    para nao mover so um dos dois arquivos."""
    teste = arq.with_name(arq.name + ".teste")
    try:
        os.rename(arq, teste)
    except OSError:
        return False
    os.rename(teste, arq)
    return True


def _mover(arq, pasta, nome=None):
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / (nome or arq.name)
    if destino.exists():
        destino = pasta / f"{destino.stem}_{datetime.now():%H%M%S}{destino.suffix}"
    shutil.move(str(arq), str(destino))
    return destino


# --------------------------------------------------------------------------
# Registro dos exames finalizados (para corrigir laudo depois de juntar)
# --------------------------------------------------------------------------

def _ler_registro(cfg):
    try:
        return json.loads((cfg["pasta_processados"] / "finalizados.json").read_text("utf-8"))
    except (OSError, ValueError):
        return []


def _gravar_registro(cfg, reg):
    cfg["pasta_processados"].mkdir(parents=True, exist_ok=True)
    gravar_atomico(cfg["pasta_processados"] / "finalizados.json",
                   lambda p: p.write_text(json.dumps(reg, ensure_ascii=False, indent=1), "utf-8"))


def _registrar(cfg, laudo, final, foto_guardada, laudo_guardado):
    _, data, nome, tipo = laudo
    reg = _ler_registro(cfg)
    reg.append({"data": data, "nome": nome, "tipo": tipo, "final": final,
                "foto": str(foto_guardada), "laudo": str(laudo_guardado),
                "em": datetime.now().isoformat(timespec="seconds")})
    _gravar_registro(cfg, reg)


def _guardar_folha(final, laudo, foto_guardada):
    """Guarda a folha de imagens como saiu no final (nome do laudo, paginas certas):
    e ela que e usada numa correcao depois que o DICOM for apagado."""
    n = len(PdfReader(str(laudo)).pages)
    def escrever(p):
        w = PdfWriter()
        w.append(str(final), pages=(n, len(PdfReader(str(final)).pages)))
        with open(p, "wb") as f:
            w.write(f)
    gravar_atomico(Path(foto_guardada), escrever)


def _nome_refeito(nome, cfg):
    """'..._Ecocardiograma.pdf' -> '..._Ecocardiograma_retificado.pdf': quem ja subiu
    o anterior ve pelo nome que este e outro. Nao acumula sufixos."""
    suf = cfg["sufixo_refeito"]
    stem = Path(nome).stem
    stem = re.sub(r"_\d{6}$", "", stem)  # laudo guardado pode ter sufixo _HHMMSS
    if suf and not stem.endswith(suf):
        stem += suf
    return stem + ".pdf"


def _tem_foto(r):
    return bool(r.get("foto")) and Path(r["foto"]).exists()


def _substituir_final(r, laudo, cfg, motivo, nome=None):
    """Refaz o final de um exame do registro com 'laudo' + imagens atuais;
    a versao anterior vai para processados/substituidos."""
    agora = datetime.now()
    antigo = cfg["pasta_final"] / r["final"]
    if antigo.exists():
        _mover(antigo, cfg["pasta_processados"] / "substituidos", f"{agora:%Y-%m-%d_%H%M%S}_{antigo.name}")
    final = cfg["pasta_final"] / _nome_refeito(nome or r["final"], cfg)
    if _tem_foto(r):
        gravar_atomico(final, lambda p: _mesclar(laudo, Path(r["foto"]), p, cfg))
        _guardar_folha(final, laudo, r["foto"])
    else:  # liberado "sem imagens": o final e so o laudo
        gravar_atomico(final, lambda p: shutil.copyfile(laudo, p))
    r.update(final=final.name, laudo=str(laudo), corrigido_em=agora.isoformat(timespec="seconds"),
             motivo=motivo)
    return final


def _laudo_guardado(r, cfg):
    if r.get("laudo") and Path(r["laudo"]).exists():
        return Path(r["laudo"])
    # registros antigos (sem o campo): procura pelo nome do final
    achados = sorted(cfg["pasta_processados"].glob(f"*/laudos/{Path(r['final']).stem}*.pdf"),
                     key=lambda a: a.stat().st_mtime)
    return achados[-1] if achados else None


def _atualizar_final(destino, data, tipo, cfg):
    """Imagens novas de um exame ja finalizado: refaz o final com o mesmo laudo."""
    reg = _ler_registro(cfg)
    cands = [r for r in reg if r["data"] == data and r["tipo"] == tipo
             and Path(r["foto"]).stem.startswith(destino.stem) and Path(r["foto"]).exists()]
    if len(cands) != 1:
        return False
    r = cands[0]
    laudo = _laudo_guardado(r, cfg)
    if laudo is None:
        return False
    final = _substituir_final(r, laudo, cfg, "IMAGENS ADICIONADAS")
    _gravar_registro(cfg, reg)
    log.info("IMAGENS ADICIONADAS: %s refeito com as imagens novas (versao anterior em processados\\substituidos)", final.name)
    return True


def _corrigir(laudo, laudos, fotos, cfg):
    """Laudo reimpresso de um exame ja finalizado: junta com as mesmas imagens,
    substitui o final e guarda a versao anterior. So age com um unico candidato."""
    arq, data, nome, tipo = laudo
    if any(f[1] == data and f[3] == tipo and _mesmo_paciente(f[2], nome, cfg) for f in fotos):
        return False  # ha imagens novas do mesmo exame aguardando: nao e correcao
    reg = _ler_registro(cfg)
    cands = [r for r in reg if r["data"] == data and r["tipo"] == tipo
             and _mesmo_paciente(r["nome"], nome, cfg) and (_tem_foto(r) or r.get("sem_imagens"))]
    if len(cands) != 1:
        return False
    if sum(1 for l in laudos if l[1] == data and l[3] == tipo and _mesmo_paciente(l[2], nome, cfg)) > 1:
        return False  # dois laudos novos do mesmo exame: espera apagar o errado
    r = cands[0]
    final = _substituir_final(r, arq, cfg, "LAUDO CORRIGIDO", arq.name)
    guardado = _mover(arq, cfg["pasta_processados"] / f"{datetime.now():%Y-%m-%d}" / "laudos")
    r.update(nome=nome, laudo=str(guardado))
    _gravar_registro(cfg, reg)
    log.info("LAUDO CORRIGIDO: %s (mesmas imagens; versao anterior em processados\\substituidos)", final.name)
    return True


MARCA_FOLHA = "EcoPDF-folha-de-imagens"
_TRIADOS = {}  # arquivo -> (mtime, tamanho) ja conferidos na triagem


def _texto(paginas):
    return re.sub(r"\s+", " ", " ".join(paginas)).strip()


def _paginas_texto(arq):
    try:
        return [(pg.extract_text() or "") for pg in PdfReader(str(arq)).pages]
    except Exception:
        return None


def _finalizado(laudo, cfg):
    """Registro unico de exame ja finalizado para este laudo (data, tipo, paciente) ou None."""
    _, data, nome, tipo = laudo
    cands = [r for r in _ler_registro(cfg) if r["data"] == data and r["tipo"] == tipo
             and _mesmo_paciente(r["nome"], nome, cfg)]
    return cands[0] if len(cands) == 1 else None


def impressora_papel():
    """Nome da impressora padrao do Windows ('' fora do Windows ou se falhar)."""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        n = ctypes.c_ulong(0)
        ctypes.windll.winspool.GetDefaultPrinterW(None, ctypes.byref(n))
        buf = ctypes.create_unicode_buffer(n.value or 1)
        if ctypes.windll.winspool.GetDefaultPrinterW(buf, ctypes.byref(n)):
            return buf.value
    except Exception:
        pass
    return ""


_AVISO_ABERTO = threading.Event()


def avisar_impressora_errada(nome_arquivo, extra=""):
    """Janela no PC: o documento foi para a PDF24 e NAO saiu em papel. Nao bloqueia o EcoPDF;
    uma janela por vez."""
    imp = impressora_papel()
    if not imp or "pdf24" in imp.lower():
        qual = "a impressora de papel"
    else:
        qual = f"a impressora {imp}"
    msg = (f"Este documento foi para a impressora PDF24 e NÃO saiu em papel.\n\n"
           f"Para imprimir em papel, imprima de novo escolhendo {qual}.\n\n"
           + (extra + "\n\n" if extra else "") + f"Arquivo: {nome_arquivo}")
    log.warning("Aviso de impressora errada: %s %s", nome_arquivo, extra)
    if os.name != "nt" or _AVISO_ABERTO.is_set():
        return

    def janela():
        _AVISO_ABERTO.set()
        try:
            import ctypes
            # MB_OK | MB_ICONWARNING | MB_SYSTEMMODAL | MB_SETFOREGROUND | MB_TOPMOST
            ctypes.windll.user32.MessageBoxW(None, msg, "EcoPDF - impressora", 0x0 | 0x30 | 0x1000 | 0x10000 | 0x40000)
        except Exception:
            pass
        finally:
            _AVISO_ABERTO.clear()

    threading.Thread(target=janela, daemon=True).start()


def _copia(arq, cfg, extra=""):
    _mover(arq, cfg["pasta_processados"] / f"{datetime.now():%Y-%m-%d}" / "copias")
    avisar_impressora_errada(arq.name, extra)


def triagem(laudos, cfg):
    """Antes de juntar: separa o que veio pela PDF24 por engano.
    - PDF com folha de imagens (final impresso na PDF24): nunca e correcao. Laudo igual ao
      guardado (ou so imagens) = copia; diferente = 9 - Revisar.
    - Laudo igual ao do exame ja finalizado (com o final na pasta 3) = copia, sem 'Subir de novo'.
      Se o final sumiu da pasta 3, segue como hoje (reimprimir refaz o final).
    - Laudo igual a outro que ja esta esperando na pasta 1 = copia.
    Devolve a lista de laudos que seguem."""
    seguem = []
    for laudo in laudos:
        arq = laudo[0]
        try:
            st = arq.stat()
            if _TRIADOS.get(arq) == (st.st_mtime, st.st_size):
                seguem.append(laudo)
                continue
            paginas = _paginas_texto(arq)
            if paginas is None:
                seguem.append(laudo)
                continue
            marcadas = [i for i, t in enumerate(paginas) if MARCA_FOLHA in t]
            texto = _texto(t for i, t in enumerate(paginas) if i not in marcadas)
            r = _finalizado(laudo, cfg)
            ref = _laudo_guardado(r, cfg) if r else None
            ref_pg = (_paginas_texto(ref) or []) if ref else []
            ref_txt = _texto(ref_pg) if ref else None
            if (not marcadas and ref_pg and len(paginas) > len(ref_pg)
                    and _texto(paginas[:len(ref_pg)]) == ref_txt):
                marcadas = list(range(len(ref_pg), len(paginas)))  # final de antes da marca
                texto = ref_txt
            if marcadas:
                if not texto or (ref_txt is not None and texto == ref_txt):
                    log.info("COPIA (final impresso na PDF24): %s", arq.name)
                    _copia(arq, cfg)
                else:
                    log.warning("PDF com folha de imagens e laudo diferente do guardado -> 9 - Revisar: %s", arq.name)
                    _mover(arq, cfg["pasta_laudos"].parent / "9 - Revisar")
                    avisar_impressora_errada(arq.name, "Ele tem imagens e o laudo é diferente do guardado: "
                                             "o EcoPDF não usou (está em 9 - Revisar). Para corrigir um laudo, "
                                             "imprima o laudo do Medware, sem as imagens.")
                continue
            if (r and ref_txt is not None and texto == ref_txt
                    and (cfg["pasta_final"] / r["final"]).exists()):
                log.info("COPIA (laudo igual ao do exame ja finalizado): %s", arq.name)
                _copia(arq, cfg)
                continue
            irmao = next((o for o in laudos if o[0] != arq and o[0].exists() and o[1:] == laudo[1:]
                          and (o[0].stat().st_mtime, o[0].name) < (st.st_mtime, arq.name)
                          and _texto(_paginas_texto(o[0]) or []) == texto), None)
            if irmao and texto:
                log.info("COPIA (laudo igual a %s na pasta 1): %s", irmao[0].name, arq.name)
                _copia(arq, cfg)
                continue
            _TRIADOS[arq] = (st.st_mtime, st.st_size)
            seguem.append(laudo)
        except Exception:
            log.exception("Falha na triagem de %s", arq.name)
            seguem.append(laudo)
    return seguem


def juntar_laudos(cfg):
    for chave in ("pasta_laudos", "pasta_imagens", "pasta_final"):
        if not cfg[chave].exists():  # alguem apagou a pasta: recria (o conteudo vai para a Lixeira)
            log.warning("Pasta apagada, recriando: %s", cfg[chave])
            cfg[chave].mkdir(parents=True, exist_ok=True)
    agora = time.time()
    laudos = triagem(_listar(cfg["pasta_laudos"], cfg, agora), cfg)
    fotos = _listar(cfg["pasta_imagens"], cfg, agora)

    erros = processar_pedidos(cfg)
    usadas = set()
    pendentes = []
    for laudo in laudos:
        arq = laudo[0]
        try:
            foto, motivo = _casar(laudo, laudos, fotos, cfg)
            if foto is None:
                if _corrigir(laudo, laudos, fotos, cfg):
                    continue  # laudo reimpresso de exame ja finalizado
            if foto is None or foto[0] in usadas:
                pendentes.append((arq, motivo or "PDF de imagens ja usado por outro laudo"))
                continue
            if not (_livre(arq) and _livre(foto[0])):
                pendentes.append((arq, "PDF aberto em outro programa: feche para juntar"))
                continue  # tenta de novo em 10 s
            final = cfg["pasta_final"] / arq.name
            # final primeiro, depois move: se cair no meio, refaz igual na proxima volta
            gravar_atomico(final, lambda p: _mesclar(arq, foto[0], p, cfg))
            destino = cfg["pasta_processados"] / f"{datetime.now():%Y-%m-%d}"
            laudo_guardado = _mover(arq, destino / "laudos")
            foto_guardada = _mover(foto[0], destino / "fotos")
            usadas.add(foto[0])
            _guardar_folha(final, laudo_guardado, foto_guardada)
            _registrar(cfg, laudo, final.name, foto_guardada, laudo_guardado)
            log.info("FINALIZADO: %s  (+ %s)", final.name, foto[0].name)
        except Exception:
            log.exception("Falha ao juntar %s", arq.name)

    for f in fotos:
        if f[0] not in usadas and f[0].exists() and _juntar_tardio(f, fotos, cfg):
            usadas.add(f[0])
    for f in fotos:
        if f[0] not in usadas:
            pendentes.append((f[0], "sem laudo correspondente em aguardando imagens"))
    pendentes = [(a, m) for a, m in pendentes
                 if a.exists() and agora - a.stat().st_mtime > cfg["pendentes_minutos"] * 60]
    # correcoes dos ultimos 3 dias: lembrar de subir de novo no prontuario
    for r in _ler_registro(cfg):
        em = r.get("corrigido_em")
        if em and agora - datetime.fromisoformat(em).timestamp() < 3 * 86400:
            pendentes.append((cfg["pasta_final"] / r["final"],
                              f"{r.get('motivo', 'LAUDO CORRIGIDO')} em {datetime.fromisoformat(em):%d/%m %H:%M}: subir novamente no prontuario"))
    _escrever_pendentes(cfg, erros + pendentes)


def _escrever_pendentes(cfg, pendentes):
    corpo = "\n".join(f"{a.parent.name} | {a.name}\n    -> {m}" for a, m in sorted(pendentes)) or "Nenhum."
    arq = cfg["pasta_imagens"] / "PENDENTES.txt"
    try:
        if arq.read_text("utf-8").split("\n", 3)[3] == corpo + "\n":
            return  # nada mudou
    except (OSError, IndexError):
        pass
    texto = (f"O que precisa de atencao (sem par ha mais de {cfg['pendentes_minutos']} min, pedidos, avisos).\n"
             f"Sem par: renomeie o PDF (data_Nome_Tipo) igual ao laudo, ou o laudo com ' sem imagens'.\n"
             f"Atualizado em {datetime.now():%d/%m/%Y %H:%M}\n{corpo}\n")
    gravar_atomico(arq, lambda p: p.write_text(texto, "utf-8"))


ROTULOS = r"(?:Paciente|Exame|Data de Nasc\.?|Sexo|Data(?! de)|Idade|Conv[eê]nio|Solicitado por)\s*:"


# --------------------------------------------------------------------------
# Tirar / voltar imagem (pedido pelo nome do arquivo, na pasta 2 ou 3)
# --------------------------------------------------------------------------

_avisados = set()
AVISO_TELA = "tirar/voltar imagem agora e pela tela do EcoPDF (icone EcoPDF na Area de Trabalho); tire o pedido do nome do arquivo"


def _achar_estudo(nome_pdf, cfg):
    for pasta in cfg["pasta_dicom"].iterdir() if cfg["pasta_dicom"].exists() else []:
        for g in _ler_grupos(pasta) if pasta.is_dir() else []:
            if Path(g["pdf"]).name == nome_pdf:
                return pasta, g
    return None, None


def registro_do_grupo(g, reg):
    """Entrada do finalizados.json do exame (grupo de um estudo), ou None."""
    stem = Path(g["pdf"]).stem
    rx = re.compile(re.escape(stem) + r"(_\d{6})?")
    cands = [r for r in reg if r.get("foto") and r["data"] == g["data"] and r["tipo"] == g["tipo"]
             and rx.fullmatch(Path(r["foto"]).stem)]
    return cands[0] if len(cands) == 1 else None


def salvar_folha(pasta, tipo, ordem, fora, cfg):
    """Ordem e imagens da folha escolhidas na tela. Refaz o PDF da pasta 2 ou, se o
    exame ja foi finalizado, o final (com _retificado). Devolve (ok, mensagem)."""
    with TRAVA:
        g = next((x for x in _ler_grupos(pasta) if (x["tipo"] or "") == (tipo or "")), None)
        if g is None:
            return False, "o exame ainda esta sendo recebido; espere a folha ser gerada"
        ref, grupos = _ler_estudo(pasta, cfg)
        itens = grupos.get(g["tipo"], [])
        ids = [i for i, _ in itens]
        ordem = [i for i in ordem if i in ids]
        fora = sorted(set(i for i in fora if i in ids))
        if len(fora) >= len(ids):
            return False, "nao da para tirar todas as imagens"
        pdf = Path(g["pdf"])
        reg, r = None, None
        if pdf.exists():
            if not _livre(pdf):
                return False, "o PDF de imagens esta aberto em outro programa: feche e salve de novo"
        else:
            reg = _ler_registro(cfg)
            r = registro_do_grupo(g, reg)
            if r is None:
                pass  # PDF sumiu da pasta 2 sem juntar (apagado/movido): salvar o recria
            else:
                antigo = cfg["pasta_final"] / r["final"]
                if antigo.exists() and not _livre(antigo):
                    return False, "o PDF final esta aberto em outro programa: feche e salve de novo"
                if _laudo_guardado(r, cfg) is None:
                    return False, "nao achei o laudo guardado deste exame em processados"
        t = g["tipo"] or ""
        o, f = _ler_ordem(pasta), _ler_ocultas(pasta)
        o[t], f[t] = ordem, fora
        gravar_atomico(pasta / ORDEM, lambda p: p.write_text(json.dumps(o, ensure_ascii=False, indent=1), "utf-8"))
        gravar_atomico(pasta / OCULTAS, lambda p: p.write_text(json.dumps(f, ensure_ascii=False, indent=1), "utf-8"))
        (pasta / GERADO).touch()  # refeito aqui; o vigia nao precisa refazer
        n = len(ids) - len(fora)
        if r is None:
            imagens = _numeradas(pasta, g["tipo"], itens)
            gravar_atomico(pdf, lambda p: _desenhar(p, imagens, cfg, dados_do_dicom(ref, cfg)))
            log.info("FOLHA AJUSTADA na tela: %s (%d de %d imagens)", pdf.name, n, len(ids))
            return True, f"Folha salva: {n} imagens."
        final = _substituir_final(r, _laudo_guardado(r, cfg), cfg, "FOLHA AJUSTADA")
        _gravar_registro(cfg, reg)
        log.info("FOLHA AJUSTADA na tela (exame finalizado): %s (%d de %d imagens)", final.name, n, len(ids))
        return True, f"Final refeito: {final.name}"


def _liberar_sem_imagens(arq, m, cfg):
    """Laudo sem imagens (esqueceu o P1, aparelho sem EcoPDF, exame antigo):
    vai sozinho para a pasta 3. Se as imagens chegarem depois, junta e avisa."""
    base = m["base"] + ".pdf"
    info = ler_nome(Path(base), cfg["tipos"])
    if not info:
        return "nome fora do padrao AAAA_MM_DD_Nome_Tipo antes de 'sem imagens'"
    data, nome, tipo = info
    agora = time.time()
    if any(f[1] == data and f[3] == tipo and _mesmo_paciente(f[2], nome, cfg)
           for f in _listar(cfg["pasta_imagens"], cfg, agora + 60)):
        return "ha imagens deste exame na pasta 2: tire o 'sem imagens' do nome para juntar"
    if not _livre(arq):
        return "PDF aberto em outro programa: feche para liberar"
    final = cfg["pasta_final"] / base
    if final.exists():
        _mover(final, cfg["pasta_processados"] / "substituidos", f"{datetime.now():%Y-%m-%d_%H%M%S}_{base}")
    gravar_atomico(final, lambda p: shutil.copyfile(arq, p))
    guardado = _mover(arq, cfg["pasta_processados"] / f"{datetime.now():%Y-%m-%d}" / "laudos", base)
    reg = _ler_registro(cfg)
    reg.append({"data": data, "nome": nome, "tipo": tipo, "final": base, "foto": "",
                "laudo": str(guardado), "sem_imagens": True,
                "em": datetime.now().isoformat(timespec="seconds")})
    _gravar_registro(cfg, reg)
    log.info("LIBERADO SEM IMAGENS: %s", base)
    return None


def _mtime(f):
    try:
        return f.stat().st_mtime
    except OSError:
        return 0.0


def _ler_descartados(pasta_estudo):
    return _ler_json(pasta_estudo / DESCARTADOS, {})


def descartar(pasta, tipo, cfg):
    """Botao Descartar da tela: imagens que nunca terao laudo (teste, duplicado, paciente
    que saiu). O PDF sai da pasta 2 para processados/descartados; o DICOM fica pelo prazo
    normal. Nao junta sozinho depois: Restaurar devolve a folha. (ok, mensagem)."""
    with TRAVA:
        t = tipo or ""
        g = next((x for x in _ler_grupos(pasta) if (x["tipo"] or "") == t), None)
        if g is None:
            return False, "o exame ainda esta sendo recebido"
        if registro_do_grupo(g, _ler_registro(cfg)):
            return False, "exame ja finalizado: nao da para descartar"
        pdf = Path(g["pdf"])
        if pdf.exists():
            if not _livre(pdf):
                return False, "o PDF de imagens esta aberto em outro programa: feche e tente de novo"
            _mover(pdf, cfg["pasta_processados"] / "descartados", f"{datetime.now():%Y-%m-%d_%H%M%S}_{pdf.name}")
        d = _ler_descartados(pasta)
        d[t] = datetime.now().isoformat(timespec="seconds")
        gravar_atomico(pasta / DESCARTADOS, lambda p: p.write_text(json.dumps(d), "utf-8"))
        log.info("DESCARTADO na tela: %s", pdf.name)
        return True, "Exame descartado: saiu de Pendentes. Se precisar, abra e clique em Restaurar."


def restaurar(pasta, tipo, cfg):
    """Desfaz o Descartar: refaz a folha na pasta 2 (junta se o laudo ja estiver na 1)."""
    t = tipo or ""
    with TRAVA:
        d = _ler_descartados(pasta)
        if t not in d:
            return False, "este exame nao esta descartado"
        g = next((x for x in _ler_grupos(pasta) if (x["tipo"] or "") == t), None)
        ok, msg = salvar_folha(pasta, tipo, _ler_ordem(pasta).get(t, []), _ler_ocultas(pasta).get(t, []), cfg) \
            if g and not Path(g["pdf"]).exists() else (True, "")
        if not ok:
            return False, msg
        d.pop(t, None)
        gravar_atomico(pasta / DESCARTADOS, lambda p: p.write_text(json.dumps(d), "utf-8"))
        log.info("RESTAURADO na tela: %s", Path(g["pdf"]).name if g else pasta.name)
        return True, "Exame restaurado: a folha voltou para a pasta 2 e junta com o laudo, se ele ja estiver la."


def _juntar_tardio(foto, fotos, cfg):
    """PDF de imagens que chegou depois de o laudo ter sido liberado 'sem imagens'."""
    arq, data, nome, tipo = foto
    if sum(1 for f in fotos if f[1] == data and f[3] == tipo and _mesmo_paciente(f[2], nome, cfg)) != 1:
        return False
    reg = _ler_registro(cfg)
    cands = [r for r in reg if r.get("sem_imagens") and not _tem_foto(r) and r["data"] == data
             and r["tipo"] == tipo and _mesmo_paciente(r["nome"], nome, cfg)]
    if len(cands) != 1 or not _livre(arq):
        return False
    r = cands[0]
    laudo = _laudo_guardado(r, cfg)
    if laudo is None:
        return False
    r["foto"] = str(_mover(arq, cfg["pasta_processados"] / f"{datetime.now():%Y-%m-%d}" / "fotos"))
    r["sem_imagens"] = False
    final = _substituir_final(r, laudo, cfg, "IMAGENS ADICIONADAS")
    _gravar_registro(cfg, reg)
    log.info("IMAGENS ADICIONADAS: %s (laudo tinha sido liberado sem imagens)", final.name)
    return True


def processar_pedidos(cfg):
    """'sem imagens' na pasta 1. Pedido antigo 'tirar/voltar' no nome do arquivo (pastas
    2 e 3) nao e mais atendido: vira aviso no PENDENTES.txt para usar a tela."""
    erros = []
    for arq in cfg["pasta_laudos"].glob("*.pdf") if cfg["pasta_laudos"].exists() else []:
        m = SEM_IMG.match(arq.stem)
        if m:
            try:
                erro = _liberar_sem_imagens(arq, m, cfg)
            except Exception:
                log.exception("Falha ao liberar %s", arq.name)
                erro = "erro ao liberar (veja o log)"
            if erro:
                erros.append((arq, erro))
    for pasta in (cfg["pasta_imagens"], cfg["pasta_final"]):
        for arq in pasta.glob("*.pdf") if pasta.exists() else []:
            if not PEDIDO.match(arq.stem):
                continue
            # o pedido pelo nome do arquivo saiu (2026.10.31.3): so avisa
            erros.append((arq, AVISO_TELA))
            if arq.name not in _avisados:
                log.warning("Pedido pelo nome do arquivo ignorado: %s", arq.name)
                _avisados.add(arq.name)
    return erros


def dados_do_laudo(arq, tipos):
    """Campos do quadro 'Dados do Paciente' do laudo, como estao escritos nele.
    Sem texto legivel, so o nome (tirado do nome do arquivo)."""
    try:
        texto = PdfReader(str(arq)).pages[0].extract_text() or ""
    except Exception:
        texto = ""

    def campo(rotulo):
        m = re.search(rotulo + r"\s*:\s*(.*?)\s*(?=" + ROTULOS + r"|\n|$)", texto)
        return " ".join(m.group(1).split()) if m else ""

    linhas = [l.strip() for l in texto.splitlines() if l.strip()]
    endereco = ""
    for k, l in enumerate(linhas[:-1]):
        if "Ecocardio" in l and "Paciente" not in linhas[k + 1]:
            endereco = linhas[k + 1]
            break
    dados = {
        "paciente": campo("Paciente"), "exame": campo("Exame"),
        "nasc": campo(r"Data de Nasc\.?"), "sexo": campo("Sexo"),
        "idade": campo("Idade"), "convenio": campo(r"Conv[eê]nio"),
        "solicitado": campo("Solicitado por"), "endereco": endereco,
    }
    nome = dados["paciente"]
    if "," in nome:  # 'FERNANDES, LARISSA BRAGA' -> 'LARISSA BRAGA FERNANDES'
        sobrenome, _, resto = nome.partition(",")
        nome = f"{resto.strip()} {sobrenome.strip()}"
    if not nome.strip():
        stem = re.sub(r"_\d+$", "", arq.stem)[11:]  # tira AAAA_MM_DD_ e sufixo _2
        for tipo in sorted(tipos, key=len, reverse=True):
            if stem.lower().endswith("_" + tipo.lower()):
                stem = stem[: -len(tipo) - 1]
                break
        nome = stem.replace("_", " ")
    dados["paciente"] = " ".join(nome.split()).upper()
    return dados


def _folha_do_laudo(laudo, foto, cfg, tmp):
    """Refaz a folha de imagens com os dados do laudo. Se o PDF de imagens nao veio
    do EcoPDF (renomeado a mao, outro aparelho), usa-o como esta."""
    for pasta in cfg["pasta_dicom"].iterdir():
        for g in _ler_grupos(pasta) if pasta.is_dir() else []:
            if Path(g["pdf"]).name == foto.name:
                ref, grupos = _ler_estudo(pasta, cfg)
                imagens = _numeradas(pasta, g["tipo"], grupos.get(g["tipo"], []))
                if imagens:
                    dados = dados_do_laudo(laudo, cfg["tipos"])
                    dados["data"] = data_br(ref.get("StudyDate", ""))  # data das imagens
                    dados["sigla"] = sigla(ref, cfg)
                    _desenhar(tmp, imagens, cfg, dados,
                              primeira_pagina=len(PdfReader(str(laudo)).pages) + 1)
                    return tmp
    return foto


RE_PAGINA = re.compile(r"P.gina:\s*\d+")


def _renumerar(folha, primeira, destino):
    """Folha guardada (DICOM ja apagado) com a barra 'Pagina: N' refeita a partir de
    'primeira'. So mexe em folha do EcoPDF (A4 com a barra); se nao der, devolve a folha."""
    try:
        r = PdfReader(str(folha))
        larg, alt = A4
        if not all(abs(float(pg.mediabox.width) - larg) < 2 and abs(float(pg.mediabox.height) - alt) < 2
                   and RE_PAGINA.search(pg.extract_text() or "") for pg in r.pages):
            return folha
        w = PdfWriter()
        for k, pg in enumerate(r.pages):
            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=A4)
            # cobre so o meio da barra (bordas e o final da serie na ponta direita ficam)
            c.setFillColorRGB(*CINZA)
            c.rect(larg / 2 - 60, 53, 120, 16, fill=1, stroke=0)
            c.setFillColorRGB(0, 0, 0)
            c.setFont("Helvetica", 7)
            c.drawCentredString(larg / 2, 52 + 9 - 7 * 0.35, f"Página: {primeira + k}")
            c.save()
            pg.merge_page(PdfReader(io.BytesIO(buf.getvalue())).pages[0])
            w.add_page(pg)
        with open(destino, "wb") as f:
            w.write(f)
        return destino
    except Exception:
        log.exception("Nao consegui renumerar a folha guardada; vai como estava")
        return folha


def _mesclar(laudo, foto, destino, cfg):
    tmp = destino.with_name(destino.name + ".folha.tmp")
    tmp2 = destino.with_name(destino.name + ".renum.tmp")
    try:
        folha = _folha_do_laudo(laudo, foto, cfg, tmp)
        if folha == foto:  # sem DICOM: folha guardada, com a numeracao do laudo antigo
            folha = _renumerar(foto, len(PdfReader(str(laudo)).pages) + 1, tmp2)
        w = PdfWriter()
        w.append(str(laudo))
        w.append(str(folha))
        with open(destino, "wb") as f:
            w.write(f)
    finally:
        for t in (tmp, tmp2):
            if t.exists():
                t.unlink()


# --------------------------------------------------------------------------
# Vigia: decide quando um estudo esta completo e roda a juncao
# --------------------------------------------------------------------------

def vigiar(cfg, parar):
    ultima_limpeza = 0.0
    laudo_visto = time.time()  # laudo que chega na pasta 1 depois disso abre a tela (TELA_ABRIR = ao_receber)
    while not parar.is_set():
        agora = time.time()
        try:
            novos = [m for m in (_mtime(f) for f in cfg["pasta_laudos"].glob("*.pdf")) if m > laudo_visto]
            if novos:
                laudo_visto = max(novos)
                _avisar_tela(cfg)
            for pasta in cfg["pasta_dicom"].iterdir():
                dcms = list(pasta.glob("*.dcm")) if pasta.is_dir() else []
                if not dcms:
                    continue
                ultima = max(f.stat().st_mtime for f in dcms)
                m = pasta / GERADO
                feito = m.stat().st_mtime if m.exists() else 0
                if ultima > feito and agora - ultima >= cfg["espera"]:
                    with TRAVA:
                        if (pasta / DESCARTADOS).exists():  # imagem nova: o exame voltou a valer
                            (pasta / DESCARTADOS).unlink()
                            log.info("Imagem nova em exame descartado: volta ao normal (%s)", pasta.name)
                        try:
                            t0 = time.monotonic()
                            gerar_pdfs_do_estudo(pasta, cfg)
                            TEMPOS_FOLHA.append((time.time(), time.monotonic() - t0))
                        except Exception:
                            log.exception("Falha ao gerar PDF do estudo %s", pasta.name)
                        m.touch()  # nao repete em laco; imagem nova reabre o estudo
            with TRAVA:
                juntar_laudos(cfg)
                if agora - ultima_limpeza > 3600:
                    limpar_antigos(cfg)
                    ultima_limpeza = agora
        except Exception:
            log.exception("Erro no vigia")
        parar.wait(10)


def limpar_antigos(cfg):
    dias = cfg["manter_dicom_dias"]
    if dias <= 0:
        return
    limite = time.time() - dias * 86400
    for pasta in cfg["pasta_dicom"].iterdir():
        m = pasta / GERADO
        if pasta.is_dir() and m.exists() and m.stat().st_mtime < limite:
            shutil.rmtree(pasta)  # inclui a subpasta previa/ da tela
            log.info("DICOM antigo removido: %s", pasta.name)
    # Pasta 3: os medicos nao apagam o que ja subiu; o EcoPDF apaga o final depois do
    # mesmo prazo (conta da ultima vez que o arquivo foi gerado). So mexe em arquivos que
    # ele mesmo gerou (estao no registro); laudo e folha guardados em processados ficam.
    nomes = {r.get("final") for r in _ler_registro(cfg) if r.get("final")}
    for nome in nomes:
        arq = cfg["pasta_final"] / nome
        try:
            if arq.is_file() and arq.stat().st_mtime < limite:
                arq.unlink()
                log.info("Final antigo removido da pasta 3: %s", nome)
        except OSError:
            pass  # aberto em outro programa: tenta na proxima limpeza


# --------------------------------------------------------------------------

def travar_instancia(pasta):
    """Garante uma copia so. No Windows a porta nao basta: SO_REUSEADDR deixa
    duas copias ouvindo a mesma porta. A trava some sozinha se o processo morrer."""
    f = open(pasta / "ecopdf.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return f  # manter referencia viva enquanto o programa roda


def iniciar_renomeador(cfg):
    """Roda o renomear_laudos.py (o de sempre, sem mudar a logica) dentro do EcoPDF:
    0 - Entrada PDF24 -> 1 - Laudos aguardando imagens (ou 9 - Revisar).
    Um programa so para instalar, iniciar e manter."""
    script = BASE / "renomear_laudos.py"
    if not cfg["renomeador"] or not script.exists():
        return
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location("renomear_laudos", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        log.exception("Renomeador nao carregou (falta pdfplumber/watchdog? rode instalar.bat)")
        return
    raiz = cfg["pasta_laudos"].parent
    mod.BASE = raiz
    mod.ENTRADA = raiz / "0 - Entrada PDF24"
    mod.PRONTOS = cfg["pasta_laudos"]
    mod.REVISAR = raiz / "9 - Revisar"
    mod.LOG = cfg["pasta_log"] / "renomeador.log"
    mod.AO_REVISAR = lambda nome: avisar_impressora_errada(
        nome, "Não parece um laudo (sem nome do paciente ou data): foi para 9 - Revisar.")

    def rodar():
        while True:
            try:
                mod.main()
            except Exception:
                log.exception("Renomeador parou; reiniciando em 30 s")
            time.sleep(30)
    threading.Thread(target=rodar, daemon=True, name="renomeador").start()
    log.info("Renomeador ativo: %s -> %s", mod.ENTRADA, mod.PRONTOS)


def ocultar_pasta_laudos(cfg):
    """Deixa a pasta-mae das pastas 0-9 ("Laudos - Nao Apagar") oculta na Area de Trabalho.
    O caminho nao muda (PDF24 continua igual); o medico chega na pasta 3 pelo botao
    Pasta de prontos da tela. OCULTAR_PASTAS = nao deixa visivel de novo. So age quando ela fica direto na Area de Trabalho."""
    if os.name != "nt":
        return
    mae = cfg["pasta_laudos"].parent
    if mae.parent != area_de_trabalho():  # so a pasta que fica direto na Area de Trabalho
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        atual = k.GetFileAttributesW(str(mae))
        if atual in (-1, 0xFFFFFFFF):
            return
        novo = (atual | 0x2) if cfg["ocultar_pastas"] else (atual & ~0x2)
        if novo != atual:
            k.SetFileAttributesW(str(mae), novo)
            log.info("Pasta dos laudos %s: %s", "oculta" if cfg["ocultar_pastas"] else "visivel de novo", mae)
    except Exception:
        log.warning("Nao consegui ocultar %s", mae)


PS_ATALHO_PRONTOS = (
    "$p=$env:ECOPDF_P3; $d=[Environment]::GetFolderPath('Desktop');"
    "$w=New-Object -ComObject WScript.Shell; $l=$w.CreateShortcut((Join-Path $d 'Laudos prontos.lnk'));"
    "$l.TargetPath=$p; $l.Description='Pasta 3 - Finalizados (subir no prontuario)'; $l.Save();"
    "if ($env:ECOPDF_FIXAR -eq '1') { (New-Object -ComObject Shell.Application).Namespace($p).Self.InvokeVerb('pintohome') }"
)


def atalho_laudos_prontos(cfg):
    """Com a pasta Laudos oculta, a pasta 3 precisa continuar facil de achar, inclusive na janela
    'Abrir arquivo' do prontuario: atalho 'Laudos prontos' na Area de Trabalho e pasta 3 fixada
    no Acesso rapido (aparece na lateral de toda janela de escolher arquivo). Fixa so uma vez,
    para respeitar quem desafixar de proposito; o atalho e recriado se for apagado."""
    if os.name != "nt" or not cfg["ocultar_pastas"]:
        return
    marca = cfg["pasta_log"] / "acesso_rapido.ok"
    atalho = area_de_trabalho() / "Laudos prontos.lnk"
    fixar = not marca.exists()
    if atalho.exists() and not fixar:
        return
    try:
        import subprocess
        env = dict(os.environ, ECOPDF_P3=str(cfg["pasta_final"]), ECOPDF_FIXAR="1" if fixar else "0")
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PS_ATALHO_PRONTOS],
                           env=env, capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0:
            if fixar:
                marca.write_text(datetime.now().isoformat(timespec="seconds"), "utf-8")
            log.info("Atalho 'Laudos prontos' na Area de Trabalho%s", " e pasta 3 no Acesso rapido" if fixar else "")
        else:
            log.warning("Nao consegui criar o atalho Laudos prontos: %s", r.stderr[-300:])
    except Exception:
        log.warning("Nao consegui criar o atalho Laudos prontos")


def main():
    cfg = carregar_config(BASE / "config.ini")
    configurar_log(cfg["pasta_log"])
    trava = travar_instancia(cfg["pasta_log"])
    if trava is None:
        log.warning("Outro EcoPDF ja esta rodando. Encerrando esta copia.")
        return
    # PC novo: cria todas as pastas do fluxo ja na primeira execucao (as do renomeador tambem)
    extras = [cfg["pasta_laudos"].parent / n for n in ("0 - Entrada PDF24", "9 - Revisar")]
    for p in [cfg["pasta_dicom"], cfg["pasta_laudos"], cfg["pasta_imagens"], cfg["pasta_final"], *extras]:
        p.mkdir(parents=True, exist_ok=True)
    ocultar_pasta_laudos(cfg)
    threading.Thread(target=atalho_laudos_prontos, args=(cfg,), daemon=True).start()
    iniciar_renomeador(cfg)

    ae = AE(ae_title=cfg["ae_title"])
    ae.maximum_pdu_size = 0
    ae.add_supported_context(Verification)
    for cx in AllStoragePresentationContexts:
        ae.add_supported_context(cx.abstract_syntax, TRANSFER_SYNTAXES)

    parar = threading.Event()
    threading.Thread(target=vigiar, args=(cfg, parar), daemon=True).start()
    try:
        global TELA
        import tela
        if tela.iniciar(cfg, sys.modules[__name__]):
            TELA = tela
    except Exception:
        log.exception("Tela EcoPDF - Exames nao iniciou (o resto continua funcionando)")
    try:
        import painel
        painel.iniciar(cfg, sys.modules[__name__], parar)
    except Exception:
        log.exception("Painel das salas nao iniciou (o resto continua funcionando)")

    log.info("EcoPDF %s ouvindo: AE=%s porta=%d | fotos em %s | laudos em %s -> %s",
             versao(), cfg["ae_title"], cfg["porta"], cfg["pasta_imagens"],
             cfg["pasta_laudos"], cfg["pasta_final"])
    BASE_CONEXAO[0] = cfg["pasta_log"] / "conexao.json"
    handlers = [(evt.EVT_C_ECHO, on_echo), (evt.EVT_C_STORE, on_store, [cfg]),
                (evt.EVT_ACCEPTED, on_conectou, [cfg]), (evt.EVT_REJECTED, on_recusou, [cfg])]
    try:
        ae.start_server(("0.0.0.0", cfg["porta"]), block=True, evt_handlers=handlers)
    except OSError:
        log.exception("Nao consegui abrir a porta %d (outro programa usando?)", cfg["porta"])
        os._exit(1)  # sai de vez; o agendador tenta de novo em 1 minuto
    except KeyboardInterrupt:
        pass
    finally:
        parar.set()


if __name__ == "__main__":
    main()
