import os
"""
Hubuzz - Backend unificado (Opcao B)
Serve, na MESMA aplicacao:
  - GET  /            -> Hub de Oportunidades (static/index.html)
  - GET  /agente      -> Painel do Agente DOU (static/agente.html)
  - GET  /api/health  -> healthcheck
  - POST /api/dou/buscar -> busca real no Diario Oficial da Uniao (DOU)

Deploy no Render: gunicorn app:app
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hubuzz")

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

DOU_API = "https://www.in.gov.br/consulta/-/buscar/dou"
REQUEST_TIMEOUT = 20


# --------------------------- Paginas estaticas -----------------------------
@app.get("/")
def hub() -> Any:
    return send_from_directory(app.static_folder, "index.html")


@app.get("/agente")
def agente() -> Any:
    return send_from_directory(app.static_folder, "agente.html")


# ------------------------------- API ---------------------------------------
@app.get("/api/health")
def health() -> Any:
    return jsonify(status="ok", time=dt.datetime.utcnow().isoformat() + "Z")


@app.post("/api/dou/buscar")
def buscar_dou() -> Any:
    """Busca publicacoes no DOU a partir de palavras-chave.

    Body JSON: {"keywords": ["...", "..."], "secao": "do1", "dias": 7}
    """
    payload = request.get_json(silent=True) or {}
    keywords = [k.strip() for k in payload.get("keywords", []) if k.strip()]
    if not keywords:
        return jsonify(error="Informe ao menos uma palavra-chave."), 400

    secao = payload.get("secao", "do1")
    dias = int(payload.get("dias", 7))
    desde = (dt.date.today() - dt.timedelta(days=dias)).strftime("%d-%m-%Y")
    ate = dt.date.today().strftime("%d-%m-%Y")

    resultados: list[dict[str, Any]] = []
    erros: list[str] = []

    for termo in keywords:
        try:
            params = {
                "q": termo,
                "s": secao,
                "exactDate": "personalizado",
                "publishFrom": desde,
                "publishTo": ate,
                "delta": 20,
            }
            headers = {"User-Agent": "Mozilla/5.0 (compatible; Hubuzz/1.0)"}
            resp = requests.get(
                DOU_API, params=params, headers=headers, timeout=REQUEST_TIMEOUT
            )
            resp.raise_for_status()
            resultados.append(
                {
                    "termo": termo,
                    "status": resp.status_code,
                    "fonte": resp.url,
                    "tamanho_resposta": len(resp.text),
                }
            )
        except requests.RequestException as exc:
            logger.warning("Falha ao buscar '%s': %s", termo, exc)
            erros.append(f"{termo}: {exc}")

    return jsonify(
        consulta={"keywords": keywords, "secao": secao, "periodo": [desde, ate]},
        resultados=resultados,
        erros=erros,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))  # noqa
