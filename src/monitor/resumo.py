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
from .roteiro import conflito, melhor_par, opcoes

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


def montar_resumo(arq_csv: Path, rotas: list[Rota], agora: datetime, todas: list[Rota] | None = None) -> str:
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
    op = opcoes(linhas) if any(r.depois_de for r in rotas) else {}
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
        if rota.depois_de and rota.depois_de in por_id:
            anterior = por_id[rota.depois_de]
            nome_ant = anterior.nome or anterior.id
            par = melhor_par(op.get(anterior.id, {}), op.get(rota.id, {}), rota.intervalo_min_horas)
            esta = op.get(rota.id, {}).get(date.fromisoformat(m["ida"]))
            aviso = conflito(op.get(anterior.id, {}), esta, rota.intervalo_min_horas, nome_ant) if esta else ""
            if aviso:
                texto += f"\n   ⚠️ {aviso}"
            if par:
                texto += (f"\n   🔗 encaixe mais barato com '{nome_ant}': {par[0].data:%d/%m} + {par[1].data:%d/%m}"
                          f" = {brl(par[0].preco + par[1].preco)}")
            else:
                texto += f"\n   🔗 nenhuma data encaixa depois de '{nome_ant}'"
        partes.append(texto)
    partes.append("\nDurante o dia, só aviso quando o preço bater uma das suas regras.")
    return "\n".join(partes)
