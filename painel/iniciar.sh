#!/bin/zsh
# Inicia o painel em segundo plano (sem Terminal) e abre o navegador.
# Chamado pelo aplicativo "Abrir painel.app". Se o painel já estiver aberto, só abre o navegador.
cd "$(dirname "$0")/.."
# o app da Mesa roda com um PATH mínimo: inclui o Homebrew (gh, git)
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
PORTA=8501
URL="http://localhost:$PORTA"
if ! curl -s "$URL/_stcore/health" | grep -q ok; then
  PYTHONPATH=src nohup .venv/bin/python -m streamlit run painel/app.py \
    --server.port $PORTA --server.headless true --browser.gatherUsageStats false \
    --server.fileWatcherType none --client.toolbarMode minimal > painel/painel.log 2>&1 &
  for i in {1..60}; do
    curl -s "$URL/_stcore/health" | grep -q ok && break
    sleep 1
  done
fi
[ -z "$NAO_ABRIR_NAVEGADOR" ] && open "$URL"
exit 0
