"""Reconstrói as questões (enunciado + alternativas A–E) a partir do texto de um PDF de caderno.

Usa o gabarito (números das questões) como guia: procura o início de cada questão na ordem
1, 2, 3... e, dentro de cada bloco, as alternativas em ordem A, B, C, D (E).
"""
import re
from collections import Counter

LETRAS = "ABCDE"

# ---------------------------------------------------------------------------
# Limpeza do texto
# ---------------------------------------------------------------------------
RX_PAGINA = re.compile(r"(?i)^\s*(p[áa]g(ina)?\.?\s*)?\d{1,4}\s*(de|/)\s*\d{1,4}\s*$")
RX_URL = re.compile(r"(?i)^\s*(https?://|www\.)\S+\s*$")


def limpar_paginas(paginas):
    """Remove cabeçalhos/rodapés repetidos e numeração de página."""
    linhas_pag = [[l.rstrip() for l in (p or "").split("\n")] for p in paginas]
    if len(linhas_pag) >= 3:
        cont = Counter()
        for ls in linhas_pag:
            for l in set(x.strip() for x in ls if x.strip()):
                cont[l] += 1
        limite = max(3, int(len(linhas_pag) * 0.4))
        repetidas = {l for l, c in cont.items() if c >= limite and len(l) > 3 and not re.fullmatch(r"\d{1,3}", l)}
    else:
        repetidas = set()
    out = []
    for ls in linhas_pag:
        for l in ls:
            s = l.strip()
            if s in repetidas or RX_PAGINA.match(s) or RX_URL.match(s):
                continue
            out.append(l)
    return "\n".join(out)


def normalizar(t):
    t = (t or "").replace("\r", "\n").replace("­", "").replace("\xa0", " ")
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)        # hifenização no fim da linha
    t = re.sub(r"[ \t]*\n[ \t]*", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = t.replace("\n\n", " ").replace("\n", " ").replace(" ", "\n\n")
    return re.sub(r"[ \t]{2,}", " ", t).strip()


# ---------------------------------------------------------------------------
# Início das questões
# ---------------------------------------------------------------------------
ESTILOS_Q = {
    "palavra": re.compile(r"(?im)^[ \t]*quest[ãa]o[ \t]*(?:n[º°o.]*[ \t]*)?0*(\d{1,3})\b[ \t]*[\.\):\-–—]?"),
    "ponto": re.compile(r"(?m)^[ \t]*0*(\d{1,3})[ \t]*[\.\)\-–—:](?!\d)"),
    "parenteses": re.compile(r"(?m)^[ \t]*0*(\d{1,3})[ \t]+(?=\()"),
    "sozinho": re.compile(r"(?m)^[ \t]*0*(\d{1,3})[ \t]*$"),
}


def _sequencia(texto, rx, numeros):
    cands = {}
    for m in rx.finditer(texto):
        cands.setdefault(int(m.group(1)), []).append((m.start(), m.end()))
    pos, achados = 0, {}
    for n in numeros:
        prox = next(((a, b) for a, b in cands.get(n, []) if a >= pos), None)
        if prox:
            achados[n] = prox
            pos = prox[1]
    return achados


def localizar_questoes(texto, numeros):
    """{n: (inicio_marcador, fim_marcador)} usando o estilo de numeração que mais acerta a sequência."""
    melhor = {}
    for rx in ESTILOS_Q.values():
        r = _sequencia(texto, rx, numeros)
        if len(r) > len(melhor):
            melhor = r
    if len(melhor) < len(numeros):  # completa buracos com qualquer estilo, respeitando a ordem
        comb = dict(melhor)
        for rx in ESTILOS_Q.values():
            for n in numeros:
                if n in comb:
                    continue
                antes = max((comb[k][1] for k in comb if k < n), default=0)
                depois = min((comb[k][0] for k in comb if k > n), default=len(texto))
                for m in rx.finditer(texto, antes, depois):
                    if int(m.group(1)) == n:
                        comb[n] = (m.start(), m.end())
                        break
        melhor = comb
    return dict(sorted(melhor.items(), key=lambda kv: kv[1][0]))


# ---------------------------------------------------------------------------
# Alternativas
# ---------------------------------------------------------------------------
ESTILOS_ALT = [
    re.compile(r"\(\s*([A-E])\s*\)"),                       # (A)
    re.compile(r"(?:^|(?<=\s))([A-E])\s*\)"),               # A)
    re.compile(r"(?:^|(?<=\s))([a-e])\s*\)"),               # a)
    re.compile(r"(?m)^[ \t]*([A-E])[ \t]*[\.\-–—][ \t]"),  # A. / A -
    re.compile(r"(?m)^[ \t]*([a-e])[ \t]*[\.\-–—][ \t]"),  # a. / a -
]


def localizar_alternativas(bloco):
    """[(letra, inicio_marcador, fim_marcador), ...] em ordem A..E (mínimo A..D)."""
    melhor = []
    for rx in ESTILOS_ALT:
        marcas = [(m.group(1).upper(), m.start(), m.end()) for m in rx.finditer(bloco)]
        # para cada 'A' possível, tenta montar a sequência; fica com a que começa mais tarde
        # entre as sequências mais longas (evita citar '(A)' dentro do enunciado)
        seqs = []
        for i, (l, a, b) in enumerate(marcas):
            if l != "A":
                continue
            seq, esperado = [(l, a, b)], 1
            for l2, a2, b2 in marcas[i + 1:]:
                if esperado < 5 and l2 == LETRAS[esperado]:
                    seq.append((l2, a2, b2))
                    esperado += 1
            if len(seq) >= 4:
                seqs.append(seq)
        if seqs:
            maior = max(len(s) for s in seqs)
            cand = [s for s in seqs if len(s) == maior][-1]
            if len(cand) > len(melhor):
                melhor = cand
        if len(melhor) == 5:
            break
    return melhor


# ---------------------------------------------------------------------------
# Textos de apoio ("Leia o texto para responder às questões 1 a 5")
# ---------------------------------------------------------------------------
RX_APOIO = re.compile(
    r"(?i)(leia\s+(?:atentamente\s+)?(?:o|a|os|as)\s+(?:texto|trecho|tirinha|charge|poema|crônica|fragmento|quadrinhos?)"
    r"|considere\s+o\s+texto|texto\s+para\s+(?:responder\s+)?(?:[àa]s?\s+)?quest"
    r"|para\s+responder\s+[àa]s?\s+quest|as\s+quest[õo]es\s+de\s+n[úu]meros)")
RX_FAIXA = re.compile(r"(?i)quest(?:ões|oes|ão|ao)\s+(?:de\s+n[úu]meros?\s+)?0*(\d{1,3})\s+(?:a|à|e|até)\s+0*(\d{1,3})")


def _faixa(txt, padrao_inicio):
    m = RX_FAIXA.search(txt[:400])
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a <= b <= a + 30:
            return a, b
    return padrao_inicio, padrao_inicio


def _limpa_cabecalho(t):
    m = re.match(r"\s*\(([^()]{4,160})\)\s*", t)
    if m and re.search(r"(?i)vunesp|fcc|cesp|cebraspe|fgv|tj|20\d\d|19\d\d|escrevente|banca", m.group(1)):
        return m.group(1).strip(), t[m.end():]
    return "", t


def extrair_questoes(texto_bruto, numeros):
    """Retorna {n: {"fonte", "enunciado", "alternativas": {A: ...}, "apoio", "ok"}}."""
    numeros = sorted(numeros)
    texto = texto_bruto
    pos = localizar_questoes(texto, numeros)
    ordem = list(pos.items())
    res, apoio_por_q = {}, {}

    # texto de apoio antes da 1ª questão
    if ordem:
        antes = texto[:ordem[0][1][0]]
        m = RX_APOIO.search(antes)
        if m:
            a, b = _faixa(antes[m.start():], ordem[0][0])
            for q in range(a, b + 1):
                apoio_por_q[q] = normalizar(antes[m.start():])

    for i, (n, (ini, fim)) in enumerate(ordem):
        prox = ordem[i + 1][1][0] if i + 1 < len(ordem) else len(texto)
        bloco = texto[fim:prox]
        alts = localizar_alternativas(bloco)
        cauda_apoio = ""
        if alts:
            ult_l, ult_a, ult_b = alts[-1]
            resto = bloco[ult_b:]
            m = RX_APOIO.search(resto)
            if m:
                cauda_apoio = resto[m.start():]
                bloco = bloco[:ult_b + m.start()]
        else:
            m = RX_APOIO.search(bloco, 40)
            if m:
                cauda_apoio = bloco[m.start():]
                bloco = bloco[:m.start()]
        if cauda_apoio and i + 1 < len(ordem):
            a, b = _faixa(cauda_apoio, ordem[i + 1][0])
            for q in range(a, b + 1):
                apoio_por_q[q] = normalizar(cauda_apoio)

        if alts:
            enun = bloco[:alts[0][1]]
            alternativas = {}
            for k, (l, a, b) in enumerate(alts):
                fim_alt = alts[k + 1][1] if k + 1 < len(alts) else len(bloco)
                alternativas[l] = normalizar(bloco[b:fim_alt])
        else:
            enun, alternativas = bloco, {}
        fonte, enun = _limpa_cabecalho(normalizar(enun))
        ok = len(alternativas) >= 4 and len(enun) >= 3 and all(alternativas.values())
        res[n] = {"fonte": fonte, "enunciado": enun, "alternativas": alternativas, "apoio": "", "ok": ok}

    for n in res:
        res[n]["apoio"] = apoio_por_q.get(n, "")
    for n in numeros:
        res.setdefault(n, {"fonte": "", "enunciado": "", "alternativas": {}, "apoio": "", "ok": False})
    return res


def cortar_gabarito(texto, parse_gabarito):
    """Remove a seção de gabarito do fim do texto (se houver)."""
    pos = [m.start() for m in re.finditer(r"(?i)gabarito", texto)]
    for p in reversed(pos):
        if p > len(texto) * 0.5 and len(parse_gabarito(texto[p:], exigir_secao=True)) >= 3:
            return texto[:p]
    return texto
