# Configuração lida automaticamente pelo gunicorn (Procfile).
def post_fork(server, worker):
    # inicia o verificador de lembretes (notificações da Tatti) em cada worker
    from app import iniciar_thread_lembretes
    iniciar_thread_lembretes()
