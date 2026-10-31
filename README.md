# DUOTATTI

Plataforma de estudos para o concurso de **Escrevente Técnico Judiciário do TJSP** (Edital 02/2026, VUNESP), com a mascote **Tatti**, que fica feliz quando as metas são batidas e brava quando não são. O ícone do app (aba do navegador e atalho) acompanha o humor dela.

## Funcionalidades
- **Trilha por matéria** (8 matérias e tópicos do edital já cadastrados).
- **Aulas em PDF**: envio de um ou vários PDFs por matéria; leitura no próprio app; botão "Concluí a leitura" registra a data e a vez (1ª, 2ª, 3ª…).
- **Cadernos de questões**: envio do PDF das questões + PDF do gabarito (ou gabarito no fim do PDF, ou colado em texto). O app lê o gabarito automaticamente e pede conferência; o app reconstrói cada questão (enunciado, alternativas e texto de apoio) e apresenta uma por vez, corrigindo na hora pelo gabarito, que nunca é exibido antes da resposta; o PDF não é mostrado. Histórico de baterias e anuladas contando como acerto.
- **Tatti**: humor (furioso → bravo → de olho → feliz → radiante) calculado por: estudou hoje, acerto dos últimos 7 dias × meta (80%), leituras e questões da semana (metas proporcionais ao dia). 3+ dias sem estudar = furioso.
- **XP, nível e ofensiva** (dias seguidos estudando).
- **Evolução**: gráfico de acerto por bateria (filtrável por matéria) com linha da meta, ritmo das últimas 12 semanas e tabela por matéria.
- **Metas** editáveis: % de acerto, leituras/semana, questões/semana, data da prova.

## Rodar localmente
```bash
pip install -r requirements.txt
python app.py        # http://localhost:5000  (usa SQLite: duotatti.db)
```

## Deploy no Railway
1. Suba a pasta para um repositório no GitHub e crie o serviço no Railway a partir dele (Nixpacks + `Procfile`).
2. Adicione um banco **PostgreSQL** ao projeto — a variável `DATABASE_URL` é injetada automaticamente.
3. Variáveis recomendadas:
   - `SECRET_KEY` — qualquer texto longo e aleatório.
   - `APP_PASSWORD` — (opcional) protege o app com senha.
   - `MAX_UPLOAD_MB` — (opcional) limite de upload, padrão 80.

Os PDFs ficam salvos **dentro do Postgres**, então não se perdem a cada deploy (o disco do Railway é efêmero).

## Estrutura
```
app.py                 rotas, modelos, leitura de gabarito, humor do Tatti
templates/_tatti.html  mascote (5 humores, imagens em static/avatar/)
static/icones/         ícones do app por humor
templates/*.html       telas
requirements.txt · Procfile
```
