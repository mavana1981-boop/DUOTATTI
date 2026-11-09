"""
DUOTATTI — plataforma de estudos para o concurso de Escrevente Técnico Judiciário do TJSP.
Flask + SQLAlchemy (Postgres no Railway / SQLite local).
"""
import hashlib
import io
import math
import json
import os
import random
import re
import time as _time
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from flask import (Flask, abort, flash, g, has_app_context, has_request_context, jsonify, redirect,
                   render_template, request, send_file, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from jinja2 import ChoiceLoader, FileSystemLoader
from pypdf import PdfReader
from sqlalchemy import event as sa_event, inspect as sa_inspect, or_, select, text as sa_text
from sqlalchemy.orm import Session as SASession, deferred, with_loader_criteria
from werkzeug.security import check_password_hash, generate_password_hash
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
    usuario = db.Column(db.String(60))


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
    ia_batida = db.Column(db.DateTime)        # último sinal de vida da leitura (para retomar se travar)
    ia_provedor = db.Column(db.String(20))    # gemini | claude — IA usada (ou em uso) na leitura
    ia_claude_ok = db.Column(db.Boolean, default=False)  # admin autorizou usar o Claude (pago) neste caderno
    ia_parcial = deferred(db.Column(db.Text))  # questões já lidas pela IA (JSON) — continua de onde parou
    ia_tokens = db.Column(db.Integer, default=0)  # tokens gastos na última leitura
    ia_escopo = db.Column(db.String(10))      # alvo | todas | tudo — o que a IA lê (escolhido antes de começar)
    pdf_hash = db.Column(db.String(64))       # assinatura do PDF: o mesmo PDF não é lido duas vezes
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
    usuario = db.Column(db.String(60))
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


class Usuario(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    login = db.Column(db.String(60), unique=True, nullable=False)
    nome = db.Column(db.String(100))
    senha_hash = db.Column(db.String(300), nullable=False)
    admin = db.Column(db.Boolean, default=False)
    criado_em = db.Column(db.DateTime, default=agora)

    def senha_ok(self, senha):
        return check_password_hash(self.senha_hash, senha or "")


class MetaDia(db.Model):
    """Foto do plano de cada dia (o que foi planejado), para histórico e remarcação — uma por usuário."""
    __tablename__ = "meta_dia_usuario"
    __table_args__ = (db.UniqueConstraint("usuario", "data", name="uq_meta_dia_usuario"),)
    id = db.Column(db.Integer, primary_key=True)
    usuario = db.Column(db.String(60), index=True)
    data = db.Column(db.Date, nullable=False, index=True)
    meta_leituras = db.Column(db.Integer, default=0)
    planejadas_json = db.Column(db.Text, default="[]")   # ids das aulas planejadas
    meta_questoes = db.Column(db.Integer, default=60)
    item_bateria = db.Column(db.String(300))
    restante_inicio = db.Column(db.Integer)
    criado_em = db.Column(db.DateTime, default=agora)

    @property
    def planejadas(self):
        try:
            return [int(x) for x in json.loads(self.planejadas_json or "[]")]
        except Exception:
            return []


class AjusteMeta(db.Model):
    """Ajuste feito pelo admin na meta do dia de um aluno.
    troca: a aula sai da meta e volta sozinha em ~10 dias (volta_em); substituta_id = aula escolhida no lugar (ou automática).
    exclusao: a aula sai da meta só naquele dia e a meta do dia diminui uma leitura.
    inclusao: a aula entra na meta só naquele dia e a meta do dia aumenta uma leitura."""
    id = db.Column(db.Integer, primary_key=True)
    usuario = db.Column(db.String(60), index=True)
    aula_id = db.Column(db.Integer, index=True, nullable=False)
    tipo = db.Column(db.String(10), nullable=False)       # troca | exclusao | inclusao
    data = db.Column(db.Date, nullable=False, index=True)
    volta_em = db.Column(db.Date)
    substituta_id = db.Column(db.Integer)
    criado_em = db.Column(db.DateTime, default=agora)


DIAS_TROCA = 10   # aula trocada volta à meta do dia depois de ~10 dias


class SessaoPdf(db.Model):
    """Tempo com uma aula aberta (só conta enquanto a tela está visível)."""
    id = db.Column(db.Integer, primary_key=True)
    usuario = db.Column(db.String(60), index=True)
    aula_id = db.Column(db.Integer, index=True)
    titulo = db.Column(db.String(200))
    materia = db.Column(db.String(120))
    inicio = db.Column(db.DateTime, default=agora, index=True)
    fim = db.Column(db.DateTime, default=agora)
    segundos = db.Column(db.Integer, default=0)


class LogAcesso(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    quando = db.Column(db.DateTime, default=agora, nullable=False, index=True)
    usuario = db.Column(db.String(60), index=True)
    evento = db.Column(db.String(20), nullable=False)  # login | falha | logout | pagina | acao
    detalhe = db.Column(db.String(300))
    caminho = db.Column(db.String(300))
    ip = db.Column(db.String(64))
    aparelho = db.Column(db.String(300))


class PushInscricao(db.Model):
    """Celular/navegador inscrito para receber notificações da Tatti."""
    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuario.id", ondelete="CASCADE"))
    endpoint = db.Column(db.Text, nullable=False, unique=True)
    p256dh = db.Column(db.String(200), nullable=False)
    auth = db.Column(db.String(100), nullable=False)
    horario = db.Column(db.String(5), default="19:00")   # HH:MM (horário de Brasília)
    ultimo_envio = db.Column(db.Date)
    aparelho = db.Column(db.String(300))
    criado_em = db.Column(db.DateTime, default=agora)


def _sq_admins():
    return select(Usuario.login).where(Usuario.admin.is_(True)).scalar_subquery()


USUARIO_PRINCIPAL = "renata"   # dona dos registros antigos (de antes dos logins)


def escopo():
    """Login cujos dados (leituras, baterias, metas, ofensiva, configurações) estão sendo vistos.
    Aluno: ele mesmo. Admin: o aluno escolhido em "ver como" (padrão: a Renata). Fora de requisição: None."""
    if not has_app_context():
        return None
    if "escopo" in g:
        return g.escopo
    if not has_request_context() or g.get("_calc_escopo"):
        return None
    g._calc_escopo = True
    try:
        u = usuario_atual()
        val = None if u is None else ((session.get("ver_como") or USUARIO_PRINCIPAL) if u.admin else u.login)
    finally:
        g._calc_escopo = False
    if u is not None:
        g.escopo = val
    return val


@sa_event.listens_for(SASession, "do_orm_execute")
def _ignorar_admin(state):
    """Cada aluno só enxerga as próprias leituras, baterias e fotos de meta.
    Leituras e baterias feitas pelo admin são testes: ficam fora de metas, humor, XP, calendário etc.
    execution_options(todos=True) enxerga tudo; todos_usuarios=True enxerga todos os alunos (sem o admin)."""
    if not state.is_select or state.execution_options.get("todos", False):
        return
    esc = None if state.execution_options.get("todos_usuarios", False) else escopo()
    if esc:
        state.statement = state.statement.options(
            with_loader_criteria(Leitura, lambda c: c.usuario == esc, include_aliases=True),
            with_loader_criteria(Tentativa, lambda c: c.usuario == esc, include_aliases=True),
            with_loader_criteria(MetaDia, lambda c: c.usuario == esc, include_aliases=True),
        )
    else:
        state.statement = state.statement.options(
            with_loader_criteria(Leitura, lambda c: or_(c.usuario.is_(None), c.usuario.notin_(_sq_admins())),
                                 include_aliases=True),
            with_loader_criteria(Tentativa, lambda c: or_(c.usuario.is_(None), c.usuario.notin_(_sq_admins())),
                                 include_aliases=True),
        )


def eh_admin():
    u = usuario_atual() if request else None
    return bool(u and u.admin)


CHAVES_USUARIO = None  # definido após DEFAULTS

USUARIOS_INICIAIS = [("admin", "Administrador", "admin", True), ("renata", "Renata", "tatti", False),
                     ("james", "James", "james123", False), ("andre", "André", "andre123", False),
                     ("alencar", "Alencar", "alencar123", False)]


class ErroApp(db.Model):
    """Erros do servidor, para o admin ver em Logs › Erros."""
    id = db.Column(db.Integer, primary_key=True)
    quando = db.Column(db.DateTime, default=agora, index=True)
    usuario = db.Column(db.String(60))
    caminho = db.Column(db.String(300))
    tipo = db.Column(db.String(120))
    mensagem = db.Column(db.Text)
    rastreio = db.Column(db.Text)


class Config(db.Model):
    chave = db.Column(db.String(50), primary_key=True)
    valor = db.Column(db.Text)  # texto livre (ex.: chave das notificações, ~240 caracteres)


DEFAULTS = {
    "nome": "Estudante",
    "data_prova": "2027-03-14",
    "meta_acerto": "80",
    "meta_leituras_por_aula": "5",      # cada aula deve ser lida 5x
    "data_limite_leituras": "2027-03-10",  # depois disso: só questões e descanso
    "max_dias_sem_ver": "10",           # nenhuma aula (já iniciada) deve ficar mais que isso sem leitura
    "novas_por_dia": "2",               # mínimo de aulas novas (1ª leitura) por dia, enquanto houver
    "meta_questoes_dia": "60",          # bateria diária obrigatória (mínimo 60)
}


CHAVES_USUARIO = set(DEFAULTS) | {"ordem_evolucao", "tutorial_visto", "bateria_escolha"}  # cada aluno tem as suas


def chave_cfg(k, login=None):
    """Nome da chave no banco: as configurações do aluno ficam como 'login:chave'."""
    login = login or escopo()
    return f"{login}:{k}" if login and k in CHAVES_USUARIO else k


def configs():
    cfg = dict(DEFAULTS)
    esc = escopo()
    linhas = Config.query.all()
    if not esc:
        for c in linhas:
            cfg[c.chave] = c.valor
        return cfg
    u = Usuario.query.filter_by(login=esc).first()
    if u and u.nome:
        cfg["nome"] = u.nome
    pref = esc + ":"
    for c in linhas:
        if c.chave.startswith(pref):
            cfg[c.chave[len(pref):]] = c.valor
        elif ":" not in c.chave and c.chave not in CHAVES_USUARIO:
            cfg[c.chave] = c.valor   # chaves globais (ex.: notificações)
    return cfg


def salvar_cfg(k, v):
    c = db.session.get(Config, chave_cfg(k)) or Config(chave=chave_cfg(k))
    c.valor = v
    db.session.add(c)


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


NOVAS_COLUNAS = [("caderno", "assunto", "VARCHAR(200)"), ("topico", "personalizado", "BOOLEAN DEFAULT FALSE"), ("caderno", "topico_id", "INTEGER"), ("topico", "grupo", "VARCHAR(200)"), ("aula", "ordem", "INTEGER"), ("tentativa", "finalizada", "BOOLEAN DEFAULT TRUE"), ("caderno", "ia_status", "VARCHAR(20)"), ("caderno", "ia_msg", "VARCHAR(300)"), ("caderno", "ia_inicio", "TIMESTAMP"), ("log_acesso", "detalhe", "VARCHAR(300)"), ("leitura", "usuario", "VARCHAR(60)"), ("tentativa", "usuario", "VARCHAR(60)"), ("caderno", "ia_batida", "TIMESTAMP"), ("caderno", "ia_provedor", "VARCHAR(20)"), ("caderno", "ia_claude_ok", "BOOLEAN DEFAULT FALSE"), ("caderno", "ia_parcial", "TEXT"), ("caderno", "ia_tokens", "INTEGER DEFAULT 0"), ("caderno", "pdf_hash", "VARCHAR(64)"), ("caderno", "ia_escopo", "VARCHAR(10)")]


def migrar(conn):
    """Adiciona colunas criadas depois do primeiro deploy (create_all não altera tabelas)."""
    from sqlalchemy import inspect
    insp = inspect(conn)
    for tabela, coluna, tipo in NOVAS_COLUNAS:
        if coluna not in {c["name"] for c in insp.get_columns(tabela)}:
            conn.exec_driver_sql(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")
    # colunas que precisaram crescer (Postgres respeita o tamanho; SQLite não)
    if conn.dialect.name == "postgresql":
        for tabela, coluna in (("config", "valor"),):
            tipo = next((str(c["type"]) for c in insp.get_columns(tabela) if c["name"] == coluna), "")
            if tipo.upper().startswith("VARCHAR"):
                conn.exec_driver_sql(f"ALTER TABLE {tabela} ALTER COLUMN {coluna} TYPE TEXT")


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


def criar_usuarios():
    for login_, nome, senha, adm in USUARIOS_INICIAIS:
        if not Usuario.query.filter_by(login=login_).first():
            db.session.add(Usuario(login=login_, nome=nome, senha_hash=generate_password_hash(senha), admin=adm))
    db.session.commit()


def reparar_cadernos_tec():
    """Uma vez: cadernos do TEC Concursos enviados antes do leitor próprio ficaram com o gabarito
    incompleto (Certo/Errado e respostas coladas não eram lidos) — e as questões fora do gabarito sumiam.
    Completa o gabarito (sem mudar nenhuma resposta já salva) e remonta as questões com o leitor do TEC."""
    if db.session.get(Config, "reparo_tec_v1"):
        return
    falhou = False
    for c in Caderno.query.all():
        try:
            if not c.pdf_questoes:
                continue
            paginas = [p.extract_text() or "" for p in PdfReader(io.BytesIO(c.pdf_questoes)).pages]
            if not QZ.eh_tec("\n".join(paginas)):
                continue
            antigo = c.gabarito
            novo = parse_gabarito_texto("\n".join(paginas), exigir_secao=True)
            if len(novo) > len(antigo) and all(novo.get(k) == v for k, v in antigo.items()):
                c.gabarito = novo
                c.ia_parcial = None
                c.ia_status = None
                c.ia_msg = f"Gabarito completado automaticamente ({len(antigo)} → {len(novo)} questões)."
                db.session.commit()
                montar_questoes(c)
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            app.logger.warning("reparo TEC caderno %s: %s", c.id, e)
            falhou = True
    if not falhou:  # só marca como feito se deu certo (senão tenta de novo no próximo deploy)
        db.session.add(Config(chave="reparo_tec_v1", valor="ok"))
        db.session.commit()


def contar_paginas_faltantes():
    """Aulas sem número de páginas (ex.: contagem falhou no envio) são contadas de novo."""
    for a in Aula.query.filter(Aula.paginas.is_(None)).all():
        try:
            if a.pdf:
                a.paginas = contar_paginas(a.pdf)
        except Exception:  # noqa: BLE001
            pass
    db.session.commit()


def limpar_registros_admin():
    """Remove do log e das sessões de PDF o que já tinha sido gravado do admin."""
    admins = [u.login for u in Usuario.query.filter_by(admin=True).all()]
    if admins:
        LogAcesso.query.filter(LogAcesso.usuario.in_(admins), LogAcesso.evento != "falha").delete(synchronize_session=False)
        SessaoPdf.query.filter(SessaoPdf.usuario.in_(admins)).delete(synchronize_session=False)
        db.session.commit()


def separar_por_usuario():
    """Antes havia um só aluno: leituras/baterias sem dono, metas do dia e configurações viram da Renata."""
    if not Usuario.query.filter_by(login=USUARIO_PRINCIPAL).first():
        return
    Leitura.query.filter(Leitura.usuario.is_(None)).update({"usuario": USUARIO_PRINCIPAL}, synchronize_session=False)
    Tentativa.query.filter(Tentativa.usuario.is_(None)).update({"usuario": USUARIO_PRINCIPAL}, synchronize_session=False)
    existentes = {c.chave for c in Config.query.all()}
    for c in Config.query.filter(Config.chave.in_(CHAVES_USUARIO)).all():
        nova = chave_cfg(c.chave, USUARIO_PRINCIPAL)
        if nova not in existentes:
            db.session.add(Config(chave=nova, valor=c.valor))
    db.session.commit()
    if sa_inspect(db.engine).has_table("meta_dia") and not MetaDia.query.execution_options(todos=True).first():
        cols = "data, meta_leituras, planejadas_json, meta_questoes, item_bateria, restante_inicio, criado_em"
        db.session.execute(sa_text(f"INSERT INTO meta_dia_usuario (usuario, {cols}) SELECT :u, {cols} FROM meta_dia"),
                           {"u": USUARIO_PRINCIPAL})
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
    criar_usuarios()
    separar_por_usuario()
    limpar_registros_admin()
    migrar_vinculos_aulas()
    atualizar_topicos_edital()
    assuntos_para_topicos()
    numerar_aulas()
    ajustar_bateria_minima()
    contar_paginas_faltantes()


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
    r"((?i:anulad[ao])|(?i:certo|errado)|[A-E]|X)(?![A-Za-zÀ-ú])"
)


def _limpar(g):
    """Fica com a maior sequência contínua de questões (tolera pequenos buracos).
    Se não começar entre 1 e 5 (ex.: caderno das questões 31 a 60), exige ao menos 5 respostas."""
    if not g:
        return {}
    ks = sorted(g)
    runs, atual = [], [ks[0]]
    for k in ks[1:]:
        if k <= atual[-1] + 3:
            atual.append(k)
        else:
            runs.append(atual)
            atual = [k]
    runs.append(atual)
    validos = [r for r in runs if r[0] <= 5 or len(r) >= 5]
    if not validos:
        return {}
    melhor = max(validos, key=len)
    return {k: g[k] for k in melhor}


def _norm_letra(l):
    """Letra do gabarito. Certo/Errado (CEBRASPE) viram C/E — as "alternativas" desses itens são C e E."""
    l = l.strip().upper()
    if l in ("CERTO", "ERRADO"):
        return l[0]
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
                and all(re.fullmatch(r"[A-EX*]|(?i:anulad[ao]|certo|errado)", x) for x in b)):
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
    # seções de gabarito (da última para a primeira): "Gabarito", "Respostas", "Chave de respostas"...
    pos = [m.start() for m in re.finditer(r"(?i)gabarito|chave\s+de\s+respostas|\brespostas?\b|folha\s+de\s+respostas", t)]
    blocos = [t[p:] for p in reversed(pos[-12:])] + ([] if exigir_secao else [t])
    minimo = 1 if manual else 3
    for k, bloco in enumerate(blocos):
        a, b = _pares(bloco), _tabular(bloco)
        melhor = a if len(a) >= len(b) else b
        if len(melhor) >= (minimo if (k == 0 or not pos or k >= len(pos)) else max(minimo, 5)):
            return melhor
    if exigir_secao:  # resposta logo após cada questão: "Gabarito: B", "Resposta: letra C"
        g = QZ.gabarito_inline(t)
        if len(g) >= 3:
            return g
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


def carga_do_dia(d, n, ult, adiadas, alvo, max_dias, limite, novas_min):
    """Meta equilibrada de um dia (a mesma regra no plano de hoje e no calendário).

    - releituras: o mínimo, num ritmo constante, para nenhuma aula iniciada passar de `max_dias`
      sem leitura (agenda pelo prazo de cada aula: última leitura + max_dias);
    - aulas novas: pelo menos `novas_min` por dia, enquanto houver;
    - e nunca menos que o ritmo para terminar tudo até a data limite (o que falta ÷ dias).
    Retorna (meta, aulas que precisam ser relidas hoje — pelo prazo, novas mínimas)."""
    restante = sum(max(0, alvo - v) for v in n.values())
    dias = (limite - d).days + 1
    if dias <= 0 or not restante:
        return 0, [], 0
    base = math.ceil(restante / dias)

    def fora(i):
        return i in adiadas and d < adiadas[i]

    ativas = sorted((ult[i] + timedelta(days=max_dias), i) for i in n
                    if 0 < n[i] < alvo and ult.get(i) and not fora(i))
    novas = min(max(0, novas_min), sum(1 for i in n if n[i] == 0 and not fora(i)))
    k_max = max(0, min(max_dias, dias - 1))
    vence = [sum(1 for prazo, _ in ativas if prazo <= d + timedelta(days=k)) for k in range(k_max + 1)]
    ritmo_rel = max([math.ceil(vence[k] / (k + 1)) for k in range(k_max + 1)] + [0])
    meta = max(base, ritmo_rel + novas)
    rel = meta - novas  # vagas de releitura no dia
    hoje_obrig = max([vence[0]] + [vence[k] - k * rel for k in range(1, k_max + 1)])
    hoje_obrig = max(0, min(hoje_obrig, rel))
    return meta, [i for _, i in ativas[:hoje_obrig]], novas


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
                     "ultima_antes": max(antes) if antes else None, "hoje": len(datas) - len(antes)}
    restante = sum(max(0, alvo - e["n"]) for e in est.values())
    restante_inicio = sum(max(0, alvo - e["n_antes"]) for e in est.values())
    feitas_hoje = restante_inicio - restante  # leituras de hoje que contaram para a meta
    dias = (limite - h).days + 1
    fase = "leitura" if dias > 0 else "questoes"
    meta_hoje = math.ceil(restante_inicio / dias) if fase == "leitura" and restante_inicio else 0

    # ajustes do admin: trocas (aula sai e volta em ~10 dias), exclusões e inclusões (só hoje)
    adiadas, voltas, excluidas, substitutas, incluidas, ajustes_hoje = {}, {}, set(), [], [], []
    adiada_em = {}  # aula trocada -> dia da troca
    escolhida_em = {}  # aula escolhida pelo admin hoje -> quando (pode ser uma aula já lida hoje: vale a leitura seguinte)
    esc = escopo()
    if esc:
        for aj in AjusteMeta.query.filter(AjusteMeta.usuario == esc, AjusteMeta.data >= h - timedelta(days=60)) \
                .order_by(AjusteMeta.data, AjusteMeta.id).all():
            if aj.aula_id not in est:
                continue
            if aj.data == h:
                ajustes_hoje.append(aj)
            if aj.tipo == "exclusao":
                if aj.data == h:
                    excluidas.add(aj.aula_id)
                continue
            if aj.tipo == "inclusao":
                if aj.data == h and aj.aula_id not in incluidas:
                    incluidas.append(aj.aula_id)
                    escolhida_em[aj.aula_id] = aj.criado_em
                continue
            if aj.data == h and aj.substituta_id in est:
                substitutas.append(aj.substituta_id)
                escolhida_em[aj.substituta_id] = aj.criado_em
            if aj.volta_em and aj.volta_em > h:
                adiadas[aj.aula_id] = aj.volta_em
                adiada_em[aj.aula_id] = aj.data
            elif aj.volta_em:
                u = est[aj.aula_id]["ultima"]
                if est[aj.aula_id]["n"] < alvo and (u is None or u.date() < aj.data or est[aj.aula_id]["hoje"]):
                    voltas[aj.aula_id] = aj.volta_em   # ainda não lida desde a troca: volta com prioridade
        for aid in adiadas:
            voltas.pop(aid, None)
    voltas_de = {}
    if esc and voltas:
        for aj in AjusteMeta.query.filter(AjusteMeta.usuario == esc, AjusteMeta.tipo == "troca",
                                          AjusteMeta.aula_id.in_(list(voltas))).all():
            voltas_de[aj.aula_id] = aj.data
    incluidas = [i for i in incluidas if i not in excluidas and est[i]["n_antes"] < alvo]
    novas_min = max(0, int(cfg_num(cfg, "novas_por_dia")))
    obrig, novas_hoje = [], 0
    if fase == "leitura" and restante_inicio:
        # meta equilibrada, calculada com a situação do início do dia (não muda com as leituras de hoje)
        meta_hoje, obrig, novas_hoje = carga_do_dia(
            h, {a.id: est[a.id]["n_antes"] for a in aulas},
            {a.id: (est[a.id]["ultima_antes"].date() if est[a.id]["ultima_antes"] else None) for a in aulas},
            {k: v for k, v in adiadas.items() if adiada_em.get(k, h) < h},  # trocas de hoje não mudam a meta
            alvo, max_dias, limite, novas_min)
    if fase == "leitura" and incluidas:
        meta_hoje += len(incluidas)

    def dias_sem(a):
        u = est[a.id]["ultima"]
        return (h - u.date()).days if u else None

    atrasadas = [a for a in aulas if 0 < est[a.id]["n"] < alvo and dias_sem(a) is not None and dias_sem(a) > max_dias
                 and a.id not in adiadas]
    atrasadas.sort(key=lambda a: -dias_sem(a))

    # última vez que cada matéria foi vista
    ultima_mat = {}
    for a in aulas:
        u = est[a.id]["ultima"]
        if u and (a.materia_id not in ultima_mat or u > ultima_mat[a.materia_id]):
            ultima_mat[a.materia_id] = u

    # aulas planejadas em dias anteriores e não lidas desde então -> remarcadas (prioridade)
    por_id = {a.id: a for a in aulas}
    pendentes = {}
    for snap in MetaDia.query.filter(MetaDia.data < h, MetaDia.data >= h - timedelta(days=30)).order_by(MetaDia.data).all():
        for aid in snap.planejadas:
            a = por_id.get(aid)
            if not a or aid in pendentes or est[aid]["n"] >= alvo or aid in adiadas:
                continue
            u = est[aid]["ultima"]
            if u is None or u.date() < snap.data:
                pendentes[aid] = snap.data
    for aid, d in voltas.items():
        if aid not in pendentes or d < pendentes[aid]:
            pendentes[aid] = d

    def motivo_de(a):
        """Por que a aula está na fila (mesmas regras do rodízio)."""
        if a.id in pendentes:
            return (f"trocada em {voltas_de[a.id].strftime('%d/%m')} — voltou à meta" if a.id in voltas_de
                    else f"planejada para {pendentes[a.id].strftime('%d/%m')} e não lida — remarcada"), True
        d = dias_sem(a)
        if est[a.id]["n"] > 0 and d is not None and d >= max_dias - 2:
            return f"sem leitura há {d} dias" + (" — passou do limite!" if d > max_dias else ""), True
        um = ultima_mat.get(a.materia_id)
        if um is None:
            return "matéria ainda não estudada", False
        d = (h - um.date()).days
        return ("matéria estudada hoje" if d == 0 else f"matéria parada há {d} dia{'s' if d > 1 else ''}"), False

    snap_hoje = MetaDia.query.filter_by(data=h).first() if esc else None
    fila = []
    if fase == "leitura":
        cands = [a for a in aulas if est[a.id]["n"] < alvo and est[a.id]["hoje"] == 0
                 and a.id not in adiadas and a.id not in excluidas]
        # aula excluída pelo admin sai da lista sem ninguém no lugar (a vaga fica vazia; a meta não muda)
        vagas_excluidas = sum(1 for i in excluidas if est[i]["hoje"] == 0)
        vagas = max(0, meta_hoje - feitas_hoje - vagas_excluidas)
        marca = agora()
        planejadas = set()
        escolhidas = [(i, "incluída pelo administrador") for i in incluidas] + \
                     [(i, "escolhida no lugar de uma aula trocada") for i in substitutas]
        ja = set()
        for sid, mot in escolhidas:  # escolhidas pelo admin (incluídas ou no lugar de uma aula trocada)
            a = por_id.get(sid)  # pode ser até uma aula trocada antes ("fora da meta"): o admin a trouxe de volta hoje
            u, quando = est[sid]["ultima"] if a else None, escolhida_em.get(sid)
            lida_depois = bool(u and (quando is None or u > quando))  # já fez a leitura pedida pelo admin
            if not a or sid in ja or sid in excluidas or est[sid]["n"] >= alvo or (est[sid]["hoje"] and lida_depois):
                continue
            ja.add(sid)
            fila.append({"aula": a, "vez": est[a.id]["n"] + 1, "motivo": mot, "urgente": False})
            if a in cands:
                cands.remove(a)
            marca += timedelta(seconds=1)
            ultima_mat[a.materia_id] = marca
            planejadas.add(a.materia_id)
        def por(a, motivo, urg):
            fila.append({"aula": a, "vez": est[a.id]["n"] + 1, "motivo": motivo, "urgente": urg})
            cands.remove(a)
            nonlocal_marca[0] += timedelta(seconds=1)
            ultima_mat[a.materia_id] = nonlocal_marca[0]  # próxima sugestão vai para outra matéria
            planejadas.add(a.materia_id)

        nonlocal_marca = [marca]

        def motivo_rodizio(a):
            um = ultima_mat.get(a.materia_id)
            if a.materia_id in planejadas:
                return "rodízio entre as matérias"
            if um is None:
                return "matéria ainda não estudada"
            d = (h - um.date()).days
            return "matéria estudada hoje" if d == 0 else f"matéria parada há {d} dia{'s' if d > 1 else ''}"

        def rodizio(pool):
            """Matéria vista há mais tempo; nela, a aula menos lida (e lida há mais tempo)."""
            mid = min({x.materia_id for x in pool}, key=lambda m: ultima_mat.get(m) or datetime.min)
            return min((x for x in pool if x.materia_id == mid),
                       key=lambda x: (est[x.id]["n"], est[x.id]["ultima"] or datetime.min, x.ordem or 0, x.id))

        def releitura(a):
            d = dias_sem(a)
            return (f"sem leitura há {d} dias" + (" — passou do limite!" if d > max_dias else "")
                    if d is not None else "releitura"), d is not None and d >= max_dias - 2

        # 1) releituras que vencem: o mínimo para nenhuma aula passar de 10 dias sem leitura
        for aid in obrig:
            a = por_id.get(aid)
            if a in cands:
                por(a, *releitura(a))
        # foto do dia (feita pela regra equilibrada): as aulas escolhidas de manhã continuam as mesmas
        if snap_hoje and snap_hoje.meta_leituras is not None:
            for aid in snap_hoje.planejadas:
                a = por_id.get(aid)
                if len(fila) < vagas and a in cands:
                    m, u = motivo_de(a)
                    por(a, ("aula nova — " + m) if est[a.id]["n"] == 0 else m, u)
        # 2) remarcadas (planejadas em outro dia e não lidas; trocadas que voltaram)
        while len(fila) < vagas:
            atrasadas_plano = [a for a in cands if a.id in pendentes]
            if not atrasadas_plano:
                break
            a = min(atrasadas_plano, key=lambda x: (pendentes[x.id], x.materia.ordem, x.ordem or 0))
            por(a, (f"trocada em {voltas_de[a.id].strftime('%d/%m')} — voltou à meta" if a.id in voltas_de
                    else f"planejada para {pendentes[a.id].strftime('%d/%m')} e não lida — remarcada"), True)
        # 3) aulas novas: pelo menos `novas_por_dia` (contando as novas já lidas hoje)
        novas_feitas = sum(1 for e in est.values() if e["n_antes"] == 0 and e["hoje"])
        while len(fila) < vagas and novas_feitas + sum(1 for f in fila if f["vez"] == 1) < novas_hoje:
            pool = [x for x in cands if est[x.id]["n"] == 0]
            if not pool:
                break
            a = rodizio(pool)
            por(a, "aula nova — " + motivo_rodizio(a), False)
        # 4) o resto da meta: releituras pelo prazo (quem está há mais tempo sem leitura); depois, novas
        while len(fila) < vagas and cands:
            rel = [x for x in cands if est[x.id]["n"] > 0 and dias_sem(x)]
            if rel:
                a = max(rel, key=lambda x: (dias_sem(x), -est[x.id]["n"]))
                por(a, *releitura(a))
            else:
                a = rodizio(cands)
                por(a, ("aula nova — " if est[a.id]["n"] == 0 else "") + motivo_rodizio(a), False)
        # segurança: aula que passaria do limite hoje entra mesmo acima da meta
        for a in [x for x in cands if est[x.id]["n"] > 0 and dias_sem(x) is not None and dias_sem(x) >= max_dias]:
            por(a, *releitura(a))

    # foto do plano do dia (uma vez por dia)
    if fase == "leitura" and aulas and esc and not snap_hoje:
        try:
            lidas_hoje = [a.id for a in aulas if est[a.id]["hoje"]]
            db.session.add(MetaDia(usuario=escopo(), data=h, meta_leituras=meta_hoje, restante_inicio=restante_inicio,
                                   planejadas_json=json.dumps(lidas_hoje + [f["aula"].id for f in fila]),
                                   meta_questoes=max(QUESTOES_MINIMO, int(cfg_num(cfg, "meta_questoes_dia")))))
            db.session.commit()
        except Exception:  # outro processo gravou ao mesmo tempo
            db.session.rollback()
    elif fase == "leitura" and snap_hoje and snap_hoje.meta_leituras is None:
        try:  # foto zerada (mudança de regra): passa a valer o plano atual
            lidas_hoje = [a.id for a in aulas if est[a.id]["hoje"]]
            snap_hoje.meta_leituras, snap_hoje.restante_inicio = meta_hoje, restante_inicio
            snap_hoje.planejadas_json = json.dumps(lidas_hoje + [f["aula"].id for f in fila])
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()

    # ritmo recalculado a cada leitura: o que falta (contando o resto da meta de hoje) ÷ dias que sobram depois de hoje
    faltam_hoje = max(0, meta_hoje - feitas_hoje) if fase == "leitura" else 0
    if fase == "leitura" and dias > 1:
        ritmo_vivo = max(0, restante - faltam_hoje) / (dias - 1)
    else:
        ritmo_vivo = float(faltam_hoje)
    total_necessario = alvo * len(aulas)
    # meta de amanhã pela mesma regra equilibrada, supondo a lista de hoje cumprida
    meta_amanha = 0
    if fase == "leitura" and dias > 1:
        n1 = {a.id: est[a.id]["n"] for a in aulas}
        u1 = {a.id: (est[a.id]["ultima"].date() if est[a.id]["ultima"] else None) for a in aulas}
        for f in fila:
            n1[f["aula"].id] += 1
            u1[f["aula"].id] = h
        meta_amanha = carga_do_dia(h + timedelta(days=1), n1, u1, adiadas, alvo, max_dias, limite, novas_min)[0]
    return {"ritmo_vivo": ritmo_vivo, "meta_vivo": math.ceil(ritmo_vivo - 1e-9) if ritmo_vivo else 0,
            "novas_hoje": novas_hoje, "releituras_hoje": max(0, meta_hoje - novas_hoje), "novas_min": novas_min,
            "faltam_hoje": faltam_hoje,"alvo": alvo, "pendentes": pendentes, "adiadas": adiadas, "excluidas": excluidas,
            "ajustes_hoje": ajustes_hoje, "limite": limite, "dias": max(dias, 0), "fase": fase, "max_dias": max_dias,
            "restante": restante, "meta_hoje": meta_hoje,
            "aulas_faltando": sum(1 for e in est.values() if e["n"] < alvo),
            "ritmo": (restante_inicio / dias) if fase == "leitura" and dias > 0 else 0,
            "meta_amanha": meta_amanha, "feitas_hoje": feitas_hoje, "fila": fila,
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
    fila_ids = {f["aula"].id for f in p["fila"]}
    adiadas = p["adiadas"]
    remarcar = [aid for aid, _ in sorted(p["pendentes"].items(), key=lambda kv: kv[1]) if aid not in fila_ids and aid in n]
    remarcar += [aid for aid, _ in sorted(adiadas.items(), key=lambda kv: kv[1]) if aid in n]
    novas_min = max(0, int(cfg_num(configs(), "novas_por_dia")))
    d = h + timedelta(days=1)
    while d <= limite:
        cands = [i for i in n if n[i] < alvo and not (i in adiadas and d < adiadas[i])]
        meta, obrig, novas = carga_do_dia(d, n, ult, adiadas, alvo, max_dias, limite, novas_min)
        if not meta or not cands:
            if not sum(max(0, alvo - n[i]) for i in n):
                break
            d += timedelta(days=1)
            continue
        escolhidas = []

        def pega(i):
            u = n[i] > 0 and ult[i] is not None and (d - ult[i]).days >= max_dias - 2
            escolhidas.append((por_id[i], n[i] + 1, u))
            proxima.setdefault(i, d)
            n[i] += 1
            ult[i] = d
            nonlocal_seq[0] += 1
            ult_mat[por_id[i].materia_id] = (d, nonlocal_seq[0])
            cands.remove(i)

        nonlocal_seq = [seq]

        def rodizio(pool):
            mid = min({por_id[x].materia_id for x in pool}, key=lambda m: ult_mat.get(m) or (date.min, 0))
            return min((x for x in pool if por_id[x].materia_id == mid),
                       key=lambda x: (n[x], ult[x] or date.min, por_id[x].ordem or 0, x))

        for i in obrig:  # 1) releituras que vencem
            if i in cands:
                pega(i)
        while len(escolhidas) < meta:  # 2) remarcadas
            rem = [i for i in remarcar if i in cands]
            if not rem:
                break
            remarcar.remove(rem[0])
            pega(rem[0])
        while len(escolhidas) < meta and sum(1 for _, v, _ in escolhidas if v == 1) < novas:  # 3) novas
            pool = [i for i in cands if n[i] == 0]
            if not pool:
                break
            pega(rodizio(pool))
        while len(escolhidas) < meta and cands:  # 4) resto: releituras pelo prazo, depois novas
            rel = [i for i in cands if n[i] > 0 and ult[i] and ult[i] < d]
            pega(max(rel, key=lambda x: ((d - ult[x]).days, -n[x])) if rel else rodizio(cands))
        for i in [x for x in cands if n[x] > 0 and ult[x] and (d - ult[x]).days >= max_dias]:
            pega(i)  # segurança: não passa do limite
        seq = nonlocal_seq[0]
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
    plano_aula = {}  # {aula_id: {vez: data prevista}}
    for d, itens in previstas.items():
        for a, vez, urg in itens:
            plano_aula.setdefault(a.id, {})[vez] = d
    return {"dias": cal, "marcos": marcos, "hoje": hoje().isoformat()}, proxima, plano_aula


def _escolha_admin():
    """Bateria escolhida pelo admin para hoje (por aluno): {"data", "caderno_id"}; vale só no dia."""
    try:
        d = json.loads(configs().get("bateria_escolha") or "null")
    except ValueError:
        d = None
    return d if isinstance(d, dict) and d.get("data") == hoje().isoformat() else None


def bateria_do_dia(plano=None):
    """Bateria diária de questões. Ordem de escolha:
      1) escolha do admin para hoje (um caderno, ou um critério: menor acerto / já estudada e nunca feita);
      2) item em que já houve questões hoje (não troca no meio do dia);
      3) JÁ ESTUDADA E NUNCA FEITA: caderno de um assunto cuja aula já foi lida e que o aluno nunca resolveu
         (aula lida hoje primeiro; depois a lida há mais tempo);
      4) item das aulas de hoje (planejadas) que tenha caderno de questões;
      5) rodízio: item com caderno, já lido, há mais tempo sem questões (o de ontem vai para o fim)."""
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

    # aulas de hoje: já lidas hoje + planejadas na meta
    if plano is None:
        plano = plano_do_dia()
    est = plano.get("est", {})
    lidas_hoje = [a for a in Aula.query.order_by(Aula.id).all() if est.get(a.id, {}).get("hoje")]
    aulas_hoje = lidas_hoje + [f["aula"] for f in plano.get("fila", []) if f["aula"] not in lidas_hoje]
    assuntos_hoje = {tp.id for a in aulas_hoje for tp in a.topicos}
    aula_do_grupo = {}
    for a in aulas_hoje:
        for tp in a.topicos:
            if tp.grupo:
                aula_do_grupo.setdefault((a.materia_id, tp.grupo), a)

    # o que já foi estudado: assuntos (e itens) de aulas com leitura — e quando foi a 1ª leitura
    estudado_em, grupo_estudado_em, lidos_hoje_top = {}, {}, set()
    for a in Aula.query.all():
        datas = [l.lida_em for l in a.leituras]
        if not datas:
            continue
        primeira = min(datas)
        for tp in a.topicos:
            if tp.id not in estudado_em or primeira < estudado_em[tp.id]:
                estudado_em[tp.id] = primeira
            if tp.grupo:
                k = (a.materia_id, tp.grupo)
                if k not in grupo_estudado_em or primeira < grupo_estudado_em[k]:
                    grupo_estudado_em[k] = primeira
            if max(datas) >= ini:
                lidos_hoje_top.add(tp.id)
                if tp.grupo:
                    lidos_hoje_top.add((a.materia_id, tp.grupo))

    def feito(c):
        return any(t.respondidas for t in c.tentativas)

    def acerto(c):
        r = sum(t.respondidas for t in c.tentativas)
        return (sum(t.acertos for t in c.tentativas) / r) if r else None

    def estudado(c):
        """Quando o assunto do caderno foi estudado (1ª leitura da aula), ou None."""
        if c.topico_id in estudado_em:
            return estudado_em[c.topico_id]
        if c.topico and c.topico.grupo:
            return grupo_estudado_em.get((c.materia_id, c.topico.grupo))
        return None

    def estudado_hoje(c):
        return c.topico_id in lidos_hoje_top or bool(c.topico and (c.materia_id, c.topico.grupo) in lidos_hoje_top)

    def ordenar_cadernos(cads):
        # nunca feitos primeiro; depois os do assunto da aula de hoje; depois os menos resolvidos
        return sorted(cads, key=lambda c: (feito(c), c.topico_id not in assuntos_hoje, len(c.tentativas), c.id))

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
            cads = ordenar_cadernos([c for c in m.cadernos if c.topico and c.topico.grupo == gr and c.total])
            itens.append({"materia": m, "grupo": gr, "chave": (m.id, gr), "ordem": (m.ordem, k),
                          "lido": gr in lidos, "ultimo": ultimo_item.get((m.id, gr)), "cadernos": cads,
                          "aula": aula_do_grupo.get((m.id, gr)), "motivo": ""})
    com_caderno = {i["chave"] for i in itens if i["cadernos"]}
    sem_caderno = []
    for a in aulas_hoje:
        grupos_a = [(a.materia_id, tp.grupo) for tp in a.topicos if tp.grupo]
        if not grupos_a:
            sem_caderno.append((a, "aula sem item do edital vinculado"))
        elif not any(gk in com_caderno for gk in grupos_a):
            sem_caderno.append((a, "nenhum caderno de questões vinculado a " + grupos_a[0][1]))
    ids_hoje = {c.id for i in itens if i["aula"] is not None for c in i["cadernos"]}
    base = {"meta": meta, "feitas": feitas, "acertos": acertos, "item": None, "alternativas": [],
            "escolha_admin": False, "aulas_sem_caderno": sem_caderno, "cadernos_hoje": ids_hoje, "modo_admin": None}
    if not itens:
        return base

    def chave(it):
        return (not it["cadernos"], not it["lido"], it["ultimo"] or datetime.min,
                ultimo_mat.get(it["materia"].id) or datetime.min, it["ordem"])

    ontem = dt0(h - timedelta(days=1))
    recente = lambda it: it["ultimo"] is not None and it["ultimo"] >= ontem  # feito ontem: vai para o fim
    ordenados = (sorted([i for i in itens if i["lido"] and not recente(i)], key=chave)
                 + sorted([i for i in itens if not i["lido"] and not recente(i)], key=chave)
                 + sorted([i for i in itens if recente(i)], key=chave))

    todos_cads = [c for i in itens for c in i["cadernos"]]

    def item_do_caderno(cad, motivo):
        gr = cad.topico.grupo if cad.topico and cad.topico.grupo else None
        base_it = next((i for i in itens if gr and i["chave"] == (cad.materia_id, gr)), None)
        if base_it:
            it = dict(base_it, cadernos=[cad] + [x for x in base_it["cadernos"] if x.id != cad.id])
        else:
            it = {"materia": cad.materia, "grupo": cad.topico.rotulo if cad.topico else cad.titulo,
                  "chave": (cad.materia_id, gr), "ordem": (cad.materia.ordem, 0), "lido": True,
                  "ultimo": None, "cadernos": [cad], "aula": None}
        it["motivo"] = motivo
        return it

    def estudada_nova():
        """Caderno de assunto já estudado e nunca resolvido (aula lida hoje primeiro; depois a mais antiga)."""
        cs = [c for c in todos_cads if not feito(c) and estudado(c)]
        if not cs:
            return None
        c = min(cs, key=lambda c: (not estudado_hoje(c), estudado(c), c.materia.ordem, c.id))
        quando = estudado(c)
        return item_do_caderno(c, "já estudada e nunca feita" + (" (aula lida hoje)" if estudado_hoje(c)
                                                               else f" (estudada em {quando.strftime('%d/%m')})"))

    def menor_acerto():
        cs = [(acerto(c), c) for c in todos_cads if acerto(c) is not None]
        if not cs:
            return None
        pct, c = min(cs, key=lambda x: (x[0], max(t.feita_em for t in x[1].tentativas), x[1].id))
        return item_do_caderno(c, f"menor acerto: {round(100 * pct)}% neste caderno")

    # prévia dos critérios (para o diálogo "Trocar bateria" mostrar o que cada um escolheria)
    _pv_e, _pv_m = estudada_nova(), menor_acerto()
    base["previa"] = {
        "estudada_nova": (_pv_e["cadernos"][0].titulo + " · " + _pv_e["motivo"]) if _pv_e else None,
        "menor_acerto": (_pv_m["cadernos"][0].titulo + " · " + _pv_m["motivo"]) if _pv_m else None,
        "por_que": {"estudada_nova": "nenhum caderno vinculado a um assunto de aula já lida que ainda não tenha sido feito",
                    "menor_acerto": "nenhum caderno resolvido ainda"},
    }
    item = None
    esc = _escolha_admin()
    modo = esc.get("modo") if esc else None
    if modo and esc.get("caderno_id"):
        fixo = db.session.get(Caderno, esc["caderno_id"])  # critério resolvido na hora da escolha: não muda no dia
        if fixo:
            item = item_do_caderno(fixo, esc.get("motivo") or modo.replace("_", " "))
            base["modo_admin"] = modo
    elif modo == "menor_acerto":
        item = menor_acerto()
        base["modo_admin"] = modo
    elif modo == "estudada_nova":
        item = estudada_nova()
        base["modo_admin"] = modo
    if item is not None:
        item["motivo"] = "escolhida pelo admin — " + item["motivo"]
        base["escolha_admin"] = True
        base["modo_ok"] = True  # o critério achou um caderno
    cad = db.session.get(Caderno, esc.get("caderno_id")) if esc and esc.get("caderno_id") and not modo else None
    if cad and item is None:
        gr = cad.topico.grupo if cad.topico and cad.topico.grupo else None
        base_it = next((i for i in itens if gr and i["chave"] == (cad.materia_id, gr)), None)
        if base_it:
            item = dict(base_it, cadernos=[cad] + [x for x in base_it["cadernos"] if x.id != cad.id])
        else:
            item = {"materia": cad.materia, "grupo": cad.topico.rotulo if cad.topico else cad.titulo,
                    "chave": (cad.materia_id, gr), "ordem": (cad.materia.ordem, 0), "lido": True,
                    "ultimo": None, "cadernos": [cad], "aula": None}
        item["motivo"] = "escolhida pelo admin para hoje"
        base["escolha_admin"] = True
    if item is None:
        hoje_item = next(((t.materia_id, t.caderno.topico.grupo) for t in tents if t.feita_em >= ini and t.caderno
                          and t.caderno.topico and t.caderno.topico.grupo), None)
        item = next((dict(i, motivo="você já começou este item hoje") for i in itens if i["chave"] == hoje_item), None)
    if item is None:
        item = estudada_nova()
    if item is None:
        do_dia = [i for i in itens if i["aula"] is not None and i["cadernos"]]
        do_dia.sort(key=lambda i: (aulas_hoje.index(i["aula"]), chave(i)))
        if do_dia:
            item = dict(do_dia[0], motivo=f"da aula de hoje: {do_dia[0]['aula'].titulo}")
    if item is None:
        item = dict(ordenados[0], motivo="rodízio dos itens do edital")
    alternativas = [i for i in ordenados if i["chave"] != item["chave"]]
    alternativas = sorted(alternativas, key=lambda i: i["aula"] is None)[:3]
    return dict(base, item=item, alternativas=alternativas)


def resumo_dia():
    cfg = configs()
    h = hoje()
    tents_hoje = Tentativa.query.filter(Tentativa.feita_em >= dt0(h)).all()
    rec = Tentativa.query.filter(Tentativa.feita_em >= dt0(h - timedelta(days=6))).all()
    r_resp = sum(t.respondidas for t in rec)
    return {
        "plano": (pl := plano_do_dia()),
        "bateria": bateria_do_dia(pl),
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
        except Exception as e:  # noqa: BLE001
            db.session.rollback()  # no Postgres, um erro "trava" a transação e derrubaria a página
            registrar_erro(e)
            g.humor_atual = "neutro"
    return g.humor_atual


# ---- Notificações (Web Push) --------------------------------------------- #
import push as PUSH  # noqa: E402

FRASES_PUSH = {
    "furioso": "Tatti está FURIOSA 😡",
    "bravo": "Tatti está brava 😠",
    "neutro": "Tatti está de olho 👀",
    "feliz": "Tatti está feliz 🙂",
    "radiante": "Tatti está radiante 🤩",
}


def mensagem_do_dia():
    """Título, texto e humor da notificação. Retorna None se a meta do dia já foi cumprida."""
    hm = calcular_humor()
    p, b = hm["dia"]["plano"], hm["dia"]["bateria"]
    faltam_leit = max(0, p["meta_hoje"] - p["feitas_hoje"]) if p["fase"] == "leitura" else 0
    faltam_q = max(0, b["meta"] - b["feitas"])
    partes = []
    if faltam_leit:
        partes.append(f"{faltam_leit} leitura{'s' if faltam_leit > 1 else ''} de aula")
    if faltam_q:
        partes.append(f"{'a bateria de ' + str(b['meta']) if faltam_q == b['meta'] else str(faltam_q)} questões")
    if not partes:
        return None
    corpo = "Faltam " + " e ".join(partes) + " hoje."
    if p["atrasadas"]:
        corpo += f" {len(p['atrasadas'])} aula(s) parada(s) há mais de {p['max_dias']} dias!"
    return {"titulo": FRASES_PUSH.get(hm["humor"], "Tatti"), "corpo": corpo, "humor": hm["humor"],
            "pendentes": faltam_leit + (1 if faltam_q else 0)}


def enviar_push(insc, msg):
    dados = {"titulo": msg["titulo"], "corpo": msg["corpo"], "url": "/",
             "icone": url_for("static", filename=f"icones/{msg['humor']}-192.png"),
             "imagem": url_for("static", filename=f"avatar/{msg['humor']}.png"),
             "badge": url_for("static", filename="icones/badge-96.png"),
             "pendentes": msg.get("pendentes", 1), "tag": "tatti-dia"}
    try:
        PUSH.enviar(insc, dados, chaves_vapid())
        return True
    except PUSH.InscricaoInvalida:
        db.session.delete(insc)
        db.session.commit()
        return False


def chaves_vapid():
    priv, pub = db.session.get(Config, "vapid_privada"), db.session.get(Config, "vapid_publica")
    if not priv or not pub:
        pem, chave_pub = PUSH.gerar_chaves()
        db.session.merge(Config(chave="vapid_privada", valor=pem))
        db.session.merge(Config(chave="vapid_publica", valor=chave_pub))
        db.session.commit()
        priv, pub = db.session.get(Config, "vapid_privada"), db.session.get(Config, "vapid_publica")
    return {"privada": priv.valor, "publica": pub.valor}


def verificar_lembretes():
    """Roda a cada minuto: envia o lembrete do dia a quem chegou no horário (uma vez por dia)."""
    agora_ = agora()
    hhmm, d = agora_.strftime("%H:%M"), agora_.date()
    pend = [i for i in PushInscricao.query.all() if (i.horario or "19:00") <= hhmm and i.ultimo_envio != d]
    if not pend:
        return
    for i in pend:
        # marca como enviado de forma atômica (evita duplicar com dois workers)
        n = (PushInscricao.query.filter(PushInscricao.id == i.id,
                                        (PushInscricao.ultimo_envio.is_(None)) | (PushInscricao.ultimo_envio != d))
             .update({"ultimo_envio": d}, synchronize_session=False))
        db.session.commit()
        if not n:
            continue
        u = db.session.get(Usuario, i.usuario_id) if i.usuario_id else None
        g.escopo = u.login if u and not u.admin else USUARIO_PRINCIPAL
        db.session.expire_all()
        msg = mensagem_do_dia()
        if msg:
            enviar_push(i, msg)
    g.pop("escopo", None)


_lembretes = {"t": None}


def _loop_lembretes():
    import time as _t
    while True:
        try:
            with app.app_context(), app.test_request_context("/"):
                verificar_lembretes()
        except Exception as e:  # noqa: BLE001
            app.logger.warning("lembretes: %s", e)
        try:  # leituras com IA interrompidas (ex.: deploy no meio) continuam de onde pararam
            with app.app_context():
                retomar_leituras()
        except Exception as e:  # noqa: BLE001
            app.logger.warning("retomar leituras: %s", e)
        finally:
            try:
                with app.app_context():
                    db.session.remove()
            except Exception:  # noqa: BLE001
                pass
        _t.sleep(60)


def iniciar_thread_lembretes():
    """Chamado pelo gunicorn em cada worker (gunicorn.conf.py) e, por garantia, na 1ª requisição."""
    if _lembretes["t"] is None or not _lembretes["t"].is_alive():
        _lembretes["t"] = threading.Thread(target=_loop_lembretes, daemon=True)
        _lembretes["t"].start()


@app.before_request
def iniciar_lembretes():
    iniciar_thread_lembretes()


@app.route("/sw.js")
def service_worker():
    resp = send_file(os.path.join(BASE_DIR, "static", "sw.js"), mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.get("/api/push/chave")
def api_push_chave():
    return jsonify(ok=True, chave=chaves_vapid()["publica"])


@app.post("/api/push/inscrever")
def api_push_inscrever():
    d = request.get_json(silent=True) or {}
    sub = d.get("inscricao") or {}
    chaves = sub.get("keys") or {}
    if not sub.get("endpoint") or not chaves.get("p256dh") or not chaves.get("auth"):
        return jsonify(ok=False, erro="inscrição inválida"), 400
    horario = d.get("horario") or "19:00"
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", horario):
        horario = "19:00"
    i = PushInscricao.query.filter_by(endpoint=sub["endpoint"]).first() or PushInscricao(endpoint=sub["endpoint"])
    i.p256dh, i.auth, i.horario = chaves["p256dh"], chaves["auth"], horario
    i.usuario_id = usuario_atual().id if usuario_atual() else None
    i.aparelho = (request.headers.get("User-Agent") or "")[:300]
    if i.ultimo_envio is None and horario <= agora().strftime("%H:%M"):
        i.ultimo_envio = hoje()  # ativou depois do horário: começa amanhã
    db.session.add(i)
    db.session.commit()
    return jsonify(ok=True, horario=i.horario)


@app.post("/api/push/horario")
def api_push_horario():
    d = request.get_json(silent=True) or {}
    i = PushInscricao.query.filter_by(endpoint=d.get("endpoint") or "").first_or_404()
    h = d.get("horario") or ""
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", h):
        return jsonify(ok=False), 400
    i.horario = h
    if h > agora().strftime("%H:%M") and i.ultimo_envio == hoje():
        i.ultimo_envio = None  # novo horário ainda não passou hoje
    db.session.commit()
    return jsonify(ok=True, horario=h)


@app.post("/api/push/estado")
def api_push_estado():
    d = request.get_json(silent=True) or {}
    i = PushInscricao.query.filter_by(endpoint=d.get("endpoint") or "").first()
    return jsonify(ok=True, inscrito=bool(i), horario=i.horario if i else None)


@app.post("/api/push/teste")
def api_push_teste():
    d = request.get_json(silent=True) or {}
    i = PushInscricao.query.filter_by(endpoint=d.get("endpoint") or "").first_or_404()
    msg = mensagem_do_dia() or {"titulo": FRASES_PUSH["radiante"], "corpo": "Metas de hoje cumpridas. Orgulho de você!",
                                "humor": "radiante", "pendentes": 0}
    ok = enviar_push(i, msg)
    return jsonify(ok=ok)


@app.post("/api/push/sair")
def api_push_sair():
    d = request.get_json(silent=True) or {}
    PushInscricao.query.filter_by(endpoint=d.get("endpoint") or "").delete()
    db.session.commit()
    return jsonify(ok=True)


@app.get("/api/pendentes")
def api_pendentes():
    msg = mensagem_do_dia()
    return jsonify(ok=True, pendentes=msg["pendentes"] if msg else 0, humor=humor_atual())


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


app.permanent_session_lifetime = timedelta(days=30)
LIVRES = ("login", "static", "manifest", "service_worker")


def usuario_atual():
    if "usuario_atual" not in g:
        uid = session.get("uid")
        g.usuario_atual = db.session.get(Usuario, uid) if uid else None
    return g.usuario_atual


def _logins_admin():
    if "logins_admin" not in g:
        g.logins_admin = {u.login for u in Usuario.query.filter_by(admin=True).all()}
    return g.logins_admin


def _ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "")[:64]


def registrar(evento, usuario=None, caminho=None, detalhe=None):
    if evento != "falha" and usuario and (usuario in _logins_admin()):
        return  # o que o admin faz não entra nos logs
    try:
        db.session.add(LogAcesso(quando=agora(), usuario=(usuario or "")[:60], evento=evento, detalhe=(detalhe or "")[:300] or None,
                                 caminho=(caminho or request.full_path.rstrip("?"))[:300], ip=_ip(),
                                 aparelho=(request.headers.get("User-Agent") or "")[:300]))
        db.session.commit()
    except Exception:  # log nunca derruba a página
        db.session.rollback()


def _login_atual():
    u = usuario_atual()
    return u.login if u else None


def acao(texto):
    """Registra uma ação de estudo do usuário no log."""
    u = usuario_atual()
    registrar("acao", u.login if u else "", request.path, texto)


@app.before_request
def exigir_login():
    if request.endpoint in LIVRES:
        return None
    u = usuario_atual()
    if u is None:
        if request.path.startswith("/api/"):
            return jsonify(ok=False, erro="faça login"), 401
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    if request.method == "GET" and not request.path.startswith("/api/") and request.endpoint not in ("aula_pdf",):
        registrar("pagina", u.login)
    return None


def so_admin(f):
    from functools import wraps

    @wraps(f)
    def w(*a, **k):
        u = usuario_atual()
        if not u or not u.admin:
            abort(403)
        return f(*a, **k)
    return w


@app.context_processor
def contexto_usuario():
    u = usuario_atual() if request.endpoint not in ("static",) else None
    alunos = Usuario.query.filter(Usuario.admin.isnot(True)).order_by(Usuario.login).all() if u and u.admin else []
    return {"usuario": u, "alunos": alunos, "vendo": escopo() if u else None, "eh_admin": bool(u and u.admin),
            "ia_gemini": IA.gemini_disponivel(), "ia_claude": IA.claude_disponivel(), "escopos": ESCOPOS}


@app.post("/ver-como")
@so_admin
def ver_como():
    login_ = (request.form.get("login") or "").strip().lower()
    if Usuario.query.filter(Usuario.login == login_, Usuario.admin.isnot(True)).first():
        session["ver_como"] = login_
    return redirect(request.referrer or url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        login_ = request.form.get("usuario", "").strip().lower()
        u = Usuario.query.filter_by(login=login_).first()
        if u and u.senha_ok(request.form.get("senha")):
            session.clear()
            session["uid"] = u.id
            session.permanent = True
            registrar("login", u.login, "/login")
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        registrar("falha", login_ or "(vazio)", "/login")
        flash("Usuário ou senha incorretos.", "erro")
    return render_template("login.html")


@app.route("/logout")
def logout():
    u = usuario_atual()
    if u:
        registrar("logout", u.login, "/logout")
    session.clear()
    return redirect(url_for("login"))


def _fmt_tempo(seg):
    seg = int(seg or 0)
    h, r = divmod(seg, 3600)
    m = r // 60
    return f"{h}h{m:02d}" if h else (f"{m} min" if m else f"{seg} s")


app.jinja_env.filters["tempo"] = _fmt_tempo


@app.route("/logs")
@so_admin
def logs():
    aba = request.args.get("aba", "acessos")
    if aba == "erros":
        erros = ErroApp.query.order_by(ErroApp.quando.desc()).limit(100).all()
        return render_template("logs.html", aba=aba, erros=erros, usuarios=[], f_usuario="", f_evento="")
    f_usuario = request.args.get("usuario", "")
    f_evento = request.args.get("evento", "")
    usuarios = Usuario.query.filter(Usuario.admin.isnot(True)).order_by(Usuario.login).all()
    if aba == "estudo":
        try:
            de = date.fromisoformat(request.args.get("de", ""))
        except ValueError:
            de = hoje() - timedelta(days=6)
        try:
            ate = date.fromisoformat(request.args.get("ate", ""))
        except ValueError:
            ate = hoje()
        ini, fim = dt0(de), dt0(ate + timedelta(days=1))
        qs = SessaoPdf.query.filter(SessaoPdf.inicio >= ini, SessaoPdf.inicio < fim)
        ql = Leitura.query.execution_options(todos_usuarios=True).filter(Leitura.lida_em >= ini, Leitura.lida_em < fim)
        qa = LogAcesso.query.filter(LogAcesso.evento == "acao", LogAcesso.quando >= ini, LogAcesso.quando < fim)
        if f_usuario:
            qs, ql, qa = qs.filter_by(usuario=f_usuario), ql.filter_by(usuario=f_usuario), qa.filter_by(usuario=f_usuario)
        sessoes = qs.all()
        leituras = ql.all()
        por_pdf = {}
        for sp in sessoes:
            k = sp.aula_id
            d = por_pdf.setdefault(k, {"titulo": sp.titulo, "materia": sp.materia, "segundos": 0, "sessoes": 0,
                                       "ultima": sp.fim, "leituras": 0, "usuarios": set()})
            d["segundos"] += sp.segundos or 0
            d["sessoes"] += 1
            d["usuarios"].add(sp.usuario)
            d["ultima"] = max(d["ultima"], sp.fim or sp.inicio)
        for l in leituras:
            a = l.aula
            d = por_pdf.setdefault(l.aula_id, {"titulo": a.titulo if a else "?", "materia": a.materia.nome if a else "",
                                               "segundos": 0, "sessoes": 0, "ultima": l.lida_em, "leituras": 0, "usuarios": set()})
            d["leituras"] += 1
            if l.usuario:
                d["usuarios"].add(l.usuario)
            d["ultima"] = max(d["ultima"], l.lida_em)
        pdfs = sorted(por_pdf.values(), key=lambda d: d["ultima"], reverse=True)
        por_dia = {}
        for sp in sessoes:
            por_dia.setdefault(sp.inicio.date(), {"segundos": 0, "leituras": 0})["segundos"] += sp.segundos or 0
        for l in leituras:
            por_dia.setdefault(l.lida_em.date(), {"segundos": 0, "leituras": 0})["leituras"] += 1
        dias = [(d, por_dia.get(d, {"segundos": 0, "leituras": 0})) for d in
                (ate - timedelta(days=k) for k in range((ate - de).days + 1))]
        acoes = qa.order_by(LogAcesso.quando.desc()).limit(300).all()
        return render_template("logs.html", aba=aba, usuarios=usuarios, f_usuario=f_usuario, de=de, ate=ate,
                               pdfs=pdfs, dias=dias, acoes=acoes,
                               total_seg=sum(sp.segundos or 0 for sp in sessoes), total_leit=len(leituras))
    q = LogAcesso.query
    if f_usuario:
        q = q.filter_by(usuario=f_usuario)
    if f_evento:
        q = q.filter_by(evento=f_evento)
    registros = q.order_by(LogAcesso.quando.desc(), LogAcesso.id.desc()).limit(500).all()
    resumo = []
    for u in usuarios:
        ult = (LogAcesso.query.filter_by(usuario=u.login, evento="login").order_by(LogAcesso.quando.desc()).first())
        ult_atv = (LogAcesso.query.filter_by(usuario=u.login).order_by(LogAcesso.quando.desc()).first())
        resumo.append({"u": u, "ultimo_login": ult.quando if ult else None, "ultima_atividade": ult_atv.quando if ult_atv else None,
                       "logins": LogAcesso.query.filter_by(usuario=u.login, evento="login").count()})
    falhas = LogAcesso.query.filter_by(evento="falha").count()
    return render_template("logs.html", aba="acessos", registros=registros, usuarios=usuarios, resumo=resumo, falhas=falhas,
                           f_usuario=f_usuario, f_evento=f_evento)


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
    hm = calcular_humor()
    ajuste = None
    if eh_admin():
        p = hm.get("dia", {}).get("plano") if isinstance(hm.get("dia"), dict) else None
        if p and p["fase"] == "leitura":
            na_fila = {f["aula"].id for f in p["fila"]}

            def aviso(a):
                st = p["est"].get(a.id, {})
                if a.id in na_fila:
                    return "já está na meta"
                if st.get("n", 0) >= p["alvo"]:
                    return f"já lida {p['alvo']}x"
                if st.get("hoje"):
                    return "lida hoje"
                if a.id in p["adiadas"]:
                    return f"trocada · voltaria em {p['adiadas'][a.id].strftime('%d/%m')}"
                if a.id in p["excluidas"]:
                    return "excluída hoje"
                return ""
            bloqueia = ("já está na meta", f"já lida {p['alvo']}x")
            opcoes = []
            for m in materias:
                for a in sorted(m.aulas, key=lambda x: (x.ordem or 0, x.id)):
                    av = aviso(a)
                    opcoes.append({"aula": a, "materia_id": m.id, "aviso": av, "ok": av not in bloqueia})
            por_id = {a.id: a for m in materias for a in m.aulas}
            ajuste = {"opcoes": opcoes, "por_id": por_id,
                      "hoje": [aj for aj in p["ajustes_hoje"]],
                      "adiadas": sorted(((por_id[i], d) for i, d in p["adiadas"].items() if i in por_id), key=lambda x: x[1])}
    cadernos_admin = (Caderno.query.join(Materia).order_by(Materia.ordem, Caderno.titulo).all()
                      if eh_admin() else [])
    return render_template("index.html", trilha=trilha, h=hm, xp=xp, nv=nivel(xp), ajuste=ajuste,
                           cadernos_admin=cadernos_admin,
                           hoje_str=f"{dias_sem[h0.weekday()]}, {h0.strftime('%d/%m')}",
                           streak=ofensiva(), tot_aulas=tot_aulas, tot_lidas=tot_lidas)


@app.post("/materia/nova")
@so_admin
def materia_nova():
    """Admin cria uma matéria nova (fora da lista do edital), com assuntos opcionais (um por linha)."""
    f = request.form
    nome = sem_nulos(f.get("nome", "")).strip()[:120]
    if not nome:
        flash("Dê um nome para a matéria.", "erro")
        return redirect(url_for("index"))
    if any(m.nome.strip().lower() == nome.lower() for m in Materia.query.all()):
        flash(f"Já existe uma matéria chamada “{nome}”.", "erro")
        return redirect(url_for("index"))
    cor = (f.get("cor") or "").strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", cor):
        cor = "#3fbf74"
    try:
        q = int(f.get("questoes_prova") or 0) or None
    except ValueError:
        q = None
    ordem = (db.session.query(db.func.max(Materia.ordem)).scalar() or 0) + 1
    m = Materia(nome=nome, info=sem_nulos(f.get("info", "")).strip()[:200] or None,
                questoes_prova=q, cor=cor, ordem=ordem)
    assuntos = list(dict.fromkeys(a.strip()[:200] for a in sem_nulos(f.get("assuntos", "")).splitlines() if a.strip()))
    m.topicos = [Topico(nome=a, ordem=k, personalizado=True) for k, a in enumerate(assuntos)]
    db.session.add(m)
    db.session.commit()
    flash(f"Matéria “{nome}” criada. Agora é só enviar as aulas em PDF.", "ok")
    return redirect(url_for("materia", mid=m.id) + "#aulas")


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
        t = sem_nulos(nome_padrao(nome))
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


@app.route("/aula/<int:aid>/ler-pdf")
def aula_tela_cheia(aid):
    a = db.get_or_404(Aula, aid)
    return render_template("aula_cheia.html", a=a, m=a.materia)


@app.post("/api/aula/<int:aid>/sessao")
def api_sessao_inicio(aid):
    a = db.get_or_404(Aula, aid)
    u = usuario_atual()
    if u and u.admin:
        return jsonify(ok=True, sessao=None)  # admin não é medido
    sp = SessaoPdf(usuario=u.login if u else "", aula_id=a.id, titulo=a.titulo[:200], materia=a.materia.nome,
                   inicio=agora(), fim=agora(), segundos=0)
    db.session.add(sp)
    db.session.commit()
    return jsonify(ok=True, sessao=sp.id)


@app.post("/api/sessao/<int:sid>/ping")
def api_sessao_ping(sid):
    sp = db.get_or_404(SessaoPdf, sid)
    u = usuario_atual()
    if u and sp.usuario != u.login:
        abort(403)
    d = request.get_json(silent=True, force=True) or {}
    try:
        seg = max(0, min(90, int(d.get("seg", 0))))  # no máximo 90 s por aviso (aba aberta e visível)
    except (TypeError, ValueError):
        seg = 0
    sp.segundos = (sp.segundos or 0) + seg
    sp.fim = agora()
    db.session.commit()
    return jsonify(ok=True, total=sp.segundos)


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
    db.session.add(Leitura(aula_id=a.id, numero=n, lida_em=agora(), usuario=_login_atual()))
    db.session.commit()
    acao(f"Concluiu a {n}ª leitura: {a.titulo} ({a.materia.nome})")
    if eh_admin():
        flash("Modo administrador: a leitura foi registrada como teste e não conta nas metas nem no humor da Tatti.", "info")
    else:
        flash(f"{n}ª leitura registrada! +{20 if n == 1 else 10} XP", "ok")
    return redirect(url_for("aula_ver", aid=aid))


@app.post("/aula/<int:aid>/desfazer")
def aula_desfazer(aid):
    a = db.get_or_404(Aula, aid)
    if a.leituras:
        n = len(a.leituras)
        db.session.delete(a.leituras[-1])
        db.session.commit()
        acao(f"Desfez a {n}ª leitura: {a.titulo}")
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
    acao(f"Excluiu a aula: {a.titulo} ({a.materia.nome})")
    Leitura.query.filter_by(aula_id=a.id).delete(synchronize_session=False)
    db.session.expire(a, ["leituras"])
    db.session.delete(a)
    db.session.commit()
    flash("Aula excluída.", "info")
    return redirect(url_for("materia", mid=mid) + "#aulas")


# ---- Questões ------------------------------------------------------------- #
RX_CONTROLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sem_nulos(t):
    """Remove caracteres de controle (ex.: \\x00) que alguns PDFs trazem e o Postgres recusa."""
    return RX_CONTROLE.sub("", str(t or ""))


def ler_questoes_sem_ia(c):
    """Leitor gratuito (por regras) sobre o texto do PDF: {n: questão} para todos os números do gabarito."""
    gab = c.gabarito or {}
    try:
        paginas = [p.extract_text() or "" for p in PdfReader(io.BytesIO(c.pdf_questoes)).pages]
    except Exception:
        paginas = []
    if QZ.eh_tec("\n".join(paginas)):  # caderno do TEC Concursos: formato próprio
        try:
            res = QZ.extrair_tec(paginas, list(gab))
        except Exception as e:  # noqa: BLE001
            registrar_erro(e)
            res = None
        if res:
            for n in gab:
                res.setdefault(n, {"fonte": "", "enunciado": "", "alternativas": {}, "apoio": "", "ok": False})
            return res
    try:
        texto = QZ.cortar_gabarito(QZ.limpar_paginas(paginas), parse_gabarito_texto)
        res = QZ.extrair_questoes(texto, list(gab))
    except Exception as e:  # noqa: BLE001  leitor simples falhou: segue só com as letras
        registrar_erro(e)
        res = {}
    for n in gab:
        res.setdefault(n, {"fonte": "", "enunciado": "", "alternativas": {}, "apoio": "", "ok": False})
    return res


def precisa_ia(q):
    """Questão do leitor gratuito que ainda precisa da IA (incompleta ou sem o texto de apoio que cita)."""
    return (not q.get("ok") or not QZ.completa(q.get("alternativas"), q.get("enunciado"))
            or (not q.get("apoio") and IA.RX_PEDE_APOIO.search(q.get("enunciado") or "")))


def montar_questoes(c, extra=None):
    """(Re)constrói as questões do caderno a partir do PDF. Retorna quantas ficaram completas."""
    Questao.query.filter_by(caderno_id=c.id).delete()
    gab = c.gabarito
    if not gab:
        db.session.commit()
        return 0
    if not c.pdf_questoes:  # sem PDF: questões só com as letras, uma por número do gabarito
        for n in gab:
            db.session.add(Questao(caderno_id=c.id, numero=n, enunciado="", alternativas_json="{}", ok=False))
        db.session.commit()
        return 0
    res = ler_questoes_sem_ia(c)
    for n, q in (extra or {}).items():  # leitura da IA tem prioridade
        if n in gab and (q["ok"] or not res.get(n, {}).get("ok")):
            if not q.get("apoio") and res.get(n, {}).get("apoio"):  # IA leu sem o texto de apoio: mantém o do leitor
                q = dict(q, apoio=res[n]["apoio"])
            res[n] = q
    for n, q in res.items():
        alts = {k: sem_nulos(v) for k, v in (q.get("alternativas") or {}).items()}
        db.session.add(Questao(caderno_id=c.id, numero=n, fonte=sem_nulos(q.get("fonte"))[:300], apoio=sem_nulos(q.get("apoio")),
                               enunciado=sem_nulos(q.get("enunciado")), alternativas_json=json.dumps(alts, ensure_ascii=False),
                               ok=q.get("ok", False)))
    db.session.commit()
    return sum(1 for q in res.values() if q["ok"])


# ---- Leitura com IA (fila em segundo plano) ------------------------------- #
# Gemini é o leitor principal (barato). O Claude (pago) só entra quando o admin AUTORIZA no caderno.
# O que já foi lido fica salvo em ia_parcial: se o servidor reiniciar ou a IA falhar, a leitura
# continua de onde parou, sem pagar de novo pelo que já foi lido.
_fila_ia = queue.Queue()
_trabalhador = {"t": None}
IA_TRAVADA = timedelta(minutes=15)  # sem sinal de vida por mais que isso = leitura interrompida


def _carregar_parcial(c):
    try:
        return {int(k): v for k, v in json.loads(c.ia_parcial or "{}").items()}
    except Exception:  # noqa: BLE001
        return {}


def _questoes_do_banco(c, so_ok=True):
    res = {}
    for q in Questao.query.filter_by(caderno_id=c.id).all():
        if q.ok or not so_ok:
            res[q.numero] = {"fonte": q.fonte or "", "apoio": q.apoio or "", "enunciado": q.enunciado or "",
                             "alternativas": q.alternativas, "ok": bool(q.ok)}
    return res


def _hash_pdf(c):
    if not c.pdf_hash and c.pdf_questoes:
        c.pdf_hash = hashlib.sha256(c.pdf_questoes).hexdigest()
        db.session.commit()
    return c.pdf_hash


def _do_mesmo_pdf(c):
    """Questões já lidas pela IA de outro caderno com exatamente o mesmo PDF (sem custo)."""
    h = _hash_pdf(c)
    if not h:
        return {}
    for o in Caderno.query.filter(Caderno.pdf_hash == h, Caderno.id != c.id,
                                  Caderno.ia_status.in_(("ok", "autorizar"))).all():
        res = {n: q for n, q in _questoes_do_banco(o).items() if n in c.gabarito}
        if res:
            return res
    return {}


def _fmt_tokens(n):
    n = int(n or 0)
    return f"{n / 1000:.0f} mil tokens" if n >= 1000 else f"{n} tokens"


def _processar_ia(cid):
    c = db.session.get(Caderno, cid)
    if not c or not c.pdf_questoes:
        return
    if c.ia_status == "lendo" and c.ia_batida and agora() - c.ia_batida < IA_TRAVADA:
        return  # outro processo já está lendo este caderno
    if c.ia_claude_ok and IA.claude_disponivel():
        prov = "claude"
    elif IA.gemini_disponivel():
        prov = "gemini"
    else:
        c.ia_status = "autorizar" if IA.claude_disponivel() else "erro"
        c.ia_msg = ("O Gemini não está configurado (falta GEMINI_API_KEY no Railway). "
                    "Para ler com o Claude (pago), o administrador precisa autorizar.")
        db.session.commit()
        return
    sem_gabarito = not c.gabarito
    c.ia_status, c.ia_provedor, c.ia_inicio, c.ia_batida = "lendo", prov, agora(), agora()
    c.ia_msg = f"Começando a leitura com {IA.nome(prov)}…"
    db.session.commit()
    uso = {}
    parcial = _carregar_parcial(c)

    def batida():
        try:
            Caderno.query.filter_by(id=cid).update({"ia_batida": agora()})
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()

    def progresso(k, total):
        cc = db.session.get(Caderno, cid)
        cc.ia_msg = (f"{IA.nome(prov)}: " + (k if isinstance(k, str) else f"lendo parte {k} de {total}…"))[:300]
        cc.ia_batida = agora()
        db.session.commit()

    def salvar(lidas):
        junto = dict(parcial)
        for n, q in lidas.items():
            if q.get("ok") or n not in junto:
                junto[n] = q
        cc = db.session.get(Caderno, cid)
        cc.ia_parcial = json.dumps({str(n): q for n, q in junto.items()}, ensure_ascii=False)
        cc.ia_batida = agora()
        cc.ia_tokens = uso.get("entrada", 0) + uso.get("saida", 0)
        db.session.commit()

    res = {}
    try:
        if sem_gabarito:
            res, gab = IA.extrair(c.pdf_questoes, progresso, com_gabarito=True, provedor=prov, batida=batida, uso=uso)
            nums = [n for n in res if res[n]["ok"]]
            # aceita se cobrir pelo menos metade das questões lidas (evita respostas soltas)
            if len(gab) < 3 or (nums and len([n for n in nums if n in gab]) < len(nums) / 2):
                c = db.session.get(Caderno, cid)
                c.ia_status = "erro"
                c.ia_msg = "Não encontrei o gabarito escrito no PDF. Digite-o em “Corrigir gabarito”."
                c.ia_tokens = uso.get("entrada", 0) + uso.get("saida", 0)
                db.session.commit()
                return
            c = db.session.get(Caderno, cid)
            c.gabarito = gab
            db.session.commit()
        else:
            escopo = c.ia_escopo or ESCOPO_PADRAO
            if not parcial and prov == "gemini" and escopo != "tudo":
                parcial = _do_mesmo_pdf(c)  # o mesmo PDF já foi lido em outro caderno: reaproveita
                if parcial:
                    salvar({})
            somente = None
            if escopo != "tudo" and not IA.modo_completo():
                # economia: a IA só lê o que o leitor gratuito (e as leituras anteriores) não montaram
                base = ler_questoes_sem_ia(c)
                for n, q in parcial.items():
                    if q.get("ok"):
                        base[n] = q
                somente = [n for n in c.gabarito if precisa_ia(base.get(n, {}))]
            if somente == []:
                res = {}
            else:
                res = IA.extrair(c.pdf_questoes, progresso, esperadas=list(c.gabarito), somente=somente,
                                 provedor=prov, salvar=salvar, batida=batida, uso=uso,
                                 todas_paginas=escopo != "alvo")
        _concluir_ia(cid, prov, parcial, res, uso)
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        c = db.session.get(Caderno, cid)
        # monta o que já foi lido, para o caderno ficar usável
        try:
            ja = _carregar_parcial(c)
            if ja and c.gabarito:
                montar_questoes(c, extra=ja)
                c = db.session.get(Caderno, cid)
        except Exception:  # noqa: BLE001
            db.session.rollback()
            c = db.session.get(Caderno, cid)
        c.ia_tokens = uso.get("entrada", 0) + uso.get("saida", 0)
        motivo = str(e)[:150] if isinstance(e, IA.IAFalhou) else f"{e.__class__.__name__}: {str(e)[:120]}"
        if prov == "gemini":
            c.ia_status = "autorizar"
            c.ia_msg = (f"O Gemini parou: {motivo}. O que já foi lido está salvo. "
                        "Tente de novo com o Gemini (continua de onde parou) ou autorize o Claude (pago).")[:300]
        else:
            c.ia_status = "erro"
            c.ia_msg = f"Erro na leitura com o Claude: {motivo}. O que já foi lido está salvo."[:300]
        db.session.commit()


def _concluir_ia(cid, prov, parcial, res, uso):
    c = db.session.get(Caderno, cid)
    tudo = dict(parcial)
    for n, q in (res or {}).items():
        if q.get("ok") or n not in tudo:
            tudo[n] = q
    ok = montar_questoes(c, extra=tudo)
    c = db.session.get(Caderno, cid)
    c.ia_parcial = json.dumps({str(n): q for n, q in tudo.items()}, ensure_ascii=False)
    c.ia_tokens = uso.get("entrada", 0) + uso.get("saida", 0)
    c.ia_claude_ok = False  # a autorização vale só para uma leitura
    db.session.commit()
    finais = {q.numero: q.ok for q in Questao.query.filter_by(caderno_id=c.id).all()}
    faltam = [n for n in sorted(c.gabarito) if not finais.get(n)]
    lidas = sum(1 for n, q in (res or {}).items() if n in c.gabarito and q.get("ok"))
    gasto = f" · {_fmt_tokens(c.ia_tokens)}" if c.ia_tokens else ""
    txt = (f"{ok} de {c.total} questões prontas ({lidas} lidas agora pelo {IA.nome(prov)}{gasto})." if lidas or gasto
           else f"{ok} de {c.total} questões prontas (sem custo de IA: nada novo para ler).")
    lista = ", ".join(map(str, faltam[:20])) + ("…" if len(faltam) > 20 else "")
    if faltam and prov == "gemini" and IA.claude_disponivel():
        c.ia_status = "autorizar"
        txt += f" O Gemini não completou {len(faltam)}: {lista}. Autorize o Claude (pago) só para elas, ou deixe assim."
    else:
        c.ia_status = "ok"
        if faltam:
            txt += f" Incompletas: {lista}."
    c.ia_msg = txt[:300]
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


def _colocar_na_fila(cid):
    if _trabalhador["t"] is None or not _trabalhador["t"].is_alive():
        _trabalhador["t"] = threading.Thread(target=_loop_ia, daemon=True)
        _trabalhador["t"].start()
    _fila_ia.put(cid)


ESCOPO_PADRAO = "todas"
ESCOPOS = {
    "todas": "Todas as páginas, transcrevendo só as questões incompletas (recomendado)",
    "alvo": "Só as páginas das questões incompletas (mais barato, pode falhar em PDF de duas colunas)",
    "tudo": "Caderno inteiro do zero: todas as páginas e todas as questões (mais caro)",
}


def opcoes_ia_do_form(form):
    """(provedor, escopo) escolhidos no formulário. provedor: gemini | claude | nenhuma."""
    prov = (form.get("ia_provedor") or "gemini").strip().lower()
    if prov not in ("gemini", "claude", "nenhuma"):
        prov = "gemini"
    if prov == "claude" and not eh_admin():
        prov = "gemini"  # só o administrador decide gastar com o Claude
    esc = (form.get("ia_escopo") or ESCOPO_PADRAO).strip().lower()
    return prov, (esc if esc in ESCOPOS else ESCOPO_PADRAO)


def iniciar_ia_com_opcoes(c, form, continuar=False):
    """Inicia a leitura com as opções escolhidas. Retorna (iniciou, mensagem)."""
    prov, esc = opcoes_ia_do_form(form)
    if prov == "nenhuma":
        return False, "Leitura com IA não iniciada (opção “Não usar IA”)."
    if prov == "claude":
        if not IA.claude_disponivel():
            return False, "O Claude não está configurado (falta ANTHROPIC_API_KEY no Railway)."
        ok = enfileirar_ia(c, continuar=continuar, claude=True, escopo=esc)
        if ok:
            acao(f"Autorizou o Claude (pago) no caderno: {c.titulo} ({esc})")
        return ok, "O Claude (autorizado) está lendo este caderno." if ok else (c.ia_msg or "Não foi possível iniciar.")
    ok = enfileirar_ia(c, continuar=continuar, escopo=esc)
    return ok, "O Gemini está lendo este caderno. Pode levar alguns minutos." if ok else (c.ia_msg or "Não foi possível iniciar.")


def enfileirar_ia(c, continuar=False, claude=False, escopo=None):
    """Põe o caderno na fila. continuar=True mantém o que já foi lido; claude=True usa o Claude (autorizado)."""
    if escopo in ESCOPOS:
        c.ia_escopo = escopo
    if claude:
        if not IA.claude_disponivel():
            return False
    elif not IA.gemini_disponivel():
        if IA.claude_disponivel():  # só o Claude configurado: pede autorização em vez de gastar
            c.ia_status, c.ia_provedor = "autorizar", "claude"
            c.ia_msg = "O Gemini não está configurado (falta GEMINI_API_KEY no Railway). Autorize o Claude (pago) para ler este caderno."
            db.session.commit()
        return False
    if claude and (c.ia_escopo or ESCOPO_PADRAO) != "tudo":
        # o Claude só transcreve o que ainda está incompleto: guarda o que já está pronto
        prontas = _carregar_parcial(c)
        prontas.update(_questoes_do_banco(c))
        c.ia_parcial = json.dumps({str(n): q for n, q in prontas.items()}, ensure_ascii=False)
    elif not continuar or (c.ia_escopo == "tudo" and not claude):
        c.ia_parcial = None
    c.ia_claude_ok = bool(claude)
    c.ia_provedor = "claude" if claude else "gemini"
    c.ia_status, c.ia_inicio, c.ia_batida = "fila", agora(), agora()
    c.ia_msg = f"Na fila para leitura com o {IA.nome(c.ia_provedor)}…"
    db.session.commit()
    _colocar_na_fila(c.id)
    return True


def ia_ocupada(c):
    """Leitura em andamento (sem sinal de vida há mais de 15 min = interrompida, ex.: servidor reiniciou)."""
    marca = c.ia_batida or c.ia_inicio
    return c.ia_status in ("fila", "lendo") and marca and (agora() - marca) < IA_TRAVADA


def retomar_leituras():
    """Leituras interrompidas (servidor reiniciou no meio) voltam para a fila e continuam de onde pararam."""
    for c in Caderno.query.filter(Caderno.ia_status.in_(("fila", "lendo"))).all():
        if ia_ocupada(c):
            continue
        marca = c.ia_batida or c.ia_inicio
        n = (Caderno.query.filter(Caderno.id == c.id, Caderno.ia_status.in_(("fila", "lendo")),
                                  (Caderno.ia_batida == marca) if c.ia_batida else Caderno.ia_batida.is_(None))
             .update({"ia_status": "fila", "ia_batida": agora(),
                      "ia_msg": "Retomando a leitura de onde parou…"}, synchronize_session=False))
        db.session.commit()
        if n:  # só um processo "pega" o caderno
            _colocar_na_fila(c.id)


def _so_admin_autoriza():
    if not eh_admin():
        flash("Só o administrador pode autorizar o uso do Claude (pago).", "erro")
        return False
    return True


@app.post("/caderno/<int:cid>/ia")
def caderno_ia(cid):
    """Inicia a leitura com IA usando as opções escolhidas antes (IA e o que ler)."""
    c = db.get_or_404(Caderno, cid)
    if not IA.disponivel():
        flash("A IA não está configurada (falta GEMINI_API_KEY no Railway).", "erro")
    elif not ia_ocupada(c):
        ok, msg = iniciar_ia_com_opcoes(c, request.form, continuar=request.form.get("continuar") == "1")
        flash(msg, "ok" if ok else "erro")
    return redirect(url_for("caderno_resolver", cid=cid))


@app.post("/caderno/<int:cid>/ia/deixar")
def caderno_ia_deixar(cid):
    c = db.get_or_404(Caderno, cid)
    if c.ia_status == "autorizar":
        c.ia_status = "ok"
        c.ia_msg = ((c.ia_msg or "").split(" O Gemini")[0] + " Sem o Claude: as incompletas ficam como estão.")[:300]
        db.session.commit()
    return redirect(url_for("caderno_resolver", cid=cid))


@app.post("/cadernos/ia-todos")
def cadernos_ia_todos():
    if not IA.gemini_disponivel():
        flash("O Gemini não está configurado (falta GEMINI_API_KEY no Railway).", "erro")
        return redirect(url_for("metas"))
    n = 0
    for c in Caderno.query.order_by(Caderno.id).all():
        if c.gabarito and c.ia_status not in ("ok", "autorizar") and not ia_ocupada(c):
            enfileirar_ia(c, continuar=True, escopo=opcoes_ia_do_form(request.form)[1])
            n += 1
    flash(f"{n} caderno(s) na fila de leitura com o Gemini." if n else "Todos os cadernos já foram lidos com IA.", "ok")
    return redirect(url_for("metas"))


@app.get("/api/caderno/<int:cid>/ia")
def api_caderno_ia(cid):
    c = db.get_or_404(Caderno, cid)
    return jsonify(ok=True, status=c.ia_status, msg=c.ia_msg, ocupada=bool(ia_ocupada(c)),
                   ia=IA.nome(c.ia_provedor) if c.ia_provedor else "")


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

    titulo = sem_nulos(request.form.get("titulo", "").strip() or os.path.splitext(nome)[0])
    c = Caderno(materia_id=m.id, titulo=titulo[:200], arquivo_nome=nome[:200], pdf_questoes=dq,
                pdf_gabarito=dg, tem_pdf_gabarito=bool(dg))
    c.gabarito = gab
    db.session.add(c)
    definir_topico_caderno(c, request.form.get("topico_id"), request.form.get("novo_assunto"))
    db.session.commit()
    if not gab:
        iniciou, _ = iniciar_ia_com_opcoes(c, request.form) if IA.disponivel() else (False, "")
        if iniciou:
            flash("Caderno criado. Não achei o gabarito com o leitor simples — a Tatti está procurando no PDF com IA.", "ok")
            return redirect(url_for("caderno_resolver", cid=c.id))
        flash("Caderno criado, mas não consegui ler o gabarito. Digite-o abaixo.", "erro")
        return redirect(url_for("caderno_gabarito", cid=c.id))
    ok = montar_questoes(c)
    iniciou, msg = iniciar_ia_com_opcoes(c, request.form) if IA.disponivel() else (False, "")
    if iniciou:
        flash(f"Caderno criado. {msg}", "ok")
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
            flash(f"Gabarito salvo: {len(gab)} questões ({ok} reconstruídas)."
                  + (" Para ler com IA, escolha as opções em “Leitura com IA”." if IA.disponivel() and ok < len(gab) else ""), "ok")
            return redirect(url_for("caderno_resolver", cid=cid))
    texto = "\n".join(f"{n}-{l}" for n, l in c.gabarito.items())
    return render_template("gabarito.html", c=c, m=c.materia, texto=texto, ia_on=IA.disponivel(), escopos=ESCOPOS)


@app.post("/caderno/<int:cid>/reprocessar")
def caderno_reprocessar(cid):
    c = db.get_or_404(Caderno, cid)
    ok = montar_questoes(c)
    flash(f"{ok} de {c.total} questões reconstruídas do PDF.", "ok" if ok else "erro")
    return redirect(url_for("caderno_resolver", cid=cid))


def _tentativa_aberta(c):
    """Bateria em andamento deste usuário neste caderno."""
    return (Tentativa.query.execution_options(todos=True)
            .filter_by(caderno_id=c.id, finalizada=False, usuario=_login_atual())
            .order_by(Tentativa.feita_em.desc()).first())


def _tentativa(tid):
    t = db.session.get(Tentativa, tid, execution_options={"todos": True})
    if t is None:
        abort(404)
    return t


@app.route("/caderno/<int:cid>")
def caderno_resolver(cid):
    c = db.get_or_404(Caderno, cid)
    if not c.gabarito:
        if not ia_ocupada(c):
            # 1º: leitor simples ampliado; 2º: IA
            gab = parse_gabarito_texto(extrair_texto(c.pdf_questoes), exigir_secao=True) if c.pdf_questoes else {}
            if not gab and c.pdf_gabarito:
                gab = parse_gabarito_texto(extrair_texto(c.pdf_gabarito))
            if gab:
                c.gabarito = gab
                db.session.commit()
                montar_questoes(c)
                flash(f"Gabarito encontrado no PDF: {len(gab)} questões.", "ok")
                return redirect(url_for("caderno_resolver", cid=cid))
        if ia_ocupada(c):
            return render_template("resolver.html", c=c, m=c.materia, numeros=[], reconstruidas=0, aberta=None,
                                   ia_on=True, ia_lendo=True, finalizadas=[])
        flash(c.ia_msg if c.ia_status == "erro" else "Este caderno ainda não tem gabarito.", "erro")
        return redirect(url_for("caderno_gabarito", cid=cid))
    if Questao.query.filter_by(caderno_id=c.id).count() < len(c.gabarito) and not ia_ocupada(c):
        montar_questoes(c)
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
        t = Tentativa(usuario=_login_atual(), caderno_id=c.id, materia_id=c.materia_id, feita_em=agora(), acertos=0, respondidas=0,
                      total=len(c.gabarito), xp=0, respostas_json="{}", finalizada=False)
        db.session.add(t)
        db.session.commit()
        acao(f"Iniciou bateria: {c.titulo} ({c.materia.nome}, {len(c.gabarito)} questões)")
    return jsonify(ok=True, tentativa=t.id, **_estado(t))


@app.get("/api/tentativa/<int:tid>/questao/<int:n>")
def api_questao(tid, n):
    t = _tentativa(tid)
    q = Questao.query.filter_by(caderno_id=t.caderno_id, numero=n).first_or_404()
    return jsonify(ok=True, questao=_questao_json(t, q), **_estado(t))


@app.post("/api/tentativa/<int:tid>/responder")
def api_responder(tid):
    t = _tentativa(tid)
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
    t = _tentativa(tid)
    if not t.respondidas:
        db.session.delete(t)
        db.session.commit()
        return jsonify(ok=True, url=url_for("caderno_resolver", cid=t.caderno_id))
    if not t.finalizada:
        meta = cfg_num(configs(), "meta_acerto")
        t.xp = t.acertos + (25 if t.percentual >= meta and t.respondidas >= 5 else 0)
        t.finalizada = True
        db.session.commit()
        acao(f"Encerrou bateria: {t.caderno.titulo} — {t.acertos}/{t.respondidas} acertos ({t.percentual:.0f}%)")
    return jsonify(ok=True, url=url_for("tentativa_ver", tid=t.id))


@app.route("/tentativa/<int:tid>")
def tentativa_ver(tid):
    t = _tentativa(tid)
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
    t = _tentativa(tid)
    cid = t.caderno_id
    db.session.delete(t)
    db.session.commit()
    flash("Tentativa removida do histórico.", "info")
    return redirect(url_for("caderno_resolver", cid=cid))


@app.post("/caderno/<int:cid>/resetar")
def caderno_resetar(cid):
    """Apaga todas as baterias (tentativas) do caderno; as questões continuam."""
    c = db.get_or_404(Caderno, cid)
    n = len(c.tentativas)
    acao(f"Resetou caderno: {c.titulo} ({n} bateria(s) apagada(s))")
    for t in list(c.tentativas):
        db.session.delete(t)
    db.session.commit()
    flash(f"Caderno “{c.titulo}” resetado: {n} bateria(s) apagada(s). Ele volta como nunca resolvido.", "info")
    destino = request.form.get("voltar")
    return redirect(url_for("caderno_resolver", cid=cid) if destino == "caderno"
                    else url_for("materia", mid=c.materia_id) + "#questoes")


@app.post("/caderno/<int:cid>/excluir")
def caderno_excluir(cid):
    c = db.get_or_404(Caderno, cid)
    mid = c.materia_id
    acao(f"Apagou caderno: {c.titulo} ({c.materia.nome})")
    Tentativa.query.filter_by(caderno_id=c.id).delete(synchronize_session=False)
    db.session.expire(c, ["tentativas"])
    db.session.delete(c)
    db.session.commit()
    flash("Caderno excluído.", "info")
    return redirect(url_for("materia", mid=mid) + "#questoes")


# ---- Histórico e metas ---------------------------------------------------- #
@app.post("/api/aula/<int:aid>/leitura")
def api_aula_leitura(aid):
    """Círculo da tela Evolução: acao=ler registra leitura; acao=desfazer remove a última."""
    a = db.get_or_404(Aula, aid)
    oper = (request.get_json(silent=True) or {}).get("acao", "ler")
    if oper == "desmarcar":  # círculo volta a ficar vazio
        for l in list(a.leituras):
            db.session.delete(l)
    elif oper == "desfazer":
        if a.leituras:
            db.session.delete(a.leituras[-1])
    else:
        db.session.add(Leitura(aula_id=a.id, numero=len(a.leituras) + 1, lida_em=agora(), usuario=_login_atual()))
    antes = len(a.leituras)
    db.session.commit()
    db.session.refresh(a)
    depois = len(a.leituras)
    if depois > antes:
        acao(f"Concluiu a {depois}ª leitura (pela Evolução): {a.titulo}")
    elif depois < antes:
        acao(f"Desfez leitura (pela Evolução): {a.titulo} — agora {depois}x")
    s = stats_materia(a.materia)
    aviso = "Modo administrador: a leitura foi registrada como teste e não conta nas metas nem no humor da Tatti." if eh_admin() and oper == "ler" else None
    return jsonify(ok=True, aviso=aviso, leituras=len(a.leituras),
                   ultima=a.leituras[-1].lida_em.strftime("%d/%m/%Y %H:%M") if a.leituras else None,
                   aulas_lidas=s["aulas_lidas"], aulas=s["aulas"])


@app.post("/api/aula/<int:aid>/titulo")
def api_aula_titulo(aid):
    """Renomear aula (tela da matéria). Vazio = volta ao nome do arquivo."""
    a = db.get_or_404(Aula, aid)
    t = ((request.get_json(silent=True) or {}).get("titulo") or "").strip()
    antigo = a.titulo
    a.titulo = t[:200] if t else nome_padrao(a.arquivo_nome)
    db.session.commit()
    if antigo != a.titulo:
        acao(f"Renomeou aula: “{antigo}” → “{a.titulo}”")
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
    salvar_cfg("ordem_evolucao", ",".join(ordem))
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/tutorial-visto")
def api_tutorial_visto():
    salvar_cfg("tutorial_visto", "1")
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
    cal, proxima, plano_aula = dados_calendario()
    return render_template("historico.html", materias=[(m, stats_materia(m), por_assunto(m)) for m in materias],
                           cal=cal, proxima=proxima, plano_aula=plano_aula, cal_hoje=hoje(), ordem_cards=ordem_cards(),
                           pontos=pontos, semanas=semanas, eventos=eventos[:20])


def resumo_de_dia(d, cfg=None, aulas=None, leituras=None, tents=None):
    """Meta e realizado de um dia (usa a foto do plano; se não houver, estima a meta)."""
    cfg = cfg or configs()
    alvo = max(1, int(cfg_num(cfg, "meta_leituras_por_aula")))
    limite = _data_cfg(cfg, "data_limite_leituras")
    ini, fim = dt0(d), dt0(d + timedelta(days=1))
    aulas = aulas if aulas is not None else Aula.query.all()
    leituras = leituras if leituras is not None else Leitura.query.all()
    tents = tents if tents is not None else Tentativa.query.all()
    snap = MetaDia.query.filter_by(data=d).first()
    do_dia = [l for l in leituras if ini <= l.lida_em < fim]
    if snap:
        meta, estimada = snap.meta_leituras, False
        meta_q = snap.meta_questoes or QUESTOES_MINIMO
    else:
        existentes = [a for a in aulas if (a.criada_em or ini) < fim]
        antes = {}
        for l in leituras:
            if l.lida_em < ini:
                antes[l.aula_id] = antes.get(l.aula_id, 0) + 1
        restante = sum(max(0, alvo - antes.get(a.id, 0)) for a in existentes)
        dias = (limite - d).days + 1
        meta = math.ceil(restante / dias) if dias > 0 and restante else 0
        estimada = True
        meta_q = max(QUESTOES_MINIMO, int(cfg_num(cfg, "meta_questoes_dia")))
    tq = [t for t in tents if ini <= t.feita_em < fim]
    q = sum(t.respondidas for t in tq)
    ac = sum(t.acertos for t in tq)
    ok_l, ok_q = len(do_dia) >= meta, q >= meta_q
    humor = "radiante" if ok_l and ok_q else "feliz" if ok_l or ok_q else "bravo" if (do_dia or q) else "furioso"
    return {"data": d, "meta": meta, "estimada": estimada, "feitas": len(do_dia), "leituras": do_dia,
            "meta_q": meta_q, "questoes": q, "acertos": ac, "baterias": tq, "snap": snap,
            "ok_l": ok_l, "ok_q": ok_q, "humor": humor}


def _editar_foto_do_dia(remover=(), na_frente=()):
    """Mexe na lista de aulas selecionadas para hoje antes de recalcular (desfazer devolve a aula ao lugar dela)."""
    snap = MetaDia.query.filter_by(data=hoje()).first()
    if not snap:
        return
    ids = [i for i in snap.planejadas if i not in set(remover) and i not in set(na_frente)]
    snap.planejadas_json = json.dumps(list(na_frente) + ids)
    db.session.commit()


def _soltar_escolhas(esc, aula_id, h, nova=None):
    """A aula sai da meta: deixa de valer como aula incluída ou escolhida no lugar de outra hoje.
    Se 'nova' vier (troca com substituta), a escolha passa para a nova aula."""
    for aj in AjusteMeta.query.filter_by(usuario=esc, data=h, tipo="inclusao", aula_id=aula_id).all():
        if nova:
            aj.aula_id = nova
            nova = None  # a vaga da inclusão fica com a substituta
        else:
            db.session.delete(aj)
    for aj in AjusteMeta.query.filter_by(usuario=esc, data=h, tipo="troca", substituta_id=aula_id).all():
        aj.substituta_id = nova
        nova = None
    return nova  # substituta que ainda não foi usada


def _refazer_foto_do_dia():
    """Depois de um ajuste do admin, a foto de hoje passa a refletir a nova meta (evita remarcar a aula trocada amanhã)."""
    p = plano_do_dia()
    snap = MetaDia.query.filter_by(data=hoje()).first()
    if snap and p["fase"] == "leitura":
        lidas_hoje = [aid for aid, e in p["est"].items() if e["hoje"]]
        snap.planejadas_json = json.dumps(lidas_hoje + [f["aula"].id for f in p["fila"]])
        snap.meta_leituras = p["meta_hoje"]
        db.session.commit()


def _aviso_bateria(msg, cat="ok"):
    """Mensagem mostrada dentro do cartão da bateria (a página volta rolada até ele)."""
    session["aviso_bateria"] = [cat, msg]


@app.post("/bateria/escolher")
@so_admin
def bateria_escolher():
    """Admin troca o caderno da bateria de hoje do aluno que está vendo (ou volta ao automático)."""
    v = request.form.get("escolha", "")
    modos = {"menor_acerto": "o caderno com menor acerto", "estudada_nova": "uma questão já estudada e nunca feita"}
    aluno = Usuario.query.filter_by(login=escopo()).first()
    if v in modos:
        salvar_cfg("bateria_escolha", json.dumps({"data": hoje().isoformat(), "modo": v}))
        db.session.commit()
        b = bateria_do_dia()
        if b.get("modo_ok") and b.get("modo_admin") == v and b.get("item") and b["item"]["cadernos"]:
            salvar_cfg("bateria_escolha", json.dumps({"data": hoje().isoformat(), "modo": v,
                                                      "caderno_id": b["item"]["cadernos"][0].id,
                                                      "motivo": b["item"]["motivo"].replace("escolhida pelo admin — ", "")}))
            _aviso_bateria(f"Bateria de hoje de {(aluno.nome if aluno else None) or escopo()}: {modos[v]} — "
                  f"{b['item']['cadernos'][0].titulo if b['item']['cadernos'] else b['item']['grupo']}.", "ok")
        else:
            salvar_cfg("bateria_escolha", "")
            _aviso_bateria(("Nenhum caderno resolvido ainda para comparar acertos." if v == "menor_acerto"
                   else "Não há caderno de assunto já estudado que ainda não tenha sido feito.")
                  + " A bateria continua automática.", "erro")
        db.session.commit()
        return redirect(url_for("index") + "#bateria")
    if v.isdigit() and db.session.get(Caderno, int(v)):
        salvar_cfg("bateria_escolha", json.dumps({"data": hoje().isoformat(), "caderno_id": int(v)}))
        c = db.session.get(Caderno, int(v))
        _aviso_bateria(f"Bateria de hoje de {(aluno.nome if aluno else None) or escopo()} trocada para “{c.titulo}”.", "ok")
    else:
        salvar_cfg("bateria_escolha", "")
        _aviso_bateria("Bateria de hoje voltou a ser escolhida automaticamente.", "info")
    db.session.commit()
    return redirect(url_for("index") + "#bateria")


@app.post("/meta/ajuste")
@so_admin
def meta_ajuste():
    """Admin troca uma aula da meta do dia por outra (a trocada volta em ~10 dias), exclui ou inclui uma aula
    na meta de hoje, ou desfaz um ajuste de hoje. Vale para o aluno em "ver como"."""
    f = request.form
    oper = f.get("acao")
    h = hoje()
    esc = escopo()
    if not esc:
        abort(400)
    if oper == "desfazer":
        aj = db.session.get(AjusteMeta, f.get("ajuste_id", type=int) or 0)
        if aj and aj.usuario == esc and aj.data == h:
            tipo, aid, sub = aj.tipo, aj.aula_id, aj.substituta_id
            db.session.delete(aj)
            db.session.commit()
            if tipo == "inclusao":
                _editar_foto_do_dia(remover=[aid])
            else:  # exclusão/troca desfeita: a aula volta ao topo das selecionadas; a substituta sai
                _editar_foto_do_dia(remover=[sub] if sub else [], na_frente=[aid])
            flash("Ajuste desfeito.", "ok")
    elif oper == "incluir" and f.getlist("aula_ids"):
        # várias aulas de uma vez
        p = plano_do_dia()
        na_fila = {x["aula"].id for x in p["fila"]}
        entraram, recusadas, voltaram = [], [], []
        for aid in dict.fromkeys(f.getlist("aula_ids", type=int)):
            a = db.session.get(Aula, aid)
            st = p["est"].get(aid) if a else None
            if not a or p["fase"] != "leitura" or not st or st["n"] >= p["alvo"] or aid in na_fila:
                if a:
                    recusadas.append(a.titulo)
                continue
            excl = AjusteMeta.query.filter_by(usuario=esc, aula_id=aid, data=h, tipo="exclusao").all()
            if excl:  # aula excluída hoje: só volta para a vaga dela (a meta não muda)
                for aj in excl:
                    db.session.delete(aj)
                voltaram.append(aid)
            else:
                db.session.add(AjusteMeta(usuario=esc, aula_id=aid, tipo="inclusao", data=h))
            entraram.append(a.titulo)
        db.session.commit()
        if voltaram:
            _editar_foto_do_dia(na_frente=voltaram)
        if entraram:
            flash(f"{len(entraram)} aula(s) adicionada(s) à meta de hoje: " + "; ".join(entraram) + ".", "ok")
        if recusadas:
            flash("Não entraram (já estão na meta ou já completaram as leituras): " + "; ".join(recusadas) + ".", "erro")
    else:
        a = db.get_or_404(Aula, f.get("aula_id", type=int) or 0)
        incl = AjusteMeta.query.filter_by(usuario=esc, aula_id=a.id, data=h, tipo="inclusao").all()
        if oper == "excluir" and incl:
            for aj in incl:
                db.session.delete(aj)  # excluir uma aula incluída hoje só desfaz a inclusão
            db.session.commit()
            _editar_foto_do_dia(remover=[a.id])
            flash(f"“{a.titulo}” saiu da meta de hoje (inclusão desfeita).", "ok")
        elif oper == "excluir":
            _soltar_escolhas(esc, a.id, h)
            db.session.add(AjusteMeta(usuario=esc, aula_id=a.id, tipo="exclusao", data=h))
            db.session.commit()
            flash(f"“{a.titulo}” saiu das aulas de hoje. Nenhuma outra entrou no lugar e a meta continua a mesma.", "ok")
        elif oper == "incluir":
            p = plano_do_dia()
            st = p["est"].get(a.id)
            if p["fase"] != "leitura" or not st or st["n"] >= p["alvo"]:
                flash(f"“{a.titulo}” não pode entrar na meta de hoje (já completou as leituras).", "erro")
            elif a.id in {x["aula"].id for x in p["fila"]}:
                flash(f"“{a.titulo}” já está na meta de hoje.", "erro")
            elif AjusteMeta.query.filter_by(usuario=esc, aula_id=a.id, data=h, tipo="exclusao").first():
                for aj in AjusteMeta.query.filter_by(usuario=esc, aula_id=a.id, data=h, tipo="exclusao").all():
                    db.session.delete(aj)  # aula excluída hoje: só volta para a vaga dela
                db.session.commit()
                _editar_foto_do_dia(na_frente=[a.id])
                flash(f"“{a.titulo}” voltou às aulas de hoje.", "ok")
            else:
                db.session.add(AjusteMeta(usuario=esc, aula_id=a.id, tipo="inclusao", data=h))
                db.session.commit()
                flash(f"“{a.titulo}” entrou na meta de hoje (a meta aumentou uma leitura).", "ok")
        elif oper == "trocar":
            sub = f.get("substituta_id", type=int)
            sub_a = db.session.get(Aula, sub) if sub else None
            if sub_a and sub_a.id == a.id:
                sub_a = None
            limite = _data_cfg(configs(), "data_limite_leituras")
            volta = min(h + timedelta(days=DIAS_TROCA), max(limite, h + timedelta(days=1)))
            # se a aula trocada tinha sido incluída/escolhida hoje, a escolha passa para a substituta
            resto = _soltar_escolhas(esc, a.id, h, nova=sub_a.id if sub_a else None)
            db.session.add(AjusteMeta(usuario=esc, aula_id=a.id, tipo="troca", data=h, volta_em=volta,
                                      substituta_id=resto))
            db.session.commit()
            flash(f"“{a.titulo}” trocada por {('“' + sub_a.titulo + '”') if sub_a else 'uma aula escolhida automaticamente'}. "
                  f"Ela volta à meta do dia em {volta.strftime('%d/%m')}.", "ok")
        else:
            abort(400)
    _refazer_foto_do_dia()
    return redirect(url_for("index") + "#meta")


@app.route("/metas/dias")
def metas_dias():
    cfg = configs()
    h = hoje()
    aulas, leituras, tents = Aula.query.all(), Leitura.query.all(), Tentativa.query.all()
    primeira = min([l.lida_em.date() for l in leituras] + [t.feita_em.date() for t in tents] +
                   [m.data for m in MetaDia.query.all()] + [h])
    try:
        sel = date.fromisoformat(request.args.get("data", ""))
    except ValueError:
        sel = h - timedelta(days=1) if h > primeira else h
    sel = min(sel, h)
    inicio_lista = max(primeira, h - timedelta(days=60))
    lista = [resumo_de_dia(h - timedelta(days=k), cfg, aulas, leituras, tents)
             for k in range((h - inicio_lista).days + 1)]
    det = resumo_de_dia(sel, cfg, aulas, leituras, tents)
    por_id = {a.id: a for a in aulas}
    planejadas = []
    if det["snap"]:
        ini_sel, fim_sel = dt0(sel), dt0(sel + timedelta(days=1))
        for aid in det["snap"].planejadas:
            a = por_id.get(aid)
            if not a:
                continue
            no_dia = any(ini_sel <= l.lida_em < fim_sel for l in a.leituras)
            depois = [l.lida_em for l in a.leituras if l.lida_em >= fim_sel]
            planejadas.append({"aula": a, "no_dia": no_dia, "depois": min(depois) if depois else None})
    sess = SessaoPdf.query.filter(SessaoPdf.usuario == escopo(), SessaoPdf.inicio >= dt0(sel),
                                  SessaoPdf.inicio < dt0(sel + timedelta(days=1))).all()
    return render_template("metas_dias.html", lista=lista, det=det, sel=sel, planejadas=planejadas, timedelta1=timedelta(days=1),
                           tempo_dia=sum(x.segundos or 0 for x in sess), hoje_d=h, primeira=primeira)


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
            if k == "nome":
                u = Usuario.query.filter_by(login=escopo()).first()
                if u:
                    u.nome = v[:100]
            salvar_cfg(k, v[:200])
        db.session.commit()
        flash("Metas salvas. A Tatti já está de olho!", "ok")
        return redirect(url_for("metas"))
    cads = Caderno.query.all()
    lendo = [c for c in cads if ia_ocupada(c)]
    ia_info = {"on": IA.disponivel(), "gemini": IA.gemini_disponivel(), "claude": IA.claude_disponivel(),
               "desc_gemini": IA.descricao("gemini"), "desc_claude": IA.descricao("claude"),
               "total": len(cads), "ok": sum(1 for c in cads if c.ia_status == "ok"),
               "lendo": len(lendo), "lendo_claude": sum(1 for c in lendo if c.ia_provedor == "claude"),
               "erro": sum(1 for c in cads if c.ia_status == "erro"),
               "autorizar": [c for c in cads if c.ia_status == "autorizar"]}
    return render_template("metas.html", h=calcular_humor(), p=plano_do_dia(), ia=ia_info)


def registrar_erro(e):
    import traceback
    try:
        db.session.rollback()
        u = g.get("usuario_atual")
        er = ErroApp(quando=agora(), usuario=(u.login if u else None), tipo=e.__class__.__name__[:120],
                     caminho=(request.full_path.rstrip("?") if request else "")[:300], mensagem=str(e)[:2000],
                     rastreio="".join(traceback.format_exception(type(e), e, e.__traceback__))[-8000:])
        db.session.add(er)
        db.session.commit()
        return er.id
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return None


@app.errorhandler(500)
def erro_interno(e):
    orig = getattr(e, "original_exception", None) or e
    app.logger.exception("Erro em %s", request.path)
    cod = registrar_erro(orig)
    if request.path.startswith("/api/"):
        return jsonify(ok=False, erro="erro interno", codigo=cod), 500
    return render_template("erro.html", codigo=cod), 500


@app.errorhandler(413)
def muito_grande(_):
    flash(f"Arquivo grande demais (limite {app.config['MAX_CONTENT_LENGTH'] // 1048576} MB).", "erro")
    return redirect(request.referrer or url_for("index"))


def zerar_fotos_de_hoje_v2():
    """Uma vez: a meta do dia mudou de regra (equilibrada). As fotos de hoje, feitas pela regra antiga,
    são zeradas (a lista de aulas é refeita na próxima abertura); a bateria de hoje é mantida."""
    if db.session.get(Config, "meta_equilibrada_v1"):
        return
    for snap in MetaDia.query.filter(MetaDia.data == hoje()).all():
        snap.meta_leituras = None
        snap.planejadas_json = "[]"
    db.session.add(Config(chave="meta_equilibrada_v1", valor=hoje().isoformat()))
    db.session.commit()


def _reparos_iniciais():
    """Reparos que dependem de funções definidas depois de init_db (roda uma vez, no carregamento)."""
    try:
        with app.app_context():
            zerar_fotos_de_hoje_v2()
    except Exception as e:  # noqa: BLE001
        app.logger.warning("fotos v2: %s", e)
    try:
        with app.app_context():
            reparar_cadernos_tec()
    except Exception as e:  # noqa: BLE001
        app.logger.warning("reparos iniciais: %s", e)


_reparos_iniciais()


if __name__ == "__main__":
    iniciar_thread_lembretes()
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
