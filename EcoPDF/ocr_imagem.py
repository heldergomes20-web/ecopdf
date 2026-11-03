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

"""

import json
import re
from pathlib import Path

import numpy as np

AQUI = Path(__file__).resolve().parent
VERSAO = 3           # muda quando a leitura muda (invalida o ocr.json de cada estudo)
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
# nome do vaso digitado na imagem (texto amarelo) -> (vaso, lado)
VASOS = {
    "ACCD": ("acc", "_d"), "ACCE": ("acc", "_e"),
    "ACID": ("aci", "_d"), "ACIE": ("aci", "_e"),
    "ACED": ("ace", "_d"), "ACEE": ("ace", "_e"),
    "AVD": ("vert", "_d"), "AVE": ("vert", "_e"),
}
# vaso -> (chave da velocidade de pico, chave da diastolica final ou None) em medidas.py
CHAVES_VASO = {"acc": ("acc", None), "ace": ("ace", None),
               "aci": ("aci_vps", "aci_vdf"), "vert": ("vert_vps", None)}
UNIDADES = ("m/s2", "m/s", "cm/s", "cm2", "cm", "mm", "ml/m2", "ml", "ms", "mmHg", "%")

_fonte = None
_fonte_rotulo = []   # letras do texto amarelo digitado pelo medico (nome do vaso)


def _carregar_fonte():
    global _fonte, _fonte_rotulo
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
        _fonte_rotulo = [(ch, np.asarray(g, dtype=np.float32) / 255.0)
                         for ch, lista in doc.get("rotulos", {}).items() for g in lista]
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
        # '#2' = segunda medida da caixa; '#' se o numero nao for reconhecido
        letras[0][0] = "#" + (letras[0][0] if letras[0][0].isdigit() and letras[0][1] <= DIST_MAX else "")
        letras[0][1] = 0.0
    texto = "".join((" " if e else "") + ch for ch, _, e in letras)
    return texto, max((d for _, d, _ in letras), default=9.0)


# --------------------------------------------------------------------------
# Interpretacao
# --------------------------------------------------------------------------

_RX = re.compile(r"^(.*?)\s+(-?\d+(?:\.\d+)?)\s*(" + "|".join(re.escape(u) for u in UNIDADES)
                 + r")?$")


def interpretar(texto):
    """'# E´SEP 0.07 m/s' -> ('E´SEP', 'es', 0.07, 'm/s'); None se nao reconhecer."""
    t = re.sub(r"^#\d?\s*", "", texto).strip()
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


# --------------------------------------------------------------------------
# Carotidas: nome do vaso digitado + caixa PS/ED
# --------------------------------------------------------------------------

def _amarelo(arr):
    """Intensidade (0-255) so dos pixels amarelos (texto digitado pelo medico)."""
    a = arr.astype(np.int16)
    m = np.minimum(a[..., 0], a[..., 1])
    return np.where((a[..., 2] < 0.6 * m) & (m > 60), m, 0).astype(np.uint8)


def _componentes(M, rot=None):
    """[(y0, y1, x0, x1)] das manchas 8-conectadas da mascara (x1/y1 exclusivos).
    Com rot (matriz int32 do tamanho de M), marca cada pixel com o numero da mancha (1, 2...)."""
    H, W = M.shape
    vis = np.zeros(M.shape, bool)
    saida = []
    for y, x in zip(*np.nonzero(M)):
        if vis[y, x]:
            continue
        vis[y, x] = True
        pilha, y0, y1, x0, x1 = [(y, x)], y, y, x, x
        while pilha:
            cy, cx = pilha.pop()
            if rot is not None:
                rot[cy, cx] = len(saida) + 1
            y0, y1, x0, x1 = min(y0, cy), max(y1, cy), min(x0, cx), max(x1, cx)
            for ny in (cy - 1, cy, cy + 1):
                for nx in (cx - 1, cx, cx + 1):
                    if 0 <= ny < H and 0 <= nx < W and M[ny, nx] and not vis[ny, nx]:
                        vis[ny, nx] = True
                        pilha.append((ny, nx))
        saida.append((int(y0), int(y1) + 1, int(x0), int(x1) + 1))
    return saida


def _aparar(g):
    """Tira colunas vazias das bordas (letra recortada de uma mancha maior)."""
    cols = np.nonzero(g.any(axis=0))[0]
    return g[:, cols[0]:cols[-1] + 1] if len(cols) else g[:, :0]


def _ler_mancha(g, fonte, profundidade=0):
    """(texto, pior distancia) de uma mancha: uma letra ou letras grudadas (ex.: 'VD', 'ID',
    visto em 03/11). Grudadas: tenta cada corte e fica com o de menor pior distancia."""
    g = _aparar(g)
    if g.shape[1] == 0 or any(t.shape[0] != g.shape[0] for _, t in fonte):
        return "", 9.0
    d, ch = _classificar(g, fonte)
    if d <= DIST_MAX or profundidade >= 2 or g.shape[1] < 6:
        return ch, d
    melhor = (ch, d)
    for c in range(2, g.shape[1] - 1):
        a, da = _ler_mancha(g[:, :c], fonte, 2)          # esquerda: uma letra so
        if da > DIST_MAX:
            continue
        b, db = _ler_mancha(g[:, c:], fonte, profundidade + 1)
        if max(da, db) < melhor[1]:
            melhor = (a + b, max(da, db))
    return melhor


MEDIA_VASO = 0.5    # 2a tentativa: distancia media por letra aceita para um nome de vaso
MARGEM_VASO = 0.3   # ... e o 2o nome mais parecido tem de ficar ao menos isto mais longe


def _custo(g, letras, fonte):
    """Soma das distancias de g (mancha) lida como a sequencia exata de letras."""
    g = _aparar(g)
    if g.shape[1] == 0:
        return 9.0 * len(letras)
    if len(letras) == 1:
        ds = [_dist(g, t) for ch, t in fonte if ch == letras and abs(t.shape[1] - g.shape[1]) <= 1]
        return min(ds) if ds else 9.0
    return min((_custo(g[:, :c], letras[0], fonte) + _custo(g[:, c:], letras[1:], fonte)
                for c in range(2, g.shape[1] - 1)), default=9.0 * len(letras))


def _vaso_da_palavra(manchas, fonte):
    """Nome de vaso mais parecido com as manchas (cada uma com 1 a 3 letras), ou None.
    Para letra desenhada um pouco diferente (o aparelho suaviza a borda conforme a
    posicao; visto em 03/11). Lado D x E: so aceita com folga sobre o 2o nome."""
    custos = []
    for nome in VASOS:
        memo = {}

        def f(i, j):
            if i == len(manchas):
                return 0.0 if j == len(nome) else 99.0
            if (i, j) not in memo:
                memo[i, j] = min((_custo(manchas[i], nome[j:j + k], fonte) + f(i + 1, j + k)
                                  for k in (1, 2, 3) if j + k <= len(nome)), default=99.0)
            return memo[i, j]
        custos.append((f(0, 0), nome))
    custos.sort()
    (c1, n1), (c2, _) = custos[0], custos[1]
    if c1 / len(n1) <= MEDIA_VASO and c2 - c1 >= MARGEM_VASO:
        return n1
    return None


def rotulos_da_imagem(arr):
    """Palavras em amarelo lidas com seguranca (ex.: 'ACCD'), em qualquer posicao."""
    if arr is None or arr.ndim != 3 or not _carregar_fonte() or not _fonte_rotulo:
        return []
    Y = _amarelo(arr)
    M = Y > TINTA
    if M.sum() > 60000:      # imagem cheia de amarelo (Doppler colorido): nao tenta
        return []
    rot = np.zeros(M.shape, np.int32)
    comps = _componentes(M, rot)
    # mancha = uma letra ou ate 3 grudadas (A encostada no V, V no D, I no D...)
    letras = sorted(((i + 1, c) for i, c in enumerate(comps) if 8 <= c[1] - c[0] <= 15 and c[3] - c[2] <= 36),
                    key=lambda ic: ic[1][2])
    palavras = []
    for i, c in letras:
        for p in palavras:
            u = p[-1][1]
            if abs(u[0] - c[0]) <= 2 and abs(u[1] - c[1]) <= 2 and -2 <= c[2] - u[3] <= 5:
                p.append((i, c))
                break
        else:
            palavras.append([(i, c)])
    saida = []
    for p in palavras:
        # so os pixels de cada mancha (a vizinha encostada nao entra no recorte)
        manchas = [np.where(rot[y0:y1, x0:x1] == i, Y[y0:y1, x0:x1], 0).astype(np.float32) / 255.0
                   for i, (y0, y1, x0, x1) in p]
        lidas = [_ler_mancha(g, _fonte_rotulo) for g in manchas]
        texto = "".join(t for t, _ in lidas)
        if any(d > DIST_MAX for _, d in lidas):
            texto = None
            if all(g.shape[0] == 12 for g in manchas) and sum(g.shape[1] for g in manchas) >= 14:
                texto = _vaso_da_palavra(manchas, _fonte_rotulo)
        if texto and len(texto) >= 2:
            saida.append(texto)
    return saida


def _carotida(arr, linhas):
    """[(chave, valor, 'cm/s', rotulo)] do vaso digitado na imagem, ou []."""
    vasos = {VASOS[p] for p in rotulos_da_imagem(arr) if p in VASOS}
    if len(vasos) != 1:
        return []      # sem nome de vaso, ou dois nomes na mesma imagem: nao arrisca
    vaso, lado = vasos.pop()
    ps, ed, idx = [], [], 0
    for texto, pior in linhas:
        m = re.match(r"^#(\d?)", texto)
        if m:
            idx = int(m.group(1) or 0)
        if pior > DIST_MAX:
            continue
        t = re.sub(r"^#\d?\s*", "", texto).strip()
        t = re.sub(r"(?<=[\d.])\s+(?=[\d.])", "", t)
        r = re.match(r"^([A-Za-z ]*?)\s*\b(PS|ED)\s+(-?\d+(?:\.\d+)?)\s*(cm/s|m/s)$", t)
        if not r:
            continue
        v = abs(float(r.group(3))) * (100 if r.group(4) == "m/s" else 1)
        if v == 0:
            continue   # ED 0.00 = segundo cursor nao colocado
        (ps if r.group(2) == "PS" else ed).append((idx, v))
    k_ps, k_ed = CHAVES_VASO[vaso]
    nome = next(n for n, vl in VASOS.items() if vl == (vaso, lado))
    saida = []
    # mais de uma medida na caixa: vale a ultima feita (maior numero)
    if ps:
        saida.append((k_ps + lado, max(ps)[1], "cm/s", nome + " PS"))
    if ed and k_ed:
        saida.append((k_ed + lado, max(ed)[1], "cm/s", nome + " ED"))
    return saida


def medidas_da_imagem(arr):
    """[(chave, valor, unidade, rotulo)] reconhecidos na imagem: itens do eco pelo rotulo
    da caixa; velocidades das carotidas pelo nome do vaso digitado na imagem."""
    saida = []
    linhas = textos_da_imagem(arr)
    for texto, pior in linhas:
        if pior > DIST_MAX:
            continue
        r = interpretar(texto)
        if r:
            rotulo, chave, valor, un = r
            saida.append((chave, valor, un, rotulo))
    if any(re.search(r"\b(PS|ED)\s+-?\d", t) for t, _ in linhas):
        saida.extend(_carotida(arr, linhas))
    return saida
