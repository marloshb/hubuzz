# Hubuzz

Hub de Oportunidades + Agente de Busca no DOU.

## Rotas
- GET  /                painel
- GET  /api/health      healthcheck
- POST /api/dou/buscar  busca real no DOU (intervalo de datas)

## Render
start: gunicorn app:app --bind 0.0.0.0:$PORT
Variavel opcional DOU_PROXY p/ contornar 502 do in.gov.br.
