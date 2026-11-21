"""Calendário de preços e projeção de roteiros (vários trechos em sequência, sem sobreposição).

Usa o histórico: para cada viagem e data, o preço mais recente de cada fonte, e o menor entre elas.
Limitação: o horário considerado é o do voo mais barato daquela data (o histórico não guarda os outros).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import product
from typing import Callable, Iterable

from .aeroportos import mesma_cidade


@dataclass(frozen=True)
class Opcao:
    rota_id: str
    origem: str
    destino: str
    data: date                   # data de ida
    volta: date | None
    preco: float
    fonte: str
    vendedor: str
    link: str
    coleta: datetime
    partida: datetime | None     # horário local de saída (None = fonte sem horário, ex.: Kayak)
    chegada: datetime | None     # horário local de chegada
    escalas: str = ""


def _dt(texto: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(texto) if texto else None
    except ValueError:
        return None


def opcoes(linhas: Iterable[dict]) -> dict[str, dict[date, Opcao]]:
    """{viagem: {data de ida: opção mais barata}}, usando a leitura mais recente de cada fonte."""
    recente: dict[tuple[str, str], dict] = {}
    for l in linhas:
        try:
            ts = datetime.fromisoformat(str(l["data_hora_utc"]))
            float(l["preco"])
        except (KeyError, ValueError):
            continue
        k = (l["chave"], l["fonte"])
        if k not in recente or ts >= recente[k]["_ts"]:
            recente[k] = {**l, "_ts": ts}
    saida: dict[str, dict[date, Opcao]] = {}
    for l in recente.values():
        o = Opcao(
            rota_id=l["rota_id"], origem=l["origem"], destino=l["destino"], data=date.fromisoformat(l["ida"]),
            volta=date.fromisoformat(l["volta"]) if l.get("volta") else None, preco=float(l["preco"]),
            fonte=l["fonte"], vendedor=l.get("vendedor") or "", link=l.get("link") or "", coleta=l["_ts"],
            partida=_dt(l.get("ida_partida")), chegada=_dt(l.get("ida_chegada")),
            escalas=l.get("ida_conexoes") or "",
        )
        atual = saida.setdefault(o.rota_id, {}).get(o.data)
        if atual is None or o.preco < atual.preco:
            saida[o.rota_id][o.data] = o
    return saida


@dataclass
class Cenario:
    trechos: list[Opcao]

    @property
    def total(self) -> float:
        return sum(o.preco for o in self.trechos)

    def estadias(self) -> list[tuple[str, datetime, datetime]]:
        """(aeroporto onde fica, de, até) entre um voo e o próximo."""
        return [(a.destino, a.chegada, b.partida) for a, b in zip(self.trechos, self.trechos[1:])]


def cenarios(sequencia: list[dict[date, Opcao]], folga_h: float = 3, min_dias: int = 0, limite: int = 10,
             mesma: Callable[[str, str], bool] = mesma_cidade) -> list[Cenario]:
    """Combinações de datas para os trechos em ordem, sem sobreposição, das mais baratas para as mais caras.

    Regras: cada trecho sai da cidade onde o anterior chegou; sai pelo menos `folga_h` horas depois da
    chegada anterior; e, se `min_dias` > 0, fica pelo menos esse número de dias em cada lugar.
    Trechos sem horário (ex.: Kayak) e ida e volta ficam de fora.
    """
    listas = [[o for o in sorted(t.values(), key=lambda o: o.data)
               if o.partida and o.chegada and o.volta is None] for t in sequencia]
    if not listas or any(not l for l in listas):
        return []
    validos = []
    for combo in product(*listas):
        ok = True
        for a, b in zip(combo, combo[1:]):
            if not mesma(a.destino, b.origem) or b.partida < a.chegada + timedelta(hours=folga_h):
                ok = False
                break
            if min_dias and (b.partida.date() - a.chegada.date()).days < min_dias:
                ok = False
                break
        if ok:
            validos.append(Cenario(list(combo)))
    validos.sort(key=lambda c: (c.total, c.trechos[0].partida))
    return validos[:limite]
