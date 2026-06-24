"""Hubuzz - Backend Flask. Busca REAL no DOU varrendo /leiturajornal por intervalo de datas."""
import os
import re
import json
import time
import unicodedata
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
MAX_DIAS_RANGE = 45          # teto de seguranca do intervalo
USER_AGENTS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
]
PARAMS_RE = re.compile(r'id="params"[^>]*>(.*?)</script>', re.S)


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def _parse_iso(value):
    return dt.datetime.strptime(value, "%Y-%m-%d").date()


@app.get("/")
def hub():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/agente")
def agente():
    return send_from_directory(app.static_folder, "agente.html")


@app.get("/api/health")
def health():
    return jsonify(status="ok", time=dt.datetime.utcnow().isoformat() + "Z")


def _fetch_one(data_ddmmyyyy, secao):
    """Baixa uma edicao e retorna (lista_de_artigos, url). Lista vazia se nao houver."""
    last = "sem resposta"
    for ua in USER_AGENTS:
        headers = {"User-Agent": ua, "Accept": "text/html",
                   "Accept-Language": "pt-BR,pt;q=0.9"}
        try:
            resp = requests.get(DOU_URL, params={"data": data_ddmmyyyy, "secao": secao},
                                headers=headers, timeout=TIMEOUT)
            if resp.status_code == 200:
                m = PARAMS_RE.search(resp.text or "")
                if m:
                    return json.loads(m.group(1).strip()).get("jsonArray", []), resp.url
                return [], resp.url
            last = "HTTP %s" % resp.status_code
        except requests.RequestException as exc:
            last = exc.__class__.__name__
        time.sleep(0.5)
    logger.warning("Falha ao buscar %s: %s", data_ddmmyyyy, last)
    return [], DOU_URL


def _match(article, termo_norm):
    blob = _norm(" ".join([
        str(article.get("title", "")), str(article.get("subTitulo", "")),
        str(article.get("content", "")), str(article.get("hierarchyStr", "")),
    ]))
    return termo_norm in blob


def _materia(article, fallback_url):
    ut = article.get("urlTitle", "")
    return {
        "titulo": article.get("title", "") or article.get("titulo", ""),
        "orgao": article.get("hierarchyStr", ""),
        "tipo": article.get("artType", ""),
        "pagina": article.get("numberPage", ""),
        "data_pub": article.get("pubDate", ""),
        "trecho": (article.get("content", "") or "")[:300],
        "link": ("https://www.in.gov.br/web/dou/-/" + ut) if ut else fallback_url,
    }


@app.post("/api/dou/buscar")
def buscar_dou():
    try:
        payload = request.get_json(silent=True) or {}
        keywords = [k.strip() for k in payload.get("keywords", []) if str(k).strip()]
        if not keywords:
            return jsonify(ok=False, error="Informe ao menos uma palavra-chave."), 400

        secao = payload.get("secao", "do3")

        # ----- resolve o intervalo de datas -----
        hoje = dt.date.today()
        try:
            d_to = _parse_iso(payload["date_to"]) if payload.get("date_to") else hoje
            d_from = _parse_iso(payload["date_from"]) if payload.get("date_from") else d_to
        except (ValueError, KeyError):
            return jsonify(ok=False, error="Datas invalidas (use AAAA-MM-DD)."), 400

        if d_from > d_to:
            d_from, d_to = d_to, d_from
        if (d_to - d_from).days > MAX_DIAS_RANGE:
            return jsonify(ok=False,
                           error="Intervalo muito grande (max %s dias)." % MAX_DIAS_RANGE), 400

        # ----- varre cada dia util do intervalo -----
        knorm = {k: _norm(k) for k in keywords}
        agg = {k: [] for k in keywords}
        total_materias = 0
        dias_uteis = 0
        dias_pulados = []
        fonte = DOU_URL

        cur = d_from
        while cur <= d_to:
            if cur.weekday() >= 5:          # sabado/domingo
                cur += dt.timedelta(days=1)
                continue
            ddmm = cur.strftime("%d-%m-%Y")
            artigos, url = _fetch_one(ddmm, secao)
            fonte = url
            if not artigos:
                dias_pulados.append(cur.isoformat())
            else:
                dias_uteis += 1
                total_materias += len(artigos)
                for a in artigos:
                    for k in keywords:
                        if _match(a, knorm[k]):
                            agg[k].append(_materia(a, url))
            cur += dt.timedelta(days=1)

        resultados = [{
            "termo": k,
            "encontrou": len(agg[k]) > 0,
            "total": len(agg[k]),
            "materias": agg[k][:40],
        } for k in keywords]

        return jsonify(
            ok=True,
            consulta={
                "keywords": keywords, "secao": secao,
                "date_from": d_from.isoformat(), "date_to": d_to.isoformat(),
                "dias_uteis": dias_uteis, "dias_pulados": dias_pulados,
                "materias_varridas": total_materias, "fonte": fonte,
            },
            resultados=resultados,
            nota=("Consulta real ao DOU (/leiturajornal). Varre cada dia util do intervalo, "
                  "soma as ocorrencias reais e ignora acentos/maiusculas. Fins de semana e "
                  "edicoes inexistentes sao pulados automaticamente."),
        )
    except Exception:
        logger.error("Erro inesperado:\n%s", traceback.format_exc())
        return jsonify(ok=False, error="Erro interno ao processar a busca."), 500


@app.errorhandler(404)
def nf(e):
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error="Rota nao encontrada."), 404
    return e


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
