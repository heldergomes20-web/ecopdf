"""
Levantamento do Medware e das medidas dos aparelhos (Helder, 20/11/2026).

Roda UMA vez em cada PC, fora do horario de atendimento, e manda o resultado para a planilha
do painel (aba "Levantamento"). Serve para planejar o historico do paciente e o preenchimento
automatico do Laudo Express. So ESTRUTURA e CONTAGENS:

  Medware (so no PC onde o banco esta, 127.0.0.1; os outros PCs da unidade pulam esta parte):
    - tabelas, colunas e quantidade de linhas; tabelas de cadastro do sistema (procedimentos,
      modelos de laudo, unidades de medida)
    - laudos por ano, por exame e por modelo
    - nomes das variaveis dos laudos (VARIAVELLAUDO) com quantidade, unidade, tipo e % numerico
    - opcoes de modelo: valor de texto que se repete em pelo menos 50 laudos (nunca numero,
      data ou valor que aparece pouco)
    - estrutura do texto final: linhas que se repetem em pelo menos 15% dos 200 laudos mais
      recentes de cada modelo, com os numeros trocados por #
    - preenchimento de CPF e nascimento e cadastros repetidos (so contagens)
  Aparelhos (todo PC):
    - catalogo das medidas que cada modelo manda no SR: codigo, nome, unidade, modificadores e
      grupo, com quantas vezes e em quantos exames, lido dos DICOM guardados (ate 180 dias)

Nunca sai do PC: nome, CPF, nascimento, valor de medida ou texto livre de laudo.
Leitura do Medware so de leitura (transacao somente leitura). Copia do que foi enviado:
log/levantamento_enviado.json. Rodar agora, a qualquer hora: levantamento.bat.
"""

import json
import os
import re
import socket
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
VERSAO_LEV = 1                 # subir para rodar de novo em todos os PCs
ESTADO = "levantamento.json"   # em log/
COPIA = "levantamento_enviado.json"
MIN_OPCAO = 50                 # valor de texto so sai se aparecer em pelo menos isso de laudos
AMOSTRA = 200                  # laudos mais recentes de cada modelo para a estrutura do texto
MIN_LINHA_FRACAO, MIN_LINHA_N = 0.15, 20
SEM_IMAGEM_MIN = 30            # so roda sem imagem chegando ha pelo menos isso
TIMEOUT_S = 1800               # consulta pesada no banco (8 mi de variaveis) pode levar minutos
MAX_LINHAS_GRUPO = 4000
TENTATIVAS = 3                 # noites com erro antes de desistir (rodar a mao continua valendo)
# tabelas de cadastro do sistema que vao inteiras (sem paciente); so se tiverem ate 300 linhas
REFERENCIA = ("PROCEDIMENTO", "SCRIPTLAUDO", "SCRIPTLAUDO_PROCEDIMENTO", "UNIDADEMEDIDALAUDO",
              "GRUPOUNIDADEMEDIDALAUDO", "TIPOMEDIDALAUDO", "STATUSLAUDO", "TIPODADOS", "TIPODADO")
COLUNAS_FORA = re.compile(r"^(CODUSUARIO\w*|DTHRULTMODIFICACAO|UCASE_\w+|OBS|OBSERVACAO|RECOMENDACOES|"
                          r"MSGENTREGAPROCED|LISTAFRASES|LISTAGRUPOS|VALOR\w*)$")
TIPOS = {7: "SMALLINT", 8: "INTEGER", 10: "FLOAT", 12: "DATE", 13: "TIME", 14: "CHAR",
         16: "BIGINT", 27: "DOUBLE", 35: "TIMESTAMP", 37: "VARCHAR", 261: "BLOB"}
# numero com virgula ou ponto, com ou sem sinal ("-" solto e invalido no SIMILAR TO do Firebird)
NUMERO_SQL = ("(trim(v.VALOR) similar to '[0-9]+([.,][0-9]+)?' "
              "or (trim(v.VALOR) starting with '-' "
              "and substring(trim(v.VALOR) from 2) similar to '[0-9]+([.,][0-9]+)?'))")
RE_DATA = re.compile(r"\d{1,2}\s*[/.-]\s*\d{1,2}(\s*[/.-]\s*\d{2,4})?|\d{1,2}:\d{2}")
RE_NUM_UN = re.compile(r"^\s*[-+]?\d+([.,]\d+)?\s*[^\s\d]{0,8}\s*$")
RE_ROTULO_PESSOAL = re.compile(r"^\s*(paciente|nome|cpf|rg|data de nasc\w*|nascimento|nasc\.?|idade|"
                               r"conv[eê]nio|plano|matr[ií]cula|telefone|endere[cç]o|n[ºo°]\.? ?(do )?exame)"
                               r"\s*[:.-]", re.I)
_RODANDO = threading.Lock()


# ---------------------------------------------------------------- utilitarios

def _limpo(s, n=120):
    s = re.sub(r"\s+", " ", str("" if s is None else s)).strip()
    return s[:n]


def _ler_estado(cfg):
    try:
        return json.loads((cfg["pasta_log"] / ESTADO).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _gravar_estado(cfg, est):
    try:
        (cfg["pasta_log"] / ESTADO).write_text(json.dumps(est, ensure_ascii=False, indent=1), "utf-8")
    except OSError:
        pass


def cpf_valido(cpf):
    d = re.sub(r"\D", "", str(cpf or ""))
    if len(d) != 11 or d == d[0] * 11:
        return False
    for n in (9, 10):
        s = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        if (s * 10 % 11) % 10 != int(d[n]):
            return False
    return True


def rtf_para_texto(rtf):
    """Texto puro de um RTF (paragrafos viram linhas). Ignora tabelas de fonte/cor, imagens etc."""
    destinos = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "header", "footer", "headerl",
                "headerr", "footerl", "footerr", "xmlnstbl", "listtable", "listoverridetable",
                "themedata", "colorschememapping", "latentstyles", "datastore", "rsidtbl", "generator",
                "filetbl", "revtbl", "object", "fldinst", "bkmkstart", "bkmkend"}
    padrao = re.compile(r"\\([a-z]{1,32})(-?\d{1,10})?[ ]?|\\'([0-9a-f]{2})|\\([^a-z])|([{}])|[\r\n]+|(.)", re.I)
    pilha, ignorar, uc, saida = [], False, 1, []
    pular = 0
    for m in padrao.finditer(rtf):
        palavra, arg, hexa, simbolo, chave, ch = m.groups()
        if chave == "{":
            pilha.append((ignorar, uc))
        elif chave == "}":
            if pilha:
                ignorar, uc = pilha.pop()
        elif simbolo:
            if simbolo == "*":
                ignorar = True
            elif not ignorar and simbolo in "\\{}":
                saida.append(simbolo)
            elif not ignorar and simbolo == "~":
                saida.append(" ")
        elif palavra:
            p = palavra.lower()
            if p in destinos:
                ignorar = True
            elif ignorar:
                pass
            elif p in ("par", "line", "row", "sect", "page"):
                saida.append("\n")
            elif p in ("tab", "cell"):
                saida.append("\t")
            elif p == "uc":
                uc = int(arg or 1)
            elif p == "u" and arg:
                n = int(arg)
                saida.append(chr(n + 65536 if n < 0 else n))
                pular = uc
        elif hexa:
            if pular:
                pular -= 1
            elif not ignorar:
                saida.append(bytes([int(hexa, 16)]).decode("cp1252", "replace"))
        elif ch:
            if pular:
                pular -= 1
            elif not ignorar:
                saida.append(ch)
    return "".join(saida)


def texto_do_blob(valor):
    """(formato, texto) de um campo de texto do Medware (RTF, HTML ou texto)."""
    if valor is None:
        return "vazio", ""
    if isinstance(valor, (bytes, bytearray)):
        valor = bytes(valor).decode("cp1252", "replace")
    v = valor.lstrip()
    if not v:
        return "vazio", ""
    if v.startswith("{\\rtf"):
        return "RTF", rtf_para_texto(v)
    if v.startswith("<"):
        return "HTML/XML", re.sub(r"<[^>]+>", "\n", v)
    return "texto", v


def linha_modelo(linha):
    """Linha do laudo com numeros trocados por # e dado pessoal cortado; '' se nao serve."""
    s = _limpo(linha.replace("\t", " "), 400)
    if len(s) < 3:
        return ""
    m = RE_ROTULO_PESSOAL.match(s)
    if m:
        s = m.group(0).strip() + " …"
    s = re.sub(r"\d+", "#", s)
    return s[:120]


# ---------------------------------------------------------------- Medware

def _local(cfg):
    """(host, banco) do Medware se o banco esta NESTE PC; senao (None, onde esta)."""
    import worklist
    alvos = list(worklist._alvos(cfg))
    for _, host, banco in alvos:
        if host.lower() in ("127.0.0.1", "localhost"):
            return host, banco
    return None, ", ".join(a[1] for a in alvos) or "?"


def _conectar(cfg, host, banco):
    import firebirdsql
    import worklist
    return firebirdsql.connect(host=host, port=cfg.get("medware_porta") or worklist.PORTA_FIREBIRD,
                               database=banco, user="SYSDBA", password=cfg["medware_senha"],
                               charset="WIN1252", timeout=TIMEOUT_S, readonly=True)


def medware(cfg, log=None):
    """[(grupo, item, detalhe, n, extra)] do banco do Medware deste PC."""
    host, banco = _local(cfg)
    if not host:
        return [("Situação", "Medware", f"banco em outro PC ({banco}): o levantamento é feito pelo PC do banco", 0, "")]
    linhas, t0 = [], time.time()
    con = _conectar(cfg, host, banco)
    cur = con.cursor()

    def parte(nome, func):
        t = time.time()
        try:
            func()
            linhas.append(("Tempo", nome, "ok", round(time.time() - t), "segundos"))
        except Exception as e:
            try:
                con.rollback()
            except Exception:
                pass
            if log:
                log.warning("Levantamento: %s falhou (%s)", nome, type(e).__name__)
            linhas.append(("Erro", nome, f"{type(e).__name__}: {_limpo(e, 200)}", round(time.time() - t), "segundos"))

    tabelas, contagem = {}, {}

    def esquema():
        cur.execute("select rdb$get_context('SYSTEM','ENGINE_VERSION') from rdb$database")
        linhas.append(("Banco", "Firebird", str(cur.fetchone()[0]), 0, banco))
        cur.execute("""select trim(r.rdb$relation_name), trim(f.rdb$field_name), fl.rdb$field_type,
                              fl.rdb$field_length
                       from rdb$relations r
                       join rdb$relation_fields f on f.rdb$relation_name = r.rdb$relation_name
                       join rdb$fields fl on fl.rdb$field_name = f.rdb$field_source
                       where coalesce(r.rdb$system_flag, 0) = 0 and r.rdb$view_blr is null
                       order by r.rdb$relation_name, f.rdb$field_position""")
        for t, c, tipo, tam in cur.fetchall():
            tabelas.setdefault(t, []).append((c, TIPOS.get(tipo, str(tipo)), tam))
        for t, cols in tabelas.items():
            try:
                cur.execute(f'select count(*) from "{t}"')
                contagem[t] = cur.fetchone()[0]
            except Exception:
                con.rollback()
                contagem[t] = -1
            txt = ", ".join(f"{c} {tp}" + (f"({tm})" if tp == "VARCHAR" else "") for c, tp, tm in cols)
            linhas.append(("Tabela", t, txt[:2000], contagem[t], f"{len(cols)} colunas"))

    def referencia():
        for t in REFERENCIA:
            if t not in tabelas or not (0 < contagem.get(t, 0) <= 300):
                continue
            cols = [c for c, tp, _ in tabelas[t] if tp != "BLOB" and not COLUNAS_FORA.match(c)]
            if not cols:
                continue
            cur.execute("select " + ", ".join(f'"{c}"' for c in cols) + f' from "{t}"')
            for r in cur.fetchall():
                det = "; ".join(f"{c}={_limpo(v, 80)}" for c, v in zip(cols[1:], r[1:]) if v not in (None, ""))
                linhas.append((f"Cadastro {t}", _limpo(r[0], 40), det[:800], 0, ""))

    titulos = {}

    def laudos():
        cur.execute("select CODSCRIPTLAUDO, TITULO from SCRIPTLAUDO")
        titulos.update({c: _limpo(t, 80) for c, t in cur.fetchall()})
        cur.execute("""select y, count(*), sum(tf), sum(bl) from (
                         select extract(year from l.DATAEMISSAO) y,
                                case when l.TEXTOFINAL is null then 0 else 1 end tf,
                                case when coalesce(l.BLOQUEADO, 0) = 0 then 0 else 1 end bl
                         from LAUDO l) t group by y order by y""")
        for y, n, tf, bl in cur.fetchall():
            linhas.append(("Laudos por ano", str(y or "sem data"), f"com texto final: {tf} · bloqueados: {bl}", n, ""))
        cur.execute("""select p, y, count(*) from (
                         select coalesce(pr.DESCRICAO, '(sem exame)') p, extract(year from l.DATAEMISSAO) y
                         from LAUDO l
                         left join AGENDAMENTO a on a.CODAGENDAMENTO = l.CODAGENDAMENTO
                         left join PROCEDIMENTO pr on pr.CODPROCEDIMENTO = a.CODPROCEDIMENTO) t
                       group by p, y order by p, y""")
        for p, y, n in cur.fetchall():
            linhas.append(("Laudos por exame e ano", _limpo(p, 80), str(y or "sem data"), n, ""))
        cur.execute("""select CODSCRIPTLAUDO, count(*), min(DATAEMISSAO), max(DATAEMISSAO)
                       from LAUDO group by CODSCRIPTLAUDO""")
        for c, n, a, b in cur.fetchall():
            anos = f"{a.year if a else '?'}–{b.year if b else '?'}"
            linhas.append(("Laudos por modelo", titulos.get(c, f"modelo {c}"), f"código {c} · {anos}", n, ""))
        cur.execute("""select y, count(*), count(distinct c) from (
                         select extract(year from l.DATAEMISSAO) y, v.CODLAUDO c
                         from VARIAVELLAUDO v join LAUDO l on l.CODLAUDO = v.CODLAUDO) t
                       group by y order by y""")
        for y, n, nl in cur.fetchall():
            linhas.append(("Variáveis por ano", str(y or "sem data"), f"laudos com variáveis: {nl}", n,
                           f"{round(n / nl) if nl else 0} por laudo"))

    def variaveis():
        cur.execute(f"""select l.CODSCRIPTLAUDO, v.TITULO, v.DESCRICAO, v.CODUNIDADEMEDIDA, v.CODTIPODADOS,
                               count(*),
                               sum(case when {NUMERO_SQL} then 1 else 0 end),
                               sum(case when v.VALOR is null or trim(v.VALOR) = '' then 1 else 0 end),
                               min(extract(year from l.DATAEMISSAO)), max(extract(year from l.DATAEMISSAO))
                        from VARIAVELLAUDO v join LAUDO l on l.CODLAUDO = v.CODLAUDO
                        group by l.CODSCRIPTLAUDO, v.TITULO, v.DESCRICAO, v.CODUNIDADEMEDIDA, v.CODTIPODADOS""")
        rs = sorted(cur.fetchall(), key=lambda r: -r[5])[:MAX_LINHAS_GRUPO]
        for sc, tit, desc, un, tp, n, num, vaz, a, b in rs:
            preench = n - vaz
            pct = f"{round(100 * num / preench)}% numérico" if preench else "sempre vazio"
            linhas.append(("Variável", _limpo(desc, 80),
                           f"modelo: {titulos.get(sc, sc)} · grupo: {_limpo(tit, 60) or '—'}",
                           n, f"unidade {un if un is not None else '—'} · tipo {tp if tp is not None else '—'} · "
                              f"{pct} · vazio {vaz} · {a}–{b}"))

    def opcoes():
        cur.execute(f"""select d, val, count(*) from (
                          select v.DESCRICAO d, trim(v.VALOR) val from VARIAVELLAUDO v
                          where v.VALOR is not null and trim(v.VALOR) <> '' and not {NUMERO_SQL}) t
                        group by d, val having count(*) >= {MIN_OPCAO}""")
        n = 0
        for d, val, q in sorted(cur.fetchall(), key=lambda r: (str(r[0]), -r[2])):
            val = _limpo(val, 80)
            if RE_DATA.search(val) or RE_NUM_UN.match(val) or len(re.findall(r"\d", val)) >= 5:
                continue
            linhas.append(("Opção", _limpo(d, 80), val, q, f"aparece em ≥ {MIN_OPCAO} laudos"))
            n += 1
            if n >= MAX_LINHAS_GRUPO:
                break

    def texto():
        cur.execute("select CODSCRIPTLAUDO, count(*) from LAUDO where TEXTOFINAL is not null group by CODSCRIPTLAUDO")
        modelos = [c for c, q in cur.fetchall() if q]
        for c in modelos:
            if c is None:
                cur.execute(f"select first {AMOSTRA} TEXTOFINAL from LAUDO where CODSCRIPTLAUDO is null "
                            "and TEXTOFINAL is not null order by DATAEMISSAO desc")
            else:
                cur.execute(f"select first {AMOSTRA} TEXTOFINAL from LAUDO where CODSCRIPTLAUDO = ? "
                            "and TEXTOFINAL is not null order by DATAEMISSAO desc", (c,))
            formatos, tamanhos, vistas, posicoes, amostra = Counter(), [], Counter(), defaultdict(list), 0
            for (blob,) in cur.fetchall():
                if hasattr(blob, "read"):
                    blob = blob.read()
                fmt, txt = texto_do_blob(blob)
                formatos[fmt] += 1
                if not txt.strip():
                    continue
                amostra += 1
                tamanhos.append(len(txt))
                ls = [x for x in (linha_modelo(l) for l in txt.splitlines()) if x]
                vistos = set()
                for i, l in enumerate(ls):
                    if l in vistos:
                        continue
                    vistos.add(l)
                    vistas[l] += 1
                    posicoes[l].append(i / max(1, len(ls) - 1))
            nome = titulos.get(c, f"modelo {c}")
            fm = " · ".join(f"{k} {v}" for k, v in formatos.most_common())
            med = round(sum(tamanhos) / len(tamanhos)) if tamanhos else 0
            linhas.append(("Formato do texto final", nome, f"{fm} · média {med} caracteres", amostra,
                           f"{AMOSTRA} mais recentes"))
            corte = max(MIN_LINHA_N, MIN_LINHA_FRACAO * amostra)
            fixas = [(l, q) for l, q in vistas.items() if q >= corte]
            fixas.sort(key=lambda x: sum(posicoes[x[0]]) / len(posicoes[x[0]]))
            for l, q in fixas[:600]:
                pos = round(100 * sum(posicoes[l]) / len(posicoes[l]))
                linhas.append((f"Texto · {nome}", l, f"posição {pos}%", q, f"de {amostra} laudos"))

    def pacientes():
        cur.execute("select count(*) from PACIENTE")
        total = cur.fetchone()[0]
        cur.execute("select CPF from PACIENTE")
        cpfs = [r[0] for r in cur.fetchall()]
        preench = sum(1 for c in cpfs if re.sub(r"\D", "", str(c or "")))
        validos = [re.sub(r"\D", "", c) for c in cpfs if cpf_valido(c)]
        rep = sum(1 for _, q in Counter(validos).items() if q > 1)
        cur.execute("select count(*) from PACIENTE where DATANASC is null")
        sem_nasc = cur.fetchone()[0]
        cur.execute("""select count(*) from (select UCASE_NOME, DATANASC from PACIENTE
                       where DATANASC is not null group by UCASE_NOME, DATANASC having count(*) > 1) t""")
        nome_nasc = cur.fetchone()[0]
        desde = datetime.now() - timedelta(days=365)
        cur.execute("""select p.CPF from PACIENTE p where exists (
                         select 1 from AGENDAMENTO a join LAUDO l on l.CODAGENDAMENTO = a.CODAGENDAMENTO
                         where a.CODPACIENTE = p.CODPACIENTE and l.DATAEMISSAO >= ?)""", (desde,))
        rec = [r[0] for r in cur.fetchall()]
        del cpfs
        pc = lambda a, b: f"{round(100 * a / b)}%" if b else "—"
        linhas.extend([
            ("Pacientes", "cadastros", "", total, ""),
            ("Pacientes", "com CPF preenchido", pc(preench, total), preench, ""),
            ("Pacientes", "com CPF válido (dígitos conferem)", pc(len(validos), total), len(validos), ""),
            ("Pacientes", "CPF repetido em mais de um cadastro", "grupos", rep, ""),
            ("Pacientes", "sem data de nascimento", pc(sem_nasc, total), sem_nasc, ""),
            ("Pacientes", "mesmo nome e nascimento em mais de um cadastro", "grupos", nome_nasc, ""),
            ("Pacientes", "com laudo nos últimos 12 meses", pc(len(rec), total), len(rec), ""),
            ("Pacientes", "desses, com CPF válido", pc(sum(1 for c in rec if cpf_valido(c)), len(rec)),
             sum(1 for c in rec if cpf_valido(c)), ""),
        ])

    try:
        parte("tabelas", esquema)
        parte("cadastros do sistema", referencia)
        parte("laudos", laudos)
        parte("variáveis", variaveis)
        parte("opções", opcoes)
        parte("texto final", texto)
        parte("pacientes", pacientes)
    finally:
        try:
            con.close()
        except Exception:
            pass
    linhas.append(("Tempo", "total", "", round(time.time() - t0), "segundos"))
    return linhas


# ---------------------------------------------------------------- aparelhos (SR)

def _codigo(seq):
    if not seq:
        return ""
    c = seq[0]
    sig = _limpo(c.get("CodeMeaning", ""), 80)
    cod = _limpo(c.get("CodeValue", ""), 30)
    esq = _limpo(c.get("CodingSchemeDesignator", ""), 20)
    return f"{sig} [{esq}:{cod}]" if cod else sig


def _itens(ds):
    """[(medida, unidade, modificadores, grupo)] dos itens numericos do SR, sem valores."""
    saida = []

    def mod(x):
        n, v = _codigo(x.get("ConceptNameCodeSequence")), _codigo(x.get("ConceptCodeSequence"))
        return f"{n}: {v}" if v else ""

    def andar(seq, ctx):
        seq = seq or []
        mods = [m for m in (mod(x) for x in seq if x.get("ValueType") == "CODE") if m]
        ctx2 = ctx + mods
        for it in seq:
            vt = it.get("ValueType")
            if vt == "NUM":
                mv = it.get("MeasuredValueSequence")
                un = ""
                if mv:
                    u = mv[0].get("MeasurementUnitsCodeSequence")
                    un = _limpo(u[0].get("CodeValue", ""), 20) if u else ""
                proprios = [m for m in (mod(x) for x in (it.get("ContentSequence") or [])
                                        if x.get("ValueType") == "CODE") if m]
                saida.append((_codigo(it.get("ConceptNameCodeSequence")), un,
                               " | ".join(proprios), " › ".join(ctx2)))
            elif vt == "CONTAINER":
                andar(it.get("ContentSequence"), ctx2 + [_codigo(it.get("ConceptNameCodeSequence"))])

    andar(ds.get("ContentSequence"), [])
    return saida


def aparelhos(cfg, ep, log=None):
    """[(grupo, item, detalhe, n, extra)]: catalogo das medidas do SR por modelo de aparelho."""
    import pydicom
    from pydicom.multival import MultiValue
    t0 = time.time()
    vezes, exames = Counter(), defaultdict(set)
    estudos, com_sr, arquivos, srs, falhas = defaultdict(set), defaultdict(set), 0, 0, 0
    pasta = cfg["pasta_dicom"]
    try:
        pastas = [p for p in pasta.iterdir() if p.is_dir()]
    except OSError:
        pastas = []
    for p in pastas:
        modelo = None
        for arq in sorted(p.glob("*.dcm")):
            arquivos += 1
            try:
                ds = pydicom.dcmread(arq, stop_before_pixels=True,
                                     specific_tags=["SOPClassUID", "Modality", "Manufacturer",
                                                    "ManufacturerModelName", "SoftwareVersions",
                                                    "DeviceSerialNumber", "StationName"])
            except Exception:
                falhas += 1
                continue
            m = " ".join(x for x in (_limpo(ds.get("Manufacturer", ""), 30),
                                     _limpo(ds.get("ManufacturerModelName", ""), 40)) if x) or "?"
            sv = ds.get("SoftwareVersions", "")
            sv = _limpo(sv[0] if isinstance(sv, (list, MultiValue)) and sv else sv, 30)
            sig = ep.sigla(ds) if ep else ""
            modelo = f"{m} · {sig or '?'}" + (f" · software {sv}" if sv else "")
            estudos[modelo].add(p.name)
            sop = str(ds.get("SOPClassUID", "") or "")
            if not (str(ds.get("Modality", "")) == "SR" or sop.startswith("1.2.840.10008.5.1.4.1.1.88.")):
                continue
            try:
                ds = pydicom.dcmread(arq)
            except Exception:
                falhas += 1
                continue
            srs += 1
            com_sr[modelo].add(p.name)
            titulo = _codigo(ds.get("ConceptNameCodeSequence"))
            for nome, un, mods, grupo in _itens(ds):
                k = (modelo, titulo, nome, un, mods, grupo)
                vezes[k] += 1
                exames[k].add(p.name)
    linhas = [("Aparelho", mo, f"exames com SR: {len(com_sr[mo])} de {len(estudos[mo])}", len(estudos[mo]), "")
              for mo in sorted(estudos)]
    por_modelo = Counter()
    for k in sorted(vezes, key=lambda k: (k[0], k[1], k[5], k[2])):
        modelo, titulo, nome, un, mods, grupo = k
        por_modelo[modelo] += 1
        if por_modelo[modelo] > MAX_LINHAS_GRUPO:
            continue
        det = " · ".join(x for x in (mods, grupo) if x)
        linhas.append((f"SR · {modelo}", nome, det[:400], vezes[k],
                       f"unidade {un or '—'} · exames {len(exames[k])} · {titulo}"))
    linhas.append(("Tempo", "SR", f"{arquivos} arquivos lidos · {srs} SR · {falhas} sem leitura",
                   round(time.time() - t0), "segundos"))
    return linhas


# ---------------------------------------------------------------- envio

def enviar(cfg, ep, parte, linhas):
    """POST para a planilha (pedido "levantamento"). True se a planilha gravou."""
    import urllib.request
    # "computador" e nao "pc": a planilha antiga (sem o pedido "levantamento") recusa sem gravar nada na aba Salas
    corpo = {"chave": cfg.get("painel_chave", ""), "pedido": "levantamento",
             "computador": (os.environ.get("COMPUTERNAME") or socket.gethostname() or "?")[:40],
             "versao": ep.versao() if ep else "", "levantamento": VERSAO_LEV, "parte": parte,
             "linhas": [[_limpo(g, 80), _limpo(i, 200), _limpo(d, 2000), n if isinstance(n, (int, float)) else 0,
                         _limpo(x, 300)] for g, i, d, n, x in linhas]}
    try:
        copia = cfg["pasta_log"] / COPIA
        anterior = json.loads(copia.read_text("utf-8")) if copia.exists() else {}
    except (OSError, ValueError):
        anterior = {}
    anterior[parte] = {"em": datetime.now().isoformat(timespec="seconds"), "linhas": corpo["linhas"]}
    try:
        (cfg["pasta_log"] / COPIA).write_text(json.dumps(anterior, ensure_ascii=False, indent=0), "utf-8")
    except OSError:
        pass
    if not cfg.get("painel_url"):
        return False
    req = urllib.request.Request(cfg["painel_url"], data=json.dumps(corpo).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = r.read(2000).decode("utf-8", "replace")
        resp = resp.replace(" ", "")
        return '"ok":true' in resp and '"linhas":' in resp  # so a planilha com o pedido "levantamento" devolve "linhas"
    except Exception:
        return False


# ---------------------------------------------------------------- quando rodar

def fora_do_horario(agora=None):
    """Noite (19h–6h), sabado a partir das 13h e domingo: o Medware quase parado."""
    agora = agora or datetime.now()
    d, h = agora.weekday(), agora.hour
    if d == 6:
        return True
    if d == 5:
        return h >= 13 or h < 6
    return h >= 19 or h < 6


def _sem_imagem_recente(cfg, ep):
    try:
        import painel
        seg = painel._ultima_imagem(cfg, ep)[0]
        return seg is None or seg >= SEM_IMAGEM_MIN * 60
    except Exception:
        return True


def rodar(cfg, ep, log, manual=False):
    """Roda as partes que faltam (ou todas, se manual). Devolve {parte: 'ok'|'erro: ...'}."""
    if not _RODANDO.acquire(blocking=False):
        return {"situacao": "já está rodando"}
    try:
        est = _ler_estado(cfg)
        if est.get("versao") != VERSAO_LEV:
            est = {"versao": VERSAO_LEV}
        hoje = datetime.now().strftime("%Y-%m-%d")
        res = {}
        if "medware_servidores" not in cfg or "medware_senha" not in cfg:
            try:
                import worklist
                worklist.configurar(cfg, ep._painel_ini)
            except Exception:
                log.warning("Levantamento: configuracao do Medware falhou")
        for parte, func in (("medware", lambda: medware(cfg, log)), ("aparelhos", lambda: aparelhos(cfg, ep, log))):
            e = est.get(parte, {})
            if not manual and (e.get("ok") or e.get("tentou") == hoje or e.get("tentativas", 0) >= TENTATIVAS):
                continue
            e["tentou"] = hoje
            log.info("Levantamento: %s começou", parte)
            try:
                linhas = func()
            except Exception as ex:
                linhas = [("Erro", parte, f"{type(ex).__name__}: {_limpo(ex, 200)}", 0, "")]
                log.warning("Levantamento: %s falhou (%s)", parte, type(ex).__name__)
            erro = any(l[0] == "Erro" for l in linhas)
            enviado = enviar(cfg, ep, parte, linhas)
            e["ok"] = enviado and not erro
            e["em"] = datetime.now().isoformat(timespec="seconds")
            e["linhas"] = len(linhas)
            e["enviado"] = enviado
            if not e["ok"]:
                e["tentativas"] = e.get("tentativas", 0) + 1
            est[parte] = e
            _gravar_estado(cfg, est)
            res[parte] = ("ok" if e["ok"] else "com erro") + f" · {len(linhas)} linhas · " + \
                         ("enviado à planilha" if enviado else "NÃO enviado")
            log.info("Levantamento: %s %s", parte, res[parte])
        return res
    finally:
        _RODANDO.release()


def _laco(cfg, ep, parar):
    parar.wait(600)  # nunca logo depois de ligar
    while not parar.is_set():
        try:
            est = _ler_estado(cfg)
            feito = est.get("versao") == VERSAO_LEV and all(
                est.get(p, {}).get("ok") or est.get(p, {}).get("tentativas", 0) >= TENTATIVAS
                for p in ("medware", "aparelhos"))
            if feito:
                return
            if fora_do_horario() and _sem_imagem_recente(cfg, ep):
                rodar(cfg, ep, ep.log)
        except Exception:
            ep.log.exception("Levantamento: erro inesperado (o resto do EcoPDF continua)")
        parar.wait(600)


def iniciar(cfg, ep, parar):
    if not cfg.get("painel_url"):
        return
    threading.Thread(target=_laco, args=(cfg, ep, parar), daemon=True, name="levantamento").start()


if __name__ == "__main__":  # levantamento.bat: roda agora e mostra o resumo
    sys.path.insert(0, str(BASE))
    import ecopdf as ep
    cfg = ep.carregar_config(BASE / "config.ini")
    ep.configurar_log(cfg["pasta_log"])
    try:
        import worklist
        worklist.configurar(cfg, ep._painel_ini)
    except Exception as e:
        print(f"Medware: configuracao da worklist falhou ({type(e).__name__})")
    print("Levantamento do Medware e das medidas dos aparelhos (so estrutura e contagens).")
    print("Pode levar de alguns minutos a meia hora. Nao feche esta janela.\n")
    for parte, r in rodar(cfg, ep, ep.log, manual=True).items():
        print(f"  {parte}: {r}")
    print(f"\nCopia do que foi enviado: {cfg['pasta_log'] / COPIA}")
