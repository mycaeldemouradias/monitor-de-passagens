"""Testes da lógica (sem rede). Rode com:  PYTHONPATH=src pytest -q"""
from datetime import date, datetime, timedelta, timezone

import pytest

from monitor import __main__ as cli
from monitor.config import Geral, consultas_da_rota, expandir_datas
from monitor.fontes.navegador import extrair_precos, montar_url
from monitor.models import Consulta, Oferta, Rota
from monitor.regras import avaliar, brl, deve_reavisar

AGORA = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
G = Geral()


def _rota(**kw):
    base = dict(id="r", origem="REC", destino="GRU", idas=[date(2027, 3, 10)])
    base.update(kw)
    return Rota(**base)


def _oferta(preco):
    return Oferta(Consulta("r", "REC", "GRU", date(2027, 3, 10)), "teste", preco)


def test_expandir_datas():
    assert expandir_datas("2027-01-01") == [date(2027, 1, 1)]
    assert len(expandir_datas({"de": "2027-01-01", "ate": "2027-01-05"})) == 5
    assert expandir_datas({"de": "2027-01-01", "ate": "2027-01-07", "a_cada_dias": 3}) == [
        date(2027, 1, 1), date(2027, 1, 4), date(2027, 1, 7)]
    with pytest.raises(ValueError):
        expandir_datas({"de": "2027-01-05", "ate": "2027-01-01"})


def test_consultas_combina_e_ignora_passado():
    r = _rota(idas=[date(2026, 11, 1), date(2027, 3, 10), date(2027, 3, 11)],
              voltas=[date(2027, 3, 10), date(2027, 3, 17)])
    cs = consultas_da_rota(r, "BRL", hoje=date(2026, 11, 20))
    # ida passada some; volta antes da ida some
    assert {(c.ida, c.volta) for c in cs} == {
        (date(2027, 3, 10), date(2027, 3, 10)), (date(2027, 3, 10), date(2027, 3, 17)),
        (date(2027, 3, 11), date(2027, 3, 17))}
    r2 = _rota(duracoes=[7])
    assert consultas_da_rota(r2, "BRL", hoje=date(2026, 11, 20))[0].volta == date(2027, 3, 17)


def test_regra_preco_alvo_vale_ja_na_primeira_leitura():
    assert avaliar(_oferta(800), [], _rota(preco_alvo=900), G, AGORA)
    assert not avaliar(_oferta(950), [], _rota(preco_alvo=900), G, AGORA)


def test_regra_menor_ja_visto_exige_observacoes():
    hist = [(AGORA - timedelta(days=1), 1000.0), (AGORA - timedelta(days=2), 1100.0)]
    assert not avaliar(_oferta(990), hist, _rota(), G, AGORA)  # só 2 leituras
    hist.append((AGORA - timedelta(days=3), 1050.0))
    motivos = avaliar(_oferta(990), hist, _rota(queda_pct=50), G, AGORA)
    assert any("menor preço já visto" in m for m in motivos)


def test_regra_queda_percentual():
    hist = [(AGORA - timedelta(days=d), 1000.0) for d in (1, 2, 3)]
    motivos = avaliar(_oferta(840), hist, _rota(queda_pct=15), G, AGORA)
    assert any("abaixo da média" in m for m in motivos)
    motivos = avaliar(_oferta(900), hist, _rota(queda_pct=15), G, AGORA)
    assert not any("abaixo da média" in m for m in motivos)
    # leituras fora da janela não contam para a média
    velho = [(AGORA - timedelta(days=40), 1000.0)] * 3
    assert not any("média" in m for m in avaliar(_oferta(500), velho, _rota(), G, AGORA))


def test_deduplicacao():
    ult = {"preco": 800.0, "data_hora_utc": (AGORA - timedelta(hours=5)).isoformat()}
    assert not deve_reavisar(ult, 800.0, G, AGORA)
    assert deve_reavisar(ult, 780.0, G, AGORA)
    ult["data_hora_utc"] = (AGORA - timedelta(hours=80)).isoformat()
    assert deve_reavisar(ult, 800.0, G, AGORA)


def test_extrair_precos_e_url():
    texto = "A partir de R$ 1.234,56 | taxa R$ 25,00 | R$899 | R$ 12.000"
    assert extrair_precos(texto, 80, 60000) == [1234.56, 899.0, 12000.0]
    # sem ponto de milhar e com espaços extras (antes virava 123 / era ignorado)
    assert extrair_precos("R$1234,56 | R$ 1234 | R$  899", 80, 60000) == [1234.56, 1234.0, 899.0]
    c = Consulta("r", "REC", "GRU", date(2027, 3, 10), date(2027, 3, 17))
    assert montar_url("{origem_min}/{ida_aammdd}/{volta_ddmmaaaa}", c) == "rec/270310/17-03-2027"
    assert brl(1234.5) == "R$ 1.234,50"


def test_execucao_ponta_a_ponta(tmp_path, monkeypatch):
    """Roda o fluxo completo com uma fonte falsa (preços de teste, só neste teste)."""
    cfg = tmp_path / "rotas.yaml"
    cfg.write_text("""
geral: {fontes_padrao: [teste], min_observacoes: 1, resumo_diario_hora: null}
rotas:
  - {id: t1, origem: REC, destino: GRU, ida: "2099-01-10", preco_alvo: 500}
""", encoding="utf-8")
    sites = tmp_path / "sites.yaml"
    sites.write_text("sites: {}", encoding="utf-8")
    precos = iter([600.0, 450.0, 450.0])

    class FonteTeste:
        nome, limite_por_execucao, usa_navegador = "teste", None, False
        def disponivel(self): return True, ""
        def buscar(self, c): return [Oferta(c, "teste", next(precos))]
        def fechar(self): pass

    enviados = []
    monkeypatch.setattr(cli, "criar_fonte", lambda *a, **k: FonteTeste())
    monkeypatch.setattr(cli.notificacao, "enviar", lambda t: enviados.append(t) or {"telegram": "ok"})
    argv = ["--config", str(cfg), "--sites", str(sites), "--dados", str(tmp_path / "data")]

    assert cli.main(argv) == 0 and enviados == []          # 600 > alvo
    assert cli.main(argv) == 0 and len(enviados) == 1      # 450: alvo + menor já visto
    assert "R$ 450,00" in enviados[0]
    assert cli.main(argv) == 0 and len(enviados) == 1      # mesmo preço: não repete
    linhas = (tmp_path / "data" / "historico_precos.csv").read_text().strip().splitlines()
    assert len(linhas) == 4


def test_rota_desligada_roda_quando_pedida_por_id(tmp_path, capsys):
    cfg = tmp_path / "rotas.yaml"
    cfg.write_text("""
rotas:
  - {id: ligada, origem: REC, destino: GRU, ida: "2099-01-10"}
  - {id: desligada, ativa: false, origem: REC, destino: BSB, ida: "2099-01-10"}
""", encoding="utf-8")
    base = ["--config", str(cfg), "--sites", str(tmp_path / "x.yaml"), "--dados", str(tmp_path), "--listar"]
    cli.main(base)
    saida = capsys.readouterr().out
    assert "GRU" in saida and "BSB" not in saida          # sem --rota: só as ativas
    cli.main(base + ["--rota", "desligada"])
    saida = capsys.readouterr().out
    assert "BSB" in saida and "GRU" not in saida          # com --rota: roda mesmo desligada


def test_cota_serpapi_distribuida_no_ciclo():
    """O ciclo da SerpApi vai de uma renovação à outra (ex.: dia 20 a dia 20), não do dia 1º ao 30."""
    from monitor.fontes.google_flights import cota_liberada
    renov = date(2026, 12, 20)                               # ciclo: 20/11 → 20/12 (30 dias)
    inicio = datetime(2026, 11, 20, 0, 30, tzinfo=timezone.utc)
    meio = datetime(2026, 12, 5, 0, 0, tzinfo=timezone.utc)
    fim = datetime(2026, 12, 19, 23, 0, tzinfo=timezone.utc)
    assert cota_liberada(240, 0, renov, 8, inicio) == 2      # 1ª rodada do ciclo: só a fração dela
    assert cota_liberada(240, 100, renov, 8, meio) == 5      # sobra acumulada, mas teto de 5
    assert cota_liberada(240, 120, renov, 8, meio) == 2      # liberado até aqui = 122
    assert cota_liberada(240, 122, renov, 8, meio) == 0      # já usou a metade na metade do ciclo
    assert cota_liberada(240, 238, renov, 8, fim) == 2       # fim do ciclo: só o que resta
    assert cota_liberada(240, 240, renov, 8, fim) == 0
    # dia 1º do mês não zera nada: com 150 usados no início de dezembro, não há cota
    assert cota_liberada(240, 150, renov, 8, datetime(2026, 12, 1, 0, 17, tzinfo=timezone.utc)) == 0
    # simulação do ciclo inteiro com o Google a cada 8h: nunca passa do orçamento
    usados, t = 0, datetime(2026, 11, 20, 0, 17, tzinfo=timezone.utc)
    while t < datetime(2026, 12, 20, tzinfo=timezone.utc):
        usados += cota_liberada(240, usados, renov, 8, t)
        t += timedelta(hours=8)
    assert 230 <= usados <= 240


def test_google_flights_respeita_intervalo(monkeypatch):
    from monitor.fontes import google_flights as gf
    monkeypatch.setenv("SERPAPI_KEY", "chave-de-teste")
    monkeypatch.setattr(gf, "saldo_serpapi", lambda k: {"usados": 0, "cota": 250, "renovacao": date(2026, 12, 20)})
    agora = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
    ok, motivo = gf.GoogleFlights(240, 8, agora - timedelta(hours=3), agora).disponivel()
    assert not ok and "a cada 8h" in motivo                  # rodou há 3h: espera
    fonte = gf.GoogleFlights(240, 8, agora - timedelta(hours=7, minutes=30), agora)
    assert fonte.disponivel()[0] and fonte.limite_por_execucao == 5   # atraso do GitHub tolerado
    monkeypatch.delenv("SERPAPI_KEY")
    assert not gf.GoogleFlights(240, 8, None, agora).disponivel()[0]


def test_skiplagged_usa_so_a_lista_de_ida():
    """Formato observado em 20/11/2026: a lista de volta traz só o preço avulso do trecho."""
    from monitor.fontes.skiplagged import _precos_em_centavos
    dados = {"itineraries": {
        "outbound": [{"flight": "a", "one_way_price": 6627, "min_round_trip_price": 12927},
                     {"flight": "b", "min_round_trip_price": 13500}],
        "inbound": [{"flight": "c", "one_way_price": 6300}],
    }}
    assert sorted(_precos_em_centavos(dados, ida_volta=True)) == [(12927, "a"), (13500, "b")]
    assert _precos_em_centavos(dados, ida_volta=False) == [(6627, "a")]
    assert _precos_em_centavos({}, ida_volta=True) == []


# --- detalhes dos voos (estruturas iguais às observadas nas respostas reais de 20/11/2026) ---

def _seg(cia, num, de, para, saida, chegada):
    return {"airline": cia, "flight_number": num,
            "departure": {"time": saida, "airport": de}, "arrival": {"time": chegada, "airport": para}}


def _dados_skiplagged():
    vend = lambda c: {"FlightNetwork": [1, c], "Kiwi": [1, c + 500]}  # noqa: E731
    dado = lambda *fontes: "x|" + __import__("json").dumps({"source": list(fontes)})  # noqa: E731
    return {
        "info": {"from": {"airports": ["REC"]}, "to": {"airports": ["GRU"]}},
        "airlines": {"AD": {"name": "Azul Linhas Aereas"}},
        "flights": {
            "ida1": {"segments": [_seg("AD", 4153, "REC", "GRU", "2027-03-10T02:25:00-03:00", "2027-03-10T05:40:00-03:00")]},
            "ida2": {"segments": [_seg("LA", 1, "REC", "FOR", "2027-03-10T11:50:00-03:00", "2027-03-10T13:15:00-03:00"),
                                  _seg("LA", 2, "FOR", "GRU", "2027-03-10T13:55:00-03:00", "2027-03-10T17:30:00-03:00"),
                                  _seg("LA", 3, "GRU", "RAO", "2027-03-10T19:00:00-03:00", "2027-03-10T20:00:00-03:00")]},
            "ida3": {"segments": [_seg("G3", 9, "REC", "GRU", "2027-03-10T09:00:00-03:00", "2027-03-10T12:00:00-03:00")]},
            "volta1": {"segments": [_seg("AD", 4232, "GRU", "REC", "2027-03-17T05:00:00-03:00", "2027-03-17T08:00:00-03:00")]},
        },
        "itineraries": {
            "outbound": [
                {"flight": "ida1", "one_way_price": 6627, "min_round_trip_price": 12927,
                 "data": dado({"cost": 12927, "source": [{"cost": 6627, "source": vend(6627)}]}, {"cost": 6627, "source": vend(6627)})},
                {"flight": "ida2", "one_way_price": 6000, "min_round_trip_price": 12300,
                 "data": dado({"cost": 6000, "source": vend(6000)})},
                {"flight": "ida3", "one_way_price": 9000, "min_round_trip_price": 14000,   # bilhete único
                 "data": dado({"cost": 14000, "source": {"Mytrip": [1, 14000]}})},
            ],
            "inbound": [{"flight": "volta1", "one_way_price": 6300,
                         "data": dado({"cost": 6300, "source": {"Kiwi": [1, 6300], "Mytrip": [1, 6551]}})}],
        },
    }


def test_skiplagged_detalhes_ida_e_volta():
    from monitor.fontes.skiplagged import Skiplagged
    c = Consulta("r", "REC", "GRU", date(2027, 3, 10), date(2027, 3, 17))
    ofertas = Skiplagged().interpretar(_dados_skiplagged(), c, cotacao=lambda m: 6.0)
    por_voo = {o.ida.voos[0]: o for o in ofertas}

    o = por_voo["AD 4153"]                                       # ida + volta mais barata = 2 bilhetes
    assert o.preco == 775.62                                     # US$ 129,27 × 6
    assert o.ida.resumo() == "qua 10/03 02:25 → 05:40 · AD 4153 · direto"
    assert o.volta.resumo() == "qua 17/03 05:00 → 08:00 · AD 4232 · direto"
    assert o.vendedor == "ida: FlightNetwork · volta: Kiwi" and o.companhia == "Azul Linhas Aereas"
    assert any("2 bilhetes separados" in a for a in o.avisos)

    h = por_voo["LA 1"]                                          # hidden-city: bilhete vai até RAO
    assert h.ida.desce_em == "GRU" and h.ida.voos == ["LA 1", "LA 2"]
    assert h.ida.conexoes == [("FOR", 40)] and h.ida.chegada.hour == 17
    assert any("cidade escondida na ida: desça em GRU" in a for a in h.avisos)

    u = por_voo["G3 9"]                                          # bilhete único: volta desconhecida
    assert u.volta is None and u.vendedor == "Mytrip"
    assert any("bilhete único" in a for a in u.avisos)


def test_skiplagged_so_ida():
    from monitor.fontes.skiplagged import Skiplagged
    c = Consulta("r", "REC", "GRU", date(2027, 3, 10))
    ofertas = Skiplagged().interpretar(_dados_skiplagged(), c, cotacao=lambda m: 6.0)
    o = min(ofertas, key=lambda o: o.preco)
    assert o.preco == 360.0 and o.volta is None and o.vendedor == "FlightNetwork"
    assert not any("2 bilhetes" in a or "bilhete único" in a for a in o.avisos)
    assert any("cidade escondida" in a for a in o.avisos)       # a mais barata aqui é a hidden-city


def test_google_flights_detalhes():
    from monitor.fontes.google_flights import GoogleFlights
    dados = {
        "price_insights": {"price_level": "low", "typical_price_range": [940, 1700]},
        "best_flights": [{"price": 860, "total_duration": 415, "layovers": [{"duration": 220, "id": "GIG"}],
                          "flights": [
                              {"departure_airport": {"id": "REC", "time": "2027-03-10 06:00"},
                               "arrival_airport": {"id": "GIG", "time": "2027-03-10 09:00"},
                               "airline": "Azul", "flight_number": "AD 1"},
                              {"departure_airport": {"id": "GIG", "time": "2027-03-10 12:40"},
                               "arrival_airport": {"id": "GRU", "time": "2027-03-10 13:55"},
                               "airline": "Azul", "flight_number": "AD 2"}]}],
    }
    c = Consulta("r", "REC", "GRU", date(2027, 3, 10), date(2027, 3, 17))
    o = GoogleFlights.__new__(GoogleFlights).interpretar(dados, c)[0]
    assert o.ida.resumo() == "qua 10/03 06:00 → 13:55 · AD 1, AD 2 · escala em GIG (3h40)"
    assert o.nivel_preco == "baixo (típico R$ 940–1.700)"
    assert o.volta is None and any("volta só no link" in a for a in o.avisos)
    texto = cli.mensagem(o, ["teste"])
    assert "🛫 Ida: qua 10/03 06:00" in texto and "📊 Preço baixo" in texto


def test_historico_grava_detalhes_e_migra_csv_antigo(tmp_path):
    from monitor.fontes.skiplagged import Skiplagged
    from monitor.historico import COLUNAS, Historico
    antigo = tmp_path / "historico_precos.csv"
    antigo.write_text("data_hora_utc,chave,preco\n2026-11-19T10:00:00+00:00,k,500.00\n", encoding="utf-8")
    c = Consulta("r", "REC", "GRU", date(2027, 3, 10), date(2027, 3, 17))
    ofertas = Skiplagged().interpretar(_dados_skiplagged(), c, cotacao=lambda m: 6.0)
    h = Historico(tmp_path)
    h.registrar([o for o in ofertas if o.ida.voos == ["AD 4153"]], AGORA)
    import csv
    linhas = list(csv.DictReader(antigo.open(encoding="utf-8")))
    assert list(linhas[0]) == COLUNAS and linhas[0]["preco"] == "500.00"   # linha antiga preservada
    assert linhas[1]["ida_partida"] == "2027-03-10T02:25" and linhas[1]["volta_voos"] == "AD 4232"  # horário local, sem fuso


def test_fonte_descansa_depois_de_bloqueio(tmp_path, monkeypatch):
    from monitor.fontes import FonteBloqueada
    cfg = tmp_path / "rotas.yaml"
    cfg.write_text("""
geral: {fontes_padrao: [teste], pausa_apos_bloqueio_horas: 6}
rotas:
  - {id: t1, origem: REC, destino: GRU, ida: ["2099-01-10", "2099-01-11", "2099-01-12"]}
""", encoding="utf-8")
    chamadas = []

    class FonteBloqueia:
        nome, limite_por_execucao, usa_navegador = "teste", None, False
        def disponivel(self): return True, ""
        def buscar(self, c): chamadas.append(c); raise FonteBloqueada("captcha")
        def fechar(self): pass

    monkeypatch.setattr(cli, "criar_fonte", lambda *a, **k: FonteBloqueia())
    argv = ["--config", str(cfg), "--sites", str(tmp_path / "x"), "--dados", str(tmp_path / "data")]
    cli.main(argv)
    assert len(chamadas) == 2                     # desiste após 2 bloqueios seguidos
    cli.main(argv)
    assert len(chamadas) == 2                     # na rodada seguinte, a fonte está descansando


def test_skiplagged_espera_resultado_completo(monkeypatch):
    from monitor.fontes import skiplagged as sk
    parcial = {**_dados_skiplagged(), "incomplete": True}
    respostas = iter([parcial, parcial, _dados_skiplagged()])

    class Resp:
        status_code, text = 200, "{}"
        def __init__(self, d): self.d = d
        def raise_for_status(self): pass
        def json(self): return self.d

    pedidos = []
    monkeypatch.setattr(sk.requests, "get", lambda *a, **k: pedidos.append(1) or Resp(next(respostas)))
    monkeypatch.setattr(sk.time, "sleep", lambda s: None)
    monkeypatch.setattr(sk, "cotacao_para_brl", lambda m: 6.0)
    ofertas = sk.Skiplagged().buscar(Consulta("r", "REC", "GRU", date(2027, 3, 10)))
    assert len(pedidos) == 3 and ofertas
    assert not any("parcial" in a for o in ofertas for a in o.avisos)


def test_painel_grava_viagens_sem_perder_comentarios(tmp_path):
    from monitor.config import carregar
    from monitor.viagens import criar_id, ler_viagens, salvar_viagens

    arq = tmp_path / "rotas.yaml"
    arq.write_text("# comentário importante\ngeral:\n  queda_pct_padrao: 20  # explicação\n\n"
                   "rotas:\n  - {id: velha, origem: REC, destino: GRU, ida: '2027-03-10'}\n", encoding="utf-8")
    viagens = ler_viagens(arq)
    viagens.append({"id": criar_id("Férias em SP", {"velha"}), "nome": "Férias em SP", "ativa": False,
                    "origem": "REC", "destino": "CGH", "ida": {"de": "2027-04-01", "ate": "2027-04-03"},
                    "duracao_dias": [7, 10], "adultos": 2, "preco_alvo": 900, "queda_pct": 15,
                    "fontes": ["skiplagged", "kayak"]})
    salvar_viagens(arq, viagens)
    texto = arq.read_text(encoding="utf-8")
    assert texto.startswith("# comentário importante\ngeral:\n  queda_pct_padrao: 20  # explicação\n")
    assert "id: ferias-em-sp" in texto
    cfg = carregar(arq, tmp_path / "nao_existe.yaml")
    nova = cfg.rotas[1]
    assert (nova.ativa, nova.destino, nova.duracoes, nova.adultos) == (False, "CGH", [7, 10], 2)
    assert len(nova.idas) == 3 and cfg.geral.queda_pct_padrao == 20
    assert ler_viagens(arq) == viagens


def test_painel_nao_grava_viagem_invalida(tmp_path):
    from monitor.viagens import salvar_viagens

    arq = tmp_path / "rotas.yaml"
    original = "rotas: []\n"
    arq.write_text(original, encoding="utf-8")
    ruins = [{"id": "a", "origem": "REC", "destino": "REC", "ida": "2027-03-10", "fontes": ["skiplagged"]},
             {"id": "a", "origem": "RECIFE", "destino": "GRU", "ida": "2027-03-10", "fontes": []}]
    with pytest.raises(ValueError) as e:
        salvar_viagens(arq, ruins)
    msg = str(e.value)
    assert "mesmo código" in msg and "iguais" in msg and "3 letras" in msg and "fonte" in msg
    assert arq.read_text(encoding="utf-8") == original


def test_fonte_desligada_no_painel_e_pulada(tmp_path, monkeypatch):
    from monitor.viagens import salvar_pausas

    rotas = tmp_path / "rotas.yaml"
    rotas.write_text("rotas:\n  - {id: r, origem: REC, destino: GRU, ida: '2099-01-10', fontes: [skiplagged]}\n",
                     encoding="utf-8")
    salvar_pausas(tmp_path, ["skiplagged"])
    chamado = []
    monkeypatch.setattr(cli, "criar_fonte", lambda *a, **k: chamado.append(a) or None)
    rc = cli.main(["--config", str(rotas), "--sites", str(tmp_path / "x.yaml"), "--dados", str(tmp_path / "d"),
                   "--simular"])
    assert rc == 0 and chamado == []


def test_aeroporto_precisa_estar_na_lista(tmp_path):
    from monitor.aeroportos import existe, todos
    from monitor.viagens import validar

    lista = todos()
    assert len(lista) > 4000 and "TPA" in lista and "MSP" in lista
    assert list(lista)[0] == "REC"  # favoritos primeiro
    assert existe("GIG") and not existe("QQQ")
    v = {"id": "x", "origem": "GIG", "destino": "QQQ", "ida": "2027-03-10", "fontes": ["skiplagged"]}
    assert any("QQQ" in e and "lista" in e for e in validar([v]))
    assert validar([{**v, "destino": "TPA"}]) == []


def test_cofre_criptografa_e_abre_sem_alteracoes_falsas(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from monitor import cofre
    from monitor.historico import Historico

    monkeypatch.setenv("MONITOR_CHAVE", Fernet.generate_key().decode())
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "rotas.yaml").write_text("rotas: [{id: segredo, origem: GIG, destino: TPA}]\n")
    h = Historico(tmp_path / "data")
    h.registrar([_oferta(1000)], datetime(2026, 11, 20, 10, tzinfo=timezone.utc))
    h.registrar([_oferta(900)], datetime(2026, 11, 21, 10, tzinfo=timezone.utc))
    (tmp_path / "data" / "estado.json").write_text('{"x": 1}')

    mudaram = cofre.fechar(tmp_path)
    assert sorted(mudaram) == ["config/rotas.yaml.cripto", "data/estado.json.cripto",
                               "data/historico/2026-11-20.csv.gz.cripto", "data/historico/2026-11-21.csv.gz.cripto"]
    assert b"segredo" not in (tmp_path / "config" / "rotas.yaml.cripto").read_bytes()
    assert cofre.fechar(tmp_path) == []  # nada mudou → nada regravado

    h.registrar([_oferta(800)], datetime(2026, 11, 21, 12, tzinfo=timezone.utc))
    assert cofre.fechar(tmp_path) == ["data/historico/2026-11-21.csv.gz.cripto"]  # só o dia que mudou

    for nome in ("config/rotas.yaml", "data/estado.json", "data/historico_precos.csv"):
        (tmp_path / nome).unlink()
    cofre.abrir(tmp_path)
    assert "segredo" in (tmp_path / "config" / "rotas.yaml").read_text()
    assert [p for _, p in Historico(tmp_path / "data").precos(_oferta(1).consulta.chave)] == [1000, 900, 800]

    monkeypatch.setenv("MONITOR_CHAVE", Fernet.generate_key().decode())
    with pytest.raises(cofre.ChaveAusente):
        cofre.abrir(tmp_path)


def test_modo_privado_nao_mostra_rotas_nem_chaves(tmp_path, monkeypatch, caplog, capsys):
    import logging

    rotas = tmp_path / "rotas.yaml"
    rotas.write_text("rotas:\n  - {id: r, origem: GIG, destino: TPA, ida: '2099-01-10', preco_alvo: 5000, "
                     "fontes: [skiplagged]}\n", encoding="utf-8")
    monkeypatch.setenv("MONITOR_PRIVADO", "1")
    monkeypatch.setenv("SERPAPI_KEY", "chave-super-secreta-123")

    class Falsa:
        limite_por_execucao, usa_navegador, pausa_s = None, False, None
        def disponivel(self): return True, ""
        def buscar(self, c): raise RuntimeError("falhou em https://x?api_key=chave-super-secreta-123")
        def fechar(self): pass

    monkeypatch.setattr(cli, "criar_fonte", lambda *a, **k: Falsa())
    with caplog.at_level(logging.INFO):
        cli.main(["--config", str(rotas), "--sites", str(tmp_path / "x"), "--dados", str(tmp_path / "d"), "--simular"])
    texto = caplog.text + capsys.readouterr().out
    assert "busca 1/1" in texto
    assert "GIG" not in texto and "TPA" not in texto and "chave-super-secreta" not in texto
    assert cli.sem_segredos("token chave-super-secreta-123 aqui") == "token *** aqui"


def test_cotacao_usa_reserva_quando_a_primeira_recusa(monkeypatch):
    import requests

    from monitor.fontes import base

    class Resp:
        def __init__(self, status, dados): self.status_code, self._d = status, dados
        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"{self.status_code}")
        def json(self): return self._d

    urls = []
    def falso_get(url, timeout, **k):
        urls.append(url)
        if "google" in url or "awesomeapi" in url:
            return Resp(429, {})  # como nos computadores do GitHub
        return Resp(200, {"usd": {"brl": 5.9}})
    monkeypatch.setattr(base.requests, "get", falso_get)
    base.cotacao_para_brl.cache_clear()
    assert base.cotacao_para_brl("USD") == 5.9
    assert base.cotacao_para_brl("USD") == 5.9 and len(urls) == 3  # 2ª vez vem do cache
    base.cotacao_para_brl.cache_clear()


def test_no_github_bloqueio_nao_pausa_a_fonte(tmp_path, monkeypatch):
    from monitor.fontes import FonteBloqueada
    from monitor.historico import Estado

    rotas = tmp_path / "rotas.yaml"
    rotas.write_text("rotas:\n  - {id: r, origem: REC, destino: GRU, ida: {de: '2099-01-10', ate: '2099-01-12'}, "
                     "fontes: [skiplagged]}\n", encoding="utf-8")

    class Bloqueada:
        limite_por_execucao, usa_navegador, pausa_s = None, False, None
        def disponivel(self): return True, ""
        def buscar(self, c): raise FonteBloqueada("403")
        def fechar(self): pass

    monkeypatch.setattr(cli, "criar_fonte", lambda *a, **k: Bloqueada())
    monkeypatch.setattr(cli.notificacao, "enviar", lambda t: {})
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    cli.main(["--config", str(rotas), "--sites", str(tmp_path / "x"), "--dados", str(tmp_path / "d")])
    assert Estado(tmp_path / "d").pausada_ate("skiplagged") is None


def test_resumo_diario_uma_vez_por_dia_a_partir_da_hora(tmp_path):
    from monitor.config import Geral
    from monitor.historico import Estado, Historico
    from monitor.resumo import deve_mandar_resumo, marcar_resumo, montar_resumo

    g, est = Geral(resumo_diario_hora=7), Estado(tmp_path)
    seis_recife = datetime(2026, 11, 21, 9, 17, tzinfo=timezone.utc)   # 06:17 em Recife
    sete_recife = datetime(2026, 11, 21, 10, 17, tzinfo=timezone.utc)  # 07:17 em Recife
    assert not deve_mandar_resumo(est, g, seis_recife)
    assert deve_mandar_resumo(est, g, sete_recife)
    marcar_resumo(est, sete_recife)
    assert not deve_mandar_resumo(est, g, sete_recife + timedelta(hours=4))
    assert deve_mandar_resumo(est, g, sete_recife + timedelta(days=1))

    h = Historico(tmp_path)
    h.registrar([_oferta(700)], sete_recife - timedelta(days=15))   # menor histórico, não deve ser mostrado
    h.registrar([_oferta(650)], sete_recife - timedelta(hours=3))
    h.registrar([_oferta(680)], sete_recife - timedelta(hours=1))
    h.registrar([_oferta(620)], sete_recife)                        # preço desta rodada
    rota = _rota(id="r", nome="Férias", preco_alvo=500)
    texto = montar_resumo(h.csv, [rota, _rota(id="outra")], sete_recife)
    assert "Férias — REC → GRU" in texto and "R$ 620,00" in texto
    assert "R$ 700,00" not in texto and "R$ 650,00" not in texto and "R$ 680,00" not in texto
    assert "menor já visto" not in texto and "abaixo de R$ 500,00" in texto
    assert "outra" in texto and "sem preço retornado nesta rodada" in texto


def test_partes_em_varios_computadores_e_juntar(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("MONITOR_CHAVE", Fernet.generate_key().decode())
    rotas = tmp_path / "rotas.yaml"
    rotas.write_text("geral: {resumo_diario_hora: null}\nrotas:\n  - {id: r, origem: REC, destino: GRU, "
                     "ida: {de: '2099-01-10', ate: '2099-01-14'}, fontes: [skiplagged]}\n", encoding="utf-8")
    buscadas = []

    class Falsa:
        limite_por_execucao, usa_navegador, pausa_s = None, False, None
        def disponivel(self): return True, ""
        def buscar(self, c):
            buscadas.append(c.ida.day)
            return [Oferta(c, "skiplagged", 100.0 + c.ida.day, ida=None)]
        def fechar(self): pass

    monkeypatch.setattr(cli, "criar_fonte", lambda *a, **k: Falsa())
    base = ["--config", str(rotas), "--sites", str(tmp_path / "x")]
    for i in (1, 2):
        assert cli.main(base + ["--dados", str(tmp_path / f"p{i}"), "--parte", f"{i}/2",
                                "--salvar-ofertas", str(tmp_path / f"parte{i}.cripto")]) == 0
    assert sorted(buscadas) == [10, 11, 12, 13, 14]  # cada data em 1 só computador
    assert b"REC" not in (tmp_path / "parte1.cripto").read_bytes()
    enviados = []
    monkeypatch.setattr(cli.notificacao, "enviar", lambda t: enviados.append(t) or {})
    assert cli.main(base + ["--dados", str(tmp_path / "final"), "--fontes", "nenhuma", "--incluir-ofertas",
                            str(tmp_path / "parte1.cripto"), str(tmp_path / "parte2.cripto")]) == 0
    linhas = (tmp_path / "final" / "historico_precos.csv").read_text().strip().splitlines()
    assert len(linhas) == 6 and len(buscadas) == 5


def test_busca_repetida_entre_viagens_e_feita_uma_vez(tmp_path, monkeypatch):
    """Duas viagens com o mesmo trecho e datas que se cruzam: cada data é buscada 1 vez e vale para as duas."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("MONITOR_CHAVE", Fernet.generate_key().decode())
    rotas = tmp_path / "rotas.yaml"
    rotas.write_text("geral: {resumo_diario_hora: null}\nrotas:\n"
                     "  - {id: curta, origem: CDG, destino: LIS, ida: {de: '2099-02-16', ate: '2099-02-18'}, fontes: [skiplagged]}\n"
                     "  - {id: longa, origem: CDG, destino: LIS, ida: {de: '2099-02-15', ate: '2099-02-19'}, fontes: [skiplagged]}\n",
                     encoding="utf-8")
    buscadas = []

    class Falsa:
        limite_por_execucao, usa_navegador, pausa_s = None, False, None
        def disponivel(self): return True, ""
        def buscar(self, c):
            buscadas.append(c.ida.day)
            return [Oferta(c, "skiplagged", 100.0 + c.ida.day, ida=None)]
        def fechar(self): pass

    monkeypatch.setattr(cli, "criar_fonte", lambda *a, **k: Falsa())
    monkeypatch.setattr(cli.notificacao, "enviar", lambda t: {})
    assert cli.main(["--config", str(rotas), "--sites", str(tmp_path / "x"), "--dados", str(tmp_path / "d")]) == 0
    assert sorted(buscadas) == [15, 16, 17, 18, 19]
    linhas = (tmp_path / "d" / "historico_precos.csv").read_text().strip().splitlines()[1:]
    assert sorted(l.split(",")[2] for l in linhas) == ["curta"] * 3 + ["longa"] * 5


def test_telegram_manda_para_todas_as_conversas(monkeypatch):
    from monitor import notificacao

    enviados = []

    class Resp:
        status_code, headers = 200, {"content-type": "application/json"}
        def json(self): return {"ok": True}

    monkeypatch.setattr(notificacao.requests, "post", lambda url, json, timeout: enviados.append(json["chat_id"]) or Resp())
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111, -222")
    monkeypatch.delenv("CALLMEBOT_PHONE", raising=False)
    assert notificacao.enviar("oi") == {"telegram": "ok"}
    assert enviados == ["111", "-222"]


def test_viagem_sem_ligacao_vinda_do_formulario_salva(tmp_path):
    """O formulário manda depois_de/intervalo vazios (None) quando não há ligação."""
    from monitor.viagens import consultas, salvar_viagens, validar

    v = {"id": "x", "nome": "REC-RJ", "origem": "REC", "destino": "GIG", "ida": {"de": "2099-02-02", "ate": "2099-02-04"},
         "adultos": 1, "preco_alvo": None, "queda_pct": 10, "fontes": ["skiplagged"],
         "depois_de": None, "intervalo_min_horas": None, "volta": None, "duracao_dias": None}
    assert validar([v]) == [] and len(consultas(v)) == 3
    arq = tmp_path / "rotas.yaml"
    arq.write_text("rotas: []\n", encoding="utf-8")
    salvar_viagens(arq, [v])
    assert "intervalo_min_horas" not in arq.read_text(encoding="utf-8")


def _voo(rota, o, d, dia, preco, sai, chega):
    from monitor.models import Trecho
    c = Consulta(rota_id=rota, origem=o, destino=d, ida=date(2027, 2, dia))
    return Oferta(c, "skiplagged", preco, ida=Trecho(datetime.fromisoformat(sai), datetime.fromisoformat(chega)))


def test_roteiro_usa_todos_os_voos_do_dia(tmp_path):
    """O voo mais barato do dia não encaixa, mas outro voo do mesmo dia encaixa: o roteiro usa o outro."""
    from monitor.roteiro import combinar, gravar_voos, ler_voos

    agora = datetime(2027, 1, 5, 12, tzinfo=timezone.utc)
    a = [_voo("a", "GIG", "TPA", 9, 1700, "2027-02-09T05:00", "2027-02-09T13:25")]
    b = [_voo("b", "TPA", "MSP", 9, 370, "2027-02-09T06:54", "2027-02-09T17:18"),   # mais barato: sai antes
         _voo("b", "TPA", "MSP", 9, 450, "2027-02-09T18:30", "2027-02-09T23:00")]   # mais caro: encaixa
    arq = tmp_path / "voos.json"
    gravar_voos(arq, {"a|skiplagged": a, "b|skiplagged": b}, agora)
    voos = ler_voos(arq)
    assert len(voos["b"][date(2027, 2, 9)]) == 2
    cs = combinar([voos["a"][date(2027, 2, 9)], voos["b"][date(2027, 2, 9)]], [(3, None)])
    assert len(cs) == 1 and cs[0].total == 2150 and cs[0].trechos[1].partida.hour == 18
    assert combinar([voos["a"][date(2027, 2, 9)], voos["b"][date(2027, 2, 9)]], [(3, 4)]) == []  # máx. 4h
    # datas que já passaram saem do arquivo
    gravar_voos(arq, {}, datetime(2027, 3, 1, tzinfo=timezone.utc))
    assert ler_voos(arq) == {}


def test_ligacao_qualquer_uma_destas_com_minimo_e_maximo(tmp_path):
    from monitor.historico import Historico
    from monitor.resumo import montar_resumo
    from monitor.roteiro import caminhos, conflito, gravar_voos, ler_voos, opcao_de_oferta, roteiros_pelas_ligacoes
    from monitor.viagens import validar

    agora = datetime(2027, 1, 5, 12, tzinfo=timezone.utc)
    voos_lista = {
        "gig|s": [_voo("gig", "REC", "GIG", 2, 546, "2027-02-02T08:20", "2027-02-02T11:15")],
        "sdu|s": [_voo("sdu", "REC", "SDU", 5, 500, "2027-02-05T08:00", "2027-02-05T11:00")],
        "tpa|s": [_voo("tpa", "GIG", "TPA", 7, 1774, "2027-02-07T17:00", "2027-02-09T13:25")],
        "msp9|s": [_voo("msp", "TPA", "MSP", 9, 374, "2027-02-09T06:54", "2027-02-09T17:18")],
        "msp10|s": [_voo("msp", "TPA", "MSP", 10, 469, "2027-02-10T11:47", "2027-02-10T14:18")],
    }
    arq = tmp_path / "voos.json"
    gravar_voos(arq, voos_lista, agora)
    voos = ler_voos(arq)
    rotas = [_rota(id="gig", nome="REC-GIG"), _rota(id="sdu", nome="REC-SDU"),
             # Rio→Tampa depois de REC→GIG OU REC→SDU, entre 1 e 4 dias
             _rota(id="tpa", nome="RJ-Tampa", depois_de=["gig", "sdu"], intervalo_min_horas=24, intervalo_max_horas=96),
             _rota(id="msp", depois_de=["tpa"], intervalo_min_horas=3)]
    assert sorted(caminhos(rotas)) == [["gig", "tpa", "msp"], ["sdu", "tpa", "msp"]]
    cs, sem = roteiros_pelas_ligacoes(rotas, voos)
    # via GIG (02/02 → 07/02 = 5 dias) passa do máximo de 4 dias; via SDU (05/02 → 07/02) encaixa
    assert sem == [] and len(cs) == 1
    assert [o.rota_id for o in cs[0].trechos] == ["sdu", "tpa", "msp"] and cs[0].trechos[2].data.day == 10
    nomes = {"tpa": "RJ-Tampa"}
    msp9 = opcao_de_oferta(voos_lista["msp9|s"][0], agora)
    assert "não encaixa depois de 'RJ-Tampa'" in conflito({"tpa": voos["tpa"]}, msp9, 3, None, nomes)
    assert conflito({"tpa": voos["tpa"]}, opcao_de_oferta(voos_lista["msp10|s"][0], agora), 3, None, nomes) == ""

    h = Historico(tmp_path)
    h.registrar([v for lista in voos_lista.values() for v in lista], agora)
    texto = montar_resumo(h.csv, rotas, agora, arq_voos=arq)
    assert "o mais barato não encaixa depois de 'RJ-Tampa'" in texto
    assert "encaixe mais barato: RJ-Tampa 07/02 + esta 10/02 = R$ 2.243,00" in texto

    base = {"origem": "REC", "destino": "GRU", "ida": "2027-03-10", "fontes": ["skiplagged"]}
    assert any("círculo" in e for e in validar([{**base, "id": "x", "depois_de": ["y"]},
                                                {**base, "id": "y", "depois_de": ["z", "x"]},
                                                {**base, "id": "z"}]))
    assert any("não existe" in e for e in validar([{**base, "id": "x", "depois_de": ["z"]}]))
    assert any("máximo é menor" in e for e in validar([{**base, "id": "z"}, {**base, "id": "x", "depois_de": "z",
                                                       "intervalo_min_horas": 48, "intervalo_max_horas": 24}]))


def test_repescagem_busca_so_o_que_faltou(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("MONITOR_CHAVE", Fernet.generate_key().decode())
    rotas = tmp_path / "rotas.yaml"
    rotas.write_text("geral: {resumo_diario_hora: null}\nrotas:\n  - {id: r, origem: REC, destino: GRU, "
                     "ida: {de: '2099-01-10', ate: '2099-01-13'}, fontes: [skiplagged]}\n", encoding="utf-8")
    from monitor.fontes import FonteBloqueada
    buscadas, bloquear = [], {"sim": True}

    class Falsa:
        limite_por_execucao, usa_navegador, pausa_s = None, False, None
        def disponivel(self): return True, ""
        def buscar(self, c):
            if bloquear["sim"] and c.ida.day in (12, 13):
                raise FonteBloqueada("403")
            buscadas.append(c.ida.day)
            return [Oferta(c, "skiplagged", 100.0)]
        def fechar(self): pass

    monkeypatch.setattr(cli, "criar_fonte", lambda *a, **k: Falsa())
    base = ["--config", str(rotas), "--sites", str(tmp_path / "x"), "--dados", str(tmp_path / "d")]
    (tmp_path / "feitas").mkdir()
    for i in (1, 2):  # parte 2 (dias 11 e 13) e parte 1 (10 e 12): 12 e 13 bloqueados
        cli.main(base + ["--parte", f"{i}/2", "--salvar-ofertas", str(tmp_path / "feitas" / f"parte-{i}.cripto")])
    assert sorted(buscadas) == [10, 11]
    bloquear["sim"] = False
    cli.main(base + ["--so-faltantes", str(tmp_path / "feitas"), "--salvar-ofertas", str(tmp_path / "r.cripto")])
    assert sorted(buscadas) == [10, 11, 12, 13]  # a repescagem fez só 12 e 13


def test_horarios_com_e_sem_fuso_se_misturam(tmp_path):
    """Skiplagged manda '…-03:00'; Google Flights manda sem fuso. O roteiro precisa juntar os dois."""
    import json

    from monitor.roteiro import combinar, ler_voos

    def bloco(rota, o, d, fonte, sai, chega):
        return {"coleta": "2027-01-05T12:00:00+00:00", "rota_id": rota, "origem": o, "destino": d,
                "ida": sai[:10], "volta": None, "fonte": fonte,
                "voos": [{"preco": 100, "vendedor": "", "link": "", "partida": sai, "chegada": chega, "escalas": ""}]}
    arq = tmp_path / "voos.json"
    arq.write_text(json.dumps({
        "a|s": bloco("a", "REC", "GIG", "skiplagged", "2027-02-02T22:05:00-03:00", "2027-02-03T01:00:00-03:00"),
        "b|g": bloco("b", "GIG", "TPA", "google_flights", "2027-02-07T08:20:00", "2027-02-07T20:00:00")}))
    v = ler_voos(arq)
    pernas = [[o for l in v["a"].values() for o in l], [o for l in v["b"].values() for o in l]]
    cs = combinar(pernas, [(3, None)])
    assert len(cs) == 1 and cs[0].trechos[0].partida.hour == 22  # horário local mantido
    from monitor.models import Trecho
    t = Trecho(datetime.fromisoformat("2027-02-02T22:05:00-03:00"), datetime.fromisoformat("2027-02-03T01:00:00"))
    assert t.partida.tzinfo is None and t.chegada - t.partida == timedelta(hours=2, minutes=55)


def test_aviso_nao_repete_se_o_resumo_falhar(tmp_path, monkeypatch):
    rotas = tmp_path / "rotas.yaml"
    rotas.write_text("geral: {resumo_diario_hora: 0}\nrotas:\n  - {id: r, origem: REC, destino: GRU, "
                     "ida: '2099-01-10', preco_alvo: 500, fontes: [skiplagged]}\n", encoding="utf-8")

    class Falsa:
        limite_por_execucao, usa_navegador, pausa_s = None, False, None
        def disponivel(self): return True, ""
        def buscar(self, c): return [Oferta(c, "skiplagged", 400.0)]
        def fechar(self): pass

    enviados = []
    monkeypatch.setattr(cli, "criar_fonte", lambda *a, **k: Falsa())
    monkeypatch.setattr(cli.notificacao, "enviar", lambda t: enviados.append(t) or {"telegram": "ok"})
    monkeypatch.setattr(cli, "montar_resumo", lambda *a, **k: 1 / 0)  # resumo quebra
    argv = ["--config", str(rotas), "--sites", str(tmp_path / "x"), "--dados", str(tmp_path / "d")]
    assert cli.main(argv) == 0 and cli.main(argv) == 0
    assert sum("Passagem mais barata" in t for t in enviados) == 1  # o aviso saiu só uma vez


def test_viagem_pausada_no_meio_da_rodada_nao_derruba(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("MONITOR_CHAVE", Fernet.generate_key().decode())
    rotas = tmp_path / "rotas.yaml"
    rotas.write_text("geral: {resumo_diario_hora: null}\nrotas:\n  - {id: r, origem: REC, destino: GRU, "
                     "ida: '2099-01-10', fontes: [skiplagged]}\n", encoding="utf-8")

    class Falsa:
        limite_por_execucao, usa_navegador, pausa_s = None, False, None
        def disponivel(self): return True, ""
        def buscar(self, c): return [Oferta(c, "skiplagged", 400.0)]
        def fechar(self): pass

    monkeypatch.setattr(cli, "criar_fonte", lambda *a, **k: Falsa())
    base = ["--sites", str(tmp_path / "x"), "--dados", str(tmp_path / "d")]
    cli.main(base + ["--config", str(rotas), "--salvar-ofertas", str(tmp_path / "p.cripto")])
    rotas.write_text(rotas.read_text().replace("fontes: [skiplagged]}", "fontes: [skiplagged], ativa: false}"))
    assert cli.main(base + ["--config", str(rotas), "--fontes", "nenhuma",
                            "--incluir-ofertas", str(tmp_path / "p.cripto")]) == 0


def test_data_que_ja_passou_nao_esconde_conflito():
    from monitor.roteiro import Opcao, conflito, sem_passado

    def op(rota, dia, sai, chega):
        return Opcao(rota, "A", "B", dia, None, 100, "s", "", "", datetime(2026, 1, 1), sai, chega)
    agora = datetime(2027, 1, 5, 12, tzinfo=timezone.utc)
    antes = {"a": {date(2025, 1, 1): [op("a", date(2025, 1, 1), datetime(2025, 1, 1, 8), datetime(2025, 1, 1, 10))],
                   date(2027, 2, 9): [op("a", date(2027, 2, 9), datetime(2027, 2, 9, 8), datetime(2027, 2, 9, 13))]}}
    b = op("b", date(2027, 2, 9), datetime(2027, 2, 9, 6), datetime(2027, 2, 9, 9))  # sai antes de 'a' chegar
    assert conflito(antes, b, 3, None, {}) == ""                      # o voo de 2025 "encaixava"
    assert "não encaixa" in conflito(sem_passado(antes, agora), b, 3, None, {})


def test_pagina_trancada_so_abre_com_a_senha():
    import base64
    import json
    import re

    import pytest
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    from monitor.pagina import trancar

    pagina = trancar("<p>REC → LIS R$ 1.234,56</p>", "senha certa")
    assert "1.234,56" not in pagina
    d = json.loads(re.search(r"const D=(\{.*?\});", pagina).group(1))
    b = {k: base64.b64decode(d[k]) for k in ("sal", "iv", "cifra")}

    def abrir(senha):
        chave = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=b["sal"], iterations=d["it"]).derive(senha.encode())
        return AESGCM(chave).decrypt(b["iv"], b["cifra"], None).decode()

    assert abrir("senha certa") == "<p>REC → LIS R$ 1.234,56</p>"
    with pytest.raises(InvalidTag):
        abrir("senha errada")


def test_maximo_em_dias_conta_dias_completos():
    from monitor.roteiro import Opcao, encaixa

    chega = datetime(2027, 2, 15, 11, 30)
    a = Opcao("a", "MSP", "CDG", date(2027, 2, 13), None, 100, "s", "", "", datetime(2026, 1, 1), chega - timedelta(hours=9), chega)

    def sai(horas):
        p = chega + timedelta(hours=horas)
        return Opcao("b", "BVA", "LIS", p.date(), None, 100, "s", "", "", datetime(2026, 1, 1), p, p + timedelta(hours=2))

    assert encaixa(a, sai(52.6), 24, 48)          # 2,2 dias = "2 dias"
    assert encaixa(a, sai(71.9), 24, 48)          # 2,99 dias ainda é "2 dias"
    assert not encaixa(a, sai(72), 24, 48)        # 3 dias, não
    assert not encaixa(a, sai(23.9), 24, 48)      # mínimo continua exato
    assert encaixa(a, sai(2), 1, 2) and not encaixa(a, sai(2.5), 1, 2)  # em horas: exato


def _trecho_h(origem, destino, sai, chega):
    from monitor.roteiro import Opcao
    return Opcao("r", origem, destino, sai.date(), None, 100, "s", "", "", datetime(2026, 1, 1), sai, chega)


def test_hospedagem_noites_reservas_e_preco():
    from monitor.hospedagem import ConfigHosp, Grupo, chave, custos, noites, reservas, total_hospedagem

    cfg = ConfigHosp(2, ["MSP"], [Grupo("paris", "Paris", ["CDG", "BVA"], ["https://www.airbnb.com.br/rooms/1",
                                                                         "https://www.airbnb.com.br/rooms/2"]),
                                  Grupo("lisboa", "Lisboa", ["LIS", "OPO"], ["https://www.airbnb.com.br/rooms/3"])])
    trechos = [_trecho_h("TPA", "MSP", datetime(2027, 2, 10, 7), datetime(2027, 2, 10, 12)),
               _trecho_h("MSP", "CDG", datetime(2027, 2, 13, 20), datetime(2027, 2, 15, 11)),   # 2 noites no avião
               _trecho_h("BVA", "OPO", datetime(2027, 2, 18, 6), datetime(2027, 2, 18, 8)),     # chega no Porto…
               _trecho_h("LIS", "REC", datetime(2027, 3, 1, 17), datetime(2027, 3, 1, 23))]     # …e sai de Lisboa
    ns = noites(trechos, cfg)
    tipo = {n.data.day if n.data.month == 2 else 100 + n.data.day: n.tipo for n in ns}
    assert tipo[10] == tipo[12] == "casa" and tipo[13] == tipo[14] == "aviao"
    assert tipo[15] == tipo[17] == "g:paris" and tipo[18] == tipo[28] == "g:lisboa"
    assert [(r.grupo, r.entrada.day, r.saida.day, r.noites) for r in reservas(ns)] == [
        ("paris", 15, 18, 3), ("lisboa", 18, 1, 11)]

    # você troca a 1ª noite de Paris por "sem hospedagem": a reserva vira 16→18
    ns2 = noites(trechos, cfg, {"2027-02-15": "sem"})
    assert [(r.entrada.day, r.saida.day) for r in reservas(ns2)][0] == (16, 18)
    # e noites separadas viram duas reservas
    ns3 = noites(trechos, cfg, {"2027-02-16": "sem"})
    assert [(r.grupo, r.entrada.day, r.saida.day) for r in reservas(ns3)][:2] == [("paris", 15, 16), ("paris", 17, 18)]

    e, s, l_e, l_s = date(2027, 2, 15), date(2027, 2, 18), date(2027, 2, 18), date(2027, 3, 1)
    precos = {chave("1", e, s, 2): {"total": 900.0, "nome": "A", "coleta": "2026-11-21T10:00+00:00"},
              chave("2", e, s, 2): {"total": 700.0, "nome": "B", "coleta": "2026-11-21T10:00+00:00"},
              chave("3", l_e, l_s, 2): {"total": None, "motivo": "Estadia mínima", "coleta": "2026-11-21T10:00+00:00"}}
    cs = custos(ns, cfg, precos)
    assert cs[0].total == 700.0 and cs[0].nome == "B" and "check_in=2027-02-15" in cs[0].link
    assert cs[0].segunda[:2] == (900.0, "A") and "rooms/1" in cs[0].segunda[2]   # a 2ª mais barata, só para mostrar
    assert cs[1].total is None and cs[1].situacao == "indisponivel"
    assert total_hospedagem(cs) == (700.0, 1)


def test_hospedagem_le_a_pagina_do_airbnb_e_junta_precos():
    from monitor.hospedagem import juntar, ler_pagina, quarto

    assert ler_pagina("14 noites em Almada\nTotal:\xa0\nO preço original era R$ 2.507 e o novo preço total é R$ 2.251") == (2251.0, "")
    assert ler_pagina("2 noites\nTotal: R$\xa0896\nOu 6x R$ 149") == (896.0, "")
    assert ler_pagina("1 noite\nAdicione datas para ver os preços\nEstadia mínima é de 2 dias")[0] is None
    assert ler_pagina("carregando…") == (None, "?")
    assert quarto("https://www.airbnb.com.br/rooms/47264426?adults=2&check_in=2027-02-14") == "47264426"
    assert quarto("https://www.booking.com/hotel/x") is None
    a = {"precos": {"k": {"total": 1, "coleta": "2026-11-21T10:00"}}, "ultima_busca": None}
    b = {"precos": {"k": {"total": 2, "coleta": "2026-11-21T08:00"}, "j": {"total": 3, "coleta": "x"}},
         "ultima_busca": "2026-11-21T08:00"}
    assert juntar(a, b)["precos"]["k"]["total"] == 1 and "j" in juntar(a, b)["precos"]


def test_hospedagem_booking():
    from monitor.hospedagem import ler_quartos_booking, link_com_datas, link_limpo, quarto

    q = quarto("https://www.booking.com/hotel/fr/chevallier.pt-br.html?label=x&checkin=2027-02-15")
    assert q == "booking:fr/chevallier" and link_limpo(q) == "https://www.booking.com/hotel/fr/chevallier.pt-br.html"
    assert "checkin=2027-02-16&checkout=2027-02-18&group_adults=2" in link_com_datas(q, "2027-02-16", "2027-02-18", 2)
    linhas = [["R$\xa0499", "+R$\xa028 em impostos e taxas", "Máx. pessoas: 2"],
              ["R$\xa0395", "+R$\xa022 em impostos e taxas", "Máx. pessoas: 1"],   # só cabe 1: fora
              ["R$\xa0665", "+R$\xa098 em impostos e taxas", ""]]
    assert ler_quartos_booking(linhas, 2) == 527.0
    assert ler_quartos_booking(linhas[1:2], 2) is None


def test_total_por_pessoa_e_para_o_grupo():
    from monitor.hospedagem import ConfigHosp, Grupo, chave, com_hospedagem
    from monitor.roteiro import Cenario

    cfg = ConfigHosp(2, [], [Grupo("paris", "Paris", ["CDG"], ["https://www.airbnb.com.br/rooms/1"])])
    a = _trecho_h("MSP", "CDG", datetime(2027, 2, 14, 8), datetime(2027, 2, 15, 9))
    b = _trecho_h("CDG", "LIS", datetime(2027, 2, 17, 9), datetime(2027, 2, 17, 11))
    precos = {chave("1", date(2027, 2, 15), date(2027, 2, 17), 2): {"total": 600.0, "coleta": "2026-11-21T10:00"}}
    rt = com_hospedagem([Cenario([a, b])], cfg, precos)[0]     # passagens de 100 cada, 1 adulto
    assert (rt.voos_pp, rt.hospedagem, rt.hosp_pp, rt.total, rt.total_grupo) == (200, 600, 300, 500, 1000)
    rt2 = com_hospedagem([Cenario([a, b])], cfg, precos, adultos={"r": 2})[0]  # busca já com 2 adultos
    assert rt2.voos_pp == 100 and rt2.total_grupo == 800


def test_hospedagem_acompanha_reservas_guardadas(tmp_path):
    from monitor.hospedagem import ConfigHosp, Grupo, Reserva, gravar_config, ler_config

    arq = tmp_path / "hospedagens.yaml"
    cfg = ConfigHosp(2, ["MSP"], [Grupo("porto", "Porto", [], ["https://www.airbnb.com.br/rooms/9"])],
                     [Reserva("porto", date(2027, 2, 18), date(2027, 2, 19))])
    gravar_config(cfg, arq)
    lido = ler_config(arq)
    assert lido.acompanhar == [Reserva("porto", date(2027, 2, 18), date(2027, 2, 19))]
    assert lido.grupos[0].links == ["https://www.airbnb.com.br/rooms/9"]


def test_cotacao_do_google_e_reserva(monkeypatch):
    """1º o Google Finance (ao vivo); se ele falhar, a próxima fonte."""
    from monitor.fontes import base

    class Resp:
        def __init__(self, texto="", dados=None, status=200):
            self.text, self._dados, self.status_code = texto, dados, status
        def raise_for_status(self):
            if self.status_code >= 400:
                raise base.requests.HTTPError(str(self.status_code))
        def json(self): return self._dados

    pagina = 'xx data:[[[["/g/1",null,"USD / BRL",3,null,[5.93419688,0.0038,0.06,4,4,3],null,5.93] yy'
    monkeypatch.setattr(base.requests, "get", lambda url, **k: Resp(pagina) if "google" in url else Resp(status=500))
    base.cotacao_para_brl.cache_clear()
    assert base.cotacao_para_brl("USD") == 5.93419688
    monkeypatch.setattr(base.requests, "get", lambda url, **k: Resp("bloqueado") if "google" in url
                        else Resp(dados={"USDBRL": {"bid": "5.9293"}}))
    base.cotacao_para_brl.cache_clear()
    assert base.cotacao_para_brl("USD") == 5.9293
    base.cotacao_para_brl.cache_clear()
