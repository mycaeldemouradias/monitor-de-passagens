"""Hospedagem: onde você dorme em cada noite do roteiro e quanto custa (Airbnb e Booking).

config/hospedagens.yaml (pessoal → vai criptografado para o GitHub):
  hospedes: 2
  casa: [GIG, SDU, MSP]            # casa de família/amigos: R$ 0
  grupos:
    - id: lisboa
      nome: Lisboa/Almada
      aeroportos: [LIS, OPO]       # chegando por estes, a sugestão é dormir aqui
      links: [https://www.airbnb.com.br/rooms/123, https://www.booking.com/hotel/fr/nome.pt-br.html, ...]
Aeroporto que não está em nenhum grupo nem em `casa` → sem hospedagem (fica no aeroporto, passeia e volta).

Cada noite tem um tipo: "g:<grupo>" (paga), "casa", "sem" ou "aviao" (dentro de um voo).
A sugestão vem da cidade onde você chegou; no painel você troca noite por noite.
Noites seguidas no mesmo grupo = uma reserva só (entrada → saída), com o preço do link mais barato
disponível nessas datas. Preços em data/hospedagens.json:
  {"precos": {"<quarto>|<entrada>|<saída>|<hóspedes>": {...}}, "ultima_busca": "..."}

Uso:
  python -m monitor.hospedagem [--a-cada HORAS] [--limite N]
    busca os preços que faltam (ou que têm mais de HORAS) para as noites dos N roteiros mais baratos
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

RAIZ = Path(__file__).resolve().parents[2]
ARQ_CONFIG = RAIZ / "config" / "hospedagens.yaml"
ARQ_PRECOS = RAIZ / "data" / "hospedagens.json"
PRIVADO = os.getenv("MONITOR_PRIVADO") == "1"


@dataclass
class Grupo:
    id: str
    nome: str
    aeroportos: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


@dataclass
class ConfigHosp:
    hospedes: int = 2
    casa: list[str] = field(default_factory=list)
    grupos: list[Grupo] = field(default_factory=list)
    # reservas dos roteiros que você guardou no painel: o GitHub busca o preço delas também (além das sugeridas)
    acompanhar: list[Reserva] = field(default_factory=list)

    def grupo(self, gid: str) -> Grupo | None:
        return next((g for g in self.grupos if g.id == gid), None)

    def opcoes(self) -> list[tuple[str, str]]:
        """(tipo, rótulo) das escolhas possíveis para uma noite."""
        return ([(f"g:{g.id}", f"🏠 {g.nome}") for g in self.grupos]
                + [("casa", "👪 Casa de família/amigos"), ("sem", "🧳 Sem hospedagem")])


def ler_config(arq: Path = ARQ_CONFIG) -> ConfigHosp | None:
    if not arq.exists():
        return None
    bruto = yaml.safe_load(arq.read_text(encoding="utf-8")) or {}
    grupos = [Grupo(str(g["id"]), str(g.get("nome") or g["id"]), [str(a).upper() for a in g.get("aeroportos") or []],
                    [str(l).strip() for l in g.get("links") or [] if str(l).strip()]) for g in bruto.get("grupos") or []]
    acomp = [Reserva(str(a[0]), date.fromisoformat(str(a[1])), date.fromisoformat(str(a[2])))
             for a in bruto.get("acompanhar") or [] if len(a) == 3]
    return ConfigHosp(int(bruto.get("hospedes") or 2), [str(a).upper() for a in bruto.get("casa") or []], grupos, acomp)


def gravar_config(cfg: ConfigHosp, arq: Path = ARQ_CONFIG) -> None:
    dados = {"hospedes": cfg.hospedes, "casa": cfg.casa,
             "grupos": [{"id": g.id, "nome": g.nome, "aeroportos": g.aeroportos, "links": g.links} for g in cfg.grupos],
             "acompanhar": [[r.grupo, r.entrada.isoformat(), r.saida.isoformat()] for r in cfg.acompanhar]}
    cab = "# Hospedagens (editar pelo painel: 🏠 Hospedagens). Pessoal: vai criptografado para o GitHub.\n"
    arq.write_text(cab + yaml.safe_dump(dados, allow_unicode=True, sort_keys=False), encoding="utf-8")


def quarto(link: str) -> str | None:
    """Código do anúncio: Airbnb '…/rooms/123?…' → '123'; Booking '…/hotel/fr/nome.pt-br.html?…' → 'booking:fr/nome'."""
    m = re.search(r"airbnb\.[a-z.]+/rooms/(\d+)", link)
    if m:
        return m.group(1)
    m = re.search(r"booking\.com/hotel/([a-z]{2})/([\w-]+?)(?:\.[a-z]{2}(?:-[a-z]{2})?)?\.html", link)
    return f"booking:{m.group(1)}/{m.group(2)}" if m else None


def link_limpo(q: str) -> str:
    """O link do anúncio sem códigos de rastreio."""
    if q.startswith("booking:"):
        return f"https://www.booking.com/hotel/{q[8:]}.pt-br.html"
    return f"https://www.airbnb.com.br/rooms/{q}"


def link_com_datas(q: str, entrada: date | str, saida: date | str, hospedes: int) -> str:
    e, s = str(entrada), str(saida)
    if q.startswith("booking:"):
        return (f"https://www.booking.com/hotel/{q[8:]}.pt-br.html?checkin={e}&checkout={s}&group_adults={hospedes}"
                f"&group_children=0&no_rooms=1&selected_currency=BRL")
    return f"https://www.airbnb.com.br/rooms/{q}?adults={hospedes}&check_in={e}&check_out={s}&currency=BRL"


def ler_precos(arq: Path = ARQ_PRECOS) -> dict[str, Any]:
    try:
        d = json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    return {"precos": d.get("precos") or {}, "ultima_busca": d.get("ultima_busca")}


def gravar_precos(d: dict[str, Any], arq: Path = ARQ_PRECOS) -> None:
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps(d, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")


def chave(q: str, entrada: date, saida: date, hospedes: int) -> str:
    return f"{q}|{entrada.isoformat()}|{saida.isoformat()}|{hospedes}"


# ------------------------------------------------------------------ noites do roteiro
@dataclass
class Noite:
    data: date          # a noite que começa neste dia
    aeroporto: str      # onde você chegou (para a sugestão)
    tipo: str           # "g:<grupo>", "casa", "sem" ou "aviao"
    sugerido: str       # o tipo que o programa sugere


def sugestao(aeroporto: str, cfg: ConfigHosp) -> str:
    for g in cfg.grupos:
        if aeroporto in g.aeroportos:
            return f"g:{g.id}"
    return "casa" if aeroporto in cfg.casa else "sem"


def noites(trechos: list, cfg: ConfigHosp, escolhas: dict[str, str] | None = None) -> list[Noite]:
    """As noites entre o primeiro voo e o último (voos em ordem, com partida e chegada)."""
    escolhas = escolhas or {}
    validos = {t for t, _ in cfg.opcoes()}
    saida: dict[date, Noite] = {}
    for a, prox in zip(trechos, list(trechos[1:]) + [None]):
        d = a.partida.date()
        while d < a.chegada.date():  # noite dentro do avião
            saida.setdefault(d, Noite(d, a.destino, "aviao", "aviao"))
            d += timedelta(days=1)
        if prox is None:
            break
        d = a.chegada.date()
        while d < prox.partida.date():
            sug = sugestao(a.destino, cfg)
            esc = escolhas.get(d.isoformat())
            saida.setdefault(d, Noite(d, a.destino, esc if esc in validos else sug, sug))
            d += timedelta(days=1)
    return [saida[d] for d in sorted(saida)]


@dataclass
class Reserva:
    grupo: str
    entrada: date
    saida: date

    @property
    def noites(self) -> int:
        return (self.saida - self.entrada).days


def reservas(lista: list[Noite]) -> list[Reserva]:
    """Noites seguidas no mesmo grupo pago = uma reserva."""
    saida: list[Reserva] = []
    for n in lista:
        if not n.tipo.startswith("g:"):
            continue
        g = n.tipo[2:]
        if saida and saida[-1].grupo == g and saida[-1].saida == n.data:
            saida[-1].saida = n.data + timedelta(days=1)
        else:
            saida.append(Reserva(g, n.data, n.data + timedelta(days=1)))
    return saida


@dataclass
class Custo:
    reserva: Reserva
    total: float | None     # o link mais barato disponível (None = sem preço)
    nome: str = ""
    link: str = ""
    situacao: str = "ok"    # ok | sem_preco (ainda não buscado) | indisponivel (nenhum link serve)
    segunda: tuple[float, str, str] | None = None   # 2ª mais barata (total, nome, link), só para mostrar


def custo(r: Reserva, cfg: ConfigHosp, precos: dict[str, Any]) -> Custo:
    g = cfg.grupo(r.grupo)
    if not g or not g.links:
        return Custo(r, None, situacao="sem_preco")
    achados, faltam = [], 0
    for link in g.links:
        q = quarto(link)
        p = precos.get(chave(q, r.entrada, r.saida, cfg.hospedes)) if q else None
        if p is None:
            faltam += 1
        elif p.get("total") is not None:
            achados.append((p["total"], p.get("nome") or "", link))
    if achados:
        achados.sort()
        t, nome, link = achados[0]
        segunda = None
        if len(achados) > 1:
            t2, nome2, link2 = achados[1]
            segunda = (t2, nome2, _link_datas(link2, r, cfg.hospedes))
        return Custo(r, t, nome, _link_datas(link, r, cfg.hospedes), segunda=segunda)
    return Custo(r, None, situacao="sem_preco" if faltam else "indisponivel")


def _link_datas(link: str, r: Reserva, hospedes: int) -> str:
    q = quarto(link)
    return link_com_datas(q, r.entrada.isoformat(), r.saida.isoformat(), hospedes) if q else link


def custos(lista: list[Noite], cfg: ConfigHosp, precos: dict[str, Any]) -> list[Custo]:
    return [custo(r, cfg, precos) for r in reservas(lista)]


def total_hospedagem(cs: list[Custo]) -> tuple[float, int]:
    """(soma das reservas com preço, quantas reservas ainda sem preço)."""
    return sum(c.total for c in cs if c.total is not None), sum(1 for c in cs if c.total is None)


# ------------------------------------------------------------------ busca no Airbnb
_PRECO = re.compile(r"novo preço total é R\$\s?([\d.]+(?:,\d{1,2})?)|Total:\s*R\$\s?([\d.]+(?:,\d{1,2})?)")
_NAO_SERVE = ("Estadia mínima", "Estadia máxima", "não estão disponíveis", "não está disponível",
              "Adicione datas para ver os preços")


def _numero(txt: str) -> float:
    return float(txt.replace(".", "").replace(",", "."))


def ler_pagina(texto: str) -> tuple[float | None, str]:
    """(total, motivo) a partir do texto da página do quarto. total None = não serve nessas datas."""
    t = texto.replace("\xa0", " ")
    m = _PRECO.search(t)
    if m:
        return _numero(m.group(1) or m.group(2)), ""
    for frase in _NAO_SERVE:
        if frase in t:
            linha = next((l for l in t.splitlines() if frase in l), frase)
            return None, linha.strip()[:80]
    return None, "?"


_BOOKING_QUARTOS = """() => [...document.querySelectorAll('table.hprt-table tbody tr')].map(tr => {
  const pr = tr.querySelector('.prco-valign-middle-helper, [data-testid="price-and-discounted-price"]');
  const tx = tr.querySelector('.prd-taxes-and-fees-under-price');
  const oc = tr.querySelector('.hprt-occupancy-occupancy-info, .c-occupancy-icons');
  return [pr ? pr.innerText : '', tx ? tx.innerText : '', oc ? (oc.getAttribute('aria-label') || oc.innerText) : ''];
})"""
_SEM_VAGA_BOOKING = ("Não temos disponibilidade", "não tem disponibilidade", "Esgotado", "esgotad",
                     "sem disponibilidade")


def ler_quartos_booking(linhas: list[list[str]], hospedes: int) -> float | None:
    """O quarto mais barato que cabe todo mundo: preço + impostos e taxas."""
    valores = []
    for preco, taxas, ocup in linhas:
        m = re.search(r"R\$\s?([\d.]+(?:,\d{1,2})?)", preco.replace("\xa0", " "))
        if not m:
            continue
        cabe = re.search(r"(\d+)", ocup or "")
        if cabe and int(cabe.group(1)) < hospedes:
            continue
        t = re.search(r"R\$\s?([\d.]+(?:,\d{1,2})?)", taxas.replace("\xa0", " "))
        valores.append(_numero(m.group(1)) + (_numero(t.group(1)) if t else 0))
    return min(valores) if valores else None


def preco_booking(pagina, q: str, entrada: date, saida: date, hospedes: int) -> dict[str, Any]:
    pagina.goto(link_com_datas(q, entrada, saida, hospedes), timeout=60_000)
    total, motivo = None, "?"
    for s in range(25):
        pagina.wait_for_timeout(1000)
        try:
            linhas = pagina.evaluate(_BOOKING_QUARTOS)
            texto = pagina.inner_text("body")
        except Exception:  # noqa: BLE001 — a página recarregou no meio da leitura: tenta de novo
            continue
        if linhas:
            total = ler_quartos_booking(linhas, hospedes)
            motivo = "" if total is not None else f"Nenhum quarto para {hospedes} pessoa(s)"
            break
        frase = next((f for f in _SEM_VAGA_BOOKING if f in texto), None)
        if frase and s >= 5:
            motivo = "Sem quartos nessas datas"
            break
    nome = (pagina.title() or "").split(",")[0].split(" (")[0][:60]
    return {"total": total, "motivo": motivo, "nome": nome,
            "coleta": datetime.now(timezone.utc).isoformat(timespec="minutes")}


def preco_airbnb(pagina, q: str, entrada: date, saida: date, hospedes: int) -> dict[str, Any]:
    pagina.goto(link_com_datas(q, entrada, saida, hospedes), timeout=60_000)
    total, motivo = None, "?"
    for s in range(25):  # até ~25 s: o preço chega depois da página
        pagina.wait_for_timeout(1000)
        try:
            total, motivo = ler_pagina(pagina.inner_text("body"))
        except Exception:  # noqa: BLE001 — a página recarregou no meio da leitura
            continue
        # "Adicione datas…" aparece por um instante enquanto carrega: só vale se continuar lá
        if total is not None or (motivo != "?" and (not motivo.startswith("Adicione") or s >= 10)):
            break
    if total is None and motivo.startswith("Adicione") and not any(f in motivo for f in _NAO_SERVE[:4]):
        motivo = "Datas não aceitas pelo anúncio"
    nome = (pagina.title() or "").split(" - ")[0][:60]
    return {"total": total, "motivo": motivo, "nome": nome,
            "coleta": datetime.now(timezone.utc).isoformat(timespec="minutes")}


def buscar(pedidos: list[tuple[str, date, date]], hospedes: int, precos: dict[str, Any],
           aviso=print) -> tuple[int, int]:
    """Busca cada (quarto, entrada, saída) e guarda em `precos`. Devolve (com preço, sem resposta)."""
    if not pedidos:
        return 0, 0
    from playwright.sync_api import sync_playwright
    ok = falhas = 0
    with sync_playwright() as p:
        nav = p.chromium.launch()
        ctx = nav.new_context(locale="pt-BR", user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0 Safari/537.36"))
        pagina = ctx.new_page()
        for i, (q, e, s) in enumerate(pedidos, 1):
            try:
                r = (preco_booking if q.startswith("booking:") else preco_airbnb)(pagina, q, e, s, hospedes)
            except Exception as erro:  # noqa: BLE001 — um quarto com problema não para os outros
                falhas += 1
                aviso(f"  {i}/{len(pedidos)}: erro ({type(erro).__name__})")
                continue
            if r["motivo"] == "?":  # página não respondeu (bloqueio?): não guarda, tenta na próxima
                falhas += 1
                aviso(f"  {i}/{len(pedidos)}: sem resposta")
                continue
            precos[chave(q, e, s, hospedes)] = r
            ok += r["total"] is not None
            if not PRIVADO:
                aviso(f"  {i}/{len(pedidos)}: {q} {e:%d/%m}→{s:%d/%m}: "
                      + (f"R$ {r['total']:.2f}" if r["total"] is not None else r["motivo"]))
        nav.close()
    return ok, falhas


def pedidos_faltando(res: list[Reserva], cfg: ConfigHosp, precos: dict[str, Any], a_cada_h: float,
                     agora: datetime | None = None) -> list[tuple[str, date, date]]:
    agora = agora or datetime.now(timezone.utc)
    saida, vistos = [], set()
    for r in res:
        g = cfg.grupo(r.grupo)
        for link in (g.links if g else []):
            q = quarto(link)
            if not q or (q, r.entrada, r.saida) in vistos:
                continue
            vistos.add((q, r.entrada, r.saida))
            p = precos.get(chave(q, r.entrada, r.saida, cfg.hospedes))
            velho = p is None or agora - datetime.fromisoformat(p["coleta"]) > timedelta(hours=a_cada_h)
            if velho:
                saida.append((q, r.entrada, r.saida))
    return saida


def main(argv: list[str] | None = None) -> int:
    import argparse
    from .pagina import _dados
    from .config import CONFIG_PADRAO
    from .roteiro import roteiros_pelas_ligacoes

    ap = argparse.ArgumentParser(prog="python -m monitor.hospedagem")
    ap.add_argument("--a-cada", type=float, default=12, help="buscar de novo preços com mais de N horas")
    ap.add_argument("--limite", type=int, default=30, help="quantos roteiros mais baratos considerar")
    ap.add_argument("--precisa", action="store_true", help="só responde (código 0 = sim) se está na hora de buscar")
    a = ap.parse_args(argv)
    cfg = ler_config()
    if not cfg or not cfg.grupos:
        print("Sem hospedagens cadastradas: nada a buscar.")
        return 1 if a.precisa else 0
    agora = datetime.now(timezone.utc)
    dados = ler_precos()
    if dados["ultima_busca"] and agora - datetime.fromisoformat(dados["ultima_busca"]) < timedelta(hours=a.a_cada):
        print(f"Última busca de hospedagem há menos de {a.a_cada:g}h: pulando.")
        return 1 if a.precisa else 0
    if a.precisa:
        print("Está na hora de buscar hospedagem.")
        return 0
    _l, _n, _op, voos, rotas, _u = _dados(CONFIG_PADRAO, RAIZ / "data", agora)
    cs, _ = roteiros_pelas_ligacoes(rotas, voos, limite=a.limite)
    res = [r for c in cs for r in reservas(noites(c.trechos, cfg))]
    res += [r for r in cfg.acompanhar if r.entrada >= agora.date()]  # as dos roteiros que você guardou
    pedidos = pedidos_faltando(res, cfg, dados["precos"], a.a_cada, agora)
    print(f"Hospedagem: {len(pedidos)} consulta(s) para {len(cs)} roteiro(s).")
    ok, falhas = buscar(pedidos, cfg.hospedes, dados["precos"])
    # tira preços de datas que já passaram
    hoje = agora.date().isoformat()
    dados["precos"] = {k: v for k, v in dados["precos"].items() if k.split("|")[1] >= hoje}
    if pedidos and falhas == len(pedidos):
        print("Nenhuma resposta do Airbnb (bloqueio?): tenta de novo na próxima rodada.")
    else:
        dados["ultima_busca"] = agora.isoformat(timespec="minutes")
    gravar_precos(dados)
    print(f"Com preço: {ok} · sem resposta: {falhas}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ------------------------------------------------------------------ roteiros: voos + hospedagem
@dataclass
class RoteiroTotal:
    cenario: Any
    noites: list[Noite]
    custos: list[Custo]
    hospedagem: float        # das reservas, para todos os hóspedes
    sem_preco: int
    pessoas: int = 1         # quantos viajam (= hóspedes)
    voos_pp: float = 0.0     # passagens de 1 pessoa

    @property
    def hosp_pp(self) -> float:
        return self.hospedagem / self.pessoas

    @property
    def total(self) -> float:
        """Por pessoa (é o que ordena os roteiros)."""
        return self.voos_pp + self.hosp_pp

    @property
    def total_grupo(self) -> float:
        return self.total * self.pessoas


def voos_por_pessoa(trechos: list, adultos: dict[str, int] | None = None) -> float:
    """Soma das passagens de 1 pessoa (a busca de uma viagem com 2 adultos traz o preço dos 2)."""
    adultos = adultos or {}
    return sum(o.preco / max(int(adultos.get(o.rota_id, 1) or 1), 1) for o in trechos)


def com_hospedagem(cenarios: list, cfg: ConfigHosp | None, precos: dict[str, Any],
                   escolhas: dict[str, str] | None = None, adultos: dict[str, int] | None = None) -> list[RoteiroTotal]:
    """Os roteiros com a hospedagem sugerida, do mais barato ao mais caro POR PESSOA
    (passagens de 1 pessoa + a hospedagem dividida pelos hóspedes)."""
    saida = []
    for c in cenarios:
        ns = noites(c.trechos, cfg, escolhas) if cfg else []
        cs = custos(ns, cfg, precos) if cfg else []
        h, falta = total_hospedagem(cs)
        saida.append(RoteiroTotal(c, ns, cs, h, falta, cfg.hospedes if cfg else 1, voos_por_pessoa(c.trechos, adultos)))
    saida.sort(key=lambda r: (r.total, r.cenario.trechos[0].partida))
    return saida


def juntar(local: dict[str, Any], novo: dict[str, Any]) -> dict[str, Any]:
    """Junta dois arquivos de preços, ficando com a coleta mais recente de cada consulta."""
    precos = dict(novo.get("precos") or {})
    for k, v in (local.get("precos") or {}).items():
        if k not in precos or str(v.get("coleta", "")) > str(precos[k].get("coleta", "")):
            precos[k] = v
    ultimas = [x for x in (local.get("ultima_busca"), novo.get("ultima_busca")) if x]
    return {"precos": precos, "ultima_busca": max(ultimas) if ultimas else None}
