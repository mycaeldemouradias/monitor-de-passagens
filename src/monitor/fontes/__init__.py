"""Registro das fontes. Para adicionar um site novo de navegador, basta incluí-lo em config/sites.yaml."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..config import Config
from .base import Fonte, FonteBloqueada
from .google_flights import GoogleFlights
from .navegador import SiteNavegador
from .skiplagged import Skiplagged

__all__ = ["Fonte", "FonteBloqueada", "criar_fonte", "fontes_de_api"]

fontes_de_api = {"google_flights", "skiplagged"}


def criar_fonte(nome: str, cfg: Config, pasta_debug: Path, agora: datetime,
                ultima_rodada: datetime | None = None) -> Fonte:
    if nome == "google_flights":
        return GoogleFlights(cfg.geral.orcamento_serpapi_mes, cfg.geral.google_flights_a_cada_horas,
                             ultima_rodada, agora)
    if nome == "skiplagged":
        return Skiplagged(cfg.geral.skiplagged_max_por_rodada)
    if nome in cfg.sites:
        return SiteNavegador(nome, cfg.sites[nome], pasta_debug)
    raise ValueError(
        f"Fonte desconhecida '{nome}'. Use google_flights, skiplagged ou um site de config/sites.yaml "
        f"({', '.join(sorted(cfg.sites)) or 'nenhum'})."
    )
