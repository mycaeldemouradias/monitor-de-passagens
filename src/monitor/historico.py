"""Histórico de preços (CSV) e estado entre execuções (JSON), guardados em data/."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Oferta, Trecho

COLUNAS = [
    "data_hora_utc", "chave", "rota_id", "origem", "destino", "ida", "volta",
    "adultos", "fonte", "preco", "moeda", "companhia", "detalhes", "link",
    # detalhes dos voos (horários locais de cada aeroporto; conexões "GIG 220min; BSB 60min")
    "ida_partida", "ida_chegada", "ida_voos", "ida_escalas", "ida_conexoes", "ida_desce_em",
    "volta_partida", "volta_chegada", "volta_voos", "volta_escalas", "volta_conexoes", "volta_desce_em",
    "vendedor", "avisos", "nivel_preco",
]


def _colunas_trecho(prefixo: str, t: Trecho | None) -> dict[str, Any]:
    if t is None:
        return {}
    return {
        f"{prefixo}_partida": t.partida.isoformat(timespec="minutes"),
        f"{prefixo}_chegada": t.chegada.isoformat(timespec="minutes"),
        f"{prefixo}_voos": ", ".join(t.voos), f"{prefixo}_escalas": t.escalas,
        f"{prefixo}_conexoes": t.conexoes_texto(), f"{prefixo}_desce_em": t.desce_em,
    }


def agora() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class Historico:
    def __init__(self, pasta: Path):
        self.pasta = pasta
        self.pasta.mkdir(parents=True, exist_ok=True)
        self.csv = pasta / "historico_precos.csv"
        self._por_chave: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        self._carregar()

    def _carregar(self) -> None:
        if not self.csv.exists():
            return
        with open(self.csv, newline="", encoding="utf-8") as f:
            for linha in csv.DictReader(f):
                try:
                    ts = datetime.fromisoformat(linha["data_hora_utc"])
                    self._por_chave[linha["chave"]].append((ts, float(linha["preco"])))
                except (KeyError, ValueError):
                    continue

    def precos(self, chave: str) -> list[tuple[datetime, float]]:
        """Observações anteriores desta busca (todas as fontes)."""
        return list(self._por_chave.get(chave, []))

    def registrar(self, ofertas: list[Oferta], quando: datetime) -> None:
        if not ofertas:
            return
        self._migrar_cabecalho()
        novo = not self.csv.exists()
        with open(self.csv, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUNAS)
            if novo:
                w.writeheader()
            for o in ofertas:
                c = o.consulta
                w.writerow({
                    "data_hora_utc": quando.isoformat(), "chave": c.chave, "rota_id": c.rota_id,
                    "origem": c.origem, "destino": c.destino, "ida": c.ida.isoformat(),
                    "volta": c.volta.isoformat() if c.volta else "", "adultos": c.adultos,
                    "fonte": o.fonte, "preco": f"{o.preco:.2f}", "moeda": o.moeda,
                    "companhia": o.companhia, "detalhes": o.detalhes, "link": o.link,
                    **_colunas_trecho("ida", o.ida), **_colunas_trecho("volta", o.volta),
                    "vendedor": o.vendedor, "avisos": " | ".join(o.avisos), "nivel_preco": o.nivel_preco,
                })
                self._por_chave[c.chave].append((quando, o.preco))

    def _migrar_cabecalho(self) -> None:
        """Se o CSV existente tem colunas antigas, reescreve com as novas (valores vazios)."""
        if not self.csv.exists():
            return
        with open(self.csv, newline="", encoding="utf-8") as f:
            leitor = csv.DictReader(f)
            if leitor.fieldnames == COLUNAS:
                return
            linhas = list(leitor)
        with open(self.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUNAS, extrasaction="ignore")
            w.writeheader()
            w.writerows(linhas)


class Estado:
    """JSON simples: últimos alertas, última consulta por busca e última rodada por fonte."""

    def __init__(self, pasta: Path):
        self.arquivo = pasta / "estado.json"
        self.dados: dict[str, Any] = {"alertas": {}, "ultima_consulta": {}, "ultima_rodada": {}, "pausa": {}}
        if self.arquivo.exists():
            self.dados.update(json.loads(self.arquivo.read_text(encoding="utf-8")))

    def salvar(self) -> None:
        self.arquivo.write_text(
            json.dumps(self.dados, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )

    # --- rodízio de consultas ---
    def ultima_consulta(self, fonte: str, chave: str) -> str:
        return self.dados["ultima_consulta"].get(f"{fonte}|{chave}", "")

    def marcar_consulta(self, fonte: str, chave: str, quando: datetime) -> None:
        self.dados["ultima_consulta"][f"{fonte}|{chave}"] = quando.isoformat()

    # --- intervalo entre rodadas de uma fonte (ex.: Google Flights a cada 8h) ---
    def ultima_rodada(self, fonte: str) -> datetime | None:
        v = self.dados["ultima_rodada"].get(fonte)
        return datetime.fromisoformat(v) if v else None

    def marcar_rodada(self, fonte: str, quando: datetime) -> None:
        self.dados["ultima_rodada"][fonte] = quando.isoformat()

    # --- descanso de uma fonte depois de bloqueio ---
    def pausada_ate(self, fonte: str) -> datetime | None:
        v = self.dados["pausa"].get(fonte)
        return datetime.fromisoformat(v) if v else None

    def pausar(self, fonte: str, ate: datetime) -> None:
        self.dados["pausa"][fonte] = ate.isoformat()

    # --- deduplicação de alertas ---
    def ultimo_alerta(self, chave: str) -> dict[str, Any] | None:
        return self.dados["alertas"].get(chave)

    def registrar_alerta(self, chave: str, preco: float, quando: datetime) -> None:
        self.dados["alertas"][chave] = {"preco": preco, "data_hora_utc": quando.isoformat()}
