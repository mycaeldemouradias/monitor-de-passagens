"""Fonte Skiplagged.

No GitHub usa o MCP oficial do Skiplagged. No Mac mantém compatibilidade com o endpoint
JSON antigo, não documentado e sujeito a mudanças ou bloqueios.

Formato legado observado em 20/11/2026:
- `itineraries.outbound`: voos de ida, com `one_way_price` (só ida) e, na busca de ida e
  volta, `min_round_trip_price` (total da viagem).
- `itineraries.inbound`: voos de volta, só com o preço avulso do trecho. Não serve como
  preço da viagem.
- Na maioria dos casos, a ida e volta é a soma de 2 bilhetes só de ida (ida + volta mais
  barata). Às vezes é um bilhete único de ida e volta, e aí a resposta não diz qual é a volta.
- `flights[id].segments`: cia, nº do voo, aeroportos e horários de cada trecho.
- `data` de cada itinerário: os vendedores (a "sala intermediária" do site) e o preço em cada um.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from ..models import Consulta, Oferta, Trecho
from .base import UA, Fonte, FonteBloqueada, cotacao_para_brl

URL = "https://skiplagged.com/api/search.php"
MCP_URL = "https://mcp.skiplagged.com/mcp"
#: quantas opções (as mais baratas) viram ofertas por busca
MAX_OFERTAS = 8
#: além das mais baratas, a mais barata de cada faixa de horário de saída (para montar roteiros que
#: encaixem: se o voo mais barato do dia sai cedo demais, pode haver outro mais tarde)
FAIXA_HORAS = 3
#: quantas vezes pedir de novo quando a resposta vem parcial
TENTATIVAS_INCOMPLETO = 6  # ~30s: o site demora até achar todos os preços


def _json_sse(texto: str) -> dict[str, Any]:
    """Lê a mensagem JSON do transporte SSE usado pelo MCP remoto."""
    try:
        bruto = json.loads(texto)
    except ValueError:
        bruto = None
        for linha in texto.splitlines():
            if linha.startswith("data:"):
                try:
                    bruto = json.loads(linha[5:].strip())
                    break
                except ValueError:
                    continue
    if not isinstance(bruto, dict):
        raise ValueError("Resposta inválida do MCP do Skiplagged")
    return bruto


def _voos_id(identificador: str) -> list[str]:
    """Extrai códigos de voo quando o MCP os inclui no identificador do cartão."""
    achado = re.search(r"trip=([^&#]+)", identificador)
    if not achado:
        return []
    return [x for x in re.split(r"[,~]+", achado.group(1)) if x]


def _precos_em_centavos(dados: dict[str, Any], ida_volta: bool) -> list[tuple[int, str]]:
    """(preço_em_centavos, id_do_voo) de cada voo de ida, já com o preço certo para o tipo de busca."""
    itins = dados.get("itineraries") or {}
    ida = itins.get("outbound") if isinstance(itins, dict) else None
    campo = "min_round_trip_price" if ida_volta else "one_way_price"
    achados = []
    for it in ida if isinstance(ida, list) else []:
        v = it.get(campo) if isinstance(it, dict) else None
        if isinstance(v, (int, float)) and v > 0:
            achados.append((int(v), str(it.get("flight", ""))))
    return achados


def _trecho(voo: Any, destinos: set[str]) -> Trecho | None:
    """Monta o Trecho a partir de flights[id]. `destinos`: aeroportos que contam como chegada."""
    segs = voo.get("segments") if isinstance(voo, dict) else None
    if not isinstance(segs, list) or not segs:
        return None
    try:
        partidas = [datetime.fromisoformat(s["departure"]["time"]) for s in segs]
        chegadas = [datetime.fromisoformat(s["arrival"]["time"]) for s in segs]
        aeroportos = [s["arrival"]["airport"] for s in segs]
    except (KeyError, TypeError, ValueError):
        return None
    # hidden-city: o bilhete segue além do destino; desce-se no primeiro pouso no destino
    fim = len(segs) - 1
    desce_em = ""
    if aeroportos[-1] not in destinos:
        for i, a in enumerate(aeroportos):
            if a in destinos:
                fim, desce_em = i, a
                break
    conexoes = [(aeroportos[i], int((partidas[i + 1] - chegadas[i]).total_seconds() // 60))
                for i in range(fim)]
    voos = [f"{s.get('airline', '')} {s.get('flight_number', '')}".strip() for s in segs[: fim + 1]]
    return Trecho(partida=partidas[0], chegada=chegadas[fim], voos=voos, conexoes=conexoes,
                  desce_em=desce_em)


def _vendedores(item: Any, custo: int) -> dict[str, int]:
    """{vendedor: centavos} do bloco de `data` cujo custo é `custo` (a "sala intermediária")."""
    try:
        bruto = json.loads(str(item["data"]).split("|", 1)[1])
    except (KeyError, IndexError, TypeError, ValueError):
        return {}

    def procurar(no: Any) -> dict[str, int] | None:
        if isinstance(no, list):
            for x in no:
                if (achado := procurar(x)) is not None:
                    return achado
        elif isinstance(no, dict):
            fonte = no.get("source")
            if no.get("cost") == custo and isinstance(fonte, dict):
                return {k: int(v[1]) for k, v in fonte.items()
                        if isinstance(v, list) and len(v) == 2 and isinstance(v[1], (int, float))}
            return procurar(fonte)
        return None

    return procurar(bruto.get("source")) or {}


def _mais_baratos(vend: dict[str, int]) -> str:
    if not vend:
        return ""
    menor = min(vend.values())
    return ", ".join(sorted(k for k, v in vend.items() if v == menor))


class Skiplagged(Fonte):
    nome = "skiplagged"
    #: pausa aleatória entre buscas, para não parecer robô
    # O MCP começou a responder HTTP 429 quando várias partes consultaram juntas.
    # O workflow limita a concorrência e cada processo mantém uma pausa mais longa.
    pausa_s = (8.0, 12.0)

    def __init__(self, max_por_rodada: int | None = None):
        self.limite_por_execucao = max_por_rodada

    def buscar(self, c: Consulta) -> list[Oferta]:
        # A API antiga agora devolve HTTP 403 nos runners do GitHub (Cloudflare). O
        # MCP oficial é a interface suportada pelo próprio Skiplagged e retorna os
        # mesmos dados úteis sem exigir sessão ou chave.
        if os.getenv("GITHUB_ACTIONS") == "true":
            return self._buscar_mcp(c)

        params = {
            "from": c.origem, "to": c.destino, "depart": c.ida.isoformat(),
            "return": c.volta.isoformat() if c.volta else "", "format": "v3",
            "counts[adults]": c.adultos, "counts[children]": 0,
        }
        # a 1ª resposta pode vir parcial ("incomplete": true) enquanto o site ainda busca;
        # repete até vir completa (visto em 20/11/2026: 228 voos parciais → 300 completos)
        for tentativa in range(TENTATIVAS_INCOMPLETO):
            r = requests.get(URL, params=params, timeout=60,
                             headers={"User-Agent": UA, "Accept": "application/json",
                                      "Referer": "https://skiplagged.com/"})
            if r.status_code in (403, 429) or "captcha" in r.text[:2000].lower():
                if r.status_code == 403 or "captcha" in r.text[:2000].lower():
                    return self._buscar_mcp(c)
                raise FonteBloqueada(f"Skiplagged bloqueou a requisição (HTTP {r.status_code})")
            r.raise_for_status()
            dados = r.json()
            if not dados.get("incomplete"):
                break
            if tentativa < TENTATIVAS_INCOMPLETO - 1:
                time.sleep(5)
        ofertas = self.interpretar(dados, c, cotacao_para_brl)
        if dados.get("incomplete"):
            for o in ofertas:
                o.avisos.append("resultado parcial do Skiplagged (pode haver opção mais barata)")
        return ofertas

    def _buscar_mcp(self, c: Consulta) -> list[Oferta]:
        """Busca pelo servidor MCP oficial do Skiplagged (JSON-RPC sobre HTTP)."""
        argumentos: dict[str, Any] = {
            "origin": c.origem,
            "destination": c.destino,
            "departureDate": c.ida.isoformat(),
            "currency": "BRL",
            "adults": c.adultos,
            "fareClass": {"economica": "economy", "premium": "premium",
                          "executiva": "business", "primeira": "first"}.get(c.cabine, "economy"),
            "includeHiddenCity": True,
            "includeStandard": True,
            "includeVirtualInterlining": True,
            "limit": 100,
            "sort": "price",
        }
        if c.volta:
            argumentos["returnDate"] = c.volta.isoformat()
        pedido = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "sk_flights_search", "arguments": argumentos}}
        cabecalhos = {"Accept": "application/json, text/event-stream",
                      "MCP-Protocol-Version": "2025-03-26"}
        resposta = None
        for tentativa in range(4):
            try:
                resposta = requests.post(MCP_URL, json=pedido, timeout=90, headers=cabecalhos)
            except requests.RequestException:
                if tentativa == 3:
                    raise
                time.sleep((5 * (2 ** tentativa)) + random.uniform(0, 3))
                continue
            if resposta.status_code == 429:
                if tentativa == 3:
                    raise FonteBloqueada("MCP do Skiplagged manteve o limite (HTTP 429) após novas tentativas")
                cabecalho = resposta.headers.get("Retry-After")
                try:
                    espera = max(1.0, float(cabecalho)) if cabecalho else 0
                except ValueError:
                    try:
                        espera = max(1.0, (parsedate_to_datetime(cabecalho) - datetime.now(timezone.utc)).total_seconds())
                    except (TypeError, ValueError, OverflowError):
                        espera = 0
                if not espera:
                    espera = (30, 60, 120)[tentativa]
                time.sleep(espera * random.uniform(0.9, 1.1))
                continue
            if resposta.status_code == 403:
                raise FonteBloqueada("MCP do Skiplagged bloqueou a busca (HTTP 403)")
            if resposta.status_code >= 500 and tentativa < 3:
                time.sleep((10 * (2 ** tentativa)) + random.uniform(0, 5))
                continue
            break
        assert resposta is not None
        resposta.raise_for_status()
        pacote = _json_sse(resposta.text)
        if pacote.get("error"):
            raise RuntimeError("MCP do Skiplagged retornou erro na busca")
        resultado = pacote.get("result") or {}
        if resultado.get("isError"):
            mensagem = " ".join(str(x.get("text", "")) for x in resultado.get("content", [])
                                 if isinstance(x, dict))
            mensagem_normalizada = mensagem.lower()
            if any(x in mensagem_normalizada for x in ("limit", "wait", "rate", "too many", "429")):
                raise FonteBloqueada("MCP do Skiplagged atingiu o limite de buscas")
            raise RuntimeError("MCP do Skiplagged não conseguiu completar a busca")
        estruturado = resultado.get("structuredContent") or {}
        if not isinstance(estruturado, dict) or not isinstance(estruturado.get("flights"), list):
            raise RuntimeError("MCP do Skiplagged retornou uma resposta sem resultados estruturados")
        return self._interpretar_mcp(estruturado, c)

    def _interpretar_mcp(self, dados: dict[str, Any], c: Consulta) -> list[Oferta]:
        """Converte os cartões estruturados do MCP no modelo comum do monitor."""
        cambio = (dados.get("pricing") or {}).get("rate")
        ofertas: list[Oferta] = []
        for voo in dados.get("flights") or []:
            if not isinstance(voo, dict):
                continue
            preco = (voo.get("price") or {}).get("amount")
            partida, chegada = voo.get("departure") or {}, voo.get("arrival") or {}
            try:
                p = datetime.fromisoformat(partida["dateTime"])
                q = datetime.fromisoformat(chegada["dateTime"])
                valor = float(preco)
            except (KeyError, TypeError, ValueError):
                continue
            attrs = [str(a).lower() for a in voo.get("attributes") or []]
            qtd_escalas = voo.get("layovers", 0)
            qtd_escalas = max(0, int(qtd_escalas)) if isinstance(qtd_escalas, (int, float)) else 0
            ida = Trecho(partida=p, chegada=q, voos=_voos_id(str(voo.get("id", ""))),
                         escalas_sem_detalhe=qtd_escalas)
            volta = None
            retorno = voo.get("returnFlight")
            if isinstance(retorno, dict):
                try:
                    escalas_volta = retorno.get("layovers", 0)
                    escalas_volta = max(0, int(escalas_volta)) if isinstance(escalas_volta, (int, float)) else 0
                    volta = Trecho(
                        partida=datetime.fromisoformat(retorno["departure"]["dateTime"]),
                        chegada=datetime.fromisoformat(retorno["arrival"]["dateTime"]),
                        voos=[str(retorno.get("airlines", ""))] if retorno.get("airlines") else [],
                        escalas_sem_detalhe=escalas_volta,
                    )
                except (KeyError, TypeError, ValueError):
                    volta = None
            avisos = []
            if "hidden-city" in attrs:
                avisos.append("opção hidden-city; confira as condições antes de reservar")
            duracao = str(voo.get("duration") or "")
            escalas = qtd_escalas
            detalhes = f"Duração: {duracao}" if duracao else ""
            if isinstance(escalas, (int, float)) and escalas:
                detalhes = f"{detalhes} · {int(escalas)} escala(s)" if detalhes else f"{int(escalas)} escala(s)"
                avisos.append("o Skiplagged não detalha os aeroportos nem os tempos das escalas")
            if cambio:
                detalhes = f"{detalhes} · câmbio aproximado do Skiplagged" if detalhes else "câmbio aproximado do Skiplagged"
            ofertas.append(Oferta(
                consulta=c, fonte=self.nome, preco=round(valor, 2), moeda="BRL",
                companhia=str(voo.get("airlines") or ""), detalhes=detalhes,
                link=str(voo.get("deepLink") or dados.get("searchUrl") or ""),
                ida=ida, volta=volta, avisos=avisos,
            ))
        if not ofertas:
            return []
        escolhidos = sorted(ofertas, key=lambda o: o.preco)[:MAX_OFERTAS]
        faixas: dict[int, Oferta] = {}
        for o in sorted(ofertas, key=lambda x: x.preco):
            if o.ida:
                faixas.setdefault(o.ida.partida.hour // FAIXA_HORAS, o)
        for o in faixas.values():
            if o not in escolhidos:
                escolhidos.append(o)
        return escolhidos

    def interpretar(self, dados: dict[str, Any], c: Consulta, cotacao=cotacao_para_brl) -> list[Oferta]:
        achados = _precos_em_centavos(dados, c.ida_volta)
        if not achados:
            return []
        voos = dados.get("flights") if isinstance(dados.get("flights"), dict) else {}
        itins = dados.get("itineraries") or {}
        por_id = {str(i.get("flight")): i for i in itins.get("outbound") or [] if isinstance(i, dict)}
        destinos = set(((dados.get("info") or {}).get("to") or {}).get("airports") or []) | {c.destino}
        origens = set(((dados.get("info") or {}).get("from") or {}).get("airports") or []) | {c.origem}

        # volta mais barata (usada quando a ida e volta é a soma de 2 bilhetes)
        volta_item, volta_preco = None, None
        for i in itins.get("inbound") or []:
            p = i.get("one_way_price") if isinstance(i, dict) else None
            if isinstance(p, (int, float)) and (volta_preco is None or p < volta_preco):
                volta_item, volta_preco = i, int(p)

        cambio = cotacao("USD")
        link = (f"https://skiplagged.com/flights/{c.origem}/{c.destino}/{c.ida.isoformat()}"
                + (f"/{c.volta.isoformat()}" if c.volta else ""))
        ofertas = []
        escolhidos = sorted(achados)[:MAX_OFERTAS]
        faixas: dict[int, tuple[int, str]] = {}
        for centavos, voo_id in sorted(achados):
            t = _trecho(voos.get(voo_id), destinos)
            if t:
                faixas.setdefault(t.partida.hour // FAIXA_HORAS, (centavos, voo_id))
        escolhidos += [x for x in sorted(faixas.values()) if x not in escolhidos]
        for centavos, voo_id in escolhidos:
            item = por_id.get(voo_id, {})
            ida = _trecho(voos.get(voo_id), destinos)
            volta, avisos, vendedor = None, [], ""
            if not c.ida_volta:
                vendedor = _mais_baratos(_vendedores(item, centavos))
            else:
                so_ida = item.get("one_way_price")
                if volta_item is not None and isinstance(so_ida, (int, float)) \
                        and centavos == int(so_ida) + volta_preco:
                    volta = _trecho(voos.get(str(volta_item.get("flight"))), origens)
                    v_ida = _mais_baratos(_vendedores(item, int(so_ida)))
                    v_volta = _mais_baratos(_vendedores(volta_item, volta_preco))
                    vendedor = v_ida if v_ida == v_volta else f"ida: {v_ida} · volta: {v_volta}"
                    avisos.append("2 bilhetes separados (ida e volta compradas à parte): "
                                  "se a ida atrasar ou for cancelada, a volta não é protegida")
                else:
                    vendedor = _mais_baratos(_vendedores(item, centavos))
                    avisos.append("bilhete único de ida e volta: horário da volta só no site")
            for nome, t in (("ida", ida), ("volta", volta)):
                if t and t.desce_em:
                    avisos.append(f"cidade escondida na {nome}: desça em {t.desce_em} (o bilhete segue "
                                  "adiante); só bagagem de mão e não pule nenhum trecho")
            usd = centavos / 100
            nomes = dados.get("airlines") if isinstance(dados.get("airlines"), dict) else {}
            codigos = {v.split()[0] for t in (ida, volta) if t for v in t.voos if v}
            cias = sorted((nomes.get(k) or {}).get("name") or k for k in codigos)
            ofertas.append(Oferta(
                consulta=c, fonte=self.nome, preco=round(usd * cambio, 2), moeda="BRL",
                companhia=", ".join(cias), detalhes=f"US$ {usd:.2f} convertido a {cambio:.4f}",
                link=link, ida=ida, volta=volta, vendedor=vendedor, avisos=avisos,
            ))
        return ofertas
