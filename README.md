# Agente DOU - Deploy no Render.com

Backend Flask que monitora o Diario Oficial da Uniao (secao FIXA, data = dia atual)
e filtra materias por palavras-chave EDITAVEIS na propria interface.

## Estrutura
    app.py             backend Flask + API (le PORT do ambiente)
    static/index.html  interface com formulario de palavras-chave (chips)
    requirements.txt   flask, requests, gunicorn
    Procfile           comando de start (gunicorn)
    render.yaml        deploy automatico (Blueprint do Render)
    runtime.txt        Python 3.12.4
    .gitignore

## DEPLOY NO RENDER - passo a passo

### Via GitHub (recomendado)
1. Crie um repositorio no GitHub e suba TODOS estes arquivos (git push).
2. Acesse https://dashboard.render.com -> New -> Web Service.
3. Conecte o repositorio.
4. O Render detecta o render.yaml. Caso peca manual:
     - Environment:    Python
     - Build Command:  pip install -r requirements.txt
     - Start Command:  gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --workers 2
     - Plan:           Free
5. Create Web Service. Em ~2 min sai a URL publica: https://SEU-APP.onrender.com

## Comandos git (na pasta do projeto)
    git init
    git add .
    git commit -m "Agente DOU - deploy inicial"
    git branch -M main
    git remote add origin https://github.com/SEU-USUARIO/SEU-REPO.git
    git push -u origin main

## Observacoes
- PORTA: o Render injeta PORT; o app.py ja le os.environ['PORT'].
- HEALTHCHECK: /healthz responde {"status":"ok"}.
- PERSISTENCIA: disco do plano free e EFEMERO. Para persistir, use Postgres
  do Render ou disco pago + DB_FILE=/var/data/agents_db.json.
- PLANO FREE dorme apos ~15 min sem trafego (acorda em ~30s).

## Rodar localmente
    pip install -r requirements.txt
    python app.py
    # http://localhost:8000
