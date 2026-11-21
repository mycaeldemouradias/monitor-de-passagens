"""Leitura de config/rotas.yaml e config/sites.yaml e expansão das datas em consultas."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import product
from pathlib import Path
from typing import Any

import yaml

from .models import Consulta, Rota

RAIZ = Path(__file__).resolve().parents[2]
CONFIG_PADRAO = RAIZ / "config" / "rotas.yaml"
SITES_PADRAO = RAIZ / "config" / "sites.yaml"
DADOS_PADRAO = RAIZ / "data"
#: fontes desligadas pelo painel ("Pausar e parar"); fica ao lado do rotas.yaml
PAUSAS_NOME = "pausas.yaml"


@dataclass
class Geral:
    moeda: str = "BRL"
    fontes_padrao: list[str] = field(default_factory=lambda: ["google_flights"])
    google_flights_a_cada_horas: float = 10
    orcamento_serpapi_mes: int = 240
    skiplagged_max_por_rodada: int = 15
    pausa_apos_bloqueio_horas: float = 6
    min_observacoes: int = 3
    janela_media_dias: int = 14
    queda_pct_padrao: float = 15.0
    reavisar_apos_horas: int = 72
    max_alertas_por_execucao: int = 10
    pausa_navegador_s: float = 4.0
    resumo_diario_hora: int | None = 7  # hora (Recife) a partir da qual sai o resumo do dia; None = desligado


@dataclass
class Config:
    geral: Geral
    rotas: list[Rota]
    sites: dict[str, dict[str, Any]]
    fontes_desligadas: list[str] = field(default_factory=list)


def _parse_data(valor: Any) -> date:
    if isinstance(valor, date):
        return valor
    return date.fromisoformat(str(valor).strip())


def expandir_datas(spec: Any) -> list[date]:
    """Aceita: "2026-12-20" | ["2026-12-20", "2026-12-21"] | {de: ..., ate: ...}."""
    if spec is None:
        return []
    if isinstance(spec, dict):
        ini, fim = _parse_data(spec["de"]), _parse_data(spec["ate"])
        if fim < ini:
            raise ValueError(f"Intervalo de datas invertido: {spec}")
        passo = int(spec.get("a_cada_dias", 1))
        return [ini + timedelta(days=i) for i in range(0, (fim - ini).days + 1, passo)]
    if isinstance(spec, (list, tuple)):
        datas: list[date] = []
        for item in spec:
            datas.extend(expandir_datas(item))
        return sorted(set(datas))
    return [_parse_data(spec)]


def intervalo_min(v: dict[str, Any]) -> float:
    """Intervalo mínimo da ligação em horas. 0 vale (pode seguir logo); vazio = 3h."""
    x = v.get("intervalo_min_horas")
    return 3.0 if x is None or x == "" else float(x)


def _lista_ids(valor: Any) -> list[str]:
    """depois_de: "a" ou ["a", "b"] → ["a", "b"]."""
    if not valor:
        return []
    return [str(v) for v in (valor if isinstance(valor, list) else [valor]) if v]


def _rota(bruta: dict[str, Any], geral: Geral) -> Rota:
    for campo in ("id", "origem", "destino", "ida"):
        if campo not in bruta:
            raise ValueError(f"Rota sem o campo obrigatório '{campo}': {bruta}")
    duracoes = bruta.get("duracao_dias") or []
    if isinstance(duracoes, int):
        duracoes = [duracoes]
    if bruta.get("volta") and duracoes:
        raise ValueError(f"Rota {bruta['id']}: use 'volta' OU 'duracao_dias', não os dois.")
    return Rota(
        id=str(bruta["id"]),
        origem=str(bruta["origem"]).upper(),
        destino=str(bruta["destino"]).upper(),
        idas=expandir_datas(bruta["ida"]),
        voltas=expandir_datas(bruta.get("volta")),
        duracoes=[int(d) for d in duracoes],
        adultos=int(bruta.get("adultos", 1)),
        cabine=str(bruta.get("cabine", "economica")),
        preco_alvo=float(bruta["preco_alvo"]) if bruta.get("preco_alvo") is not None else None,
        queda_pct=float(bruta["queda_pct"]) if bruta.get("queda_pct") is not None else None,
        fontes=list(bruta.get("fontes") or geral.fontes_padrao),
        ativa=bool(bruta.get("ativa", True)),
        nome=str(bruta.get("nome") or ""),
        depois_de=_lista_ids(bruta.get("depois_de")),
        intervalo_min_horas=intervalo_min(bruta),
        intervalo_max_horas=float(bruta["intervalo_max_horas"]) if bruta.get("intervalo_max_horas") else None,
    )


def carregar(caminho: Path = CONFIG_PADRAO, sites: Path = SITES_PADRAO) -> Config:
    with open(caminho, encoding="utf-8") as f:
        bruto = yaml.safe_load(f) or {}
    geral_bruto = bruto.get("geral") or {}
    campos_validos = Geral.__dataclass_fields__.keys()
    desconhecidos = set(geral_bruto) - set(campos_validos)
    if desconhecidos:
        raise ValueError(f"Campos desconhecidos em 'geral': {sorted(desconhecidos)}")
    geral = Geral(**geral_bruto)
    rotas = [_rota(r, geral) for r in (bruto.get("rotas") or [])]
    ids = [r.id for r in rotas]
    if len(ids) != len(set(ids)):
        raise ValueError("Há rotas com 'id' repetido em rotas.yaml.")
    sites_cfg: dict[str, dict[str, Any]] = {}
    if sites.exists():
        with open(sites, encoding="utf-8") as f:
            sites_cfg = (yaml.safe_load(f) or {}).get("sites") or {}
    return Config(geral=geral, rotas=rotas, sites=sites_cfg,
                  fontes_desligadas=ler_pausas(Path(caminho).parent))


def ler_pausas(pasta_config: Path) -> list[str]:
    arq = pasta_config / PAUSAS_NOME
    if not arq.exists():
        return []
    with open(arq, encoding="utf-8") as f:
        return [str(x) for x in ((yaml.safe_load(f) or {}).get("fontes_desligadas") or [])]


def consultas_da_rota(rota: Rota, moeda: str, hoje: date | None = None) -> list[Consulta]:
    """Gera todas as combinações de datas da rota, ignorando datas que já passaram."""
    hoje = hoje or date.today()
    idas = [d for d in rota.idas if d >= hoje]
    pares: list[tuple[date, date | None]]
    if rota.voltas:
        pares = [(i, v) for i, v in product(idas, rota.voltas) if v >= i]
    elif rota.duracoes:
        pares = [(i, i + timedelta(days=d)) for i, d in product(idas, rota.duracoes)]
    else:
        pares = [(i, None) for i in idas]
    return [
        Consulta(
            rota_id=rota.id, origem=rota.origem, destino=rota.destino, ida=i, volta=v,
            adultos=rota.adultos, cabine=rota.cabine, moeda=moeda,
        )
        for i, v in pares
    ]
