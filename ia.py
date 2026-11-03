"""Leitura de cadernos de questões com IA (Claude API) — leitura criteriosa, com conferência.

Como funciona:
1. O PDF é enviado em partes pequenas (com uma página de contexto antes e depois). Junto vai o
   texto extraído das páginas, como apoio para a IA conferir palavra por palavra.
2. A IA primeiro faz um INVENTÁRIO (todos os números de questão que começam nas páginas) e depois
   transcreve cada questão. Os textos de apoio vêm UMA vez, com a faixa de questões que os usam
   ("Leia o texto para responder às questões 1 a 5"), e são copiados para cada questão aqui no código.
   Isso evita respostas enormes que cortavam as últimas questões.
3. Se a resposta for cortada (limite de tamanho) a parte é dividida ao meio e lida de novo; se o
   JSON vier quebrado, a parte é lida outra vez.
4. CONFERÊNCIA: no fim, tudo que faltou (número do gabarito/inventário não transcrito, questão sem
   enunciado, com menos de 4 alternativas, ou que fala de "texto" mas ficou sem texto de apoio) é
   pedido de novo, só aquelas questões, nas páginas onde elas estão. Até 2 rodadas.

O gabarito NUNCA é pedido nem guardado a partir da IA (ele vem do gabarito do caderno), exceto quando
o caderno chegou sem gabarito e ele está ESCRITO no próprio PDF.

Configuração (variáveis de ambiente no Railway):
  ANTHROPIC_API_KEY    chave da API (obrigatória para ativar a IA)
  ANTHROPIC_MODEL      modelo (padrão: claude-sonnet-5-5)
  IA_PAGINAS_POR_PARTE páginas por chamada (padrão: 4)
  IA_RODADAS_CONFERENCIA rodadas de releitura do que faltou (padrão: 2)
"""
import base64
import io
import json
import os
import re

from pypdf import PdfReader, PdfWriter

MODELO_PADRAO = "claude-sonnet-5-5"
MAX_TOKENS = 32000
MAX_TOKENS_GRANDE = 64000

REGRAS = """REGRAS DE LEITURA (seja criterioso — nada pode ficar para trás):
- Leia as páginas INTEIRAS, de cima a baixo, coluna por coluna. Cadernos da VUNESP costumam ter DUAS COLUNAS: termine a coluna da esquerda antes de ir para a da direita. Não pule caixas, quadros, notas de rodapé de textos, legendas nem trechos em itálico/negrito.
- Transcreva fielmente, em português, sem resumir, sem parafrasear, sem corrigir e sem "melhorar" o texto. Mantenha as quebras de parágrafo com \\n. Mantenha numeração de linhas/versos se fizer parte do texto. Junte palavras hifenizadas no fim de linha.
- TEXTOS DE APOIO: todo texto, poema, crônica, tirinha, charge, tabela ou trecho de lei que vem antes das questões (ex.: "Leia o texto para responder às questões de números 1 a 5") deve ser transcrito COMPLETO, do título à fonte/referência final (autor, obra, site, data). Informe a faixa de questões que o usam. Se o texto começar numa página de contexto anterior, transcreva-o inteiro mesmo assim. Tirinhas/charges: transcreva as falas de cada quadro e descreva brevemente as cenas.
- ENUNCIADO: o comando completo da questão, sem o número e sem a fonte — incluindo frases citadas, trechos destacados, itens I, II, III, afirmações, colunas para associar e qualquer texto curto que esteja DENTRO da questão (esses ficam no enunciado, não no apoio).
- ALTERNATIVAS: todas, de (A) até a última (normalmente E), cada uma completa até o fim, mesmo que continue na coluna ou página seguinte.
- FIGURAS: se a questão tiver imagem, gráfico, tabela ou esquema necessário para respondê-la, descreva-o objetivamente (tabelas: transcreva linha a linha).
- IGNORE gabaritos, folhas de resposta, rascunho, comentários, resoluções, marcações de alternativa correta, cabeçalhos/rodapés, instruções gerais da prova e propagandas. NUNCA indique qual alternativa é a correta e nunca resolva as questões.
- O "texto extraído" que acompanha o PDF (quando houver) serve só para conferir palavras; a ordem dele pode estar embaralhada. Confie na imagem do PDF para a ordem e a estrutura.
- Antes de responder, CONFIRA: cada número do inventário tem sua questão transcrita, com enunciado e todas as alternativas? Cada questão que depende de texto de apoio aparece em "textos" com a faixa certa?"""

FORMATO = """Responda SOMENTE com JSON válido, sem texto antes ou depois, no formato:
{{"inventario": [1, 2, 3],
 "textos": [{{"questoes": [1, 5], "texto": "título\\n\\ntexto completo...\\n\\n(Autor, Obra)"}}],
 "questoes": [{{"numero": 1, "fonte": "", "apoio": "", "enunciado": "", "alternativas": {{"A": "", "B": "", "C": "", "D": "", "E": ""}}, "figura": ""}}]{campo_gabarito}}}

- "inventario": TODOS os números de questão que começam nas páginas principais, em ordem.
- "textos": cada texto de apoio UMA única vez; "questoes": [primeira, última] questão que o usa. Se não houver, [].
- "apoio" dentro da questão: deixe "" quando o texto já está em "textos"; use só se a questão tiver um texto de apoio exclusivo que não esteja em "textos".
- "fonte": o cabeçalho da questão, se houver (ex.: "VUNESP - 2023 - TJ-SP - Escrevente Técnico Judiciário"); senão "".
- "numero": inteiro, como aparece no caderno."""

PROMPT = """Você está transcrevendo um caderno de questões de concurso (banca VUNESP, concurso de Escrevente do TJSP) para um aplicativo de estudos.

Este PDF contém {total} página(s). As páginas PRINCIPAIS são as páginas {ini} a {fim} deste PDF.{contexto}{esperadas}

Transcreva TODAS as questões de múltipla escolha cujo número/início aparece nas páginas principais. Se o final de uma questão continuar numa página de contexto seguinte, use-a para completar. Não transcreva questões que começam apenas nas páginas de contexto (mas transcreva textos de apoio de páginas de contexto anteriores que as questões principais usem).

{regras}
{extra_gabarito}{texto_extraido}
{formato}"""

PROMPT_RELEITURA = """Você está transcrevendo um caderno de questões de concurso (banca VUNESP, concurso de Escrevente do TJSP) para um aplicativo de estudos.

Uma primeira leitura deixou falhas. Este PDF contém {total} página(s) do caderno. Procure nele e transcreva APENAS as questões de números: {lista}.
{problemas}
Procure com atenção em todas as páginas e nas duas colunas — a questão pode estar no pé de uma coluna, continuar na coluna/página seguinte, ou depender de um texto de apoio que começa páginas antes. Transcreva também, COMPLETO, o texto de apoio que essas questões usam. Se uma dessas questões realmente não estiver neste PDF, deixe-a fora da lista "questoes" e fora do "inventario".

{regras}
{texto_extraido}
{formato}"""

EXTRA_GABARITO = """
GABARITO: este caderno chegou sem gabarito identificado. Se as páginas (principais ou de contexto) trouxerem o GABARITO OFICIAL escrito — uma tabela/lista de respostas, ou "Gabarito: B" / "Resposta: letra C" depois de cada questão —, transcreva-o no campo "gabarito" como objeto {"número": "letra"}, exatamente como está escrito (use "X" para questão anulada). Inclua TODAS as respostas que aparecerem nessas páginas, inclusive de questões que começam em outras páginas.
NUNCA resolva as questões nem deduza respostas. Se o gabarito não estiver escrito nas páginas, use "gabarito": {}.
"""

RX_PEDE_APOIO = re.compile(
    r"(?i)\b(?:d?[on]o?s?\s+texto|texto\s+(?:acima|apresentado|lido|i{1,3}\b)|d[ao]\s+(?:tirinha|charge|poema|crônica|cronica|trecho|fragmento|quadrinho)"
    r"|segundo\s+o\s+(?:autor|narrador|texto)|(?:primeiro|segundo|terceiro|quarto|último|ultimo)\s+parágrafo|\bverso)")


def disponivel():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def modelo():
    return os.environ.get("ANTHROPIC_MODEL") or MODELO_PADRAO


def _pdf_paginas(reader, idxs):
    w = PdfWriter()
    for i in idxs:
        w.add_page(reader.pages[i])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _json(texto):
    texto = (texto or "").strip()
    texto = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto)
    a, b = texto.find("{"), texto.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("a IA não devolveu JSON")
    return json.loads(texto[a:b + 1])


def _limpa(s):
    return re.sub(r"[ \t]+", " ", str(s or "")).strip()


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _bloco_texto(textos_pag, idxs, limite=40000):
    """Texto extraído (pypdf) das páginas enviadas, como apoio de conferência."""
    partes = []
    for k, i in enumerate(idxs, 1):
        t = (textos_pag[i] or "").strip()
        if t:
            partes.append(f"--- página {k} ---\n{t}")
    if not partes:
        return ""
    corpo = "\n".join(partes)[:limite]
    return f"\nTEXTO EXTRAÍDO DAS PÁGINAS (só para conferir palavras; a ordem pode estar embaralhada):\n<<<\n{corpo}\n>>>\n"


class _Leitor:
    def __init__(self, pdf_bytes, com_gabarito=False):
        import anthropic

        self.cliente = anthropic.Anthropic(max_retries=3, timeout=900)
        self.reader = PdfReader(io.BytesIO(pdf_bytes))
        self.n_pag = len(self.reader.pages)
        self.com_gabarito = com_gabarito
        try:
            self.textos = [p.extract_text() or "" for p in self.reader.pages]
        except Exception:  # noqa: BLE001
            self.textos = [""] * self.n_pag
        self.resultado = {}     # numero -> questão
        self.origem = {}        # numero -> (pag_ini, pag_fim) onde foi lida
        self.apoios = {}        # numero -> texto de apoio (pelas faixas)
        self.inventario = set()
        self.gabarito = {}

    # -- chamada à API ------------------------------------------------------ #
    def _chamar(self, idxs, prompt, max_tokens=MAX_TOKENS):
        dados = base64.standard_b64encode(_pdf_paginas(self.reader, idxs)).decode()
        with self.cliente.messages.stream(
            model=modelo(), max_tokens=max_tokens,
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": dados}},
                {"type": "text", "text": prompt},
            ]}],
        ) as stream:
            msg = stream.get_final_message()
        texto = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return texto, getattr(msg, "stop_reason", "")

    def _chamar_json(self, idxs, prompt, max_tokens=MAX_TOKENS):
        """(dados, cortado). Tenta de novo uma vez se o JSON vier inválido."""
        cortado = False
        for _ in range(2):
            texto, stop = self._chamar(idxs, prompt, max_tokens)
            cortado = stop == "max_tokens"
            try:
                return _json(texto), cortado
            except Exception:  # noqa: BLE001
                if cortado:
                    return {}, True
        return {}, cortado

    # -- leitura de uma parte (com divisão se a resposta for cortada) -------- #
    def ler_parte(self, ini, fim, esperadas=None):
        idxs = ([ini - 1] if ini > 0 else []) + list(range(ini, fim)) + ([fim] if fim < self.n_pag else [])
        p_ini = 2 if ini > 0 else 1
        p_fim = p_ini + (fim - ini) - 1
        ctx = []
        if ini > 0:
            ctx.append("A página 1 é contexto (página anterior)")
        if fim < self.n_pag:
            ctx.append(f"a página {len(idxs)} é contexto (página seguinte)")
        contexto = (" " + "; ".join(ctx) + ".") if ctx else ""
        esp = ""
        if esperadas:
            esp = (f"\nO caderno completo tem as questões de números {min(esperadas)} a {max(esperadas)} "
                   f"({len(esperadas)} questões); transcreva as que começam nestas páginas principais.")
        prompt = PROMPT.format(
            total=len(idxs), ini=p_ini, fim=p_fim, contexto=contexto, esperadas=esp, regras=REGRAS,
            extra_gabarito=EXTRA_GABARITO if self.com_gabarito else "",
            texto_extraido=_bloco_texto(self.textos, idxs),
            formato=FORMATO.format(campo_gabarito=',\n "gabarito": {}' if self.com_gabarito else ""))
        dados, cortado = self._chamar_json(idxs, prompt)
        if cortado:
            if fim - ini > 1:  # divide ao meio e lê cada metade
                meio = (ini + fim) // 2
                self.ler_parte(ini, meio, esperadas)
                self.ler_parte(meio, fim, esperadas)
                return
            dados2, _ = self._chamar_json(idxs, prompt, MAX_TOKENS_GRANDE)
            dados = dados2 or dados
        self.incorporar(dados, (max(0, ini - 1), min(self.n_pag, fim + 1)))

    # -- junta a resposta no resultado --------------------------------------- #
    def incorporar(self, dados, origem, so=None):
        if not dados:
            return
        if not so:
            for n in dados.get("inventario") or []:
                n = _int(n)
                if n and 1 <= n <= 300:
                    self.inventario.add(n)
        for t in dados.get("textos") or []:
            texto = str((t or {}).get("texto") or "").strip()
            faixa = [_int(x) for x in ((t or {}).get("questoes") or [])]
            faixa = [x for x in faixa if x]
            if not texto or not faixa:
                continue
            a, b = min(faixa), max(faixa)
            if b - a > 40:
                continue
            for q in range(a, b + 1):
                if len(texto) > len(self.apoios.get(q, "")):
                    self.apoios[q] = texto
        if self.com_gabarito:
            for num, letra in (dados.get("gabarito") or {}).items():
                num = _int(num)
                if num is None:
                    continue
                letra = str(letra or "").strip().upper()
                letra = "X" if letra.startswith("ANUL") else letra[:1]
                if letra in ("A", "B", "C", "D", "E", "X") and 1 <= num <= 300:
                    self.gabarito.setdefault(num, letra)
        for q in dados.get("questoes") or []:
            n = _int((q or {}).get("numero"))
            if n is None or not 1 <= n <= 300 or (so and n not in so):
                continue
            alts = {str(l).strip().upper()[:1]: _limpa(t) for l, t in (q.get("alternativas") or {}).items()
                    if str(l).strip().upper()[:1] in "ABCDE" and _limpa(t)}
            enun = str(q.get("enunciado") or "").strip()
            fig = str(q.get("figura") or "").strip()
            if fig:
                enun = (enun + "\n\n[Figura/tabela] " + fig).strip()
            novo = {"fonte": _limpa(q.get("fonte"))[:300], "apoio": str(q.get("apoio") or "").strip(),
                    "enunciado": enun, "alternativas": dict(sorted(alts.items())),
                    "ok": len(alts) >= 4 and bool(enun)}
            antigo = self.resultado.get(n)
            if antigo is None or self._melhor(novo, antigo):
                self.resultado[n] = novo
                self.origem[n] = origem

    @staticmethod
    def _peso(q):
        return (q["ok"], len(q["alternativas"]), len(q["enunciado"]) + len(q["apoio"]))

    def _melhor(self, novo, antigo):
        return self._peso(novo) > self._peso(antigo)

    # -- conferência ---------------------------------------------------------- #
    def final(self):
        """Resultado com os textos de apoio das faixas aplicados."""
        res = {}
        for n, q in self.resultado.items():
            q = dict(q)
            faixa = self.apoios.get(n, "")
            if faixa and faixa not in q["apoio"]:
                q["apoio"] = (faixa + ("\n\n" + q["apoio"] if q["apoio"] else "")).strip()
            res[n] = q
        return dict(sorted(res.items()))

    def problemas(self, esperadas=None):
        """{numero: motivo} do que precisa ser relido."""
        res = self.final()
        alvo = set(esperadas or []) | self.inventario
        if res and not esperadas:  # sem gabarito: buracos na sequência também contam
            alvo |= set(range(min(res), max(res) + 1))
        prob = {}
        for n in sorted(alvo):
            q = res.get(n)
            if q is None:
                prob[n] = "não foi encontrada/transcrita"
            elif not q["enunciado"]:
                prob[n] = "ficou sem enunciado"
            elif len(q["alternativas"]) < 4:
                faltam = [l for l in "ABCDE" if l not in q["alternativas"]]
                prob[n] = "faltaram alternativas (" + ", ".join(faltam) + ")"
            elif not q["apoio"] and RX_PEDE_APOIO.search(q["enunciado"]):
                prob[n] = "cita um texto de apoio que não foi transcrito"
        return prob

    def _pagina_da_questao(self, n):
        rx = re.compile(rf"(?im)^[ \t]*(?:quest[ãa]o[ \t]*)?0*{n}[ \t]*[\.\)\-–—:]?(?!\d)")
        for i, t in enumerate(self.textos):
            if rx.search(t or ""):
                return i
        return None

    def _paginas_para(self, numeros):
        """Páginas a enviar na releitura de um grupo de questões."""
        pags = set()
        for n in numeros:
            if n in self.origem:
                a, b = self.origem[n]
                pags |= set(range(a, b))
            p = self._pagina_da_questao(n)
            if p is not None:
                pags |= set(range(max(0, p - 2), min(self.n_pag, p + 2)))
            if n not in self.origem and p is None:  # usa vizinhas já lidas
                abaixo = [k for k in self.origem if k < n]
                acima = [k for k in self.origem if k > n]
                if abaixo:
                    pags |= set(range(*self.origem[max(abaixo)]))
                if acima:
                    pags |= set(range(*self.origem[min(acima)]))
        if not pags:
            pags = set(range(self.n_pag))
        pags = sorted(pags)
        if len(pags) > 10:  # mantém a releitura enxuta: janela contínua em volta do centro
            meio = pags[len(pags) // 2]
            pags = [p for p in pags if abs(p - meio) <= 5][:10]
        return pags

    def reler(self, prob):
        """Relê, em grupos pequenos, as questões com problema."""
        grupos, atual = [], []
        for n in sorted(prob):
            if atual and (n - atual[-1] > 3 or len(atual) >= 5):
                grupos.append(atual)
                atual = []
            atual.append(n)
        if atual:
            grupos.append(atual)
        for g in grupos:
            idxs = self._paginas_para(g)
            probl = "\n".join(f"- Questão {n}: {prob[n]}." for n in g)
            prompt = PROMPT_RELEITURA.format(
                total=len(idxs), lista=", ".join(map(str, g)), problemas="\nO que faltou:\n" + probl + "\n",
                regras=REGRAS, texto_extraido=_bloco_texto(self.textos, idxs),
                formato=FORMATO.format(campo_gabarito=""))
            dados, cortado = self._chamar_json(idxs, prompt, MAX_TOKENS_GRANDE if len(g) > 2 else MAX_TOKENS)
            self.incorporar(dados, (idxs[0], idxs[-1] + 1), so=set(g))
            yield g


def extrair(pdf_bytes, progresso=None, com_gabarito=False, esperadas=None):
    """Retorna {numero: {"fonte","apoio","enunciado","alternativas","ok"}} lendo o PDF com o Claude.
    esperadas: números de questão do gabarito (ajudam a conferir se alguma ficou para trás).
    Com com_gabarito=True, retorna (questoes, gabarito) — o gabarito só é transcrito se estiver escrito no PDF."""
    lt = _Leitor(pdf_bytes, com_gabarito)
    esperadas = sorted(set(esperadas or []))
    passo = max(1, int(os.environ.get("IA_PAGINAS_POR_PARTE", "4")))
    partes = list(range(0, lt.n_pag, passo))
    for k, ini in enumerate(partes):
        if progresso:
            progresso(k + 1, len(partes))
        lt.ler_parte(ini, min(lt.n_pag, ini + passo), esperadas)

    esp_conf = esperadas or (sorted(lt.gabarito) if com_gabarito else None)
    rodadas = max(0, int(os.environ.get("IA_RODADAS_CONFERENCIA", "2")))
    for r in range(rodadas):
        prob = lt.problemas(esp_conf)
        if not prob:
            break
        if progresso:
            try:
                progresso(f"Conferindo {len(prob)} questão(ões) incompletas (rodada {r + 1})…", None)
            except TypeError:
                pass
        antes = dict(prob)
        for _ in lt.reler(prob):
            pass
        if lt.problemas(esp_conf) == antes:  # nada melhorou: não insiste
            break

    res = lt.final()
    if com_gabarito:
        return res, dict(sorted(lt.gabarito.items()))
    return res
