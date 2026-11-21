"""Monitor de passagens aéreas.

Uso:
  python -m monitor                      # execução normal (busca, grava histórico, notifica)
  python -m monitor --simular            # busca e mostra os alertas, mas NÃO notifica nem grava
  python -m monitor --listar             # mostra as buscas geradas pelo rotas.yaml
  python -m monitor --testar-notificacao # manda uma mensagem de teste p/ Telegram/WhatsApp
  python -m monitor --rota rec-gru --fontes google_flights   # filtra rota/fonte
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from . import notificacao
from .config import CONFIG_PADRAO, DADOS_PADRAO, SITES_PADRAO, carregar, consultas_da_rota
from .fontes import FonteBloqueada, criar_fonte
from .historico import Estado, Historico, agora
from .models import Consulta, Oferta, Rota
from .regras import avaliar, brl, deve_reavisar

log = logging.getLogger("monitor")

#: variáveis com chaves: nunca aparecem nos registros (viram ***)
SEGREDOS = ("SERPAPI_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "CALLMEBOT_PHONE", "CALLMEBOT_APIKEY",
            "MONITOR_CHAVE")


def sem_segredos(texto: str) -> str:
    for nome in SEGREDOS:
        valor = os.getenv(nome)
        if valor and len(valor) >= 6:
            texto = texto.replace(valor, "***")
    return texto


class _FiltroSegredos(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = sem_segredos(record.getMessage()), None
        return True


def _erro_curto(e: Exception) -> str:
    """Erro sem endereço nem parâmetros (que podem ter a rota): só o tipo e o código HTTP."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    return f"{type(e).__name__}" + (f" (HTTP {status})" if status else "")


def privado() -> bool:
    """No GitHub (repositório público) os registros não mostram rotas, datas nem preços."""
    return os.getenv("MONITOR_PRIVADO") == "1"


def _carregar_env_local() -> None:
    """Lê um arquivo .env na raiz (uso local no Mac). No GitHub Actions usam-se os Secrets."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def mensagem(oferta: Oferta, motivos: list[str]) -> str:
    c = oferta.consulta
    onde = f"{oferta.fonte} ({oferta.vendedor})" if oferta.vendedor else oferta.fonte
    linhas = [
        "✈️ Passagem mais barata encontrada",
        c.descricao(),
        f"💰 {brl(oferta.preco)} — via {onde}",
    ]
    if oferta.ida:
        linhas.append(f"🛫 Ida: {oferta.ida.resumo()}")
    if oferta.volta:
        linhas.append(f"🛬 Volta: {oferta.volta.resumo()}")
    if oferta.companhia:
        linhas.append(f"Cia: {oferta.companhia}")
    if oferta.nivel_preco:
        linhas.append(f"📊 Preço {oferta.nivel_preco} segundo o Google")
    if oferta.detalhes:
        linhas.append(oferta.detalhes)
    linhas += [f"⚠️ {a}" for a in oferta.avisos]
    linhas.append("Motivo: " + "; ".join(motivos))
    if oferta.link:
        linhas.append(oferta.link)
    return "\n".join(linhas)


def executar(args: argparse.Namespace) -> int:
    cfg = carregar(Path(args.config), Path(args.sites))
    dados = Path(args.dados)
    historico, estado = Historico(dados), Estado(dados)
    inicio = agora()

    # com --rota, a rota pedida roda mesmo com `ativa: false` (para testar antes de ligar)
    rotas: dict[str, Rota] = {r.id: r for r in cfg.rotas if (r.id in args.rota if args.rota else r.ativa)}
    for faltando in set(args.rota or []) - set(rotas):
        log.warning("Rota '%s' não existe em rotas.yaml.", "?" if privado() else faltando)
    consultas_por_fonte: dict[str, list[Consulta]] = defaultdict(list)
    for rota in rotas.values():
        for fonte in rota.fontes:
            if args.fontes and fonte not in args.fontes:
                continue
            consultas_por_fonte[fonte].extend(consultas_da_rota(rota, cfg.geral.moeda))

    if args.listar:
        for fonte, cs in consultas_por_fonte.items():
            print(f"\n[{fonte}] {len(cs)} busca(s)")
            for c in ([] if privado() else cs):
                print("  ", c.descricao())
        return 0

    if not consultas_por_fonte:
        log.warning("Nenhuma busca a fazer (rotas inativas ou todas as datas já passaram).")
        return 0

    ofertas: list[Oferta] = []
    resumo: dict[str, dict[str, int]] = {}
    tentativas = falhas = 0

    for nome_fonte, consultas in consultas_por_fonte.items():
        r = resumo.setdefault(nome_fonte, {"buscas": 0, "com_preco": 0, "falhas": 0, "puladas": 0})
        if nome_fonte in cfg.fontes_desligadas:
            log.warning("Fonte %s desligada no painel (config/pausas.yaml).", nome_fonte)
            r["puladas"] = len(consultas)
            continue
        pausa = estado.pausada_ate(nome_fonte)
        if pausa and pausa > inicio:
            log.warning("Fonte %s descansando após bloqueio até %s UTC.", nome_fonte, f"{pausa:%d/%m %H:%M}")
            r["puladas"] = len(consultas)
            continue
        try:
            fonte = criar_fonte(nome_fonte, cfg, dados / "debug", inicio, estado.ultima_rodada(nome_fonte))
        except ValueError as e:
            log.error("%s", e)
            r["puladas"] = len(consultas)
            continue
        ok, motivo = fonte.disponivel()
        if not ok:
            log.warning("Fonte %s indisponível: %s", nome_fonte, motivo)
            r["puladas"] = len(consultas)
            continue

        # rodízio: as buscas há mais tempo sem atualização vão primeiro
        consultas = sorted(consultas, key=lambda c: estado.ultima_consulta(nome_fonte, c.chave))
        if fonte.limite_por_execucao is not None and len(consultas) > fonte.limite_por_execucao:
            r["puladas"] = len(consultas) - fonte.limite_por_execucao
            consultas = consultas[: fonte.limite_por_execucao]

        if consultas and not args.simular:
            estado.marcar_rodada(nome_fonte, inicio)
        bloqueios_seguidos = 0
        try:
            for i, c in enumerate(consultas, 1):
                desc = f"busca {i}/{len(consultas)}" if privado() else c.descricao()
                tentativas += 1
                r["buscas"] += 1
                try:
                    achadas = fonte.buscar(c)
                    bloqueios_seguidos = 0
                except FonteBloqueada as e:
                    falhas += 1
                    r["falhas"] += 1
                    bloqueios_seguidos += 1
                    log.warning("%s", e)
                    if bloqueios_seguidos >= 2:
                        horas = cfg.geral.pausa_apos_bloqueio_horas
                        log.warning("%s bloqueou 2 vezes seguidas; descansa %gh.", nome_fonte, horas)
                        if not args.simular:
                            estado.pausar(nome_fonte, inicio + timedelta(hours=horas))
                        break
                    continue
                except Exception as e:  # noqa: BLE001 — uma busca falhar não para as outras
                    falhas += 1
                    r["falhas"] += 1
                    log.error("Erro em %s para %s: %s", nome_fonte, desc, _erro_curto(e) if privado() else e)
                    continue
                if not args.simular:
                    estado.marcar_consulta(nome_fonte, c.chave, inicio)
                if achadas:
                    r["com_preco"] += 1
                    melhor = min(achadas, key=lambda o: o.preco)
                    ofertas.append(melhor)
                    log.info("%-14s %s → %s", nome_fonte, desc, "com preço" if privado() else brl(melhor.preco))
                else:
                    log.info("%-14s %s → sem preço", nome_fonte, desc)
                if fonte.usa_navegador:
                    time.sleep(cfg.geral.pausa_navegador_s)
                elif getattr(fonte, "pausa_s", None) and c is not consultas[-1]:
                    time.sleep(random.uniform(*fonte.pausa_s))
        finally:
            fonte.fechar()

    # melhor preço por busca (entre todas as fontes) → regras de alerta
    melhores: dict[str, Oferta] = {}
    for o in ofertas:
        atual = melhores.get(o.consulta.chave)
        if atual is None or o.preco < atual.preco:
            melhores[o.consulta.chave] = o

    alertas: list[tuple[Oferta, list[str]]] = []
    for chave, o in melhores.items():
        motivos = avaliar(o, historico.precos(chave), rotas[o.consulta.rota_id], cfg.geral, inicio)
        if motivos and deve_reavisar(estado.ultimo_alerta(chave), o.preco, cfg.geral, inicio):
            alertas.append((o, motivos))
    alertas.sort(key=lambda x: x[0].preco)
    alertas = alertas[: cfg.geral.max_alertas_por_execucao]

    for o, motivos in alertas:
        texto = mensagem(o, motivos)
        if args.simular:
            print("\n--- ALERTA (simulação, não enviado) ---\n" + ("(oculto no modo privado)" if privado() else texto))
            continue
        envio = notificacao.enviar(texto)
        if any(v == "ok" for v in envio.values()):
            estado.registrar_alerta(o.consulta.chave, o.preco, inicio)

    if not args.simular:
        historico.registrar(ofertas, inicio)
        estado.salvar()
        (dados / "ultima_execucao.json").write_text(json.dumps({
            "data_hora_utc": inicio.isoformat(), "fontes": resumo,
            "precos_registrados": len(ofertas), "alertas": len(alertas),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    _resumo_github(resumo, melhores, alertas)
    log.info("Fim: %d preço(s), %d alerta(s), %d falha(s) em %d busca(s).",
             len(ofertas), len(alertas), falhas, tentativas)
    # falha o job só se TUDO falhou (o GitHub avisa por e-mail quando o job falha)
    return 1 if tentativas and falhas == tentativas else 0


def _resumo_github(resumo: dict, melhores: dict[str, Oferta], alertas: list) -> None:
    destino = os.getenv("GITHUB_STEP_SUMMARY")
    if not destino:
        return
    linhas = ["## Monitor de passagens", "", "| Fonte | Buscas | Com preço | Falhas | Puladas |",
              "|---|---|---|---|---|"]
    for f, r in resumo.items():
        linhas.append(f"| {f} | {r['buscas']} | {r['com_preco']} | {r['falhas']} | {r['puladas']} |")
    if privado():
        melhores = {}  # repositório público: sem rotas nem preços no resumo
    if melhores:
        linhas += ["", "| Busca | Melhor preço | Fonte |", "|---|---|---|"]
    for o in sorted(melhores.values(), key=lambda o: (o.consulta.rota_id, o.consulta.ida)):
        linhas.append(f"| {o.consulta.descricao()} | {brl(o.preco)} | {o.fonte} |")
    linhas += ["", f"**Alertas enviados:** {len(alertas)}"]
    with open(destino, "a", encoding="utf-8") as f:
        f.write("\n".join(linhas) + "\n")


def precisa_navegador(args: argparse.Namespace) -> int:
    """Código 0 se alguma rota ativa usa site de navegador (o workflow só instala o Chromium nesse caso)."""
    cfg = carregar(Path(args.config), Path(args.sites))
    usados = {f for r in cfg.rotas if r.ativa for f in r.fontes} - set(cfg.fontes_desligadas)
    return 0 if usados & set(cfg.sites) else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="monitor", description="Monitor de preços de passagens aéreas")
    p.add_argument("--config", default=str(CONFIG_PADRAO))
    p.add_argument("--sites", default=str(SITES_PADRAO))
    p.add_argument("--dados", default=str(DADOS_PADRAO))
    p.add_argument("--rota", nargs="*", help="só estas rotas (ids)")
    p.add_argument("--fontes", nargs="*", help="só estas fontes")
    p.add_argument("--simular", action="store_true", help="não notifica nem grava nada")
    p.add_argument("--listar", action="store_true", help="lista as buscas e sai")
    p.add_argument("--testar-notificacao", action="store_true")
    p.add_argument("--precisa-navegador", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for h in logging.getLogger().handlers:
        h.addFilter(_FiltroSegredos())
    _carregar_env_local()

    if args.precisa_navegador:
        return precisa_navegador(args)
    if args.testar_notificacao:
        res = notificacao.enviar("✅ Teste do monitor de passagens: notificações funcionando.")
        print(sem_segredos(str(res)) if res else "Nenhum canal configurado.")
        return 0 if res and all(v == "ok" for v in res.values()) else 1
    return executar(args)


if __name__ == "__main__":
    sys.exit(main())
