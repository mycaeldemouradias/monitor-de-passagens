"""Estruturas de dados do monitor."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class Consulta:
    """Uma busca concreta: um trecho + uma combinação de datas."""

    rota_id: str
    origem: str
    destino: str
    ida: date
    volta: date | None = None
    adultos: int = 1
    cabine: str = "economica"  # economica | premium | executiva | primeira
    moeda: str = "BRL"

    @property
    def ida_volta(self) -> bool:
        return self.volta is not None

    @property
    def chave(self) -> str:
        """Identifica a mesma busca entre execuções (base do histórico)."""
        volta = self.volta.isoformat() if self.volta else "-"
        return f"{self.rota_id}|{self.origem}-{self.destino}|{self.ida.isoformat()}|{volta}|{self.adultos}"

    def descricao(self) -> str:
        datas = self.ida.strftime("%d/%m/%Y")
        if self.volta:
            datas += f" → {self.volta.strftime('%d/%m/%Y')}"
        tipo = "ida e volta" if self.volta else "só ida"
        return f"{self.origem} → {self.destino} | {datas} ({tipo}, {self.adultos} adulto(s))"


@dataclass
class Trecho:
    """Um sentido da viagem (ida ou volta), com horários locais de cada aeroporto."""

    partida: datetime
    chegada: datetime
    voos: list[str] = field(default_factory=list)                 # ["AD 4153", "LA 3319"]
    conexoes: list[tuple[str, int]] = field(default_factory=list)  # [(aeroporto, minutos)]
    desce_em: str = ""  # hidden-city: aeroporto onde descer (o bilhete segue além do destino)

    def __post_init__(self) -> None:
        # sempre o horário local do aeroporto, sem fuso: o Skiplagged manda "…-03:00" e o Google não manda
        # fuso; misturar os dois quebra as contas ("can't subtract offset-naive and offset-aware")
        self.partida = hora_local(self.partida)
        self.chegada = hora_local(self.chegada)

    @property
    def escalas(self) -> int:
        return len(self.conexoes)

    def resumo(self) -> str:
        """Ex.: 'qua 10/03 02:25 → 05:40 · AD 4153 · direto'."""
        dia = DIAS[self.partida.weekday()]
        chegada = self.chegada.strftime("%H:%M")
        if self.chegada.date() != self.partida.date():
            chegada += f" ({self.chegada:%d/%m})"
        if self.conexoes:
            escalas = "escala em " + ", ".join(f"{a} ({m // 60}h{m % 60:02d})" for a, m in self.conexoes)
        else:
            escalas = "direto"
        partes = [f"{dia} {self.partida:%d/%m %H:%M} → {chegada}"]
        if self.voos:
            partes.append(", ".join(self.voos))
        return " · ".join(partes + [escalas])

    def conexoes_texto(self) -> str:
        return "; ".join(f"{a} {m}min" for a, m in self.conexoes)


DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def hora_local(dt: datetime | None) -> datetime | None:
    """Tira o fuso mantendo o horário de relógio (o horário local que aparece na passagem)."""
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo is not None else dt


@dataclass
class Oferta:
    """Preço encontrado por uma fonte para uma consulta."""

    consulta: Consulta
    fonte: str
    preco: float
    moeda: str = "BRL"
    companhia: str = ""
    detalhes: str = ""
    link: str = ""
    ida: Trecho | None = None
    volta: Trecho | None = None
    vendedor: str = ""      # onde comprar (ex.: "Mytrip"), quando a fonte informa
    avisos: list[str] = field(default_factory=list)  # ex.: 2 bilhetes separados, hidden-city
    nivel_preco: str = ""   # Google: "baixo (típico R$ 940–1.700)"


@dataclass
class Rota:
    """Rota configurada pelo usuário em config/rotas.yaml."""

    id: str
    origem: str
    destino: str
    idas: list[date]
    voltas: list[date] = field(default_factory=list)
    duracoes: list[int] = field(default_factory=list)
    adultos: int = 1
    cabine: str = "economica"
    preco_alvo: float | None = None
    queda_pct: float | None = None
    fontes: list[str] = field(default_factory=list)
    ativa: bool = True
    nome: str = ""  # nome amigável dado no painel
    #: acontece depois de QUALQUER UMA destas viagens (a que for comprada), com intervalo entre a
    #: chegada daquela e a saída desta de no mínimo `intervalo_min_horas` e, se houver, no máximo `..._max_`
    depois_de: list[str] = field(default_factory=list)
    intervalo_min_horas: float = 3
    intervalo_max_horas: float | None = None


def _trecho_dict(t: Trecho | None) -> dict | None:
    if t is None:
        return None
    return {"partida": t.partida.isoformat(), "chegada": t.chegada.isoformat(), "voos": t.voos,
            "conexoes": [list(c) for c in t.conexoes], "desce_em": t.desce_em}


def _trecho_de(d: dict | None) -> Trecho | None:
    if not d:
        return None
    return Trecho(partida=datetime.fromisoformat(d["partida"]), chegada=datetime.fromisoformat(d["chegada"]),
                  voos=list(d["voos"]), conexoes=[(a, int(m)) for a, m in d["conexoes"]], desce_em=d["desce_em"])


def oferta_para_dict(o: Oferta) -> dict:
    """Para enviar ofertas entre os computadores do GitHub (cada parte do Skiplagged roda em um)."""
    c = o.consulta
    return {
        "consulta": {"rota_id": c.rota_id, "origem": c.origem, "destino": c.destino, "ida": c.ida.isoformat(),
                     "volta": c.volta.isoformat() if c.volta else None, "adultos": c.adultos,
                     "cabine": c.cabine, "moeda": c.moeda},
        "fonte": o.fonte, "preco": o.preco, "moeda": o.moeda, "companhia": o.companhia, "detalhes": o.detalhes,
        "link": o.link, "ida": _trecho_dict(o.ida), "volta": _trecho_dict(o.volta), "vendedor": o.vendedor,
        "avisos": o.avisos, "nivel_preco": o.nivel_preco,
    }


def oferta_de_dict(d: dict) -> Oferta:
    c = dict(d["consulta"])
    c["ida"] = date.fromisoformat(c["ida"])
    c["volta"] = date.fromisoformat(c["volta"]) if c["volta"] else None
    return Oferta(consulta=Consulta(**c), fonte=d["fonte"], preco=float(d["preco"]), moeda=d["moeda"],
                  companhia=d["companhia"], detalhes=d["detalhes"], link=d["link"], ida=_trecho_de(d["ida"]),
                  volta=_trecho_de(d["volta"]), vendedor=d["vendedor"], avisos=list(d["avisos"]),
                  nivel_preco=d["nivel_preco"])
