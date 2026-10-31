"""
DUOTATTI — plataforma de estudos para o concurso de Escrevente Técnico Judiciário do TJSP.
Flask + SQLAlchemy (Postgres no Railway / SQLite local).
"""
import io
import math
import json
import os
import random
import re
import time as _time
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from jinja2 import ChoiceLoader, FileSystemLoader
from pypdf import PdfReader
from sqlalchemy.orm import deferred
from werkzeug.utils import secure_filename

from edital import EDITAL, TODO, chave_legada

TZ = ZoneInfo("America/Sao_Paulo")
LETRAS = "ABCDE"


def agora():
    return datetime.now(TZ).replace(tzinfo=None, microsecond=0)


def hoje():
    return agora().date()


# --------------------------------------------------------------------------- #
# App / banco
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"),
            static_folder=os.path.join(BASE_DIR, "static"))
# procura os .html em templates/ e, se não achar, na raiz do projeto
app.jinja_env.loader = ChoiceLoader([FileSystemLoader(os.path.join(BASE_DIR, "templates")),
                                     FileSystemLoader(BASE_DIR)])
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "duotatti-dev-key")
db_url = os.environ.get("DATABASE_URL", "sqlite:///duotatti.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "80")) * 1024 * 1024
db = SQLAlchemy(app)


class Materia(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), unique=True, nullable=False)
    info = db.Column(db.String(200))
    questoes_prova = db.Column(db.Integer)
    cor = db.Column(db.String(9), default="#3fbf74")
    ordem = db.Column(db.Integer, default=0)
    topicos = db.relationship("Topico", backref="materia", order_by="Topico.ordem",
                              cascade="all, delete-orphan", lazy="selectin")
    aulas = db.relationship("Aula", backref="materia", order_by="(Aula.ordem, Aula.id)",
                            cascade="all, delete-orphan", lazy="selectin")
    cadernos = db.relationship("Caderno", backref="materia", order_by="Caderno.criado_em",
                               cascade="all, delete-orphan", lazy="selectin")


class Topico(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    materia_id = db.Column(db.Integer, db.ForeignKey("materia.id"), nullable=False)
    nome = db.Column(db.String(200), nullable=False)
    ordem = db.Column(db.Integer, default=0)
    personalizado = db.Column(db.Boolean, default=False)  # True = assunto criado por você (não veio do edital)
    grupo = db.Column(db.String(200))  # item do edital (macrotópico) ao qual o assunto pertence

    @property
    def rotulo(self):
        """Texto para exibir fora da lista agrupada."""
        if self.nome == TODO and self.grupo:
            return f"{self.grupo} (todo o item)"
        return self.nome


class Aula(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    materia_id = db.Column(db.Integer, db.ForeignKey("materia.id"), nullable=False)
    topico_id = db.Column(db.Integer, db.ForeignKey("topico.id"))
    titulo = db.Column(db.String(200), nullable=False)
    arquivo_nome = db.Column(db.String(200))
    pdf = deferred(db.Column(db.LargeBinary))
    paginas = db.Column(db.Integer)
    tamanho = db.Column(db.Integer)
    criada_em = db.Column(db.DateTime, default=agora)
    ordem = db.Column(db.Integer)  # posição na lista da matéria (o usuário reordena)
    topico = db.relationship("Topico")
    leituras = db.relationship("Leitura", backref="aula", order_by="Leitura.lida_em",
                               cascade="all, delete-orphan", lazy="selectin")


class Leitura(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    aula_id = db.Column(db.Integer, db.ForeignKey("aula.id"), nullable=False)
    numero = db.Column(db.Integer, nullable=False)  # 1ª, 2ª, 3ª leitura...
    lida_em = db.Column(db.DateTime, default=agora, nullable=False)


class Caderno(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    materia_id = db.Column(db.Integer, db.ForeignKey("materia.id"), nullable=False)
    titulo = db.Column(db.String(200), nullable=False)
    arquivo_nome = db.Column(db.String(200))
    pdf_questoes = deferred(db.Column(db.LargeBinary))
    pdf_gabarito = deferred(db.Column(db.LargeBinary))
    tem_pdf_gabarito = db.Column(db.Boolean, default=False)
    assunto = db.Column(db.String(200))  # assunto das questões (ex.: Crase)
    topico_id = db.Column(db.Integer, db.ForeignKey("topico.id"))
    topico = db.relationship("Topico")
    gabarito_json = db.Column(db.Text, default="{}")
    criado_em = db.Column(db.DateTime, default=agora)
    tentativas = db.relationship("Tentativa", backref="caderno", order_by="Tentativa.feita_em",
                                 cascade="all, delete-orphan", lazy="selectin")

    @property
    def gabarito(self):
        try:
            return {int(k): v for k, v in sorted(json.loads(self.gabarito_json or "{}").items(),
                                                  key=lambda kv: int(kv[0]))}
        except Exception:
            return {}

    @gabarito.setter
    def gabarito(self, g):
        self.gabarito_json = json.dumps({str(k): v for k, v in sorted(g.items())})

    @property
    def total(self):
        return len(self.gabarito)


class Tentativa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    caderno_id = db.Column(db.Integer, db.ForeignKey("caderno.id"), nullable=False)
    materia_id = db.Column(db.Integer, db.ForeignKey("materia.id"), nullable=False)
    feita_em = db.Column(db.DateTime, default=agora, nullable=False)
    acertos = db.Column(db.Integer, default=0)
    respondidas = db.Column(db.Integer, default=0)
    total = db.Column(db.Integer, default=0)
    xp = db.Column(db.Integer, default=0)
    respostas_json = db.Column(db.Text, default="{}")
    materia = db.relationship("Materia")

    @property
    def percentual(self):
        return round(100 * self.acertos / self.respondidas, 1) if self.respondidas else 0.0

    @property
    def respostas(self):
        return {int(k): v for k, v in json.loads(self.respostas_json or "{}").items()}


class Config(db.Model):
    chave = db.Column(db.String(50), primary_key=True)
    valor = db.Column(db.String(200))


DEFAULTS = {
    "nome": "Estudante",
    "data_prova": "2027-03-14",
    "meta_acerto": "80",
    "meta_leituras_por_aula": "5",      # cada aula deve ser lida 5x
    "data_limite_leituras": "2027-03-10",  # depois disso: só questões e descanso
    "max_dias_sem_ver": "10",           # nenhuma aula (já iniciada) deve ficar mais que isso sem leitura
    "meta_questoes_dia": "10",
}


def configs():
    cfg = dict(DEFAULTS)
    for c in Config.query.all():
        cfg[c.chave] = c.valor
    return cfg


def cfg_num(cfg, chave):
    try:
        return float(cfg[chave])
    except (KeyError, ValueError, TypeError):
        return float(DEFAULTS[chave])


# --------------------------------------------------------------------------- #
# Matérias do edital (TJSP Escrevente — Edital 02/2026)
# --------------------------------------------------------------------------- #
MATERIAS = [  # nome, questões na prova, descrição, cor  (tópicos ficam em edital.py)
    ("Língua Portuguesa", 16, "Bloco I · eliminatória · mínimo 8 acertos", "#3d8bfd"),
    ("Direito Penal", None, "Bloco II · Conhecimentos em Direito (30 questões)", "#e5484d"),
    ("Direito Processual Penal", None, "Bloco II · CPP e Lei 9.099/95", "#d6409f"),
    ("Direito Processual Civil", None, "Bloco II · CPC e Juizados", "#8e4ec6"),
    ("Direito Constitucional", None, "Bloco II · Constituição Federal", "#e8a317"),
    ("Direito Administrativo", None, "Bloco II · Estatuto de SP e improbidade", "#12a594"),
    ("Legislação interna do TJSP", None, "Bloco II · resoluções, regimento e normas", "#0b8fd6"),
    ("Conhecimentos Gerais", 24, "Bloco III · atualidades, matemática, informática e lógica", "#46a758"),
]


def topicos_desejados(nome_materia):
    return [(g, a) for g, assuntos in EDITAL.get(nome_materia, []) for a in assuntos]


def seed():
    if Materia.query.count():
        return
    for i, (nome, q, info, cor) in enumerate(MATERIAS):
        m = Materia(nome=nome, questoes_prova=q, info=info, cor=cor, ordem=i)
        m.topicos = [Topico(nome=a, grupo=g, ordem=k, personalizado=False)
                     for k, (g, a) in enumerate(topicos_desejados(nome))]
        db.session.add(m)
    db.session.commit()


NOVAS_COLUNAS = [("caderno", "assunto", "VARCHAR(200)"), ("topico", "personalizado", "BOOLEAN DEFAULT FALSE"), ("caderno", "topico_id", "INTEGER"), ("topico", "grupo", "VARCHAR(200)"), ("aula", "ordem", "INTEGER")]


def migrar(conn):
    """Adiciona colunas criadas depois do primeiro deploy (create_all não altera tabelas)."""
    from sqlalchemy import inspect
    insp = inspect(conn)
    for tabela, coluna, tipo in NOVAS_COLUNAS:
        if coluna not in {c["name"] for c in insp.get_columns(tabela)}:
            conn.exec_driver_sql(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")


def definir_topico_caderno(c, topico_id=None, novo=None):
    """Vincula o caderno a um item do edital/assunto (ou cria um assunto novo)."""
    t = None
    if novo and novo.strip():
        t = garantir_topico(c.materia_id, novo)
    elif topico_id not in (None, ""):
        try:
            tid = int(topico_id)
        except (TypeError, ValueError):
            tid = None
        t = db.session.get(Topico, tid) if tid else None
        if t and t.materia_id != c.materia_id:
            t = None
    c.topico_id = t.id if t else None
    c.assunto = t.rotulo if t else None
    return t


def fundir_topico(origem, destino):
    """Passa aulas e cadernos do tópico 'origem' para 'destino' e apaga 'origem'."""
    Aula.query.filter_by(topico_id=origem.id).update({"topico_id": destino.id}, synchronize_session=False)
    Caderno.query.filter_by(topico_id=origem.id).update({"topico_id": destino.id}, synchronize_session=False)
    db.session.delete(origem)


def atualizar_topicos_edital():
    """Sincroniza os tópicos com edital.py (item -> assuntos), fundindo nomes antigos sem perder vínculos."""
    for m in Materia.query.all():
        desejados = topicos_desejados(m.nome)
        if not desejados:
            continue
        meta = next((x for x in MATERIAS if x[0] == m.nome), None)
        if meta and m.info != meta[2]:
            m.info = meta[2]
        alvo = set(desejados)
        edital = [t for t in Topico.query.filter_by(materia_id=m.id).order_by(Topico.id).all()
                  if not t.personalizado]
        edital.sort(key=lambda t: ((t.grupo, t.nome) not in alvo, t.id))  # os já atuais viram destino
        por_chave = {}
        for t in edital:
            chave = (t.grupo, t.nome) if (t.grupo, t.nome) in alvo else chave_legada(m.nome, t.nome)
            if chave is None:  # tópico antigo sem correspondente
                tem_vinculo = (Aula.query.filter_by(topico_id=t.id).count()
                               or Caderno.query.filter_by(topico_id=t.id).count())
                if tem_vinculo:
                    t.grupo = "Outros (versão anterior)"
                else:
                    db.session.delete(t)
                continue
            if chave in por_chave:
                fundir_topico(t, por_chave[chave])
            else:
                t.grupo, t.nome = chave
                por_chave[chave] = t
        db.session.flush()
        for k, chave in enumerate(desejados):
            t = por_chave.get(chave)
            if t is None:
                t = Topico(materia_id=m.id, grupo=chave[0], nome=chave[1], personalizado=False)
                db.session.add(t)
            t.ordem = k
    db.session.flush()
    for c in Caderno.query.filter(Caderno.topico_id.isnot(None)).all():
        t = db.session.get(Topico, c.topico_id)
        if t and c.assunto != t.rotulo:
            c.assunto = t.rotulo
    db.session.commit()


def garantir_topico(materia_id, nome):
    """Devolve o tópico com esse nome na matéria; se não existir, cria como assunto personalizado."""
    nome = (nome or "").strip()[:200]
    if not nome:
        return None
    for t in Topico.query.filter_by(materia_id=materia_id).all():
        if t.nome.strip().lower() == nome.lower() or t.rotulo.strip().lower() == nome.lower():
            return t
    ultimo = db.session.query(db.func.max(Topico.ordem)).filter_by(materia_id=materia_id).scalar() or 0
    t = Topico(materia_id=materia_id, nome=nome, ordem=max(ultimo, 999) + 1, personalizado=True)
    db.session.add(t)
    db.session.flush()
    return t


def assuntos_para_topicos():
    """Assuntos de cadernos já cadastrados viram tópicos (para bancos antigos)."""
    for c in Caderno.query.filter(Caderno.assunto.isnot(None)).all():
        t = garantir_topico(c.materia_id, c.assunto)
        if t and not c.topico_id:
            c.topico_id = t.id
    db.session.commit()


def numerar_aulas():
    """Aulas sem posição (bancos antigos) entram no fim, na ordem de envio."""
    for m in Materia.query.all():
        sem = Aula.query.filter_by(materia_id=m.id, ordem=None).order_by(Aula.criada_em, Aula.id).all()
        if not sem:
            continue
        prox = (db.session.query(db.func.max(Aula.ordem)).filter_by(materia_id=m.id).scalar() or 0) + 1
        for a in sem:
            a.ordem = prox
            prox += 1
    db.session.commit()


def _preparar_banco():
    if db.engine.dialect.name == "postgresql":
        with db.engine.begin() as conn:
            db.metadata.create_all(bind=conn, checkfirst=True)
            migrar(conn)
    else:
        db.create_all()
        with db.engine.begin() as conn:
            migrar(conn)
    seed()
    atualizar_topicos_edital()
    assuntos_para_topicos()
    numerar_aulas()


def init_db():
    """Cria/atualiza tabelas e tópicos. Em Postgres roda com trava: um processo por vez."""
    with app.app_context():
        for tentativa in range(6):
            trava = None
            try:
                if db.engine.dialect.name == "postgresql":
                    trava = db.engine.connect()
                    trava.exec_driver_sql("SELECT pg_advisory_lock(424242)")
                _preparar_banco()
                return
            except Exception as e:
                db.session.rollback()
                app.logger.warning("init_db tentativa %s: %s: %s", tentativa + 1, e.__class__.__name__, e)
                _time.sleep(1 + tentativa)
            finally:
                if trava is not None:
                    try:
                        trava.exec_driver_sql("SELECT pg_advisory_unlock(424242)")
                    finally:
                        trava.close()
        app.logger.error("init_db não concluiu; tentando seguir assim mesmo")


init_db()


# --------------------------------------------------------------------------- #
# PDF e leitura de gabarito
# --------------------------------------------------------------------------- #
def nome_padrao(arquivo):
    """Nome da aula = nome do arquivo sem a extensão (.pdf, .pdf.pdf...)."""
    n = re.sub(r"(\.pdf)+$", "", os.path.basename(arquivo or "").strip(), flags=re.I).strip()
    return (n or "Aula")[:200]


def eh_pdf(data):
    return bool(data) and data[:1024].lstrip().startswith(b"%PDF")


def contar_paginas(data):
    try:
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return None


def extrair_texto(data):
    try:
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return ""


RX_PAR = re.compile(
    r"(?<![\d/.,])(\d{1,3})\s*(?:[-–—.:=)]|ª|º)?\s*\)?\s*"
    r"(?:[Ll]etra\s+|[Aa]lternativa\s+)?"
    r"((?i:anulad[ao])|[A-E]|X)(?![A-Za-zÀ-ú])"
)


def _limpar(g):
    """Mantém a sequência contínua de questões (tolera pequenos buracos)."""
    if not g:
        return {}
    ks = sorted(g)
    out, prev = {}, ks[0] - 1
    if ks[0] > 5:
        return {}
    for k in ks:
        if k > prev + 3:
            break
        out[k] = g[k]
        prev = k
    return out


def _norm_letra(l):
    l = l.strip().upper()
    return "X" if l.startswith("ANULAD") or l == "*" else l


def _pares(t):
    g = {}
    for m in RX_PAR.finditer(t):
        n = int(m.group(1))
        if 1 <= n <= 300 and n not in g:
            g[n] = _norm_letra(m.group(2))
    return _limpar(g)


def _tabular(t):
    linhas = [l.split() for l in t.split("\n") if l.strip()]
    g = {}
    for a, b in zip(linhas, linhas[1:]):
        if (len(a) >= 3 and len(a) == len(b) and all(x.isdigit() for x in a)
                and all(re.fullmatch(r"[A-EX*]|(?i:anulad[ao])", x) for x in b)):
            for n, l in zip(a, b):
                g.setdefault(int(n), _norm_letra(l))
    return _limpar(g)


def parse_gabarito_texto(txt, exigir_secao=False, manual=False):
    """Aceita: '1-A 2-C', '01) B', 'Questão 3: D', tabelas (números / letras),
    ou uma sequência pura 'ACDBE...'. 'Anulada' vira X (vale para todos)."""
    if not txt or not txt.strip():
        return {}
    t = txt.replace("\r", "\n")
    if manual:
        t = t.upper()
    if not exigir_secao:
        compacto = re.sub(r"[\s,;.\-]", "", t).upper()
        if compacto and re.fullmatch(r"[A-EX]+", compacto):
            return {i + 1: c for i, c in enumerate(compacto)}
    pos = [m.start() for m in re.finditer(r"(?i)gabarito", t)]
    if exigir_secao and not pos:
        return {}
    blocos = ([t[pos[-1]:]] if pos else []) + ([] if exigir_secao else [t])
    for bloco in blocos:
        a, b = _pares(bloco), _tabular(bloco)
        melhor = a if len(a) >= len(b) else b
        if len(melhor) >= (1 if manual else 3):
            return melhor
    return {}


# --------------------------------------------------------------------------- #
# Estatísticas, XP, ofensiva e humor do Tatti
# --------------------------------------------------------------------------- #
def inicio_semana(d=None):
    d = d or hoje()
    return d - timedelta(days=d.weekday())


def dt0(d):
    return datetime.combine(d, time.min)


def dias_atividade():
    dias = {r[0].date() for r in db.session.query(Leitura.lida_em).all()}
    dias |= {r[0].date() for r in db.session.query(Tentativa.feita_em).all()}
    return dias


def ofensiva(dias=None):
    dias = dias if dias is not None else dias_atividade()
    d = hoje()
    if d not in dias:
        d -= timedelta(days=1)  # a ofensiva continua viva até o fim do dia
    n = 0
    while d in dias:
        n += 1
        d -= timedelta(days=1)
    return n


def xp_total():
    xp = 0
    for (n,) in db.session.query(Leitura.numero).all():
        xp += 20 if n == 1 else 10
    xp += sum(x or 0 for (x,) in db.session.query(Tentativa.xp).all())
    return xp


def nivel(xp):
    return {"nivel": xp // 250 + 1, "no_nivel": xp % 250, "por_nivel": 250}


FALAS = {
    "radiante": ["Isso é ritmo de aprovado! Continue assim.",
                 "Você está voando! O cargo está ficando com o seu nome.",
                 "Meta batida. Estou orgulhoso de você!"],
    "feliz": ["Bom trabalho! Falta pouco para fechar as metas.",
              "Estamos no caminho certo. Mais um empurrão!",
              "Gostei de ver. Não pare agora."],
    "neutro": ["Dá para melhorar. Que tal uma aula agora?",
               "Hoje é um bom dia para resolver umas questões.",
               "Estou de olho. Bora estudar?"],
    "bravo": ["Hmm… as metas estão escapando. Vamos reagir?",
              "Assim não dá! Cadê o estudo de hoje?",
              "O edital não vai se estudar sozinho."],
    "furioso": ["VOCÊ SUMIU! A prova não vai esperar.",
                "Estou furioso! Volte agora para os estudos.",
                "Três dias sem estudar?! Abra um PDF já."],
}
TITULOS = {"radiante": "Tatti está radiante", "feliz": "Tatti está feliz",
           "neutro": "Tatti está de olho", "bravo": "Tatti está bravo",
           "furioso": "Tatti está furioso"}


def humor_por_pct(pct, meta):
    if pct >= meta:
        return "radiante"
    if pct >= meta - 10:
        return "feliz"
    if pct >= meta - 20:
        return "neutro"
    if pct >= 50:
        return "bravo"
    return "furioso"


def _data_cfg(cfg, chave):
    try:
        return date.fromisoformat(cfg[chave])
    except (KeyError, ValueError, TypeError):
        return date.fromisoformat(DEFAULTS[chave])


def plano_do_dia():
    """Meta diária de leituras (5x por aula até a data limite) e a fila de aulas de hoje.

    - meta de hoje = leituras que faltavam no início do dia / dias até a data limite (inclusive hoje)
    - fila: aula da matéria há mais tempo sem ser vista; aulas já iniciadas perto de
      completar o máximo de dias sem leitura passam na frente."""
    cfg = configs()
    alvo = max(1, int(cfg_num(cfg, "meta_leituras_por_aula")))
    max_dias = max(1, int(cfg_num(cfg, "max_dias_sem_ver")))
    limite = _data_cfg(cfg, "data_limite_leituras")
    h = hoje()
    ini = dt0(h)
    aulas = Aula.query.join(Materia).order_by(Materia.ordem, Aula.ordem, Aula.id).all()

    est = {}
    for a in aulas:
        datas = [l.lida_em for l in a.leituras]
        antes = [d for d in datas if d < ini]
        est[a.id] = {"n": len(datas), "n_antes": len(antes), "ultima": max(datas) if datas else None,
                     "hoje": len(datas) - len(antes)}
    restante = sum(max(0, alvo - e["n"]) for e in est.values())
    restante_inicio = sum(max(0, alvo - e["n_antes"]) for e in est.values())
    feitas_hoje = restante_inicio - restante  # leituras de hoje que contaram para a meta
    dias = (limite - h).days + 1
    fase = "leitura" if dias > 0 else "questoes"
    meta_hoje = math.ceil(restante_inicio / dias) if fase == "leitura" and restante_inicio else 0

    def dias_sem(a):
        u = est[a.id]["ultima"]
        return (h - u.date()).days if u else None

    atrasadas = [a for a in aulas if 0 < est[a.id]["n"] < alvo and dias_sem(a) is not None and dias_sem(a) > max_dias]
    atrasadas.sort(key=lambda a: -dias_sem(a))

    # última vez que cada matéria foi vista
    ultima_mat = {}
    for a in aulas:
        u = est[a.id]["ultima"]
        if u and (a.materia_id not in ultima_mat or u > ultima_mat[a.materia_id]):
            ultima_mat[a.materia_id] = u

    fila = []
    if fase == "leitura":
        cands = [a for a in aulas if est[a.id]["n"] < alvo and est[a.id]["hoje"] == 0]
        vagas = max(0, meta_hoje - feitas_hoje)
        marca = agora()
        planejadas = set()
        while cands:
            urgentes = [a for a in cands if est[a.id]["n"] > 0 and dias_sem(a) >= max_dias - 2]
            if len(fila) >= vagas and not urgentes:
                break  # meta cumprida; aulas perto do limite de dias entram mesmo assim
            if urgentes:
                a = max(urgentes, key=lambda x: (dias_sem(x), -est[x.id]["n"]))
                d = dias_sem(a)
                motivo = f"sem leitura há {d} dias" + (" — passou do limite!" if d > max_dias else "")
                urg = True
            else:
                mid = min({x.materia_id for x in cands}, key=lambda m: ultima_mat.get(m) or datetime.min)
                a = min((x for x in cands if x.materia_id == mid),
                        key=lambda x: (est[x.id]["n"], est[x.id]["ultima"] or datetime.min, x.ordem or 0, x.id))
                um = ultima_mat.get(mid)
                if mid in planejadas:
                    motivo = "rodízio entre as matérias"
                elif um is None:
                    motivo = "matéria ainda não estudada"
                else:
                    d = (h - um.date()).days
                    motivo = "matéria estudada hoje" if d == 0 else f"matéria parada há {d} dia{'s' if d > 1 else ''}"
                urg = False
            fila.append({"aula": a, "vez": est[a.id]["n"] + 1, "motivo": motivo, "urgente": urg})
            cands.remove(a)
            marca += timedelta(seconds=1)
            ultima_mat[a.materia_id] = marca  # próxima sugestão vai para outra matéria
            planejadas.add(a.materia_id)

    total_necessario = alvo * len(aulas)
    return {"alvo": alvo, "limite": limite, "dias": max(dias, 0), "fase": fase, "max_dias": max_dias,
            "restante": restante, "meta_hoje": meta_hoje,
            "aulas_faltando": sum(1 for e in est.values() if e["n"] < alvo), "feitas_hoje": feitas_hoje, "fila": fila,
            "atrasadas": atrasadas, "est": est, "total_aulas": len(aulas),
            "total_necessario": total_necessario, "total_feito": total_necessario - restante}


def resumo_dia():
    cfg = configs()
    h = hoje()
    tents_hoje = Tentativa.query.filter(Tentativa.feita_em >= dt0(h)).all()
    rec = Tentativa.query.filter(Tentativa.feita_em >= dt0(h - timedelta(days=6))).all()
    r_resp = sum(t.respondidas for t in rec)
    return {
        "plano": plano_do_dia(),
        "questoes": sum(t.respondidas for t in tents_hoje),
        "meta_questoes": int(cfg_num(cfg, "meta_questoes_dia")),
        "acerto7": round(100 * sum(t.acertos for t in rec) / r_resp, 1) if r_resp else None,
        "meta_acerto": cfg_num(cfg, "meta_acerto"),
    }


def calcular_humor():
    s = resumo_dia()
    p = s["plano"]
    dias = dias_atividade()
    h = hoje()
    if not dias:
        return {"humor": "neutro", "titulo": "Oi! Eu sou o Tatti",
                "fala": "Vou acompanhar seus estudos. Bata a meta do dia e eu fico feliz; "
                        "suma e eu fico bravo. Comece enviando o PDF da primeira aula!",
                "motivos": [], "dia": s}
    motivos, pts = [], 0
    sem = (h - max(dias)).days
    if sem == 0:
        pts += 2
        motivos.append((True, "Você estudou hoje"))
    elif sem == 1:
        motivos.append((False, "Ainda não estudou hoje"))
    else:
        pts -= 3
        motivos.append((False, f"{sem} dias sem estudar"))

    if p["fase"] == "leitura" and p["total_aulas"]:
        if p["meta_hoje"] == 0:
            pts += 3
            motivos.append((True, "Todas as aulas já foram lidas 5x"))
        elif p["feitas_hoje"] >= p["meta_hoje"]:
            pts += 3
            motivos.append((True, f"Meta de leituras de hoje batida ({p['feitas_hoje']}/{p['meta_hoje']})"))
        elif p["feitas_hoje"] * 2 >= p["meta_hoje"]:
            pts += 1
            motivos.append((False, f"{p['feitas_hoje']}/{p['meta_hoje']} leituras hoje — falta pouco"))
        else:
            motivos.append((False, f"{p['feitas_hoje']}/{p['meta_hoje']} leituras hoje"))
        if p["atrasadas"]:
            pts -= 2
            n = len(p["atrasadas"])
            motivos.append((False, f"{n} aula{'s' if n > 1 else ''} há mais de {p['max_dias']} dias sem leitura"))
    elif p["fase"] == "leitura":
        motivos.append((False, "Envie as aulas em PDF para eu montar sua meta diária"))

    meta = s["meta_acerto"]
    if s["acerto7"] is None:
        pts -= 1
        motivos.append((False, "Nenhuma questão resolvida nos últimos 7 dias"))
    elif s["acerto7"] >= meta:
        pts += 3
        motivos.append((True, f"Acerto de {s['acerto7']:.0f}% nos últimos 7 dias (meta {meta:.0f}%)"))
    elif s["acerto7"] >= meta - 15:
        motivos.append((False, f"Acerto de {s['acerto7']:.0f}%: abaixo da meta de {meta:.0f}%"))
    else:
        pts -= 3
        motivos.append((False, f"Acerto de {s['acerto7']:.0f}%: bem abaixo da meta de {meta:.0f}%"))

    if s["meta_questoes"]:
        if s["questoes"] >= s["meta_questoes"]:
            pts += 2
            motivos.append((True, f"Meta de questões de hoje batida ({s['questoes']}/{s['meta_questoes']})"))
        elif s["questoes"]:
            pts += 1
            motivos.append((False, f"{s['questoes']}/{s['meta_questoes']} questões hoje"))

    if sem >= 3:
        humor = "furioso"
    elif pts >= 8:
        humor = "radiante"
    elif pts >= 4:
        humor = "feliz"
    elif pts >= 1:
        humor = "neutro"
    elif pts >= -3:
        humor = "bravo"
    else:
        humor = "furioso"
    return {"humor": humor, "titulo": TITULOS[humor], "fala": random.choice(FALAS[humor]),
            "motivos": motivos, "dia": s}


def stats_materia(m):
    tents = Tentativa.query.filter_by(materia_id=m.id).order_by(Tentativa.feita_em).all()
    resp = sum(t.respondidas for t in tents)
    lidas = sum(1 for a in m.aulas if a.leituras)
    alvo = max(1, int(cfg_num(configs(), "meta_leituras_por_aula")))
    feitas = sum(min(len(a.leituras), alvo) for a in m.aulas)
    lidos_top = {a.topico_id for a in m.aulas if a.leituras and a.topico_id}
    return {
        "aulas": len(m.aulas),
        "aulas_lidas": lidas,
        "leituras": sum(len(a.leituras) for a in m.aulas),
        "pct_aulas": round(100 * lidas / len(m.aulas)) if m.aulas else 0,
        "alvo": alvo,
        "leituras_meta": feitas,
        "aulas_completas": sum(1 for a in m.aulas if len(a.leituras) >= alvo),
        "leituras_total_meta": alvo * len(m.aulas),
        "pct_meta": round(100 * feitas / (alvo * len(m.aulas))) if m.aulas else 0,
        "topicos_lidos": lidos_top,
        "tentativas": len(tents),
        "questoes": resp,
        "acerto": round(100 * sum(t.acertos for t in tents) / resp, 1) if resp else None,
        "ultimo": tents[-1].percentual if tents else None,
    }


def dias_para_prova(cfg):
    try:
        return max((date.fromisoformat(cfg["data_prova"]) - hoje()).days, 0)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Filtros, contexto e senha opcional
# --------------------------------------------------------------------------- #
@app.template_filter("dt")
def f_dt(v, fmt="%d/%m/%Y %H:%M"):
    return v.strftime(fmt) if v else "—"


@app.template_filter("pct")
def f_pct(v):
    return "—" if v is None else f"{v:.0f}%"


@app.template_filter("kb")
def f_kb(v):
    if not v:
        return ""
    return f"{v / 1048576:.1f} MB" if v > 1048576 else f"{v // 1024} KB"


@app.context_processor
def contexto():
    cfg = configs()
    return {"cfg": cfg, "dias_prova": dias_para_prova(cfg), "meta_acerto": cfg_num(cfg, "meta_acerto")}


@app.before_request
def exigir_senha():
    senha = os.environ.get("APP_PASSWORD")
    if not senha or request.endpoint in ("login", "static") or session.get("ok"):
        return None
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("senha") == os.environ.get("APP_PASSWORD"):
            session["ok"] = True
            session.permanent = True
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") else "/")
        flash("Senha incorreta.", "erro")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# Telas
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    materias = Materia.query.order_by(Materia.ordem).all()
    trilha = [(m, stats_materia(m)) for m in materias]
    xp = xp_total()
    tot_aulas = sum(s["aulas"] for _, s in trilha)
    tot_lidas = sum(s["aulas_lidas"] for _, s in trilha)
    dias_sem = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
    h0 = hoje()
    return render_template("index.html", trilha=trilha, h=calcular_humor(), xp=xp, nv=nivel(xp),
                           hoje_str=f"{dias_sem[h0.weekday()]}, {h0.strftime('%d/%m')}",
                           streak=ofensiva(), tot_aulas=tot_aulas, tot_lidas=tot_lidas)


@app.route("/materia/<int:mid>")
def materia(mid):
    m = db.get_or_404(Materia, mid)
    return render_template("materia.html", m=m, s=stats_materia(m))


def _ler_upload(f):
    if not f or not f.filename:
        return None, None
    data = f.read()
    if not eh_pdf(data):
        flash(f"“{f.filename}” não parece ser um PDF válido.", "erro")
        return None, None
    return data, f.filename


@app.post("/materia/<int:mid>/aulas")
def aula_nova(mid):
    m = db.get_or_404(Materia, mid)
    arquivos = [f for f in request.files.getlist("pdfs") if f and f.filename]
    topico_id = request.form.get("topico_id", type=int) or None
    novo = request.form.get("novo_assunto", "").strip()
    if novo:
        topico_id = garantir_topico(m.id, novo).id
    n = 0
    prox = (db.session.query(db.func.max(Aula.ordem)).filter_by(materia_id=m.id).scalar() or 0) + 1
    for f in arquivos:
        data, nome = _ler_upload(f)
        if not data:
            continue
        t = nome_padrao(nome)
        db.session.add(Aula(materia_id=m.id, topico_id=topico_id, titulo=t[:200], arquivo_nome=nome[:200],
                            pdf=data, paginas=contar_paginas(data), tamanho=len(data), ordem=prox))
        prox += 1
        n += 1
    db.session.commit()
    if n:
        flash(f"{n} aula(s) adicionada(s). Agora é ler!", "ok")
    elif not arquivos:
        flash("Escolha ao menos um PDF.", "erro")
    return redirect(url_for("materia", mid=mid) + "#aulas")


@app.route("/aula/<int:aid>")
def aula_ver(aid):
    a = db.get_or_404(Aula, aid)
    return render_template("aula.html", a=a, m=a.materia)


@app.route("/aula/<int:aid>/pdf")
def aula_pdf(aid):
    a = db.get_or_404(Aula, aid)
    if not a.pdf:
        abort(404)
    return send_file(io.BytesIO(a.pdf), mimetype="application/pdf",
                     download_name=secure_filename(a.arquivo_nome or f"aula-{aid}.pdf") or "aula.pdf")


@app.post("/aula/<int:aid>/ler")
def aula_ler(aid):
    a = db.get_or_404(Aula, aid)
    n = len(a.leituras) + 1
    db.session.add(Leitura(aula_id=a.id, numero=n, lida_em=agora()))
    db.session.commit()
    flash(f"{n}ª leitura registrada! +{20 if n == 1 else 10} XP", "ok")
    return redirect(url_for("aula_ver", aid=aid))


@app.post("/aula/<int:aid>/desfazer")
def aula_desfazer(aid):
    a = db.get_or_404(Aula, aid)
    if a.leituras:
        db.session.delete(a.leituras[-1])
        db.session.commit()
        flash("Última leitura removida.", "info")
    return redirect(url_for("aula_ver", aid=aid))


@app.post("/aula/<int:aid>/editar")
def aula_editar(aid):
    a = db.get_or_404(Aula, aid)
    t = request.form.get("titulo", "").strip()
    if t:
        a.titulo = t[:200]
    a.topico_id = request.form.get("topico_id", type=int) or None
    db.session.commit()
    flash("Aula atualizada.", "ok")
    return redirect(url_for("aula_ver", aid=aid))


@app.post("/aula/<int:aid>/excluir")
def aula_excluir(aid):
    a = db.get_or_404(Aula, aid)
    mid = a.materia_id
    db.session.delete(a)
    db.session.commit()
    flash("Aula excluída.", "info")
    return redirect(url_for("materia", mid=mid) + "#aulas")


# ---- Questões ------------------------------------------------------------- #
@app.post("/materia/<int:mid>/cadernos")
def caderno_novo(mid):
    m = db.get_or_404(Materia, mid)
    dq, nome = _ler_upload(request.files.get("pdf_questoes"))
    if not dq:
        flash("Envie o PDF com as questões.", "erro")
        return redirect(url_for("materia", mid=mid) + "#questoes")
    dg, _ = _ler_upload(request.files.get("pdf_gabarito"))
    texto = request.form.get("gabarito_texto", "")

    gab, origem = {}, ""
    if texto.strip():
        gab, origem = parse_gabarito_texto(texto, manual=True), "texto digitado"
    if not gab and dg:
        gab, origem = parse_gabarito_texto(extrair_texto(dg)), "PDF do gabarito"
    if not gab:
        gab, origem = parse_gabarito_texto(extrair_texto(dq), exigir_secao=True), "PDF das questões"

    titulo = request.form.get("titulo", "").strip() or os.path.splitext(nome)[0]
    c = Caderno(materia_id=m.id, titulo=titulo[:200], arquivo_nome=nome[:200], pdf_questoes=dq,
                pdf_gabarito=dg, tem_pdf_gabarito=bool(dg))
    c.gabarito = gab
    db.session.add(c)
    definir_topico_caderno(c, request.form.get("topico_id"), request.form.get("novo_assunto"))
    db.session.commit()
    if gab:
        flash(f"Caderno criado: {len(gab)} questões lidas do {origem}. Confira o gabarito antes de resolver.", "ok")
    else:
        flash("Caderno criado, mas não consegui ler o gabarito automaticamente. Digite-o abaixo.", "erro")
    return redirect(url_for("caderno_gabarito", cid=c.id))


@app.route("/caderno/<int:cid>/pdf/<tipo>")
def caderno_pdf(cid, tipo):
    c = db.get_or_404(Caderno, cid)
    data = c.pdf_questoes if tipo == "questoes" else c.pdf_gabarito if tipo == "gabarito" else None
    if not data:
        abort(404)
    return send_file(io.BytesIO(data), mimetype="application/pdf",
                     download_name=f"caderno-{cid}-{tipo}.pdf")


@app.route("/caderno/<int:cid>/gabarito", methods=["GET", "POST"])
def caderno_gabarito(cid):
    c = db.get_or_404(Caderno, cid)
    if request.method == "POST":
        gab = parse_gabarito_texto(request.form.get("gabarito", ""), manual=True)
        t = request.form.get("titulo", "").strip()
        if t:
            c.titulo = t[:200]
        definir_topico_caderno(c, request.form.get("topico_id"), request.form.get("novo_assunto"))
        if not gab:
            flash("Não entendi o gabarito. Use uma linha por questão, ex.: 1-A", "erro")
        else:
            c.gabarito = gab
            db.session.commit()
            flash(f"Gabarito salvo: {len(gab)} questões.", "ok")
            return redirect(url_for("caderno_resolver", cid=cid))
    texto = "\n".join(f"{n}-{l}" for n, l in c.gabarito.items())
    return render_template("gabarito.html", c=c, m=c.materia, texto=texto)


@app.route("/caderno/<int:cid>", methods=["GET", "POST"])
def caderno_resolver(cid):
    c = db.get_or_404(Caderno, cid)
    gab = c.gabarito
    if not gab:
        flash("Este caderno ainda não tem gabarito.", "erro")
        return redirect(url_for("caderno_gabarito", cid=cid))
    if request.method == "POST":
        resp = {n: request.form.get(f"q{n}") for n in gab}
        resp = {n: v for n, v in resp.items() if v in LETRAS}
        if not resp:
            flash("Marque ao menos uma resposta.", "erro")
            return redirect(url_for("caderno_resolver", cid=cid))
        acertos = sum(1 for n, v in resp.items() if gab[n] in ("X", v))
        pct = 100 * acertos / len(resp)
        meta = cfg_num(configs(), "meta_acerto")
        xp = acertos + (25 if pct >= meta and len(resp) >= 5 else 0)
        t = Tentativa(caderno_id=c.id, materia_id=c.materia_id, feita_em=agora(), acertos=acertos,
                      respondidas=len(resp), total=len(gab), xp=xp,
                      respostas_json=json.dumps({str(k): v for k, v in resp.items()}))
        db.session.add(t)
        db.session.commit()
        return redirect(url_for("tentativa_ver", tid=t.id))
    return render_template("resolver.html", c=c, m=c.materia, gab=gab, letras=LETRAS)


@app.route("/tentativa/<int:tid>")
def tentativa_ver(tid):
    t = db.get_or_404(Tentativa, tid)
    gab, resp = t.caderno.gabarito, t.respostas
    linhas = [{"n": n, "sua": resp.get(n), "certa": g,
               "ok": (n in resp and g in ("X", resp[n])), "branco": n not in resp}
              for n, g in gab.items()]
    meta = cfg_num(configs(), "meta_acerto")
    anteriores = [x for x in t.caderno.tentativas if x.id != t.id and x.feita_em <= t.feita_em]
    return render_template("resultado.html", t=t, c=t.caderno, m=t.caderno.materia, linhas=linhas,
                           humor=humor_por_pct(t.percentual, meta),
                           anterior=anteriores[-1] if anteriores else None)


@app.post("/tentativa/<int:tid>/excluir")
def tentativa_excluir(tid):
    t = db.get_or_404(Tentativa, tid)
    cid = t.caderno_id
    db.session.delete(t)
    db.session.commit()
    flash("Tentativa removida do histórico.", "info")
    return redirect(url_for("caderno_resolver", cid=cid))


@app.post("/caderno/<int:cid>/excluir")
def caderno_excluir(cid):
    c = db.get_or_404(Caderno, cid)
    mid = c.materia_id
    db.session.delete(c)
    db.session.commit()
    flash("Caderno excluído.", "info")
    return redirect(url_for("materia", mid=mid) + "#questoes")


# ---- Histórico e metas ---------------------------------------------------- #
@app.post("/api/aula/<int:aid>/leitura")
def api_aula_leitura(aid):
    """Círculo da tela Evolução: acao=ler registra leitura; acao=desfazer remove a última."""
    a = db.get_or_404(Aula, aid)
    acao = (request.get_json(silent=True) or {}).get("acao", "ler")
    if acao == "desmarcar":  # círculo volta a ficar vazio
        for l in list(a.leituras):
            db.session.delete(l)
    elif acao == "desfazer":
        if a.leituras:
            db.session.delete(a.leituras[-1])
    else:
        db.session.add(Leitura(aula_id=a.id, numero=len(a.leituras) + 1, lida_em=agora()))
    db.session.commit()
    db.session.refresh(a)
    s = stats_materia(a.materia)
    return jsonify(ok=True, leituras=len(a.leituras),
                   ultima=a.leituras[-1].lida_em.strftime("%d/%m/%Y %H:%M") if a.leituras else None,
                   aulas_lidas=s["aulas_lidas"], aulas=s["aulas"])


@app.post("/api/aula/<int:aid>/titulo")
def api_aula_titulo(aid):
    """Renomear aula (tela da matéria). Vazio = volta ao nome do arquivo."""
    a = db.get_or_404(Aula, aid)
    t = ((request.get_json(silent=True) or {}).get("titulo") or "").strip()
    a.titulo = t[:200] if t else nome_padrao(a.arquivo_nome)
    db.session.commit()
    return jsonify(ok=True, titulo=a.titulo)


@app.post("/api/aula/<int:aid>/topico")
def api_aula_topico(aid):
    """Item do edital da aula (tela da matéria). Vazio = sem item."""
    a = db.get_or_404(Aula, aid)
    tid = (request.get_json(silent=True) or {}).get("topico_id")
    try:
        tid = int(tid) if tid not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify(ok=False), 400
    if tid is not None and not any(t.id == tid for t in a.materia.topicos):
        return jsonify(ok=False), 400
    a.topico_id = tid
    db.session.commit()
    return jsonify(ok=True, topico_id=tid)


@app.post("/api/materia/<int:mid>/ordem-aulas")
def api_ordem_aulas(mid):
    """Recebe a lista de ids das aulas na nova ordem."""
    m = db.get_or_404(Materia, mid)
    ids = (request.get_json(silent=True) or {}).get("ids") or []
    minhas = {a.id: a for a in m.aulas}
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return jsonify(ok=False), 400
    if set(ids) != set(minhas):
        return jsonify(ok=False, erro="lista incompleta"), 400
    for pos, aid in enumerate(ids, start=1):
        minhas[aid].ordem = pos
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/caderno/<int:cid>/assunto")
def api_caderno_assunto(cid):
    c = db.get_or_404(Caderno, cid)
    d = request.get_json(silent=True) or {}
    if "topico_id" in d or "novo" in d:
        definir_topico_caderno(c, d.get("topico_id"), d.get("novo"))
    else:  # compatibilidade: texto livre
        definir_topico_caderno(c, None, (d.get("assunto") or "").strip() or None)
    db.session.commit()
    return jsonify(ok=True, assunto=c.assunto, topico_id=c.topico_id)


def por_assunto(m):
    """Desempenho nas questões agrupado pelo assunto de cada caderno."""
    grupos = {}
    for c in m.cadernos:
        nome = c.topico.rotulo if c.topico else (c.assunto or "Sem assunto definido")
        g = grupos.setdefault(nome, {"resp": 0, "ac": 0, "tent": 0})
        for t in c.tentativas:
            g["resp"] += t.respondidas
            g["ac"] += t.acertos
            g["tent"] += 1
    linhas = [{"assunto": k, "questoes": v["resp"], "tentativas": v["tent"],
               "acerto": round(100 * v["ac"] / v["resp"], 1) if v["resp"] else None}
              for k, v in grupos.items()]
    return sorted(linhas, key=lambda x: (x["assunto"] == "Sem assunto definido", x["assunto"]))


@app.route("/historico")
def historico():
    materias = Materia.query.order_by(Materia.ordem).all()
    tents = Tentativa.query.order_by(Tentativa.feita_em).all()
    pontos = [{"d": t.feita_em.strftime("%d/%m"), "p": t.percentual, "m": t.materia.nome,
               "cor": t.materia.cor, "c": t.caderno.titulo, "q": t.respondidas} for t in tents]
    semanas = []
    ini = inicio_semana()
    leituras = [r[0] for r in db.session.query(Leitura.lida_em).all()]
    for i in range(11, -1, -1):
        a = ini - timedelta(weeks=i)
        b = a + timedelta(days=7)
        ts = [t for t in tents if a <= t.feita_em.date() < b]
        resp = sum(t.respondidas for t in ts)
        semanas.append({"rot": a.strftime("%d/%m"),
                        "leituras": sum(1 for l in leituras if a <= l.date() < b),
                        "questoes": resp,
                        "acerto": round(100 * sum(t.acertos for t in ts) / resp, 1) if resp else None})
    eventos = ([("leitura", l.lida_em, l) for l in Leitura.query.order_by(Leitura.lida_em.desc()).limit(15)]
               + [("questoes", t.feita_em, t) for t in reversed(tents[-15:])])
    eventos.sort(key=lambda e: e[1], reverse=True)
    return render_template("historico.html", materias=[(m, stats_materia(m), por_assunto(m)) for m in materias],
                           pontos=pontos, semanas=semanas, eventos=eventos[:20])


@app.route("/metas", methods=["GET", "POST"])
def metas():
    if request.method == "POST":
        for k in DEFAULTS:
            v = request.form.get(k, "").strip()
            if not v:
                continue
            if k.startswith("data_"):
                try:
                    date.fromisoformat(v)
                except ValueError:
                    flash("Data inválida.", "erro")
                    continue
            elif k != "nome":
                try:
                    v = str(max(0, min(1000, int(float(v)))))
                except ValueError:
                    continue
            c = db.session.get(Config, k) or Config(chave=k)
            c.valor = v[:200]
            db.session.add(c)
        db.session.commit()
        flash("Metas salvas. O Tatti já está de olho!", "ok")
        return redirect(url_for("metas"))
    return render_template("metas.html", h=calcular_humor(), p=plano_do_dia())


@app.errorhandler(413)
def muito_grande(_):
    flash(f"Arquivo grande demais (limite {app.config['MAX_CONTENT_LENGTH'] // 1048576} MB).", "erro")
    return redirect(request.referrer or url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
