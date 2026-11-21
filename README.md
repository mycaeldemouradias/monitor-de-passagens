# Monitor de passagens aéreas

Esse monitor consulta os preços das rotas e datas que você define. Ele guarda o histórico e avisa no **Telegram** e/ou no **WhatsApp** quando encontra uma passagem mais barata. Roda sozinho no **GitHub Actions**, a cada 2 horas, mesmo com o Mac desligado.

> **Repositório público, dados criptografados.** As viagens (`config/rotas.yaml`) e o histórico (`data/`) só vão para o GitHub criptografados (arquivos `.cripto`, AES via `cryptography`/Fernet). No GitHub, os registros de cada execução mostram só contagens, sem rotas, datas ou preços. As chaves ficam nos Secrets do GitHub e no `.env` do Mac.

## Como funciona

```
config/rotas.yaml ─► gera as buscas (rota × datas) ─► consulta as fontes
                                                          │
           data/historico_precos.csv ◄── grava ◄──────────┤
                                                          ▼
                              regras de alerta ─► Telegram / WhatsApp
```

**Regras de alerta.** Basta uma regra disparar. O monitor compara o melhor preço da busca, considerando todas as fontes:

1. **Preço-alvo**: o preço ficou menor ou igual ao `preco_alvo` da rota. Essa regra vale desde a primeira leitura.
2. **Menor preço já visto**: o preço bateu o mínimo do histórico daquela busca. Só começa a valer depois de `min_observacoes` leituras.
3. **Queda vs. média**: o preço ficou `queda_pct`% abaixo da média dos últimos `janela_media_dias`.

O mesmo alerta não se repete por `reavisar_apos_horas`. A exceção é quando o preço cai mais.

## Fontes

| Fonte | Como acessa | Confiabilidade |
|---|---|---|
| `google_flights` | API da SerpApi (dados do Google Flights) | **Alta.** É a fonte principal e cobre quase todas as companhias. |
| `skiplagged` | Endpoint JSON do próprio site, sem API oficial | Experimental. Preço em US$ convertido para R$. Traz horários, escalas e vendedores; marca ida e volta em 2 bilhetes e *hidden-city*. Em 20/11/2026 trouxe ~300 opções para REC→GRU. |
| `latam`, `gol`, `azul` | Navegador automatizado (Playwright) | **Baixa no GitHub Actions.** Esses sites bloqueiam robôs vindos de servidores. |
| `decolar`, `kayak`, `skyscanner` | Navegador automatizado (Playwright) | **Baixa no GitHub Actions**, pelo mesmo motivo. Em 20/11/2026, no Mac, só a `kayak` funcionou; as outras 5 bloquearam. |

As fontes por navegador são **plugáveis e tolerantes a falha**. Quando um site bloqueia, o monitor registra o bloqueio, guarda um print em `data/debug/` (só no Mac; no GitHub os prints não são publicados) e segue com as outras fontes. Os modelos de URL ficam em `config/sites.yaml`. Eles foram montados a partir do formato público de cada site, mas **ainda não foram validados** (`validado: false`). Abra uma URL gerada no navegador para conferir e ajuste o modelo se for preciso.

> **Orçamento da SerpApi:** o plano gratuito dá 250 buscas por mês, renovadas no dia em que a conta foi criada. Cada combinação de datas conta como 1 busca. O Google Flights só roda a cada `google_flights_a_cada_horas` (8h = 3x/dia). Em cada rodada, o monitor consulta o saldo real na SerpApi (consulta gratuita) e libera o orçamento (`orcamento_serpapi_mes`) aos poucos até a renovação, com no máximo 5 buscas por rodada. Ele faz rodízio: as buscas há mais tempo sem atualização vão primeiro. Isso dá cerca de 8 buscas no Google Flights por dia. Para vigiar muitas datas, reduza as combinações ou assine um plano maior.

## Configuração (uma vez)

### 1. Chaves e contas
- **SerpApi**: crie uma conta em serpapi.com e copie a API key.
- **Telegram**: no app, fale com o **@BotFather**, use `/newbot` e guarde o *token*. Depois mande qualquer mensagem para o seu bot e abra `https://api.telegram.org/bot<TOKEN>/getUpdates`. O número em `"chat":{"id": ...}` é o seu `TELEGRAM_CHAT_ID`.
- **WhatsApp (CallMeBot, gratuito para uso pessoal)**: siga as instruções de callmebot.com/blog/free-api-whatsapp-messages. Você adiciona o número do bot e manda *"I allow callmebot to send me messages"*, e o bot responde com a sua apikey. O CallMeBot não é a API oficial do WhatsApp. Funciona bem para alertas pessoais, mas pode ter instabilidade.

### 2. Rodar no Mac (teste)
```bash
cd "Monitor de Passagens"
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium        # só se for usar cias/agências
cp .env.example .env                         # preencha as chaves
export PYTHONPATH=src

python -m monitor --listar                   # confere as buscas geradas
python -m monitor --testar-notificacao       # testa Telegram/WhatsApp
python -m monitor --simular                  # busca de verdade, sem notificar nem gravar
python -m monitor --simular --fontes latam --rota exemplo-so-ida-sites   # testa um site
```

### 3. Subir para o GitHub
1. Crie a chave de criptografia (`PYTHONPATH=src .venv/bin/python -m monitor.cofre nova-chave`) e **guarde uma cópia em lugar seguro**: sem ela, o histórico criptografado não pode ser lido. Depois rode `python -m monitor.cofre fechar tudo` para gerar os `.cripto`.
   O repositório pode ser **público**, o que dá minutos ilimitados no Actions. Em repositório privado, a cota grátis é de 2.000 min/mês, e cada rodada leva ~10 min com 4 viagens (estimativa de 20/11/2026).
2. Envie esta pasta:
   ```bash
   git init && git add . && git commit -m "monitor de passagens"
   git branch -M main
   git remote add origin https://github.com/<seu-usuario>/<repo>.git
   git push -u origin main
   ```
3. Abra **Settings → Secrets and variables → Actions** e cadastre `MONITOR_CHAVE`, `SERPAPI_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `CALLMEBOT_PHONE` e `CALLMEBOT_APIKEY`.
4. Abra **Actions → Monitor de passagens → Run workflow**, marque "Só simular" na primeira vez e veja o resumo da execução.

A partir daí ele roda sozinho. Cada execução abre o cofre, busca, e grava o histórico de volta em `data/` criptografado (um arquivo por dia; só o dia de hoje muda). O GitHub só manda e-mail de falha quando **todas** as buscas da execução falham.

## Painel no Mac
Dois cliques no aplicativo **Abrir painel** (na Mesa do Mac) abrem o painel no navegador, sem Terminal. Para fechar, use o botão **Fechar painel** na barra lateral.
O aplicativo só chama `painel/iniciar.sh`. Ele fica no disco interno porque o SSD (exFAT) não aceita aplicativos do Mac. Para recriá-lo, veja `CONTEXTO CLAUDE/CONTEXTO.md`.
Na primeira vez, instale as bibliotecas do painel: `.venv/bin/pip install -r requirements-painel.txt`. Elas ficam só no Mac e não são usadas no GitHub Actions.

| Tela | Para quê |
|---|---|
| Minhas viagens | Cadastrar, editar, ligar/desligar e excluir viagens (ida e volta ou só ida, datas no calendário) |
| Preços ao longo do tempo | Gráfico de cada combinação de datas, menor preço já visto e seu preço de aviso |
| Melhor dia e horário | Mapa dia da semana × horário da coleta (confiável depois de 3–4 semanas de dados) |
| Histórico de buscas | Todas as leituras com horários, escalas, vendedor e avisos; baixa em planilha |
| Pausar e parar | Liga/desliga viagens e fontes (`config/pausas.yaml`); mostra fontes descansando após bloqueio |
| Testar uma viagem | Busca de verdade agora, sem avisar nem gravar |
| Salvar e publicar | Envia as mudanças ao GitHub (a partir da etapa 7) |

O painel grava em `config/rotas.yaml` só a parte `rotas:`. Antes de gravar, ele valida tudo; se algo estiver errado, não grava e mostra o motivo.

## Editando rotas sem o painel
Tudo fica em `config/rotas.yaml`, e o próprio arquivo explica os formatos de data. Pelo site do GitHub dá para editar direto (ícone de lápis), sem precisar do Mac.

## Estrutura
```
config/        rotas.yaml (suas rotas) · sites.yaml (URLs dos sites) · pausas.yaml (fontes desligadas no painel)
painel/        painel no Mac (app.py) e iniciar.sh (chamado pelo app "Abrir painel")
src/monitor/   código (fontes/, regras.py, notificacao.py, historico.py, __main__.py)
data/          histórico de preços e estado (gerados automaticamente)
tests/         testes da lógica:  PYTHONPATH=src pytest -q
.github/       workflow agendado
```

## Limites honestos
- O Google Flights (SerpApi) é a única fonte que deve funcionar de forma consistente no GitHub Actions.
- Companhias e agências podem funcionar melhor rodando no seu Mac, porque o IP residencial sofre menos bloqueio. Mesmo assim, o layout dos sites muda e exige manutenção do `sites.yaml`.
- O preço de ida e volta é o total da viagem, e o preço por navegador é o menor "R$" visível na página. Use `seletor_preco` no `sites.yaml` para deixar essa leitura mais precisa.
- Raspar sites pode violar os termos de uso deles. Use com moderação, para uso pessoal.
