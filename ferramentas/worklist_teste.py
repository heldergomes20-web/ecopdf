"""
PROTOTIPO da lista de pacientes (worklist DICOM) lendo a agenda do Medware.

Roda numa janela separada, ao lado do EcoPDF (que continua recebendo as imagens na 11112).
Esta janela responde so a CONSULTA de worklist, na porta 11113.
So LE o banco do Medware (nunca grava). Nao guarda nada em arquivo: mostra na janela.
Fechar a janela = desliga a lista (o aparelho volta a dar "Sem conexao" no Query).

Uso: C:\\EcoPDF\\venv\\Scripts\\python.exe worklist_teste.py [senha] [porta] [caminho_do_fdb]
"""

import datetime
import sys
import unicodedata

import firebirdsql
from pydicom.dataset import Dataset
from pydicom.uid import generate_uid
from pynetdicom import AE, evt
from pynetdicom.sop_class import ModalityWorklistInformationFind, Verification

SENHA = sys.argv[1] if len(sys.argv) > 1 else "masterkey"
PORTA = int(sys.argv[2]) if len(sys.argv) > 2 else 11113
BANCO = sys.argv[3] if len(sys.argv) > 3 else r"C:\Medware LaudosFlex\BD\Clinicas.fdb"
MAX_DIAS = 7


def ascii_maiusculo(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return " ".join(s.upper().split())


def peso_kg(bruto):
    """Medware guarda PESO como BIGINT; escala ainda nao confirmada. Aceita so valor plausivel."""
    if not bruto:
        return None
    for div in (10000, 1000, 100, 10, 1):
        v = bruto / div
        if 1 <= v <= 300:
            return round(v, 1)
    return None


def altura_m(bruto):
    if not bruto:
        return None
    for div in (10000, 1000, 100, 10, 1):
        v = bruto / div
        if 0.3 <= v <= 2.3:
            return round(v, 2)
        if 30 <= v <= 230:
            return round(v / 100, 2)
    return None


def intervalo(valor):
    """'20261103' | '20261103-20261104' | '-20261103' | '20261103-' | '' -> (ini, fim) datas."""
    hoje = datetime.date.today()
    s = str(valor or "").strip()
    d = lambda t: datetime.datetime.strptime(t, "%Y%m%d").date()
    try:
        if not s:
            return hoje, hoje
        if "-" not in s:
            return d(s), d(s)
        a, b = s.split("-", 1)
        ini = d(a) if a else hoje
        fim = d(b) if b else hoje
    except ValueError:
        return hoje, hoje
    if (fim - ini).days > MAX_DIAS:
        ini = fim - datetime.timedelta(days=MAX_DIAS)
    return ini, fim


def filtro_texto(valor):
    """'SILVA*' -> 'SILVA'; '*' ou vazio -> ''."""
    return ascii_maiusculo(str(valor or "").replace("*", " ").replace("?", " ").replace("^", " "))


def buscar(ini, fim):
    con = firebirdsql.connect(host="127.0.0.1", port=3050, database=BANCO, user="SYSDBA",
                              password=SENHA, charset="WIN1252")
    try:
        cur = con.cursor()
        cur.execute("""
            select a.codagendamento, a.datahoraagendada, a.datahorachegada, a.encaixe, a.status,
                   a.codstatusagendamento, p.codpaciente, p.nome, p.datanasc, p.sexo, p.peso,
                   p.altura, m.nomeagenda, m.nome, pr.descricaoagenda, pr.descricao
            from agendamento a
            join paciente p on p.codpaciente = a.codpaciente
            left join medico m on m.codmedico = a.codmedico
            left join procedimento pr on pr.codprocedimento = a.codprocedimento
            where a.datahoraagendada >= ? and a.datahoraagendada < ?
              and a.datahoracancelado is null and a.datahorafalta is null
              and a.datahorasuspensao is null
            order by a.datahoraagendada""",
                    (ini, fim + datetime.timedelta(days=1)))
        linhas = cur.fetchall()
    finally:
        con.close()
    pacientes = {}
    for (codag, ag, cheg, enc, st, codst, codpac, nome, nasc, sexo, peso, alt,
         med_ag, med, proc_ag, proc) in linhas:
        dia = ag.date()
        chave = (codpac, dia)
        procedimento = ascii_maiusculo(proc_ag or proc)
        if chave in pacientes:
            if procedimento and procedimento not in pacientes[chave]["procs"]:
                pacientes[chave]["procs"].append(procedimento)
            continue
        pacientes[chave] = {
            "codpac": codpac, "dia": dia, "hora": ag, "nome": ascii_maiusculo(nome),
            "nasc": nasc.strftime("%Y%m%d") if nasc else "",
            "sexo": (sexo or "").strip().upper()[:1] if (sexo or "").strip().upper()[:1] in "MF" else "",
            "peso": peso_kg(peso), "altura": altura_m(alt), "peso_bruto": peso, "alt_bruto": alt,
            "medico": ascii_maiusculo(med_ag or med), "procs": [procedimento] if procedimento else [],
            "cheg": bool(cheg), "status": (st, codst),
        }
    return list(pacientes.values())


def montar(p, pedido):
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
    ds.StudyInstanceUID = generate_uid(entropy_srcs=["ecopdf-mwl", str(p["codpac"]), p["dia"].isoformat()])
    ds.RequestedProcedureID = f"{p['codpac']}{p['dia']:%m%d}"[-16:]
    ds.RequestedProcedureDescription = " + ".join(p["procs"])[:64]
    sps = Dataset()
    sps.Modality = "US"
    sps.ScheduledStationAETitle = ""
    sps.ScheduledProcedureStepStartDate = p["dia"].strftime("%Y%m%d")
    sps.ScheduledProcedureStepStartTime = p["hora"].strftime("%H%M%S")
    sps.ScheduledPerformingPhysicianName = p["medico"][:64]
    sps.ScheduledProcedureStepDescription = " + ".join(p["procs"])[:64]
    sps.ScheduledProcedureStepID = ds.RequestedProcedureID
    ds.ScheduledProcedureStepSequence = [sps]
    # devolve vazio o que o aparelho pediu e nao temos (alguns aparelhos exigem a chave presente)
    for el in pedido:
        if el.keyword and el.keyword not in ds and el.keyword != "QueryRetrieveLevel":
            try:
                setattr(ds, el.keyword, "" if el.VR != "SQ" else [])
            except Exception:
                pass
    return ds


def ao_consultar(event):
    pedido = event.identifier
    ip = event.assoc.requestor.address
    ae = str(event.assoc.requestor.ae_title).strip()
    sps = pedido.get("ScheduledProcedureStepSequence")
    data_pedida = sps[0].get("ScheduledProcedureStepStartDate", "") if sps else ""
    ini, fim = intervalo(data_pedida)
    nome = filtro_texto(pedido.get("PatientName", ""))
    pid = filtro_texto(pedido.get("PatientID", ""))
    agora = datetime.datetime.now().strftime("%H:%M:%S")
    try:
        lista = buscar(ini, fim)
    except Exception as e:
        print(f"{agora}  ERRO lendo o Medware: {e}")
        yield 0xC001, None
        return
    if nome:
        lista = [p for p in lista if all(t in p["nome"] for t in nome.split())]
    if pid:
        lista = [p for p in lista if str(p["codpac"]).startswith(pid)]
    print(f"\n{agora}  Consulta de {ae} ({ip}): dias {ini:%d/%m}-{fim:%d/%m}"
          f"{', nome ' + nome if nome else ''}{', ID ' + pid if pid else ''} -> {len(lista)} paciente(s)")
    for p in lista:
        print(f"   {p['hora']:%H:%M}  {p['nome'][:40]:40}  nasc {p['nasc'] or '-':8}  sexo {p['sexo'] or '-'}"
              f"  peso {p['peso'] or '-'} (bruto {p['peso_bruto']})  alt {p['altura'] or '-'} (bruto {p['alt_bruto']})"
              f"  {'/'.join(p['procs'])[:30]}  {p['medico'][:20]}  chegou={'s' if p['cheg'] else 'n'}")
    for p in lista:
        if event.is_cancelled:
            yield 0xFE00, None
            return
        yield 0xFF00, montar(p, pedido)


def main():
    try:
        n = len(buscar(datetime.date.today(), datetime.date.today()))
    except Exception as e:
        sys.exit(f"NAO consegui ler o Medware: {e}")
    print(f"Medware lido: {n} paciente(s) hoje.")
    ae = AE(ae_title="ECOPDF")
    ae.add_supported_context(ModalityWorklistInformationFind)
    ae.add_supported_context(Verification)
    print(f"Lista de pacientes (worklist) ouvindo na porta {PORTA}, AE ECOPDF. "
          f"Deixe esta janela aberta. Ctrl+C ou fechar = desliga.")
    ae.start_server(("0.0.0.0", PORTA), block=True,
                    evt_handlers=[(evt.EVT_C_FIND, ao_consultar)])


if __name__ == "__main__":
    main()
