"""
Conversor do laudo do site para o Medware (atalho Ctrl+Alt+L).

O botao "Medware: copiar laudo" do site de laudos poe na area de transferencia o codigo RTF
como TEXTO PURO (comeca com "{\\rtf"), porque navegador nenhum entrega o formato RTF ao Windows.
O Medware so cola formatado se o dado estiver no formato "Rich Text Format".

Ctrl+Alt+L (em qualquer janela):
  - texto da area de transferencia comeca com "{\\rtf" -> regrava o mesmo conteudo no formato
    "Rich Text Format" (igual ao medware.ps1 testado em 10/11/2026) e, se a janela em foco for
    o Medware, ja manda Ctrl+V;
  - ja esta em Rich Text Format (atalho apertado de novo) -> so cola, se for o Medware;
  - outra coisa -> nao mexe e mostra um aviso que some em 3 s.

Substitui o Medware.bat + medware.ps1 (a instalacao tira a tecla do atalho antigo).
Roda dentro do EcoPDF (thread), na sessao do usuario. So le e grava a area de transferencia:
nao guarda nem envia o conteudo; o log registra apenas o resultado.
So Windows (ctypes, sem componente novo).
"""

import logging
import os
import threading
import time

log = logging.getLogger("ecopdf")

PREFIXO = "{\\rtf"
FORMATO_RTF = "Rich Text Format"
AVISO_NAO_LAUDO = ("A área de transferência não tem um laudo do botão Medware.\n\n"
                   "No site, clique em \"Medware: copiar laudo\" e aperte Ctrl+Alt+L de novo.")
# Janela em foco e do Medware se o caminho do programa tiver uma destas palavras
PROGRAMAS_MEDWARE = ("medware", "laudosflex")

MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
VK_L, VK_V, VK_CONTROL, VK_MENU = 0x4C, 0x56, 0x11, 0x12
WM_HOTKEY = 0x0312
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
KEYEVENTF_KEYUP = 0x0002
HWND_MESSAGE = -3
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ID_ATALHO = 0x4C31


def _api():
    import ctypes
    from ctypes import wintypes as w
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    H = w.HANDLE
    for f, args, res in [
        (u.CreateWindowExW, [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID], w.HWND),
        (u.RegisterHotKey, [w.HWND, ctypes.c_int, w.UINT, w.UINT], w.BOOL),
        (u.UnregisterHotKey, [w.HWND, ctypes.c_int], w.BOOL),
        (u.GetMessageW, [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT], w.BOOL),
        (u.OpenClipboard, [w.HWND], w.BOOL),
        (u.CloseClipboard, [], w.BOOL),
        (u.EmptyClipboard, [], w.BOOL),
        (u.GetClipboardData, [w.UINT], H),
        (u.SetClipboardData, [w.UINT, H], H),
        (u.IsClipboardFormatAvailable, [w.UINT], w.BOOL),
        (u.RegisterClipboardFormatW, [w.LPCWSTR], w.UINT),
        (u.GetForegroundWindow, [], w.HWND),
        (u.GetWindowThreadProcessId, [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
        (u.GetAsyncKeyState, [ctypes.c_int], ctypes.c_short),
        (u.keybd_event, [w.BYTE, w.BYTE, w.DWORD, ctypes.c_size_t], None),
        (u.MessageBeep, [w.UINT], w.BOOL),
        (k.GlobalAlloc, [w.UINT, ctypes.c_size_t], H),
        (k.GlobalLock, [H], w.LPVOID),
        (k.GlobalUnlock, [H], w.BOOL),
        (k.GlobalFree, [H], H),
        (k.OpenProcess, [w.DWORD, w.BOOL, w.DWORD], H),
        (k.CloseHandle, [H], w.BOOL),
        (k.QueryFullProcessImageNameW, [H, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL),
    ]:
        f.argtypes, f.restype = args, res
    return ctypes, w, u, k


def eh_laudo_rtf(texto):
    return bool(texto) and texto.lstrip().startswith(PREFIXO)


def bytes_rtf(texto):
    """O RTF do site e ASCII puro (acentos como \\uNNNN?). Se vier acento cru, vai em cp1252,
    que e o que o \\ansi do RTF espera. Termina em \\0 (exigencia do formato no Windows)."""
    t = texto.lstrip()
    try:
        b = t.encode("ascii")
    except UnicodeEncodeError:
        b = t.encode("cp1252", errors="replace")
    return b + b"\0"


class Conversor:
    def __init__(self):
        self.ctypes, self.w, self.u, self.k = _api()
        self.fmt = self.u.RegisterClipboardFormatW(FORMATO_RTF)
        # janela invisivel (so mensagens): dona da area de transferencia e do atalho
        self.hwnd = self.u.CreateWindowExW(0, "STATIC", "EcoPDF Medware", 0, 0, 0, 0, 0,
                                           HWND_MESSAGE, None, None, None)

    def _abrir(self):
        for _ in range(20):          # outro programa pode estar com a area aberta
            if self.u.OpenClipboard(self.hwnd):
                return True
            time.sleep(0.05)
        return False

    def converter(self):
        """Retorna 'convertido', 'ja_rtf', 'nao_laudo' ou 'ocupada'."""
        if not self._abrir():
            return "ocupada"
        try:
            texto = None
            h = self.u.GetClipboardData(CF_UNICODETEXT)
            if h:
                p = self.k.GlobalLock(h)
                if p:
                    try:
                        texto = self.ctypes.wstring_at(p)
                    finally:
                        self.k.GlobalUnlock(h)
            if not eh_laudo_rtf(texto):
                return "ja_rtf" if self.u.IsClipboardFormatAvailable(self.fmt) else "nao_laudo"
            dado = bytes_rtf(texto)
            hmem = self.k.GlobalAlloc(GMEM_MOVEABLE, len(dado))
            if not hmem:
                return "ocupada"
            p = self.k.GlobalLock(hmem)
            self.ctypes.memmove(p, dado, len(dado))
            self.k.GlobalUnlock(hmem)
            self.u.EmptyClipboard()
            if not self.u.SetClipboardData(self.fmt, hmem):
                self.k.GlobalFree(hmem)
                return "ocupada"
            return "convertido"      # o Windows passa a ser dono do bloco de memoria
        finally:
            self.u.CloseClipboard()

    def programa_em_foco(self):
        hwnd = self.u.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = self.w.DWORD(0)
        self.u.GetWindowThreadProcessId(hwnd, self.ctypes.byref(pid))
        hp = self.k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hp:
            return ""
        try:
            buf = self.ctypes.create_unicode_buffer(1024)
            n = self.w.DWORD(1024)
            return buf.value if self.k.QueryFullProcessImageNameW(hp, 0, buf, self.ctypes.byref(n)) else ""
        finally:
            self.k.CloseHandle(hp)

    def colar(self):
        # espera soltar Ctrl/Alt/L; senao o Medware recebe Ctrl+Alt+V
        fim = time.time() + 2
        while time.time() < fim and any(self.u.GetAsyncKeyState(v) & 0x8000 for v in (VK_MENU, VK_L)):
            time.sleep(0.02)
        if self.u.GetAsyncKeyState(VK_MENU) & 0x8000:
            self.u.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)
        kb = self.u.keybd_event
        kb(VK_CONTROL, 0, 0, 0); kb(VK_V, 0, 0, 0)
        kb(VK_V, 0, KEYEVENTF_KEYUP, 0); kb(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

    def ao_atalho(self):
        r = self.converter()
        prog = self.programa_em_foco()
        no_medware = any(p in prog.lower() for p in PROGRAMAS_MEDWARE)
        colou = r in ("convertido", "ja_rtf") and no_medware
        if colou:
            self.colar()
        log.info("Ctrl+Alt+L: %s%s", r, " (colado no Medware)" if colou else "")
        if r == "nao_laudo":
            aviso(AVISO_NAO_LAUDO)
        elif r == "ocupada":
            aviso("Não consegui usar a área de transferência agora. Aperte Ctrl+Alt+L de novo.")

    def rodar(self):
        msg = self.w.MSG()
        avisado = False
        while not self.u.RegisterHotKey(self.hwnd, ID_ATALHO, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_L):
            if not avisado:   # outro programa (ou o atalho antigo do Medware.bat) com a tecla
                log.warning("Ctrl+Alt+L ocupado por outro programa; tento de novo a cada minuto")
                avisado = True
            time.sleep(60)
        log.info("Atalho Ctrl+Alt+L (laudo do site -> Medware) ligado")
        while self.u.GetMessageW(self.ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam == ID_ATALHO:
                try:
                    self.ao_atalho()
                except Exception:
                    log.exception("Ctrl+Alt+L: erro ao converter")


_AVISO = threading.Event()


def aviso(texto):
    """Caixa pequena que some sozinha em 3 s (MessageBoxTimeoutW); uma por vez."""
    if _AVISO.is_set():
        return

    def janela():
        _AVISO.set()
        try:
            import ctypes
            u = ctypes.windll.user32
            u.MessageBeep(0x40)
            # MB_ICONINFORMATION | MB_TOPMOST | MB_SETFOREGROUND
            u.MessageBoxTimeoutW(None, texto, "EcoPDF - Medware", 0x40 | 0x40000 | 0x10000, 0, 3000)
        except Exception:
            pass
        finally:
            _AVISO.clear()

    threading.Thread(target=janela, daemon=True).start()


def iniciar():
    if os.name != "nt":
        return False

    def rodar():
        try:
            Conversor().rodar()
        except Exception:
            log.exception("Conversor Ctrl+Alt+L nao iniciou (o resto continua funcionando)")

    threading.Thread(target=rodar, daemon=True, name="medware-rtf").start()
    return True
