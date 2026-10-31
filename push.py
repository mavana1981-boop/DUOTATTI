"""Envio de Web Push (notificações no celular) com chaves VAPID geradas pelo próprio app."""
import base64
import json

from cryptography.hazmat.primitives import serialization


class InscricaoInvalida(Exception):
    """O navegador cancelou a inscrição (desinstalou, limpou dados ou bloqueou)."""


def gerar_chaves():
    from py_vapid import Vapid01
    v = Vapid01()
    v.generate_keys()
    pem = v.private_pem().decode()
    pub = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return pem, base64.urlsafe_b64encode(pub).rstrip(b"=").decode()


def enviar(insc, dados, chaves, contato="mailto:duotatti@duotatti.app"):
    from py_vapid import Vapid02
    from pywebpush import WebPushException, webpush
    vapid = Vapid02.from_pem(chaves["privada"].encode())
    try:
        webpush(subscription_info={"endpoint": insc.endpoint, "keys": {"p256dh": insc.p256dh, "auth": insc.auth}},
                data=json.dumps(dados, ensure_ascii=False), vapid_private_key=vapid,
                vapid_claims={"sub": contato}, ttl=12 * 3600, timeout=15)
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):
            raise InscricaoInvalida() from e
        raise
