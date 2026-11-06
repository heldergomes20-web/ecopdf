"""
Medidas do exame para a tela do EcoPDF e para o botao "Copiar medidas".

Fontes:
  - cabecalho DICOM de qualquer imagem: sexo, nascimento, idade, altura, peso, FC (se o
    aparelho mandar; nascimento/idade so se digitados no cadastro do aparelho)
  - relatorio estruturado (DICOM SR) que o aparelho manda ao encerrar o exame
  - caixa de medida gravada na imagem (ocr_imagem.py): no eco so quando o SR nao tem a medida;
    nas carotidas, velocidades pelo nome do vaso digitado na imagem (SR do Vivid vem com o
    vaso errado); marcado na tela (03/11/2026: e′ do Vivid do Bessa e item de usuario e nao vai
    no SR; nas carotidas o medico mede todos os vasos com o mesmo item)

Regras (decididas com o Helder em 31/10/2026):
  - valor exibido = valor recebido; so converte unidade (cm->mm, m/s->cm/s...), nunca calcula
    (excecao: idade pelo nascimento e a data do exame quando o aparelho nao manda a idade)
  - na duvida (mais de um valor diferente sem "media", unidade desconhecida) fica em branco
  - medida em branco nao gera linha no texto copiado
  - valor fora do possivel (FAIXAS, IMC, DSVE >= DDVE) fica 'suspeito': fora do texto ate confirmar na tela
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
    ("ae", "AE (diâmetro)", "mm", r"^(?!.*(vol|area|index|superior|inferior|4.?ch|chamber|A4C|length|major|minor))(?=.*(left atri|\bLA\b|\bLAD\b|\bLA ?Dim|\bLA ?Diam|\bLAs\b))(?=.*(diam|dimen|antero|\bAP\b|\bLAD\b|\bLAs\b))"),
    ("ddve", "VE diastólico (DDVE)", "mm", r"LVIDd|LVEDD|\bLVDd\b|^(?!.*(vol|area|mass))(?=.*(LVID|internal\b.*\b(dimension|diam)))(?=.*diast)"),
    ("dsve", "VE sistólico (DSVE)", "mm", r"LVIDs|LVESD|\bLVDs\b|^(?!.*(vol|area|mass))(?=.*(LVID|internal\b.*\b(dimension|diam)))(?=.*syst)"),
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
    # 1) o valor que o aparelho marcou como escolhido (Selection Status: "Mean value chosen",
    # "User chosen value"); 2) a media; 3) todos (na duvida = em branco)
    escolhidos = [v for v, d in cands if re.search(r"chosen|escolhid", d, I)]
    medias = [v for v, d in cands if re.search(r"mean|average|m[eé]dia", d, I)]
    # Philips manda o volume do AE em 4C, 2C e biplano, sem marcar qual foi escolhido: vale o biplano
    biplano = [v for v, d in cands if re.search(r"biplane|bi-plane", d, I)]
    usar = escolhidos or medias or biplano or [v for v, _ in cands]
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
        if v is not None and unidade in ("cm/s", "m/s"):
            v = abs(v)  # o sinal so diz o sentido do fluxo (Vivid manda negativo)
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
    elif "nasc" in d:
        # o Vivid nao manda PatientAge: idade na data do exame, pelo nascimento do cadastro
        exame = re.sub(r"\D", "", str(ref.get("StudyDate", "") or ref.get("ContentDate", "") or ""))
        if len(exame) == 8:
            anos = int(exame[:4]) - int(nasc[:4]) - (exame[4:8] < nasc[4:8])
            if 0 <= anos <= 120:
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

# diametros do eco em mm inteiros (Helder 03/11/2026); arredonda 0,5 para cima
INTEIROS = {"raiz", "ae", "ddve", "dsve", "siv", "pp", "asc"}


def formatar(valor, unidade):
    """Numero com virgula, sem zeros sobrando: 33, 2,4, 0,85."""
    if isinstance(valor, str):
        return valor
    casas = 2 if unidade == "m/s" else (0 if unidade in ("cm", "bpm") else 1)
    s = f"{valor:.{casas}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(".", ",")


def _do_ocr(ocr, chave, unidade):
    """Valor lido das imagens para a chave; None se nao houver ou se discordarem."""
    vals = []
    for k, valor, un in ocr or []:
        if k != chave:
            continue
        v = _converter(valor, un, unidade)
        if v is not None:
            vals.append(abs(v) if unidade in ("cm/s", "m/s") else v)
    return vals[0] if vals and len({round(v, 3) for v in vals}) == 1 else None


T8_TOL_EMED = 0.012   # m/s: E's + E'l = 2 x E' med, com o arredondamento da caixa
T8_TOL_SR = 0.011     # cm/s: velocidade lida tem de estar no SR (2 casas)


def _validar_t8(ocr, itens):
    """Leitura do Vivid T8 (chaves 't8_...') so vale conferida (Helder 04/11/2026: nao vai
    medir vaso por vaso com o item proprio; a fonte do T8 ainda tem poucas amostras):
      - e′: septal e lateral so entram se a caixa tambem mostra o E′ med que o aparelho
        calcula e (septal + lateral) bate com 2 x E′ med;
      - carotidas: cada velocidade lida tem de existir no SR do exame (o SR traz os numeros
        certos, so com o vaso errado); o vaso vem do nome digitado na imagem.
    Sem como conferir (so um e′ medido; PS/ED generico, que nao vai para o SR) o valor aparece,
    mas so entra no "Copiar medidas" depois de o medico clicar em Confirmar. Conferencia que
    falha (soma nao bate com o E′ med) = valor descartado.
    Devolve (lista sem as chaves t8_ e com as lidas no nome normal, {chaves a confirmar})."""
    if not any(str(k).startswith("t8_") for k, _, _ in ocr or []):
        return ocr, set()
    saida = [m for m in ocr if not str(m[0]).startswith("t8_")]
    conferir = set()
    es, el, em = (_do_ocr(ocr, "t8_" + c, "m/s") for c in ("es", "el", "em"))
    if None not in (es, el, em):
        if abs(es + el - 2 * em) <= T8_TOL_EMED:
            saida += [("es", es, "m/s"), ("el", el, "m/s")]
    else:
        for chave, v in (("es", es), ("el", el)):
            if v is not None:
                saida.append((chave, v, "m/s"))
                conferir.add(chave)
    no_sr = [abs(c) for _, v, u, _ in itens for c in [_converter(v, u, "cm/s")] if c is not None]
    for k, v, u in ocr:
        k = str(k)
        if not k.startswith("t8_") or k in ("t8_es", "t8_el", "t8_em"):
            continue
        c = _converter(v, u, "cm/s")
        if c is None:
            continue
        saida.append((k[3:], v, u))
        if not any(abs(abs(c) - x) <= T8_TOL_SR for x in no_sr):
            conferir.add(k[3:])
    return saida, conferir


def _sr_um_vaso_so(itens):
    """True se o SR traz mais de 2 velocidades e todas no mesmo vaso e lado: o medico mediu
    tudo com o mesmo item ("Rt Prox ACC"), entao o vaso do SR nao vale (visto no T8 em 04/11:
    a VPS da ACE esquerda aparecia como ACC direita)."""
    vasos, n = set(), 0
    for desc, _, un, _ in itens:
        if _converter(1, un, "cm/s") is None or not re.search(rf"{_PSV}|{_EDV}", desc, I):
            continue
        n += 1
        vaso = next((nome for nome, rx in (("acc", _CCA), ("ace", _ECA), ("aci", _ICA), ("vert", _VERT))
                     if re.search(rx, desc, I)), "?")
        lado = next((suf for suf, _, rx in LADOS if re.search(rx, desc, I)), "?")
        vasos.add((vaso, lado))
    return n > 2 and len(vasos) <= 1


# Faixa do POSSIVEL (nao do normal), na unidade de saida: fora dela o valor e quase certamente
# erro de leitura, de unidade ou de cadastro. Larga de proposito: vale para crianca (eco infantil)
# e para doenca grave. Valor fora fica em vermelho na tela e so entra no "Copiar medidas" depois
# que o medico confirma (Helder 04/11/2026: altura 92 cm e peso 180 kg passaram sem aviso).
FAIXAS = {
    "fc": (25, 250), "alt": (40, 220), "peso": (1.5, 300),
    "raiz": (8, 65), "ae": (8, 90), "ddve": (12, 100), "dsve": (6, 90), "siv": (2, 35), "pp": (2, 35),
    "asc": (8, 80), "aei": (5, 200), "fe": (5, 90),
    "E": (15, 300), "A": (10, 250), "es": (1.5, 30), "el": (1.5, 35), "v": (0.5, 7),
    "acc": (10, 400), "ace": (10, 600), "aci_vps": (10, 700), "aci_vdf": (1, 400), "vert_vps": (5, 300),
    "vert_diam": (1, 9), "placa_acc": (0.5, 15), "placa_aci": (0.5, 15), "placa_ace": (0.5, 15),
}
IMC = (10, 70)   # peso / altura^2: pega altura e peso trocados ou na escala errada


def _numero(texto):
    try:
        return float(str(texto).replace(",", "."))
    except ValueError:
        return None


def _marcar_suspeitos(linhas):
    """Poe 'suspeito' (motivo) nas linhas com valor fora do possivel."""
    por = {l["chave"]: l for l in linhas}
    for l in linhas:
        base = re.sub(r"_[de]$", "", l["chave"])
        v = _numero(l["valor"]) if l["valor"] else None
        if v is None or base not in FAIXAS:
            continue
        lo, hi = FAIXAS[base]
        if not lo <= v <= hi:
            l["suspeito"] = f"fora do possível ({formatar(lo, l['unidade'])} a {formatar(hi, l['unidade'])} {l['unidade']})"
    dd, ds = por.get("ddve"), por.get("dsve")
    if dd and ds and dd["valor"] and ds["valor"] and _numero(ds["valor"]) >= _numero(dd["valor"]):
        for l in (dd, ds):
            l.setdefault("suspeito", "DSVE maior ou igual ao DDVE")
    alt, peso = por.get("alt"), por.get("peso")
    if alt and peso and alt["valor"] and peso["valor"]:
        imc = _numero(peso["valor"]) / (_numero(alt["valor"]) / 100) ** 2
        if not IMC[0] <= imc <= IMC[1]:
            for l in (alt, peso):
                l.setdefault("suspeito", f"altura e peso não combinam (IMC {imc:.0f}): trocados ou em escala errada")


def medidas_do_exame(tipo, ref, srs, ocr=None):
    """{'categoria', 'linhas': [{chave, rotulo, unidade, valor (texto ou ''), origem}],
        'texto', ...} para o tipo do exame. ref = dataset de uma imagem; srs = datasets SR;
        ocr = [(chave, valor, unidade)] lidos das caixas de medida das imagens."""
    cat = categoria(tipo)
    itens = []
    for sr in srs:
        try:
            itens.extend(itens_do_sr(sr))
        except Exception:
            pass
    ocr, conferir = _validar_t8(ocr, itens)
    linhas = []
    if cat == "eco":
        cab = _do_cabecalho(ref)
        for chave, rotulo, un, padrao in ECO:
            v = cab.get(chave)
            origem = ""
            if v is None and padrao:
                v = _procurar(itens, padrao, un)
            if v is None and padrao and ocr:
                v = _do_ocr(ocr, chave, un)
                origem = "imagem" if v is not None else ""
            linhas.append({"chave": chave, "rotulo": rotulo, "unidade": un,
                           "valor": "" if v is None else formatar(
                               float(int(v + 0.5)) if chave in INTEIROS and isinstance(v, float) else v, un),
                           "grupo": "",
                           "origem": origem})
    elif cat == "carotidas":
        # No Vivid o medico costuma medir todos os vasos com o mesmo item do pacote
        # ("Rt Prox ACC PS/ED") ou com a ferramenta PS/ED generica, e o SR fica com o vaso
        # errado. Se alguma imagem tem o nome do vaso digitado (ACCD, ACID, AVE...), as
        # velocidades vem so das imagens e o SR e ignorado para elas (Helder, 03/11/2026).
        chaves_ocr = {k for k, _, _ in ocr or []
                      if re.fullmatch(r"(acc|ace|aci_vps|aci_vdf|vert_vps)_[de]", k)}
        sr_um_vaso = _sr_um_vaso_so(itens)
        for suf, nome_lado, lado in LADOS:
            for chave, rotulo, un, padrao in CAROTIDAS:
                origem = ""
                if chaves_ocr and un == "cm/s":
                    v = _do_ocr(ocr, chave + suf, un)
                    origem = "imagem" if v is not None else ""
                elif un == "cm/s" and sr_um_vaso:
                    v = None
                else:
                    v = _procurar(itens, padrao, un, lado)
                linhas.append({"chave": chave + suf, "rotulo": rotulo, "unidade": un,
                               "valor": "" if v is None else formatar(v, un),
                               "grupo": "Lado " + nome_lado, "origem": origem})
    _marcar_suspeitos(linhas)
    for l in linhas:
        if l["chave"] in conferir and l["valor"] and l["origem"] == "imagem":
            l.setdefault("suspeito", "lido da imagem do T8 sem conferência automática: confira na imagem")
    # valor suspeito so entra no texto copiado depois que o medico confirma na tela
    texto = "\n".join(f"{l['chave']}={l['valor']}" for l in linhas if l["valor"] and not l.get("suspeito"))
    return {"categoria": cat, "linhas": linhas, "texto": texto,
            "tem_sr": bool(srs), "n_medidas_sr": len(itens),
            "n_imagem": sum(1 for l in linhas if l["origem"] == "imagem")}


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
