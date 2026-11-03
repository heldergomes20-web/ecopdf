"""
Leitura das caixas de medida que o aparelho grava na propria imagem (OCR).

Uso: so quando o relatorio estruturado (SR) nao traz a medida. Caso tipico: itens
criados pelo usuario no pacote de medidas do Vivid (ex.: E´SEP e E´LAT), que aparecem
na Worksheet e na imagem, mas nao vao para o SR (pedido do Helder em 03/11/2026).

Como funciona:
  - procura na imagem as caixas de medida (retangulo de borda clara), em qualquer
    posicao: o medico pode arrastar a caixa;
  - separa as linhas e as letras e compara cada letra com a fonte da GE guardada em
    fonte_vivid.json (gerada de exames reais do Vivid S6 do Bessa);
  - so aceita a linha se todas as letras baterem bem com a fonte e se o rotulo for
    exatamente um dos conhecidos (ROTULOS); qualquer duvida = linha ignorada.

Aparelho com outra fonte ou outra resolucao simplesmente nao gera leitura.

fonte_vivid.json NAO vai no GitHub nem no EcoPDF.zip (foi gerada de imagens de exames):
copiar do pendrive para a pasta do EcoPDF, como o painel.ini. Sem ela, nada e lido da imagem.
"""

import json
import re
from pathlib import Path

import numpy as np

AQUI = Path(__file__).resolve().parent
CLARO = 150          # borda da caixa (menor canal RGB)
TINTA = 100          # pixel de letra
DIST_MAX = 0.35      # pior letra aceita na linha (0 = identica a fonte)

# rotulo da caixa (sem espacos, pontos e acento agudo; maiusculas) -> chave das medidas
ROTULOS = {
    "ESEP": "es",
    "ELAT": "el",
    "VELOCEVM": "E",
    "VELOCAVM": "A",
    "VMÁXRT": "v",
    "VMAXRT": "v",
    "AOASC": "asc",
    "DIAMAO": "raiz",
    "DAE": "ae",
    "DIVED": "ddve",
    "DIVES": "dsve",
    "PPVED": "pp",
    "SIVD": "siv",
}
UNIDADES = ("m/s2", "m/s", "cm/s", "cm2", "cm", "mm", "ml/m2", "ml", "ms", "mmHg", "%")

_fonte = None


def _carregar_fonte():
    global _fonte
    if _fonte is None:
        arq = AQUI / "fonte_vivid.json"
        if not arq.exists():
            _fonte = False
            return _fonte
        doc = json.loads(arq.read_text(encoding="utf-8"))
        glifos = []
        for ch, lista in doc["glifos"].items():
            for g in lista:
                glifos.append((ch, np.asarray(g, dtype=np.float32) / 255.0))
        _fonte = (doc["altura"], doc["acima"], glifos)
    return _fonte


# --------------------------------------------------------------------------
# Caixas e linhas
# --------------------------------------------------------------------------

def _corridas(linha_bool, minimo):
    """[(x0, x1)] das sequencias True com tamanho >= minimo (x1 exclusivo)."""
    d = np.diff(np.concatenate(([0], linha_bool.astype(np.int8), [0])))
    ini, fim = np.where(d == 1)[0], np.where(d == -1)[0]
    return [(a, b) for a, b in zip(ini, fim) if b - a >= minimo]


def caixas(claro):
    """[(y_topo, y_fundo, x0, x1)] das caixas de medida. claro = imagem 2D (0-255)."""
    C = claro > CLARO
    H, W = C.shape
    achadas = []
    y = 0
    while y < H - 12:
        for x0, x1 in _corridas(C[y], 40):
            x1 -= 1
            if any(a[0] <= y < a[1] and a[2] - 3 <= x0 <= a[3] for a in achadas):
                continue
            esq = C[y + 1:y + 12, x0:x0 + 3].any(axis=1).all()
            dir_ = C[y + 1:y + 12, max(x0, x1 - 2):x1 + 1].any(axis=1).all()
            if not (esq and dir_):
                continue
            fundo = H
            for yy in range(y + 6, H):
                if C[yy, x0 + 3:x1 - 2].mean() > 0.9 or not C[yy, x0:x0 + 3].any():
                    fundo = yy
                    break
            if fundo - y >= 10:
                achadas.append((y, fundo, x0, x1))
        y += 1
    return achadas


def _linhas(claro, cx):
    y0, y1, x0, x1 = cx
    w = claro[y0 + 2:y1, x0 + 3:x1 - 2]
    on = (w > CLARO).any(axis=1)
    saida = []
    for a, b in _corridas(on, 5):
        a2, b2 = max(0, a - 2), min(len(w), b + 3)   # margem para acento e descendente
        saida.append(w[a2:b2])
    return saida


def _janela(lin, altura, acima):
    on = (lin > TINTA).sum(axis=1)
    mx = on.max()
    if mx == 0:
        return None
    base = max(i for i, c in enumerate(on) if c >= 0.3 * mx)
    topo = base - acima
    out = np.zeros((altura, lin.shape[1]), np.float32)
    for r in range(altura):
        y = topo + r
        if 0 <= y < lin.shape[0]:
            out[r] = lin[y] / 255.0
    return out


def _dist(a, b):
    w = max(a.shape[1], b.shape[1])
    A = np.zeros((a.shape[0], w), np.float32)
    B = np.zeros((a.shape[0], w), np.float32)
    A[:, :a.shape[1]] = a
    B[:, :b.shape[1]] = b
    return float(np.abs(A - B).sum() / max(1.0, (A + B).sum() / 2))


def _classificar(g, glifos):
    melhor = (9.0, "?")
    for ch, t in glifos:
        if abs(t.shape[1] - g.shape[1]) > 1:
            continue
        d = _dist(g, t)
        if d < melhor[0]:
            melhor = (d, ch)
    return melhor


def ler_linha(lin):
    """(texto, pior distancia) de uma linha recortada da caixa. O numero colorido do
    inicio (ordem da medida na caixa) nao conta para a pior distancia e vira '#'."""
    altura, acima, glifos = _carregar_fonte()
    win = _janela(lin, altura, acima)
    if win is None:
        return "", 9.0
    on = (win > TINTA / 255).any(axis=0)
    letras, ant = [], None   # [(texto, distancia, espaco antes)]
    for s, x in _corridas(on, 1):
        espaco = ant is not None and s - ant >= 4
        g = win[:, s:x]
        d, ch = _classificar(g, glifos)
        if d > DIST_MAX and g.shape[1] >= 6:   # duas letras grudadas (ex.: "ra")
            for c in range(2, g.shape[1] - 1):
                d1, c1 = _classificar(g[:, :c], glifos)
                d2, c2 = _classificar(g[:, c:], glifos)
                if max(d1, d2) < d:
                    d, ch = max(d1, d2), c1 + c2
        letras.append([ch, d, espaco])
        ant = x
    if len(letras) > 2 and letras[1][2] and len(letras[0][0]) == 1:
        letras[0][0], letras[0][1] = "#", 0.0
    texto = "".join((" " if e else "") + ch for ch, _, e in letras)
    return texto, max((d for _, d, _ in letras), default=9.0)


# --------------------------------------------------------------------------
# Interpretacao
# --------------------------------------------------------------------------

_RX = re.compile(r"^(.*?)\s+(-?\d+(?:\.\d+)?)\s*(" + "|".join(re.escape(u) for u in UNIDADES)
                 + r")?$")


def interpretar(texto):
    """'# E´SEP 0.07 m/s' -> ('E´SEP', 'es', 0.07, 'm/s'); None se nao reconhecer."""
    t = texto.lstrip("#").strip()
    t = re.sub(r"(?<=[\d.])\s+(?=[\d.])", "", t)       # '1 1 2.1 1' -> '112.11'
    m = _RX.match(t)
    if not m:
        return None
    rotulo, num, un = m.group(1).strip(), m.group(2), m.group(3) or ""
    chave = ROTULOS.get(re.sub(r"[\s.´]", "", rotulo).upper())
    if not chave:
        return None
    try:
        valor = float(num)
    except ValueError:
        return None
    return rotulo, chave, valor, un.lower()


def textos_da_imagem(arr):
    """Todas as linhas lidas com seguranca: [(texto, pior distancia)]."""
    if arr is None or arr.ndim not in (2, 3) or not _carregar_fonte():
        return []
    claro = arr.min(axis=2) if arr.ndim == 3 else arr
    if claro.dtype != np.uint8:
        claro = np.clip(claro, 0, 255).astype(np.uint8)
    saida = []
    for cx in caixas(claro):
        for lin in _linhas(claro, cx):
            texto, pior = ler_linha(lin)
            if texto:
                saida.append((texto, pior))
    return saida


def medidas_da_imagem(arr):
    """[(chave, valor, unidade, rotulo)] reconhecidos na imagem."""
    saida = []
    for texto, pior in textos_da_imagem(arr):
        if pior > DIST_MAX:
            continue
        r = interpretar(texto)
        if r:
            rotulo, chave, valor, un = r
            saida.append((chave, valor, un, rotulo))
    return saida
