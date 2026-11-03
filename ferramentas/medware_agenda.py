"""
Segundo levantamento do Medware para a worklist: COMO a agenda e usada (so numeros).

So LE. Sem nome, nascimento ou qualquer dado de paciente: contagens por dia, por
procedimento, por status, e quanto tempo antes do exame o agendamento e criado.
Resultado: Area de Trabalho\\medware_agenda.txt

Uso: C:\\EcoPDF\\venv\\Scripts\\python.exe medware_agenda.py [senha] [caminho_do_fdb]
"""

import datetime
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import firebirdsql

SENHA = sys.argv[1] if len(sys.argv) > 1 else "masterkey"
BANCO = sys.argv[2] if len(sys.argv) > 2 else r"C:\Medware LaudosFlex\BD\Clinicas.fdb"
CASA = Path(os.path.expandvars(r"%USERPROFILE%"))
SAIDA = CASA / "Desktop" / "medware_agenda.txt"
for d in (CASA / "OneDrive" / "Desktop", CASA / "OneDrive" / "Área de Trabalho"):
    if d.is_dir():
        SAIDA = d / "medware_agenda.txt"
        break


def main():
    con = firebirdsql.connect(host="127.0.0.1", port=3050, database=BANCO, user="SYSDBA",
                              password=SENHA, charset="WIN1252")
    cur = con.cursor()
    out = []
    p = out.append
    p(f"Gerado em {datetime.datetime.now():%d/%m/%Y %H:%M}\n")

    cur.execute("select codunidade, descricao from unidade")
    p("Unidades: " + "; ".join(f"{c}={d}" for c, d in cur.fetchall()))
    cur.execute("select codempresa, descricao from empresa")
    p("Empresas: " + "; ".join(f"{c}={d}" for c, d in cur.fetchall()))
    cur.execute("select codagenda, codunidade, descricao, status from agenda")
    p("Agendas: " + "; ".join(f"{c}={d} (unid {u}, status {st})" for c, u, d, st in cur.fetchall()))
    cur.execute("select codprocedimento, descricao, status from procedimento order by 1")
    procs = {c: d for c, d, _ in cur.fetchall()}
    p("Procedimentos: " + "; ".join(f"{c}={d}" for c, d in procs.items()))
    try:
        cur.execute("select * from statusagendamento")
        cols = [d[0] for d in cur.description]
        p("STATUSAGENDAMENTO colunas: " + ", ".join(cols))
        for r in cur.fetchall():
            p("   " + " | ".join(str(x) for x in r[:4]))
    except Exception as e:
        con.rollback()
        p(f"STATUSAGENDAMENTO: {e}")

    hoje = datetime.date.today()
    ini = hoje - datetime.timedelta(days=14)
    cur.execute("""select a.datahoraagendada, a.datahoramarcacao, a.datahorachegada,
                          a.datahoraatendimento, a.codprocedimento, a.codstatusagendamento,
                          a.codmedico, a.encaixe, a.status, a.codunidade, a.codpaciente, a.codagenda,
                          p.peso, p.altura, p.datanasc, p.sexo
                   from agendamento a left join paciente p on p.codpaciente = a.codpaciente
                   where a.datahoraagendada >= ? and a.datahoraagendada < ?""",
                (ini, hoje + datetime.timedelta(days=2)))
    linhas = cur.fetchall()
    p(f"\nAgendamentos de {ini:%d/%m} a {hoje + datetime.timedelta(days=1):%d/%m}: {len(linhas)}")
    por_dia = defaultdict(Counter)
    antecedencia = defaultdict(list)
    for (ag, marc, cheg, aten, proc, st, med, enc, status, uni, pac, agenda, peso, alt, nasc, sexo) in linhas:
        dia = ag.date()
        c = por_dia[dia]
        c["total"] += 1
        c[f"proc:{procs.get(proc, proc)}"] += 1
        c[f"codstatus:{st}"] += 1
        c[f"status:{status}"] += 1
        c[f"unidade:{uni}"] += 1
        c[f"agenda:{agenda}"] += 1
        c["encaixe"] += 1 if enc else 0
        c["chegada"] += 1 if cheg else 0
        c["atendimento"] += 1 if aten else 0
        c["peso"] += 1 if peso else 0
        c["altura"] += 1 if alt else 0
        c["nasc"] += 1 if nasc else 0
        c["sexo"] += 1 if sexo else 0
        c["medico"] += 1 if med else 0
        c[f"pacientes_distintos"] = 0
        if marc:
            antecedencia[dia].append((ag - marc).total_seconds() / 60)
    pac_dia = defaultdict(set)
    for r in linhas:
        pac_dia[r[0].date()].add(r[10])
    for dia in sorted(por_dia):
        c = por_dia[dia]
        ant = sorted(antecedencia[dia])
        med = ant[len(ant) // 2] if ant else None
        mesmo_dia = sum(1 for a in ant if a < 24 * 60)
        p(f"\n{dia:%a %d/%m}: {c['total']} agendamentos, {len(pac_dia[dia])} pacientes distintos, "
          f"encaixe {c['encaixe']}, chegada {c['chegada']}, atendimento {c['atendimento']}")
        p(f"   criados <24h antes: {mesmo_dia}; antecedencia mediana: {med and round(med)} min")
        p(f"   com peso {c['peso']}, altura {c['altura']}, nascimento {c['nasc']}, sexo {c['sexo']}, medico {c['medico']}")
        p("   " + "; ".join(f"{k}={v}" for k, v in sorted(c.items()) if k.startswith(("proc:", "codstatus:", "status:", "unidade:", "agenda:"))))
    # exemplos de formato de peso/altura (sem identificar ninguem)
    pesos = [r[12] for r in linhas if r[12]][:10]
    alts = [r[13] for r in linhas if r[13]][:10]
    p(f"\nExemplos de valores PESO: {pesos}  ALTURA: {alts}")
    con.close()
    SAIDA.write_text("\n".join(out), encoding="utf-8")
    print(f"Pronto. Arquivo: {SAIDA}")


if __name__ == "__main__":
    main()
