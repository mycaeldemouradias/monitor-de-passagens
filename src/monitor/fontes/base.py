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


def _google(m: str) -> float:
    """Cotação ao vivo do Google Finance (a mesma da busca "dólar hoje"). O valor vem num bloco de dados da
    página: ..."USD / BRL",3,null,[5.93419688,... (o 1º número é a cotação atual)."""
    import re
    r = requests.get(f"https://www.google.com/finance/quote/{m}-BRL?hl=pt", timeout=20,
                     headers={"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})
    r.raise_for_status()
    achou = re.search(rf'"{m} / BRL",\d+,null,\[([0-9.]+)', r.text)
    if not achou:
        raise ValueError("cotação não encontrada na página")
    return float(achou[1])


def _json(url: str, extrair):
    def buscar(m: str) -> float:
        r = requests.get(url.format(m=m, mi=m.lower()), timeout=20)
        r.raise_for_status()
        return float(extrair(r.json(), m))
    return buscar


#: serviços de cotação, na ordem de preferência. Google e AwesomeAPI são ao vivo; o AwesomeAPI recusa
#: (HTTP 429) os computadores do GitHub Actions (visto em 21/11/2026). Os outros são reservas (cotação do dia).
_COTACOES = (
    ("Google Finance", _google),
    ("AwesomeAPI", _json("https://economia.awesomeapi.com.br/json/last/{m}-BRL", lambda d, m: d[f"{m}BRL"]["bid"])),
    ("Currency API", _json("https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{mi}.json",
                           lambda d, m: d[m.lower()]["brl"])),
    ("Open ER-API", _json("https://open.er-api.com/v6/latest/{m}", lambda d, m: d["rates"]["BRL"])),
)


@functools.lru_cache(maxsize=8)
def cotacao_para_brl(moeda: str) -> float:
    """Cotação atual para BRL (1ª fonte que responder). Usada só quando a fonte não devolve BRL."""
    moeda = moeda.upper()
    if moeda == "BRL":
        return 1.0
    erros = []
    for nome, buscar in _COTACOES:
        try:
            valor = buscar(moeda)
            if 0 < valor < 1000:
                log.info("Cotação %s→BRL: %s", moeda, nome)
                return valor
        except (requests.RequestException, KeyError, TypeError, ValueError) as e:
            erros.append(f"{nome}: {type(e).__name__}")
    raise RuntimeError("Sem cotação do dólar (" + "; ".join(erros) + ")")
