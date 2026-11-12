"""
Tela "EcoPDF - Exames": pagina local (so neste PC, 127.0.0.1) para ver as imagens
de cada exame, as medidas do aparelho e escolher/ordenar as imagens da folha.

Roda dentro do EcoPDF (thread). Nao guarda estado proprio: le e grava nos mesmos
arquivos do EcoPDF (dicom/<estudo>/grupos.json, ordem.json, ocultas.json,
processados/finalizados.json). A folha e sempre refeita pela mesma funcao do EcoPDF.
"""

import io
import json
import logging
import re
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import pydicom
from pypdf import PdfReader, PdfWriter

import impressao
import medidas as med
import ocr_imagem

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
    freq = ep.regra_freq(pasta, cfg)
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
        grupos.setdefault(ep.tipo_da_imagem(ds, cfg["tipos"], freq), []).append(
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
    # quando a ultima imagem chegou (AAAAMMDD, relogio do PC): reenvio de exame antigo
    # aparece em "Hoje" mesmo com a data original do exame
    chegou = max((f.stat().st_mtime for f in pasta.glob("*.dcm")), default=0)
    info = {"tipos": {}, "srs": len(srs),
            "chegou": datetime.fromtimestamp(chegou).strftime("%Y%m%d") if chegou else "",
            "chegou_hora": datetime.fromtimestamp(chegou).strftime("%H%M") if chegou else ""}
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
    if (tipo or "") in ep._ler_descartados(pasta):
        return "Descartado"
    if ep.registro_do_grupo(g, reg):
        return "Finalizado"
    return "Fora da pasta 2"


def _subir_de_novo(g, reg, ep, cfg):
    """Final refeito ainda sem "Ja subi" (mesma regra do PENDENTES): motivo, senao ''."""
    r = ep.registro_do_grupo(g, reg) if g else None
    if not ep.subir_de_novo(r, cfg):
        return ""
    em = datetime.fromisoformat(r["corrigido_em"])
    return f"{(r.get('motivo') or 'LAUDO CORRIGIDO').capitalize()} em {em:%d/%m %H:%M}"


def ja_subi(estudo, tipo, ep, cfg):
    """Botao "Ja subi": tira o exame do "Subir de novo"."""
    p = _pasta(estudo, cfg)
    g = next((x for x in ep._ler_grupos(p) if (x["tipo"] or "") == (tipo or "")), None) if p else None
    r = ep.registro_do_grupo(g, ep._ler_registro(cfg)) if g else None
    if not r or not ep.subir_de_novo(r, cfg):
        return False, "Este exame não está para subir de novo."
    if not ep.marcar_subido(cfg, r["final"]):
        return False, "Não achei o registro deste exame."
    return True, "Pronto: saiu dos pendentes."


def _reenvio(data, chegou):
    """Imagens chegaram mais de 1 dia depois da data do exame: reenvio de exame antigo
    (ou relogio do aparelho errado)."""
    try:
        return (datetime.strptime(chegou, "%Y%m%d") - datetime.strptime(data, "%Y%m%d")).days > 1
    except ValueError:
        return False


def nomes_do_exame(do_aparelho, r, ep):
    """(nome para mostrar, nome do aparelho se diferente ou ""). Finalizado mostra o nome do
    laudo (Medware); o do aparelho so aparece a parte quando e outro (Helder 10/11/2026: cadastro
    a mao abreviado). Laudo cortado pelo Medware e o aparelho com o nome inteiro: fica o do aparelho."""
    laudo = " ".join(str((r or {}).get("nome") or "").split()).upper()
    if not laudo:
        return do_aparelho, ""
    a, b = ep.normalizar(do_aparelho), ep.normalizar(laudo)
    if a == b or a.startswith(b + " "):
        return do_aparelho, ""
    return laudo, do_aparelho


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
            r = ep.registro_do_grupo(g, reg) if g else None
            fin = ((r or {}).get("corrigido_em") or (r or {}).get("em") or "")[:10].replace("-", "")
            paciente, como = nomes_do_exame(t["paciente"], r, ep)
            exames.append({
                "estudo": pasta.name, "tipo": tipo or "",
                "tipo_nome": (tipo or "Exame").replace("_", " "),
                "paciente": paciente, "nome_aparelho": como, "data": t["data"], "hora": t["hora"],
                "sigla": t["sigla"], "n": t["n"], "na_folha": t["n"] - fora,
                "medidas": med.categoria(tipo) != "" and info["srs"] > 0,
                "sem_medidas": med.categoria(tipo) == "",
                "chegou": info["chegou"], "chegou_hora": info["chegou_hora"], "finalizado": fin,
                "reenvio": _reenvio(t["data"], info["chegou"]),
                "situacao": _situacao(pasta, tipo, g, reg, ep, cfg),
                "subir_de_novo": _subir_de_novo(g, reg, ep, cfg),
            })
    exames.sort(key=lambda e: (e["data"], e["hora"]), reverse=True)
    return exames


RAIZ_WORKLIST = "1.2.826.0.1.3680043.8.498."   # UID que a worklist do EcoPDF cria (pydicom)
TROCA_TURNO = 13   # medicos trocam por volta das 13h (Helder 07/11): o aviso do worklist vale por turno


def turno_atual(agora=None):
    """(id do turno, inicio AAAAMMDDHHMM): manha ate 13h, tarde depois."""
    agora = agora or datetime.now()
    tarde = agora.hour >= TROCA_TURNO
    return (agora.strftime("%Y%m%d") + ("T" if tarde else "M"),
            agora.strftime("%Y%m%d") + (f"{TROCA_TURNO:02d}00" if tarde else "0000"))


def dica_worklist(exames, cfg, agora=None):
    """Turno (id) em que mostrar 'puxe pelo worklist', ou "": worklist ligada, nenhuma consulta
    neste turno e pelo menos um exame deste turno cadastrado a mao no aparelho."""
    if not cfg.get("worklist"):
        return ""
    turno, inicio = turno_atual(agora)
    try:
        import worklist
        ult = str(worklist.ler_situacao(cfg).get("ultima_consulta", ""))
    except Exception:
        ult = ""
    if ult.replace("-", "").replace("T", "").replace(":", "")[:12] >= inicio:
        return ""
    for e in exames:
        if (e["chegou"] + e["chegou_hora"]) >= inicio and not e["estudo"].startswith(RAIZ_WORKLIST):
            return turno
    return ""


_imp_cache = {"em": 0.0, "d": None}
_imp_trava = threading.Lock()


def impressora_situacao(cfg):
    """Aviso para o quadro Imprimir: {"grave": bool, "texto": str}; texto "" = tudo certo.
    Mesma consulta do painel (impressora padrao do Windows), guardada por 30 s."""
    with _imp_trava:
        if _imp_cache["d"] is not None and time.time() - _imp_cache["em"] < 30:
            return _imp_cache["d"]
    d = {"grave": False, "texto": ""}
    import impressora_reparo
    rep = impressora_reparo.reparar(logging.getLogger("ecopdf"), cfg)    # cabo em outra porta USB: corrige antes de avisar
    if rep.get("texto") and (rep.get("grave") or rep.get("botao")):
        d = {"grave": rep["grave"], "texto": rep["texto"], "botao": rep["botao"]}
        with _imp_trava:
            _imp_cache.update(em=time.time(), d=d)
        return d
    ok, nome = impressao.disponivel(cfg)
    if ok:
        import painel
        i = painel._impressora_padrao()
        sit, fila, minutos = i.get("situacao", ""), i.get("fila", 0), i.get("fila_min", 0)
        if sit == "offline":
            d = {"grave": True, "texto": f"A impressora {nome} está offline: confira se está ligada e com o "
                                         "cabo USB conectado ao computador."}
        elif i.get("grave") and sit != "pronta" and sit:
            d = {"grave": True, "texto": f"A impressora {nome} está com problema ({sit}): resolva na impressora "
                                         "antes de imprimir."}
        elif fila and minutos > painel.FILA_PARADA_MIN:
            d = {"grave": True, "texto": f"Há {fila} impressão(ões) parada(s) na fila da {nome} há {minutos} min: "
                                         "confira se a impressora está ligada e com o cabo USB, ou cancele a fila."}
        elif sit in ("pouco papel", "pouca tinta/toner"):
            d = {"grave": False, "texto": f"A impressora {nome} avisa: {sit}."}
        elif impressao.existe_impressora(impressao.FILA_FOTO) and not impressao.fila_foto_valida(nome):
            d = {"grave": False, "botao": True,
                 "texto": "A fila EcoPDF Foto é de outra impressora: as fotos saem pela padrão, com o papel "
                          "escolhido pelo EcoPDF. Para corrigir, clique em Reconfigurar impressora."}
        elif rep.get("corrigido"):
            d = {"grave": False, "texto": rep["texto"]}
    elif "EcoPDF Foto" in nome:
        d = {"grave": True, "botao": True, "texto": "A EcoPDF Foto está como impressora padrão: clique em "
                                                    "Reconfigurar impressora (ela volta para a impressora da sala)."}
    elif nome == "sem impressora padrao":
        d = {"grave": True, "botao": True, "texto": "Este computador está sem impressora padrão no Windows. Confira "
                                                    "se a impressora está ligada e com o cabo USB e clique em "
                                                    "Reconfigurar impressora."}
    with _imp_trava:
        _imp_cache.update(em=time.time(), d=d)
    return d


LAUDO_ESPERA_MIN = 3  # laudo recem-impresso costuma juntar em segundos: so mostra depois disso


def _outra_data(data, nome, tipo, fotos, ep, cfg):
    """Imagens do mesmo paciente e tipo em OUTRA data na pasta 2 (relogio do aparelho errado).
    Devolve a data mais proxima (AAAAMMDD) ou ''."""
    achadas = sorted((d for _a, d, n, t in fotos
                      if d != data and t == tipo and ep._mesmo_paciente(nome, n, cfg)),
                     key=lambda d: abs(int(d) - int(data)) if d.isdigit() and data.isdigit() else 0)
    return achadas[0] if achadas else ""


def laudos_sem_imagens(ep, cfg):
    """Laudos parados na pasta 1 (sem par na pasta 2), com o motivo. So leitura."""
    pasta = cfg["pasta_laudos"]
    if not pasta.exists():
        return []
    agora = time.time()
    fotos = ep._listar(cfg["pasta_imagens"], cfg, agora + 60)
    laudos = ep._listar(pasta, cfg, agora + 60)
    out = []
    for arq in sorted(pasta.glob("*.pdf")):
        try:
            idade = agora - arq.stat().st_mtime
        except OSError:
            continue
        if idade < LAUDO_ESPERA_MIN * 60 or ep.SEM_IMG.match(arq.stem) or ep.PEDIDO.match(arq.stem):
            continue
        info = ep.ler_nome(arq, cfg["tipos"])
        if info:
            data, nome, tipo = info
            motivo = None
            for l in laudos:
                if l[0] == arq:
                    foto, motivo = ep._casar(l, laudos, fotos, cfg)
                    if foto is not None:
                        motivo = ("as imagens foram achadas, mas um dos PDFs esta aberto em outro "
                                  "programa: feche para juntar")
            if motivo and motivo.startswith("sem PDF de imagens"):
                outra = _outra_data(data, nome, tipo, fotos, ep, cfg)
                if _tem_descartado(data, nome, tipo, ep, cfg):
                    motivo = "ha imagens DESCARTADAS deste paciente nesta data: abra o exame e clique em Restaurar"
                elif outra:
                    motivo = (f"ha imagens deste paciente com data {ep.data_br(outra)}: confira a data e a hora "
                              "do aparelho (pode ter desacertado depois de falta de energia)")
                else:
                    motivo = "nenhuma imagem deste paciente nesta data (nome diferente no aparelho ou exame sem fotos)"
        else:
            data, nome, tipo, motivo = "", arq.stem, "", "nome fora do padrao AAAA_MM_DD_Nome_Tipo"
        out.append({"laudo": arq.name, "paciente": nome.upper(), "data": data,
                    "tipo_nome": (tipo or "Exame").replace("_", " "),
                    "minutos": int(idade // 60), "motivo": motivo or "aguardando a juncao",
                    "liberavel": bool(info)})
    return out


def _tem_descartado(data, nome, tipo, ep, cfg):
    raiz = cfg["pasta_dicom"]
    for pasta in raiz.iterdir() if raiz.exists() else []:
        d = ep._ler_descartados(pasta) if pasta.is_dir() else {}
        for g in ep._ler_grupos(pasta) if d else []:
            if (g["tipo"] or "") in d:
                info = ep.ler_nome(Path(g["pdf"]), cfg["tipos"])
                if info and info[0] == data and info[2] == tipo and ep._mesmo_paciente(info[1], nome, cfg):
                    return True
    return False


def liberar_sem_imagens(nome, ep, cfg):
    """Botao 'Liberar sem imagens': renomeia o laudo da pasta 1 com ' sem imagens'
    (o mesmo pedido de sempre; o vigia libera em ate 10 s). (ok, mensagem)."""
    arq = cfg["pasta_laudos"] / Path(nome).name
    if Path(nome).name != nome or arq.suffix.lower() != ".pdf" or not arq.is_file():
        return False, "Laudo nao encontrado na pasta 1 (talvez ja tenha juntado)."
    info = ep.ler_nome(arq, cfg["tipos"])
    if not info:
        return False, "Nome do laudo fora do padrao: renomeie na pasta 1."
    data, paciente, tipo = info
    if any(f[1] == data and f[3] == tipo and ep._mesmo_paciente(f[2], paciente, cfg)
           for f in ep._listar(cfg["pasta_imagens"], cfg, time.time() + 60)):
        return False, "Ha imagens deste exame na pasta 2: elas vao juntar, nao libere sem imagens."
    with ep.TRAVA:
        try:
            arq.rename(arq.with_name(arq.stem + " sem imagens.pdf"))
        except OSError:
            return False, "Laudo aberto em outro programa: feche e tente de novo."
    ep.log.info("Tela: laudo liberado sem imagens: %s", arq.name)
    return True, "Laudo liberado sem imagens: vai para a pasta 3 em alguns segundos."


def _pasta(estudo, cfg):
    """Pasta do estudo, sem permitir sair de dicom/."""
    if not estudo or "/" in estudo or "\\" in estudo or estudo.startswith("."):
        return None
    p = cfg["pasta_dicom"] / estudo
    return p if p.is_dir() else None


def _ocr_estudo(pasta, itens, ep):
    """[(chave, valor, unidade)] lidos das caixas de medida das imagens do grupo.
    Guarda o resultado por arquivo em dicom/<estudo>/ocr.json (ler pixels e lento)."""
    arq_cache = pasta / "ocr.json"
    try:
        cache = json.loads(arq_cache.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    mudou, saida = False, []
    for _, ident, _ in itens:
        arq = pasta / (ident.split("#")[0] + ".dcm")
        try:
            st = arq.stat()
        except OSError:
            continue
        # versao da leitura na marca: leitor novo rele as imagens ja guardadas no cache
        marca = f"{st.st_size}-{int(st.st_mtime)}-{ocr_imagem.VERSAO}"
        c = cache.get(arq.name)
        if not c or c.get("marca") != marca:
            try:
                ds = pydicom.dcmread(arq)
                lidas = [list(m[:3]) for m in ocr_imagem.medidas_da_imagem(
                    ds.pixel_array, ds.get("ManufacturerModelName", ""))] \
                    if int(ds.get("NumberOfFrames", 1) or 1) == 1 else []
            except Exception:
                ep.log.exception("Tela: OCR falhou em %s", arq.name)
                lidas = []
            c = {"marca": marca, "medidas": lidas}
            cache[arq.name] = c
            mudou = True
        saida.extend(tuple(m) for m in c["medidas"])
    if mudou:
        try:
            arq_cache.write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass
    return saida


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
    paciente, como = nomes_do_exame((ep.nome_paciente(ref.get("PatientName")) or "SEM NOME").upper(), r, ep)
    return {
        "estudo": pasta.name, "tipo": t, "tipo_nome": (tipo or "Exame").replace("_", " "),
        "paciente": paciente, "nome_aparelho": como,
        "data": ep.data_br(ref.get("StudyDate", "")),
        "hora": str(ref.get("StudyTime", "") or "")[:4],
        "sigla": ep.sigla(ref), "aparelho": ep.aparelho(ref, cfg),
        "situacao": situacao, "final": r["final"] if r else "",
        "subir_de_novo": _subir_de_novo(g, reg, ep, cfg),
        "impressao": _paginas_impressao(r, ep, cfg) if situacao == "Finalizado" else None,
        "por_pagina": max(1, cfg["colunas"]) * max(1, cfg["linhas"]),
        "imagens": imagens,
        "medidas": med.medidas_do_exame(
            tipo, ref, srs, _ocr_estudo(pasta, itens, ep) if med.categoria(tipo) in ("eco", "carotidas") else None),
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


def arquivo_final(pasta, tipo, ep, cfg):
    """PDF da pasta 3 do exame finalizado (o mesmo que sobe no prontuario), ou None."""
    t = tipo or ""
    g = next((x for x in ep._ler_grupos(pasta) if (x["tipo"] or "") == t), None)
    r = ep.registro_do_grupo(g, ep._ler_registro(cfg)) if g else None
    arq = cfg["pasta_final"] / r["final"] if r and r.get("final") else None
    return arq if arq and arq.is_file() else None


def _partes_impressao(r, ep, cfg):
    """(laudo, folha de fotos) guardados do exame finalizado: os mesmos que formam o final."""
    laudo = ep._laudo_guardado(r, cfg)
    foto = Path(r["foto"]) if ep._tem_foto(r) else None
    return laudo, foto


def _paginas_impressao(r, ep, cfg):
    if not r:
        return None
    try:
        laudo, foto = _partes_impressao(r, ep, cfg)
        if laudo is None:
            return None
        return {"laudo": len(PdfReader(str(laudo)).pages),
                "folhas": len(PdfReader(str(foto)).pages) if foto else 0}
    except Exception:
        ep.log.exception("Tela: nao li as paginas para impressao")
        return None


def paginas_impressao(pasta, tipo, folhas, com_laudo, ep, cfg):
    """([(pdf, indice)] das folhas de fotos escolhidas, [(pdf, indice)] do laudo) para a
    impressao direta (dois envios). Listas vazias se nao houver o que imprimir."""
    g = next((x for x in ep._ler_grupos(pasta) if (x["tipo"] or "") == (tipo or "")), None)
    r = ep.registro_do_grupo(g, ep._ler_registro(cfg)) if g else None
    if not r:
        return [], []
    laudo, foto = _partes_impressao(r, ep, cfg)
    fotos, pl = [], []
    if foto and folhas:
        n = len(PdfReader(str(foto)).pages)
        fotos = [(foto, f - 1) for f in sorted(set(folhas)) if 1 <= f <= n]
    if com_laudo and laudo:
        pl = [(laudo, i) for i in range(len(PdfReader(str(laudo)).pages))]
    return fotos, pl


MAX_LOTE = 10


def itens_impressao(corpo, cfg):
    """Exames de um pedido de impressao: {itens: [{e, t, folhas, laudo}, ...]} (varios exames,
    Helder 03/11) ou o pedido antigo de um exame so {e, t, folhas, laudo}. Ordem preservada."""
    itens = corpo.get("itens") if isinstance(corpo.get("itens"), list) else [corpo]
    out = []
    for it in itens[:MAX_LOTE]:
        if not isinstance(it, dict):
            continue
        pasta = _pasta(str(it.get("e", "")), cfg)
        if pasta is None:
            continue
        folhas = [int(x) for x in it.get("folhas", []) if str(x).isdigit()]
        out.append((pasta, str(it.get("t", "")), folhas, bool(it.get("laudo"))))
    return out


def paginas_lote(itens, ep, cfg):
    """Todas as folhas de fotos dos exames, na ordem, e depois todos os laudos na mesma ordem:
    um envio de fotos (modo foto, lento, uma vez so) e um envio de laudos."""
    fotos, laudos = [], []
    for pasta, tipo, folhas, com_laudo in itens:
        f, l = paginas_impressao(pasta, tipo, folhas, com_laudo, ep, cfg)
        fotos += f
        laudos += l
    return fotos, laudos


def pdf_lote(itens, ep, cfg):
    """PDF unico para a janela do navegador: todas as fotos, depois todos os laudos."""
    fotos, laudos = paginas_lote(itens, ep, cfg)
    if not fotos and not laudos:
        return None
    w = PdfWriter()
    for pdf, i in fotos + laudos:
        w.append(str(pdf), pages=[i])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


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
            try:
                self.send_response(codigo)
                self.send_header("Content-Type", tipo)
                self.send_header("Content-Length", str(len(corpo)))
                self.send_header("Cache-Control", "max-age=86400" if tipo.startswith("image/") else "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(corpo)
            except ConnectionError:
                # navegador fechou a conexao antes da resposta terminar (recarregar, fechar a aba,
                # trocar de exame): WinError 10053/10054 ou BrokenPipe. Normal, nao e erro do EcoPDF.
                self.close_connection = True
                ep.log.debug("Tela: navegador desconectou em %s", self.path)

        def _ok_host(self):
            # so atende pelo proprio PC (protege contra paginas de fora usando o endereco local)
            if self.headers.get("Host", "") not in hosts:
                self._enviar(403, {"erro": "acesso so pelo proprio PC"})
                return False
            return True

        def do_GET(self):
            if not self._ok_host():
                return
            _estado["contato"] = cfg["_tela_contato"] = time.time()
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path in ("/", "/index.html"):
                    return self._enviar(200, (BASE / "tela.html").read_bytes(), "text/html; charset=utf-8")
                if u.path == "/favicon.ico":
                    return self._enviar(200, (BASE / "ecopdf.ico").read_bytes(), "image/x-icon")
                if u.path == "/simbolo.png":
                    return self._enviar(200, (BASE / "simbolo.png").read_bytes(), "image/png")
                if u.path == "/manual":
                    html = (BASE / "manual.html").read_text("utf-8")
                    html = re.sub(r'(<b id="versao">)[^<]*', lambda m: m.group(1) + ep.versao(), html, count=1)
                    return self._enviar(200, html.encode("utf-8"), "text/html; charset=utf-8")
                if u.path == "/referencia":
                    # tabela de consulta (valores de referencia do eco); vem no zip, abre no leitor de PDF
                    return self._enviar(200, (BASE / "valores_referencia.pdf").read_bytes(), "application/pdf",
                                        {"Content-Disposition": "inline; filename=valores_referencia.pdf"})
                if u.path == "/api/exames":
                    exames = listar_exames(ep, cfg)
                    return self._enviar(200, {"exames": exames, "versao": ep.versao(),
                                              "dica_worklist": dica_worklist(exames, cfg),
                                              "laudos_sem_imagens": laudos_sem_imagens(ep, cfg),
                                              "dias_guardados": cfg["manter_dicom_dias"],
                                              "agora": datetime.now().strftime("%H:%M:%S")})
                if u.path == "/api/worklist_medicos":
                    import worklist
                    return self._enviar(200, worklist.medicos_de_hoje(cfg))
                if u.path == "/api/worklist":
                    import worklist
                    return self._enviar(200, worklist.lista_da_tela(cfg))
                if u.path == "/api/atualizacao":
                    return self._enviar(200, consultar_atualizacao())
                if u.path == "/api/impressora":
                    return self._enviar(200, impressora_situacao(cfg))
                if u.path == "/api/imprimir_estado":
                    st = impressao.estado(int(q.get("id", "0") or 0))
                    return self._enviar(200, st or {"estado": "erro", "mensagem": "impressão não encontrada"})
                if u.path == "/api/sinal":
                    return self._enviar(200, sinal(q.get("e", ""), ep, cfg))
                if u.path == "/api/exame":
                    pasta = _pasta(q.get("e", ""), cfg)
                    # assinatura ANTES de ler: imagem que chegar durante a leitura (OCR demora)
                    # muda a assinatura e a pagina recarrega (05/11: abria com 4 de 8 e parava)
                    ass = "-".join(str(x) for x in _assinatura(pasta)) if pasta else ""
                    d = detalhe(pasta, q.get("t", ""), ep, cfg) if pasta else None
                    if d:
                        d["ass"] = ass
                    return self._enviar(200 if d else 404, d or {"erro": "exame nao encontrado"})
                if u.path == "/final":
                    pasta = _pasta(q.get("e", ""), cfg)
                    arq = arquivo_final(pasta, q.get("t", ""), ep, cfg) if pasta else None
                    if arq is None:
                        return self._enviar(404, "<p style='font:16px sans-serif'>Arquivo final nao encontrado na "
                                            "pasta 3 (pode ter sido apagado apos o prazo de guarda).</p>",
                                            "text/html; charset=utf-8")
                    return self._enviar(200, arq.read_bytes(), "application/pdf",
                                        {"Content-Disposition": "inline; filename*=UTF-8''" + quote(arq.name)})
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
                if u.path == "/api/abrir_pasta":
                    ok, msg = abrir_pasta_final(str(corpo.get("e", "")), str(corpo.get("t", "")), ep, cfg)
                    return self._enviar(200, {"ok": ok, "mensagem" if ok else "erro": msg})
                if u.path == "/api/ja_subi":
                    ok, msg = ja_subi(str(corpo.get("e", "")), str(corpo.get("t", "")), ep, cfg)
                    return self._enviar(200, {"ok": ok, "mensagem" if ok else "erro": msg})
                if u.path == "/api/atualizar":
                    ok, msg = iniciar_atualizacao()
                    return self._enviar(200, {"ok": ok, "mensagem" if ok else "erro": msg})
                if u.path == "/api/worklist_medico":
                    import worklist
                    cfg["_tela_contato"] = time.time()
                    return self._enviar(200, {"ok": True, "atual": worklist.escolher_medico(cfg, str(corpo.get("medico", "")))})
                if u.path == "/api/worklist_excluir":
                    import worklist
                    cod = str(corpo.get("codpac", ""))
                    if not cod.isdigit():
                        return self._enviar(400, {"erro": "paciente invalido"})
                    worklist.excluir(cfg, cod, bool(corpo.get("voltar")))
                    return self._enviar(200, {"ok": True})
                if u.path == "/api/reparar_impressora":
                    import impressora_reparo
                    r = impressora_reparo.reconfigurar(ep.log, cfg)
                    with _imp_trava:
                        _imp_cache.update(em=0.0, d=None)
                    return self._enviar(200, {"grave": r.get("grave", False), "texto": r.get("texto", ""),
                                              "botao": r.get("botao", False), "estado": r.get("estado", "")})
                if u.path in ("/api/imprimir_direto", "/api/imprimir"):
                    itens = itens_impressao(corpo, cfg)
                    if u.path == "/api/imprimir":
                        pdf = pdf_lote(itens, ep, cfg)
                        if pdf is None:
                            return self._enviar(404, {"erro": "nada para imprimir"})
                        _anotar_impressao(cfg)
                        return self._enviar(200, pdf, "application/pdf")
                    fotos, laudo = paginas_lote(itens, ep, cfg)
                    if not fotos and not laudo:
                        return self._enviar(404, {"erro": "nada para imprimir"})
                    import impressora_reparo
                    impressora_reparo.reparar(ep.log, cfg)   # porta USB trocada: corrige antes (guardado 20 s)
                    tid = impressao.iniciar(fotos, laudo, cfg, ep.log, rapido=bool(corpo.get("rapido")),
                                            qualidade=str(corpo.get("qualidade") or "normal"))
                    if tid is None:
                        return self._enviar(200, {"navegador": True})
                    _anotar_impressao(cfg)
                    return self._enviar(200, {"id": tid})
                if u.path == "/api/liberar":
                    ok, msg = liberar_sem_imagens(str(corpo.get("laudo", "")), ep, cfg)
                    return self._enviar(200, {"ok": ok, "mensagem" if ok else "erro": msg})
                pasta = _pasta(str(corpo.get("e", "")), cfg)
                if pasta is None:
                    return self._enviar(404, {"erro": "exame nao encontrado"})
                tipo = str(corpo.get("t", ""))
                ordem = [str(x) for x in corpo.get("ordem", [])]
                fora = [str(x) for x in corpo.get("fora", [])]
                if u.path == "/api/salvar":
                    ok, msg = ep.salvar_folha(pasta, tipo, ordem, fora, cfg)
                    return self._enviar(200 if ok else 409, {"ok": ok, "mensagem": msg})
                if u.path in ("/api/descartar", "/api/restaurar"):
                    f = ep.descartar if u.path.endswith("descartar") else ep.restaurar
                    ok, msg = f(pasta, tipo, cfg)
                    return self._enviar(200, {"ok": ok, "mensagem" if ok else "erro": msg})
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


_estado = {"contato": 0.0, "aberta_em": 0.0, "url": "", "ultimo": None}
ABERTA_SEG = 120   # pagina aberta consulta a cada 15 s (aba minimizada: ~1 por minuto)
ULTIMO_SEG = 600   # exame que chegou ha menos de 10 min: a tela abre direto nele


def recebida(estudo, tipo):
    """Imagem gravada (chamado pelo on_store). Guarda o exame da ultima imagem: a tela abre
    nele e a pagina aberta mostra as imagens novas / avisa que chegou outro exame.
    So na memoria: reiniciar o EcoPDF esquece (a tela volta a abrir na lista)."""
    if tipo is None:   # SR ou arquivo sem imagem: nao muda o exame
        return
    _estado["ultimo"] = {"estudo": estudo, "tipo": tipo or "", "em": time.time()}


def _url_ultimo():
    """Endereco da tela ja no exame da ultima imagem (se recente); senao, a lista."""
    u = _estado["ultimo"]
    if u and time.time() - u["em"] < ULTIMO_SEG:
        from urllib.parse import quote
        return f'{_estado["url"]}#e={quote(u["estudo"], safe="")}&t={quote(u["tipo"], safe="")}'
    return _estado["url"]


def sinal(estudo, ep, cfg):
    """Consulta leve da pagina (a cada 4 s com um exame aberto): ultimo exame que chegou e
    se a pasta do exame aberto mudou (imagem nova, medidas, folha). Le so datas de arquivo."""
    agora = time.time()
    r = {"ultimo": None, "ass": "", "ultima_ha": None}
    u = _estado["ultimo"]
    if u:
        r["ultimo"] = {"estudo": u["estudo"], "tipo": u["tipo"], "ha": int(agora - u["em"]),
                       "tipo_nome": (u["tipo"] or "Exame").replace("_", " ")}
        p = _pasta(u["estudo"], cfg)
        try:
            t = _info_estudo(p, ep, cfg)["tipos"].get(u["tipo"]) if p else None
        except Exception:
            t = None
        if t:
            r["ultimo"].update(paciente=t["paciente"], hora=t["hora"])
    p = _pasta(estudo, cfg) if estudo else None
    if p:
        ass = _assinatura(p)
        r["ass"] = "-".join(str(x) for x in ass)
        r["ultima_ha"] = int(agora - ass[-1]) if ass[-1] else None
        # aparelho ja encerrou o envio depois da ultima imagem: nao esta mais "recebendo"
        try:
            mi = (p / ep.ULTIMA_IMG).stat().st_mtime
            r["envio_ok"] = ep.FIM_ENVIO.get(p.name, 0) >= mi
        except (OSError, AttributeError):
            r["envio_ok"] = False
    return r


def _janelas_da_tela():
    """Janelas abertas da tela no Windows (titulo "EcoPDF – Exames", com ou sem o numero
    de pendencias na frente). Nao depende da pagina responder: o Edge congela pagina de
    janela minimizada (modo eficiencia) e a tela abria duplicada (visto no Bessa em 03/11).
    Fora do Windows: None."""
    import sys
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        achadas = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cada(hwnd, _):
            n = u32.GetWindowTextLengthW(hwnd)
            if n and u32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                t = buf.value
                if "EcoPDF" in t and "Exames" in t:
                    achadas.append(hwnd)
            return True
        u32.EnumWindows(cada, 0)
        return achadas
    except Exception:
        return None


def _trazer_para_frente(hwnd):
    """Restaura a janela minimizada e tenta pô-la na frente (o Windows pode só piscar na barra)."""
    try:
        import ctypes
        u32 = ctypes.windll.user32
        if u32.IsIconic(hwnd):
            u32.ShowWindow(hwnd, 9)   # SW_RESTORE
        u32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _edge():
    """msedge.exe (vem em todo Windows 10/11), para abrir a tela numa janela propria."""
    import os
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if p.exists():
                return p
    return None


def abrir_janela(url):
    """Abre a tela como programa: janela propria do Edge, sem barra de endereco nem abas.
    Sem Edge (ou fora do Windows), abre no navegador padrao."""
    edge = _edge()
    if edge is not None:
        try:
            import subprocess
            subprocess.Popen([str(edge), f"--app={url}"], close_fds=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return
        except OSError:
            pass
    webbrowser.open(url)


def consultar_atualizacao():
    """Botao escondido (clique no numero da versao): diz se ha versao nova no canal deste PC."""
    import atualizador as at
    local, canal = at.versao_local(), at.ler_canal()
    try:
        validos = at.releases()
    except Exception:
        return {"mensagem": f"Versão instalada: {local}. Sem acesso ao GitHub agora; tente mais tarde."}
    alvo = at.melhor(validos, canal)
    if alvo and (not at.VERSAO_RE.match(local) or at.chave(alvo["tag"]) > at.chave(local)):
        return {"mensagem": f"Versão instalada: {local} (canal {canal}). Há versão nova: {alvo['tag']}.",
                "disponivel": alvo["tag"]}
    if alvo and canal == "geral" and at.VERSAO_RE.match(local) and at.chave(alvo["tag"]) < at.chave(local):
        return {"mensagem": f"Versão instalada: {local} (canal {canal}). É uma versão de teste; a de "
                            f"produção é {alvo['tag']} (volta com atualizar_agora.bat geral)."}
    return {"mensagem": f"Versão instalada: {local} (canal {canal}). Já é a mais nova; nada a fazer."}


def iniciar_atualizacao():
    """Abre o atualizar_agora.bat (o Windows pede permissao de administrador)."""
    import os
    bat = BASE / "atualizar_agora.bat"
    if os.name != "nt" or not bat.exists():
        return False, "Atualização só pelo PC da clínica (atualizar_agora.bat em C:\\EcoPDF)."
    try:
        os.startfile(str(bat))
    except OSError as e:
        return False, f"Não consegui abrir o atualizar_agora.bat ({e})."
    return True, ("Aceite a permissão de administrador. A janela preta mostra o andamento; "
                  "esta tela some por 1 a 3 minutos e volta sozinha.")


def abrir_se_fechada(cfg):
    """TELA_ABRIR = ao_receber: abre o navegador quando chega imagem ou laudo, so se a
    tela nao estiver aberta (sem consulta recente) e nao tiver sido aberta agora ha pouco."""
    agora = time.time()
    if (cfg.get("abrir_tela") != "ao_receber" or not _estado["url"]
            or agora - _estado["contato"] < ABERTA_SEG or agora - _estado["aberta_em"] < ABERTA_SEG):
        return
    _estado["aberta_em"] = agora
    janelas = _janelas_da_tela()
    if janelas:
        _trazer_para_frente(janelas[0])
        return
    abrir_janela(_url_ultimo())


def abrir_pasta_final(estudo, tipo, ep, cfg):
    """Abre a pasta 3 no Explorador do Windows; com um exame, ja deixa o arquivo final
    selecionado (para arrastar para o prontuario). So no proprio PC (a tela so atende 127.0.0.1)."""
    import os
    import subprocess
    pasta3 = cfg["pasta_final"]
    arq = None
    if estudo:
        p = _pasta(estudo, cfg)
        arq = arquivo_final(p, tipo, ep, cfg) if p else None
    if os.name != "nt":
        return False, "so funciona no Windows"
    try:
        if arq:
            subprocess.Popen(["explorer", "/select,", str(arq)])
        else:
            pasta3.mkdir(parents=True, exist_ok=True)
            os.startfile(str(pasta3))
    except OSError:
        ep.log.exception("Tela: nao consegui abrir a pasta 3")
        return False, "nao consegui abrir a pasta"
    return True, "Pasta aberta (veja na barra de tarefas, se nao aparecer na frente)."


def _anotar_impressao(cfg):
    """Data e hora de cada uso do botao Imprimir (para o painel). Nada do paciente."""
    arq = cfg["pasta_log"] / "impressoes.json"
    try:
        usos = json.loads(arq.read_text("utf-8"))
    except (OSError, ValueError):
        usos = []
    usos = (usos + [datetime.now().isoformat(timespec="seconds")])[-300:]
    try:
        arq.write_text(json.dumps(usos), "utf-8")
    except OSError:
        pass


def iniciar(cfg, ep):
    """Sobe a tela em 127.0.0.1:TELA_PORTA. Navegador: ao iniciar (TELA_ABRIR = sim) ou
    quando chega imagem/laudo e a tela nao esta aberta (ao_receber, padrao)."""
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
    _estado["url"] = url
    if cfg.get("abrir_tela") == "inicio":
        try:
            abrir_janela(url)
        except Exception:
            ep.log.warning("Nao consegui abrir o navegador; use o atalho EcoPDF na Area de Trabalho")
    return srv
