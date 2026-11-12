"""OCR (gratuito, local) para PDFs cujo texto vem embaralhado.

Alguns PDFs (ex.: cadernos do TEC "impressos" com PDFCreator/Ghostscript) trazem fontes Type 3 sem
tabela de caracteres: o pypdf extrai lixo ("8889:;<<=><?@A...") e o leitor simples não acha o
gabarito nem as questões. Nesses casos as páginas são desenhadas (pypdfium2) e lidas com o Tesseract.

Se o Tesseract não estiver instalado no servidor, tudo aqui devolve vazio e o app segue como antes
(cai na leitura com IA ou no gabarito digitado).
"""
import hashlib
import io
import re
import shutil
import subprocess
import threading
from collections import OrderedDict

from pypdf import PdfReader

DPI = 300
_cache = OrderedDict()  # hash do PDF -> {indice_pagina: texto}
_trava = threading.Lock()
_LIMITE_CACHE = 6

PALAVRAS = set("""de a o que e do da em um para com não nao uma os no se na por mais as dos como mas ao
ele das à seu sua ou quando muito nos já ja também tambem só so pelo pela até ate isso entre depois sem
mesmo aos seus quem nas me esse eles você essa num nem suas meu às minha numa pelos elas qual nós lhe
deles essas esses pelas este dele tu te vocês vos lhes meus minhas teu tua teus tuas nosso nossa nossos
nossas dela delas esta estes estas aquele aquela aqueles aquelas isto aquilo é são sao foi ser será sera
art lei juiz direito processo questão questao certo errado alternativa correta incorreta""".split())


def disponivel():
    try:
        import pypdfium2  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return bool(shutil.which("tesseract"))


def ilegivel(texto):
    """True se o texto extraído do PDF não parece português (fonte sem mapeamento) ou está vazio."""
    tokens = re.findall(r"[A-Za-zÀ-ú]{1,}", texto or "")
    if len(tokens) < 15:
        # quase nada de texto: PDF escaneado/embaralhado (se tiver caracteres estranhos de sobra)
        return len(re.sub(r"\s", "", texto or "")) > 40 or not (texto or "").strip()
    conhecidas = sum(1 for t in tokens if t.lower() in PALAVRAS)
    return conhecidas / len(tokens) < 0.06


def _idioma():
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=20).stdout
        return "por" if re.search(r"(?m)^por$", out) else "eng"
    except Exception:  # noqa: BLE001
        return "eng"


_lang = {"v": None}


def _ocr_imagem(png, psm="6"):
    if _lang["v"] is None:
        _lang["v"] = _idioma()
    r = subprocess.run(["tesseract", "stdin", "stdout", "-l", _lang["v"], "--psm", psm],
                       input=png, capture_output=True, timeout=180)
    return r.stdout.decode("utf-8", "replace")


RX_LINK_TEC = re.compile(
    r"(?im)^[^\S\n]*\W{0,3}w+[^\S\n]*[-.,]*[^\S\n]*\.?[^\S\n]*tecc?onc[uo]rsos[^\S\n]*[.,][^\S\n]*com[^\S\n]*[.,][^\S\n]*br"
    r"[^\S\n]*/[^\S\n]*quest[oõ]es[^\S\n]*/[^\S\n]*(\d+)")


def limpar_ocr(texto):
    """Corrige erros comuns do OCR que atrapalham o leitor (ex.: 'Www -.tecconcursos.com.br/questoes/ 123')."""
    return RX_LINK_TEC.sub(r"www.tecconcursos.com.br/questoes/\1", texto or "")


def _hash(data):
    return hashlib.sha1(data).hexdigest()


def paginas_ocr(data, indices=None, progresso=None):
    """Texto (OCR) das páginas pedidas (todas se None). Usa cache em memória por PDF."""
    import pypdfium2 as pdfium
    h = _hash(data)
    with _trava:
        feitas = _cache.setdefault(h, {})
        _cache.move_to_end(h)
        while len(_cache) > _LIMITE_CACHE:
            _cache.popitem(last=False)
    doc = pdfium.PdfDocument(data)
    try:
        total = len(doc)
        idxs = list(range(total)) if indices is None else [i for i in indices if 0 <= i < total]
        for k, i in enumerate(idxs):
            if i in feitas:
                continue
            img = doc[i].render(scale=DPI / 72, grayscale=True).to_pil()
            buf = io.BytesIO()
            img.save(buf, "PNG")
            feitas[i] = limpar_ocr(_ocr_imagem(buf.getvalue()))
            if progresso:
                progresso(k + 1, len(idxs))
        return [feitas.get(i, "") for i in idxs]
    finally:
        doc.close()


def texto_paginas_pypdf(data):
    try:
        return [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]
    except Exception:  # noqa: BLE001
        return []


def pdf_ilegivel(data, paginas=None):
    paginas = texto_paginas_pypdf(data) if paginas is None else paginas
    return bool(paginas) and ilegivel("\n".join(paginas[:6]))


# --------------------------------------------------------------------------- #
# Gabarito lido por OCR (tolerante aos erros típicos do Tesseract)
# --------------------------------------------------------------------------- #
RX_ITEM = re.compile(r"(?<![\d])(\d{1,4})\s*[)\]}]\s*([A-Za-zÀ-ú€©]+)")


def _letra(tok):
    t = tok.strip()
    tl = t.lower()
    if tl.startswith("cert"):
        return "C"
    if tl.startswith("err"):
        return "E"
    if tl.startswith("anul"):
        return "X"
    if len(t) > 2:  # palavra comum (texto de questão), não é resposta
        return None
    c = t[0]
    if c in "€©(":
        return "C"
    c = c.upper()
    return c if c in "ABCDEX" else None


def gabarito_ocr(texto, exigir_secao=True):
    """{n: letra} a partir do texto OCR do gabarito ("1) A  2) Certo  3) Errado ...").
    exigir_secao: só lê depois do título "Gabarito" (no PDF de questões, para não confundir com "1) Corresponde...")."""
    if not texto:
        return {}
    pos = [m.start() for m in re.finditer(r"(?i)gabarito", texto)]
    if pos:
        texto = texto[pos[-1]:]
    elif exigir_secao:
        return {}
    itens = []
    for m in RX_ITEM.finditer(texto):
        l = _letra(m.group(2))
        if l:
            itens.append([int(m.group(1)), l])
    # conserta números lidos errado ("79)" no lugar de "7)", "949)" no lugar de "94)"),
    # já que o gabarito vem em ordem: anterior + 1
    for i in range(1, len(itens)):
        ant = itens[i - 1][0]
        n = itens[i][0]
        esperado = ant + 1
        if n == esperado:
            continue
        prox = itens[i + 1][0] if i + 1 < len(itens) else None
        if str(n).startswith(str(esperado)) or prox == esperado + 1:
            itens[i][0] = esperado
    g = {}
    for n, l in itens:
        if 1 <= n <= 400 and n not in g:
            g[n] = l
    return g
