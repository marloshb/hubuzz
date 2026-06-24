"""Hubuzz - Backend Flask. Busca REAL no DOU via /leiturajornal (JSON).

Robustez contra 502/anti-bot do in.gov.br:
- Sessao HTTP reutilizada + retry com backoff exponencial.
- Rotacao de User-Agent e header Referer.
- Fallback de parsing quando o bloco 'params' nao vem inline.
- Cache em memoria por (secao,data) para nao remartelar a origem.
"""
from __future__ import annotations

import os
import re
import json
import html
import time
import random
import logging
import datetime as dt
from typing import Any

import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hubuzz")

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

LEITURA_URL = "https://www.in.gov.br/leiturajornal"
TIMEOUT = 45
MAX_RETRIES = 4
MAX_MATERIAS_POR_TERMO = 30

_UAS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
     "Gecko/20100101 Firefox/125.0"),
]

_TAG_RE = re.compile(r"<[^>]+>")
_cache: dict[str, list[dict[str, Any]]] = {}
_session = requests.Session()


def _headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(_UAS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Referer": "https://www.in.gov.br/leiturajornal",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def _strip(text: str) -> str:
    return _TAG_RE.sub(" ", text or "").strip()


def _fetch_html(secao: str, data: str) -> str:
    """GET com retry/backoff. Trata 502/503/429 como transitorios."""
    last_exc: Exception | None = None
    for tentativa in range(1, MAX_RETRIES + 1):
        try:
            resp = _session.get(
                LEITURA_URL,
                params={"secao": secao, "data": data},
                headers=_headers(),
                timeout=TIMEOUT,
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"{resp.status_code}", response=resp)
            resp.raise_for_status()
            if "params" not in resp.text and "jsonArray" not in resp.text:
                # corpo veio sem o payload esperado (pagina de erro/anti-bot)
                raise requests.HTTPError("payload ausente", response=resp)
            return resp.text
        except requests.RequestException as exc:
            last_exc = exc
            if tentativa < MAX_RETRIES:
                espera = min(2 ** tentativa + random.uniform(0, 0.6), 9.0)
                logger.warning("DOU %s/%s falhou (tentativa %d/%d): %s. Retry em %.1fs",
                               secao, data, tentativa, MAX_RETRIES, exc, espera)
                time.sleep(espera)
            else:
                logger.error("DOU %s/%s esgotou retries: %s", secao, data, exc)
    raise last_exc if last_exc else RuntimeError("Falha desconhecida no DOU")


def _carregar_secao(secao: str, data: str) -> list[dict[str, Any]]:
    chave = f"{secao}|{data}"
    if chave in _cache:
        return _cache[chave]

    texto = _fetch_html(secao, data)
    match = re.search(r'id="params"[^>]*>(.*?)</script>', texto, re.S)
    if not match:
        match = re.search(r'id="params"[^>]*>([^<]+)<', texto)
    if not match:
        # fallback: tenta achar o jsonArray cru no corpo
        bruto = re.search(r'"jsonArray"\s*:\s*(\[.*?\])\s*[,}]', texto, re.S)
        if bruto:
            materias = json.loads(bruto.group(1))
            _cache[chave] = materias
            return materias
        raise ValueError("Estrutura do DOU mudou: bloco 'params' nao encontrado.")

    payload = json.loads(html.unescape(match.group(1).strip()))
    materias = payload.get("jsonArray", []) or []
    _cache[chave] = materias
    return materias


def _materia_publica(m: dict[str, Any]) -> dict[str, Any]:
    titulo = _strip(m.get("title") or m.get("titulo") or "Sem titulo")
    url_title = m.get("urlTitle", "")
    url = f"https://www.in.gov.br/web/dou/-/{url_title}" if url_title else LEITURA_URL
    return {
        "id": str(m.get("pubOrder", "")),
        "titulo": titulo[:160],
        "orgao": _strip(m.get("subTitulo") or m.get("artType") or "")[:120],
        "resumo": _strip(m.get("content", ""))[:280],
        "url": url,
    }


@app.get("/")
def hub() -> Any:
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health() -> Any:
    return jsonify(status="ok", time=dt.datetime.utcnow().isoformat() + "Z")


@app.post("/api/dou/buscar")
def buscar_dou() -> Any:
    """Varre TODAS as materias da secao/data e conta ocorrencias reais por termo."""
    try:
        body = request.get_json(silent=True) or {}
        keywords = [k.strip() for k in body.get("keywords", []) if str(k).strip()]
        if not keywords:
            return jsonify(ok=False, error="Informe ao menos uma palavra-chave."), 400

        secao = body.get("secao", "do3")
        data = body.get("data") or dt.date.today().strftime("%d-%m-%Y")

        try:
            materias = _carregar_secao(secao, data)
        except requests.HTTPError as exc:
            code = getattr(getattr(exc, "response", None), "status_code", 502)
            return jsonify(
                ok=False,
                error=(f"O DOU respondeu {code} apos {MAX_RETRIES} tentativas para "
                       f"{secao}/{data}. O portal aplica bloqueio temporario; "
                       "aguarde alguns minutos e tente de novo."),
            ), 502
        except requests.RequestException:
            return jsonify(ok=False, error="Falha de conexao com o DOU. Tente novamente."), 502
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 502

        indexado = []
        for m in materias:
            blob = (_strip(m.get("title", "")) + " " +
                    _strip(m.get("titulo", "")) + " " +
                    _strip(m.get("content", ""))).lower()
            indexado.append((blob, m))

        resultados = []
        for termo in keywords:
            tl = termo.lower()
            achados = [m for blob, m in indexado if tl in blob]
            resultados.append({
                "termo": termo,
                "ocorrencias": len(achados),
                "materias": [_materia_publica(m) for m in achados[:MAX_MATERIAS_POR_TERMO]],
            })

        return jsonify(
            ok=True,
            consulta={"secao": secao, "data": data, "total_materias": len(materias)},
            resultados=resultados,
            nota=("Consulta real ao DOU via endpoint /leiturajornal (JSON). "
                  "Varre todas as materias da secao na data e conta ocorrencias reais."),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erro inesperado na busca DOU")
        return jsonify(ok=False, error=f"Erro interno: {exc.__class__.__name__}."), 500


@app.errorhandler(404)
def nf(e):
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error="Rota nao encontrada."), 404
    return e


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
