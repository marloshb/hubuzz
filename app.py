"""Hubuzz - Backend Flask. Busca REAL no DOU via endpoint /leiturajornal (JSON)."""
import os
import re
import json
import time
import datetime as dt
import logging
import traceback

import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hubuzz")

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

DOU_URL = "https://www.in.gov.br/leiturajornal"
TIMEOUT = 45
RETRIES = 3
USER_AGENTS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
    ("Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"),
]
PARAMS_RE = re.compile(r'id="params"[^>]*>(.*?)</script>', re.S)


@app.get("/")
def hub():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/agente")
def agente():
    return send_from_directory(app.static_folder, "agente.html")


@app.get("/api/health")
def health():
    return jsonify(status="ok", time=dt.datetime.utcnow().isoformat() + "Z")


def _fetch_articles(secao, data):
    last_err = None
    for attempt in range(RETRIES):
        headers = {
            "User-Agent": USER_AGENTS[attempt % len(USER_AGENTS)],
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "pt-BR,pt;q=0.9",
        }
        params = {"data": data, "secao": secao}
        try:
            resp = requests.get(DOU_URL, params=params, headers=headers, timeout=TIMEOUT)
            if resp.status_code == 200:
                m = PARAMS_RE.search(resp.text or "")
                if not m:
                    last_err = "bloco de dados (#params) nao encontrado"
                else:
                    arr = json.loads(m.group(1).strip()).get("jsonArray", [])
                    return arr, resp.url, None
            else:
                last_err = "HTTP %s" % resp.status_code
        except requests.RequestException as exc:
            last_err = exc.__class__.__name__
        if attempt < RETRIES - 1:
            time.sleep(1.2 * (attempt + 1))
    return [], DOU_URL, last_err


def _match(article, termo):
    blob = " ".join([
        str(article.get("title", "")), str(article.get("subTitulo", "")),
        str(article.get("content", "")), str(article.get("hierarchyStr", "")),
    ]).lower()
    return termo.lower() in blob


@app.post("/api/dou/buscar")
def buscar_dou():
    try:
        payload = request.get_json(silent=True) or {}
        keywords = [k.strip() for k in payload.get("keywords", []) if str(k).strip()]
        if not keywords:
            return jsonify(ok=False, error="Informe ao menos uma palavra-chave."), 400

        secao = payload.get("secao", "do3")
        data = payload.get("data") or dt.date.today().strftime("%d-%m-%Y")

        artigos, url, erro = _fetch_articles(secao, data)
        if erro and not artigos:
            return jsonify(ok=False,
                           error="DOU indisponivel no momento (%s). Tente novamente." % erro,
                           consulta={"secao": secao, "data": data, "fonte": url}), 200

        resultados = []
        for termo in keywords:
            hits = []
            for a in artigos:
                if _match(a, termo):
                    ut = a.get("urlTitle", "")
                    hits.append({
                        "titulo": a.get("title", "") or a.get("titulo", ""),
                        "orgao": a.get("hierarchyStr", ""),
                        "tipo": a.get("artType", ""),
                        "pagina": a.get("numberPage", ""),
                        "data_pub": a.get("pubDate", ""),
                        "trecho": (a.get("content", "") or "")[:300],
                        "link": ("https://www.in.gov.br/web/dou/-/" + ut) if ut else url,
                    })
            resultados.append({"termo": termo, "encontrou": len(hits) > 0,
                               "total": len(hits), "materias": hits[:20]})

        return jsonify(
            ok=True,
            consulta={"keywords": keywords, "secao": secao, "data": data,
                      "fonte": url, "materias_varridas": len(artigos)},
            resultados=resultados,
            nota=("Consulta real ao DOU via endpoint /leiturajornal (JSON). "
                  "Varre todas as materias da secao na data e conta ocorrencias reais."),
        )
    except Exception:
        logger.error("Erro inesperado:\n%s", traceback.format_exc())
        return jsonify(ok=False, error="Erro interno ao processar a busca."), 500


@app.errorhandler(404)
def nf(e):
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error="Rota nao encontrada."), 404
    return e


@app.errorhandler(500)
def ie(e):
    return jsonify(ok=False, error="Erro interno do servidor."), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
