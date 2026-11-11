"""
Renomeia automaticamente os PDFs de laudo que a impressora PDF24 salva em
C:\\Laudos\\entrada, para o formato:

    AAAA_MM_DD_Nome_Do_Paciente_Tipo_Exame.pdf

- Le o nome e a data de realizacao (Data Ex / Data) de dentro do proprio PDF.
- Sucesso  -> C:\\Laudos\\prontos
- Falha    -> C:\\Laudos\\revisar (com o nome original; nada e perdido)
- Tudo roda localmente. Nenhum dado sai do computador.

Uso:  python renomear_laudos.py            (pasta padrao C:\\Laudos)
      python renomear_laudos.py D:\\Outra   (outra pasta base)
"""

import logging
import queue
import re
import shutil
import sys
import threading
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import pdfplumber
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ----------------------------------------------------------------------------
# CONFIGURACAO
# ----------------------------------------------------------------------------
BASE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"C:\Laudos")
ENTRADA = BASE / "0 - Entrada PDF24"
PRONTOS = BASE / "1 - Laudos aguardando imagens"
REVISAR = BASE / "9 - Revisar"
AO_REVISAR = None  # o EcoPDF preenche: aviso de impressora errada
AO_PRONTO = None   # o EcoPDF preenche: laudo entregue na pasta 1 (junta na hora)
LOG = BASE / "log.txt"

MANTER_ACENTOS = False  # False: "JOAO" em vez de "JOAO" com til (mais seguro p/ upload/e-mail)

# Particulas que ficam em minuscula no nome (Maria da Silva)
PARTICULAS = {"de", "da", "do", "das", "dos", "e"}

# Palavras que encerram o campo "Paciente" na mesma linha do PDF
PARAR_NOME = {"Data", "Exame", "Exame:", "Nº", "N°", "No", "Sexo", "Convênio", "Convenio", "Idade",
              "Nasc", "Nasc.", "CPF", "RG", "Prontuário", "Prontuario", "Registro", "Atendimento",
              "Código", "Codigo", "Realizado"}

# Tipo de exame: se o titulo do laudo contiver a chave (sem acento, minusculo),
# usa o nome da direita. Ajuste/adicione livremente. Se nao houver regra, usa
# o proprio titulo do laudo.
TIPOS_PERSONALIZADOS = {
    # "duplex scan vascular carotidas": "Doppler_Carotidas",
}

# Titulo do laudo -> tipo do [tipos] do config.ini (cada medico/modelo escreve de um jeito:
# "Ecocardiograma", "Ecodopplercardiograma transtoracico", "Doppler de carotidas e vertebrais").
# Testado na ordem, no titulo sem acento e minusculo. None = nao e o eco comum: fica o titulo.
TIPOS_POR_PALAVRA = [
    (r"fetal|idade gestacional", "Ecocardiograma_Fetal"),
    (r"carotid|vertebr", "Duplex_Scan_Vascular_Carotidas"),
    (r"estresse|stress|esforco|dobutamina|transesofag", None),
    (r"(?:eco|doppler)\w*cardiogra|ecocardiografia|transtoracic", "Ecocardiograma"),
]

# ----------------------------------------------------------------------------
# EXTRACAO
# ----------------------------------------------------------------------------
# Data do exame. Cada modelo de laudo usa um rotulo: "Data :", "Data Ex:", "Data Exame:",
# "Data do Exame:", "Realizado em:", "Data de realizacao:", "Data do atendimento:".
# Na ordem: primeiro o rotulo que diz que e a data do exame, depois "Data :" sozinho.
# Nunca a data de nascimento ("Data Nasc.:", "Data de Nascimento:").
DATA = r"(\d{2}/\d{2}/\d{4})"
RE_DATAS = [
    re.compile(r"(?:Data\s*(?:d[oae]\s+)?(?:Ex(?:ame)?\b\.?|Realiza\w*|Atendimento)|Realizad[oa]\s+em)"
               r"\s*:?\s*" + DATA, re.I),
    re.compile(r"\bData\s*:\s*" + DATA, re.I),
]
RE_DATA = RE_DATAS[1]  # compatibilidade
RE_QUALQUER_DATA = re.compile(r"\b" + DATA + r"\b")
RE_NASC = re.compile(r"nasc", re.I)
# Nome do paciente: "Paciente:", "Paciente :", "Nome:", "Nome do Paciente:" (nao "Dados do Paciente")
RE_NOME = re.compile(r"^(?:(?:Nome\s+(?:d[oa]\s+)?)?Paciente\b\s*:?|Nome\s*:)\s*(.*)$", re.I)
# Laudo fetal (vem de outra plataforma, com titulo livre): reconhecido pelo conteudo
RE_FETAL = re.compile(r"(?:cardiograma|eco)\w*\s+fetal|idade\s+gestacional")


def sem_acento(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def agrupar_linhas(words, tol=3.0):
    """Agrupa palavras por linha (coordenada vertical) e ordena da esquerda p/ direita."""
    linhas, atual, ref = [], [], None
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if ref is None or abs(w["top"] - ref) <= tol:
            atual.append(w)
            ref = w["top"] if ref is None else ref
        else:
            linhas.append(sorted(atual, key=lambda x: x["x0"]))
            atual, ref = [w], w["top"]
    if atual:
        linhas.append(sorted(atual, key=lambda x: x["x0"]))
    return linhas


def _data_valida(txt):
    try:
        d = datetime.strptime(txt, "%d/%m/%Y")
    except ValueError:
        return False
    return 2000 <= d.year and d <= datetime.now() + timedelta(days=1)


def _nome_da_linha(junto):
    m = RE_NOME.match(junto)
    if not m:
        return None
    partes = []
    for t in m.group(1).split():
        if t == ":":
            continue
        if t in PARAR_NOME or t.endswith(":") or RE_QUALQUER_DATA.search(t):
            break
        partes.append(t)
    return " ".join(partes) or None


def extrair_campos(pdf_path: Path):
    """Retorna (nome, data 'dd/mm/aaaa', titulo/tipo bruto) ou None nos que faltarem.
    Aceita varios modelos de laudo (cada medico usa um no Medware)."""
    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            return None, None, None
        pagina = pdf.pages[0]
        palavras = pagina.extract_words()
        texto_pagina = pagina.extract_text() or ""

    linhas = agrupar_linhas(palavras)
    nome = titulo = None
    juntos = [" ".join(w["text"] for w in linha) for linha in linhas]

    for i, junto in enumerate(juntos):
        # Nome: linha que comeca com "Paciente"/"Nome" (ignora o cabecalho "Dados do Paciente")
        if nome is None:
            nome = _nome_da_linha(junto)

        # Titulo do exame: linha logo abaixo de "Solicitado por" / "Solicitador por" / "Solicitante"
        if titulo is None and junto.lower().startswith("solicit") and i + 1 < len(juntos):
            titulo = juntos[i + 1]

    # Data de realizacao: rotulo de data do exame em qualquer linha; depois "Data :"
    data = None
    for rx in RE_DATAS:
        for junto in juntos:
            m = rx.search(junto)
            if m and _data_valida(m.group(1)):
                data = m.group(1)
                break
        if data:
            break
    if data is None:
        # Sem rotulo conhecido: unica data do cabecalho que nao e a de nascimento
        cab, vistas = juntos[:10], []
        for junto in cab:
            for m in RE_QUALQUER_DATA.finditer(junto):
                rotulo = junto[:m.start()].rsplit(":", 1)[0][-20:]  # texto logo antes da data
                if not RE_NASC.search(rotulo) and _data_valida(m.group(1)) and m.group(1) not in vistas:
                    vistas.append(m.group(1))
        if len(vistas) == 1:
            data = vistas[0]

    # Fetal vence qualquer titulo: o EcoPDF junta pelo tipo "Ecocardiograma_Fetal"
    if RE_FETAL.search(sem_acento(texto_pagina).lower()):
        titulo = "Ecocardiograma Fetal"

    # Laudo de eco sem linha de titulo: identifica pelo conteudo
    if titulo is None:
        t = sem_acento(texto_pagina).lower()
        if ("unidimensional" in t or "ecocardiograma" in t or "laudo descritivo" in t
                or "ecodopplercardiograma" in t):
            titulo = "Ecocardiograma"

    return nome, data, titulo


# ----------------------------------------------------------------------------
# NOMES
# ----------------------------------------------------------------------------
def limpar(s: str) -> str:
    if not MANTER_ACENTOS:
        s = sem_acento(s)
    s = re.sub(r"[^\w]+", "_", s, flags=re.UNICODE)
    return s.strip("_")


def formatar_nome(nome: str) -> str:
    palavras = []
    for p in nome.split():
        p = p.lower()
        palavras.append(p if p in PARTICULAS else p.capitalize())
    return limpar(" ".join(palavras))


def formatar_tipo(titulo: str | None) -> str:
    if not titulo:
        return "Exame"
    chave = sem_acento(titulo).lower()
    for trecho, nome_curto in TIPOS_PERSONALIZADOS.items():
        if trecho in chave:
            return nome_curto
    for rx, tipo in TIPOS_POR_PALAVRA:
        if re.search(rx, chave):
            if tipo:
                return tipo
            break
    palavras = [w.capitalize() if w.lower() not in PARTICULAS else w.lower()
                for w in titulo.split()]
    return limpar(" ".join(palavras)) or "Exame"


def formatar_data(data: str) -> str:
    d, m, a = data.split("/")
    return f"{a}_{m}_{d}"


def destino_livre(pasta: Path, nome: str) -> Path:
    alvo = pasta / nome
    if not alvo.exists():
        return alvo
    base, ext = alvo.stem, alvo.suffix
    n = 2
    while (pasta / f"{base}_{n}{ext}").exists():
        n += 1
    return pasta / f"{base}_{n}{ext}"


# ----------------------------------------------------------------------------
# PROCESSAMENTO
# ----------------------------------------------------------------------------
def aguardar_arquivo_pronto(p: Path, tentativas=40, intervalo=0.5) -> bool:
    """Espera o PDF24 terminar de gravar (tamanho estavel e arquivo abrivel)."""
    ultimo = -1
    for _ in range(tentativas):
        try:
            tam = p.stat().st_size
            if tam > 0 and tam == ultimo:
                with open(p, "ab"):  # falha se ainda estiver bloqueado
                    pass
                return True
            ultimo = tam
        except (FileNotFoundError, PermissionError, OSError):
            pass
        time.sleep(intervalo)
    return False


def mover(origem: Path, pasta: Path, nome: str) -> Path:
    alvo = destino_livre(pasta, nome)
    for _ in range(10):
        try:
            shutil.move(str(origem), str(alvo))
            return alvo
        except PermissionError:
            time.sleep(0.5)
    raise PermissionError(f"Arquivo bloqueado: {origem}")


def processar(pdf: Path):
    if not pdf.exists() or pdf.suffix.lower() != ".pdf":
        return
    if not aguardar_arquivo_pronto(pdf):
        logging.error("Arquivo nao ficou pronto: %s", pdf.name)
        return
    try:
        nome, data, titulo = extrair_campos(pdf)
    except Exception as e:  # PDF corrompido, sem texto, etc.
        logging.exception("Erro lendo %s: %s", pdf.name, e)
        nome = data = titulo = None

    if not nome or not data:
        alvo = mover(pdf, REVISAR, pdf.name)
        logging.warning("-> REVISAR (nome=%r data=%r): %s", nome, data, alvo.name)
        if AO_REVISAR:
            try:
                AO_REVISAR(alvo.name)  # EcoPDF: avisa que nao saiu em papel
            except Exception:
                pass
        return

    novo = f"{formatar_data(data)}_{formatar_nome(nome)}_{formatar_tipo(titulo)}.pdf"
    alvo = mover(pdf, PRONTOS, novo)
    logging.info("%s -> %s", pdf.name, alvo.name)
    if AO_PRONTO:
        try:
            AO_PRONTO(alvo)
        except Exception:
            pass


class Handler(FileSystemEventHandler):
    def __init__(self, fila):
        self.fila = fila

    def _enfileirar(self, caminho):
        p = Path(caminho)
        if p.suffix.lower() == ".pdf":
            self.fila.put(p)

    def on_created(self, event):
        if not event.is_directory:
            self._enfileirar(event.src_path)

    def on_moved(self, event):  # PDF24 pode gravar com outro nome e renomear
        if not event.is_directory:
            self._enfileirar(event.dest_path)


def trabalhador(fila):
    vistos = set()
    while True:
        p = fila.get()
        if p in vistos and not p.exists():
            continue
        try:
            processar(p)
        except Exception:
            logging.exception("Falha inesperada em %s", p)
        vistos.add(p)
        if len(vistos) > 500:
            vistos.clear()


def main():
    for pasta in (ENTRADA, PRONTOS, REVISAR):
        pasta.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    logging.info("Monitorando %s", ENTRADA)

    fila = queue.Queue()
    threading.Thread(target=trabalhador, args=(fila,), daemon=True).start()

    # Arquivos que ja estavam na pasta quando o script iniciou
    for existente in sorted(ENTRADA.glob("*.pdf")):
        fila.put(existente)

    obs = Observer()
    obs.schedule(Handler(fila), str(ENTRADA), recursive=False)
    obs.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        obs.stop()
    obs.join()


if __name__ == "__main__":
    main()
