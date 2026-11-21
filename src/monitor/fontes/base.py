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


#: serviços de cotação, na ordem de preferência. O AwesomeAPI recusa (HTTP 429) os computadores do
#: GitHub Actions (visto em 21/11/2026); os outros dois são reservas (cotação diária).
_COTACOES = (
    ("AwesomeAPI", "https://economia.awesomeapi.com.br/json/last/{m}-BRL", lambda d, m: d[f"{m}BRL"]["bid"]),
    ("Frankfurter", "https://api.frankfurter.dev/v1/latest?base={m}&symbols=BRL", lambda d, m: d["rates"]["BRL"]),
    ("Open ER-API", "https://open.er-api.com/v6/latest/{m}", lambda d, m: d["rates"]["BRL"]),
)


@functools.lru_cache(maxsize=8)
def cotacao_para_brl(moeda: str) -> float:
    """Cotação atual para BRL (1ª fonte que responder). Usada só quando a fonte não devolve BRL."""
    moeda = moeda.upper()
    if moeda == "BRL":
        return 1.0
    erros = []
    for nome, url, extrair in _COTACOES:
        try:
            r = requests.get(url.format(m=moeda), timeout=20)
            r.raise_for_status()
            valor = float(extrair(r.json(), moeda))
            if valor > 0:
                return valor
        except (requests.RequestException, KeyError, TypeError, ValueError) as e:
            erros.append(f"{nome}: {type(e).__name__}")
    raise RuntimeError("Sem cotação do dólar (" + "; ".join(erros) + ")")
