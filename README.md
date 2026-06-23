# Hubuzz

Backend Flask: serve o Hub e busca no Diario Oficial da Uniao (DOU).

## Rotas
  GET  /                Hub (static/index.html)
  GET  /agente          Painel do Agente (static/agente.html)
  GET  /api/health      healthcheck
  POST /api/dou/buscar  busca no DOU

## Deploy no Render
1. Suba TODOS estes arquivos (mantendo a pasta static/).
2. dashboard.render.com -> New -> Blueprint -> selecione o repo.
3. O Render le o render.yaml e cria o servico. Apply.
4. ~2 min -> https://hubuzz.onrender.com
