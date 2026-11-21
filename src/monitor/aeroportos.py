"""Aeroportos: a lista completa (config/aeroportos_comerciais.csv) e os favoritos com nome em português.

A lista completa vem do OurAirports (4.007 aeroportos comerciais, 20/11/2026), com cidades e
aeroportos em inglês. Os favoritos aparecem primeiro no painel e podem ser achados pelo nome em
português (ex.: "Nova York", "Lisboa").
"""
from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path

CSV_PADRAO = Path(__file__).resolve().parents[2] / "config" / "aeroportos_comerciais.csv"

#: código → nome em português (cidade – aeroporto, país/UF). Ordem = ordem no painel.
FAVORITOS = {
    # Brasil
    "REC": "Recife – Guararapes (PE)", "GRU": "São Paulo – Guarulhos (SP)",
    "CGH": "São Paulo – Congonhas (SP)", "VCP": "Campinas – Viracopos (SP)",
    "GIG": "Rio de Janeiro – Galeão (RJ)", "SDU": "Rio de Janeiro – Santos Dumont (RJ)",
    "BSB": "Brasília – Juscelino Kubitschek (DF)", "SSA": "Salvador – Luís Eduardo Magalhães (BA)",
    "FOR": "Fortaleza – Pinto Martins (CE)", "CNF": "Belo Horizonte – Confins (MG)",
    "POA": "Porto Alegre – Salgado Filho (RS)", "CWB": "Curitiba (PR)", "FLN": "Florianópolis (SC)",
    "NAT": "Natal (RN)", "JPA": "João Pessoa (PB)", "MCZ": "Maceió (AL)", "AJU": "Aracaju (SE)",
    "THE": "Teresina (PI)", "SLZ": "São Luís (MA)", "BEL": "Belém (PA)", "MAO": "Manaus (AM)",
    "VIX": "Vitória (ES)", "GYN": "Goiânia (GO)", "CGB": "Cuiabá (MT)", "CGR": "Campo Grande (MS)",
    "NVT": "Navegantes (SC)", "IGU": "Foz do Iguaçu (PR)", "FEN": "Fernando de Noronha (PE)",
    "PNZ": "Petrolina (PE)", "JDO": "Juazeiro do Norte (CE)", "BPS": "Porto Seguro (BA)",
    "IOS": "Ilhéus (BA)", "CPV": "Campina Grande (PB)", "UDI": "Uberlândia (MG)", "LDB": "Londrina (PR)",
    "PMW": "Palmas (TO)", "MCP": "Macapá (AP)", "BVB": "Boa Vista (RR)", "PVH": "Porto Velho (RO)",
    "RBR": "Rio Branco (AC)",
    # EUA
    "JFK": "Nova York – John F. Kennedy (EUA)", "LGA": "Nova York – LaGuardia (EUA)",
    "EWR": "Nova York – Newark Liberty, Nova Jersey (EUA)", "TPA": "Tampa – Tampa International (EUA)",
    "MCO": "Orlando – Orlando International (EUA)", "MIA": "Miami – Miami International (EUA)",
    "FLL": "Fort Lauderdale – Fort Lauderdale-Hollywood (EUA)", "ATL": "Atlanta – Hartsfield-Jackson (EUA)",
    "DFW": "Dallas – Dallas/Fort Worth (EUA)", "IAH": "Houston – George Bush Intercontinental (EUA)",
    "ORD": "Chicago – O'Hare (EUA)", "DEN": "Denver – Denver International (EUA)",
    "LAX": "Los Angeles – Los Angeles International (EUA)", "SFO": "San Francisco – San Francisco International (EUA)",
    "LAS": "Las Vegas – Harry Reid (EUA)", "BOS": "Boston – Logan (EUA)", "IAD": "Washington – Dulles (EUA)",
    "MSP": "Minneapolis – Minneapolis–Saint Paul (EUA)", "CLT": "Charlotte – Charlotte Douglas (EUA)",
    # Canadá e México
    "YYZ": "Toronto – Pearson (Canadá)", "YTZ": "Toronto – Billy Bishop, centro (Canadá)",
    "YUL": "Montreal – Trudeau (Canadá)", "YVR": "Vancouver – Vancouver International (Canadá)",
    "MEX": "Cidade do México – Benito Juárez (México)", "CUN": "Cancún (México)",
    # América Latina
    "PTY": "Cidade do Panamá – Tocumen (Panamá)", "BOG": "Bogotá – El Dorado (Colômbia)",
    "LIM": "Lima – Jorge Chávez (Peru)", "SCL": "Santiago – Arturo Merino Benítez (Chile)",
    "EZE": "Buenos Aires – Ezeiza, internacional (Argentina)",
    "AEP": "Buenos Aires – Aeroparque, regional e doméstico (Argentina)",
    "MVD": "Montevidéu – Carrasco (Uruguai)",
    # Portugal e Espanha
    "LIS": "Lisboa – Humberto Delgado (Portugal)", "OPO": "Porto – Francisco Sá Carneiro (Portugal)",
    "FAO": "Faro, Algarve (Portugal)", "FNC": "Funchal, Madeira – Cristiano Ronaldo (Portugal)",
    "MAD": "Madri – Barajas (Espanha)", "BCN": "Barcelona – El Prat (Espanha)",
    "AGP": "Málaga – Costa del Sol (Espanha)", "SVQ": "Sevilha – San Pablo (Espanha)",
    # Resto da Europa
    "CDG": "Paris – Charles de Gaulle, o principal (França)", "ORY": "Paris – Orly, mais perto do centro (França)",
    "BVA": "Paris – Beauvais, low-cost e longe do centro (França)",
    "LHR": "Londres – Heathrow (Reino Unido)", "LGW": "Londres – Gatwick (Reino Unido)",
    "STN": "Londres – Stansted, low-cost (Reino Unido)", "LTN": "Londres – Luton, low-cost (Reino Unido)",
    "AMS": "Amsterdã – Schiphol (Holanda)", "FRA": "Frankfurt (Alemanha)", "MUC": "Munique (Alemanha)",
    "BER": "Berlim – Brandenburg (Alemanha)", "FCO": "Roma – Fiumicino (Itália)",
    "MXP": "Milão – Malpensa (Itália)", "ZRH": "Zurique (Suíça)", "BRU": "Bruxelas (Bélgica)",
    "VIE": "Viena (Áustria)", "DUB": "Dublin (Irlanda)", "CPH": "Copenhague – Kastrup (Dinamarca)",
    "ATH": "Atenas – Eleftherios Venizelos (Grécia)", "IST": "Istambul (Turquia)",
    # Oriente Médio, Ásia e Oceania
    "DXB": "Dubai – Dubai International (Emirados Árabes)", "DOH": "Doha – Hamad (Catar)",
    "AUH": "Abu Dhabi – Zayed (Emirados Árabes)", "HND": "Tóquio – Haneda (Japão)",
    "NRT": "Tóquio – Narita (Japão)", "ICN": "Seul – Incheon (Coreia do Sul)",
    "PVG": "Xangai – Pudong (China)", "PEK": "Pequim – Capital (China)", "CAN": "Guangzhou – Baiyun (China)",
    "SZX": "Shenzhen – Bao'an (China)", "HKG": "Hong Kong – Hong Kong International",
    "SIN": "Singapura – Changi", "KUL": "Kuala Lumpur – Kuala Lumpur International (Malásia)",
    "BKK": "Bangkok – Suvarnabhumi (Tailândia)", "DEL": "Nova Délhi – Indira Gandhi (Índia)",
    "SYD": "Sydney – Kingsford Smith (Austrália)",
}

_ORDEM_PORTE = {"Grande": 0, "Médio": 1, "Pequeno": 2}


@lru_cache(maxsize=4)
def todos(caminho: Path = CSV_PADRAO) -> dict[str, str]:
    """código → rótulo de busca, favoritos primeiro e depois os demais (grandes antes). {} se não houver CSV."""
    if not caminho.exists():
        return {}
    with open(caminho, newline="", encoding="utf-8") as f:
        linhas = [r for r in csv.DictReader(f) if r.get("Código IATA")]
    rotulos, cidades = {}, {}
    for r in sorted(linhas, key=lambda r: (_ORDEM_PORTE.get(r["Porte"], 3), r["Cidade"])):
        regiao = f", {r['Estado/Região']}" if r["Estado/Região"] and r["País"] in ("Estados Unidos", "Brasil") else ""
        rotulos[r["Código IATA"]] = f"{r['Cidade']} – {r['Aeroporto']} ({r['País']}{regiao})"
        cidades[r["Código IATA"]] = r["Cidade"].split(" (")[0]
    if not rotulos:
        return dict(FAVORITOS)
    # favoritos: nome em português + a cidade em inglês quando for diferente (achar por "Lisboa" ou "Lisbon")
    fav = {}
    for c, n in FAVORITOS.items():
        if c in rotulos:
            ingles = cidades[c]
            fav[c] = n if ingles.lower() in n.lower() else f"{n} · {ingles}"
    return fav | {c: n for c, n in rotulos.items() if c not in fav}


def existe(codigo: str, caminho: Path = CSV_PADRAO) -> bool:
    """True se o código está na lista. Sem a lista (arquivo ausente), não bloqueia."""
    if not caminho.exists():
        return True
    return codigo in todos(caminho)


def rotulo(codigo: str) -> str:
    nome = FAVORITOS.get(codigo) or todos().get(codigo)
    return f"{codigo} — {nome}" if nome else codigo
