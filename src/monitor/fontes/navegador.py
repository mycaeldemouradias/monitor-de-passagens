"""Fonte genérica por navegador (Playwright/Chromium) para sites de companhias e agências.

Cada site é descrito em config/sites.yaml: um modelo de URL de busca e, opcionalmente,
um seletor CSS onde ficam os preços. A fonte abre a página, espera os resultados carregarem
e pega o MENOR valor em R$ visível na área indicada.

LIMITES (importante):
- Esses sites usam proteção anti-robô (Akamai, PerimeterX, Cloudflare...). A partir de IPs
  de data center, como os do GitHub Actions, é comum receber captcha ou página de bloqueio.
  Nesse caso a fonte registra "bloqueada", salva um print em data/debug/ e o monitor segue
  com as outras fontes.
- Sem seletor específico, o "menor R$ da página" pode pegar valores que não são a tarifa
  (ex.: bagagem, "a partir de" de outra data). Ajuste `seletor_preco` no sites.yaml depois
  de inspecionar o site.
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from ..models import Consulta, Oferta
from .base import UA, Fonte, FonteBloqueada

log = logging.getLogger(__name__)

# "R$ 1.234,56" (com ponto de milhar) ou "R$1234,56" / "R$ 899" (sem); aceita vários espaços
RE_PRECO = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})+(?:,\d{2})?|\d+(?:,\d{2})?)")
SINAIS_BLOQUEIO = (
    "captcha", "are you a robot", "access denied", "pardon our interruption",
    "verifique que você é humano", "confirme que você é humano", "request unsuccessful",
    "acesso negado", "unusual traffic", "press & hold", "pressione e segure",
    "person or a robot", "acesso está temporariamente restrito",  # Skyscanner, Decolar (20/11/2026)
    "verificação de segurança", "não sou um robô",  # Kayak no GitHub Actions (21/11/2026)
)


def extrair_precos(texto: str, minimo: float, maximo: float) -> list[float]:
    valores = []
    for bruto in RE_PRECO.findall(texto):
        try:
            v = float(bruto.replace(".", "").replace(",", "."))
        except ValueError:
            continue
        if minimo <= v <= maximo:
            valores.append(v)
    return valores


def montar_url(modelo: str, c: Consulta) -> str:
    campos = {
        "origem": c.origem, "destino": c.destino,
        "origem_min": c.origem.lower(), "destino_min": c.destino.lower(),
        "ida": c.ida.isoformat(), "volta": c.volta.isoformat() if c.volta else "",
        "ida_ddmmaaaa": c.ida.strftime("%d-%m-%Y"),
        "volta_ddmmaaaa": c.volta.strftime("%d-%m-%Y") if c.volta else "",
        "ida_mmddaaaa": c.ida.strftime("%m/%d/%Y"),
        "volta_mmddaaaa": c.volta.strftime("%m/%d/%Y") if c.volta else "",
        "ida_aammdd": c.ida.strftime("%y%m%d"),
        "volta_aammdd": c.volta.strftime("%y%m%d") if c.volta else "",
        "adultos": c.adultos,
    }
    return modelo.format(**campos)


class _Navegador:
    """Um Chromium compartilhado por todas as fontes de navegador da execução."""

    _instancia: "_Navegador | None" = None

    def __init__(self) -> None:
        from playwright.sync_api import sync_playwright  # import tardio: só se for usar

        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )

    @classmethod
    def obter(cls) -> "_Navegador":
        if cls._instancia is None:
            cls._instancia = cls()
        return cls._instancia

    @classmethod
    def encerrar(cls) -> None:
        if cls._instancia is not None:
            cls._instancia.browser.close()
            cls._instancia._pw.stop()
            cls._instancia = None


class SiteNavegador(Fonte):
    usa_navegador = True

    def __init__(self, nome: str, cfg: dict[str, Any], pasta_debug: Path):
        self.nome = nome
        self.cfg = cfg
        self.pasta_debug = pasta_debug

    def disponivel(self) -> tuple[bool, str]:
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False, "playwright não instalado (pip install playwright && playwright install chromium)"
        return True, ""

    def buscar(self, c: Consulta) -> list[Oferta]:
        chave_modelo = "url_ida_volta" if c.ida_volta else "url_so_ida"
        modelo = self.cfg.get(chave_modelo)
        if not modelo:
            log.info("%s não tem %s configurada; pulando %s", self.nome, chave_modelo,
                     "uma busca" if os.getenv("MONITOR_PRIVADO") == "1" else c.chave)
            return []
        url = montar_url(modelo, c)
        nav = _Navegador.obter()
        contexto = nav.browser.new_context(
            locale="pt-BR", timezone_id="America/Sao_Paulo", user_agent=UA,
            viewport={"width": 1366, "height": 900},
        )
        pagina = contexto.new_page()
        try:
            pagina.goto(url, wait_until="domcontentloaded", timeout=60_000)
            seletor = self.cfg.get("seletor_preco")
            espera = float(self.cfg.get("espera_s", 15))
            if seletor:
                try:
                    pagina.wait_for_selector(seletor, timeout=espera * 1000 + 30_000)
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(espera)
            texto = (pagina.inner_text("body") or "")
            if any(s in texto.lower()[:5000] for s in SINAIS_BLOQUEIO) or \
               any(s in (pagina.title() or "").lower() for s in SINAIS_BLOQUEIO):
                self._print(pagina, c, "bloqueio")
                raise FonteBloqueada(f"{self.nome}: página de bloqueio/captcha")
            if seletor:
                blocos = pagina.locator(seletor).all_inner_texts()
                texto = "\n".join(blocos) if blocos else ""
            precos = extrair_precos(
                texto, float(self.cfg.get("preco_minimo", 80)), float(self.cfg.get("preco_maximo", 60000))
            )
            if not precos:
                self._print(pagina, c, "sem_precos")
                return []
            return [Oferta(
                consulta=c, fonte=self.nome, preco=min(precos), moeda="BRL",
                detalhes="menor R$ visível na página", link=url,
            )]
        finally:
            contexto.close()

    def _print(self, pagina: Any, c: Consulta, motivo: str) -> None:
        try:
            self.pasta_debug.mkdir(parents=True, exist_ok=True)
            nome = f"{self.nome}_{c.origem}{c.destino}_{c.ida:%Y%m%d}_{motivo}.png"
            pagina.screenshot(path=str(self.pasta_debug / nome), full_page=False)
        except Exception:  # noqa: BLE001
            pass

    def fechar(self) -> None:
        _Navegador.encerrar()
