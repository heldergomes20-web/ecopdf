"""
Lista de pacientes (worklist DICOM / MWL) do EcoPDF, lida da agenda do Medware.

O aparelho consulta (C-FIND) o mesmo AE/porta das imagens. A cada consulta o EcoPDF LE o
banco do Medware (Firebird; nunca grava) e devolve os pacientes do dia, um por paciente.
Sem polling: encaixe aparece na proxima consulta.

Onde fica o banco: MEDWARE_SERVIDORES no painel.ini, se houver. Sem ele, o EcoPDF le o
C:\\Medware LaudosFlex\\Bd.xml deste PC (o mesmo arquivo que o Medware usa: NomeServidor,
CaminhoBanco, Porta) e tenta esse servidor primeiro; depois o MEDWARE_SERVIDORES do config.ini
(127.0.0.1 = banco neste PC). O primeiro que responder e usado. Cada item pode ser
"servidor" ou "servidor:C:\\caminho\\Clinicas.fdb". Senha: MEDWARE_SENHA no painel.ini
(fora do zip publico); sem ela, a padrao do Firebird.
Ultima consulta e ultimo erro vao para log/worklist.json (tela/painel). Nada de paciente
vai para o painel: so hora, IP do aparelho e quantidade.
"""

import datetime
import json
import os
import xml.etree.ElementTree as ET
import logging
import socket
import sys
import threading
import time
import unicodedata

from pydicom.dataset import Dataset
from pydicom.uid import generate_uid

log = logging.getLogger("ecopdf")

BANCO_PADRAO = r"C:\Medware LaudosFlex\BD\Clinicas.fdb"
BDXML_MEDWARE = r"C:\Medware LaudosFlex\Bd.xml"
SENHA_PADRAO_FIREBIRD = "masterkey"
PORTA_FIREBIRD = 3050
MAX_DIAS = 7
_TRAVA = threading.Lock()
_SERVIDOR_OK = {"item": None, "em": 0.0}   # ultimo servidor que respondeu (cache 10 min)


# ---------------------------------------------------------------- configuracao

def ler_bdxml(caminho=BDXML_MEDWARE):
    """Servidor do Medware lido do Bd.xml deste PC: ("servidor:caminho", porta) ou (None, None)."""
    try:
        raiz = ET.parse(caminho).getroot()
    except (OSError, ET.ParseError):
        return None, None
    nome = (raiz.findtext("NomeServidor") or "").strip()
    banco = (raiz.findtext("CaminhoBanco") or "").strip()
    try:
        porta = int((raiz.findtext("Porta") or "").strip() or PORTA_FIREBIRD)
    except ValueError:
        porta = PORTA_FIREBIRD
    if not nome:
        return None, None
    if nome.lower() in ("localhost", "127.0.0.1", socket.gethostname().lower(),
                        os.environ.get("COMPUTERNAME", "").lower()):
        nome = "127.0.0.1"
    return (f"{nome}:{banco}" if banco else nome), porta


def configurar(cfg, painel_ini):
    """Completa cfg com as chaves da worklist. painel_ini(chave) -> '' se nao houver."""
    servidores = painel_ini("MEDWARE_SERVIDORES")
    cfg["medware_origem"] = "painel.ini"
    if not servidores:
        item, porta = ler_bdxml(BDXML_MEDWARE)
        extra = cfg.get("medware_servidores_txt", "") or "127.0.0.1"
        servidores = f"{item}, {extra}" if item else extra
        cfg["medware_origem"] = "Bd.xml do Medware" if item else "config.ini"
        if porta:
            cfg["medware_porta"] = porta
    lista = []
    for s in servidores.split(","):
        s = s.strip()
        if s and s.lower() not in [x.lower() for x in lista]:
            lista.append(s)
    cfg["medware_servidores"] = lista
    cfg["medware_senha"] = painel_ini("MEDWARE_SENHA") or SENHA_PADRAO_FIREBIRD
    cfg.setdefault("medware_banco", BANCO_PADRAO)
    return cfg


def _alvos(cfg):
    for item in cfg.get("medware_servidores") or ["127.0.0.1"]:
        host, banco = item, cfg.get("medware_banco") or BANCO_PADRAO
        # "servidor:C:\..." (o ":" da letra do disco nao conta)
        if ":" in item and not (len(item) > 1 and item[1] == ":"):
            h, _, b = item.partition(":")
            if b:
                host, banco = h.strip(), b.strip()
        yield item, host, banco


def _eh_ip(host):
    try:
        socket.inet_aton(host)
        return host.count(".") == 3
    except OSError:
        return False


def _testar(host, porta=PORTA_FIREBIRD, segundos=2.0):
    """(ip, None) se a porta responde; senao (ip ou None, motivo legivel)."""
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        return None, "nome nao encontrado na rede"
    try:
        with socket.create_connection((ip, porta), timeout=segundos):
            return ip, None
    except OSError:
        return ip, f"sem resposta na porta {porta}"


def _ips_conhecidos(cfg):
    """log/medware_ip.json: {nome do servidor: ultimo IP que respondeu}."""
    try:
        return json.loads((cfg["pasta_log"] / "medware_ip.json").read_text("utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _guardar_ip(cfg, host, ip):
    if _eh_ip(host) or not ip or ip.startswith("127."):
        return
    ips = _ips_conhecidos(cfg)
    if ips.get(host.lower()) == ip:
        return
    ips[host.lower()] = ip
    try:
        (cfg["pasta_log"] / "medware_ip.json").write_text(json.dumps(ips), "utf-8")
    except (OSError, KeyError, TypeError):
        pass


def _conectar(cfg):
    """(conexao, item). Tenta primeiro o ultimo que funcionou.
    Servidor por nome: se o nome nao achar ou nao responder, tenta o ultimo IP em que ele
    respondeu (log/medware_ip.json), para nao depender da resolucao de nomes da rede."""
    import firebirdsql
    alvos = list(_alvos(cfg))
    ok = _SERVIDOR_OK["item"]
    if ok and time.time() - _SERVIDOR_OK["em"] < 600:
        alvos.sort(key=lambda a: a[0] != ok)
    erros = []
    porta = cfg.get("medware_porta") or PORTA_FIREBIRD
    conhecidos = _ips_conhecidos(cfg)
    for item, host, banco in alvos:
        ip, motivo = _testar(host, porta)
        rotulo = host if (not ip or ip == host) else f"{host} ({ip})"
        if motivo:
            guardado = None if _eh_ip(host) else conhecidos.get(host.lower())
            if guardado and guardado != ip:
                ip2, motivo2 = _testar(guardado, porta)
                if not motivo2:
                    log.warning("Medware: %s %s; usando o IP guardado %s", host, motivo, guardado)
                    ip, motivo = ip2, None
                    rotulo = f"{host} ({guardado}, IP guardado)"
                else:
                    motivo += f"; IP guardado {guardado}: {motivo2}"
        if motivo:
            erros.append(f"{rotulo}: {motivo}")
            continue
        try:
            con = firebirdsql.connect(host=ip, port=porta, database=banco, user="SYSDBA",
                                      password=cfg["medware_senha"], charset="WIN1252", timeout=10)
            _SERVIDOR_OK.update(item=item, em=time.time())
            _guardar_ip(cfg, host, ip)
            return con, item
        except Exception as e:
            erros.append(f"{rotulo}: {type(e).__name__}: {str(e)[:120]}")
    raise ConnectionError("; ".join(erros) or "nenhum servidor do Medware configurado")


# ---------------------------------------------------------------- leitura

def ascii_maiusculo(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return " ".join(s.upper().split())


def peso_kg(bruto):
    """Medware: PESO em kg (BIGINT com 4 casas, chega como 84.0000)."""
    try:
        v = float(bruto or 0)
    except (TypeError, ValueError):
        return None
    return round(v, 1) if 1 <= v <= 300 else None


def altura_m(bruto):
    """Medware: ALTURA em cm (172.0000). DICOM quer metros."""
    try:
        v = float(bruto or 0)
    except (TypeError, ValueError):
        return None
    if 30 <= v <= 230:
        return round(v / 100, 2)
    if 0.3 <= v <= 2.3:
        return round(v, 2)
    return None


def buscar(cfg, ini, fim):
    """[{paciente}], um por paciente e dia, e o servidor usado. So leitura."""
    con, item = _conectar(cfg)
    try:
        cur = con.cursor()
        cur.execute("""
            select a.datahoraagendada, a.datahorachegada, p.codpaciente, p.nome, p.datanasc,
                   p.sexo, p.peso, p.altura, m.nomeagenda, m.nome, pr.descricaoagenda, pr.descricao
            from agendamento a
            join paciente p on p.codpaciente = a.codpaciente
            left join medico m on m.codmedico = a.codmedico
            left join procedimento pr on pr.codprocedimento = a.codprocedimento
            where a.datahoraagendada >= ? and a.datahoraagendada < ?
              and a.datahoracancelado is null and a.datahorafalta is null
              and a.datahorasuspensao is null
            order by a.datahoraagendada""", (ini, fim + datetime.timedelta(days=1)))
        linhas = cur.fetchall()
    finally:
        try:
            con.close()
        except Exception:
            pass
    pacientes = {}
    for ag, cheg, codpac, nome, nasc, sexo, peso, alt, med_ag, med, proc_ag, proc in linhas:
        chave = (codpac, ag.date())
        procedimento = ascii_maiusculo(proc_ag or proc)
        if chave in pacientes:
            if procedimento and procedimento not in pacientes[chave]["procs"]:
                pacientes[chave]["procs"].append(procedimento)
            continue
        s = (sexo or "").strip().upper()[:1]
        pacientes[chave] = {
            "codpac": codpac, "dia": ag.date(), "hora": ag, "nome": ascii_maiusculo(nome),
            "nasc": nasc.strftime("%Y%m%d") if nasc else "", "sexo": s if s in ("M", "F") else "",
            "peso": peso_kg(peso), "altura": altura_m(alt), "medico": ascii_maiusculo(med_ag or med),
            "procs": [procedimento] if procedimento else [],
        }
    return list(pacientes.values()), item


# ---------------------------------------------------------------- resposta DICOM

def _intervalo(valor):
    hoje = datetime.date.today()
    s = str(valor or "").strip()

    def d(t):
        return datetime.datetime.strptime(t, "%Y%m%d").date()
    try:
        if not s:
            return hoje, hoje
        if "-" not in s:
            return d(s), d(s)
        a, b = s.split("-", 1)
        ini, fim = (d(a) if a else hoje), (d(b) if b else hoje)
    except ValueError:
        return hoje, hoje
    if fim < ini:
        ini, fim = fim, ini
    if (fim - ini).days > MAX_DIAS:
        ini = fim - datetime.timedelta(days=MAX_DIAS)
    return ini, fim


def _filtro(valor):
    return ascii_maiusculo(str(valor or "").replace("*", " ").replace("?", " ").replace("^", " "))


# procedimento do Medware -> sigla curta no aparelho (Helder 03/11/2026: "ECO", "DCV",
# eco + carotidas = "ECO + DCV"). Procedimento sem sigla conhecida vai como esta.
SIGLAS = (("FETAL", "ECO FETAL"), ("CAROT", "DCV"), ("DUPLEX", "DCV"), ("VASCULAR", "DCV"),
          ("ECO", "ECO"))
ORDEM_SIGLAS = ("ECO", "ECO FETAL", "DCV")


def sigla(procedimento):
    p = ascii_maiusculo(procedimento)
    return next((s for chave, s in SIGLAS if chave in p), p)


def descricao(procs):
    """['ECODOPPLERCARDIOGRAMA...', 'DUPLEX SCAN...'] -> 'ECO + DCV'."""
    vistas = []
    for pr in procs:
        s = sigla(pr)
        if s and s not in vistas:
            vistas.append(s)
    vistas.sort(key=lambda s: ORDEM_SIGLAS.index(s) if s in ORDEM_SIGLAS else len(ORDEM_SIGLAS))
    return " + ".join(vistas)


def montar(p, pedido):
    """Dataset de resposta. Nome inteiro como 'sobrenome' (sem ^): chega ao PDF igual ao Medware."""
    ds = Dataset()
    ds.SpecificCharacterSet = "ISO_IR 100"
    ds.PatientName = p["nome"]
    ds.PatientID = str(p["codpac"])
    ds.PatientBirthDate = p["nasc"]
    ds.PatientSex = p["sexo"]
    if p["peso"]:
        ds.PatientWeight = str(p["peso"])
    if p["altura"]:
        ds.PatientSize = str(p["altura"])
    ds.AccessionNumber = ""
    ds.ReferringPhysicianName = ""
    # mesmo paciente no mesmo dia = mesmo estudo (eco + carotidas viram 2 folhas do mesmo estudo)
    ds.StudyInstanceUID = uid_do_estudo(p)
    ds.RequestedProcedureID = f"{p['codpac']}{p['dia']:%m%d}"[-16:]
    desc = descricao(p["procs"])[:64]
    ds.RequestedProcedureDescription = desc
    sps = Dataset()
    sps.Modality = "US"
    sps.ScheduledStationAETitle = ""
    sps.ScheduledProcedureStepStartDate = p["dia"].strftime("%Y%m%d")
    sps.ScheduledProcedureStepStartTime = p["hora"].strftime("%H%M%S")
    sps.ScheduledPerformingPhysicianName = p["medico"][:64]
    sps.ScheduledProcedureStepDescription = desc
    sps.ScheduledProcedureStepID = ds.RequestedProcedureID
    ds.ScheduledProcedureStepSequence = [sps]
    for el in pedido:   # chave pedida e sem valor vai vazia (alguns aparelhos exigem)
        if el.keyword and el.keyword not in ds and el.keyword != "QueryRetrieveLevel":
            try:
                setattr(ds, el.keyword, None if el.VR != "SQ" else [])
            except Exception:
                pass
    return ds


# ---------------------------------------------------------------- quem ja foi atendido

def uid_do_estudo(p):
    """Mesmo paciente no mesmo dia = mesmo estudo (eco + carotidas viram 2 folhas)."""
    return generate_uid(entropy_srcs=["ecopdf-mwl", str(p["codpac"]), p["dia"].isoformat()])


def _sigla_do_tipo(tipo):
    t = str(tipo or "").lower()
    if "fetal" in t:
        return "ECO FETAL"
    if "carot" in t or "vascular" in t or "duplex" in t:
        return "DCV"
    if "eco" in t:
        return "ECO"
    return ""   # tipo desconhecido (sem sonda/frequencia): vale para qualquer exame


def _recebidos(cfg, dia):
    """{(chave, sigla)} das imagens que ja chegaram no dia. chave = UID do estudo ou nome."""
    feitos = set()
    inicio = datetime.datetime.combine(dia, datetime.time()).timestamp()
    raiz = cfg.get("pasta_dicom")
    if not raiz or not raiz.is_dir():
        return feitos
    for pasta in raiz.iterdir():
        try:
            if not pasta.is_dir() or pasta.stat().st_mtime < inicio:
                continue
            grupos = json.loads((pasta / "grupos.json").read_text("utf-8"))
        except (OSError, ValueError):
            continue
        for g in grupos:
            if str(g.get("data", "")) != dia.strftime("%Y%m%d"):
                continue
            sg = _sigla_do_tipo(g.get("tipo"))
            feitos.add((pasta.name, sg))
            feitos.add((ascii_maiusculo(g.get("paciente", "")), sg))
    return feitos


def _ep():
    """Modulo principal do EcoPDF (regra de nome parecido e registro dos finalizados)."""
    m = sys.modules.get("__main__")
    if hasattr(m, "_mesmo_paciente"):
        return m
    import ecopdf
    return ecopdf


def _nomes_do_dia(cfg, dia):
    """Nomes de hoje para tirar da lista quem foi cadastrado a mao no aparelho (Helder
    10/11/2026): ([(nome das imagens, sigla)], [(nome do laudo finalizado, sigla)])."""
    imagens, laudos = [], []
    inicio = datetime.datetime.combine(dia, datetime.time()).timestamp()
    data = dia.strftime("%Y%m%d")
    raiz = cfg.get("pasta_dicom")
    for pasta in raiz.iterdir() if raiz and raiz.is_dir() else []:
        try:
            if not pasta.is_dir() or pasta.stat().st_mtime < inicio:
                continue
            grupos = json.loads((pasta / "grupos.json").read_text("utf-8"))
        except (OSError, ValueError):
            continue
        imagens += [(ascii_maiusculo(g.get("paciente", "")), _sigla_do_tipo(g.get("tipo")))
                    for g in grupos if str(g.get("data", "")) == data]
    try:
        reg = _ep()._ler_registro(cfg)
    except Exception:
        log.exception("Worklist: nao li o registro dos finalizados")
        reg = []
    laudos = [(ascii_maiusculo(r.get("nome", "")), _sigla_do_tipo(r.get("tipo")))
              for r in reg if str(r.get("data", "")) == data]
    return imagens, laudos


def _mesmo_nome(laudo, medware):
    """Nome do laudo (vem do Medware) = nome da agenda; o laudo pode vir cortado."""
    return bool(laudo) and (laudo == medware or medware.startswith(laudo + " ") or _ep().nome_cortado(laudo, medware))


def atendidos(lista, cfg, dia):
    """Codigos dos pacientes do dia em que TODOS os exames ja foram feitos. Um exame conta como
    feito se: (a) chegaram imagens com o estudo da worklist; (b) chegaram imagens com o nome
    igual; (c) ha laudo finalizado com o nome do paciente; (d) chegaram imagens com nome
    parecido (regra que junta laudo e foto: iniciais, 1-2 letras de diferenca) e esse nome so
    casa com UM paciente da lista do dia (com dois, nao tira nenhum)."""
    do_dia = [p for p in lista if p["dia"] == dia]
    if not do_dia:
        return set()
    feitos = _recebidos(cfg, dia)
    imagens, laudos = _nomes_do_dia(cfg, dia)
    ep = _ep()
    parecido = {}   # nome das imagens -> codpac, se casa com um so paciente
    for nome, _ in imagens:
        if nome in parecido:
            continue
        try:
            quem = [p["codpac"] for p in do_dia if ep._mesmo_paciente(nome, p["nome"], cfg)]
        except Exception:
            quem = []
        parecido[nome] = quem[0] if len(quem) == 1 else None
    saida = set()
    for p in do_dia:
        chaves = (uid_do_estudo(p), p["nome"])

        def feito(sg):
            def vale(s):
                return s == sg or s == ""
            return (any((c, sg) in feitos or (c, "") in feitos for c in chaves)
                    or any(vale(s) and _mesmo_nome(n, p["nome"]) for n, s in laudos)
                    or any(vale(s) and parecido.get(n) == p["codpac"] for n, s in imagens))
        if all(feito(sg) for sg in ([sigla(x) for x in p["procs"]] or [""])):
            saida.add(p["codpac"])
    return saida


def _arq_excluidos(cfg):
    return cfg["pasta_log"] / "worklist_excluidos.json"


def ler_excluidos(cfg, dia):
    """Codigos de paciente tirados da lista na tela, no dia (zera sozinho no dia seguinte)."""
    try:
        d = json.loads(_arq_excluidos(cfg).read_text("utf-8"))
    except (OSError, ValueError):
        return set()
    return set(d.get(dia.isoformat(), []))


def excluir(cfg, codpac, voltar=False):
    dia = datetime.date.today().isoformat()
    with _TRAVA:
        try:
            d = json.loads(_arq_excluidos(cfg).read_text("utf-8"))
        except (OSError, ValueError):
            d = {}
        d = {dia: d.get(dia, [])}           # so o dia de hoje
        lista = set(d[dia])
        (lista.discard if voltar else lista.add)(str(codpac))
        d[dia] = sorted(lista)
        _arq_excluidos(cfg).write_text(json.dumps(d), "utf-8")
    log.info("Worklist: paciente %s %s na tela", codpac, "devolvido" if voltar else "tirado da lista")


def lista_da_tela(cfg):
    """Pacientes de hoje para a janela Worklist da tela: quem esta na lista do aparelho,
    quem ja tem imagens e quem foi tirado a mao."""
    if not cfg.get("worklist") or "medware_servidores" not in cfg:
        return {"ok": False, "erro": "Worklist desligada neste PC (WORKLIST = nao ou componente do Medware ausente)."}
    hoje = datetime.date.today()
    try:
        lista, _ = buscar(cfg, hoje, hoje)
    except Exception as e:
        return {"ok": False, "erro": f"Não consegui ler a agenda do Medware: {str(e)[:150]}"}
    feitos, fora = atendidos(lista, cfg, hoje), ler_excluidos(cfg, hoje)
    saida = []
    for p in sorted(lista, key=lambda x: x["hora"]):
        sit = "excluido" if str(p["codpac"]) in fora else ("atendido" if p["codpac"] in feitos else "na lista")
        saida.append({"codpac": str(p["codpac"]), "hora": p["hora"].strftime("%H:%M"), "nome": p["nome"],
                      "exames": descricao(p["procs"]), "medico": p["medico"], "situacao": sit})
    return {"ok": True, "pacientes": saida, "consulta": ler_situacao(cfg)}


# ---------------------------------------------------------------- filtro por medico
# Seletor "Worklist [Todos os medicos]" na tela (Helder 02/11 e 03/11/2026): com um medico
# escolhido, ESTE PC so devolve ao aparelho os pacientes do dia daquele executante (e os sem
# executante, para ninguem sumir). Volta para Todos sozinho: a meia-noite e quando a tela
# fica mais de 3 minutos sem falar com o EcoPDF (fechada). Fica so na memoria.
SEM_TELA_SEG = 180   # janela minimizada: o navegador espaça o polling para ate 1 por minuto


def medico_ativo(cfg):
    f = cfg.get("_wl_filtro") or {}
    if not f.get("medico"):
        return ""
    if f.get("dia") != datetime.date.today().isoformat() or \
            time.time() - cfg.get("_tela_contato", 0) > SEM_TELA_SEG:
        cfg["_wl_filtro"] = {}
        log.info("Worklist: filtro por medico voltou para Todos")
        return ""
    return f["medico"]


def escolher_medico(cfg, medico):
    medico = ascii_maiusculo(medico)
    cfg["_wl_filtro"] = {"medico": medico, "dia": datetime.date.today().isoformat()} if medico else {}
    log.info("Worklist: filtro por medico = %s", medico or "Todos")
    return medico


def medicos_de_hoje(cfg):
    """Para o seletor: medicos executantes da agenda de hoje e o filtro atual."""
    if not cfg.get("worklist") or "medware_servidores" not in cfg:
        return {"ok": False, "medicos": [], "atual": ""}
    hoje = datetime.date.today()
    try:
        lista, _ = buscar(cfg, hoje, hoje)
    except Exception as e:
        return {"ok": False, "medicos": [], "atual": medico_ativo(cfg), "erro": str(e)[:150]}
    return {"ok": True, "medicos": sorted({p["medico"] for p in lista if p["medico"]}),
            "atual": medico_ativo(cfg)}


def _anotar(cfg, **dados):
    """log/worklist.json: ultima consulta (hora, IP, n) e ultimo erro. Sem nome de paciente."""
    arq = cfg["pasta_log"] / "worklist.json"
    with _TRAVA:
        try:
            atual = json.loads(arq.read_text("utf-8"))
        except (OSError, ValueError):
            atual = {}
        atual.update(dados)
        try:
            arq.write_text(json.dumps(atual, ensure_ascii=False), "utf-8")
        except OSError:
            pass


def ler_situacao(cfg):
    try:
        return json.loads((cfg["pasta_log"] / "worklist.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def on_find(event, cfg):
    """Handler EVT_C_FIND (Modality Worklist)."""
    try:
        yield from _consultar(event, cfg)
    except Exception as e:
        log.exception("Worklist: erro na consulta")
        _anotar(cfg, erro=f"{type(e).__name__}: {e}"[:200], erro_em=datetime.datetime.now().isoformat(timespec="seconds"))
        yield 0xC002, None


def _consultar(event, cfg):
    pedido = event.identifier
    ip = event.assoc.requestor.address
    ae = str(event.assoc.requestor.ae_title).strip()
    sps = pedido.get("ScheduledProcedureStepSequence")
    ini, fim = _intervalo(sps[0].get("ScheduledProcedureStepStartDate", "") if sps else "")
    nome, pid = _filtro(pedido.get("PatientName", "")), _filtro(pedido.get("PatientID", ""))
    agora = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        lista, servidor = buscar(cfg, ini, fim)
    except Exception as e:
        log.warning("Worklist: nao li o Medware (%s)", e)
        _anotar(cfg, erro=str(e)[:200], erro_em=agora, ultima_consulta=agora, ip=ip, ae=ae, n=None)
        yield 0xC001, None
        return
    # sai da lista quem ja fez todos os exames do dia (imagens ou laudo) e quem foi tirado na tela
    hoje = datetime.date.today()
    feitos = atendidos(lista, cfg, hoje)
    fora = ler_excluidos(cfg, hoje)
    lista = [p for p in lista if p["dia"] != hoje or (str(p["codpac"]) not in fora and p["codpac"] not in feitos)]
    medico = medico_ativo(cfg)
    if medico:
        lista = [p for p in lista if not p["medico"] or p["medico"] == medico]
    if nome:
        lista = [p for p in lista if all(t in p["nome"] for t in nome.split())]
    if pid:
        lista = [p for p in lista if str(p["codpac"]).startswith(pid)]
    log.info("Worklist: consulta de %s (%s), %s a %s%s%s -> %d paciente(s) [Medware %s]", ae, ip,
             ini, fim, " com filtro" if nome or pid else "", f" medico {medico}" if medico else "",
             len(lista), servidor)
    _anotar(cfg, ultima_consulta=agora, ip=ip, ae=ae, n=len(lista), servidor=servidor,
            erro="", **({"primeira_consulta": agora} if not ler_situacao(cfg).get("primeira_consulta") else {}))
    for p in lista:
        if event.is_cancelled:
            yield 0xFE00, None
            return
        yield 0xFF00, montar(p, pedido)


def testar_banco(cfg):
    """Para o painel: (True, servidor, n de hoje) ou (False, None, erro)."""
    hoje = datetime.date.today()
    try:
        lista, servidor = buscar(cfg, hoje, hoje)
        return True, servidor, len(lista)
    except Exception as e:
        return False, None, str(e)[:150]
