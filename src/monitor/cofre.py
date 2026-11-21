"""Cofre: criptografa o que é pessoal antes de ir para o repositório (que é público).

No repositório só ficam os arquivos `.cripto`. As versões legíveis ficam fora do Git (.gitignore):
- config/rotas.yaml e config/pausas.yaml          → config/<nome>.cripto
- data/estado.json e data/ultima_execucao.json    → data/<nome>.cripto
- data/historico_precos.csv                       → data/historico/AAAA-MM-DD.csv.gz.cripto
  (um arquivo por dia UTC: cada rodada só regrava o dia de hoje, e o repositório não incha)

Um arquivo só é regravado quando o conteúdo muda (a criptografia gera um resultado diferente a
cada vez, então regravar sem mudança criaria alterações falsas no Git).

Chave: MONITOR_CHAVE (Fernet = AES-128 + HMAC-SHA256), no .env do Mac e nos Secrets do GitHub.
Sem ela, os dados não podem ser lidos por ninguém, nem por você: guarde uma cópia em lugar seguro.

Uso:
  python -m monitor.cofre abrir  config|dados|tudo
  python -m monitor.cofre fechar config|dados|tudo
  python -m monitor.cofre nova-chave      # cria a chave no .env (se ainda não existir), sem mostrá-la
"""
from __future__ import annotations

import csv
import gzip
import io
import os
import sys
from collections import defaultdict
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .historico import COLUNAS

RAIZ = Path(__file__).resolve().parents[2]
EXT = ".cripto"
SIMPLES = {
    "config": ["config/rotas.yaml", "config/pausas.yaml"],
    "dados": ["data/estado.json", "data/ultima_execucao.json"],
}
HISTORICO = "data/historico_precos.csv"
PASTA_HISTORICO = "data/historico"


class ChaveAusente(RuntimeError):
    pass


def _fernet() -> Fernet:
    chave = os.getenv("MONITOR_CHAVE")
    if not chave:
        try:
            from dotenv import load_dotenv
            load_dotenv(RAIZ / ".env")
        except ImportError:
            pass
        chave = os.getenv("MONITOR_CHAVE")
    if not chave:
        raise ChaveAusente("Falta a MONITOR_CHAVE (no .env do Mac ou nos Secrets do GitHub).")
    return Fernet(chave.encode())


def _ler(cripto: Path, f: Fernet) -> bytes:
    try:
        return f.decrypt(cripto.read_bytes())
    except InvalidToken:
        raise ChaveAusente(f"A MONITOR_CHAVE não abre {cripto.name}: é outra chave.") from None


def _gravar_se_mudou(cripto: Path, conteudo: bytes, f: Fernet) -> bool:
    if cripto.exists() and _ler(cripto, f) == conteudo:
        return False
    cripto.parent.mkdir(parents=True, exist_ok=True)
    cripto.write_bytes(f.encrypt(conteudo))
    return True


def _por_dia(texto: str) -> dict[str, str]:
    """Separa o CSV do histórico em um CSV por dia (data UTC da coleta), cada um com cabeçalho."""
    linhas = list(csv.DictReader(io.StringIO(texto)))
    dias: dict[str, list[dict]] = defaultdict(list)
    for linha in linhas:
        dias[str(linha.get("data_hora_utc", ""))[:10] or "sem-data"].append(linha)
    saida = {}
    for dia, grupo in dias.items():
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=COLUNAS, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(grupo)
        saida[dia] = buf.getvalue()
    return saida


def fechar(raiz: Path = RAIZ, parte: str = "tudo") -> list[str]:
    """Criptografa os arquivos legíveis. Devolve os .cripto que mudaram."""
    f = _fernet()
    mudaram = []
    for grupo, arquivos in SIMPLES.items():
        if parte not in (grupo, "tudo"):
            continue
        for nome in arquivos:
            legivel = raiz / nome
            if legivel.exists() and _gravar_se_mudou(raiz / (nome + EXT), legivel.read_bytes(), f):
                mudaram.append(nome + EXT)
    hist = raiz / HISTORICO
    if parte in ("dados", "tudo") and hist.exists():
        for dia, texto in _por_dia(hist.read_text(encoding="utf-8")).items():
            destino = raiz / PASTA_HISTORICO / f"{dia}.csv.gz{EXT}"
            # mtime=0: o mesmo conteúdo gera sempre os mesmos bytes compactados
            if _gravar_se_mudou(destino, gzip.compress(texto.encode("utf-8"), mtime=0), f):
                mudaram.append(str(destino.relative_to(raiz)))
    return mudaram


def abrir(raiz: Path = RAIZ, parte: str = "tudo") -> list[str]:
    """Decifra os .cripto, sobrescrevendo as versões legíveis. Devolve o que foi aberto."""
    f = _fernet()
    abertos = []
    for grupo, arquivos in SIMPLES.items():
        if parte not in (grupo, "tudo"):
            continue
        for nome in arquivos:
            cripto = raiz / (nome + EXT)
            if cripto.exists():
                (raiz / nome).parent.mkdir(parents=True, exist_ok=True)
                (raiz / nome).write_bytes(_ler(cripto, f))
                abertos.append(nome)
    pasta = raiz / PASTA_HISTORICO
    if parte in ("dados", "tudo") and pasta.exists():
        dias = sorted(pasta.glob(f"*.csv.gz{EXT}"))
        if dias:
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=COLUNAS, extrasaction="ignore", lineterminator="\n")
            w.writeheader()
            for arq in dias:
                texto = gzip.decompress(_ler(arq, f)).decode("utf-8")
                w.writerows(csv.DictReader(io.StringIO(texto)))
            (raiz / HISTORICO).write_text(buf.getvalue(), encoding="utf-8")
            abertos.append(HISTORICO)
    return abertos


def pendentes(raiz: Path = RAIZ) -> list[str]:
    """Arquivos de configuração mudados no Mac e ainda não criptografados (= não publicados)."""
    f = _fernet()
    mudados = []
    for nome in SIMPLES["config"]:
        legivel, cripto = raiz / nome, raiz / (nome + EXT)
        if legivel.exists() and (not cripto.exists() or _ler(cripto, f) != legivel.read_bytes()):
            mudados.append(nome)
    return mudados


def nova_chave(env: Path = RAIZ / ".env") -> bool:
    """Cria a MONITOR_CHAVE no .env, se ainda não existir. Nunca mostra a chave."""
    texto = env.read_text(encoding="utf-8") if env.exists() else ""
    if any(l.strip().startswith("MONITOR_CHAVE=") and l.split("=", 1)[1].strip() for l in texto.splitlines()):
        return False
    linhas = [l for l in texto.splitlines() if not l.strip().startswith("MONITOR_CHAVE=")]
    linhas += ["", "# Chave da criptografia (viagens e histórico no GitHub). Guarde uma cópia em lugar seguro:",
               "# sem ela, os dados criptografados não podem ser recuperados.",
               f"MONITOR_CHAVE={Fernet.generate_key().decode()}"]
    env.write_text("\n".join(linhas).lstrip("\n") + "\n", encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["nova-chave"]:
        print("Chave criada no .env." if nova_chave() else "A chave já existe no .env (nada mudou).")
        return 0
    if len(argv) != 2 or argv[0] not in ("abrir", "fechar") or argv[1] not in ("config", "dados", "tudo"):
        print(__doc__)
        return 2
    try:
        feitos = (abrir if argv[0] == "abrir" else fechar)(RAIZ, argv[1])
    except ChaveAusente as e:
        print(f"Erro: {e}")
        return 1
    print(f"{argv[0]}: {len(feitos)} arquivo(s)" + "".join(f"\n  {x}" for x in feitos))
    return 0


if __name__ == "__main__":
    sys.exit(main())
