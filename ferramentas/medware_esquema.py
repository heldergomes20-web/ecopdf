"""
Levanta a ESTRUTURA do banco do Medware (Firebird) para planejar a worklist do EcoPDF.

So LE. Nao grava nada no banco. Nao copia dado de paciente: lista tabelas, colunas,
quantidade de linhas e, nas tabelas com data, quantas linhas tem hoje.
Resultado: Area de Trabalho\\medware_esquema.txt (pode mandar para a administracao).

Uso (no PC do Medware, com o EcoPDF instalado):
  C:\\EcoPDF\\venv\\Scripts\\python.exe medware_esquema.py [senha] [caminho_do_fdb]
Sem senha: tenta a padrao do Firebird (masterkey).
"""

import datetime
import os
import sys
from pathlib import Path

try:
    import firebirdsql
except ImportError:
    sys.exit("Falta o componente firebirdsql: C:\\EcoPDF\\venv\\Scripts\\python.exe -m pip install firebirdsql")

SENHA = sys.argv[1] if len(sys.argv) > 1 else "masterkey"
BANCO = sys.argv[2] if len(sys.argv) > 2 else r"C:\Medware LaudosFlex\BD\Clinicas.fdb"
SAIDA = Path(os.path.expandvars(r"%USERPROFILE%")) / "Desktop" / "medware_esquema.txt"
for d in (Path(os.path.expandvars(r"%USERPROFILE%")) / "OneDrive" / "Desktop",
          Path(os.path.expandvars(r"%USERPROFILE%")) / "OneDrive" / "Área de Trabalho"):
    if d.is_dir():
        SAIDA = d / "medware_esquema.txt"
        break

TIPOS = {7: "SMALLINT", 8: "INTEGER", 10: "FLOAT", 12: "DATE", 13: "TIME", 14: "CHAR",
         16: "BIGINT", 27: "DOUBLE", 35: "TIMESTAMP", 37: "VARCHAR", 261: "BLOB"}
INTERESSE = ("AGEND", "PACIENT", "EXAME", "ATEND", "MEDIC", "PROFISS", "LAUDO", "MARCA",
             "CONSULT", "PROCED", "UNIDADE", "SALA", "CONVENI")


def main():
    linhas = []
    escrever = linhas.append
    try:
        con = firebirdsql.connect(host="127.0.0.1", port=3050, database=BANCO, user="SYSDBA",
                                  password=SENHA, charset="WIN1252")
    except Exception as e:
        print(f"NAO CONECTEI: {e}")
        print("Se a mensagem fala de usuario/senha, rode de novo com a senha certa:")
        print("  ...python.exe medware_esquema.py SENHA")
        sys.exit(1)
    cur = con.cursor()
    cur.execute("select rdb$get_context('SYSTEM','ENGINE_VERSION') from rdb$database")
    escrever(f"Medware / Firebird {cur.fetchone()[0]}  banco: {BANCO}")
    escrever(f"Gerado em {datetime.datetime.now():%d/%m/%Y %H:%M}\n")
    cur.execute("""select trim(r.rdb$relation_name), trim(f.rdb$field_name), fl.rdb$field_type,
                          fl.rdb$field_length
                   from rdb$relations r
                   join rdb$relation_fields f on f.rdb$relation_name = r.rdb$relation_name
                   join rdb$fields fl on fl.rdb$field_name = f.rdb$field_source
                   where coalesce(r.rdb$system_flag, 0) = 0 and r.rdb$view_blr is null
                   order by r.rdb$relation_name, f.rdb$field_position""")
    tabelas = {}
    for t, c, tipo, tam in cur.fetchall():
        tabelas.setdefault(t, []).append((c, TIPOS.get(tipo, str(tipo)), tam))
    hoje = datetime.date.today()
    resumo = []
    for t, cols in tabelas.items():
        n = "?"
        try:
            cur.execute(f'select count(*) from "{t}"')
            n = cur.fetchone()[0]
        except Exception:
            con.rollback()
        hoje_txt = ""
        if any(k in t for k in INTERESSE):
            for c, tipo, _ in cols:
                if tipo in ("DATE", "TIMESTAMP"):
                    try:
                        cur.execute(f'select count(*) from "{t}" where cast("{c}" as date) = ?', (hoje,))
                        q = cur.fetchone()[0]
                        if q:
                            hoje_txt += f" {c}={q}"
                    except Exception:
                        con.rollback()
        resumo.append((t, n, hoje_txt))
        escrever(f"== {t}  ({n} linhas){'  HOJE:' + hoje_txt if hoje_txt else ''}")
        escrever("   " + ", ".join(f"{c} {tipo}{'(' + str(tam) + ')' if tipo in ('CHAR', 'VARCHAR') else ''}"
                                   for c, tipo, tam in cols))
    escrever("\n== Tabelas com linhas de HOJE (provavel agenda/exames do dia):")
    for t, n, h in resumo:
        if h:
            escrever(f"   {t}:{h}")
    con.close()
    SAIDA.write_text("\n".join(linhas), encoding="utf-8")
    print(f"Pronto: {len(tabelas)} tabelas. Arquivo: {SAIDA}")


if __name__ == "__main__":
    main()
