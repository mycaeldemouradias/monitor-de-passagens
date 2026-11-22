"""Página com senha: preços, roteiros e calendário para ver de qualquer lugar (GitHub Pages).

O repositório é público, então a página publicada é só um "cofre": o conteúdo vai criptografado
(AES-256-GCM, chave tirada da senha com PBKDF2-SHA256) e o navegador destranca com a senha.
Só para ver: ajustes nas viagens continuam no painel do Mac.

Uso:
  PAGINA_SENHA=... python -m monitor.pagina <pasta de saída>     # gera <pasta>/index.html
"""
from __future__ import annotations

import base64
import csv
import html
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .calendario import MESES, _quando, calendario_html, cidade_curta, noites_do_calendario
from .hospedagem import com_hospedagem, ler_config, ler_precos, link_com_datas, quarto
from .config import CONFIG_PADRAO, Geral, _rota
from .regras import brl
from .roteiro import Cenario, hoje_recife, ler_voos, opcoes, roteiros_pelas_ligacoes, todos_os_voos
from .viagens import consultas, ler_viagens

RAIZ = Path(__file__).resolve().parents[2]
FUSO = ZoneInfo("America/Recife")
ITERACOES = 600_000
NOMES_FONTES = {"google_flights": "Google Flights", "skiplagged": "Skiplagged", "kayak": "Kayak"}
N_ROTEIROS = 5


# ------------------------------------------------------------------ dados (o mesmo filtro do painel)
def _dados(config: Path, dados: Path, agora: datetime):
    lista = [v for v in ler_viagens(config) if v.get("ativa", True)]
    nomes = {v["id"]: v.get("nome") or f"{v.get('origem')} → {v.get('destino')}" for v in lista}
    hoje = hoje_recife(agora)
    chaves: set[str] = set()
    for v in lista:
        try:
            chaves |= {c.chave for c in consultas(v, hoje)}
        except Exception:  # noqa: BLE001 — viagem com erro não derruba a página
            pass
    arq = dados / "historico_precos.csv"
    linhas = []
    if arq.exists():
        with open(arq, newline="", encoding="utf-8") as f:
            linhas = [l for l in csv.DictReader(f) if l.get("chave") in chaves]
    op = opcoes(linhas)
    op = {r: {d: o for d, o in dias.items() if d >= hoje} for r, dias in op.items()}
    voos = todos_os_voos(op, ler_voos(dados / "voos.json"))
    voos = {r: {d: [o for o in l if o.chave in chaves] for d, l in dias.items() if d >= hoje} for r, dias in voos.items()}
    voos = {r: {d: l for d, l in dias.items() if l} for r, dias in voos.items()}
    rotas = []
    for v in lista:
        try:
            rotas.append(_rota(v, Geral()))
        except Exception:  # noqa: BLE001
            pass
    ultima = max((datetime.fromisoformat(l["data_hora_utc"]) for l in linhas if l.get("data_hora_utc")), default=None)
    return lista, nomes, {r: d for r, d in op.items() if d}, voos, rotas, ultima


# ------------------------------------------------------------------ conteúdo
def _roteiro_html(cen: Cenario, nomes: dict, rt=None) -> str:
    itens = []
    for a, prox in zip(cen.trechos, cen.trechos[1:] + [None]):
        onde = NOMES_FONTES.get(a.fonte, a.fonte) + (f" ({a.vendedor})" if a.vendedor else "")
        link = f' · <a href="{html.escape(a.link)}" target="_blank" rel="noopener">abrir</a>' if a.link else ""
        itens.append(f"<li>✈️ <b>{a.origem} → {a.destino}</b> ({html.escape(nomes.get(a.rota_id, a.rota_id))}) · "
                     f"sai {_quando(a.partida)} · chega {_quando(a.chegada)} · <b>{brl(a.preco)}</b> · "
                     f"{html.escape(onde)}{link}</li>")
        if prox:
            horas = (prox.partida - a.chegada).total_seconds() / 3600
            tempo = f"{horas / 24:.1f} dias".replace(".", ",") if horas >= 24 else f"{horas:.0f}h"
            itens.append(f'<li class="estadia">📍 Em {html.escape(cidade_curta(a.destino))}: de {_quando(a.chegada)} '
                         f"a {_quando(prox.partida)} ({tempo})</li>")
    if rt is not None:
        for c in rt.custos:
            r = c.reserva
            if c.total is not None:
                link = f' · <a href="{html.escape(c.link)}" target="_blank" rel="noopener">abrir</a>' if c.link else ""
                itens.append(f"<li>🏠 {r.noites} noite(s) {r.entrada:%d/%m}→{r.saida:%d/%m} · <b>{brl(c.total)}</b> · "
                             f"{html.escape(c.nome)}{link}</li>")
                if c.segunda:
                    t2, n2, l2 = c.segunda
                    a2 = f' · <a href="{html.escape(l2)}" target="_blank" rel="noopener">abrir</a>' if l2 else ""
                    itens.append(f'<li class="estadia">↳ 2ª opção: {brl(t2)} · {html.escape(n2)}{a2}</li>')
            else:
                itens.append(f"<li>🏠 {r.noites} noite(s) {r.entrada:%d/%m}→{r.saida:%d/%m} · ⏳ sem preço ainda</li>")
        falta = " (falta preço de hospedagem)" if rt.sem_preco else ""
        total = (f"👤 Por pessoa: voos {brl(rt.voos_pp)} + hospedagem {brl(rt.hosp_pp)} = <b>{brl(rt.total)}</b><br>"
                 f"👥 Para {rt.pessoas}: voos {brl(rt.voos_pp * rt.pessoas)} + hospedagem {brl(rt.hospedagem)} = "
                 f"<b>{brl(rt.total_grupo)}</b>{falta}")
    else:
        total = f"Total: <b>{brl(cen.total)}</b>"
    return f'<ul class="roteiro">{"".join(itens)}</ul><p class="total">{total}</p>'


def conteudo(config: Path = CONFIG_PADRAO, dados: Path = RAIZ / "data", agora: datetime | None = None) -> str:
    agora = agora or datetime.now(timezone.utc)
    lista, nomes, op, voos, rotas, ultima = _dados(config, dados, agora)
    rotas_lig = {r.id: r for r in rotas}
    ids = [v["id"] for v in lista if v["id"] in op]
    quando = (ultima if ultima.tzinfo else ultima.replace(tzinfo=timezone.utc)).astimezone(FUSO) if ultima else None

    partes = [f'<h1>✈️ Monitor de passagens</h1><p class="sub">Preços coletados até '
              f'{quando:%d/%m/%Y às %H:%M} (horário de Recife). Só para ver: para mudar viagens, use o painel do Mac.</p>'
              if quando else "<h1>✈️ Monitor de passagens</h1><p>Ainda não há preços guardados.</p>"]

    # menor preço atual de cada viagem
    linhas_t = []
    for v in sorted(lista, key=lambda v: min(op.get(v["id"], {}) or [date.max])):
        dias = op.get(v["id"], {})
        if not dias:
            linhas_t.append(f"<tr><td>{html.escape(nomes[v['id']])}</td><td colspan=3>ainda sem preço</td></tr>")
            continue
        o = min(dias.values(), key=lambda o: o.preco)
        fonte = html.escape(NOMES_FONTES.get(o.fonte, o.fonte))
        if o.link:
            fonte = f'<a href="{html.escape(o.link)}" target="_blank" rel="noopener">{fonte}</a>'
        linhas_t.append(f"<tr><td>{html.escape(nomes[v['id']])}</td><td><b>{brl(o.preco)}</b></td>"
                        f"<td>{o.data:%d/%m/%Y}</td><td>{fonte}</td></tr>")
    partes.append('<h2>💰 Menor preço de cada viagem</h2><div class="rolar"><table class="precos"><tr><th>Viagem</th>'
                  "<th>Preço</th><th>Data</th><th>Onde</th></tr>" + "".join(linhas_t) + "</table></div>")

    # roteiros pelas ligações
    cenarios: list[Cenario | None] = []
    cfg = ler_config(config.parent / "hospedagens.yaml")
    precos = ler_precos(dados / "hospedagens.json")["precos"]
    rts: list = []
    if any(r.depois_de for r in rotas):
        cenarios, _ = roteiros_pelas_ligacoes(rotas, voos, limite=40 if cfg else N_ROTEIROS)
        if cfg:  # ordena por voos + hospedagem sugerida
            adultos = {v["id"]: max(int(v.get("adultos") or 1), 1) for v in lista}
            rts = com_hospedagem(cenarios, cfg, precos, adultos=adultos)[:N_ROTEIROS]
            cenarios = [rt.cenario for rt in rts]
    if cenarios:
        botoes = "".join(f'<button class="aba{" ativa" if i == 0 else ""}" data-i="{i}">{i + 1}º · '
                         f'{brl(rts[i].total) + " por pessoa" if rts else brl(c.total)}</button>' for i, c in enumerate(cenarios))
        detalhes = "".join(f'<div class="cen" data-i="{i}"{"" if i == 0 else " hidden"}>'
                           f'{_roteiro_html(c, nomes, rts[i] if rts else None)}</div>' for i, c in enumerate(cenarios))
        partes.append(f'<h2>🧭 Roteiros mais baratos</h2><p class="nota">Montados pelas ligações das viagens'
                      + (", somando a hospedagem sugerida" if rts else "")
                      + f'. Escolha um para ver no calendário.</p><div class="abas">{botoes}</div>{detalhes}')
    else:
        cenarios = [None]

    # calendário (um por roteiro; o fundo mostra em que cidade você estaria)
    if ids:
        meses = sorted({(d.year, d.month) for r in ids for d in op[r]})
        cals = []
        for i, cen in enumerate(cenarios):
            nts = noites_do_calendario(rts[i].noites, rts[i].custos, cfg) if rts else None
            corpo = "".join(f"<h3>{MESES[m - 1].capitalize()} de {a}</h3><div class=\"rolar\">"
                            + calendario_html(a, m, op, ids, cen, nomes, voos, rotas_lig, clicavel=True, noites=nts,
                                              adultos={v["id"]: max(int(v.get("adultos") or 1), 1) for v in lista})
                            + "</div>" for a, m in meses)
            cals.append(f'<div class="cen" data-i="{i}"{"" if i == 0 else " hidden"}>{corpo}</div>')
        partes.append('<h2>📅 Calendário</h2><p class="nota">Toque num voo para pôr ou tirar da sua simulação '
                      '(azul = escolhido). <b>★ verde</b> = o mais barato do dia para o mesmo destino ou da mesma '
                      'origem. ❌ vermelho = choque de horário. 🌙 = onde você dorme: troque na lista de cada noite '
                      '(noites seguidas no mesmo lugar = uma reserva). Passe o dedo para o lado para ver a semana toda.</p>'
                      '<div id="soma" class="soma"></div>' + "".join(cals))
    partes.append('<p class="nota">Preços da última coleta de cada data; podem ter mudado. Bilhetes separados: '
                  "se um voo atrasar, o seguinte não é protegido.</p>")
    return (_MOLDE.replace("{{CORPO}}", "".join(partes)).replace("{{HOSP}}", json.dumps(_tabela_hospedagem(cfg, precos)))
            .replace("{{PESSOAS}}", str(cfg.hospedes if cfg else 1)))


def _tabela_hospedagem(cfg, precos: dict) -> dict:
    """{grupo: {"entrada|saída": [[total, nome, link], …]}}: as 2 mais baratas de cada reserva já buscada (para a
    página recalcular quando você troca uma noite)."""
    if not cfg:
        return {}
    saida: dict = {}
    for g in cfg.grupos:
        quartos = {quarto(l) for l in g.links} - {None}
        for k, p in precos.items():
            q, e, s, h = k.split("|")  # q: código do anúncio (Airbnb ou "booking:…")
            if q in quartos and int(h) == cfg.hospedes and p.get("total") is not None:
                saida.setdefault(f"g:{g.id}", {}).setdefault(f"{e}|{s}", []).append(
                    [p["total"], (p.get("nome") or "")[:28], link_com_datas(q, e, s, int(h))])
    for reservas in saida.values():
        for k, lista in reservas.items():
            reservas[k] = sorted(lista, key=lambda x: x[0])[:2]
    return saida


_MOLDE = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Monitor de passagens</title>
<style>
:root{color-scheme:light}
body{font-family:-apple-system,system-ui,sans-serif;margin:0 auto;padding:16px;max-width:1200px;background:#fff;color:#111827}
h1{font-size:1.6rem;margin:.2em 0}h2{margin-top:1.6em;font-size:1.25rem}h3{margin:1.2em 0 .4em}
.sub,.nota{color:#4b5563;font-size:.9rem}a{color:#1d4ed8}
.rolar{overflow-x:auto;-webkit-overflow-scrolling:touch}.rolar table.cal{min-width:760px}
table.precos{border-collapse:collapse;width:100%;font-size:.95rem}
table.precos td,table.precos th{border-bottom:1px solid #e5e7eb;padding:6px;text-align:left;white-space:nowrap}
table.precos td:first-child{white-space:normal;overflow-wrap:anywhere}
.abas{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.aba{border:1px solid #d1d5db;background:#f9fafb;border-radius:8px;padding:6px 10px;font-size:.95rem;cursor:pointer}
.aba.ativa{background:#1d4ed8;color:#fff;border-color:#1d4ed8}
ul.roteiro{padding-left:0;list-style:none}ul.roteiro li{margin:6px 0}li.estadia{color:#4b5563;font-size:.9rem}
.cal .chip[data-id]{cursor:pointer}
.soma{position:sticky;top:0;z-index:5;background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:8px 12px;margin:8px 0}
.soma.erro{background:#fef2f2;border-color:#fecaca}
</style></head><body>{{CORPO}}
<script>
const abas=document.querySelectorAll('.aba');
function mostrar(i){document.querySelectorAll('.cen').forEach(e=>e.hidden=e.dataset.i!==String(i));
  abas.forEach(b=>b.classList.toggle('ativa',b.dataset.i===String(i)));somar();}
abas.forEach(b=>b.addEventListener('click',()=>mostrar(b.dataset.i)));
const brl=v=>v.toLocaleString('pt-BR',{style:'currency',currency:'BRL'});
function somar(){
  const vis=[...document.querySelectorAll('.cen:not([hidden]) .chip[data-id]')];
  const esc=vis.filter(c=>c.classList.contains('voo'));
  vis.forEach(c=>c.classList.remove('problema'));
  const comHora=esc.filter(c=>c.dataset.partida).sort((a,b)=>a.dataset.partida<b.dataset.partida?-1:1);
  let choques=0;
  for(let i=1;i<comHora.length;i++){if(comHora[i].dataset.partida<comHora[i-1].dataset.chegada){
    comHora[i].classList.add('problema');comHora[i-1].classList.add('problema');choques++;}}
  const total=esc.reduce((s,c)=>s+parseFloat(c.dataset.preco),0);
  const [hosp,falta]=noites();
  const caixa=document.getElementById('soma');if(!caixa)return;
  caixa.classList.toggle('erro',choques>0);
  const N=PESSOAS;
  let txt=`Sua simulação: <b>${esc.length} voo(s)</b>`;
  if(hosp!==null){txt+=`<br>👤 Por pessoa: voos ${brl(total)} + hospedagem ${brl(hosp/N)} = <b>${brl(total+hosp/N)}</b>`+
    `<br>👥 Para ${N}: voos ${brl(total*N)} + hospedagem ${brl(hosp)} = <b>${brl(total*N+hosp)}</b>`+
    (falta?`<br>⏳ ${falta} reserva(s) de hospedagem sem preço`:'');}
  else txt+=` · ${brl(total)} por pessoa`;
  caixa.innerHTML=esc.length?txt+(choques?`<br>❌ ${choques} choque(s) de horário`:''):'Nenhum voo escolhido. Toque num voo do calendário.';
}
// 🌙 noites: junta noites seguidas no mesmo lugar em reservas e procura o preço já buscado
const HOSP={{HOSP}}, PESSOAS={{PESSOAS}};
const dia1=d=>{const x=new Date(d+'T12:00:00');x.setDate(x.getDate()+1);return x.toISOString().slice(0,10)};
function noites(){
  const sels=[...document.querySelectorAll('.cen:not([hidden]) select[data-noite]')].sort((a,b)=>a.dataset.noite<b.dataset.noite?-1:1);
  if(!sels.length)return [null,0];
  const res=[];
  for(const s of sels){const d=s.dataset.noite,t=s.value,ult=res[res.length-1];
    if(!t.startsWith('g:'))continue;
    if(ult&&ult.g===t&&ult.saida===d){ult.saida=dia1(d);ult.sels.push(s);}else res.push({g:t,entrada:d,saida:dia1(d),sels:[s]});}
  sels.forEach(s=>{const nt=s.parentElement.querySelector('.nt');if(nt)nt.innerHTML=''});
  let soma=0,falta=0;
  const f=d=>d.slice(8,10)+'/'+d.slice(5,7);
  for(const r of res){const ps=(HOSP[r.g]||{})[r.entrada+'|'+r.saida]||[],p=ps[0],p2=ps[1];const n=r.sels.length;
    const per=`${n} noite(s), ${f(r.entrada)}→${f(r.saida)}`;
    const nt=r.sels[0].parentElement.querySelector('.nt');
    if(p){soma+=p[0];if(nt)nt.innerHTML=`<b>${brl(p[0])}</b> · ${per} · ${p[1]} · <a href="${p[2]}" target="_blank" rel="noopener">abrir</a>`
      +(p2?`<br>2ª: ${brl(p2[0])} · ${p2[1]} · <a href="${p2[2]}" target="_blank" rel="noopener">abrir</a>`:'');}
    else{falta++;if(nt)nt.innerHTML=`⏳ sem preço para ${per} (o GitHub busca as datas dos roteiros sugeridos)`;}
    r.sels.slice(1).forEach(s=>{const x=s.parentElement.querySelector('.nt');if(x)x.textContent='↳ mesma reserva'});}
  return [soma,falta];
}
document.addEventListener('change',e=>{if(e.target.matches('select[data-noite]'))somar();});
document.addEventListener('click',e=>{const c=e.target.closest('.chip[data-id]');if(!c)return;
  c.classList.toggle('voo');somar();});
somar();
</script></body></html>"""


# ------------------------------------------------------------------ cofre da página
def trancar(pagina: str, senha: str) -> str:
    sal, iv = os.urandom(16), os.urandom(12)
    chave = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=sal, iterations=ITERACOES).derive(senha.encode())
    cifra = AESGCM(chave).encrypt(iv, pagina.encode("utf-8"), None)
    dados = {k: base64.b64encode(v).decode() for k, v in (("sal", sal), ("iv", iv), ("cifra", cifra))}
    return _PORTA.replace("{{DADOS}}", json.dumps(dados | {"it": ITERACOES}))


_PORTA = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>Monitor de passagens</title>
<style>
:root{color-scheme:light dark}
body{font-family:-apple-system,system-ui,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0;padding:16px;background:#f3f4f6;color:#111827}
form{background:#fff;padding:24px;border-radius:14px;box-shadow:0 4px 20px #0001;width:100%;max-width:340px}
h1{font-size:1.3rem;margin:0 0 12px}input[type=password]{width:100%;box-sizing:border-box;font-size:1.1rem;padding:10px;border:1px solid #d1d5db;border-radius:8px}
button{margin-top:12px;width:100%;font-size:1.05rem;padding:10px;border:0;border-radius:8px;background:#1d4ed8;color:#fff}
label{display:block;margin-top:10px;font-size:.9rem;color:#4b5563}#erro{color:#dc2626;min-height:1.2em;font-size:.9rem}
@media (prefers-color-scheme:dark){body{background:#111827}form{background:#1f2937;color:#f9fafb}label{color:#d1d5db}}
</style></head><body>
<form id="f"><h1>✈️ Monitor de passagens</h1>
<input type="password" id="s" placeholder="Senha" autocomplete="current-password" autofocus>
<label><input type="checkbox" id="lembrar"> Lembrar neste aparelho</label>
<button>Abrir</button><p id="erro"></p></form>
<script>
const D={{DADOS}};
const b64=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
async function abrir(senha){
  const base=await crypto.subtle.importKey('raw',new TextEncoder().encode(senha),'PBKDF2',false,['deriveKey']);
  const chave=await crypto.subtle.deriveKey({name:'PBKDF2',salt:b64(D.sal),iterations:D.it,hash:'SHA-256'},
    base,{name:'AES-GCM',length:256},false,['decrypt']);
  const txt=new TextDecoder().decode(await crypto.subtle.decrypt({name:'AES-GCM',iv:b64(D.iv)},chave,b64(D.cifra)));
  document.open();document.write(txt);document.close();
}
const guardada=(()=>{try{return localStorage.getItem('monitor_senha')}catch(e){return null}})();
if(guardada){document.getElementById('erro').textContent='Abrindo…';
  abrir(guardada).catch(()=>{try{localStorage.removeItem('monitor_senha')}catch(e){}
    document.getElementById('erro').textContent='';});}
document.getElementById('f').addEventListener('submit',async e=>{e.preventDefault();
  const senha=document.getElementById('s').value;document.getElementById('erro').textContent='Abrindo…';
  try{await abrir(senha);
    if(document.getElementById('lembrar')?.checked){try{localStorage.setItem('monitor_senha',senha)}catch(e){}}
  }catch(err){document.getElementById('erro').textContent='Senha errada.';}
});
</script></body></html>"""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__)
        return 2
    senha = os.getenv("PAGINA_SENHA")
    if not senha:
        try:
            from dotenv import load_dotenv
            load_dotenv(RAIZ / ".env")
        except ImportError:
            pass
        senha = os.getenv("PAGINA_SENHA")
    if not senha:
        print("Falta a PAGINA_SENHA (no .env do Mac ou nos Secrets do GitHub): página não gerada.")
        return 1
    saida = Path(argv[0])
    saida.mkdir(parents=True, exist_ok=True)
    (saida / "index.html").write_text(trancar(conteudo(), senha), encoding="utf-8")
    (saida / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Página gerada (trancada com senha) em {saida / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
