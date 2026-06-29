# Hubuzz

Hub de Oportunidades + Agente de Busca no DOU.

## Estrutura
- `app.py` backend Flask (serve static/ e expoe a API do DOU)
- `static/index.html` frontend SPA (Dashboard, Kanban, Busca DOU ao vivo e por periodo)
- requirements.txt, Procfile, render.yaml, runtime.txt

## Rotas
- `/`         Painel
- `/health`   healthcheck
- `/buscar`   busca real no DOU (data unica ou intervalo)

## Variaveis de ambiente (Render)
- `DOU_PROXY` (opcional): proxy residencial p/ contornar bloqueio 502 do in.gov.br.

## Rodar local
```
pip install -r requirements.txt
python app.py
```
