"""Calendário de voos em HTML: usado pelo painel (Streamlit) e pela página com senha (monitor.pagina)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from .aeroportos import todos as aeroportos
from .models import DIAS
from .regras import brl
from .roteiro import Cenario, conflito


MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
         "novembro", "dezembro"]
CORES_CIDADES = ["#dbeafe", "#fde68a", "#ddd6fe", "#fbcfe8", "#bbf7d0", "#fed7aa", "#cffafe", "#e5e7eb"]


def cidade_curta(codigo: str) -> str:
    nome = aeroportos().get(codigo, codigo)
    return nome.split(" – ")[0].split(" · ")[0]


def _quando(dt: datetime) -> str:
    return f"{DIAS[dt.weekday()]} {dt:%d/%m %H:%M}"


def _mais_baratos_do_dia(chips: list) -> dict:
    """Para cada voo do dia: motivo de ser o mais barato entre os do mesmo destino ou da mesma origem."""
    from .aeroportos import chave_cidade
    motivos: dict = defaultdict(list)
    for campo, texto in (("destino", "para {}"), ("origem", "saindo de {}")):
        grupos: dict = defaultdict(list)
        for o in chips:
            grupos[chave_cidade(getattr(o, campo))].append(o)
        for lista in grupos.values():
            if len(lista) >= 2:  # só faz sentido comparar quando há mais de uma rota
                m = min(lista, key=lambda o: o.preco)
                motivos[id(m)].append(texto.format(cidade_curta(getattr(m, campo))))
    return motivos


def calendario_html(ano: int, mes: int, op: dict, sel: list[str], cen: Cenario | None, nomes: dict,
                    voos: dict | None = None, rotas_lig: dict | None = None, clicavel: bool = False,
                    problemas: dict | None = None, noites: dict | None = None,
                    adultos: dict | None = None) -> str:
    import calendar
    import html

    # onde a pessoa está em cada dia (só quando há um roteiro escolhido)
    local: dict[date, str] = {}
    no_roteiro: set = set()
    cores: dict[str, str] = {}
    if cen:
        t = cen.trechos
        no_roteiro = {(o.rota_id, o.data) for o in t}
        for dest, de, ate in cen.estadias():
            d = de.date()
            while d <= ate.date():
                local[d] = cidade_curta(dest)
                d += timedelta(days=1)
        local.setdefault(t[0].partida.date(), cidade_curta(t[0].origem))
        local.setdefault(t[-1].chegada.date(), cidade_curta(t[-1].destino))
        cidades = list(dict.fromkeys(local.values()))
        for o in t:  # dias de voo: "Tampa → Minneapolis"; dentro de um voo longo: "em trânsito"
            d = o.partida.date()
            while d <= o.chegada.date():
                if d == o.partida.date():
                    local[d] = f"{cidade_curta(o.origem)} → {cidade_curta(o.destino)}"
                elif d < o.chegada.date():
                    local[d] = "✈️ em trânsito"
                elif d not in local or local[d] == "✈️ em trânsito":
                    local[d] = cidade_curta(o.destino)
                d += timedelta(days=1)
        for c in cidades:
            cores[c] = CORES_CIDADES[len(cores) % len(CORES_CIDADES)]
        for nome in list(local.values()):
            if "→" in nome:
                cores.setdefault(nome, cores.get(nome.split(" → ")[1], "#e0e7ff"))
        cores["✈️ em trânsito"] = "#e0e7ff"
    voo_do_roteiro = {(o.rota_id, o.data): o for o in (cen.trechos if cen else [])}

    linhas = ['<table class="cal"><tr>' + "".join(f"<th>{d}</th>" for d in DIAS) + "</tr>"]
    for semana in calendar.Calendar(firstweekday=0).monthdatescalendar(ano, mes):
        linhas.append("<tr>")
        for dia in semana:
            fora = dia.month != mes
            fundo = cores.get(local.get(dia, ""), "")
            estilo = f' style="background:{fundo};color:#111827"' if fundo and not fora else ""
            cel = [f'<div class="num">{dia.day}</div>']
            if not fora and dia in local:
                marca = "" if local[dia].startswith("✈️") else "📍 "
                cel.append(f'<div class="loc">{marca}{html.escape(local[dia])}</div>')
            chips = [] if fora else [o for r in sel if (o := op.get(r, {}).get(dia))]
            baratos = _mais_baratos_do_dia(chips)
            for o in sorted(chips, key=lambda o: o.preco):
                r = o.rota_id
                no_rot = (r, dia) in no_roteiro
                if no_rot:  # o voo do roteiro pode não ser o mais barato do dia: mostra o escolhido
                    o = voo_do_roteiro[(r, dia)]
                classes = "chip" + (" barato" if id(o) in baratos or baratos.get(id(op[r][dia])) else "")
                classes += " voo" if no_rot else ""
                titulo = f"{nomes.get(r, r)} · {o.fonte}" + (f" · {o.vendedor}" if o.vendedor else "")
                motivo = baratos.get(id(op[r][dia]))
                if motivo:
                    titulo += " · mais barato do dia " + " e ".join(motivo)
                lig = (rotas_lig or {}).get(r)
                aviso = conflito({a: (voos or {}).get(a, {}) for a in lig.depois_de}, o, lig.intervalo_min_horas,
                                 lig.intervalo_max_horas, nomes) if lig and lig.depois_de and not no_rot else ""
                icone = "✈️ " if no_rot else ("★ " if motivo else "")
                if aviso:
                    classes += " conflito"
                    icone = "⚠️ " + icone
                    titulo += " · ⚠️ o mais barato " + aviso
                hora = ""
                if o.partida and o.chegada:
                    dias = (o.chegada.date() - o.partida.date()).days
                    hora = (f'<span class="hora">sai {o.partida:%H:%M} · chega {o.chegada:%H:%M}'
                            + (f" ({o.chegada:%d/%m})" if dias else "") + "</span>")
                problema = (problemas or {}).get((r, dia))
                if problema:  # voo marcado: ❌ choque de horário / ⚠️ fora das ligações
                    tipo, motivo = problema
                    classes += " problema" if tipo == "sobrepoe" else " foraregra"
                    icone = "❌ " if tipo == "sobrepoe" else "⚠️ "
                    titulo = html.escape(motivo) + " · " + titulo
                marca_id = ""
                if clicavel:  # dados para a página com senha somar o total (por pessoa) e achar choques no navegador
                    marca_id = (f' data-id="{html.escape(r)}|{dia.isoformat()}" data-preco="{o.preco / max((adultos or {}).get(r, 1), 1):.2f}"'
                                + (f' data-partida="{o.partida:%Y-%m-%dT%H:%M}" data-chegada="{o.chegada:%Y-%m-%dT%H:%M}"'
                                   if o.partida and o.chegada else ""))
                cel.append(f'<div class="{classes}"{marca_id} title="{html.escape(titulo)}">{icone}{o.origem}→{o.destino} '
                           f'<b>{html.escape(brl(o.preco))}</b>{hora}</div>')
            if not fora and noites and dia in noites:
                cel.append(_faixa_noite(dia, noites[dia], clicavel))
            linhas.append(f'<td class="{"fora" if fora else ""}"{estilo}>{"".join(cel)}</td>')
        linhas.append("</tr>")
    linhas.append("</table>")
    css = """<style>
    table.cal{width:100%;border-collapse:collapse;table-layout:fixed;font-size:12px;background:#ffffff;color:#111827}
    table.cal th{padding:6px;text-align:left;color:#111827;background:#f3f4f6;border-bottom:2px solid #d1d5db}
    table.cal td{vertical-align:top;height:92px;padding:4px;border:1px solid #d1d5db;color:#111827;background:#ffffff}
    table.cal td.fora{background:#f3f4f6;color:#9ca3af}
    .cal .num{font-weight:600;margin-bottom:2px}
    .cal .loc{font-size:11px;font-weight:600;margin-bottom:2px}
    .cal .hora{font-size:10.5px;opacity:.85;display:block}
    .cal .chip{color:#111827;background:#f3f4f6;border-radius:6px;padding:2px 4px;margin:2px 0;line-height:1.25;overflow-wrap:anywhere}
    .cal .chip.barato{background:#dcfce7;border:1px solid #16a34a}
    .cal .chip.voo{background:#1d4ed8;color:#fff;border:none}
    .cal .chip.conflito{border:1px dashed #dc2626}
    .cal .chip.problema{background:#dc2626 !important;color:#fff !important;border:none !important}
    .cal .chip.foraregra{background:#f59e0b !important;color:#111827 !important;border:none !important}
    .cal .noite{margin-top:5px;border-top:1px dashed #6b7280;padding-top:3px;font-size:11px;line-height:1.3}
    .cal .noite .nt{display:block;opacity:.85}
    .cal .noite.paga .nt b{color:#9a3412}
    .cal .noite select{font-size:11px;max-width:100%;padding:1px 2px;border:1px solid #9ca3af;border-radius:4px;background:#fff;color:#111827}
    .cal .noite a{color:#1d4ed8}
    </style>"""
    return css + "".join(linhas)


def _faixa_noite(dia: date, n: dict, editavel: bool) -> str:
    """🌙 onde você dorme na noite que começa neste dia (escolha + preço da reserva)."""
    import html
    if n["tipo"] == "aviao" or not editavel:
        rotulo = html.escape(n["rotulo"])
    else:
        opts = "".join(f'<option value="{html.escape(v)}"{" selected" if v == n["tipo"] else ""}>{html.escape(r)}</option>'
                       for v, r in n["opcoes"])
        rotulo = f'<select data-noite="{dia.isoformat()}">{opts}</select>'
    classe = "noite paga" if n["tipo"].startswith("g:") else "noite"
    return f'<div class="{classe}">🌙 {rotulo}<span class="nt">{n.get("texto", "")}</span></div>'


def noites_do_calendario(ns: list, cs: list, cfg) -> dict:
    """{dia: {tipo, rotulo, opcoes, texto}} para desenhar as noites do roteiro no calendário."""
    import html
    opcoes = cfg.opcoes()
    nomes = dict(opcoes) | {"aviao": "✈️ No avião"}
    por_dia: dict = {}
    for c in cs:
        r = c.reserva
        d = r.entrada
        while d < r.saida:
            por_dia[d] = c
            d += timedelta(days=1)
    saida = {}
    for n in ns:
        texto = ""
        c = por_dia.get(n.data)
        if c:
            if n.data == c.reserva.entrada:
                periodo = f"{c.reserva.noites} noite(s), {c.reserva.entrada:%d/%m}→{c.reserva.saida:%d/%m}"
                if c.total is not None:
                    link = f' · <a href="{html.escape(c.link)}" target="_blank" rel="noopener">abrir</a>' if c.link else ""
                    texto = f"<b>{html.escape(brl(c.total))}</b> · {periodo} · {html.escape(c.nome[:28])}{link}"
                    if c.segunda:
                        t2, n2, l2 = c.segunda
                        a2 = f' · <a href="{html.escape(l2)}" target="_blank" rel="noopener">abrir</a>' if l2 else ""
                        texto += f"<br>2ª: {html.escape(brl(t2))} · {html.escape(n2[:28])}{a2}"
                elif c.situacao == "indisponivel":
                    texto = f"⚠️ nenhum dos seus links serve ({periodo})"
                else:
                    texto = f"⏳ preço ainda não buscado ({periodo})"
            else:
                texto = "↳ mesma reserva"
        elif n.tipo != "aviao" and n.tipo != n.sugerido:
            texto = "(você mudou)"
        saida[n.data] = {"tipo": n.tipo, "rotulo": nomes.get(n.tipo, n.tipo), "opcoes": opcoes, "texto": texto}
    return saida
