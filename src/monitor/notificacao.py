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
    if os.getenv("TELEGRAM_BOT_TOKEN") and chats_telegram():
        canais.append("telegram")
    if os.getenv("CALLMEBOT_PHONE") and os.getenv("CALLMEBOT_APIKEY"):
        canais.append("whatsapp")
    return canais


def chats_telegram() -> list[str]:
    """TELEGRAM_CHAT_ID aceita várias conversas separadas por vírgula (seu privado, grupos…)."""
    return [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").replace(";", ",").split(",") if c.strip()]


def _telegram(texto: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    falhas = []
    for chat in chats_telegram():
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": texto[:4000], "disable_web_page_preview": True},
            timeout=30,
        )
        dados = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        novo = (dados.get("parameters") or {}).get("migrate_to_chat_id")
        if novo:  # grupo virou supergrupo: o Telegram informa o novo número
            log.warning("Um grupo do Telegram mudou de número; atualize as conversas no painel.")
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": novo, "text": texto[:4000], "disable_web_page_preview": True},
                              timeout=30)
            dados = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not dados.get("ok"):
            falhas.append(f"{r.status_code}: {str(dados.get('description', r.text))[:200]}")
    if falhas and len(falhas) == len(chats_telegram()):
        raise RuntimeError("Telegram: " + " | ".join(falhas))
    for f in falhas:
        log.error("Telegram falhou em uma das conversas: %s", f)


def conversas_do_bot(token: str) -> dict[str, dict]:
    """Conversas onde o bot foi visto (privado e grupos), pelas atualizações recentes do Telegram.

    O Telegram guarda as atualizações por ~24h: depois de adicionar o bot a um grupo, procure logo
    (ou mande uma mensagem no grupo).
    """
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30)
    r.raise_for_status()
    achadas: dict[str, dict] = {}
    for u in r.json().get("result", []):
        for k in ("message", "my_chat_member", "channel_post", "edited_message"):
            if k in u:
                c = u[k]["chat"]
                saiu = k == "my_chat_member" and u[k]["new_chat_member"]["status"] in ("left", "kicked")
                achadas[str(c["id"])] = {"tipo": c["type"], "nome": c.get("title") or c.get("first_name") or "",
                                         "saiu": saiu}
    return achadas


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
