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

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from jinja2 import ChoiceLoader, FileSystemLoader
from pypdf import PdfReader
from sqlalchemy.orm import deferred
from werkzeug.utils import secure_filename

from edital import EDITAL, TODO, chave_legada
import questoes as QZ
import ia as IA
import queue
import threading

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


aula_topico = db.Table(
    "aula_topico",
    db.Column("aula_id", db.Integer, db.ForeignKey("aula.id", ondelete="CASCADE"), primary_key=True),
    db.Column("topico_id", db.Integer, db.ForeignKey("topico.id", ondelete="CASCADE"), primary_key=True),
)


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
    topico = db.relationship("Topico")  # legado: vínculo único (migrado para 'topicos')
    topicos = db.relationship("Topico", secondary=aula_topico, order_by="Topico.ordem", lazy="selectin",
                              backref=db.backref("aulas_vinc", lazy="selectin"))
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
    ia_status = db.Column(db.String(20))   # fila | lendo | ok | erro
    ia_msg = db.Column(db.String(300))
    ia_inicio = db.Column(db.DateTime)
    questoes = db.relationship("Questao", backref="caderno", order_by="Questao.numero",
                               cascade="all, delete-orphan", lazy="select")
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
    finalizada = db.Column(db.Boolean, default=True)  # False = bateria em andamento (questão a questão)
    materia = db.relationship("Materia")

    @property
    def percentual(self):
        return round(100 * self.acertos / self.respondidas, 1) if self.respondidas else 0.0

    @property
    def respostas(self):
        return {int(k): v for k, v in json.loads(self.respostas_json or "{}").items()}


class Questao(db.Model):
    """Questão reconstruída do PDF (sem o gabarito, que fica no Caderno)."""
    id = db.Column(db.Integer, primary_key=True)
    caderno_id = db.Column(db.Integer, db.ForeignKey("caderno.id", ondelete="CASCADE"), nullable=False, index=True)
    numero = db.Column(db.Integer, nullable=False)
    fonte = db.Column(db.String(300))
    apoio = db.Column(db.Text)
    enunciado = db.Column(db.Text)
    alternativas_json = db.Column(db.Text, default="{}")
    ok = db.Column(db.Boolean, default=False)

    @property
    def alternativas(self):
        try:
            return json.loads(self.alternativas_json or "{}")
        except Exception:
            return {}


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
    "meta_questoes_dia": "60",          # bateria diária obrigatória (mínimo 60)
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


NOVAS_COLUNAS = [("caderno", "assunto", "VARCHAR(200)"), ("topico", "personalizado", "BOOLEAN DEFAULT FALSE"), ("caderno", "topico_id", "INTEGER"), ("topico", "grupo", "VARCHAR(200)"), ("aula", "ordem", "INTEGER"), ("tentativa", "finalizada", "BOOLEAN DEFAULT TRUE"), ("caderno", "ia_status", "VARCHAR(20)"), ("caderno", "ia_msg", "VARCHAR(300)"), ("caderno", "ia_inicio", "TIMESTAMP")]


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
    for a in list(origem.aulas_vinc):
        if destino not in a.topicos:
            a.topicos.append(destino)
        a.topicos.remove(origem)
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
                tem_vinculo = (t.aulas_vinc or Aula.query.filter_by(topico_id=t.id).count()
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


def migrar_vinculos_aulas():
    """Vínculo único antigo (aula.topico_id) vira vínculo múltiplo (aula_topico)."""
    for a in Aula.query.filter(Aula.topico_id.isnot(None)).all():
        t = db.session.get(Topico, a.topico_id)
        if t and t not in a.topicos:
            a.topicos.append(t)
        a.topico_id = None
    db.session.commit()


QUESTOES_MINIMO = 60


def ajustar_bateria_minima():
    """A bateria diária é de no mínimo 60 questões (ajusta valores salvos antes)."""
    c = db.session.get(Config, "meta_questoes_dia")
    try:
        if c and int(float(c.valor)) < QUESTOES_MINIMO:
            c.valor = str(QUESTOES_MINIMO)
            db.session.commit()
    except (TypeError, ValueError):
        c.valor = str(QUESTOES_MINIMO)
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
    migrar_vinculos_aulas()
    atualizar_topicos_edital()
    assuntos_para_topicos()
    numerar_aulas()
    ajustar_bateria_minima()


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
# Estatísticas, XP, ofensiva e humor da Tatti
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
                 "Meta batida. Estou orgulhosa de você!"],
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
                "Estou furiosa! Volte agora para os estudos.",
                "Três dias sem estudar?! Abra um PDF já."],
}
TITULOS = {"radiante": "Tatti está radiante", "feliz": "Tatti está feliz",
           "neutro": "Tatti está de olho", "bravo": "Tatti está brava",
           "furioso": "Tatti está furiosa"}


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
            "aulas_faltando": sum(1 for e in est.values() if e["n"] < alvo),
            "ritmo": (restante_inicio / dias) if fase == "leitura" and dias > 0 else 0, "feitas_hoje": feitas_hoje, "fila": fila,
            "atrasadas": atrasadas, "est": est, "total_aulas": len(aulas),
            "total_necessario": total_necessario, "total_feito": total_necessario - restante}


def projecao_leituras():
    """Simula, dia a dia até a data limite, quais aulas serão lidas, com as mesmas regras do plano:
    meta = leituras que faltam ÷ dias restantes; rodízio de matérias; aulas perto do limite de dias primeiro.
    Retorna ({data: [(aula, vez, urgente)]}, {aula_id: data da próxima leitura prevista})."""
    p = plano_do_dia()
    h = hoje()
    alvo, max_dias, limite = p["alvo"], p["max_dias"], p["limite"]
    aulas = Aula.query.join(Materia).order_by(Materia.ordem, Aula.ordem, Aula.id).all()
    por_id = {a.id: a for a in aulas}
    n = {a.id: p["est"][a.id]["n"] for a in aulas}
    ult = {a.id: (p["est"][a.id]["ultima"].date() if p["est"][a.id]["ultima"] else None) for a in aulas}
    seq = 0
    ult_mat = {}
    for a in aulas:
        if ult[a.id] and (a.materia_id not in ult_mat or (ult[a.id], 0) > ult_mat[a.materia_id]):
            ult_mat[a.materia_id] = (ult[a.id], 0)
    dias, proxima = {}, {}
    for f in p["fila"]:  # hoje = fila do plano
        a = f["aula"]
        dias.setdefault(h, []).append((a, f["vez"], f["urgente"]))
        proxima.setdefault(a.id, h)
        n[a.id] += 1
        ult[a.id] = h
        seq += 1
        ult_mat[a.materia_id] = (h, seq)
    d = h + timedelta(days=1)
    while d <= limite:
        restante = sum(max(0, alvo - n[i]) for i in n)
        if not restante:
            break
        meta = math.ceil(restante / ((limite - d).days + 1))
        cands = [i for i in n if n[i] < alvo]
        escolhidas = []
        while cands:
            urg = [i for i in cands if n[i] > 0 and ult[i] and (d - ult[i]).days >= max_dias - 2]
            if len(escolhidas) >= meta and not urg:
                break
            if urg:
                i = max(urg, key=lambda x: ((d - ult[x]).days, -n[x]))
                u = True
            else:
                mid = min({por_id[x].materia_id for x in cands},
                          key=lambda m: ult_mat.get(m) or (date.min, 0))
                i = min((x for x in cands if por_id[x].materia_id == mid),
                        key=lambda x: (n[x], ult[x] or date.min, por_id[x].ordem or 0, x))
                u = False
            escolhidas.append((por_id[i], n[i] + 1, u))
            proxima.setdefault(i, d)
            n[i] += 1
            ult[i] = d
            seq += 1
            ult_mat[por_id[i].materia_id] = (d, seq)
            cands.remove(i)
        if escolhidas:
            dias[d] = escolhidas
        d += timedelta(days=1)
    return dias, proxima


def dados_calendario():
    """JSON do calendário: leituras feitas (passado) e previstas (futuro)."""
    cfg = configs()
    previstas, proxima = projecao_leituras()
    cal = {}
    for l in Leitura.query.order_by(Leitura.lida_em).all():
        a = l.aula
        cal.setdefault(l.lida_em.date().isoformat(), {"f": [], "p": []})["f"].append(
            {"t": a.titulo, "m": a.materia.nome, "c": a.materia.cor, "v": l.numero,
             "h": l.lida_em.strftime("%H:%M"), "id": a.id})
    for d, itens in previstas.items():
        for a, vez, urg in itens:
            cal.setdefault(d.isoformat(), {"f": [], "p": []})["p"].append(
                {"t": a.titulo, "m": a.materia.nome, "c": a.materia.cor, "v": vez, "u": urg, "id": a.id})
    marcos = {_data_cfg(cfg, "data_limite_leituras").isoformat(): "Fim das leituras",
              _data_cfg(cfg, "data_prova").isoformat(): "Prova"}
    return {"dias": cal, "marcos": marcos, "hoje": hoje().isoformat()}, proxima


def bateria_do_dia():
    """Bateria diária de questões, alternando os itens do edital.

    Item sugerido = item do edital (macrotópico) há mais tempo sem questões, preferindo itens
    cujas aulas já foram lidas; o item feito ontem vai para o fim da fila (alternância);
    desempate: matéria há mais tempo sem questões e ordem do edital.
    Se já houve questões hoje, mantém o item do dia."""
    cfg = configs()
    meta = max(QUESTOES_MINIMO, int(cfg_num(cfg, "meta_questoes_dia")))
    h = hoje()
    ini = dt0(h)
    materias = Materia.query.order_by(Materia.ordem).all()
    tents = Tentativa.query.order_by(Tentativa.feita_em).all()
    feitas = sum(t.respondidas for t in tents if t.feita_em >= ini)
    acertos = sum(t.acertos for t in tents if t.feita_em >= ini)

    ultimo_item, ultimo_mat = {}, {}
    for t in tents:
        c = t.caderno
        ultimo_mat[t.materia_id] = t.feita_em
        if c and c.topico and c.topico.grupo:
            ultimo_item[(t.materia_id, c.topico.grupo)] = t.feita_em

    itens = []
    for m in materias:
        grupos = []
        for tp in m.topicos:
            if tp.personalizado or not tp.grupo or tp.grupo.startswith("Outros"):
                continue
            if tp.grupo not in grupos:
                grupos.append(tp.grupo)
        lidos = {tp.grupo for a in m.aulas if a.leituras for tp in a.topicos if tp.grupo}
        for k, gr in enumerate(grupos):
            cads = [c for c in m.cadernos if c.topico and c.topico.grupo == gr and c.total]
            itens.append({"materia": m, "grupo": gr, "chave": (m.id, gr), "ordem": (m.ordem, k),
                          "lido": gr in lidos, "ultimo": ultimo_item.get((m.id, gr)), "cadernos": cads})
    if not itens:
        return {"meta": meta, "feitas": feitas, "acertos": acertos, "item": None, "alternativas": []}

    hoje_item = None
    for t in tents:
        if t.feita_em >= ini and t.caderno and t.caderno.topico and t.caderno.topico.grupo:
            hoje_item = (t.materia_id, t.caderno.topico.grupo)
            break

    def chave(it):
        return (not it["lido"], it["ultimo"] or datetime.min,
                ultimo_mat.get(it["materia"].id) or datetime.min, it["ordem"])

    ontem = dt0(h - timedelta(days=1))
    recente = lambda it: it["ultimo"] is not None and it["ultimo"] >= ontem  # feito ontem: vai para o fim
    ordenados = (sorted([i for i in itens if i["lido"] and not recente(i)], key=chave)
                 + sorted([i for i in itens if not i["lido"] and not recente(i)], key=chave)
                 + sorted([i for i in itens if recente(i)], key=chave))
    item = next((i for i in itens if i["chave"] == hoje_item), None) or ordenados[0]
    alternativas = [i for i in ordenados if i is not item][:3]
    return {"meta": meta, "feitas": feitas, "acertos": acertos, "item": item, "alternativas": alternativas}


def resumo_dia():
    cfg = configs()
    h = hoje()
    tents_hoje = Tentativa.query.filter(Tentativa.feita_em >= dt0(h)).all()
    rec = Tentativa.query.filter(Tentativa.feita_em >= dt0(h - timedelta(days=6))).all()
    r_resp = sum(t.respondidas for t in rec)
    return {
        "plano": plano_do_dia(),
        "bateria": bateria_do_dia(),
        "questoes": sum(t.respondidas for t in tents_hoje),
        "meta_questoes": max(QUESTOES_MINIMO, int(cfg_num(cfg, "meta_questoes_dia"))),
        "acerto7": round(100 * sum(t.acertos for t in rec) / r_resp, 1) if r_resp else None,
        "meta_acerto": cfg_num(cfg, "meta_acerto"),
    }


def calcular_humor():
    s = resumo_dia()
    p = s["plano"]
    dias = dias_atividade()
    h = hoje()
    if not dias:
        g.humor_atual = "neutro"
        return {"humor": "neutro", "titulo": "Oi! Eu sou a Tatti",
                "fala": "Vou acompanhar seus estudos. Bata a meta do dia e eu fico feliz; "
                        "suma e eu fico brava. Comece pela Meta de hoje!",
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
        motivos.append((False, "Nenhuma aula cadastrada para montar a meta diária"))

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
            motivos.append((True, f"Bateria de hoje concluída ({s['questoes']}/{s['meta_questoes']})"))
        elif s["questoes"]:
            pts += 1
            motivos.append((False, f"Bateria de hoje: {s['questoes']}/{s['meta_questoes']} questões"))
        else:
            motivos.append((False, f"Bateria de {s['meta_questoes']} questões de hoje ainda não feita"))

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
    g.humor_atual = humor
    return {"humor": humor, "titulo": TITULOS[humor], "fala": random.choice(FALAS[humor]),
            "motivos": motivos, "dia": s}


def stats_materia(m):
    tents = Tentativa.query.filter_by(materia_id=m.id).order_by(Tentativa.feita_em).all()
    resp = sum(t.respondidas for t in tents)
    lidas = sum(1 for a in m.aulas if a.leituras)
    alvo = max(1, int(cfg_num(configs(), "meta_leituras_por_aula")))
    feitas = sum(min(len(a.leituras), alvo) for a in m.aulas)
    lidos_top = {t.id for a in m.aulas if a.leituras for t in a.topicos}
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
    return {"cfg": cfg, "dias_prova": dias_para_prova(cfg), "meta_acerto": cfg_num(cfg, "meta_acerto"),
            "humor_atual": humor_atual()}


def humor_atual():
    """Humor da Tatti (calculado uma vez por requisição) — usado no ícone do app e no logo."""
    if "humor_atual" not in g:
        try:
            calcular_humor()
        except Exception:
            g.humor_atual = "neutro"
    return g.humor_atual


@app.route("/manifest.webmanifest")
def manifest():
    h = humor_atual()
    cores = {"furioso": "#e5484d", "bravo": "#f0893a", "neutro": "#8e4ec6", "feliz": "#2fb36d", "radiante": "#e8a317"}
    dados = {
        "name": "DUOTATTI", "short_name": "DUOTATTI", "start_url": "/", "scope": "/",
        "display": "standalone", "background_color": "#f6f7f9", "theme_color": cores.get(h, "#8e4ec6"),
        "icons": [{"src": url_for("static", filename=f"icones/{h}-{t}.png"), "sizes": f"{t}x{t}",
                   "type": "image/png", "purpose": "any"} for t in (192, 512)],
    }
    resp = app.response_class(json.dumps(dados, ensure_ascii=False), mimetype="application/manifest+json")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.before_request
def exigir_senha():
    senha = os.environ.get("APP_PASSWORD")
    if not senha or request.endpoint in ("login", "static", "manifest") or session.get("ok"):
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
    validos = {t.id: t for t in m.topicos}
    vinc = [validos[i] for i in dict.fromkeys(request.form.getlist("topico_ids", type=int)) if i in validos]
    novo = request.form.get("novo_assunto", "").strip()
    if novo:
        t_novo = garantir_topico(m.id, novo)
        if t_novo not in vinc:
            vinc.append(t_novo)
    n = 0
    prox = (db.session.query(db.func.max(Aula.ordem)).filter_by(materia_id=m.id).scalar() or 0) + 1
    for f in arquivos:
        data, nome = _ler_upload(f)
        if not data:
            continue
        t = nome_padrao(nome)
        db.session.add(Aula(materia_id=m.id, titulo=t[:200], arquivo_nome=nome[:200], topicos=list(vinc),
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
def montar_questoes(c, extra=None):
    """(Re)constrói as questões do caderno a partir do PDF. Retorna quantas ficaram completas."""
    Questao.query.filter_by(caderno_id=c.id).delete()
    gab = c.gabarito
    if not gab or not c.pdf_questoes:
        db.session.commit()
        return 0
    try:
        paginas = [p.extract_text() or "" for p in PdfReader(io.BytesIO(c.pdf_questoes)).pages]
    except Exception:
        paginas = []
    texto = QZ.cortar_gabarito(QZ.limpar_paginas(paginas), parse_gabarito_texto)
    res = QZ.extrair_questoes(texto, list(gab))
    for n, q in (extra or {}).items():  # leitura da IA tem prioridade
        if n in gab and (q["ok"] or not res.get(n, {}).get("ok")):
            res[n] = q
    for n, q in res.items():
        db.session.add(Questao(caderno_id=c.id, numero=n, fonte=(q["fonte"] or "")[:300], apoio=q["apoio"],
                               enunciado=q["enunciado"], alternativas_json=json.dumps(q["alternativas"], ensure_ascii=False),
                               ok=q["ok"]))
    db.session.commit()
    return sum(1 for q in res.values() if q["ok"])


# ---- Leitura com IA (fila em segundo plano) ------------------------------- #
_fila_ia = queue.Queue()
_trabalhador = {"t": None}


def _processar_ia(cid):
    c = db.session.get(Caderno, cid)
    if not c or not c.gabarito:
        return
    c.ia_status, c.ia_inicio, c.ia_msg = "lendo", agora(), "Começando a leitura…"
    db.session.commit()

    def progresso(k, total):
        c.ia_msg = f"Lendo parte {k} de {total}…"
        db.session.commit()

    try:
        res = IA.extrair(c.pdf_questoes, progresso)
        ok = montar_questoes(c, extra=res)
        lidas = sum(1 for n, q in res.items() if n in c.gabarito and q["ok"])
        c = db.session.get(Caderno, cid)
        c.ia_status = "ok"
        c.ia_msg = f"{ok} de {c.total} questões prontas ({lidas} lidas pela IA)."
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        c = db.session.get(Caderno, cid)
        c.ia_status = "erro"
        c.ia_msg = f"Erro na leitura com IA: {e.__class__.__name__}: {str(e)[:200]}"
    db.session.commit()


def _loop_ia():
    while True:
        cid = _fila_ia.get()
        with app.app_context():
            try:
                _processar_ia(cid)
            except Exception:  # noqa: BLE001
                db.session.rollback()
            finally:
                db.session.remove()


def enfileirar_ia(c):
    if not IA.disponivel():
        return False
    c.ia_status, c.ia_msg, c.ia_inicio = "fila", "Na fila para leitura com IA…", agora()
    db.session.commit()
    if _trabalhador["t"] is None or not _trabalhador["t"].is_alive():
        _trabalhador["t"] = threading.Thread(target=_loop_ia, daemon=True)
        _trabalhador["t"].start()
    _fila_ia.put(c.id)
    return True


def ia_ocupada(c):
    """Leitura em andamento (ignora leituras 'presas' há mais de 30 min, ex.: servidor reiniciou)."""
    return c.ia_status in ("fila", "lendo") and c.ia_inicio and (agora() - c.ia_inicio) < timedelta(minutes=30)


@app.post("/caderno/<int:cid>/ia")
def caderno_ia(cid):
    c = db.get_or_404(Caderno, cid)
    if not IA.disponivel():
        flash("A IA não está configurada (falta ANTHROPIC_API_KEY no Railway).", "erro")
    elif not ia_ocupada(c):
        enfileirar_ia(c)
        flash("A Tatti está lendo este caderno com IA. Pode levar alguns minutos.", "ok")
    return redirect(url_for("caderno_resolver", cid=cid))


@app.post("/cadernos/ia-todos")
def cadernos_ia_todos():
    if not IA.disponivel():
        flash("A IA não está configurada (falta ANTHROPIC_API_KEY no Railway).", "erro")
        return redirect(url_for("metas"))
    n = 0
    for c in Caderno.query.order_by(Caderno.id).all():
        if c.gabarito and c.ia_status != "ok" and not ia_ocupada(c):
            enfileirar_ia(c)
            n += 1
    flash(f"{n} caderno(s) na fila de leitura com IA." if n else "Todos os cadernos já foram lidos com IA.", "ok")
    return redirect(url_for("metas"))


@app.get("/api/caderno/<int:cid>/ia")
def api_caderno_ia(cid):
    c = db.get_or_404(Caderno, cid)
    return jsonify(ok=True, status=c.ia_status, msg=c.ia_msg, ocupada=bool(ia_ocupada(c)))


@app.post("/materia/<int:mid>/cadernos")
def caderno_novo(mid):
    m = db.get_or_404(Materia, mid)
    dq, nome = _ler_upload(request.files.get("pdf_questoes"))
    if not dq:
        flash("Escolha o PDF com as questões.", "erro")
        return redirect(url_for("materia", mid=mid) + "#questoes")
    dg, _ = _ler_upload(request.files.get("pdf_gabarito"))
    texto = request.form.get("gabarito_texto", "")

    gab = {}
    if texto.strip():
        gab = parse_gabarito_texto(texto, manual=True)
    if not gab and dg:
        gab = parse_gabarito_texto(extrair_texto(dg))
    if not gab:
        gab = parse_gabarito_texto(extrair_texto(dq), exigir_secao=True)

    titulo = request.form.get("titulo", "").strip() or os.path.splitext(nome)[0]
    c = Caderno(materia_id=m.id, titulo=titulo[:200], arquivo_nome=nome[:200], pdf_questoes=dq,
                pdf_gabarito=dg, tem_pdf_gabarito=bool(dg))
    c.gabarito = gab
    db.session.add(c)
    definir_topico_caderno(c, request.form.get("topico_id"), request.form.get("novo_assunto"))
    db.session.commit()
    if not gab:
        flash("Caderno criado, mas não consegui ler o gabarito. Digite-o abaixo.", "erro")
        return redirect(url_for("caderno_gabarito", cid=c.id))
    ok = montar_questoes(c)
    if enfileirar_ia(c):
        flash("Caderno criado. A Tatti está lendo as questões com IA — leva alguns minutos.", "ok")
    else:
        flash(f"Caderno criado: {ok} de {len(gab)} questões reconstruídas. Bons estudos!", "ok" if ok else "erro")
    return redirect(url_for("materia", mid=mid) + "#questoes")


@app.route("/caderno/<int:cid>/gabarito", methods=["GET", "POST"])
def caderno_gabarito(cid):
    """Corrigir o gabarito (mostra as respostas — por isso fica escondido atrás de um aviso)."""
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
            ok = montar_questoes(c)
            if enfileirar_ia(c):
                flash(f"Gabarito salvo: {len(gab)} questões. Relendo com IA…", "ok")
            else:
                flash(f"Gabarito salvo: {len(gab)} questões ({ok} reconstruídas).", "ok")
            return redirect(url_for("caderno_resolver", cid=cid))
    texto = "\n".join(f"{n}-{l}" for n, l in c.gabarito.items())
    return render_template("gabarito.html", c=c, m=c.materia, texto=texto)


@app.post("/caderno/<int:cid>/reprocessar")
def caderno_reprocessar(cid):
    c = db.get_or_404(Caderno, cid)
    ok = montar_questoes(c)
    flash(f"{ok} de {c.total} questões reconstruídas do PDF.", "ok" if ok else "erro")
    return redirect(url_for("caderno_resolver", cid=cid))


def _tentativa_aberta(c):
    return (Tentativa.query.filter_by(caderno_id=c.id, finalizada=False)
            .order_by(Tentativa.feita_em.desc()).first())


@app.route("/caderno/<int:cid>")
def caderno_resolver(cid):
    c = db.get_or_404(Caderno, cid)
    if not c.gabarito:
        flash("Este caderno ainda não tem gabarito.", "erro")
        return redirect(url_for("caderno_gabarito", cid=cid))
    if not Questao.query.filter_by(caderno_id=c.id).count():
        montar_questoes(c)
        if IA.disponivel() and not c.ia_status:
            enfileirar_ia(c)
    qs = Questao.query.filter_by(caderno_id=c.id).order_by(Questao.numero).all()
    aberta = _tentativa_aberta(c)
    return render_template("resolver.html", c=c, m=c.materia, numeros=[q.numero for q in qs],
                           reconstruidas=sum(1 for q in qs if q.ok), aberta=aberta,
                           ia_on=IA.disponivel(), ia_lendo=bool(ia_ocupada(c)),
                           finalizadas=[t for t in c.tentativas if t.finalizada is not False])


def _questao_json(t, q):
    resp = t.respostas
    d = {"n": q.numero, "fonte": q.fonte or "", "apoio": q.apoio or "", "enunciado": q.enunciado or "",
         "alternativas": q.alternativas, "ok": bool(q.ok)}
    if q.numero in resp:  # já respondida nesta bateria: pode mostrar a correção
        certa = t.caderno.gabarito.get(q.numero)
        d.update(sua=resp[q.numero], certa=certa, correta=certa in ("X", resp[q.numero]))
    return d


def _estado(t):
    resp, gab = t.respostas, t.caderno.gabarito
    return {"acertos": t.acertos, "respondidas": t.respondidas, "total": t.total,
            "status": {str(n): ("ok" if gab.get(n) in ("X", v) else "erro") for n, v in resp.items()}}


@app.post("/api/caderno/<int:cid>/iniciar")
def api_iniciar(cid):
    c = db.get_or_404(Caderno, cid)
    t = _tentativa_aberta(c)
    if t is None or (request.get_json(silent=True) or {}).get("nova"):
        if t is not None:  # descarta a bateria aberta vazia; se já tinha respostas, encerra
            if t.respondidas:
                t.finalizada = True
            else:
                db.session.delete(t)
        t = Tentativa(caderno_id=c.id, materia_id=c.materia_id, feita_em=agora(), acertos=0, respondidas=0,
                      total=len(c.gabarito), xp=0, respostas_json="{}", finalizada=False)
        db.session.add(t)
        db.session.commit()
    return jsonify(ok=True, tentativa=t.id, **_estado(t))


@app.get("/api/tentativa/<int:tid>/questao/<int:n>")
def api_questao(tid, n):
    t = db.get_or_404(Tentativa, tid)
    q = Questao.query.filter_by(caderno_id=t.caderno_id, numero=n).first_or_404()
    return jsonify(ok=True, questao=_questao_json(t, q), **_estado(t))


@app.post("/api/tentativa/<int:tid>/responder")
def api_responder(tid):
    t = db.get_or_404(Tentativa, tid)
    if t.finalizada:
        return jsonify(ok=False, erro="bateria encerrada"), 400
    d = request.get_json(silent=True) or {}
    try:
        n = int(d.get("n"))
    except (TypeError, ValueError):
        return jsonify(ok=False), 400
    letra = str(d.get("letra") or "").upper()
    gab = t.caderno.gabarito
    if n not in gab or letra not in LETRAS:
        return jsonify(ok=False), 400
    resp = t.respostas
    if n not in resp:  # resposta é definitiva
        resp[n] = letra
        t.respostas_json = json.dumps({str(k): v for k, v in resp.items()})
        t.respondidas = len(resp)
        t.acertos = sum(1 for k, v in resp.items() if gab.get(k) in ("X", v))
        t.xp = t.acertos
        t.feita_em = agora()
        db.session.commit()
    certa = gab[n]
    return jsonify(ok=True, n=n, sua=resp[n], certa=certa, correta=certa in ("X", resp[n]), **_estado(t))


@app.post("/api/tentativa/<int:tid>/finalizar")
def api_finalizar(tid):
    t = db.get_or_404(Tentativa, tid)
    if not t.respondidas:
        db.session.delete(t)
        db.session.commit()
        return jsonify(ok=True, url=url_for("caderno_resolver", cid=t.caderno_id))
    if not t.finalizada:
        meta = cfg_num(configs(), "meta_acerto")
        t.xp = t.acertos + (25 if t.percentual >= meta and t.respondidas >= 5 else 0)
        t.finalizada = True
        db.session.commit()
    return jsonify(ok=True, url=url_for("tentativa_ver", tid=t.id))


@app.route("/tentativa/<int:tid>")
def tentativa_ver(tid):
    t = db.get_or_404(Tentativa, tid)
    if t.finalizada is False:
        return redirect(url_for("caderno_resolver", cid=t.caderno_id))
    gab, resp = t.caderno.gabarito, t.respostas
    linhas = [{"n": n, "sua": resp.get(n), "certa": g,
               "ok": (n in resp and g in ("X", resp[n])), "branco": n not in resp}
              for n, g in gab.items()]
    meta = cfg_num(configs(), "meta_acerto")
    anteriores = [x for x in t.caderno.tentativas if x.id != t.id and x.feita_em <= t.feita_em and x.finalizada is not False]
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


def _vinculos_json(a):
    return [{"id": t.id, "rotulo": t.rotulo, "personalizado": bool(t.personalizado)} for t in a.topicos]


@app.post("/api/aula/<int:aid>/topicos")
def api_aula_topicos(aid):
    """Vínculos da aula com itens do edital: {"add": id} ou {"remove": id}."""
    a = db.get_or_404(Aula, aid)
    d = request.get_json(silent=True) or {}
    validos = {t.id: t for t in a.materia.topicos}
    try:
        add = int(d["add"]) if d.get("add") not in (None, "") else None
        rem = int(d["remove"]) if d.get("remove") not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify(ok=False), 400
    if add is not None:
        if add not in validos:
            return jsonify(ok=False), 400
        if validos[add] not in a.topicos:
            a.topicos.append(validos[add])
    if rem is not None and rem in validos and validos[rem] in a.topicos:
        a.topicos.remove(validos[rem])
    db.session.commit()
    return jsonify(ok=True, topicos=_vinculos_json(a))


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


CARDS_EVOLUCAO = ["calendario", "acerto", "semanas", "materias", "atividade"]


def ordem_cards():
    salvo = [c for c in (configs().get("ordem_evolucao") or "").split(",") if c in CARDS_EVOLUCAO]
    return salvo + [c for c in CARDS_EVOLUCAO if c not in salvo]


@app.post("/api/ordem-cards")
def api_ordem_cards():
    ordem = (request.get_json(silent=True) or {}).get("ordem") or []
    ordem = [c for c in ordem if c in CARDS_EVOLUCAO]
    if sorted(set(ordem)) != sorted(CARDS_EVOLUCAO):
        return jsonify(ok=False), 400
    c = db.session.get(Config, "ordem_evolucao") or Config(chave="ordem_evolucao")
    c.valor = ",".join(ordem)
    db.session.add(c)
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/tutorial-visto")
def api_tutorial_visto():
    c = db.session.get(Config, "tutorial_visto") or Config(chave="tutorial_visto")
    c.valor = "1"
    db.session.add(c)
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
    cal, proxima = dados_calendario()
    return render_template("historico.html", materias=[(m, stats_materia(m), por_assunto(m)) for m in materias],
                           cal=cal, proxima=proxima, cal_hoje=hoje(), ordem_cards=ordem_cards(),
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
                    v = str(max(QUESTOES_MINIMO if k == "meta_questoes_dia" else 0, min(1000, int(float(v)))))
                except ValueError:
                    continue
            c = db.session.get(Config, k) or Config(chave=k)
            c.valor = v[:200]
            db.session.add(c)
        db.session.commit()
        flash("Metas salvas. A Tatti já está de olho!", "ok")
        return redirect(url_for("metas"))
    cads = Caderno.query.all()
    ia_info = {"on": IA.disponivel(), "modelo": IA.modelo(), "total": len(cads),
               "ok": sum(1 for c in cads if c.ia_status == "ok"),
               "lendo": sum(1 for c in cads if ia_ocupada(c)),
               "erro": sum(1 for c in cads if c.ia_status == "erro")}
    return render_template("metas.html", h=calcular_humor(), p=plano_do_dia(), ia=ia_info)


@app.errorhandler(413)
def muito_grande(_):
    flash(f"Arquivo grande demais (limite {app.config['MAX_CONTENT_LENGTH'] // 1048576} MB).", "erro")
    return redirect(request.referrer or url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
