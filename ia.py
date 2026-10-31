"""Leitura de cadernos de questões com IA (Claude API).

O PDF é enviado em partes de algumas páginas (com uma página de contexto antes e depois) e o
modelo devolve as questões em JSON: fonte, texto de apoio, enunciado, alternativas e figuras.
O gabarito NUNCA é pedido nem guardado a partir da IA (ele vem do gabarito do caderno).

Configuração (variáveis de ambiente no Railway):
  ANTHROPIC_API_KEY   chave da API (obrigatória para ativar a IA)
  ANTHROPIC_MODEL     modelo (padrão: claude-sonnet-5-5)
  IA_PAGINAS_POR_PARTE páginas por chamada (padrão: 6)
"""
import base64
import io
import json
import os
import re

from pypdf import PdfReader, PdfWriter

MODELO_PADRAO = "claude-sonnet-5-5"

PROMPT = """Você está transcrevendo um caderno de questões de concurso (banca VUNESP, concurso de Escrevente do TJSP) para um aplicativo de estudos.

Este PDF contém {total} página(s). As páginas PRINCIPAIS são as páginas {ini} a {fim} deste PDF.{contexto}

Transcreva TODAS as questões de múltipla escolha cujo número/início aparece nas páginas principais. Se o final de uma questão continuar numa página de contexto seguinte, use-a para completar. Não transcreva questões que começam apenas nas páginas de contexto.

Regras:
- Transcreva fielmente, em português, sem resumir nem corrigir. Mantenha quebras de parágrafo com \\n.
- "numero": o número da questão como aparece no caderno (inteiro).
- "fonte": o cabeçalho da questão, se houver (ex.: "VUNESP - 2023 - TJ-SP - Escrevente Técnico Judiciário"); senão "".
- "apoio": o texto de apoio (texto, poema, tirinha transcrita, tabela) que a questão usa, COMPLETO, repetido em cada questão que depende dele. Se o texto de apoio estiver numa página de contexto anterior, transcreva-o também. Se não houver, "".
- "enunciado": o comando da questão, sem o número e sem a fonte.
- "alternativas": objeto com as letras como chaves ("A" a "E") e o texto de cada alternativa, sem a letra.
- "figura": se a questão tiver imagem, gráfico, tabela ou esquema necessário para respondê-la, descreva-o objetivamente (tabelas: transcreva em linhas de texto); senão "".
- IGNORE gabaritos, folhas de resposta, comentários, resoluções, marcações de alternativa correta, cabeçalhos/rodapés de site e propagandas. NUNCA indique qual alternativa é a correta.

Responda SOMENTE com JSON válido, sem texto antes ou depois, no formato:
{{"questoes": [{{"numero": 1, "fonte": "", "apoio": "", "enunciado": "", "alternativas": {{"A": "", "B": "", "C": "", "D": "", "E": ""}}, "figura": ""}}]}}"""


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


def extrair(pdf_bytes, progresso=None):
    """Retorna {numero: {"fonte","apoio","enunciado","alternativas","ok"}} lendo o PDF com o Claude."""
    import anthropic

    cliente = anthropic.Anthropic(max_retries=3, timeout=600)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    n_pag = len(reader.pages)
    passo = max(1, int(os.environ.get("IA_PAGINAS_POR_PARTE", "6")))
    partes = list(range(0, n_pag, passo))
    resultado = {}
    for k, ini in enumerate(partes):
        if progresso:
            progresso(k + 1, len(partes))
        fim = min(n_pag, ini + passo)
        idxs = ([ini - 1] if ini > 0 else []) + list(range(ini, fim)) + ([fim] if fim < n_pag else [])
        p_ini = 2 if ini > 0 else 1
        p_fim = p_ini + (fim - ini) - 1
        ctx = []
        if ini > 0:
            ctx.append("A página 1 é contexto (página anterior)")
        if fim < n_pag:
            ctx.append(f"a página {len(idxs)} é contexto (página seguinte)")
        contexto = (" " + "; ".join(ctx) + ".") if ctx else ""
        dados = base64.standard_b64encode(_pdf_paginas(reader, idxs)).decode()
        prompt = PROMPT.format(total=len(idxs), ini=p_ini, fim=p_fim, contexto=contexto)
        with cliente.messages.stream(
            model=modelo(), max_tokens=32000,
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": dados}},
                {"type": "text", "text": prompt},
            ]}],
        ) as stream:
            msg = stream.get_final_message()
        texto = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        try:
            lista = _json(texto).get("questoes", [])
        except Exception:
            lista = []
        for q in lista:
            try:
                n = int(q.get("numero"))
            except (TypeError, ValueError):
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
            antigo = resultado.get(n)
            if antigo is None or (novo["ok"] and not antigo["ok"]) or (
                    novo["ok"] == antigo["ok"] and len(novo["enunciado"]) + len(novo["apoio"]) > len(antigo["enunciado"]) + len(antigo["apoio"])):
                resultado[n] = novo
    return resultado
