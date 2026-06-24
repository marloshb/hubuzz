# Hubuzz - Busca DOU por periodo

Aba 'Busca DOU' com **campo de range de datas** (De / Ate) e atalhos (Hoje, 7, 15, 30 dias).
O backend varre `/leiturajornal` dia a dia no intervalo, pula fim de semana e edicoes vazias, e soma as ocorrencias reais por palavra-chave.

## Deploy (Render)
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app --bind 0.0.0.0:$PORT --timeout 180 --workers 2`
- index.html e agente.html ficam em `static/`.
