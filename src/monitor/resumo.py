"""Resumo diário no Telegram: o menor preço de cada viagem nas últimas 24h.

Sai uma vez por dia, na primeira rodada a partir de `resumo_diario_hora` (hora de Recife). Durante o
dia, os avisos só saem quando o preço bate uma das regras (preço-alvo, menor já visto ou queda de X%).
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Geral
from .historico import Estado
from .models import Rota
from .regras import brl
from .roteiro import combinar, conflito, ler_voos, opcoes, sem_passado, todos_os_voos

FUSO = ZoneInfo("America/Recife")


def deve_mandar_resumo(estado: Estado, geral: Geral, agora: datetime) -> bool:
    if geral.resumo_diario_hora is None:
        return False
    local = agora.astimezone(FUSO)
    return local.hour >= geral.resumo_diario_hora and estado.dados.get("ultimo_resumo") != local.date().isoformat()


def marcar_resumo(estado: Estado, agora: datetime) -> None:
    estado.dados["ultimo_resumo"] = agora.astimezone(FUSO).date().isoformat()


def _datas(linha: dict) -> str:
    ida = date.fromisoformat(linha["ida"]).strftime("%d/%m")
    return ida + (f" → {date.fromisoformat(linha['volta']):%d/%m}" if linha.get("volta") else " (só ida)")


def montar_resumo(arq_csv: Path, rotas: list[Rota], agora: datetime, todas: list[Rota] | None = None,
                  arq_voos: Path | None = None) -> str:
    linhas = []
    if arq_csv.exists():
        with open(arq_csv, newline="", encoding="utf-8") as f:
            for l in csv.DictReader(f):
                try:
                    l["_ts"], l["_preco"] = datetime.fromisoformat(l["data_hora_utc"]), float(l["preco"])
                except (KeyError, ValueError):
                    continue
                linhas.append(l)
    hoje, ontem = agora - timedelta(hours=24), agora - timedelta(hours=48)
    por_id = {r.id: r for r in (todas or rotas)}
    voos = sem_passado(todos_os_voos(opcoes(linhas), ler_voos(arq_voos) if arq_voos else {}), agora) \
        if any(r.depois_de for r in rotas) else {}
    nomes = {r.id: r.nome or r.id for r in por_id.values()}
    partes = [f"☀️ Resumo das passagens — {agora.astimezone(FUSO):%d/%m}",
              "Menor preço de cada viagem nas últimas 24h:"]
    for rota in rotas:
        da_rota = [l for l in linhas if l.get("rota_id") == rota.id]
        recentes = [l for l in da_rota if l["_ts"] >= hoje]
        anteriores = [l for l in da_rota if ontem <= l["_ts"] < hoje]
        titulo = f"\n✈️ {rota.nome or rota.id} — {rota.origem} → {rota.destino}"
        if not recentes:
            partes.append(f"{titulo}\nsem preço nas últimas 24h")
            continue
        m = min(recentes, key=lambda l: l["_preco"])
        onde = m["fonte"] + (f" ({m['vendedor']})" if m.get("vendedor") else "")
        texto = f"{titulo}\n💰 {brl(m['_preco'])} · {_datas(m)} · {onde}"
        if anteriores:
            antes = min(l["_preco"] for l in anteriores)
            dif = m["_preco"] - antes
            texto += (f"\n   {'▼' if dif < 0 else '▲'} {brl(abs(dif))} vs. ontem ({brl(antes)})"
                      if abs(dif) >= 0.01 else "\n   = igual a ontem")
        menor = min(l["_preco"] for l in da_rota)
        if menor < m["_preco"]:
            texto += f"\n   menor já visto: {brl(menor)}"
        if rota.preco_alvo:
            texto += f"\n   seu aviso: abaixo de {brl(rota.preco_alvo)}"
        anteriores = [a for a in rota.depois_de if a in por_id]
        if anteriores:
            esta = next((o for o in voos.get(rota.id, {}).get(date.fromisoformat(m["ida"]), [])
                         if abs(o.preco - m["_preco"]) < 0.01), None)
            aviso = conflito({a: voos.get(a, {}) for a in anteriores}, esta, rota.intervalo_min_horas,
                             rota.intervalo_max_horas, nomes) if esta else ""
            if aviso:
                texto += f"\n   ⚠️ o mais barato {aviso}"
            usaveis = lambda r: [o for d in voos.get(r, {}).values() for o in d if o.partida and o.chegada]
            pares = [c for a in anteriores for c in combinar([usaveis(a), usaveis(rota.id)],
                                                            [(rota.intervalo_min_horas, rota.intervalo_max_horas)], 1)]
            if pares:
                p = min(pares, key=lambda c: c.total)
                a, b = p.trechos
                texto += (f"\n   🔗 encaixe mais barato: {nomes.get(a.rota_id)} {a.data:%d/%m} + esta "
                          f"{b.data:%d/%m} = {brl(p.total)}")
            else:
                texto += f"\n   🔗 nenhuma data encaixa depois de {' ou '.join(nomes.get(a, a) for a in anteriores)}"
        partes.append(texto)
    partes.append("\nDurante o dia, só aviso quando o preço bater uma das suas regras.")
    return "\n".join(partes)
