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

from monitor.aeroportos import mesma_cidade, rotulo, todos as aeroportos  # noqa: E402
from monitor.calendario import MESES, _quando, calendario_html, cidade_curta  # noqa: E402
from monitor.config import CONFIG_PADRAO, Geral, carregar, expandir_datas, intervalo_min, ler_pausas  # noqa: E402
from monitor.models import DIAS  # noqa: E402
from monitor.regras import brl  # noqa: E402
from monitor.roteiro import Cenario, conflito, opcoes  # noqa: E402
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
PARTES_SKIPLAGGED = 6


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
            # 6 computadores por rodada + repescagem (ver .github/workflows/monitor.yml), até N datas cada
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
    try:
        df = pd.read_csv(caminho, dtype=str, keep_default_na=False)
    except pd.errors.EmptyDataError:  # arquivo vazio (0 bytes)
        return pd.DataFrame()
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
def _limitar(valor, minimo: int, maximo: int) -> int:
    """Valor salvo fora dos limites do formulário (ex.: queda de 90%) não pode derrubar a tela."""
    try:
        return min(max(int(round(float(valor))), minimo), maximo)
    except (TypeError, ValueError):
        return minimo


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
                                value=_limitar(spec.get("a_cada_dias", 1), 1, 30) if isinstance(spec, dict) else 1,
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
    with c2:
        sem_ref = st.checkbox("Ainda não sei um valor de referência", key=k + "_semref",
                              value=not v.get("preco_alvo") and not nova,
                              help="Marque se você ainda não sabe quanto essa passagem costuma custar.")
        if sem_ref:
            alvo = 0
            st.caption("Sem problema: você será avisado quando o preço **cair** (novo menor preço ou queda abaixo "
                       "da média) e recebe o resumo diário. Quando tiver uma ideia de valor, volte aqui.")
        else:
            alvo = st.number_input("Me avise se ficar abaixo de (R$)", 1, 100_000, step=50, key=k + "_alvo",
                                   value=_limitar(v.get("preco_alvo") or 1000, 1, 100_000),
                                   help="Basta UMA regra para avisar. Na ida e volta, é o preço total.")
    queda = c3.number_input("…ou se cair este % abaixo da média", 5, 80, key=k + "_queda",
                            value=_limitar(v.get("queda_pct") or g.queda_pct_padrao, 5, 80))
    referencia_preco(v.get("id"))
    with st.expander("Como funcionam os avisos?"):
        st.markdown(REGRAS_TEXTO)
    fontes = escolher_fontes(v.get("fontes") or ["google_flights", "skiplagged"], k)

    st.markdown("**🔗 Ligação com outras viagens** (opcional)")
    outras = [x["id"] for x in lista if x.get("id") != v.get("id")]
    nomes_o = {x["id"]: nome_viagem(x) for x in lista}
    atuais_dep = v.get("depois_de") or []
    atuais_dep = [atuais_dep] if isinstance(atuais_dep, str) else atuais_dep
    depois_de = st.multiselect("Esta viagem acontece depois de **qualquer uma** destas", outras, key=k + "_dep",
                               default=[x for x in atuais_dep if x in outras], format_func=lambda i: nomes_o.get(i, i),
                               placeholder="(nenhuma)",
                               help="Ex.: Rio→Tampa depois de REC→Galeão OU REC→Santos Dumont (vale a que você "
                                    "comprar). O monitor avisa quando o voo mais barato de um dia não encaixa, e "
                                    "monta os roteiros possíveis no Calendário.")
    int_min = int_max = None
    if depois_de:
        st.caption("Intervalo entre a **chegada** daquela viagem e a **saída** desta:")
        int_min, int_max = campo_intervalo(k + "_int", intervalo_min(v),
                                           float(v["intervalo_max_horas"]) if v.get("intervalo_max_horas") else None)

    ids = {x["id"] for x in lista if x.get("id") != v.get("id")}
    nova_v = {
        "id": v.get("id") or criar_id(nome or f"{origem}-{destino}", ids),
        "nome": nome.strip(), "ativa": v.get("ativa", True), "origem": origem, "destino": destino,
        "ida": ida, "volta": volta, "duracao_dias": duracao, "adultos": int(adultos),
        "preco_alvo": int(alvo) or None, "queda_pct": int(queda), "fontes": fontes,
        "depois_de": depois_de or None, "intervalo_min_horas": int_min if depois_de else None,
        "intervalo_max_horas": int_max if depois_de else None,
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
    ligadas = [x for x in lista if id_ in (x.get("depois_de") if isinstance(x.get("depois_de"), list)
                                           else [x.get("depois_de")])]
    st.write(f"Excluir **{nome_viagem(v)}**? Os preços já guardados dela continuam no histórico.")
    if ligadas:
        st.warning("Estas viagens estão ligadas a ela e vão perder essa ligação: "
                   + ", ".join(nome_viagem(x) for x in ligadas))
    if st.button("Sim, excluir", type="primary"):
        restantes = []
        for x in lista:
            if x["id"] == id_:
                continue
            deps = x.get("depois_de")
            if deps:
                deps = [d for d in (deps if isinstance(deps, list) else [deps]) if d != id_]
                x["depois_de"] = deps or None
                if not deps:
                    x["intervalo_min_horas"] = x["intervalo_max_horas"] = None
            restantes.append(x)
        if gravar(restantes, "Viagem excluída"):
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
        st.session_state[chave] = not st.session_state[chave]  # não salvou: o botão volta para onde estava


# ----------------------------------------------------------------- telas
def mostrar_precos(df: pd.DataFrame, id_: str) -> None:
    """Menor preço das últimas 24h e menor já visto, no cartão da viagem (só a configuração atual)."""
    d = df[df["rota_id"] == id_] if not df.empty else df
    if not d.empty:
        v = next((x for x in viagens() if x["id"] == id_), None)
        if v:
            try:
                d = d[d["chave"].isin({c.chave for c in consultas(v)})]
            except Exception:  # noqa: BLE001
                pass
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

    def datas_da(v: dict) -> list[date]:
        try:
            return [c.ida for c in consultas(v)]
        except Exception:  # noqa: BLE001
            return []
    meses_v = sorted({(d.year, d.month) for v in lista for d in datas_da(v)})
    c1, c2 = st.columns(2)
    ordem = c1.selectbox("Ordenar por", ["Data da viagem (mais cedo primeiro)", "Data da viagem (mais tarde primeiro)",
                                         "Nome", "Ordem em que cadastrei"], key="viagens_ordem")
    filtro = c2.multiselect("Mostrar só as viagens de", meses_v, key="viagens_meses", placeholder="Todos os meses",
                            format_func=lambda m: f"{MESES[m[1] - 1]} de {m[0]}")
    mostrar = [v for v in lista if not filtro or any((d.year, d.month) in filtro for d in datas_da(v))]
    if ordem.startswith("Data"):
        mostrar.sort(key=lambda v: min(datas_da(v), default=date.max), reverse="tarde" in ordem)
    elif ordem == "Nome":
        mostrar.sort(key=lambda v: nome_viagem(v).lower())
    if filtro:
        st.caption(f"Mostrando {len(mostrar)} de {len(lista)} viagens.")

    df = historico()
    for v in mostrar:
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
                if v.get("depois_de"):
                    from monitor.roteiro import _intervalo_txt
                    deps = v["depois_de"] if isinstance(v["depois_de"], list) else [v["depois_de"]]
                    quais = " ou ".join(next((nome_viagem(x) for x in lista if x["id"] == d), d) for d in deps)
                    st.caption(f"🔗 Depois de **{quais}**, com "
                               + _intervalo_txt(intervalo_min(v),
                                                float(v["intervalo_max_horas"]) if v.get("intervalo_max_horas") else None))
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
                    st.session_state["testar_sel"] = v["id"]
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


def _rotas_obj(lista: list[dict]) -> list:
    from monitor.config import _rota
    saida = []
    for v in lista:
        try:
            saida.append(_rota(v, Geral()))
        except Exception:  # noqa: BLE001
            pass
    return saida


def _mostrar_cenario(cen: Cenario, nomes: dict) -> None:
    for a, prox in zip(cen.trechos, cen.trechos[1:] + [None]):
        onde = NOMES_FONTES.get(a.fonte, a.fonte) + (f" ({a.vendedor})" if a.vendedor else "")
        st.markdown(f"✈️ **{a.origem} → {a.destino}** ({nomes.get(a.rota_id, a.rota_id)}) · sai {_quando(a.partida)} · "
                    f"chega {_quando(a.chegada)} · **{brl(a.preco)}** · {onde}"
                    + (f" · [abrir]({a.link})" if a.link else ""))
        if prox:
            horas = (prox.partida - a.chegada).total_seconds() / 3600
            tempo = f"{horas / 24:.1f} dias".replace(".", ",") if horas >= 24 else f"{horas:.0f}h"
            st.caption(f"📍 Em {cidade_curta(a.destino)}: de {_quando(a.chegada)} a {_quando(prox.partida)} ({tempo})")
    st.caption(f"Total: **{brl(cen.total)}**. Preços da última coleta de cada data; podem ter mudado. "
               "Bilhetes separados: se um voo atrasar, o seguinte não é protegido.")


def _escolher_cenario(cs: list[Cenario], chave: str) -> Cenario:
    def rotulo_c(i: int) -> str:
        c = cs[i]
        return f"{i + 1}º · {brl(c.total)} · termina em {cidade_curta(c.trechos[-1].destino)} · " + " | ".join(
            f"{o.origem}→{o.destino} {o.partida:%d/%m %H:%M}" for o in c.trechos)
    i = st.radio("Escolha um para ver no calendário", range(len(cs)), format_func=rotulo_c, key=chave)
    return cs[i]


def ligacoes_impossiveis(rotas: list, voos: dict, nomes: dict) -> list[str]:
    from monitor.roteiro import _intervalo_txt, _usaveis, combinar
    saida = []
    por = {r.id: r for r in rotas if r.ativa}
    for r in por.values():
        for a in r.depois_de:
            va, vb = _usaveis(voos.get(a, {})), _usaveis(voos.get(r.id, {}))
            if a not in por or not va or not vb:
                continue
            if combinar([va, vb], [(r.intervalo_min_horas, r.intervalo_max_horas)], 1):
                continue
            chegada, saida_b = min(o.chegada for o in va), max(o.partida for o in vb)
            if saida_b <= chegada:
                motivo = (f"as datas de **{nomes.get(r.id)}** ({min(o.data for o in vb):%d/%m/%Y} a "
                          f"{max(o.data for o in vb):%d/%m/%Y}) são antes da chegada de **{nomes.get(a)}** "
                          f"({chegada:%d/%m/%Y}). Será que a data foi escolhida errada?")
            else:
                motivo = (f"nenhum voo de **{nomes.get(r.id)}** sai {_intervalo_txt(r.intervalo_min_horas, r.intervalo_max_horas)} "
                          f"depois de **{nomes.get(a)}** chegar")
                if r.intervalo_max_horas and r.intervalo_max_horas < 24:
                    motivo += f" (o máximo está em {r.intervalo_max_horas:g} **horas**; era para ser dias?)"
            saida.append(motivo)
    return saida


def dados_atuais(df: pd.DataFrame, lista: list[dict]) -> tuple[dict, dict]:
    """Preços (mais barato por data) e voos guardados, só nas datas que as viagens ativas têm AGORA.

    O histórico guarda também datas antigas (de antes de você mudar uma viagem): elas ficam de fora.
    """
    from monitor.roteiro import ler_voos, todos_os_voos
    chaves = chaves_atuais(lista)
    op = opcoes(r for r in df.to_dict("records") if r.get("chave") in chaves)
    voos = todos_os_voos(op, ler_voos(DADOS / "voos.json"))
    voos = {r: {d: [o for o in l if o.chave in chaves] for d, l in dias.items()} for r, dias in voos.items()}
    voos = {r: {d: l for d, l in dias.items() if l} for r, dias in voos.items()}
    return {r: d for r, d in op.items() if d}, {r: d for r, d in voos.items() if d}


def chaves_atuais(lista: list[dict]) -> set[str]:
    """As buscas exatas que as viagens ativas fazem AGORA (datas, volta, adultos). Preços de configurações
    antigas (ex.: antes de mudar a data ou o nº de adultos) ficam de fora."""
    chaves: set[str] = set()
    for v in lista:
        if v.get("ativa", True):
            try:
                chaves |= {c.chave for c in consultas(v)}
            except Exception:  # noqa: BLE001
                pass
    return chaves


def filtro_meses(meses: list[tuple[int, int]], chave: str) -> list[tuple[int, int]]:
    """Escolher quais meses mostrar (um ou vários). Padrão: a partir do mês atual."""
    hoje = date.today()
    padrao = [m for m in meses if m >= (hoje.year, hoje.month)] or meses
    escolha = st.multiselect("Meses", meses, default=padrao, key=chave, placeholder="Escolha os meses",
                             format_func=lambda m: f"{MESES[m[1] - 1]} de {m[0]}")
    return sorted(escolha)


_calendario_clicavel = None


def calendario_clicavel(html_cal: str, selecionados: list[str], chave: str) -> list[str]:
    global _calendario_clicavel
    if _calendario_clicavel is None:
        import streamlit.components.v1 as components
        _calendario_clicavel = components.declare_component(
            "calendario_clicavel", path=str(Path(__file__).resolve().parent / "componentes" / "calendario"))
    valor = _calendario_clicavel(html=html_cal, selecionados=selecionados, key=chave, default=None)
    return selecionados if valor is None else list(valor)


ARQ_SIMULACOES = Path(__file__).resolve().parent / "simulacoes.json"  # só no Mac (fora do Git)


def _sim_ler() -> dict:
    try:
        d = json.loads(ARQ_SIMULACOES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    return {"atual": d.get("atual") or {"marcados": [], "voos": {}}, "salvas": d.get("salvas") or {}}


def _sim_gravar(d: dict) -> None:
    ARQ_SIMULACOES.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def _sim_carregar(sim: dict) -> None:
    """Põe uma simulação (marcados + voo escolhido em cada um) na tela; os voos entram na próxima rodada."""
    for k in [k for k in st.session_state if str(k).startswith("sim_voo_")]:
        del st.session_state[k]
    st.session_state["sim_marcados"] = list(sim.get("marcados") or [])
    st.session_state["sim_pendente_voos"] = dict(sim.get("voos") or {})


def abrir_no_simulador(cen: Cenario) -> None:
    _sim_carregar({"marcados": [f"{o.rota_id}|{o.data.isoformat()}" for o in cen.trechos],
                   "voos": {f"{o.rota_id}|{o.data.isoformat()}": o.partida.isoformat() for o in cen.trechos}})
    st.session_state["sim_carregado"] = True
    st.switch_page(PAGINAS["simulador"])


def problemas_da_simulacao(escolhidos: list, rotas_lig: dict, nomes: dict) -> dict:
    """{(viagem, data): (tipo, motivo)} dos voos marcados com problema.

    tipo "sobrepoe": sai antes de o voo anterior chegar (choque de verdade).
    tipo "ligacao": não respeita a ligação que você marcou na viagem (falta a anterior ou intervalo fora).
    """
    from monitor.roteiro import _intervalo_txt, encaixa

    def tempo(h: float) -> str:
        return f"{h / 24:.1f} dias".replace(".", ",") if h >= 24 else f"{h:.0f}h"
    saida: dict = {}
    anterior, feitos = None, []
    for _x, r, d, _l, o in escolhidos:
        rotulo_v = f"{o.origem}→{o.destino} {d:%d/%m}"
        if anterior and anterior.chegada and o.partida and o.partida < anterior.chegada:
            saida[(r, d)] = ("sobrepoe", f"{rotulo_v} sai {o.partida:%d/%m %H:%M}, antes de {anterior.origem}→"
                                         f"{anterior.destino} chegar ({anterior.chegada:%d/%m %H:%M})")
        lig = rotas_lig.get(r)
        if (r, d) not in saida and lig and lig.depois_de:
            antes = [p for p in feitos if p.rota_id in lig.depois_de]
            quais = " ou ".join(nomes.get(a, a) for a in lig.depois_de)
            regra = _intervalo_txt(lig.intervalo_min_horas, lig.intervalo_max_horas)
            if not antes:
                saida[(r, d)] = ("ligacao", f"{rotulo_v}: na ligação que você marcou, antes dele vem {quais}, e "
                                            "nenhuma delas está marcada")
            elif not any(encaixa(p, o, lig.intervalo_min_horas, lig.intervalo_max_horas) for p in antes):
                p = antes[-1]
                h = (o.partida - p.chegada).total_seconds() / 3600 if p.chegada and o.partida else 0
                saida[(r, d)] = ("ligacao", f"{rotulo_v}: a ligação que você marcou pede {regra} depois da chegada de "
                                            f"{p.origem}→{p.destino} ({p.chegada:%d/%m %H:%M}); aqui são {tempo(h)}. "
                                            "Não é choque de horário: se quiser, ajuste o intervalo no ✏️ da viagem")
        anterior = o
        feitos.append(o)
    return saida


def _diferenca(valor: float) -> str:
    """'-R$ 267,21' / '+R$ 50,00' (o sinal na frente faz a seta do st.metric apontar certo)."""
    return ("-" if valor < 0 else "+") + brl(abs(valor))


def mostrar_problemas(problemas: dict, escolhidos: list, chave_ok: str = "") -> None:
    sobre = [m for t, m in problemas.values() if t == "sobrepoe"]
    fora = [m for t, m in problemas.values() if t == "ligacao"]
    if sobre:
        st.error("❌ **Choque de horário:**  \n" + "  \n".join(sobre))
    if fora:
        st.warning("⚠️ **Fora das ligações que você marcou:**  \n" + "  \n".join(fora))
    if not problemas and escolhidos:
        st.success(f"{len(escolhidos)} voo(s) marcado(s): nenhum choque de horário e todos dentro das suas ligações.")


def tela_simulador() -> None:
    from monitor.aeroportos import mesma_cidade
    st.title("🧮 Simular viagem")
    st.caption("O mesmo calendário de \"Calendário e roteiros\": **clique num voo para marcar** (fica azul) e "
               "clique de novo para desmarcar. Embaixo aparecem as rotas na ordem, o valor total e se algum voo se "
               "sobrepõe. O fundo colorido mostra onde você estaria. Nada aqui muda suas viagens nem os avisos.")
    df = historico()
    if df.empty:
        sem_dados()
        return
    lista = viagens()
    nomes = {v["id"]: nome_viagem(v) for v in lista}
    op, voos = dados_atuais(df, lista)
    if not op:
        sem_dados()
        return
    rotas_lig = {r.id: r for r in _rotas_obj(lista)}
    arquivo = _sim_ler()
    if not st.session_state.get("sim_carregado"):  # 1ª vez: volta a última simulação
        _sim_carregar(arquivo["atual"])
        st.session_state["sim_carregado"] = True
    marcados = [x for x in st.session_state.get("sim_marcados", [])
                if x.split("|")[0] in op and date.fromisoformat(x.split("|")[1]) in op[x.split("|")[0]]]

    # voo escolhido em cada marcação (padrão: o mais barato do dia)
    def opcoes_do_dia(r: str, d: date) -> list:
        return sorted(voos.get(r, {}).get(d) or [op[r][d]], key=lambda o: o.preco)

    pendentes = st.session_state.pop("sim_pendente_voos", {}) or {}
    escolhidos = []
    for x in marcados:
        r, d = x.split("|")[0], date.fromisoformat(x.split("|")[1])
        lista_o = opcoes_do_dia(r, d)
        if x in pendentes:  # voo escolhido numa simulação salva / no roteiro: acha o mesmo horário
            alvo = pendentes[x]
            st.session_state[f"sim_voo_{x}"] = next(
                (j for j, o in enumerate(lista_o) if o.partida and o.partida.isoformat() == alvo), 0)
        i = st.session_state.get(f"sim_voo_{x}", 0)
        escolhidos.append((x, r, d, lista_o, lista_o[min(i, len(lista_o) - 1)]))
    escolhidos.sort(key=lambda e: (e[4].partida or datetime.combine(e[2], datetime.min.time())))
    com_hora = [e[4] for e in escolhidos if e[4].partida and e[4].chegada]
    cen = Cenario(com_hora) if com_hora else None
    problemas_sel = problemas_da_simulacao(escolhidos, rotas_lig, nomes)

    c1, c2 = st.columns([4, 1])
    with c1:
        meses = sorted({(d.year, d.month) for dias in op.values() for d in dias})
        meses_sel = filtro_meses(meses, "sim_meses")
    with c2:
        st.write("")
        if st.button("🧹 Limpar seleção"):
            _sim_carregar({})
            st.rerun()
    with st.expander("💾 Simulações salvas"):
        c1, c2 = st.columns([3, 1])
        nome_sim = c1.text_input("Nome para guardar a simulação atual", placeholder="Ex.: via Porto, volta 01/03")
        if c2.button("Guardar", disabled=not (nome_sim.strip() and marcados)):
            arquivo["salvas"][nome_sim.strip()] = {
                "marcados": marcados, "voos": {e[0]: e[4].partida.isoformat() for e in escolhidos if e[4].partida},
                "total": round(sum(e[4].preco for e in escolhidos), 2), "quando": datetime.now().isoformat(timespec="minutes")}
            _sim_gravar(arquivo)
            st.toast(f"Guardada: {nome_sim.strip()}", icon="💾")
        if arquivo["salvas"]:
            nomes_s = list(arquivo["salvas"])
            esc = st.selectbox("Guardadas", nomes_s, format_func=lambda n: f"{n} · {brl(arquivo['salvas'][n].get('total', 0))} "
                               f"(guardada em {arquivo['salvas'][n].get('quando', '')[:16].replace('T', ' ')})")
            b1, b2, _ = st.columns([1, 1, 3])
            if b1.button("Abrir"):
                _sim_carregar(arquivo["salvas"][esc])
                st.rerun()
            if b2.button("Apagar"):
                arquivo["salvas"].pop(esc, None)
                _sim_gravar(arquivo)
                st.rerun()
            st.caption("O total mostrado é o da hora em que foi guardada; ao abrir, os preços são os mais recentes.")
    # aviso na hora: logo acima do calendário, sem precisar rolar até o fim
    if escolhidos:
        c1, c2 = st.columns([1, 3])
        c1.metric("Total marcado", brl(sum(e[4].preco for e in escolhidos)))
        with c2:
            mostrar_problemas(problemas_sel, escolhidos)
    html_cal = "".join(f"<h4>{MESES[m - 1].capitalize()} de {a}</h4>"
                       + calendario_html(a, m, op, list(op), cen, nomes, voos, rotas_lig, clicavel=True,
                                         problemas=problemas_sel)
                       for a, m in meses_sel)
    novos = calendario_clicavel(html_cal, marcados, "sim_cal")
    if sorted(novos) != sorted(marcados):
        st.session_state["sim_marcados"] = novos
        st.rerun()

    # guarda a simulação atual no Mac (volta igual quando você abrir o painel de novo)
    atual = {"marcados": marcados, "voos": {e[0]: e[4].partida.isoformat() for e in escolhidos if e[4].partida}}
    if atual != arquivo["atual"]:
        arquivo["atual"] = atual
        _sim_gravar(arquivo)

    st.subheader("Sua viagem simulada")
    if not escolhidos:
        st.info("Clique nos voos do calendário acima para montar a viagem.")
        return
    from monitor.roteiro import _intervalo_txt, encaixa
    total, anterior, problemas, fora_regra = 0.0, None, 0, 0
    feitos: list = []
    for x, r, d, lista_o, _ in escolhidos:
        with st.container(border=True):
            fmt = lambda o: (f"sai {o.partida:%d/%m %H:%M} · chega {o.chegada:%d/%m %H:%M} · " if o.partida and o.chegada else "") + \
                f"{brl(o.preco)} · {NOMES_FONTES.get(o.fonte, o.fonte)}" + (f" ({o.vendedor})" if o.vendedor else "")
            st.markdown(f"✈️ **{lista_o[0].origem} → {lista_o[0].destino}** · {nomes.get(r, r)} · {d:%d/%m/%Y}")
            i = st.selectbox("Voo", range(len(lista_o)), format_func=lambda i: fmt(lista_o[i]), key=f"sim_voo_{x}",
                             help="Todos os voos guardados desse dia (o mais barato vem primeiro).")
            o = lista_o[i]
            if o.link:
                st.markdown(f"[abrir no site]({o.link})")
            if anterior and anterior.chegada and o.partida:
                horas = (o.partida - anterior.chegada).total_seconds() / 3600
                if horas < 0:
                    st.error(f"❌ Sobrepõe: sai {o.partida:%d/%m %H:%M}, antes de o voo anterior chegar "
                             f"({anterior.chegada:%d/%m %H:%M}).")
                    problemas += 1
                else:
                    tempo = f"{horas / 24:.1f} dias".replace(".", ",") if horas >= 24 else f"{horas:.0f}h"
                    st.caption(f"⏱️ {tempo} depois da chegada do voo anterior ({cidade_curta(anterior.destino)}).")
                    if horas < 3:
                        st.warning("Menos de 3h entre os voos: apertado, principalmente com bilhetes separados.")
                if not mesma_cidade(anterior.destino, o.origem):
                    if cidade_curta(anterior.destino) == cidade_curta(o.origem):
                        st.warning(f"O voo anterior chega no aeroporto {anterior.destino} e este sai de outro aeroporto, "
                                   f"{o.origem} ({cidade_curta(o.origem)}): conte o tempo de deslocamento entre eles.")
                    else:
                        st.warning(f"O voo anterior chega em {cidade_curta(anterior.destino)} e este sai de "
                                   f"{cidade_curta(o.origem)}: você precisa se deslocar entre as cidades.")
            # as ligações que você marcou nas viagens (🔗 "depois de qualquer uma destas")
            prob = problemas_sel.get((r, d))
            if prob and prob[0] == "ligacao":
                st.warning("⚠️ " + prob[1])
                fora_regra += 1
            total += o.preco
            anterior = o
            feitos.append(o)
    st.metric("Valor total", brl(total), help="Soma dos voos escolhidos (preços da última coleta; podem ter mudado).")
    if problemas:
        st.error(f"{problemas} voo(s) se sobrepõem. Troque a data ou o horário marcado em vermelho.")
    else:
        st.success("Nenhum voo se sobrepõe.")
    if fora_regra:
        st.warning(f"{fora_regra} trecho(s) fora das ligações que você marcou nas viagens (não é choque de horário). "
                   "Por isso esta simulação "
                   "pode sair mais barata que os \"Roteiros possíveis\", que só usam combinações dentro das ligações.")


def tela_calendario() -> None:
    from monitor.roteiro import combinar, ler_voos, roteiros_pelas_ligacoes, todos_os_voos
    st.title("📅 Calendário e roteiros")
    df = historico()
    if df.empty:
        sem_dados()
        return
    lista = viagens()
    nomes = {v["id"]: nome_viagem(v) for v in lista}
    op, voos = dados_atuais(df, lista)
    rotas = [r for r in _rotas_obj(lista) if r.ativa]
    rotas_lig = {r.id: r for r in rotas}
    ids = [v["id"] for v in lista if v["id"] in op]
    if not ids:
        sem_dados()
        return

    cen = None
    with st.container(border=True):
        st.subheader("🧭 Roteiros possíveis")
        st.caption("Montados pelas **ligações** que você marca em cada viagem (✏️ → 🔗 \"Esta viagem acontece depois "
                   "de…\"). Cada roteiro escolhe **um voo de cada trecho**, entre todos os voos guardados de cada dia "
                   "(não só o mais barato), respeitando os intervalos de cada ligação. Os mais baratos aparecem primeiro.")
        if not any(r.depois_de for r in rotas):
            st.info("Você ainda não marcou ligações. Exemplo: em **Rio→Tampa**, marque \"depois de REC→Galeão ou "
                    "REC→Santos Dumont\", com intervalo de 1 a 5 dias. Aí os roteiros aparecem aqui sozinhos.")
        else:
            cs, sem_voos = roteiros_pelas_ligacoes(rotas, voos, limite=10)
            if sem_voos:
                st.caption("Ainda sem voos com horário: " + ", ".join(nomes.get(i, i) for i in sem_voos) +
                           " (esses caminhos ficam de fora por enquanto).")
            fins = {cidade_curta(c.trechos[-1].destino) for c in cs}
            if len(fins) > 1:
                st.caption("⚠️ Os roteiros terminam em lugares diferentes (" + ", ".join(sorted(fins)) + "): um "
                           "roteiro com menos trechos parece mais barato. Para comparar de igual para igual, "
                           "cadastre também a volta de cada lugar (ex.: Paris→Recife) ligada à chegada lá.")
            if not cs:
                st.warning("Nenhuma combinação de datas e horários respeita os intervalos das ligações.")
            else:
                cen = _escolher_cenario(cs, "cen_lig")
                _mostrar_cenario(cen, nomes)
                if st.button("🧮 Abrir este roteiro no simulador", key="abrir_sim_lig"):
                    abrir_no_simulador(cen)

    problemas = ligacoes_impossiveis(rotas, voos, nomes)
    if problemas:
        with st.container(border=True):
            st.markdown("**⚠️ Ligações que nunca encaixam** (com os voos encontrados até agora)")
            for p in problemas:
                st.markdown(f"- {p}")
            st.caption("Corrija em Minhas viagens (✏️ → 🔗). Enquanto isso, esses caminhos ficam fora dos roteiros.")

    with st.expander("Montar um roteiro à mão (sem usar as ligações)"):
        so_ida = [i for i in ids if any(o.volta is None for o in op[i].values())]
        trechos = st.multiselect("Trechos, na ordem da viagem", so_ida, placeholder="Escolha 2 ou mais",
                                 format_func=lambda i: nomes.get(i, i))
        if len(trechos) >= 2:
            intervalos = []
            for a, b in zip(trechos, trechos[1:]):
                st.markdown(f"**Entre {nomes.get(a, a)} e {nomes.get(b, b)}**")
                intervalos.append(campo_intervalo(f"man_{a}_{b}", 3, None))
            pernas = [[o for d in voos.get(t, {}).values() for o in d if o.partida and o.chegada and o.volta is None]
                      for t in trechos]
            cs_m = combinar(pernas, intervalos, limite=10)
            if not cs_m:
                st.warning("Nenhuma combinação respeita esses intervalos.")
            else:
                cen = _escolher_cenario(cs_m, "cen_man")
                _mostrar_cenario(cen, nomes)

    st.subheader("Calendário")
    st.caption("Cada dia mostra as viagens com saída naquele dia e o voo mais barato daquela data. "
               "**★ verde** = o mais barato do dia entre as rotas para o mesmo destino, ou entre as que saem da "
               "mesma origem. **Clique num voo** para tirar ou pôr no roteiro (azul = no roteiro).")

    # roteiro editável: começa com o escolhido acima; você pode desmarcar um voo e marcar outro
    sugeridos = [f"{o.rota_id}|{o.data.isoformat()}" for o in cen.trechos] if cen else []
    assinatura = tuple(f"{o.rota_id}|{o.data.isoformat()}|{o.partida.isoformat()}" for o in cen.trechos) if cen else ()
    if st.session_state.get("cal_assinatura") != assinatura:
        st.session_state["cal_assinatura"] = assinatura
        st.session_state["cal_marcados"] = list(sugeridos)
        st.session_state["cal_voos"] = {f"{o.rota_id}|{o.data.isoformat()}": o.partida.isoformat()
                                        for o in cen.trechos} if cen else {}
    marcados = [x for x in st.session_state.get("cal_marcados", [])
                if x.split("|")[0] in op and date.fromisoformat(x.split("|")[1]) in op[x.split("|")[0]]]
    preferidos = st.session_state.get("cal_voos", {})
    escolhidos = []
    for x in marcados:
        r, d = x.split("|")[0], date.fromisoformat(x.split("|")[1])
        lista_o = sorted(voos.get(r, {}).get(d) or [op[r][d]], key=lambda o: o.preco)
        o = next((o for o in lista_o if o.partida and o.partida.isoformat() == preferidos.get(x)), lista_o[0])
        escolhidos.append((x, r, d, lista_o, o))
    escolhidos.sort(key=lambda e: (e[4].partida or datetime.combine(e[2], datetime.min.time())))
    com_hora = [e[4] for e in escolhidos if e[4].partida and e[4].chegada]
    cen_ed = Cenario(com_hora) if com_hora else None
    problemas = problemas_da_simulacao(escolhidos, rotas_lig, nomes)

    if sorted(marcados) != sorted(sugeridos):
        with st.container(border=True):
            st.markdown("**✏️ Você alterou o roteiro**")
            c1, c2 = st.columns([1, 3])
            c1.metric("Total", brl(sum(e[4].preco for e in escolhidos)),
                      delta=_diferenca(sum(e[4].preco for e in escolhidos) - cen.total) if cen else None,
                      delta_color="inverse",
                      help="A diferença é em relação ao roteiro escolhido acima.")
            with c2:
                mostrar_problemas(problemas, escolhidos)
            c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
            nome_g = c1.text_input("Nome para guardar", placeholder="Ex.: via Paris, volta 01/03", key="cal_nome")
            if c2.button("💾 Guardar", disabled=not (nome_g.strip() and marcados), key="cal_guardar"):
                arquivo = _sim_ler()
                arquivo["salvas"][nome_g.strip()] = {
                    "marcados": marcados, "voos": {e[0]: e[4].partida.isoformat() for e in escolhidos if e[4].partida},
                    "total": round(sum(e[4].preco for e in escolhidos), 2),
                    "quando": datetime.now().isoformat(timespec="minutes")}
                _sim_gravar(arquivo)
                st.toast(f"Guardado: {nome_g.strip()} (em Simular viagem → Simulações salvas)", icon="💾")
            if c3.button("↩️ Voltar", key="cal_voltar", help="Volta ao roteiro escolhido acima"):
                st.session_state["cal_assinatura"] = None
                st.rerun()
            if c4.button("🧮 Detalhar", key="cal_detalhar", help="Abre no simulador para escolher o horário de cada voo"):
                _sim_carregar({"marcados": marcados,
                               "voos": {e[0]: e[4].partida.isoformat() for e in escolhidos if e[4].partida}})
                st.session_state["sim_carregado"] = True
                st.switch_page(PAGINAS["simulador"])

    sel = st.multiselect("Viagens no calendário", ids, default=ids, format_func=lambda i: nomes.get(i, i))
    meses = sorted({(d.year, d.month) for r in sel for d in op[r]} |
                   ({(o.partida.year, o.partida.month) for o in cen_ed.trechos} if cen_ed else set()))
    if not meses:
        st.info("Escolha ao menos uma viagem.")
        return
    html_cal = "".join(f"<h4>{MESES[m - 1].capitalize()} de {a}</h4>"
                       + calendario_html(a, m, op, sel, cen_ed, nomes, voos, rotas_lig, clicavel=True,
                                         problemas=problemas)
                       for a, m in filtro_meses(meses, "cal_meses"))
    novos = calendario_clicavel(html_cal, marcados, "cal_clicavel")
    if sorted(novos) != sorted(marcados):
        st.session_state["cal_marcados"] = novos
        st.rerun()
    st.caption("Azul ✈️ = voos do roteiro. ❌ vermelho = choque de horário. ⚠️ laranja = fora das ligações que você "
               "marcou. O fundo mostra em que cidade você estaria em cada dia.")


def campo_intervalo(chave: str, min_h: float, max_h: float | None) -> tuple[float, float | None]:
    """Intervalo mínimo (e máximo opcional) em horas ou dias. Devolve (mín, máx) em horas."""
    def em_dias(h: float) -> bool:
        return h >= 24 and h % 24 == 0
    c1, c2, c3, c4 = st.columns([2, 1, 2, 1])
    vmin = c1.number_input("No mínimo", 0, 9999, _limitar(min_h // 24 if em_dias(min_h) else min_h, 0, 9999), key=chave + "_min")
    umin = c2.radio("em", ["horas", "dias"], index=1 if em_dias(min_h) else 0, key=chave + "_umin", horizontal=False)
    vmax = c3.number_input("No máximo (0 = sem limite)", 0, 9999,
                           _limitar((max_h // 24 if em_dias(max_h) else max_h) if max_h else 0, 0, 9999), key=chave + "_max")
    umax = c4.radio("em ", ["horas", "dias"], index=1 if (max_h and em_dias(max_h)) else 0, key=chave + "_umax",
                    horizontal=False)
    mn = float(vmin) * (24 if umin == "dias" else 1)
    mx = float(vmax) * (24 if umax == "dias" else 1) if vmax else None
    if mx is not None and mx < mn:
        st.warning("O máximo está menor que o mínimo.")
    return mn, mx


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
    if st.session_state.get("testar_sel") not in ids:
        st.session_state["testar_sel"] = ids[0]
    sel = st.selectbox("Viagem", ids, key="testar_sel",
                       format_func=lambda i: nome_viagem(next(v for v in lista if v["id"] == i)))
    v = next(x for x in lista if x["id"] == sel)
    fontes = st.multiselect("Fontes", v.get("fontes") or [], default=v.get("fontes") or [], key=f"testar_fontes_{sel}",
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


def _env() -> dict:
    from dotenv import dotenv_values
    return dotenv_values(RAIZ / ".env")


def _gravar_env(nome: str, valor: str) -> None:
    arq = RAIZ / ".env"
    linhas = arq.read_text(encoding="utf-8").splitlines()
    novas = [f"{nome}={valor}" if l.strip().startswith(f"{nome}=") else l for l in linhas]
    if not any(l.strip().startswith(f"{nome}=") for l in linhas):
        novas.append(f"{nome}={valor}")
    arq.write_text("\n".join(novas) + "\n", encoding="utf-8")


def tela_avisos() -> None:
    from monitor.notificacao import conversas_do_bot
    st.title("📣 Onde chegam os avisos")
    st.caption("Os avisos e o resumo diário vão para todas as conversas marcadas aqui: seu privado e grupos.")
    env = _env()
    token, atuais = env.get("TELEGRAM_BOT_TOKEN"), [c.strip() for c in (env.get("TELEGRAM_CHAT_ID") or "").split(",") if c.strip()]
    if not token:
        st.error("Falta o TELEGRAM_BOT_TOKEN no .env.")
        return
    st.markdown("**Para incluir um grupo:** no Telegram, adicione o bot ao grupo e mande qualquer mensagem lá. "
                "Depois clique em **Procurar conversas do bot** (o Telegram só mostra o que aconteceu nas "
                "últimas ~24h).")
    if st.button("🔎 Procurar conversas do bot") or "conversas" not in st.session_state:
        try:
            st.session_state["conversas"] = conversas_do_bot(token)
        except Exception as e:  # noqa: BLE001
            st.error(f"Não consegui falar com o Telegram ({type(e).__name__}).")
            st.session_state["conversas"] = {}
    achadas = dict(st.session_state.get("conversas") or {})
    for c in atuais:  # as que já recebem avisos aparecem mesmo que o Telegram não as mostre agora
        achadas.setdefault(c, {"tipo": "?", "nome": "(conversa já cadastrada)", "saiu": False})
    tipos = {"private": "👤 privado", "group": "👥 grupo", "supergroup": "👥 grupo", "channel": "📢 canal"}
    escolhidas = []
    for cid, info in achadas.items():
        rotulo_c = f"{tipos.get(info['tipo'], '💬')} · {info['nome']}" + (" — o bot saiu deste grupo" if info["saiu"] else "")
        if st.checkbox(rotulo_c, value=cid in atuais and not info["saiu"], key=f"chat_{cid}", disabled=info["saiu"]):
            escolhidas.append(cid)
    if sorted(escolhidas) != sorted(atuais):
        st.warning("Você mudou as conversas. Clique em salvar para valer.")
        if st.button("💾 Salvar e enviar ao GitHub", type="primary", disabled=not escolhidas):
            valor = ",".join(escolhidas)
            _gravar_env("TELEGRAM_CHAT_ID", valor)
            ok, saida = True, ""
            if (RAIZ / ".git").exists():
                try:
                    r = subprocess.run(["gh", "secret", "set", "TELEGRAM_CHAT_ID", "--repo", REPO], input=valor,
                                       text=True, capture_output=True, cwd=RAIZ, timeout=60)
                    ok, saida = r.returncode == 0, r.stderr[:300]
                except (OSError, subprocess.TimeoutExpired) as e:
                    ok, saida = False, str(e)
            (st.success if ok else st.error)("Salvo no Mac e no GitHub." if ok else f"Salvo no Mac, mas não no GitHub: {saida}")
    if st.button("✉️ Mandar mensagem de teste para as conversas salvas"):
        from monitor import notificacao
        os.environ.update({"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": _env().get("TELEGRAM_CHAT_ID") or ""})
        res = notificacao.enviar("✅ Teste do Monitor de Passagens: este lugar vai receber os avisos.")
        (st.success if res.get("telegram") == "ok" else st.error)(
            "Mensagem enviada." if res.get("telegram") == "ok" else "Falhou. Peça ajuda ao Claude.")


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
    rodando = [r for r in execucoes(5) if r["status"] in ("in_progress", "queued", "waiting", "pending")]
    if rodando:
        st.info("Já tem uma busca rodando no GitHub. Espere ela terminar (acompanhe em Últimas execuções) "
                "antes de pedir outra.")
    if st.button("▶️ Buscar agora", disabled=bool(rodando)):
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
    "calendario": st.Page(tela_calendario, title="Calendário e roteiros", icon="📅", url_path="calendario"),
    "simulador": st.Page(tela_simulador, title="Simular viagem", icon="🧮", url_path="simular"),
    "historico": st.Page(tela_historico, title="Preços ao longo do tempo", icon="📈", url_path="historico"),
    "analise": st.Page(tela_analise, title="Melhor dia e horário", icon="🗓️", url_path="analise"),
    "buscas": st.Page(tela_buscas, title="Histórico de buscas", icon="🔎", url_path="buscas"),
    "parar": st.Page(tela_parar, title="Pausar e parar", icon="⏸️", url_path="parar"),
    "avisos": st.Page(tela_avisos, title="Onde chegam os avisos", icon="📣", url_path="avisos"),
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
