"""
Painel das salas: o EcoPDF manda um resumo da situacao deste PC para a planilha
da administracao (Google Apps Script), nos horarios de PAINEL_HORARIOS (9h e 15h).
PC desligado no horario: manda assim que ligar (uma vez por horario).

Vai so numero, horario, versao e nome do computador. NUNCA nome de paciente,
nome de arquivo ou texto do registro (log).

PAINEL_URL vazio no config.ini = desligado (nada sai do PC).
Teste manual: enviar_status.bat (manda agora e mostra o que foi enviado).
"""

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
ESTADO = "painel.json"          # em log/: ultimo envio feito
PASTA0_PARADO_MIN = 5           # laudo na pasta 0 ha mais que isso = renomeador travado
ESQUECIDO_HORAS = 24            # pendencia mais velha que isso deixa a sala amarela
SEM_IMAGEM_DIAS = 3             # dias uteis sem nenhuma imagem = aparelho desligado do EcoPDF?
DISCO_ATENCAO_GB, DISCO_PROBLEMA_GB = 10, 3
SINAIS_DIAS = 7                 # janela dos sinais de problema abaixo
LIBERADOS_MAX = 2               # mais que isso em 7 dias = aparelho nao esta enviando?
DESCARTADOS_MAX = 3             # mais que isso em 7 dias = nome diferente / envio duplicado?
# motivo do laudo sem par (texto da tela) -> categoria curta que vai para a planilha
MOTIVOS = (("nome fora do padrao", "nome fora do padrão"), ("aberto em outro", "PDF aberto"),
           ("DESCARTADAS", "imagens descartadas"), ("nenhuma imagem", "nenhuma imagem"))
RE_ERRO = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (ERROR|CRITICAL) ")


def _min(seg):
    return int(seg // 60)


def _pdfs(pasta):
    try:
        return [p for p in pasta.glob("*.pdf") if p.is_file()]
    except OSError:
        return []


def _idades(arqs, agora):
    out = []
    for a in arqs:
        try:
            out.append(agora - a.stat().st_mtime)
        except OSError:
            pass
    return out


def _ultima_imagem(cfg, ep):
    """(segundos desde a ultima imagem, sigla do aparelho, data do aparelho ok?)."""
    try:
        estudos = sorted((p for p in cfg["pasta_dicom"].iterdir() if p.is_dir()),
                         key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    except OSError:
        return None, "", None
    melhor, quando = None, 0.0
    for e in estudos:
        try:
            for f in e.glob("*.dcm"):
                m = f.stat().st_mtime
                if m > quando:
                    melhor, quando = f, m
        except OSError:
            continue
    if melhor is None:
        return None, "", None
    try:
        import pydicom
        ds = pydicom.dcmread(str(melhor), stop_before_pixels=True)
    except Exception:
        return time.time() - quando, "", None
    data = str(ds.get("StudyDate", "") or ds.get("ContentDate", "") or "")
    ok = None
    if len(data) == 8:
        ok = data == datetime.fromtimestamp(quando).strftime("%Y%m%d")
    return time.time() - quando, ep.sigla(ds), ok


def _erros_24h(cfg):
    limite = datetime.now() - timedelta(hours=24)
    n = 0
    for nome in ("ecopdf.log", "ecopdf.log.1"):
        try:
            with open(cfg["pasta_log"] / nome, encoding="utf-8", errors="replace") as f:
                for linha in f:
                    m = RE_ERRO.match(linha)
                    if m and datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") >= limite:
                        n += 1
        except OSError:
            pass
    return n


def _recente(iso, agora, dias=SINAIS_DIAS):
    try:
        return bool(iso) and agora - datetime.fromisoformat(iso).timestamp() < dias * 86400
    except (TypeError, ValueError):
        return False


def _sinais(cfg, ep, reg, agora):
    """Sinais de problema (nao de produtividade) dos ultimos SINAIS_DIAS dias:
    laudos liberados sem imagens, exames descartados, PDFs sumidos da pasta 2 e o
    motivo mais comum dos laudos parados sem par. So contagens e categorias."""
    liberados = sum(1 for r in reg if r.get("sem_imagens") and _recente(r.get("em"), agora))
    descartados = fora = 0
    try:
        estudos = [p for p in cfg["pasta_dicom"].iterdir() if p.is_dir()]
    except OSError:
        estudos = []
    with ep.TRAVA:  # nao contar exame no meio da juncao (PDF ja saiu, registro ainda nao)
        reg_agora = ep._ler_registro(cfg)
        for pasta in estudos:
            marcas = ep._ler_descartados(pasta)
            descartados += sum(1 for em in marcas.values() if _recente(em, agora))
            try:
                if agora - pasta.stat().st_mtime > SINAIS_DIAS * 86400:
                    continue
            except OSError:
                continue
            for g in ep._ler_grupos(pasta):
                if (not Path(g["pdf"]).exists() and not ep.registro_do_grupo(g, reg_agora)
                        and (g.get("tipo") or "") not in marcas):
                    fora += 1
    motivos = {}
    try:
        import tela
        for l in tela.laudos_sem_imagens(ep, cfg):
            m = next((c for k, c in MOTIVOS if k in l["motivo"]), "outro")
            motivos[m] = motivos.get(m, 0) + 1
    except Exception:
        ep.log.exception("Painel: nao consegui ler os motivos dos laudos sem imagens")
    return {"liberados_sem_imagens_7d": liberados, "descartados_7d": descartados,
            "fora_pasta2_7d": fora, "motivos_laudo_sem_par": motivos}


_impressora = {"em": 0.0, "valor": ""}


def _impressora_padrao():
    """'Nome (driver)' da impressora padrao do Windows. Consulta 1x por hora."""
    if os.name != "nt":
        return ""
    if time.time() - _impressora["em"] < 3600:
        return _impressora["valor"]
    cmd = ("$p=Get-CimInstance Win32_Printer -Filter 'Default=true' | Select-Object -First 1;"
           "if($p){$p.Name+' ('+$p.DriverName+')'}")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True,
                           text=True, timeout=30, creationflags=0x08000000)
        v = r.stdout.strip()[:120]
    except Exception:
        v = ""
    _impressora.update(em=time.time(), valor=v)
    return v


def _ips():
    """IPv4 deste PC: o da rede principal primeiro (rota padrao), depois os outros."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # nao envia nada; so escolhe a placa de rede
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ", ".join(ips)[:80]


def _conexao(cfg):
    try:
        c = json.loads((cfg["pasta_log"] / "conexao.json").read_text("utf-8"))
        return {k: str(c.get(k, ""))[:40] for k in ("ip", "ae", "tipo", "em")}
    except (OSError, ValueError):
        return {"ip": "", "ae": "", "tipo": "", "em": ""}


def _porta_aberta(porta):
    try:
        socket.create_connection(("127.0.0.1", porta), timeout=3).close()
        return True
    except OSError:
        return False


def coletar(cfg, ep, manual=False):
    """Resumo da situacao. So numeros, horarios e versao. manual = enviar_status.bat
    (fora do EcoPDF: confere se o EcoPDF esta ouvindo a porta em vez de ver a thread)."""
    agora = time.time()
    raiz = cfg["pasta_laudos"].parent
    ultima, sig, data_ok = _ultima_imagem(cfg, ep)
    reg = ep._ler_registro(cfg)
    juncoes = [r.get("corrigido_em") or r.get("em") for r in reg if r.get("em")]
    ultima_juncao = max(juncoes) if juncoes else ""
    retificados = 0
    for r in reg:
        em = r.get("corrigido_em")
        try:
            if em and agora - datetime.fromisoformat(em).timestamp() < 3 * 86400:
                retificados += 1
        except ValueError:
            pass
    lim = cfg["pendentes_minutos"] * 60
    img = [s for s in _idades([p for p in _pdfs(cfg["pasta_imagens"])], agora) if s > lim]
    lau = [s for s in _idades(_pdfs(cfg["pasta_laudos"]), agora) if s > lim]
    p0 = [s for s in _idades(_pdfs(raiz / "0 - Entrada PDF24"), agora) if s > PASTA0_PARADO_MIN * 60]
    revisar = len(_pdfs(raiz / "9 - Revisar"))
    try:
        disco = round(shutil.disk_usage(cfg["pasta_dicom"]).free / 1e9, 1)
    except OSError:
        disco = None
    rodando = _porta_aberta(cfg["porta"])
    if manual:
        renomeador = None  # nao da para ver de fora
    else:
        renomeador = (not cfg["renomeador"]) or any(t.name == "renomeador" and t.is_alive()
                                                    for t in threading.enumerate())
    d = {
        "pc": (os.environ.get("COMPUTERNAME") or socket.gethostname() or "?")[:40],
        "versao": ep.versao(),
        "agora": datetime.now().astimezone().isoformat(timespec="seconds"),
        "envio": "manual" if manual else "agendado",
        "ecopdf_rodando": rodando,
        "ip": _ips(),
        "ae_title": cfg["ae_title"],
        "porta": cfg["porta"],
        "ultima_conexao": _conexao(cfg),
        "ligado_desde": "" if manual else datetime.fromtimestamp(_INICIO).isoformat(timespec="minutes"),
        "ultima_imagem_min": None if ultima is None else _min(ultima),
        "aparelho": sig,
        "data_aparelho_ok": data_ok,
        "ultima_juncao": ultima_juncao,
        "imagens_sem_laudo": len(img),
        "imagens_sem_laudo_mais_antiga_min": _min(max(img)) if img else 0,
        "laudos_sem_imagens": len(lau),
        "laudos_sem_imagens_mais_antigo_min": _min(max(lau)) if lau else 0,
        "pasta0_parados": len(p0),
        "revisar": revisar,
        "retificados_3d": retificados,
        "erros_24h": _erros_24h(cfg),
        "disco_livre_gb": disco,
        "renomeador_ok": renomeador,
        "impressora": _impressora_padrao(),
        "windows": platform.release()[:20],
    }
    d.update(_sinais(cfg, ep, reg, agora))
    d["nivel"], d["problemas"] = avaliar(d)
    return d


def avaliar(d):
    """('verde'|'amarelo'|'vermelho', [frases]). A pior coisa decide a cor."""
    verm, amar = [], []
    if not d["ecopdf_rodando"]:
        verm.append("EcoPDF parado (porta DICOM fechada)")
    if d["renomeador_ok"] is False:
        verm.append("renomeador parado")
    if d["pasta0_parados"]:
        verm.append(f"{d['pasta0_parados']} laudo(s) parado(s) na pasta 0")
    if d["disco_livre_gb"] is not None:
        if d["disco_livre_gb"] < DISCO_PROBLEMA_GB:
            verm.append(f"disco quase cheio ({d['disco_livre_gb']} GB)")
        elif d["disco_livre_gb"] < DISCO_ATENCAO_GB:
            amar.append(f"pouco disco ({d['disco_livre_gb']} GB)")
    if d["data_aparelho_ok"] is False:
        amar.append("data do aparelho diferente da do PC")
    if d["revisar"]:
        amar.append(f"{d['revisar']} em 9 - Revisar")
    if d["retificados_3d"]:
        amar.append(f"{d['retificados_3d']} retificado(s) para subir de novo")
    if d["erros_24h"]:
        amar.append(f"{d['erros_24h']} erro(s) no registro em 24 h")
    esquecido = ESQUECIDO_HORAS * 60
    if d["imagens_sem_laudo_mais_antiga_min"] > esquecido:
        amar.append("imagens sem laudo há mais de 1 dia")
    if d["laudos_sem_imagens_mais_antigo_min"] > esquecido:
        amar.append("laudo sem imagens há mais de 1 dia" + _motivo_principal(d))
    if d.get("liberados_sem_imagens_7d", 0) > LIBERADOS_MAX:
        amar.append(f"{d['liberados_sem_imagens_7d']} laudos liberados sem imagens em 7 dias: "
                    "conferir se o aparelho está enviando (Store, rede, AE)")
    if d.get("descartados_7d", 0) > DESCARTADOS_MAX:
        amar.append(f"{d['descartados_7d']} exames descartados em 7 dias: conferir se o nome "
                    "no aparelho bate com o do Medware ou se há envio duplicado")
    if d.get("fora_pasta2_7d", 0):
        amar.append(f"{d['fora_pasta2_7d']} PDF(s) de imagens sumiram da pasta 2 sem juntar: "
                    "alguém apagou ou moveu à mão (Salvar folha recria)")
    if d["ultima_imagem_min"] is not None and d["ultima_imagem_min"] > SEM_IMAGEM_DIAS * 1440:
        amar.append(f"nenhuma imagem há {d['ultima_imagem_min'] // 1440} dias")
    c = d.get("ultima_conexao") or {}
    if c.get("tipo", "").startswith("recusada"):
        amar.append(f"aparelho {c.get('ip')} tentou conectar e foi recusado")
    nivel = "vermelho" if verm else "amarelo" if amar else "verde"
    return nivel, verm + amar


def _motivo_principal(d):
    m = d.get("motivos_laudo_sem_par") or {}
    if not m:
        return ""
    k = max(m, key=m.get)
    return f" (motivo: {k})"


def enviar(cfg, ep, dados=None):
    """POST para a planilha. True se a planilha respondeu ok."""
    dados = dados or coletar(cfg, ep)
    corpo = json.dumps({"chave": cfg.get("painel_chave", ""), **dados}).encode("utf-8")
    req = urllib.request.Request(cfg["painel_url"], data=corpo, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = r.read(2000).decode("utf-8", "replace")
        ok = '"ok":true' in resp.replace(" ", "")
        if not ok:
            ep.log.warning("Painel: planilha recusou o envio (%s)", resp[:200])
        return ok
    except Exception as e:
        ep.log.warning("Painel: envio falhou (%s)", type(e).__name__)
        return False


def _horarios(cfg):
    out = []
    for h in cfg.get("painel_horarios", []):
        try:
            hh, mm = (int(x) for x in h.split(":"))
            out.append((hh, mm))
        except ValueError:
            pass
    return sorted(out)


def _ultimo_horario(cfg, agora):
    """Horario agendado mais recente que ja passou HOJE (None antes do primeiro)."""
    feitos = [agora.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in _horarios(cfg)]
    feitos = [t for t in feitos if t <= agora]
    return feitos[-1] if feitos else None


def _ler_estado(cfg):
    try:
        return json.loads((cfg["pasta_log"] / ESTADO).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _rodar(cfg, ep, parar):
    parar.wait(120)  # deixa o resto do EcoPDF subir antes do primeiro envio
    tentou = 0.0
    while not parar.is_set():
        try:
            agora = datetime.now()
            alvo = _ultimo_horario(cfg, agora)
            feito = _ler_estado(cfg).get("horario", "")
            if alvo and feito < alvo.isoformat() and time.time() - tentou > 600:
                tentou = time.time()  # falhou: tenta de novo em 10 min
                if enviar(cfg, ep):
                    (cfg["pasta_log"] / ESTADO).write_text(
                        json.dumps({"horario": alvo.isoformat(), "enviado": agora.isoformat()}), "utf-8")
                    ep.log.info("Painel: status enviado (horario %s)", alvo.strftime("%H:%M"))
        except Exception:
            ep.log.exception("Painel: erro ao preparar o envio")
        parar.wait(60)


_INICIO = time.time()


def iniciar(cfg, ep, parar):
    if not cfg.get("painel_url"):
        return
    threading.Thread(target=_rodar, args=(cfg, ep, parar), daemon=True, name="painel").start()
    ep.log.info("Painel ativo: envio as %s",
                " e ".join(f"{h:02d}:{m:02d}" for h, m in _horarios(cfg)))


if __name__ == "__main__":  # enviar_status.bat: manda agora e mostra o que foi
    sys.path.insert(0, str(BASE))
    import ecopdf as ep
    cfg = ep.carregar_config(BASE / "config.ini")
    dados = coletar(cfg, ep, manual=True)
    print(json.dumps(dados, ensure_ascii=False, indent=1))
    if not cfg.get("painel_url"):
        print("\nPAINEL_URL vazio no config.ini: nada foi enviado.")
    else:
        print("\nEnviado: OK" if enviar(cfg, ep, dados) else "\nNAO ENVIOU (veja internet / link)")
