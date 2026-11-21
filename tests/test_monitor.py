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
geral: {fontes_padrao: [teste], min_observacoes: 1}
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
    assert linhas[1]["ida_partida"] == "2027-03-10T02:25-03:00" and linhas[1]["volta_voos"] == "AD 4232"


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
    def falso_get(url, timeout):
        urls.append(url)
        return Resp(429, {}) if "awesomeapi" in url else Resp(200, {"rates": {"BRL": 5.9}})
    monkeypatch.setattr(base.requests, "get", falso_get)
    base.cotacao_para_brl.cache_clear()
    assert base.cotacao_para_brl("USD") == 5.9
    assert base.cotacao_para_brl("USD") == 5.9 and len(urls) == 2  # 2ª vez vem do cache
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
