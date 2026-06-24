"""
Hubuzz - Backend unificado (Flask).
Rotas:
  GET  /               -> Hub completo (static/index.html)
  GET  /api/health     -> healthcheck {"status":"ok"}
  POST /api/dou/buscar -> busca no DOU (SEMPRE responde JSON, nunca HTML)
"""
from __future__ import annotations

import os
import datetime as dt
import logging
import traceback
from typing import Any

import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hubuzz")

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

DOU_BASE = "https://www.in.gov.br/consulta/-/buscar/dou"
TIMEOUT = 20


@app.get("/")
def hub() -> Any:
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health() -> Any:
    return jsonify(status="ok", time=dt.datetime.utcnow().isoformat() + "Z")


@app.post("/api/dou/buscar")
def buscar_dou() -> Any:
    """Busca publicacoes no DOU. SEMPRE retorna JSON, mesmo em erro."""
    try:
        payload = request.get_json(silent=True) or {}
        keywords = [k.strip() for k in payload.get("keywords", []) if str(k).strip()]
        if not keywords:
            return jsonify(ok=False, error="Informe ao menos uma palavra-chave."), 400

        secao = payload.get("secao", "do3")
        dias = int(payload.get("dias", 7))
        desde = (dt.date.today() - dt.timedelta(days=dias)).strftime("%d-%m-%Y")
        ate = dt.date.today().strftime("%d-%m-%Y")

        resultados: list[dict[str, Any]] = []
        avisos: list[str] = []
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml",
        }

        for termo in keywords:
            item: dict[str, Any] = {"termo": termo, "status": None,
                                    "fonte": None, "encontrou": False, "tamanho": 0}
            try:
                params = {"q": termo, "s": secao, "exactDate": "personalizado",
                          "publishFrom": desde, "publishTo": ate, "delta": 20}
                resp = requests.get(DOU_BASE, params=params, headers=headers, timeout=TIMEOUT)
                texto = resp.text or ""
                item["status"] = resp.status_code
                item["fonte"] = resp.url
                item["encontrou"] = termo.lower() in texto.lower()
                item["tamanho"] = len(texto)
                if resp.status_code >= 400:
                    avisos.append(f"'{termo}': DOU respondeu {resp.status_code}.")
            except requests.Timeout:
                avisos.append(f"'{termo}': tempo esgotado ao consultar o DOU.")
            except requests.RequestException as exc:
                avisos.append(f"'{termo}': falha de conexao ({exc.__class__.__name__}).")
            resultados.append(item)

        return jsonify(
            ok=True,
            consulta={"keywords": keywords, "secao": secao, "periodo": [desde, ate]},
            resultados=resultados,
            avisos=avisos,
            nota=("O DOU nao oferece API JSON oficial; esta consulta usa o "
                  "buscador publico e retorna o link da pesquisa para conferencia."),
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
