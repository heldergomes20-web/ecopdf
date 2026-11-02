"""
Medidas do exame para a tela do EcoPDF e para o botao "Copiar medidas".

Fontes:
  - cabecalho DICOM de qualquer imagem: sexo, nascimento, idade, altura, peso, FC (se o
    aparelho mandar; nascimento/idade so se digitados no cadastro do aparelho)
  - relatorio estruturado (DICOM SR) que o aparelho manda ao encerrar o exame

Regras (decididas com o Helder em 31/10/2026):
  - valor exibido = valor recebido; so converte unidade (cm->mm, m/s->cm/s...), nunca calcula
  - na duvida (mais de um valor diferente sem "media", unidade desconhecida) fica em branco
  - medida em branco nao gera linha no texto copiado
  - nunca entra nome, ID ou outro dado de identificacao no texto copiado; nascimento e idade
    entram (pedido do Helder em 01/11/2026, para a pagina de laudos)
  - eco fetal: nenhuma medida

Os padroes abaixo sao procurados no nome da medida + contexto do SR (grupo, local,
lado, metodo). Sao o ponto de calibracao: ajustar com exames reais de cada aparelho.
"""

import re

import pydicom

I = re.IGNORECASE

# chave, rotulo na tela, unidade de saida, padrao no SR (None = so cabecalho)
ECO = [
    ("sexo", "Sexo", "", None),
    ("nasc", "Nascimento", "", None),
    ("idade", "Idade", "anos", None),
    ("fc", "FC", "bpm", r"heart rate|\bHR\b|freq.*card"),
    ("alt", "Altura", "cm", None),
    ("peso", "Peso", "kg", None),
    ("raiz", "Raiz da aorta", "mm", r"^(?!.*asc)(?=.*(aortic root|\bao(rtic)? ?root|sinus(es)? of valsalva|\bAoR\b|\bAo Diam))"),
    ("ae", "AE (diâmetro)", "mm", r"^(?!.*(vol|area|index))(?=.*(left atri|\bLA\b|\bLAD\b|\bLA ?Dim|\bLA ?Diam|\bLAs\b))(?=.*(diam|dimen|antero|\bAP\b|\bLAD\b|\bLAs\b))"),
    ("ddve", "VE diastólico (DDVE)", "mm", r"LVIDd|LVEDD|\bLVDd\b|^(?=.*(LVID|internal dimension|internal diam))(?=.*diast)"),
    ("dsve", "VE sistólico (DSVE)", "mm", r"LVIDs|LVESD|\bLVDs\b|^(?=.*(LVID|internal dimension|internal diam))(?=.*syst)"),
    ("siv", "Septo (SIV)", "mm", r"^(?!.*(veloc|tissue|prime|e'|e′|e´|annul))(?=.*(IVSd|^(?=.*(interventricular septum|\bIVS\b|septum))(?=.*diast)))"),
    ("pp", "Parede posterior (PP)", "mm", r"LVPWd|^(?=.*(posterior wall|\bPW\b|\bLVPW\b))(?=.*diast)"),
    ("asc", "Aorta ascendente", "mm", r"ascending aorta|\bAo ?Asc|\bAscAo\b|asc.*aort|aort.*asc"),
    ("aei", "Volume do AE indexado", "mL/m²", r"^(?=.*(left atri|\bLA\b|\bLAV))(?=.*vol)(?=.*(index|/ ?BSA|\bBSA\b|\bLAVi\b))|\bLAVi\b|\bLAVI\b"),
    ("fe", "FE (Simpson)", "%", r"^(?=.*(ejection fraction|\bEF\b|\bLVEF\b))(?=.*(simpson|biplane|bi-plane|\bMOD\b|method of disk|BP\b))"),
    ("E", "Onda E mitral", "cm/s", r"^(?!.*(prime|e'|e′|e´|tissue|\btdi\b|annul|E/A|E/e|decel|\bDT\b|ratio|\btime\b|VTI|\blat|\bsept|medial))(?=.*(mitral|\bMV\b))(?=.*(E.?wave|E ?vel|peak ?E\b|\bE\b))"),
    ("A", "Onda A mitral", "cm/s", r"^(?!.*(prime|a'|a′|tissue|\btdi\b|annul|E/A|ratio|\bdur|\btime\b|\barea\b|\bMVA\b|VTI|\blat|\bsept))(?=.*(mitral|\bMV\b))(?=.*(A.?wave|A ?vel|peak ?A\b|\bA\b))"),
    ("es", "e′ septal", "cm/s", r"^(?!.*(ratio|E/e|a'|a′|s'|s′))(?=.*(sept|medial))(?=.*(e'|e′|e´|e prime|eprime|tissue|\bTDI\b|annul))"),
    ("el", "e′ lateral", "cm/s", r"^(?!.*(ratio|E/e|a'|a′|s'|s′))(?=.*\blat)(?=.*(e'|e′|e´|e prime|eprime|tissue|\bTDI\b|annul))"),
    ("v", "Vel. máx. regurgitação tricúspide", "m/s", r"^(?!.*(gradient|pressure|\bPG\b|VTI|mean|PISA|ERO|radius))(?=.*(tricusp|\bTR\b|\bTV\b))(?=.*(regurg|\bTR\b))(?=.*(max|peak|vmax|vel))"),
]

_PSV = r"(peak syst|\bPSV\b|\bPS\b|systolic vel|\bVPS\b)"
_EDV = r"(end diast|\bEDV\b|\bED\b|diastolic vel|\bVDF\b)"
_NAO = r"(?!.*(diam|thick|\bIMT\b|plaque|placa|ratio|index|\bRI\b|\bPI\b))"
_CCA = r"(common carotid|\bCCA\b|\bACC\b)"
_ECA = r"(external carotid|\bECA\b|\bACE\b)"
_ICA = r"(internal carotid|\bICA\b|\bACI\b|bulb)"
_VERT = r"(vertebral|\bVA\b|\bvert)"
_PLACA = r"(plaque|placa)"

# por lado: chave sem sufixo, rotulo, unidade, padrao (o lado entra a parte)
CAROTIDAS = [
    ("acc", "VPS ACC", "cm/s", rf"^{_NAO}(?=.*{_CCA})(?=.*{_PSV})"),
    ("ace", "VPS ACE", "cm/s", rf"^{_NAO}(?=.*{_ECA})(?=.*{_PSV})"),
    ("aci_vps", "VPS ACI", "cm/s", rf"^{_NAO}(?=.*{_ICA})(?=.*{_PSV})"),
    ("aci_vdf", "VDF ACI", "cm/s", rf"^{_NAO}(?=.*{_ICA})(?=.*{_EDV})"),
    ("vert_vps", "VPS vertebral", "cm/s", rf"^{_NAO}(?=.*{_VERT})(?=.*{_PSV})"),
    ("vert_diam", "Diâmetro vertebral", "mm", rf"^(?!.*{_PLACA})(?=.*{_VERT})(?=.*diam)"),
    ("placa_acc", "Placa ACC", "mm", rf"^(?=.*{_PLACA})(?=.*{_CCA})"),
    ("placa_aci", "Placa ACI/bulbo", "mm", rf"^(?=.*{_PLACA})(?=.*{_ICA})"),
    ("placa_ace", "Placa ACE", "mm", rf"^(?=.*{_PLACA})(?=.*{_ECA})"),
]
LADOS = (("_d", "direito", r"\bright\b|\bdir(eit[oa])?\b|\brt\b"),
         ("_e", "esquerdo", r"\bleft\b|\besq(uerd[oa])?\b|\blt\b"))

# unidade de saida -> {unidade recebida (UCUM, minusculas): fator}
FATORES = {
    "mm": {"mm": 1, "cm": 10, "m": 1000},
    "cm/s": {"cm/s": 1, "m/s": 100, "mm/s": 0.1},
    "m/s": {"m/s": 1, "cm/s": 0.01, "mm/s": 0.001},
    "%": {"%": 1},
    "mL/m²": {"ml/m2": 1, "ml/m^2": 1, "cm3/m2": 1, "ml/m²": 1},
    "bpm": {"/min": 1, "1/min": 1, "{beats}/min": 1, "{h.b.}/min": 1, "bpm": 1, "{heartbeats}/min": 1},
    "kg": {"kg": 1},
    "cm": {"cm": 1, "m": 100},
}


def categoria(tipo):
    """'eco', 'carotidas' ou '' (sem medidas: fetal e tipos desconhecidos)."""
    t = (tipo or "").lower()
    if "fetal" in t:
        return ""
    if "carot" in t or "vascular" in t:
        return "carotidas"
    if "ecocardio" in t:
        return "eco"
    return ""


# --------------------------------------------------------------------------
# Leitura do SR
# --------------------------------------------------------------------------

def _conceito(seq):
    if not seq:
        return ""
    c = seq[0]
    sig = str(c.get("CodeMeaning", "") or "")
    cod = str(c.get("CodeValue", "") or "")
    return f"{sig} [{cod}]" if cod else sig


def _texto_code(it):
    """'Finding Site: Mitral Valve' de um item CODE (modificador)."""
    nome = _conceito(it.get("ConceptNameCodeSequence"))
    valor = _conceito(it.get("ConceptCodeSequence"))
    return f"{nome}: {valor}" if valor else ""


def _num(it):
    """(valor, unidade em minusculas) de um item NUM, ou None."""
    mv = it.get("MeasuredValueSequence")
    if not mv:
        return None
    try:
        valor = float(mv[0].NumericValue)
    except (AttributeError, TypeError, ValueError):
        return None
    un = mv[0].get("MeasurementUnitsCodeSequence")
    unidade = str(un[0].get("CodeValue", "") or "").strip().lower() if un else ""
    return valor, unidade


def itens_do_sr(ds):
    """[(descritor, valor, unidade, derivacao)] de todas as medidas numericas do SR.
    descritor = nome da medida + modificadores + grupos onde ela esta."""
    saida = []

    def andar(seq, contexto):
        seq = seq or []
        # modificadores diretos do grupo (local, lado...) valem para tudo dentro dele
        mods = [_texto_code(x) for x in seq if x.get("ValueType") == "CODE"]
        ctx = contexto + [m for m in mods if m]
        for it in seq:
            vt = it.get("ValueType")
            nome = _conceito(it.get("ConceptNameCodeSequence"))
            if vt == "NUM":
                v = _num(it)
                if v is None:
                    continue
                proprios = [_texto_code(x) for x in it.get("ContentSequence", []) or []
                            if x.get("ValueType") == "CODE"]
                proprios = [p for p in proprios if p]
                deriv = " ".join(p for p in proprios if re.search(r"deriv|method|selection", p, I))
                desc = " | ".join([nome] + proprios + list(reversed(ctx)))
                saida.append((desc, v[0], v[1], deriv))
            elif vt == "CONTAINER":
                andar(it.get("ContentSequence"), ctx + [nome])
    andar(ds.get("ContentSequence"), [_conceito(ds.get("ConceptNameCodeSequence"))])
    return saida


def _converter(valor, unidade, destino):
    fator = FATORES.get(destino, {}).get(unidade)
    return None if fator is None else valor * fator


def _escolher(cands):
    """Um valor so, ou None na duvida. cands = [(valor convertido, derivacao)]."""
    if not cands:
        return None
    medias = [v for v, d in cands if re.search(r"mean|average|m[eé]dia", d, I)]
    usar = medias or [v for v, _ in cands]
    distintos = {round(v, 3) for v in usar}
    return usar[0] if len(distintos) == 1 else None


def _procurar(itens, padrao, unidade, lado=None):
    rx = re.compile(padrao, I)
    lrx = re.compile(lado, I) if lado else None
    cands = []
    for desc, valor, un, deriv in itens:
        if not rx.search(desc) or (lrx and not lrx.search(desc)):
            continue
        v = _converter(valor, un, unidade)
        if v is not None:
            cands.append((v, deriv))
    return _escolher(cands)


# --------------------------------------------------------------------------
# Cabecalho DICOM
# --------------------------------------------------------------------------

def _do_cabecalho(ref):
    d = {}
    if ref is None:
        return d
    sexo = str(ref.get("PatientSex", "") or "").strip().upper()
    if sexo in ("M", "F"):
        d["sexo"] = sexo
    nasc = re.sub(r"\D", "", str(ref.get("PatientBirthDate", "") or ""))
    if len(nasc) == 8 and 1900 <= int(nasc[:4]) <= 2100 and 1 <= int(nasc[4:6]) <= 12 and 1 <= int(nasc[6:]) <= 31:
        d["nasc"] = f"{nasc[6:]}/{nasc[4:6]}/{nasc[:4]}"
    m = re.fullmatch(r"0*(\d{1,3})([YMWD])", str(ref.get("PatientAge", "") or "").strip().upper())
    if m:
        anos = int(m.group(1)) if m.group(2) == "Y" else 0   # meses/semanas/dias = 0 ano
        if 0 <= anos <= 99:
            d["idade"] = str(anos)
    try:
        alt = float(ref.get("PatientSize") or 0)
        if 0.3 < alt < 2.5:  # metros
            d["alt"] = alt * 100
    except (TypeError, ValueError):
        pass
    try:
        peso = float(ref.get("PatientWeight") or 0)
        if 0.5 < peso < 400:
            d["peso"] = peso
    except (TypeError, ValueError):
        pass
    try:
        fc = float(ref.get("HeartRate") or 0)
        if 20 < fc < 300:
            d["fc"] = fc
    except (TypeError, ValueError):
        pass
    return d


# --------------------------------------------------------------------------
# Saida
# --------------------------------------------------------------------------

def formatar(valor, unidade):
    """Numero com virgula, sem zeros sobrando: 33, 2,4, 0,85."""
    if isinstance(valor, str):
        return valor
    casas = 2 if unidade == "m/s" else (0 if unidade in ("cm", "bpm") else 1)
    s = f"{valor:.{casas}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(".", ",")


def medidas_do_exame(tipo, ref, srs):
    """{'categoria', 'linhas': [{chave, rotulo, unidade, valor (texto ou '')}], 'texto',
        'fonte'} para o tipo do exame. ref = dataset de uma imagem; srs = datasets SR."""
    cat = categoria(tipo)
    itens = []
    for sr in srs:
        try:
            itens.extend(itens_do_sr(sr))
        except Exception:
            pass
    linhas = []
    if cat == "eco":
        cab = _do_cabecalho(ref)
        for chave, rotulo, un, padrao in ECO:
            v = cab.get(chave)
            if v is None and padrao:
                v = _procurar(itens, padrao, un)
            linhas.append({"chave": chave, "rotulo": rotulo, "unidade": un,
                           "valor": "" if v is None else formatar(v, un), "grupo": ""})
    elif cat == "carotidas":
        for suf, nome_lado, lado in LADOS:
            for chave, rotulo, un, padrao in CAROTIDAS:
                v = _procurar(itens, padrao, un, lado)
                linhas.append({"chave": chave + suf, "rotulo": rotulo, "unidade": un,
                               "valor": "" if v is None else formatar(v, un),
                               "grupo": "Lado " + nome_lado})
    texto = "\n".join(f"{l['chave']}={l['valor']}" for l in linhas if l["valor"])
    return {"categoria": cat, "linhas": linhas, "texto": texto,
            "tem_sr": bool(srs), "n_medidas_sr": len(itens)}


def eh_sr(ds):
    sop = str(ds.get("SOPClassUID", "") or "")
    return str(ds.get("Modality", "")) == "SR" or sop.startswith("1.2.840.10008.5.1.4.1.1.88.")


def ler_srs(pasta_estudo):
    srs = []
    for arq in sorted(pasta_estudo.glob("*.dcm")):
        try:
            ds = pydicom.dcmread(arq)
        except Exception:
            continue
        if "PixelData" not in ds and eh_sr(ds):
            srs.append(ds)
    return srs
