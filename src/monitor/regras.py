"""Regras que decidem se um preço merece alerta."""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import mean

from .config import Geral
from .models import Oferta, Rota


def brl(valor: float) -> str:
    return "R$ " + f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def avaliar(
    oferta: Oferta,
    anteriores: list[tuple[datetime, float]],
    rota: Rota,
    geral: Geral,
    agora: datetime,
) -> list[str]:
    """Devolve os motivos do alerta (lista vazia = sem alerta).

    `anteriores` são observações de execuções passadas da mesma busca (todas as fontes).
    """
    motivos: list[str] = []
    preco = oferta.preco

    # 1) Abaixo do preço-alvo definido para a rota
    if rota.preco_alvo is not None and preco <= rota.preco_alvo:
        motivos.append(f"abaixo do seu preço-alvo ({brl(rota.preco_alvo)})")

    # 2) Menor preço já visto (exige um mínimo de observações para não disparar na 1ª execução)
    if len(anteriores) >= geral.min_observacoes:
        minimo = min(p for _, p in anteriores)
        if preco < minimo:
            motivos.append(f"menor preço já visto (anterior: {brl(minimo)})")

    # 3) Queda de X% em relação à média da janela recente
    pct = rota.queda_pct if rota.queda_pct is not None else geral.queda_pct_padrao
    limite = agora - timedelta(days=geral.janela_media_dias)
    recentes = [p for ts, p in anteriores if ts >= limite]
    if pct and len(recentes) >= geral.min_observacoes:
        media = mean(recentes)
        if preco <= media * (1 - pct / 100):
            queda = (1 - preco / media) * 100
            motivos.append(
                f"{queda:.0f}% abaixo da média de {geral.janela_media_dias} dias ({brl(media)})"
            )
    return motivos


def deve_reavisar(ultimo: dict | None, preco: float, geral: Geral, agora: datetime) -> bool:
    """Evita repetir o mesmo alerta: só reavisa se o preço caiu de novo ou passou o prazo."""
    if not ultimo:
        return True
    if preco < float(ultimo["preco"]) - 0.99:
        return True
    quando = datetime.fromisoformat(ultimo["data_hora_utc"])
    return agora - quando >= timedelta(hours=geral.reavisar_apos_horas)
