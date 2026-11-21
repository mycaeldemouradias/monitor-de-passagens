"""Calendário de preços, voos guardados por dia e roteiros formados pelas ligações entre viagens.

- `opcoes(...)`: o voo mais barato de cada viagem e data (histórico) → calendário.
- `data/voos.json`: vários voos por busca (os mais baratos + o mais barato de cada faixa de horário),
  da última vez que cada data foi pesquisada → roteiros e conferência de encaixe.
- Ligações: cada viagem pode acontecer "depois de qualquer uma destas" (ex.: Rio→Tampa depois de
  Recife→Galeão OU Recife→Santos Dumont), com intervalo mínimo e, se quiser, máximo.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Protocol

from .models import hora_local


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


class Ligavel(Protocol):  # monitor.models.Rota
    id: str
    depois_de: list[str]
    intervalo_min_horas: float
    intervalo_max_horas: float | None


def _dt(texto: str | None) -> datetime | None:
    """Horário local do aeroporto, sem fuso (os dados antigos do Skiplagged vieram com fuso)."""
    try:
        return hora_local(datetime.fromisoformat(texto)) if texto else None
    except ValueError:
        return None


# ------------------------------------------------------------------ preços por dia (histórico)
def opcoes(linhas: Iterable[dict]) -> dict[str, dict[date, Opcao]]:
    """{viagem: {data de ida: voo mais barato}}, usando a leitura mais recente de cada fonte."""
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


def opcao_de_oferta(o: Any, coleta: datetime) -> Opcao:
    c = o.consulta
    return Opcao(rota_id=c.rota_id, origem=c.origem, destino=c.destino, data=c.ida, volta=c.volta,
                 preco=o.preco, fonte=o.fonte, vendedor=o.vendedor, link=o.link, coleta=coleta,
                 partida=o.ida.partida if o.ida else None, chegada=o.ida.chegada if o.ida else None,
                 escalas=o.ida.conexoes_texto() if o.ida else "")


# ------------------------------------------------------------------ vários voos por dia
def gravar_voos(arq: Path, achadas: dict[str, list[Any]], coleta: datetime) -> None:
    """Guarda os voos encontrados agora ({"chave|fonte": [ofertas]}), mantendo as outras buscas."""
    dados: dict[str, Any] = json.loads(arq.read_text(encoding="utf-8")) if arq.exists() else {}
    for k, ofertas in achadas.items():
        if not ofertas:
            continue
        c = ofertas[0].consulta
        dados[k] = {"coleta": coleta.isoformat(), "rota_id": c.rota_id, "origem": c.origem, "destino": c.destino,
                    "ida": c.ida.isoformat(), "volta": c.volta.isoformat() if c.volta else None,
                    "fonte": ofertas[0].fonte,
                    "voos": [{"preco": o.preco, "vendedor": o.vendedor, "link": o.link,
                              "partida": o.ida.partida.isoformat() if o.ida else None,
                              "chegada": o.ida.chegada.isoformat() if o.ida else None,
                              "escalas": o.ida.conexoes_texto() if o.ida else ""} for o in ofertas]}
    # datas que já passaram saem do arquivo
    hoje = coleta.date().isoformat()
    dados = {k: v for k, v in dados.items() if v["ida"] >= hoje}
    arq.write_text(json.dumps(dados, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def ler_voos(arq: Path) -> dict[str, dict[date, list[Opcao]]]:
    if not arq.exists():
        return {}
    saida: dict[str, dict[date, list[Opcao]]] = {}
    for d in json.loads(arq.read_text(encoding="utf-8")).values():
        dia = date.fromisoformat(d["ida"])
        lista = saida.setdefault(d["rota_id"], {}).setdefault(dia, [])
        for v in d["voos"]:
            lista.append(Opcao(rota_id=d["rota_id"], origem=d["origem"], destino=d["destino"], data=dia,
                               volta=date.fromisoformat(d["volta"]) if d["volta"] else None,
                               preco=float(v["preco"]), fonte=d["fonte"], vendedor=v["vendedor"], link=v["link"],
                               coleta=datetime.fromisoformat(d["coleta"]), partida=_dt(v["partida"]),
                               chegada=_dt(v["chegada"]), escalas=v["escalas"]))
    return saida


def todos_os_voos(melhores: dict[str, dict[date, Opcao]],
                  voos: dict[str, dict[date, list[Opcao]]]) -> dict[str, dict[date, list[Opcao]]]:
    """Voos guardados de cada viagem e data; onde não houver, o mais barato do histórico."""
    saida: dict[str, dict[date, list[Opcao]]] = {}
    for rota in set(melhores) | set(voos):
        for dia in set(melhores.get(rota, {})) | set(voos.get(rota, {})):
            lista = list(voos.get(rota, {}).get(dia) or [])
            m = melhores.get(rota, {}).get(dia)
            if m and m not in lista:
                lista.append(m)
            saida.setdefault(rota, {})[dia] = sorted(lista, key=lambda o: o.preco)
    return saida


# ------------------------------------------------------------------ encaixe entre viagens ligadas
def encaixa(antes: Opcao, depois: Opcao, min_h: float, max_h: float | None = None) -> bool:
    """`depois` sai entre `min_h` e `max_h` horas depois que `antes` chegou?"""
    if not (antes.chegada and depois.partida):
        return False
    intervalo = depois.partida - antes.chegada
    return intervalo >= timedelta(hours=min_h) and (max_h is None or intervalo <= timedelta(hours=max_h))


def _usaveis(dias: dict[date, list[Opcao]]) -> list[Opcao]:
    return [o for lista in dias.values() for o in lista if o.partida and o.chegada and o.volta is None]


def _intervalo_txt(min_h: float, max_h: float | None) -> str:
    f = lambda h: f"{h / 24:g} dia(s)" if h >= 24 and h % 24 == 0 else f"{h:g}h"
    return f"entre {f(min_h)} e {f(max_h)}" if max_h else f"pelo menos {f(min_h)}"


def conflito(anteriores: dict[str, dict[date, list[Opcao]]], b: Opcao, min_h: float, max_h: float | None,
             nomes: dict[str, str]) -> str:
    """Aviso quando o voo `b` não encaixa depois de nenhuma das viagens anteriores ('' se encaixa)."""
    if not b.partida:
        return ""
    candidatos = [a for dias in anteriores.values() for a in _usaveis(dias)]
    if not candidatos or any(encaixa(a, b, min_h, max_h) for a in candidatos):
        return ""
    quais = " ou ".join(f"'{nomes.get(r, r)}'" for r in anteriores)
    return f"não encaixa depois de {quais} ({_intervalo_txt(min_h, max_h)} entre a chegada e esta saída)"


@dataclass
class Cenario:
    trechos: list[Opcao]

    @property
    def total(self) -> float:
        return sum(o.preco for o in self.trechos)

    def estadias(self) -> list[tuple[str, datetime, datetime]]:
        """(aeroporto onde fica, de, até) entre um voo e o próximo."""
        return [(a.destino, a.chegada, b.partida) for a, b in zip(self.trechos, self.trechos[1:])]


def combinar(pernas: list[list[Opcao]], intervalos: list[tuple[float, float | None]], limite: int = 10,
             por_opcao: int = 5) -> list[Cenario]:
    """Os roteiros mais baratos escolhendo 1 voo por perna, cada um encaixando no anterior.

    Programação dinâmica: para cada voo, guarda os `por_opcao` caminhos mais baratos que terminam nele.
    """
    if not pernas or any(not p for p in pernas):
        return []
    melhores: dict[Opcao, list[tuple[float, list[Opcao]]]] = {o: [(o.preco, [o])] for o in pernas[0]}
    for perna, (min_h, max_h) in zip(pernas[1:], intervalos):
        novos: dict[Opcao, list[tuple[float, list[Opcao]]]] = {}
        for b in perna:
            caminhos_b = [(t + b.preco, c + [b]) for a, lista in melhores.items()
                          if encaixa(a, b, min_h, max_h) for t, c in lista]
            if caminhos_b:
                novos[b] = sorted(caminhos_b, key=lambda x: x[0])[:por_opcao]
        melhores = novos
        if not melhores:
            return []
    finais = sorted((x for lista in melhores.values() for x in lista), key=lambda x: (x[0], x[1][0].partida))
    return [Cenario(c) for _, c in finais[:limite]]


def caminhos(rotas: list[Ligavel]) -> list[list[str]]:
    """Todas as sequências formadas pelas ligações, do começo ao fim (cada ramo é um caminho)."""
    ids = {r.id for r in rotas}
    seguintes: dict[str, list[str]] = {r.id: [] for r in rotas}
    tem_anterior = set()
    for r in rotas:
        for a in r.depois_de:
            if a in ids:
                seguintes[a].append(r.id)
                tem_anterior.add(r.id)
    saida: list[list[str]] = []

    def andar(cam: list[str]) -> None:
        prox = [s for s in seguintes[cam[-1]] if s not in cam]
        if not prox:
            if len(cam) > 1:
                saida.append(cam)
            return
        for s in prox:
            andar(cam + [s])

    for r in rotas:
        if r.id not in tem_anterior and seguintes[r.id]:
            andar([r.id])
    return saida


def roteiros_pelas_ligacoes(rotas: list[Ligavel], voos: dict[str, dict[date, list[Opcao]]],
                            limite: int = 10) -> tuple[list[Cenario], list[str]]:
    """Os roteiros mais baratos entre todos os caminhos das ligações. Devolve também as viagens sem voos."""
    por_id = {r.id: r for r in rotas}
    todos: list[Cenario] = []
    sem_voos: set[str] = set()
    for cam in caminhos(rotas):
        pernas = [_usaveis(voos.get(i, {})) for i in cam]
        sem_voos |= {i for i, p in zip(cam, pernas) if not p}
        intervalos = [(por_id[i].intervalo_min_horas, por_id[i].intervalo_max_horas) for i in cam[1:]]
        todos += combinar(pernas, intervalos, limite)
    todos.sort(key=lambda c: (c.total, c.trechos[0].partida))
    return todos[:limite], sorted(sem_voos)
