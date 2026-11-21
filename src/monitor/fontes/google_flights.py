"""Google Flights via SerpApi (engine=google_flights).

Fonte principal: estável e cobre praticamente todas as companhias.
Precisa da variável SERPAPI_KEY. O plano gratuito da SerpApi tem cota mensal que renova
no dia em que a conta foi criada (não no dia 1º). Por isso, a cada rodada o monitor
consulta o saldo real na SerpApi (consulta gratuita) e libera a cota aos poucos até a
renovação. Testes e simulações entram na conta automaticamente.
"""
from __future__ import annotations

import calendar
import math
import os
import time
from datetime import date, datetime, timedelta, timezone

import requests

from ..models import Consulta, Oferta, Trecho
from ..regras import brl
from .base import Fonte

URL = "https://serpapi.com/search.json"
URL_CONTA = "https://serpapi.com/account.json"  # não gasta busca
CABINES = {"economica": 1, "premium": 2, "executiva": 3, "primeira": 4}


def _um_mes_antes(d: date) -> date:
    ano, mes = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    return date(ano, mes, min(d.day, calendar.monthrange(ano, mes)[1]))


def saldo_serpapi(chave: str) -> dict:
    """{'usados': n, 'renovacao': date, 'cota': n} lidos da conta (a consulta é gratuita)."""
    for tentativa in range(3):  # falhas de rede momentâneas acontecem (visto em 20/11/2026)
        try:
            r = requests.get(URL_CONTA, params={"api_key": chave}, timeout=30)
            r.raise_for_status()
            break
        except requests.RequestException:
            if tentativa == 2:
                raise
            time.sleep(5)
    d = r.json()
    cota = int(d["searches_per_month"])
    return {"usados": cota - int(d["plan_searches_left"]), "cota": cota,
            "renovacao": date.fromisoformat(d["plan_renewal_date"])}


def cota_liberada(orcamento: int, usados: int, renovacao: date, horas_por_rodada: float,
                  agora: datetime) -> int:
    """Distribui o orçamento de forma uniforme entre a última renovação e a próxima.

    Até um dado momento, só fica liberada a fração do orçamento proporcional ao tempo
    decorrido no ciclo (contando a rodada atual). Assim a cota nunca acaba antes da renovação.
    """
    fim = datetime.combine(renovacao, datetime.min.time(), timezone.utc)
    inicio = datetime.combine(_um_mes_antes(renovacao), datetime.min.time(), timezone.utc)
    fracao = (agora - inicio + timedelta(hours=horas_por_rodada)) / (fim - inicio)
    liberado = math.floor(orcamento * min(1.0, max(0.0, fracao)))
    return max(0, min(liberado - usados, GoogleFlights.MAX_POR_EXECUCAO))


class GoogleFlights(Fonte):
    nome = "google_flights"

    #: teto de buscas numa única execução (evita gastar o acumulado de uma vez)
    MAX_POR_EXECUCAO = 5
    #: folga para atrasos do agendamento do GitHub (ex.: rodada das 8h17 que começa às 8h40)
    TOLERANCIA = timedelta(hours=1)

    def __init__(self, orcamento: int, a_cada_horas: float, ultima_rodada: datetime | None,
                 agora: datetime):
        self.chave = os.getenv("SERPAPI_KEY", "")
        self.limite_por_execucao = 0
        self._motivo = ""
        if not self.chave:
            self._motivo = "SERPAPI_KEY não definida"
            return
        if ultima_rodada and agora - ultima_rodada < timedelta(hours=a_cada_horas) - self.TOLERANCIA:
            proxima = ultima_rodada + timedelta(hours=a_cada_horas)
            self._motivo = f"roda a cada {a_cada_horas:g}h (próxima a partir de {proxima:%d/%m %H:%M} UTC)"
            return
        try:
            s = saldo_serpapi(self.chave)
        except Exception as e:  # noqa: BLE001 — sem saldo confiável, não arrisca a cota
            self._motivo = f"não consegui consultar o saldo da SerpApi ({type(e).__name__})"
            return
        self.limite_por_execucao = cota_liberada(
            min(orcamento, s["cota"]), s["usados"], s["renovacao"], a_cada_horas, agora)
        if self.limite_por_execucao <= 0:
            self._motivo = (f"cota liberada até agora já foi usada ({s['usados']}/{s['cota']}, "
                            f"renova em {s['renovacao']:%d/%m})")

    def disponivel(self) -> tuple[bool, str]:
        return (not self._motivo, self._motivo)

    def buscar(self, c: Consulta) -> list[Oferta]:
        params = {
            "engine": "google_flights",
            "departure_id": c.origem,
            "arrival_id": c.destino,
            "outbound_date": c.ida.isoformat(),
            "type": "1" if c.ida_volta else "2",
            "currency": c.moeda,
            "hl": "pt-br",
            "gl": "br",
            "adults": c.adultos,
            "travel_class": CABINES.get(c.cabine, 1),
            "api_key": self.chave,
        }
        if c.volta:
            params["return_date"] = c.volta.isoformat()
        r = requests.get(URL, params=params, timeout=90)
        dados = r.json()
        if r.status_code != 200 or dados.get("error"):
            erro = dados.get("error", r.text[:200])
            if "hasn't returned any results" in str(erro):
                return []  # sem voos para essa data: não é falha
            raise RuntimeError(f"SerpApi: {erro}")

        return self.interpretar(dados, c)

    def interpretar(self, dados: dict, c: Consulta) -> list[Oferta]:
        link = (dados.get("search_metadata") or {}).get("google_flights_url", "")
        nivel = _nivel_preco(dados.get("price_insights") or {})
        ofertas: list[Oferta] = []
        for voo in (dados.get("best_flights") or []) + (dados.get("other_flights") or []):
            preco = voo.get("price")
            if not isinstance(preco, (int, float)):
                continue
            trechos = voo.get("flights") or []
            cias = sorted({t.get("airline", "") for t in trechos if t.get("airline")})
            dur = voo.get("total_duration")
            detalhes = f"{dur // 60}h{dur % 60:02d} de viagem na ida" if isinstance(dur, int) else ""
            avisos = []
            if c.ida_volta:
                # os voos da volta exigiriam uma busca extra por data (gasta cota)
                avisos.append("Google: horário da volta só no link (ver detalhes gastaria +1 busca)")
            ofertas.append(Oferta(
                consulta=c, fonte=self.nome, preco=float(preco), moeda=c.moeda,
                companhia=", ".join(cias), detalhes=detalhes, link=link,
                ida=_trecho(trechos, voo.get("layovers") or []), avisos=avisos, nivel_preco=nivel,
            ))
        return ofertas


def _trecho(voos: list[dict], escalas: list[dict]) -> Trecho | None:
    """Formato SerpApi: horários "2027-03-10 02:25" (locais); escalas com aeroporto e minutos."""
    try:
        partida = datetime.strptime(voos[0]["departure_airport"]["time"], "%Y-%m-%d %H:%M")
        chegada = datetime.strptime(voos[-1]["arrival_airport"]["time"], "%Y-%m-%d %H:%M")
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    return Trecho(
        partida=partida, chegada=chegada,
        voos=[v["flight_number"] for v in voos if v.get("flight_number")],
        conexoes=[(e.get("id", "?"), int(e.get("duration", 0))) for e in escalas],
    )


NIVEIS = {"low": "baixo", "typical": "normal", "high": "alto"}


def _nivel_preco(insights: dict) -> str:
    """Ex.: 'baixo (típico R$ 940–1.700)'. Vazio se o Google não informar."""
    nivel = NIVEIS.get(str(insights.get("price_level", "")).lower(), "")
    faixa = insights.get("typical_price_range")
    if isinstance(faixa, list) and len(faixa) == 2:
        txt = f"típico {brl(faixa[0])[:-3]}–{brl(faixa[1])[3:-3]}"
        return f"{nivel} ({txt})" if nivel else txt
    return nivel
