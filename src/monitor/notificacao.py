"""Envio de alertas por Telegram (Bot API) e WhatsApp (CallMeBot).

Cada canal só é usado se as variáveis de ambiente dele estiverem definidas:
  Telegram : TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  WhatsApp : CALLMEBOT_PHONE (com +55...), CALLMEBOT_APIKEY
"""
from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger(__name__)


def canais_configurados() -> list[str]:
    canais = []
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        canais.append("telegram")
    if os.getenv("CALLMEBOT_PHONE") and os.getenv("CALLMEBOT_APIKEY"):
        canais.append("whatsapp")
    return canais


def _telegram(texto: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": os.environ["TELEGRAM_CHAT_ID"],
            "text": texto[:4000],
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if r.status_code != 200 or not r.json().get("ok"):
        raise RuntimeError(f"Telegram respondeu {r.status_code}: {r.text[:300]}")


def _whatsapp(texto: str) -> None:
    r = requests.get(
        "https://api.callmebot.com/whatsapp.php",
        params={
            "phone": os.environ["CALLMEBOT_PHONE"],
            "apikey": os.environ["CALLMEBOT_APIKEY"],
            "text": texto[:1500],
        },
        timeout=30,
    )
    if r.status_code != 200 or "error" in r.text.lower()[:200]:
        raise RuntimeError(f"CallMeBot respondeu {r.status_code}: {r.text[:300]}")


ENVIADORES = {"telegram": _telegram, "whatsapp": _whatsapp}


def enviar(texto: str) -> dict[str, str]:
    """Envia para todos os canais configurados. Retorna {canal: "ok" | erro}."""
    resultado: dict[str, str] = {}
    canais = canais_configurados()
    if not canais:
        log.warning("Nenhum canal de notificação configurado (veja o README).")
    for canal in canais:
        try:
            ENVIADORES[canal](texto)
            resultado[canal] = "ok"
        except Exception as e:  # noqa: BLE001 — um canal falhar não pode derrubar o outro
            log.error("Falha ao enviar por %s: %s", canal, e)
            resultado[canal] = str(e)
        if canal == "whatsapp":
            time.sleep(2)  # o CallMeBot limita mensagens em sequência
    return resultado
