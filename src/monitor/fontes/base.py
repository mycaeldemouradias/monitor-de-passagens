"""Interface comum das fontes de preço."""
from __future__ import annotations

import functools
import logging

import requests

from ..models import Consulta, Oferta

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class FonteBloqueada(RuntimeError):
    """O site respondeu com captcha / bloqueio anti-robô."""


class Fonte:
    nome: str = ""
    #: limite de consultas por execução (None = sem limite). Fontes pagas sobrescrevem.
    limite_por_execucao: int | None = None
    usa_navegador: bool = False
    #: pausa aleatória (mín, máx) em segundos entre buscas da mesma fonte
    pausa_s: tuple[float, float] | None = None

    def disponivel(self) -> tuple[bool, str]:
        return True, ""

    def buscar(self, consulta: Consulta) -> list[Oferta]:
        raise NotImplementedError

    def fechar(self) -> None:
        pass


@functools.lru_cache(maxsize=8)
def cotacao_para_brl(moeda: str) -> float:
    """Cotação de compra (bid) atual via AwesomeAPI. Usada só quando a fonte não devolve BRL."""
    moeda = moeda.upper()
    if moeda == "BRL":
        return 1.0
    r = requests.get(f"https://economia.awesomeapi.com.br/json/last/{moeda}-BRL", timeout=20)
    r.raise_for_status()
    return float(r.json()[f"{moeda}BRL"]["bid"])
