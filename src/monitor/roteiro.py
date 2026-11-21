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


def cenarios(sequencia: list[dict[date, Opcao]], folga_h: float | list[float] = 3, min_dias: int = 0,
             limite: int = 10, mesma: Callable[[str, str], bool] | None = mesma_cidade) -> list[Cenario]:
    """Combinações de datas para os trechos em ordem, sem sobreposição, das mais baratas para as mais caras.

    Regras: cada trecho sai da cidade onde o anterior chegou; sai pelo menos `folga_h` horas depois da
    chegada anterior; e, se `min_dias` > 0, fica pelo menos esse número de dias em cada lugar.
    Trechos sem horário (ex.: Kayak) e ida e volta ficam de fora.
    """
    listas = [[o for o in sorted(t.values(), key=lambda o: o.data)
               if o.partida and o.chegada and o.volta is None] for t in sequencia]
    if not listas or any(not l for l in listas):
        return []
    folgas = folga_h if isinstance(folga_h, list) else [folga_h] * (len(listas) - 1)
    validos = []
    for combo in product(*listas):
        ok = True
        for (a, b), folga in zip(zip(combo, combo[1:]), folgas):
            if (mesma and not mesma(a.destino, b.origem)) or b.partida < a.chegada + timedelta(hours=folga):
                ok = False
                break
            if min_dias and (b.partida.date() - a.chegada.date()).days < min_dias:
                ok = False
                break
        if ok:
            validos.append(Cenario(list(combo)))
    validos.sort(key=lambda c: (c.total, c.trechos[0].partida))
    return validos[:limite]


def encaixa(antes: Opcao, depois: Opcao, folga_h: float) -> bool:
    """O voo `depois` sai pelo menos `folga_h` horas depois que o voo `antes` chegou?"""
    if not (antes.chegada and depois.partida):
        return False
    return depois.partida >= antes.chegada + timedelta(hours=folga_h)


def melhor_par(antes: dict[date, Opcao], depois: dict[date, Opcao], folga_h: float) -> tuple[Opcao, Opcao] | None:
    """A combinação mais barata (uma data de cada viagem) que respeita o intervalo."""
    pares = [(a, b) for a in antes.values() for b in depois.values() if encaixa(a, b, folga_h)]
    return min(pares, key=lambda p: (p[0].preco + p[1].preco, p[1].partida)) if pares else None


def conflito(antes: dict[date, Opcao], b: Opcao, folga_h: float, nome_antes: str) -> str:
    """Aviso quando o voo `b` não encaixa com a viagem anterior ('' se encaixa com alguma data dela)."""
    if not b.partida or not antes:
        return ""
    if any(encaixa(a, b, folga_h) for a in antes.values()):
        mais_barato = min(antes.values(), key=lambda a: a.preco)
        if encaixa(mais_barato, b, folga_h):
            return ""
        return (f"o voo mais barato de '{nome_antes}' ({mais_barato.data:%d/%m}) chega "
                f"{mais_barato.chegada:%d/%m %H:%M}: não dá tempo; só encaixa com outra data dela")
    chegadas = [a.chegada for a in antes.values() if a.chegada]
    quando = f" (a chegada mais cedo é {min(chegadas):%d/%m %H:%M})" if chegadas else ""
    return f"não encaixa com nenhuma data de '{nome_antes}' (precisa de {folga_h:g}h depois da chegada){quando}"


def opcao_de_oferta(o, coleta: datetime) -> Opcao:
    c = o.consulta
    return Opcao(rota_id=c.rota_id, origem=c.origem, destino=c.destino, data=c.ida, volta=c.volta,
                 preco=o.preco, fonte=o.fonte, vendedor=o.vendedor, link=o.link, coleta=coleta,
                 partida=o.ida.partida if o.ida else None, chegada=o.ida.chegada if o.ida else None,
                 escalas=o.ida.conexoes_texto() if o.ida else "")
