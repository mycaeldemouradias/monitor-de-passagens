"""Painel do monitor de passagens (abre no navegador do Mac).

Abrir: dois cliques no aplicativo "Abrir painel" (chama painel/iniciar.sh), ou
  PYTHONPATH=src .venv/bin/python -m streamlit run painel/app.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from monitor.aeroportos import rotulo, todos as aeroportos  # noqa: E402
from monitor.config import CONFIG_PADRAO, Geral, carregar, expandir_datas, ler_pausas  # noqa: E402
from monitor.models import DIAS  # noqa: E402
from monitor.regras import brl  # noqa: E402
from monitor.viagens import (consultas, criar_id, ler_viagens, salvar_pausas,  # noqa: E402
                             salvar_viagens)

# MONITOR_CONFIG / MONITOR_DADOS: só para testar o painel com cópias, sem mexer nos arquivos reais
CONFIG = Path(os.getenv("MONITOR_CONFIG") or CONFIG_PADRAO)
PASTA_CONFIG = CONFIG.parent
DADOS = Path(os.getenv("MONITOR_DADOS") or RAIZ / "data")
FUSO = "America/Recife"

FONTES_OK = {
    "google_flights": ("Google Flights", "referência de preço; cota limitada (~8 buscas por dia, somando tudo)"),
    "skiplagged": ("Skiplagged", "costuma ser o mais barato; traz horários, escalas e onde comprar"),
    "kayak": ("Kayak", "para comparar; traz só o menor preço da página"),
}
FONTES_BLOQUEADAS = {"latam": "LATAM", "gol": "Gol", "azul": "Azul", "decolar": "Decolar",
                     "skyscanner": "Skyscanner"}
NOMES_FONTES = {k: v[0] for k, v in FONTES_OK.items()} | FONTES_BLOQUEADAS
#: em quantos computadores do GitHub o Skiplagged roda por rodada (matriz do workflow)
PARTES_SKIPLAGGED = 4


# ----------------------------------------------------------------- utilidades
def fmt_data(iso: str | date | None) -> str:
    if not iso:
        return ""
    d = iso if isinstance(iso, date) else date.fromisoformat(str(iso)[:10])
    return d.strftime("%d/%m/%Y")


def fmt_quando(iso: str) -> str:
    if not iso:
        return ""
    dt = datetime.fromisoformat(iso)
    return f"{DIAS[dt.weekday()]} {dt:%d/%m %H:%M}"


def fmt_conexoes(texto: str) -> str:
    return re.sub(r"(\w+) (\d+)min", lambda m: f"{m[1]} ({int(m[2]) // 60}h{int(m[2]) % 60:02d})",
                  texto or "")


def resumo_datas(spec) -> str:
    if isinstance(spec, dict):
        txt = f"{fmt_data(spec['de'])} a {fmt_data(spec['ate'])}"
        passo = int(spec.get("a_cada_dias", 1))
        return txt + (f", a cada {passo} dias" if passo > 1 else "")
    if isinstance(spec, list):
        return ", ".join(fmt_data(d) for d in expandir_datas(spec))
    return fmt_data(spec)


def nome_viagem(v: dict) -> str:
    return v.get("nome") or f"{v.get('origem')} → {v.get('destino')}"


def geral() -> Geral:
    try:
        return carregar(CONFIG).geral
    except Exception:  # noqa: BLE001
        return Geral()


def viagens() -> list[dict]:
    return ler_viagens(CONFIG)


def gravar(lista: list[dict], ok: str) -> bool:
    try:
        salvar_viagens(CONFIG, lista)
    except ValueError as e:
        st.error("Não salvei, porque:\n\n" + "\n".join(f"- {x}" for x in str(e).splitlines()))
        return False
    st.toast(ok, icon="✅")
    return True


def frequencia(lista: list[dict], g: Geral) -> dict[str, str]:
    """Quantas vezes por dia cada data é atualizada, por fonte (estimativa)."""
    total: dict[str, int] = defaultdict(int)
    for v in lista:
        if v.get("ativa", True):
            try:
                n = len(consultas(v))
            except Exception:  # noqa: BLE001
                continue
            for f in v.get("fontes") or []:
                total[f] += n
    rodadas = 12  # o monitor roda a cada 2h
    texto = {}
    for f, n in total.items():
        if not n:
            continue
        if f == "google_flights":
            vezes = min(24 / g.google_flights_a_cada_horas, (g.orcamento_serpapi_mes / 30) / n)
        elif f == "skiplagged":
            # 4 computadores por rodada (ver .github/workflows/monitor.yml), até N datas cada
            vezes = rodadas * min(1, PARTES_SKIPLAGGED * g.skiplagged_max_por_rodada / n)
        else:
            vezes = rodadas
        if vezes >= 1:
            quando = f"~{vezes:.0f}x por dia" if vezes >= 2 else f"~{vezes:.1f}x por dia".replace(".", ",")
        else:
            quando = f"1x a cada ~{1 / vezes:.0f} dias"
        texto[f] = f"{n} data(s) → cada uma atualizada {quando}"
    return texto


@st.cache_data
def _ler_csv(caminho: str, mtime: float) -> pd.DataFrame:
    df = pd.read_csv(caminho, dtype=str, keep_default_na=False)
    df["preco"] = pd.to_numeric(df["preco"], errors="coerce")
    coleta = pd.to_datetime(df["data_hora_utc"], utc=True, errors="coerce", format="ISO8601")
    df["coleta"] = coleta.dt.tz_convert(FUSO).dt.tz_localize(None)
    return df.dropna(subset=["preco", "coleta"])


def historico() -> pd.DataFrame:
    arq = DADOS / "historico_precos.csv"
    if not arq.exists():
        return pd.DataFrame()
    return _ler_csv(str(arq), arq.stat().st_mtime)


def sem_dados() -> None:
    st.info(
        "Ainda não há preços guardados.\n\n"
        "O histórico enche conforme o monitor roda no GitHub (a cada 2h). Para trazer os preços para cá, use "
        "**⬇️ Baixar preços novos** na tela **Salvar e publicar**. "
        "Os testes da tela **Testar uma viagem** não entram no histórico."
    )


def descricao_busca(linha) -> str:
    txt = fmt_data(linha["ida"])
    return txt + (f" → {fmt_data(linha['volta'])}" if linha["volta"] else " (só ida)")


def nomes_por_id() -> dict[str, str]:
    try:
        return {v["id"]: nome_viagem(v) for v in viagens()}
    except Exception:  # noqa: BLE001
        return {}


# ----------------------------------------------------------------- formulário
def campo_aeroporto(titulo: str, codigo: str, chave: str) -> str:
    lista = aeroportos()
    opcoes = list(lista) if codigo in lista or not codigo else [codigo, *lista]
    return st.selectbox(titulo, opcoes, index=opcoes.index(codigo) if codigo in opcoes else 0, key=chave,
                        format_func=lambda c: f"{c} — {lista[c]}" if c in lista else f"{c} (fora da lista)",
                        help=f"Digite a cidade, o aeroporto ou o código. São {len(lista):,} aeroportos comerciais "
                             "do mundo; os mais usados aparecem primeiro. Cidades de fora do Brasil podem estar "
                             "em inglês (ex.: Lisbon).".replace(",", "."))


def campo_datas(chave: str, spec, titulo: str):
    """Devolve a especificação de datas do rotas.yaml (texto, intervalo ou lista) ou None."""
    hoje = date.today()
    datas = expandir_datas(spec) if spec else []
    minimo = min([hoje, *datas])
    padrao = datas[0] if datas else hoje + timedelta(days=30)
    modos = ["Uma data", "Período", "Datas avulsas"]
    inicial = 1 if isinstance(spec, dict) else 2 if isinstance(spec, list) and len(datas) > 1 else 0
    modo = st.radio(titulo, modos, index=inicial, horizontal=True, key=chave + "_modo",
                    help="Período = todos os dias entre duas datas. Datas avulsas = escolha dia por dia.")
    if modo == "Uma data":
        d = st.date_input("Data", value=padrao, min_value=minimo, format="DD/MM/YYYY", key=chave + "_uma")
        return d.isoformat() if d else None
    if modo == "Período":
        ini = date.fromisoformat(spec["de"]) if isinstance(spec, dict) else padrao
        fim = date.fromisoformat(spec["ate"]) if isinstance(spec, dict) else (datas[-1] if len(datas) > 1
                                                                            else padrao + timedelta(days=2))
        r = st.date_input("Do dia … até o dia … (clique na primeira e depois na última data)",
                          value=(ini, fim), min_value=minimo, format="DD/MM/YYYY", key=chave + "_per")
        if not isinstance(r, tuple) or len(r) < 2:
            st.caption("👉 Clique também na data final.")
            return None
        passo = st.number_input("Buscar a cada quantos dias?", 1, 30, key=chave + "_passo",
                                value=int(spec.get("a_cada_dias", 1)) if isinstance(spec, dict) else 1,
                                help="1 = todos os dias do período. 2 = um dia sim, outro não…")
        saida = {"de": r[0].isoformat(), "ate": r[1].isoformat()}
        if passo > 1:
            saida["a_cada_dias"] = int(passo)
        return saida
    tabela = pd.DataFrame({"Data": pd.to_datetime(datas or [padrao])})
    st.caption("Clique numa data para trocar; use a última linha (+) para acrescentar.")
    ed = st.data_editor(tabela, num_rows="dynamic", hide_index=True, key=chave + "_avul",
                        column_config={"Data": st.column_config.DateColumn(
                            "Data", format="DD/MM/YYYY", min_value=minimo)})
    escolhidas = sorted({pd.Timestamp(x).date() for x in ed["Data"].dropna()})
    return [d.isoformat() for d in escolhidas] or None


def escolher_fontes(atuais: list[str], chave: str) -> list[str]:
    st.markdown("**Onde procurar**")
    escolhidas = []
    for f, (nome, desc) in FONTES_OK.items():
        if st.checkbox(f"{nome} — {desc}", value=f in atuais, key=f"{chave}_f_{f}"):
            escolhidas.append(f)
    bloq_marcadas = [f for f in FONTES_BLOQUEADAS if f in atuais]
    with st.expander("Sites que bloquearam o robô no teste de 20/11/2026 (não recomendados)",
                     expanded=bool(bloq_marcadas)):
        st.caption("Esses sites recusaram o acesso automático. Se marcar, o monitor tenta, mas deve falhar.")
        for f, nome in FONTES_BLOQUEADAS.items():
            if st.checkbox(nome, value=f in atuais, key=f"{chave}_f_{f}"):
                escolhidas.append(f)
    return escolhidas


def limpar_formulario() -> None:
    for k in [k for k in st.session_state if str(k).startswith("form_")]:
        del st.session_state[k]
    st.session_state.pop("editando", None)


def formulario(v: dict | None, lista: list[dict], g: Geral) -> None:
    nova = v is None
    v = v or {}
    k = "form_" + (v.get("id") or "nova")
    st.subheader("➕ Nova viagem" if nova else f"✏️ Editar: {nome_viagem(v)}")

    nome = st.text_input("Nome da viagem (para você reconhecer)", value=v.get("nome", ""),
                         placeholder="Ex.: Férias em São Paulo", key=k + "_nome")
    tem_volta = nova or bool(v.get("volta") or v.get("duracao_dias"))
    tipo = st.radio("Tipo de passagem", ["Ida e volta", "Só ida"], index=0 if tem_volta else 1,
                    horizontal=True, key=k + "_tipo")
    c1, c2 = st.columns(2)
    with c1:
        origem = campo_aeroporto("Saindo de", v.get("origem", "REC"), k + "_orig")
    with c2:
        destino = campo_aeroporto("Indo para", v.get("destino", "GRU"), k + "_dest")

    ida = campo_datas(k + "_ida", v.get("ida"), "Datas de ida")
    volta = duracao = None
    if tipo == "Ida e volta":
        modo = st.radio("Como escolher a volta", ["Data de volta", "Ficar um número de dias"],
                        index=1 if v.get("duracao_dias") else 0, horizontal=True, key=k + "_vmodo")
        if modo == "Data de volta":
            volta = campo_datas(k + "_volta", v.get("volta"), "Datas de volta")
        else:
            atual = v.get("duracao_dias") or [7]
            duracao = st.multiselect("Quantos dias de viagem? (pode marcar mais de um)", list(range(1, 61)),
                                     default=atual if isinstance(atual, list) else [atual], key=k + "_dur")

    c1, c2, c3 = st.columns(3)
    adultos = c1.number_input("Adultos", 1, 9, value=int(v.get("adultos", 1)), key=k + "_adultos")
    alvo = c2.number_input("Me avise se ficar abaixo de (R$)", 0, 100_000, step=50, key=k + "_alvo",
                           value=int(v.get("preco_alvo") or 0),
                           help="0 = sem preço-alvo. Os outros avisos (menor já visto e queda) continuam valendo: "
                                "basta UMA regra para avisar. Na ida e volta, é o preço total.")
    queda = c3.number_input("…ou se cair este % abaixo da média", 5, 80, key=k + "_queda",
                            value=int(v.get("queda_pct") or g.queda_pct_padrao))
    referencia_preco(v.get("id"))
    with st.expander("Como funcionam os avisos?"):
        st.markdown(REGRAS_TEXTO)
    fontes = escolher_fontes(v.get("fontes") or ["google_flights", "skiplagged"], k)

    ids = {x["id"] for x in lista if x.get("id") != v.get("id")}
    nova_v = {
        "id": v.get("id") or criar_id(nome or f"{origem}-{destino}", ids),
        "nome": nome.strip(), "ativa": v.get("ativa", True), "origem": origem, "destino": destino,
        "ida": ida, "volta": volta, "duracao_dias": duracao, "adultos": int(adultos),
        "preco_alvo": int(alvo) or None, "queda_pct": int(queda), "fontes": fontes,
    }
    if ida and (tipo == "Só ida" or volta or duracao):
        try:
            cs = consultas(nova_v)
            st.info(f"Esta viagem gera **{len(cs)} busca(s)** em cada fonte marcada.")
            with st.expander("Ver as buscas"):
                st.write("\n".join(f"- {c.descricao()}" for c in cs) or "Nenhuma (as datas já passaram).")
        except Exception as e:  # noqa: BLE001
            st.warning(f"Datas incompletas: {e}")

    b1, b2, _ = st.columns([1, 1, 4])
    if b1.button("💾 Salvar", type="primary", key=k + "_salvar"):
        if tipo == "Ida e volta" and not (volta or duracao):
            st.error("Escolha a volta (data ou número de dias), ou marque **Só ida**.")
            return
        novas = [nova_v if x.get("id") == v.get("id") else x for x in lista] if not nova else [*lista, nova_v]
        if gravar(novas, "Viagem salva no Mac"):
            limpar_formulario()
            st.rerun()
    if b2.button("Cancelar", key=k + "_cancelar"):
        limpar_formulario()
        st.rerun()


@st.dialog("Excluir viagem")
def confirmar_exclusao(id_: str) -> None:
    lista = viagens()
    v = next((x for x in lista if x["id"] == id_), None)
    if v is None:
        st.rerun()
    st.write(f"Excluir **{nome_viagem(v)}**? Os preços já guardados dela continuam no histórico.")
    if st.button("Sim, excluir", type="primary"):
        if gravar([x for x in lista if x["id"] != id_], "Viagem excluída"):
            st.rerun()


def alternar_viagem(id_: str, chave: str) -> None:
    lista = viagens()
    for x in lista:
        if x["id"] == id_:
            x["ativa"] = bool(st.session_state[chave])
    try:
        salvar_viagens(CONFIG, lista)
        st.toast("Ligada" if st.session_state[chave] else "Desligada", icon="✅")
    except ValueError as e:
        st.session_state["erro_toggle"] = str(e)


# ----------------------------------------------------------------- telas
def mostrar_precos(df: pd.DataFrame, id_: str) -> None:
    """Menor preço das últimas 24h e menor já visto, no cartão da viagem."""
    d = df[df["rota_id"] == id_] if not df.empty else df
    if d.empty:
        st.caption("💰 Ainda sem preço coletado. (Depois das rodadas, use ⬇️ Baixar preços novos em Salvar e publicar.)")
        return
    recentes = d[d["coleta"] >= d["coleta"].max() - pd.Timedelta(hours=24)]
    m, menor = recentes.loc[recentes["preco"].idxmin()], d.loc[d["preco"].idxmin()]
    onde = NOMES_FONTES.get(m["fonte"], m["fonte"]) + (f" ({m['vendedor']})" if m.get("vendedor") else "")
    st.markdown(f"💰 **{brl(m['preco'])}** · {descricao_busca(m)} · {onde} — menor preço das últimas 24h")
    st.caption(f"Menor já visto: {brl(menor['preco'])} ({descricao_busca(menor)}) · última coleta: "
               f"{d['coleta'].max():%d/%m %H:%M} · {len(d)} preço(s) guardados")


REGRAS_TEXTO = (
    "Você recebe aviso quando **qualquer uma** destas acontecer (é \"ou\", não \"e\"):\n"
    "1. o preço fica **igual ou abaixo** do valor de aviso (0 = não usar esta regra);\n"
    "2. aparece o **menor preço já visto** para aquela data (a partir da 3ª leitura);\n"
    "3. o preço cai **X%** abaixo da média dos últimos 14 dias.\n\n"
    "O mesmo aviso não se repete por 72h, a não ser que o preço caia mais. **Sem referência de preço?** "
    "Deixe 0: as regras 2 e 3 avisam sozinhas quando o preço cair. E todo dia, a partir das 7h, chega o "
    "resumo com o menor preço de cada viagem."
)


def referencia_preco(id_: str | None) -> None:
    df = historico()
    if not id_ or df.empty or id_ not in set(df["rota_id"]):
        st.caption("Referência de preço: aparece aqui depois das primeiras coletas desta viagem.")
        return
    d = df[df["rota_id"] == id_]
    texto = f"Referência: menor preço encontrado até agora **{brl(d['preco'].min())}**"
    tipicos = d.loc[d["nivel_preco"].str.contains("típico", na=False), "nivel_preco"]
    if not tipicos.empty:
        texto += f"; Google Flights: {tipicos.iloc[-1]}"
    st.caption(texto + ".")


def tela_viagens() -> None:
    st.title("✈️ Minhas viagens")
    g = geral()
    try:
        lista = viagens()
    except Exception as e:  # noqa: BLE001
        st.error(f"Não consegui ler o config/rotas.yaml: {e}")
        return
    if erro := st.session_state.pop("erro_toggle", None):
        st.error(erro)

    editando = st.session_state.get("editando")
    if editando:
        v = None if editando == "__nova__" else next((x for x in lista if x["id"] == editando), None)
        formulario(v, lista, g)
        return

    if st.button("➕ Nova viagem", type="primary"):
        limpar_formulario()
        st.session_state["editando"] = "__nova__"
        st.rerun()
    if not lista:
        st.info("Nenhuma viagem cadastrada ainda.")

    df = historico()
    for v in lista:
        with st.container(border=True):
            c1, c2 = st.columns([5, 2])
            ida_volta = bool(v.get("volta") or v.get("duracao_dias"))
            with c1:
                st.markdown(f"#### {nome_viagem(v)}")
                st.markdown(f"{rotulo(v['origem'])} → {rotulo(v['destino'])}")
                partes = [("Ida e volta" if ida_volta else "Só ida"), f"Ida: {resumo_datas(v['ida'])}"]
                if v.get("volta"):
                    partes.append(f"Volta: {resumo_datas(v['volta'])}")
                if v.get("duracao_dias"):
                    d = v["duracao_dias"]
                    partes.append(f"Ficar: {', '.join(map(str, d if isinstance(d, list) else [d]))} dias")
                partes.append(f"{v.get('adultos', 1)} adulto(s)")
                if v.get("preco_alvo"):
                    partes.append(f"aviso abaixo de {brl(float(v['preco_alvo']))}")
                st.caption(" · ".join(partes))
                fontes = ", ".join(NOMES_FONTES.get(f, f) for f in v.get("fontes") or [])
                try:
                    n = len(consultas(v))
                    st.caption(f"Fontes: {fontes} · {n} busca(s) por fonte"
                               + (" — ⚠️ todas as datas já passaram" if n == 0 else ""))
                except Exception as e:  # noqa: BLE001
                    st.caption(f"Fontes: {fontes} · ⚠️ {e}")
                mostrar_precos(df, v["id"])
            with c2:
                chave = f"tg_viagem_{v['id']}"
                st.toggle("Ligada", value=bool(v.get("ativa", True)), key=chave,
                          on_change=alternar_viagem, args=(v["id"], chave),
                          help="Desligada = o monitor não procura esta viagem (a partir da próxima rodada).")
                b1, b2, b3 = st.columns(3)
                if b1.button("✏️", key=f"ed_{v['id']}", help="Editar"):
                    limpar_formulario()
                    st.session_state["editando"] = v["id"]
                    st.rerun()
                if b2.button("🧪", key=f"te_{v['id']}", help="Testar agora"):
                    st.session_state["testar_id"] = v["id"]
                    st.switch_page(PAGINAS["testar"])
                if b3.button("🗑️", key=f"ex_{v['id']}", help="Excluir"):
                    confirmar_exclusao(v["id"])

    freq = frequencia(lista, g)
    if freq:
        with st.container(border=True):
            st.markdown("**Com as viagens ligadas, a frequência de atualização fica assim:**")
            for f, txt in freq.items():
                st.markdown(f"- **{NOMES_FONTES.get(f, f)}:** {txt}")
            st.caption("Google Flights: o plano gratuito permite ~8 buscas por dia no total. Quanto mais datas, "
                       f"menos vezes cada uma é atualizada. Skiplagged: {PARTES_SKIPLAGGED} computadores a cada 2h, "
                       f"até {PARTES_SKIPLAGGED * g.skiplagged_max_por_rodada} datas por rodada.")


def tela_historico() -> None:
    st.title("📈 Preços ao longo do tempo")
    df = historico()
    if df.empty:
        sem_dados()
        return
    nomes = nomes_por_id()
    ids = sorted(df["rota_id"].unique(), key=lambda i: nomes.get(i, i))
    sel = st.selectbox("Viagem", ids, format_func=lambda i: nomes.get(i, i))
    d = df[df["rota_id"] == sel].copy()
    d["busca"] = d.apply(descricao_busca, axis=1)

    resumo = (d.sort_values("preco").groupby("chave", as_index=False)
              .agg(busca=("busca", "first"), menor=("preco", "first"), fonte=("fonte", "first"),
                   quando=("coleta", "first"), leituras=("preco", "size")))
    chaves = resumo.sort_values("menor")["chave"].tolist()
    chave = st.selectbox("Datas", chaves, format_func=lambda c: resumo.set_index("chave").at[c, "busca"],
                         help="Já vem escolhida a combinação de datas com o menor preço.")
    b = d[d["chave"] == chave].sort_values("coleta")
    melhor = b.loc[b["preco"].idxmin()]
    ultima = b[b["coleta"] == b["coleta"].max()]
    alvo = next((float(v["preco_alvo"]) for v in viagens() if v["id"] == sel and v.get("preco_alvo")), None)

    c1, c2, c3 = st.columns(3)
    c1.metric("Menor preço já visto", brl(melhor["preco"]),
              help=f"{NOMES_FONTES.get(melhor['fonte'], melhor['fonte'])}, em {melhor['coleta']:%d/%m %H:%M}")
    c1.caption(f"{NOMES_FONTES.get(melhor['fonte'], melhor['fonte'])} · {melhor['coleta']:%d/%m/%Y %H:%M}")
    c2.metric("Último preço coletado", brl(ultima["preco"].min()))
    c2.caption(f"{ultima['coleta'].max():%d/%m/%Y %H:%M}")
    c3.metric("Seu preço de aviso", brl(alvo) if alvo else "—")

    tooltip = [alt.Tooltip("coleta:T", title="Coleta", format="%d/%m %H:%M"),
               alt.Tooltip("preco:Q", title="Preço (R$)", format=",.2f"),
               alt.Tooltip("fonte:N", title="Fonte"), alt.Tooltip("companhia:N", title="Cia")]
    linhas = alt.Chart(b).mark_line(point=True).encode(
        x=alt.X("coleta:T", title="Data da coleta", axis=alt.Axis(format="%d/%m")),
        y=alt.Y("preco:Q", title="Preço (R$)", scale=alt.Scale(zero=False)),
        color=alt.Color("fonte:N", title="Fonte"), tooltip=tooltip)
    grafico = linhas
    if alvo:
        grafico += alt.Chart(pd.DataFrame({"y": [alvo]})).mark_rule(
            strokeDash=[6, 4], color="#d62728").encode(y="y:Q")
    st.altair_chart(grafico, use_container_width=True)
    if alvo:
        st.caption("Linha vermelha tracejada = seu preço de aviso.")

    st.markdown("**Todas as datas desta viagem** (menor preço de cada uma)")
    st.dataframe(
        resumo.sort_values("menor").assign(fonte=lambda x: x["fonte"].map(lambda f: NOMES_FONTES.get(f, f)),
                                           menor=lambda x: x["menor"].map(brl))
        [["busca", "menor", "fonte", "quando", "leituras"]],
        hide_index=True, use_container_width=True,
        column_config={"busca": "Datas", "menor": "Menor preço",
                       "fonte": "Fonte", "quando": st.column_config.DatetimeColumn("Quando", format="DD/MM HH:mm"),
                       "leituras": "Nº de leituras"})


def tela_analise() -> None:
    st.title("🗓️ Melhor dia e horário")
    st.caption("Em que dia da semana e em que horário o monitor costuma achar preços mais baixos que o normal.")
    df = historico()
    if df.empty:
        sem_dados()
        return
    nomes = nomes_por_id()
    c1, c2 = st.columns(2)
    viagem = c1.selectbox("Viagem", ["(todas)", *sorted(df["rota_id"].unique())],
                          format_func=lambda i: nomes.get(i, i))
    fonte = c2.selectbox("Fonte", ["(todas)", *sorted(df["fonte"].unique())],
                         format_func=lambda f: NOMES_FONTES.get(f, f))
    d = df.copy()
    if viagem != "(todas)":
        d = d[d["rota_id"] == viagem]
    if fonte != "(todas)":
        d = d[d["fonte"] == fonte]
    if d.empty:
        st.info("Sem dados para esse filtro.")
        return

    # compara cada preço com o preço "típico" (mediana) da mesma busca na mesma fonte
    d["dif"] = (d["preco"] / d.groupby(["chave", "fonte"])["preco"].transform("median") - 1) * 100
    d["dia_n"] = d["coleta"].dt.weekday
    d["dia"] = d["dia_n"].map(lambda i: DIAS[i])
    d["hora"] = d["coleta"].dt.hour.map(lambda h: f"{h:02d}h")
    dias_de_dados = (d["coleta"].max() - d["coleta"].min()).days + 1
    if dias_de_dados < 21:
        st.warning(f"Por enquanto há **{dias_de_dados} dia(s)** de coletas. A análise fica confiável com "
                   "3 a 4 semanas; antes disso, as diferenças podem ser acaso.")

    grade = d.groupby(["dia_n", "dia", "hora"], as_index=False).agg(dif=("dif", "mean"), n=("dif", "size"))
    lim = max(5.0, float(grade["dif"].abs().max()))
    base = alt.Chart(grade).encode(
        x=alt.X("hora:O", title="Horário da coleta (Recife)"),
        y=alt.Y("dia:O", title=None, sort=DIAS),
    )
    mapa = base.mark_rect().encode(
        color=alt.Color("dif:Q", title="% vs. normal",
                        scale=alt.Scale(scheme="redyellowgreen", reverse=True, domain=[-lim, lim])),
        tooltip=[alt.Tooltip("dia:O", title="Dia"), alt.Tooltip("hora:O", title="Hora"),
                 alt.Tooltip("dif:Q", title="% vs. normal", format="+.1f"),
                 alt.Tooltip("n:Q", title="Nº de preços")])
    texto = base.mark_text(fontSize=11).encode(text=alt.Text("dif:Q", format="+.0f"))
    st.altair_chart(mapa + texto, use_container_width=True)
    st.caption("Verde = preços abaixo do normal daquela busca; vermelho = acima. O número é a diferença média "
               "em %. O monitor coleta a cada 2h, então só aparecem os horários das rodadas.")

    por_dia = d.groupby("dia_n")["dif"].mean()
    por_hora = d.groupby(d["coleta"].dt.hour)["dif"].mean()
    c1, c2 = st.columns(2)
    c1.metric("Melhor dia (até agora)", DIAS[int(por_dia.idxmin())], f"{por_dia.min():+.1f}% vs. normal",
              delta_color="inverse")
    c2.metric("Melhor horário (até agora)", f"{int(por_hora.idxmin()):02d}h", f"{por_hora.min():+.1f}% vs. normal",
              delta_color="inverse")


def tela_buscas() -> None:
    st.title("🔎 Histórico de buscas")
    df = historico()
    if df.empty:
        sem_dados()
        return
    nomes = nomes_por_id()
    c1, c2, c3 = st.columns(3)
    sel_v = c1.multiselect("Viagens", sorted(df["rota_id"].unique()), format_func=lambda i: nomes.get(i, i),
                           placeholder="Todas")
    sel_f = c2.multiselect("Fontes", sorted(df["fonte"].unique()), format_func=lambda f: NOMES_FONTES.get(f, f),
                           placeholder="Todas")
    ini, fim = df["coleta"].min().date(), df["coleta"].max().date()
    periodo = c3.date_input("Coletas entre", value=(ini, fim), format="DD/MM/YYYY")
    d = df
    if sel_v:
        d = d[d["rota_id"].isin(sel_v)]
    if sel_f:
        d = d[d["fonte"].isin(sel_f)]
    if isinstance(periodo, tuple) and len(periodo) == 2:
        d = d[(d["coleta"].dt.date >= periodo[0]) & (d["coleta"].dt.date <= periodo[1])]
    d = d.sort_values("coleta", ascending=False)

    tabela = pd.DataFrame({
        "Coleta": d["coleta"],
        "Viagem": d["rota_id"].map(lambda i: nomes.get(i, i)),
        "Trecho": d["origem"] + " → " + d["destino"],
        "Ida": d["ida"].map(fmt_data), "Volta": d["volta"].map(fmt_data),
        "Fonte": d["fonte"].map(lambda f: NOMES_FONTES.get(f, f)),
        "Preço": d["preco"].map(brl),
        "Cia": d["companhia"],
        "Saída (ida)": d["ida_partida"].map(fmt_quando), "Chegada (ida)": d["ida_chegada"].map(fmt_quando),
        "Escalas (ida)": d["ida_conexoes"].map(fmt_conexoes),
        "Saída (volta)": d["volta_partida"].map(fmt_quando), "Chegada (volta)": d["volta_chegada"].map(fmt_quando),
        "Escalas (volta)": d["volta_conexoes"].map(fmt_conexoes),
        "Onde comprar": d["vendedor"], "Avisos": d["avisos"], "Nível (Google)": d["nivel_preco"],
        "Link": d["link"],
    })
    st.caption(f"{len(tabela)} preço(s) encontrados.")
    st.dataframe(tabela, hide_index=True, use_container_width=True, column_config={
        "Coleta": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm"),
        "Link": st.column_config.LinkColumn(display_text="abrir"),
    })
    planilha = tabela.assign(Coleta=tabela["Coleta"].dt.strftime("%d/%m/%Y %H:%M"), **{"Preço": d["preco"]})
    st.download_button("⬇️ Baixar planilha (abre no Excel/Numbers)",
                       planilha.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
                       file_name=f"historico_passagens_{date.today():%Y-%m-%d}.csv", mime="text/csv")


def alternar_fonte(fonte: str, chave: str) -> None:
    desligadas = set(ler_pausas(PASTA_CONFIG))
    if st.session_state[chave]:
        desligadas.discard(fonte)
    else:
        desligadas.add(fonte)
    salvar_pausas(PASTA_CONFIG, sorted(desligadas))
    st.toast(f"{NOMES_FONTES.get(fonte, fonte)} {'ligada' if st.session_state[chave] else 'desligada'}", icon="✅")


def tela_parar() -> None:
    st.title("⏸️ Pausar e parar buscas")
    st.caption("As mudanças valem a partir da próxima rodada do monitor, depois de publicar (tela Salvar e publicar).")
    lista = viagens()

    st.subheader("Viagens")
    if not lista:
        st.caption("Nenhuma viagem cadastrada.")
    for v in lista:
        chave = f"tg_parar_{v['id']}"
        st.toggle(nome_viagem(v), value=bool(v.get("ativa", True)), key=chave,
                  on_change=alternar_viagem, args=(v["id"], chave))

    st.subheader("Fontes")
    st.caption("Desligar uma fonte vale para todas as viagens.")
    desligadas = set(ler_pausas(PASTA_CONFIG))
    usadas = {f for v in lista for f in v.get("fontes") or []}
    for f in [*FONTES_OK, *[x for x in FONTES_BLOQUEADAS if x in usadas | desligadas]]:
        chave = f"tg_fonte_{f}"
        st.toggle(NOMES_FONTES.get(f, f), value=f not in desligadas, key=chave,
                  on_change=alternar_fonte, args=(f, chave))

    st.subheader("Descanso automático")
    estado = DADOS / "estado.json"
    agora = datetime.now(timezone.utc)
    pausas = {}
    if estado.exists():
        pausas = {f: datetime.fromisoformat(a) for f, a in
                  (json.loads(estado.read_text(encoding="utf-8")).get("pausa") or {}).items()}
    ativas = {f: a for f, a in pausas.items() if a > agora}
    if ativas:
        for f, a in ativas.items():
            local = pd.Timestamp(a).tz_convert(FUSO)
            st.warning(f"**{NOMES_FONTES.get(f, f)}** bloqueou 2 vezes seguidas e está descansando até "
                       f"{local:%d/%m %H:%M}.")
    else:
        st.caption("Nenhuma fonte descansando. (Quando um site bloqueia 2 vezes seguidas, ele descansa "
                   f"{geral().pausa_apos_bloqueio_horas:g}h sozinho.)")

    st.subheader("Parar uma execução que está rodando agora")
    rodando = [r for r in execucoes(5) if r["status"] in ("in_progress", "queued")]
    if not rodando:
        st.caption("Nenhuma execução rodando no GitHub agora.")
    for r in rodando:
        quando = pd.Timestamp(r["createdAt"]).tz_convert(FUSO)
        if st.button(f"⏹️ Parar agora a execução das {quando:%H:%M}", key=f"parar_{r['databaseId']}"):
            ok, saida = _cmd("gh", "run", "cancel", str(r["databaseId"]), "--repo", REPO, timeout=30)
            (st.success if ok else st.error)("Pedido de parada enviado." if ok else saida)

    ultima = DADOS / "ultima_execucao.json"
    if ultima.exists():
        info = json.loads(ultima.read_text(encoding="utf-8"))
        quando = pd.Timestamp(info["data_hora_utc"]).tz_convert(FUSO)
        st.caption(f"Última rodada registrada: {quando:%d/%m/%Y %H:%M} · {info.get('precos_registrados', 0)} "
                   f"preço(s) · {info.get('alertas', 0)} aviso(s).")


@st.cache_data(ttl=300, show_spinner=False)
def saldo_google() -> dict | None:
    try:
        from dotenv import load_dotenv

        from monitor.fontes.google_flights import saldo_serpapi
        load_dotenv(RAIZ / ".env")
        chave = os.getenv("SERPAPI_KEY")
        return saldo_serpapi(chave) if chave else None
    except Exception:  # noqa: BLE001
        return None


def tela_testar() -> None:
    st.title("🧪 Testar uma viagem")
    st.caption("Faz uma busca de verdade agora e mostra o resultado. Não manda aviso e não grava no histórico.")
    lista = viagens()
    if not lista:
        st.info("Cadastre uma viagem primeiro.")
        return
    ids = [v["id"] for v in lista]
    pedido = st.session_state.pop("testar_id", None)
    sel = st.selectbox("Viagem", ids, index=ids.index(pedido) if pedido in ids else 0,
                       format_func=lambda i: nome_viagem(next(v for v in lista if v["id"] == i)))
    v = next(x for x in lista if x["id"] == sel)
    fontes = st.multiselect("Fontes", v.get("fontes") or [], default=v.get("fontes") or [],
                            format_func=lambda f: NOMES_FONTES.get(f, f))
    try:
        n = len(consultas(v))
    except Exception as e:  # noqa: BLE001
        st.error(str(e))
        return
    if n == 0:
        st.warning("Todas as datas desta viagem já passaram.")
        return
    st.markdown(f"**{n} busca(s) por fonte.**")
    if "google_flights" in fontes:
        s = saldo_google()
        saldo = (f"Saldo agora: **{s['cota'] - s['usados']} de {s['cota']}**, renova em {fmt_data(s['renovacao'])}."
                 if s else "Não consegui consultar o saldo agora.")
        st.warning(f"Cada data no Google Flights gasta 1 busca da cota mensal (no máximo 5 neste teste). {saldo}")
    st.caption("Tempo: o Skiplagged leva de 10 a 40 segundos por busca; a Kayak, cerca de 25 segundos.")

    if not st.button("▶️ Rodar o teste agora", type="primary", disabled=not fontes):
        return
    cmd = [sys.executable, "-m", "monitor", "--simular", "--config", str(CONFIG), "--dados", str(DADOS),
           "--rota", sel, "--fontes", *fontes]
    env = {**os.environ, "PYTHONPATH": str(RAIZ / "src")}
    resultados, saida = [], []
    with st.status("Buscando… pode levar alguns minutos.", expanded=True) as status:
        proc = subprocess.Popen(cmd, cwd=RAIZ, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
        for linha in proc.stdout:
            saida.append(linha.rstrip())
            m = re.search(r"(INFO|WARNING|ERROR)\s+(\w+)\s+(.+) → (.+)$", linha)
            if m and m[2] in NOMES_FONTES:
                resultados.append(m[0])
                st.write(f"{NOMES_FONTES[m[2]]}: {m[3].strip()} → **{m[4].strip()}**")
            elif re.search(r"WARNING|ERROR", linha):
                st.write("⚠️ " + re.sub(r"^\S+\s+(WARNING|ERROR)\s+", "", linha.strip()))
        proc.wait()
        status.update(label="Teste concluído." if proc.returncode == 0 else "Teste terminou com falhas.",
                      state="complete" if proc.returncode == 0 else "error", expanded=True)
    if not resultados:
        st.warning("Nenhum resultado de busca apareceu. Abra o registro completo abaixo.")
    alertas = "\n".join(saida).split("--- ALERTA (simulação, não enviado) ---")[1:]
    for a in alertas:
        with st.expander("🔔 Aviso que seria enviado no Telegram"):
            st.text(a.strip().split("\n\n")[0])
    with st.expander("Registro completo (para o Claude analisar, se der problema)"):
        st.code("\n".join(saida) or "(vazio)")


REPO = "mycaeldemouradias/monitor-de-passagens"


def limpar_auxiliares() -> int:
    """Apaga os arquivos "._*" que o macOS cria no SSD (exFAT). Não são dados; só atrapalham."""
    n = 0
    for p in RAIZ.rglob("._*"):
        if ".venv" not in p.parts and p.is_file():
            p.unlink(missing_ok=True)
            n += 1
    return n


def _cmd(*args: str, timeout: int = 120) -> tuple[bool, str]:
    """Roda git/gh na pasta do projeto. Devolve (deu_certo, saída)."""
    try:
        r = subprocess.run(list(args), cwd=RAIZ, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def _pendentes() -> list[str]:
    try:
        from monitor.cofre import pendentes
        return pendentes(RAIZ)
    except Exception:  # noqa: BLE001
        return []


def baixar_novidades() -> tuple[bool, str]:
    """Traz os commits do GitHub (preços novos) e abre o histórico criptografado."""
    from monitor.cofre import abrir
    # merge (e não rebase): no SSD exFAT o macOS cria "._*" no meio do rebase e ele trava (visto em 21/11)
    limpar_auxiliares()
    ok, saida = _cmd("git", "pull", "--no-rebase", "--no-edit")
    if not ok:
        return False, saida
    abertos = abrir(RAIZ, "dados")
    limpar_auxiliares()
    st.cache_data.clear()
    return True, f"{len(abertos)} arquivo(s) de dados atualizados."


def publicar() -> tuple[bool, str]:
    from monitor.cofre import fechar
    mudaram = fechar(RAIZ, "config")
    if mudaram:
        ok, saida = _cmd("git", "add", *mudaram)
        if not ok:
            return False, saida
        ok, saida = _cmd("git", "commit", "-m", "viagens: atualizadas pelo painel")
        if not ok:
            return False, saida
    ok, saida = baixar_novidades()
    if not ok:
        return False, saida
    ok, saida = _cmd("git", "push")
    if not ok:
        return False, saida
    return True, "Publicado." if mudaram else "Não havia mudanças nas viagens; os preços novos foram baixados."


def execucoes(limite: int = 8) -> list[dict]:
    ok, saida = _cmd("gh", "run", "list", "--repo", REPO, "--limit", str(limite), "--json",
                     "databaseId,status,conclusion,createdAt,event,url,displayTitle", timeout=30)
    return json.loads(saida) if ok and saida.startswith("[") else []


def tela_publicar() -> None:
    st.title("☁️ Salvar e publicar")
    st.success("Tudo o que você muda no painel já fica **salvo no Mac** na hora.")
    if not (RAIZ / ".git").exists():
        st.info("O projeto ainda não está ligado ao GitHub.")
        return
    pend = _pendentes()
    if pend:
        st.warning("Você tem mudanças **ainda não enviadas** ao GitHub (viagens ou pausas). "
                   "O monitor no GitHub só vê as mudanças depois de publicar.")
    else:
        st.caption("Nenhuma mudança de viagens esperando para ser enviada.")
    c1, c2 = st.columns(2)
    if c1.button("☁️ Publicar no GitHub", type="primary", disabled=not pend,
                 help="Criptografa suas viagens, envia ao GitHub e já baixa os preços novos."):
        with st.spinner("Publicando…"):
            limpar_auxiliares()
            ok, msg = publicar()
        (st.success if ok else st.error)(msg if ok else "Não consegui publicar. Mostre isto ao Claude:\n\n" + msg)
    if c2.button("⬇️ Baixar preços novos do GitHub",
                 help="Traz o histórico mais recente (criptografado) e abre aqui no Mac para os gráficos."):
        with st.spinner("Baixando…"):
            ok, msg = baixar_novidades()
        (st.success if ok else st.error)(msg if ok else "Não consegui baixar. Mostre isto ao Claude:\n\n" + msg)

    st.subheader("Buscar agora no GitHub")
    st.caption("Não precisa esperar a próxima rodada automática (a cada 2h). Os preços entram no histórico e, "
               "se baterem as regras, chega aviso no Telegram. Depois de ~10 min, use ⬇️ Baixar preços novos.")
    so_sk = st.toggle("Só Skiplagged (não gasta a cota do Google)", value=True)
    if st.button("▶️ Buscar agora"):
        args = ["gh", "workflow", "run", "monitor.yml", "--repo", REPO]
        if so_sk:
            args += ["-f", "fontes=skiplagged"]
        ok, saida = _cmd(*args, timeout=30)
        (st.success if ok else st.error)("Pedido enviado. Acompanhe abaixo em Últimas execuções." if ok else saida)

    st.subheader("Últimas execuções no GitHub")
    runs = execucoes()
    if not runs:
        st.caption("Não consegui consultar o GitHub agora (ou ainda não houve execuções).")
        return
    nomes = {"completed": "terminou", "in_progress": "rodando", "queued": "na fila"}
    resultados = {"success": "✅ ok", "failure": "❌ falhou", "cancelled": "⏹️ parada", "": ""}
    tabela = pd.DataFrame([{
        "Quando": pd.Timestamp(r["createdAt"]).tz_convert(FUSO).tz_localize(None),
        "Tipo": "automática" if r["event"] == "schedule" else "manual",
        "Situação": nomes.get(r["status"], r["status"]),
        "Resultado": resultados.get(r.get("conclusion") or "", r.get("conclusion")),
        "Detalhes": r["url"],
    } for r in runs])
    st.dataframe(tabela, hide_index=True, use_container_width=True, column_config={
        "Quando": st.column_config.DatetimeColumn(format="DD/MM HH:mm"),
        "Detalhes": st.column_config.LinkColumn(display_text="abrir no GitHub")})


# ----------------------------------------------------------------- navegação
st.set_page_config(page_title="Monitor de Passagens", page_icon="✈️", layout="wide")
PAGINAS = {
    "viagens": st.Page(tela_viagens, title="Minhas viagens", icon="✈️", default=True),
    "historico": st.Page(tela_historico, title="Preços ao longo do tempo", icon="📈", url_path="historico"),
    "analise": st.Page(tela_analise, title="Melhor dia e horário", icon="🗓️", url_path="analise"),
    "buscas": st.Page(tela_buscas, title="Histórico de buscas", icon="🔎", url_path="buscas"),
    "parar": st.Page(tela_parar, title="Pausar e parar", icon="⏸️", url_path="parar"),
    "testar": st.Page(tela_testar, title="Testar uma viagem", icon="🧪", url_path="testar"),
    "publicar": st.Page(tela_publicar, title="Salvar e publicar", icon="☁️", url_path="publicar"),
}
def fechar_painel() -> None:
    """Encerra o painel (o servidor roda em segundo plano, sem janela do Terminal)."""
    import signal
    import threading
    threading.Timer(1.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()


with st.sidebar:
    st.caption("Monitor de Passagens · as mudanças ficam salvas no Mac na hora.")
    if (RAIZ / ".git").exists() and _pendentes():
        st.warning("Há mudanças não publicadas.")
        st.page_link(PAGINAS["publicar"], label="Publicar agora", icon="☁️")
    if st.button("⏻ Fechar painel", help="Desliga o painel. Para abrir de novo, use o atalho \"Abrir painel\"."):
        fechar_painel()
        st.success("Painel fechado. Pode fechar esta aba do navegador.")
        st.stop()
    if os.getenv("MONITOR_DADOS"):
        st.caption(f"Dados lidos de: {DADOS}")
st.navigation(list(PAGINAS.values())).run()
