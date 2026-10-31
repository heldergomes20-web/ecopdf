"""
Tela "EcoPDF - Exames": pagina local (so neste PC, 127.0.0.1) para ver as imagens
de cada exame, as medidas do aparelho e escolher/ordenar as imagens da folha.

Roda dentro do EcoPDF (thread). Nao guarda estado proprio: le e grava nos mesmos
arquivos do EcoPDF (dicom/<estudo>/grupos.json, ordem.json, ocultas.json,
processados/finalizados.json). A folha e sempre refeita pela mesma funcao do EcoPDF.
"""

import io
import json
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pydicom

import medidas as med

BASE = Path(__file__).resolve().parent
SEM_LAUDO = ("Recebendo", "Novo", "Folha ajustada", "Pendente")
_cache = {}           # pasta -> (assinatura, info)
_cache_trava = threading.Lock()


# --------------------------------------------------------------------------
# Leitura dos estudos (so cabecalhos: rapido)
# --------------------------------------------------------------------------

def _cabecalhos(pasta, ep, cfg):
    """{tipo: [(chave_ordem, id, ds)]} das imagens que vao para a folha + lista de SR.
    Mesmos ids e mesma ordem que o EcoPDF usa (_ler_estudo)."""
    grupos, srs = {}, []
    for arq in sorted(pasta.glob("*.dcm")):
        try:
            ds = pydicom.dcmread(arq, stop_before_pixels=True)
        except Exception:
            continue
        if med.eh_sr(ds) and "ContentSequence" in ds:
            srs.append(ds)
            continue
        quadros = int(ds.get("NumberOfFrames", 1) or 1)
        if quadros > 1 and cfg["quadro_loop"] == "ignorar":
            continue
        if ds.get("Rows") is None:  # sem imagem
            continue
        grupos.setdefault(ep.tipo_da_imagem(ds, cfg["tipos"]), []).append(
            (ep.momento(ds, arq), f"{arq.stem}#0", ds))
    for itens in grupos.values():
        itens.sort(key=lambda x: x[:2])
    return grupos, srs


def _assinatura(pasta):
    partes = []
    for nome in ("grupos.json", "ordem.json", "ocultas.json", ".gerado"):
        p = pasta / nome
        partes.append(p.stat().st_mtime if p.exists() else 0)
    dcms = list(pasta.glob("*.dcm"))
    partes.append(len(dcms))
    partes.append(max((f.stat().st_mtime for f in dcms), default=0))
    return tuple(partes)


def _info_estudo(pasta, ep, cfg):
    """Resumo do estudo para a lista, com cache enquanto nada mudar na pasta."""
    ass = _assinatura(pasta)
    with _cache_trava:
        c = _cache.get(pasta)
        if c and c[0] == ass:
            return c[1]
    grupos, srs = _cabecalhos(pasta, ep, cfg)
    info = {"tipos": {}, "srs": len(srs)}
    for tipo, itens in grupos.items():
        ds = itens[0][2]
        info["tipos"][tipo] = {
            "n": len(itens),
            "paciente": (ep.nome_paciente(ds.get("PatientName")) or "SEM NOME").upper(),
            "data": str(ds.get("StudyDate", "") or ""),
            "hora": str(ds.get("StudyTime", "") or "")[:4],
            "sigla": ep.sigla(ds),
        }
    with _cache_trava:
        _cache[pasta] = (ass, info)
    return info


def _situacao(pasta, tipo, g, reg, ep, cfg):
    if g is None:
        return "Recebendo"
    pdf = Path(g["pdf"])
    if pdf.exists():
        try:
            idade = time.time() - pdf.stat().st_mtime
        except OSError:
            idade = 0
        if idade > cfg["pendentes_minutos"] * 60:
            return "Pendente"
        t = tipo or ""
        if t in ep._ler_ordem(pasta) or ep._ler_ocultas(pasta).get(t):
            return "Folha ajustada"
        return "Novo"
    if ep.registro_do_grupo(g, reg):
        return "Finalizado"
    return "Fora da pasta 2"


def listar_exames(ep, cfg):
    reg = ep._ler_registro(cfg)
    exames = []
    raiz = cfg["pasta_dicom"]
    for pasta in raiz.iterdir() if raiz.exists() else []:
        if not pasta.is_dir():
            continue
        try:
            info = _info_estudo(pasta, ep, cfg)
        except Exception:
            ep.log.exception("Tela: nao consegui ler o estudo %s", pasta.name)
            continue
        grupos = {(g["tipo"] or ""): g for g in ep._ler_grupos(pasta)}
        for tipo, t in info["tipos"].items():
            g = grupos.get(tipo or "")
            fora = len(ep._ler_ocultas(pasta).get(tipo or "", []))
            exames.append({
                "estudo": pasta.name, "tipo": tipo or "",
                "tipo_nome": (tipo or "Exame").replace("_", " "),
                "paciente": t["paciente"], "data": t["data"], "hora": t["hora"],
                "sigla": t["sigla"], "n": t["n"], "na_folha": t["n"] - fora,
                "medidas": med.categoria(tipo) != "" and info["srs"] > 0,
                "sem_medidas": med.categoria(tipo) == "",
                "situacao": _situacao(pasta, tipo, g, reg, ep, cfg),
            })
    exames.sort(key=lambda e: (e["data"], e["hora"]), reverse=True)
    return exames


def _pasta(estudo, cfg):
    """Pasta do estudo, sem permitir sair de dicom/."""
    if not estudo or "/" in estudo or "\\" in estudo or estudo.startswith("."):
        return None
    p = cfg["pasta_dicom"] / estudo
    return p if p.is_dir() else None


def detalhe(pasta, tipo, ep, cfg):
    grupos, srs = _cabecalhos(pasta, ep, cfg)
    itens = grupos.get(tipo, [])
    if not itens:
        return None
    ids = [i for _, i, _ in itens]
    t = tipo or ""
    fora = set(ep._ler_ocultas(pasta).get(t, []))
    ordem = ep.ordenar(ids, ep._ler_ordem(pasta).get(t, []), [])
    por_id = {i: ds for _, i, ds in itens}
    imagens = []
    for i in ordem:
        ds = por_id[i]
        hora = str(ds.get("ContentTime") or ds.get("AcquisitionTime") or "").split(".")[0]
        imagens.append({"id": i, "fora": i in fora,
                        "hora": f"{hora[0:2]}:{hora[2:4]}:{hora[4:6]}" if len(hora) >= 6 else ""})
    ref = itens[0][2]
    reg = ep._ler_registro(cfg)
    g = next((x for x in ep._ler_grupos(pasta) if (x["tipo"] or "") == t), None)
    situacao = _situacao(pasta, tipo, g, reg, ep, cfg)
    r = ep.registro_do_grupo(g, reg) if g else None
    return {
        "estudo": pasta.name, "tipo": t, "tipo_nome": (tipo or "Exame").replace("_", " "),
        "paciente": (ep.nome_paciente(ref.get("PatientName")) or "SEM NOME").upper(),
        "data": ep.data_br(ref.get("StudyDate", "")),
        "hora": str(ref.get("StudyTime", "") or "")[:4],
        "sigla": ep.sigla(ref), "aparelho": ep.aparelho(ref, cfg),
        "situacao": situacao, "final": r["final"] if r else "",
        "por_pagina": max(1, cfg["colunas"]) * max(1, cfg["linhas"]),
        "imagens": imagens,
        "medidas": med.medidas_do_exame(tipo, ref, srs),
    }


# --------------------------------------------------------------------------
# Imagens (JPEG de previa, feito uma vez e guardado em dicom/<estudo>/previa/)
# --------------------------------------------------------------------------

TAMANHOS = {"g": 1600, "m": 240}


def previa(pasta, ident, tam, ep, cfg):
    stem = ident.split("#")[0]
    if not stem or "/" in stem or "\\" in stem or stem.startswith("."):
        return None
    arq = pasta / f"{stem}.dcm"
    if not arq.exists():
        return None
    destino = pasta / "previa" / f"{stem}_{tam}.jpg"
    if destino.exists():
        return destino.read_bytes()
    ds = pydicom.dcmread(arq)
    imgs = ep.dicom_para_imagens(ds, cfg["quadro_loop"])
    if not imgs:
        return None
    img = imgs[0]
    lado = TAMANHOS.get(tam, 240)
    if max(img.size) > lado:
        img.thumbnail((lado, lado))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90 if tam == "g" else 80)
    dados = buf.getvalue()
    destino.parent.mkdir(exist_ok=True)
    ep.gravar_atomico(destino, lambda p: p.write_bytes(dados))
    return dados


def pdf_previa(pasta, tipo, ordem, fora, ep, cfg):
    ref, grupos = ep._ler_estudo(pasta, cfg)
    itens = grupos.get(tipo, [])
    if not itens:
        return None
    imagens = ep._numeradas(pasta, tipo, itens, ordem=ordem, fora=fora)
    tmp = pasta / f"previa_{uuid.uuid4().hex}.pdf.tmp"
    try:
        ep._desenhar(tmp, imagens, cfg, ep.dados_do_dicom(ref, cfg))
        return tmp.read_bytes()
    finally:
        if tmp.exists():
            tmp.unlink()


# --------------------------------------------------------------------------
# Servidor
# --------------------------------------------------------------------------

def _handler(ep, cfg):
    porta = cfg["porta_tela"]
    hosts = {f"127.0.0.1:{porta}", f"localhost:{porta}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # sem barulho no log

        def _enviar(self, codigo, corpo, tipo="application/json; charset=utf-8", extra=None):
            if isinstance(corpo, (dict, list)):
                corpo = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
            elif isinstance(corpo, str):
                corpo = corpo.encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store" if "json" in tipo or "html" in tipo else "max-age=86400")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(corpo)

        def _ok_host(self):
            # so atende pelo proprio PC (protege contra paginas de fora usando o endereco local)
            if self.headers.get("Host", "") not in hosts:
                self._enviar(403, {"erro": "acesso so pelo proprio PC"})
                return False
            return True

        def do_GET(self):
            if not self._ok_host():
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path in ("/", "/index.html"):
                    return self._enviar(200, (BASE / "tela.html").read_bytes(), "text/html; charset=utf-8")
                if u.path == "/api/exames":
                    return self._enviar(200, {"exames": listar_exames(ep, cfg), "versao": ep.versao(),
                                              "dias_guardados": cfg["manter_dicom_dias"],
                                              "agora": datetime.now().strftime("%H:%M:%S")})
                if u.path == "/api/exame":
                    pasta = _pasta(q.get("e", ""), cfg)
                    d = detalhe(pasta, q.get("t", ""), ep, cfg) if pasta else None
                    return self._enviar(200 if d else 404, d or {"erro": "exame nao encontrado"})
                if u.path == "/img":
                    pasta = _pasta(q.get("e", ""), cfg)
                    dados = previa(pasta, q.get("i", ""), q.get("s", "m"), ep, cfg) if pasta else None
                    if dados is None:
                        return self._enviar(404, {"erro": "imagem nao encontrada"})
                    return self._enviar(200, dados, "image/jpeg")
                return self._enviar(404, {"erro": "pagina nao existe"})
            except Exception:
                ep.log.exception("Tela: erro em %s", u.path)
                return self._enviar(500, {"erro": "erro interno (veja o log do EcoPDF)"})

        def do_POST(self):
            if not self._ok_host():
                return
            u = urlparse(self.path)
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                corpo = json.loads(self.rfile.read(n) or b"{}")
                pasta = _pasta(str(corpo.get("e", "")), cfg)
                if pasta is None:
                    return self._enviar(404, {"erro": "exame nao encontrado"})
                tipo = str(corpo.get("t", ""))
                ordem = [str(x) for x in corpo.get("ordem", [])]
                fora = [str(x) for x in corpo.get("fora", [])]
                if u.path == "/api/salvar":
                    ok, msg = ep.salvar_folha(pasta, tipo, ordem, fora, cfg)
                    return self._enviar(200 if ok else 409, {"ok": ok, "mensagem": msg})
                if u.path == "/api/previa":
                    pdf = pdf_previa(pasta, tipo, ordem, fora, ep, cfg)
                    if pdf is None:
                        return self._enviar(404, {"erro": "sem imagens"})
                    return self._enviar(200, pdf, "application/pdf")
                return self._enviar(404, {"erro": "pagina nao existe"})
            except Exception:
                ep.log.exception("Tela: erro em %s", u.path)
                return self._enviar(500, {"erro": "erro interno (veja o log do EcoPDF)"})
    return H


def iniciar(cfg, ep):
    """Sobe a tela em 127.0.0.1:TELA_PORTA e abre o navegador (uma vez, ao iniciar)."""
    porta = cfg["porta_tela"]
    if porta <= 0:
        return None
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", porta), _handler(ep, cfg))
    except OSError:
        ep.log.exception("Tela: porta %d ocupada; troque TELA_PORTA no config.ini", porta)
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="tela").start()
    url = f"http://127.0.0.1:{porta}/"
    ep.log.info("Tela EcoPDF - Exames em %s", url)
    if cfg.get("abrir_tela"):
        try:
            webbrowser.open(url)
        except Exception:
            ep.log.warning("Nao consegui abrir o navegador; use o atalho EcoPDF - Exames")
    return srv
