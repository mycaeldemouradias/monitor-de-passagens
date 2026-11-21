"""Leitura e gravação das viagens (config/rotas.yaml) e das pausas (config/pausas.yaml) pelo painel.

O painel reescreve só a parte `rotas:` do rotas.yaml. O cabeçalho e o bloco `geral:`, com os
comentários, ficam como estão. Antes de gravar, tudo é validado com as mesmas regras do monitor,
para nunca existir um arquivo "quebrado".
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .aeroportos import existe
from .config import PAUSAS_NOME, Geral, _rota, consultas_da_rota
from .models import Consulta

INICIO_ROTAS = re.compile(r"^rotas:.*$", re.M)
#: ordem dos campos de cada viagem no arquivo
ORDEM = ["id", "nome", "ativa", "origem", "destino", "ida", "volta", "duracao_dias",
         "adultos", "preco_alvo", "queda_pct", "fontes"]


def _texto_datas(spec: Any) -> Any:
    """Troca objetos date por "AAAA-MM-DD" (o YAML lê datas sem aspas como date)."""
    if isinstance(spec, date):
        return spec.isoformat()
    if isinstance(spec, dict):
        return {k: _texto_datas(v) for k, v in spec.items()}
    if isinstance(spec, list):
        return [_texto_datas(v) for v in spec]
    return spec


def ler_viagens(caminho: Path) -> list[dict[str, Any]]:
    bruto = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
    return [_texto_datas(v) for v in bruto.get("rotas") or []]


def criar_id(nome: str, existentes: set[str]) -> str:
    """'Férias em SP' → 'ferias-em-sp' (com -2, -3… se já existir)."""
    base = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-") or "viagem"
    novo, n = base, 2
    while novo in existentes:
        novo, n = f"{base}-{n}", n + 1
    return novo


def consultas(viagem: dict[str, Any], hoje: date | None = None) -> list[Consulta]:
    """Buscas que a viagem gera (por fonte), sem as datas que já passaram."""
    return consultas_da_rota(_rota(viagem, Geral()), "BRL", hoje)


def validar(viagens: list[dict[str, Any]]) -> list[str]:
    erros = []
    ids = [str(v.get("id", "")) for v in viagens]
    for i in sorted({i for i in ids if ids.count(i) > 1}):
        erros.append(f"Há duas viagens com o mesmo código '{i}'.")
    for v in viagens:
        nome = v.get("nome") or v.get("id") or "(sem nome)"
        try:
            r = _rota(v, Geral())
        except (ValueError, KeyError, TypeError) as e:
            erros.append(f"{nome}: {e}")
            continue
        for campo, codigo in (("origem", r.origem), ("destino", r.destino)):
            if not re.fullmatch(r"[A-Z]{3}", codigo):
                erros.append(f"{nome}: {campo} '{codigo}' não é um código de aeroporto (3 letras).")
            elif not existe(codigo):
                erros.append(f"{nome}: {campo} '{codigo}' não está na lista de aeroportos comerciais "
                             "(config/aeroportos_comerciais.csv).")
        if r.origem == r.destino:
            erros.append(f"{nome}: origem e destino são iguais.")
        if not r.idas:
            erros.append(f"{nome}: escolha pelo menos uma data de ida.")
        if "fontes" in v and not v["fontes"]:  # sem a linha, vale fontes_padrao
            erros.append(f"{nome}: escolha pelo menos uma fonte.")
        if not 1 <= r.adultos <= 9:
            erros.append(f"{nome}: número de adultos deve ser de 1 a 9.")
        if r.voltas and r.idas and max(r.voltas) < min(r.idas):
            erros.append(f"{nome}: a volta é antes da ida.")
    return erros


def _bloco(v: dict[str, Any]) -> str:
    ordenado = {k: v[k] for k in ORDEM if k in v and v[k] not in (None, "", [])}
    ordenado.update({k: x for k, x in v.items() if k not in ordenado and x not in (None, "", [])})
    texto = yaml.safe_dump([_texto_datas(ordenado)], sort_keys=False, allow_unicode=True,
                           default_flow_style=None, width=1000)
    return "".join("  " + linha + "\n" for linha in texto.splitlines())


def salvar_viagens(caminho: Path, viagens: list[dict[str, Any]]) -> None:
    """Valida e grava. Levanta ValueError (com a lista de problemas) sem tocar no arquivo."""
    erros = validar(viagens)
    if erros:
        raise ValueError("\n".join(erros))
    texto = caminho.read_text(encoding="utf-8")
    m = INICIO_ROTAS.search(texto)
    cabecalho = texto[: m.start()] if m else texto.rstrip() + "\n\n"
    corpo = ("rotas:\n" + "\n".join(_bloco(v) for v in viagens)) if viagens else "rotas: []\n"
    novo = cabecalho + corpo
    # confere que o arquivo novo é lido exatamente como as viagens pedidas
    relido = [_texto_datas(v) for v in (yaml.safe_load(novo) or {}).get("rotas") or []]
    esperado = [{k: x for k, x in _texto_datas(v).items() if x not in (None, "", [])} for v in viagens]
    if relido != esperado:
        raise ValueError("Erro interno ao montar o rotas.yaml; nada foi gravado.")
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(novo, encoding="utf-8")
    tmp.replace(caminho)


def salvar_pausas(pasta_config: Path, fontes_desligadas: list[str]) -> None:
    texto = ("# Fontes desligadas pelo painel (tela \"Pausar e parar\"). O monitor pula estas fontes.\n"
             + yaml.safe_dump({"fontes_desligadas": sorted(set(fontes_desligadas))},
                              allow_unicode=True, default_flow_style=False))
    (pasta_config / PAUSAS_NOME).write_text(texto, encoding="utf-8")
