"""
Impressao direta pelo Windows, sem a janela de impressao do navegador.

Acao do medico nao muda: coloca a(s) folha(s) de papel fotografico por cima do papel
comum na bandeja e clica Imprimir. O EcoPDF manda DOIS envios para a impressora padrao:
  1. folhas de fotos com o tipo de papel "fotografico" e a qualidade de FOTO_QUALIDADE
     (padrao = qualidade Padrao do driver para papel foto; alta; driver = nao mexe);
  2. o laudo, com papel comum, SO depois que o envio das fotos terminou (fotos com erro
     ou canceladas = laudo nao sai, para nunca cair na folha de foto).
Impressora sem modo foto (laser, preto e branco, driver sem papel fotografico): um envio
so, fotos e depois laudo, como antes.

Fila "EcoPDF Foto" (opcional, a mais estavel): se existir uma impressora com esse nome
(a mesma impressora fisica adicionada de novo, com papel fotografico salvo nas
preferencias do proprio driver; criar com criar_fila_foto.bat), as fotos vao para ela com
a configuracao salva, a qualidade trocada pela de FOTO_QUALIDADE (assim muda em todos os
PCs pela atualizacao, sem abrir as preferencias em cada um), e o laudo vai para a padrao. Serve para
drivers (ex.: Epson) que guardam o tipo de papel numa parte propria e nao obedecem ao
pedido feito por programa.

Generico: nao ha cadastro de impressora. O tipo de papel fotografico e escolhido pelos
nomes que o proprio driver informa (foto/photo/glossy/brilho...) ou pelo codigo padrao do
Windows (DMMEDIA_GLOSSY). Impressora virtual (PDF24, Microsoft Print to PDF...) ou qualquer
falha = a tela volta para o jeito antigo (janela do navegador).
Desligar: IMPRESSAO_DIRETA = nao no config.ini.
"""

import ctypes
import itertools
import os
import re
import threading
import time
from ctypes import wintypes

VIRTUAIS = re.compile(r"pdf|xps|onenote|fax|send to|enviar para", re.I)
FOTO = re.compile(r"foto|photo|glossy|brilh|lustr|satin|cetim", re.I)
FOSCO = re.compile(r"matte|fosc", re.I)
FILA_FOTO = "EcoPDF Foto"
DPI_MAX = 300
ESPERA_FOTOS_S = 15 * 60       # foto em jato de tinta e lenta; depois disso o laudo nao sai

# DEVMODEW (offsets fixos da estrutura publica)
OFF_FIELDS, OFF_QUALITY, OFF_COLOR, OFF_MEDIA = 72, 90, 92, 196
DM_PRINTQUALITY, DM_COLOR, DM_MEDIATYPE = 0x400, 0x800, 0x2000000
DMRES_MEDIUM, DMRES_HIGH, DMCOLOR_COLOR = -3, -4, 2
QUALIDADES = {"padrao": DMRES_MEDIUM, "alta": DMRES_HIGH, "driver": None}
NOME_QUALIDADE = {-1: "rascunho", -2: "baixa", -3: "padrao", -4: "alta"}
DMMEDIA_STANDARD, DMMEDIA_GLOSSY = 1, 3
DM_OUT_BUFFER, DM_IN_BUFFER = 2, 8
DC_COLORDEVICE, DC_MEDIATYPENAMES, DC_MEDIATYPES = 32, 34, 35
HORZRES, VERTRES, LOGPIXELSX, LOGPIXELSY = 8, 10, 88, 90
PHYSICALWIDTH, PHYSICALHEIGHT, PHYSICALOFFSETX, PHYSICALOFFSETY = 110, 111, 112, 113
JOB_ERRO = 0x2 | 0x20 | 0x40 | 0x200 | 0x400      # erro, offline, sem papel, bloqueado, pede intervencao
JOB_CANCELADO = 0x4 | 0x100                         # apagando, apagado

_tarefas = {}
_ids = itertools.count(1)
_trava = threading.Lock()


# --------------------------------------------------------------------------- Windows
class DOCINFOW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_int), ("lpszDocName", wintypes.LPCWSTR),
                ("lpszOutput", wintypes.LPCWSTR), ("lpszDatatype", wintypes.LPCWSTR),
                ("fwType", wintypes.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class SYSTEMTIME(ctypes.Structure):
    _fields_ = [(n, wintypes.WORD) for n in ("wYear", "wMonth", "wDayOfWeek", "wDay",
                                             "wHour", "wMinute", "wSecond", "wMilliseconds")]


class JOB_INFO_1W(ctypes.Structure):
    _fields_ = [("JobId", wintypes.DWORD), ("pPrinterName", wintypes.LPWSTR),
                ("pMachineName", wintypes.LPWSTR), ("pUserName", wintypes.LPWSTR),
                ("pDocument", wintypes.LPWSTR), ("pDatatype", wintypes.LPWSTR),
                ("pStatus", wintypes.LPWSTR), ("Status", wintypes.DWORD),
                ("Priority", wintypes.DWORD), ("Position", wintypes.DWORD),
                ("TotalPages", wintypes.DWORD), ("PagesPrinted", wintypes.DWORD),
                ("Submitted", SYSTEMTIME)]


def _api():
    ws = ctypes.WinDLL("winspool.drv", use_last_error=True)
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    ws.GetDefaultPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    ws.OpenPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
    ws.ClosePrinter.argtypes = [wintypes.HANDLE]
    ws.DocumentPropertiesW.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.LPWSTR,
                                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
    ws.DocumentPropertiesW.restype = wintypes.LONG
    ws.DeviceCapabilitiesW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.WORD,
                                       ctypes.c_void_p, ctypes.c_void_p]
    ws.DeviceCapabilitiesW.restype = ctypes.c_int
    ws.GetJobW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    gdi.CreateDCW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
    gdi.CreateDCW.restype = wintypes.HDC
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    gdi.StartDocW.argtypes = [wintypes.HDC, ctypes.POINTER(DOCINFOW)]
    gdi.EndDoc.argtypes = gdi.StartPage.argtypes = gdi.EndPage.argtypes = gdi.AbortDoc.argtypes = [wintypes.HDC]
    gdi.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi.SetBrushOrgEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    gdi.StretchDIBits.argtypes = [wintypes.HDC] + [ctypes.c_int] * 8 + [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.DWORD]
    return ws, gdi


def impressora_padrao():
    if os.name != "nt":
        return ""
    ws, _ = _api()
    n = wintypes.DWORD(0)
    ws.GetDefaultPrinterW(None, ctypes.byref(n))
    if not n.value:
        return ""
    buf = ctypes.create_unicode_buffer(n.value)
    return buf.value if ws.GetDefaultPrinterW(buf, ctypes.byref(n)) else ""


def existe_impressora(nome):
    if os.name != "nt":
        return False
    ws, _ = _api()
    h = wintypes.HANDLE()
    if not ws.OpenPrinterW(nome, ctypes.byref(h), None):
        return False
    ws.ClosePrinter(h)
    return True


def driver_porta(nome):
    """(driver, porta) da impressora, para conferir se a EcoPDF Foto e a mesma impressora da padrao."""
    ws, _ = _api()
    ws.GetPrinterW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD)]
    h = wintypes.HANDLE()
    if not ws.OpenPrinterW(nome, ctypes.byref(h), None):
        return None
    try:
        prec = wintypes.DWORD(0)
        ws.GetPrinterW(h, 2, None, 0, ctypes.byref(prec))
        if not prec.value:
            return None
        buf = ctypes.create_string_buffer(prec.value)
        if not ws.GetPrinterW(h, 2, buf, prec.value, ctypes.byref(prec)):
            return None
        ptrs = (ctypes.c_void_p * 5).from_buffer(buf)   # PRINTER_INFO_2W: server, nome, share, porta, driver
        porta = ctypes.wstring_at(ptrs[3]) if ptrs[3] else ""
        driver = ctypes.wstring_at(ptrs[4]) if ptrs[4] else ""
        return driver, porta
    finally:
        ws.ClosePrinter(h)


def fila_foto_valida(padrao):
    """True se existe a EcoPDF Foto e ela e a mesma impressora (driver e porta) da padrao."""
    if not existe_impressora(FILA_FOTO):
        return False
    try:
        a, b = driver_porta(FILA_FOTO), driver_porta(padrao)
    except Exception:
        return True    # nao deu para conferir: usa a fila (foi criada de proposito)
    return not a or not b or a == b


def _tipos_papel(nome):
    """[(codigo, nome)] dos tipos de papel que o driver informa."""
    ws, _ = _api()
    n = ws.DeviceCapabilitiesW(nome, None, DC_MEDIATYPES, None, None)
    if n <= 0:
        return []
    cods = (wintypes.DWORD * n)()
    ws.DeviceCapabilitiesW(nome, None, DC_MEDIATYPES, cods, None)
    nomes = [""] * n
    m = ws.DeviceCapabilitiesW(nome, None, DC_MEDIATYPENAMES, None, None)
    if m == n:
        buf = ctypes.create_unicode_buffer(64 * n)
        ws.DeviceCapabilitiesW(nome, None, DC_MEDIATYPENAMES, buf, None)
        raw = ctypes.wstring_at(ctypes.addressof(buf), 64 * n)
        nomes = [raw[i * 64:(i + 1) * 64].split("\0", 1)[0] for i in range(n)]
    return list(zip(list(cods), nomes))


def escolher_papel_foto(tipos):
    """Codigo do papel fotografico (brilhante de preferencia) ou None."""
    fotos = [(c, n) for c, n in tipos if FOTO.search(n or "")]
    if fotos:
        brilho = [c for c, n in fotos if not FOSCO.search(n)]
        return (brilho or [fotos[0][0]])[0]
    if any(c == DMMEDIA_GLOSSY for c, _ in tipos):
        return DMMEDIA_GLOSSY
    return None


def modo_foto(nome):
    """(codigo do papel foto ou None, codigo do papel comum ou None, texto para o registro)."""
    ws, _ = _api()
    colorida = ws.DeviceCapabilitiesW(nome, None, DC_COLORDEVICE, None, None) == 1
    tipos = _tipos_papel(nome)
    comum = DMMEDIA_STANDARD if any(c == DMMEDIA_STANDARD for c, _ in tipos) else None
    if not colorida:
        return None, comum, "impressora preto e branco: um envio so"
    foto = escolher_papel_foto(tipos)
    if foto is None:
        return None, comum, "driver sem papel fotografico: um envio so"
    nome_foto = next((n for c, n in tipos if c == foto), "") or f"codigo {foto}"
    return foto, comum, f"papel fotografico do driver: {nome_foto}"


def qualidade_foto(cfg):
    """Codigo DMRES_* da FOTO_QUALIDADE do config.ini (None = deixa a do driver/fila)."""
    return QUALIDADES.get(str(cfg.get("foto_qualidade", "padrao")).strip().lower(), DMRES_MEDIUM)


def _qualidade_txt(dm):
    """Texto da qualidade que o DEVMODE carrega, para o registro."""
    campos = ctypes.c_uint32.from_buffer(dm, OFF_FIELDS).value
    if not campos & DM_PRINTQUALITY:
        return "nao informada pelo driver"
    q = ctypes.c_int16.from_buffer(dm, OFF_QUALITY).value
    return NOME_QUALIDADE.get(q, f"{q} dpi" if q > 0 else str(q))


def _devmode(nome, papel=None, qualidade=None, log=None):
    """DEVMODE da impressora com tipo de papel/qualidade trocados (ou o padrao dela).
    qualidade = DMRES_* (None = a salva no driver). Com log, registra o que o driver devolveu
    (driver que guarda a qualidade so na parte propria pode ignorar o pedido)."""
    ws, _ = _api()
    h = wintypes.HANDLE()
    if not ws.OpenPrinterW(nome, ctypes.byref(h), None):
        raise OSError(f"nao abri a impressora {nome}")
    try:
        tam = ws.DocumentPropertiesW(None, h, nome, None, None, 0)
        if tam <= 0:
            raise OSError("driver nao informou as configuracoes")
        dm = ctypes.create_string_buffer(tam)
        if ws.DocumentPropertiesW(None, h, nome, dm, None, DM_OUT_BUFFER) < 0:
            raise OSError("driver nao entregou as configuracoes")
        campos = ctypes.c_uint32.from_buffer(dm, OFF_FIELDS)
        if papel is not None:
            ctypes.c_uint32.from_buffer(dm, OFF_MEDIA).value = papel
            campos.value |= DM_MEDIATYPE
        antes = _qualidade_txt(dm) if log else ""
        if qualidade is not None:
            ctypes.c_int16.from_buffer(dm, OFF_QUALITY).value = qualidade
            ctypes.c_int16.from_buffer(dm, OFF_COLOR).value = DMCOLOR_COLOR
            campos.value |= DM_PRINTQUALITY | DM_COLOR
        # o driver junta a troca com a parte propria dele (qualidade de foto do fabricante)
        saida = ctypes.create_string_buffer(tam)
        if ws.DocumentPropertiesW(None, h, nome, saida, dm, DM_IN_BUFFER | DM_OUT_BUFFER) < 0:
            saida = dm
        if log and qualidade is not None:
            log.info("Impressao: qualidade em %s: salva %s, pedida %s, driver ficou com %s", nome, antes,
                     NOME_QUALIDADE.get(qualidade, qualidade), _qualidade_txt(saida))
        return saida
    finally:
        ws.ClosePrinter(h)


def _imprimir_paginas(nome, paginas, devmode, titulo):
    """Um envio (um trabalho na fila). paginas = [(caminho_pdf, indice)]. Devolve o id do trabalho."""
    import numpy as np
    import pypdfium2 as pdfium
    _, gdi = _api()
    hdc = gdi.CreateDCW(None, nome, None, devmode)
    if not hdc:
        raise OSError("nao consegui preparar a impressora")
    try:
        di = DOCINFOW(ctypes.sizeof(DOCINFOW), titulo, None, None, 0)
        job = gdi.StartDocW(hdc, ctypes.byref(di))
        if job <= 0:
            raise OSError("a impressora recusou o envio")
        dpi = min(gdi.GetDeviceCaps(hdc, LOGPIXELSX) or DPI_MAX, DPI_MAX)
        fw, fh = gdi.GetDeviceCaps(hdc, PHYSICALWIDTH), gdi.GetDeviceCaps(hdc, PHYSICALHEIGHT)
        ox, oy = gdi.GetDeviceCaps(hdc, PHYSICALOFFSETX), gdi.GetDeviceCaps(hdc, PHYSICALOFFSETY)
        docs = {}
        try:
            for caminho, i in paginas:
                doc = docs.get(caminho) or docs.setdefault(caminho, pdfium.PdfDocument(str(caminho)))
                pag = doc[i]
                img = pag.render(scale=dpi / 72).to_pil().convert("RGB")
                w, h = img.size
                arr = np.asarray(img)[:, :, ::-1]
                linha = (w * 3 + 3) & ~3
                if linha != w * 3:
                    arr = np.pad(arr.reshape(h, w * 3), ((0, 0), (0, linha - w * 3)))
                bits = np.ascontiguousarray(arr).tobytes()
                bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 24, 0, 0, 0, 0, 0, 0)
                # tamanho real (100%): a pagina do PDF ocupa o papel inteiro; a margem que a
                # impressora nao alcanca fica de fora, como na impressao "tamanho real"
                esc = min(fw / w, fh / h)
                dw, dh = int(w * esc), int(h * esc)
                x, y = (fw - dw) // 2 - ox, (fh - dh) // 2 - oy
                if gdi.StartPage(hdc) <= 0:
                    raise OSError("a impressora recusou a pagina")
                gdi.SetStretchBltMode(hdc, 4)  # HALFTONE
                gdi.SetBrushOrgEx(hdc, 0, 0, None)
                gdi.StretchDIBits(hdc, x, y, dw, dh, 0, 0, w, h, bits, ctypes.byref(bmi), 0, 0x00CC0020)
                gdi.EndPage(hdc)
        except Exception:
            gdi.AbortDoc(hdc)
            raise
        finally:
            for d in docs.values():
                d.close()
        gdi.EndDoc(hdc)
        return job
    finally:
        gdi.DeleteDC(hdc)


def _estado_trabalho(nome, job):
    """None = saiu da fila; senao o Status do trabalho."""
    ws, _ = _api()
    h = wintypes.HANDLE()
    if not ws.OpenPrinterW(nome, ctypes.byref(h), None):
        return None
    try:
        prec = wintypes.DWORD(0)
        ws.GetJobW(h, job, 1, None, 0, ctypes.byref(prec))
        if not prec.value:
            return None
        buf = ctypes.create_string_buffer(prec.value)
        if not ws.GetJobW(h, job, 1, buf, prec.value, ctypes.byref(prec)):
            return None
        return JOB_INFO_1W.from_buffer(buf).Status
    finally:
        ws.ClosePrinter(h)


def esperar_trabalho(nome, job, avisar, limite=ESPERA_FOTOS_S):
    """True quando o envio saiu da fila sem ser cancelado; False se cancelado ou no limite."""
    fim = time.time() + limite
    while time.time() < fim:
        st = _estado_trabalho(nome, job)
        if st is None:
            return True
        if st & JOB_CANCELADO:
            return False
        if st & JOB_ERRO:
            avisar("A impressora pede atenção (papel, tinta, tampa ou offline). O laudo sai quando as fotos terminarem.")
        time.sleep(2)
    return False


# --------------------------------------------------------------------------- uso pela tela
def disponivel(cfg):
    """(True, impressora) ou (False, motivo) para a tela usar o jeito antigo."""
    if os.name != "nt":
        return False, "fora do Windows"
    if not cfg.get("impressao_direta", True):
        return False, "IMPRESSAO_DIRETA = nao"
    try:
        nome = impressora_padrao()
    except Exception:
        return False, "nao li a impressora padrao"
    if not nome:
        return False, "sem impressora padrao"
    if VIRTUAIS.search(nome):
        return False, f"impressora padrao virtual ({nome})"
    if nome.strip().casefold() == FILA_FOTO.casefold():
        return False, f"a impressora padrao e a {FILA_FOTO} (deixe a impressora normal como padrao)"
    return True, nome


def iniciar(paginas_fotos, paginas_laudo, cfg, log, rapido=False, qualidade="normal"):
    """Comeca a impressao em segundo plano. Devolve o id para acompanhar ou None (usar o jeito antigo).
    rapido = caixinha "Baixa qualidade": mesma sequencia (fotos, depois laudo em outro envio), mas
    tudo na impressora padrao com papel comum e qualidade padrao do driver (sem EcoPDF Foto).
    qualidade = seletor da janela Imprimir (Baixa = rapido; Alta = "alta", pede qualidade Alta nas
    fotos). O driver Epson ignora esse pedido (log 10/11): a qualidade real e a salva na EcoPDF Foto."""
    ok, nome = disponivel(cfg)
    if not ok:
        log.info("Impressao direta indisponivel: %s", nome)
        return None
    tid = next(_ids)
    with _trava:
        _tarefas[tid] = {"estado": "preparando", "mensagem": "Preparando a impressão...", "em": time.time()}
        for k in [k for k, v in _tarefas.items() if time.time() - v["em"] > 3600]:
            del _tarefas[k]

    nf = len(paginas_fotos)
    txt_fotos = "a folha de fotos" if nf == 1 else f"as {nf} folhas de fotos"
    txt_laudo = "o laudo" if len({str(pdf) for pdf, _ in paginas_laudo}) <= 1 else "os laudos"

    def atualizar(estado, msg):
        with _trava:
            _tarefas[tid].update(estado=estado, mensagem=msg)

    def rodar():
        try:
            if rapido:
                # mesma sequencia do modo normal (fotos, espera, laudo em outro envio): a pilha sai
                # igual (fotos embaixo, laudo em cima) mesmo com driver em "ordem inversa"
                _, comum, _ = modo_foto(nome)
                log.info("Impressao direta em baixa qualidade em %s (papel comum, qualidade padrao)%s", nome,
                         "" if paginas_fotos else ": so o laudo")
                if paginas_fotos:
                    atualizar("fotos", f"Imprimindo {txt_fotos} (baixa qualidade)...")
                    job = _imprimir_paginas(nome, paginas_fotos, _devmode(nome, comum), "EcoPDF - fotos")
                    if paginas_laudo and not esperar_trabalho(nome, job, lambda m: atualizar("fotos", m)):
                        log.warning("Impressao: fotos canceladas ou sem terminar; laudo nao enviado")
                        atualizar("erro", "As fotos não terminaram (canceladas ou erro na impressora). "
                                          f"{txt_laudo.capitalize()} NÃO foi impresso: confira a impressora e clique em Imprimir de novo.")
                        return
                if paginas_laudo:
                    atualizar("laudo", f"Imprimindo {txt_laudo} (papel comum)...")
                    _imprimir_paginas(nome, paginas_laudo, _devmode(nome, comum), "EcoPDF - laudo")
                atualizar("pronto", f"Enviado para {nome}" + (" (fotos em baixa qualidade)." if paginas_fotos else "."))
                return
            qfoto = DMRES_HIGH if qualidade == "alta" else qualidade_foto(cfg)
            fila = None
            if paginas_fotos and existe_impressora(FILA_FOTO):
                if fila_foto_valida(nome):
                    fila = FILA_FOTO
                else:
                    log.warning("Impressao: %s nao e a mesma impressora da padrao (%s); ignorada. "
                                "Rode criar_fila_foto.bat de novo.", FILA_FOTO, nome)
            if fila:
                log.info("Impressao direta: fotos em %s (configuracao salva no driver), laudo em %s", fila, nome)
                atualizar("fotos", f"Imprimindo {txt_fotos} (papel fotográfico)...")
                job = _imprimir_paginas(fila, paginas_fotos, _devmode(fila, None, qfoto, log), "EcoPDF - fotos")
                if paginas_laudo:
                    if not esperar_trabalho(fila, job, lambda m: atualizar("fotos", m)):
                        log.warning("Impressao: fotos canceladas ou sem terminar; laudo nao enviado")
                        atualizar("erro", "As fotos não terminaram (canceladas ou erro na impressora). "
                                          f"{txt_laudo.capitalize()} NÃO foi impresso: confira a impressora e clique em Imprimir de novo.")
                        return
                    atualizar("laudo", f"Fotos prontas. Imprimindo {txt_laudo} (papel comum)...")
                    _imprimir_paginas(nome, paginas_laudo, _devmode(nome), "EcoPDF - laudo")
                atualizar("pronto", f"Enviado: fotos em {fila}, laudo em {nome}.")
                return
            foto, comum, como = modo_foto(nome)
            log.info("Impressao direta em %s (%s)", nome, como)
            if paginas_fotos and paginas_laudo and foto is not None:
                atualizar("fotos", f"Imprimindo {txt_fotos} (papel fotográfico)...")
                job = _imprimir_paginas(nome, paginas_fotos, _devmode(nome, foto, qfoto, log), "EcoPDF - fotos")
                if not esperar_trabalho(nome, job, lambda m: atualizar("fotos", m)):
                    log.warning("Impressao: fotos canceladas ou sem terminar; laudo nao enviado")
                    atualizar("erro", "As fotos não terminaram (canceladas ou erro na impressora). "
                                      f"{txt_laudo.capitalize()} NÃO foi impresso: confira a impressora e clique em Imprimir de novo.")
                    return
                atualizar("laudo", f"Fotos prontas. Imprimindo {txt_laudo} (papel comum)...")
                _imprimir_paginas(nome, paginas_laudo, _devmode(nome, comum), "EcoPDF - laudo")
            elif paginas_fotos and foto is not None:
                atualizar("fotos", f"Imprimindo {txt_fotos} (papel fotográfico)...")
                _imprimir_paginas(nome, paginas_fotos, _devmode(nome, foto, qfoto, log), "EcoPDF - fotos")
            else:
                # sem modo foto ou so o laudo: um envio so, na ordem fotos -> laudo
                atualizar("laudo", "Imprimindo...")
                _imprimir_paginas(nome, paginas_fotos + paginas_laudo, _devmode(nome, comum), "EcoPDF - exame")
            atualizar("pronto", f"Enviado para {nome}.")
        except Exception as e:
            log.exception("Impressao direta falhou")
            atualizar("erro", f"Não consegui imprimir direto ({e}). Use Imprimir de novo: "
                              "vai abrir a janela de impressão do navegador.")
            with _trava:
                _tarefas[tid]["usar_navegador"] = True

    threading.Thread(target=rodar, daemon=True, name="impressao").start()
    return tid


def estado(tid):
    with _trava:
        t = _tarefas.get(tid)
        return dict(t) if t else None
