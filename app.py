from __future__ import annotations

"""Hubuzz - Backend Flask. Busca REAL no DOU via /leiturajornal (JSON).

Compativel com o front index.html:
  POST /api/dou/buscar  body: {keywords[], secao, date_from, date_to}
  -> {ok, consulta:{...}, resultados:[{termo,encontrou,total,materias[]}], nota}

Robustez contra 502/anti-bot do in.gov.br:
  - sessao HTTP com Retry/backoff exponencial
  - rotacao de User-Agent + warm-up na home + Referer
"""

import os
import re
import json
import html
import time
import random
import logging
import unicodedata
import datetime as dt
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hubuzz")

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

DOU_BASE = "https://www.in.gov.br"
LEITURA_URL = DOU_BASE + "/leiturajornal"
TIMEOUT = 50
MAX_DIAS_RANGE = 31
MAX_MATERIAS_POR_TERMO = 40
PARAMS_RE = re.compile(r'id=["\']params["\'][^>]*>(.*?)</script>', re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")

UAS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

_cache: dict[str, list[dict[str, Any]]] = {}


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def _strip(text: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", str(text or ""))).strip()


def _field(item: dict[str, Any], *names: str) -> Any:
    for n in names:
        v = item.get(n)
        if v not in (None, ""):
            return v
    return ""


def _build_session(ua: str) -> requests.Session:
    s = requests.Session()
    retry = Retry(total=4, connect=3, read=3,
                  status_forcelist=(429, 500, 502, 503, 504),
                  backoff_factor=1.5, allowed_methods=frozenset(["GET"]),
                  raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    return s


def _fetch_one(ddmm: str, secao: str) -> tuple[list[dict[str, Any]], str]:
    """Baixa e parseia as materias de um dia (cache em memoria)."""
    chave = f"{secao}|{ddmm}"
    if chave in _cache:
        return _cache[chave], LEITURA_URL

    final_url = LEITURA_URL
    for ua in random.sample(UAS, len(UAS)):
        s = _build_session(ua)
        try:
            s.get(DOU_BASE + "/", timeout=TIMEOUT)        # warm-up
            time.sleep(random.uniform(0.3, 0.8))
            s.headers["Referer"] = DOU_BASE + "/"
            resp = s.get(LEITURA_URL, params={"data": ddmm, "secao": secao}, timeout=TIMEOUT)
            final_url = resp.url
            if resp.status_code != 200:
                time.sleep(random.uniform(0.8, 1.6))
                continue
            m = PARAMS_RE.search(resp.text or "")
            if not m:
                _cache[chave] = []
                return [], final_url
            payload = json.loads(html.unescape(m.group(1).strip()))
            materias = payload.get("jsonArray", []) or []
            _cache[chave] = materias
            return materias, final_url
        except requests.RequestException:
            time.sleep(random.uniform(0.6, 1.2))
        finally:
            s.close()
    return [], final_url


def _match(item: dict[str, Any], termo_norm: str) -> bool:
    blob = _norm(" ".join([
        _strip(_field(item, "title", "titulo")),
        _strip(_field(item, "subTitulo", "subtitle", "subtitulo")),
        _strip(_field(item, "content", "conteudo")),
        _strip(_field(item, "hierarchyStr", "hierarquia", "orgao")),
    ]))
    return termo_norm in blob


def _materia(item: dict[str, Any], url: str) -> dict[str, Any]:
    ut = item.get("urlTitle") or item.get("url_title") or ""
    link = f"{DOU_BASE}/web/dou/-/{ut}" if ut else (item.get("url") or url)
    content = _strip(_field(item, "content", "conteudo"))
    return {
        "titulo": _strip(_field(item, "title", "titulo")) or "(sem titulo)",
        "orgao": _strip(_field(item, "hierarchyStr", "hierarquia", "orgao")),
        "tipo_ato": _strip(_field(item, "artType", "tipo", "tipoAto")),
        "pagina": _field(item, "numberPage", "pagina"),
        "edicao": _field(item, "editionNumber", "edicao"),
        "data_pub": _field(item, "pubDate", "dataPublicacao"),
        "trecho": content[:400],
        "link": link,
    }


@app.get("/")
def hub() -> Any:
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health() -> Any:
    return jsonify(status="ok", time=dt.datetime.utcnow().isoformat() + "Z")


@app.post("/api/dou/buscar")
def buscar_dou() -> Any:
    """Varre cada dia util do intervalo e conta ocorrencias reais por termo."""
    try:
        body = request.get_json(silent=True) or {}
        keywords = [k.strip() for k in body.get("keywords", []) if str(k).strip()]
        if not keywords:
            return jsonify(ok=False, error="Informe ao menos uma palavra-chave."), 400

        secao = body.get("secao", "do3")
        hoje = dt.date.today()

        def _parse(v, default):
            if not v:
                return default
            try:
                return dt.date.fromisoformat(str(v)[:10])
            except ValueError:
                return default

        d_from = _parse(body.get("date_from"), hoje)
        d_to = _parse(body.get("date_to"), hoje)
        if d_from > d_to:
            d_from, d_to = d_to, d_from
        if (d_to - d_from).days > MAX_DIAS_RANGE:
            return jsonify(ok=False,
                           error=f"Intervalo muito grande (max {MAX_DIAS_RANGE} dias)."), 400

        knorm = {k: _norm(k) for k in keywords}
        agg: dict[str, list] = {k: [] for k in keywords}
        total_materias = 0
        dias_uteis = 0
        dias_pulados: list[str] = []
        fonte = LEITURA_URL

        cur = d_from
        while cur <= d_to:
            if cur.weekday() >= 5:                 # sabado/domingo
                cur += dt.timedelta(days=1)
                continue
            artigos, url = _fetch_one(cur.strftime("%d-%m-%Y"), secao)
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
            "materias": agg[k][:MAX_MATERIAS_POR_TERMO],
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
            nota=("Consulta real ao DOU (/leiturajornal, JSON). Varre cada dia util do "
                  "intervalo na secao e conta ocorrencias reais (titulo, subtitulo, "
                  "conteudo e orgao), com retry e rotacao de User-Agent contra 502."),
        )
    except Exception as exc:
        logger.exception("Erro inesperado na busca DOU")
        return jsonify(ok=False, error=f"Erro interno: {exc.__class__.__name__}."), 500


@app.errorhandler(404)
def nf(e):
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error="Rota nao encontrada."), 404
    return e


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
