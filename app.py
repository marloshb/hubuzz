from __future__ import annotations

import os
import re
import json
import time
import random
import unicodedata
import datetime as dt
from typing import List, Dict, Any, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, request, jsonify, send_from_directory

LEITURA_URL = "https://www.in.gov.br/leiturajornal"
BASE_URL = "https://www.in.gov.br"
TIMEOUT = int(os.environ.get("DOU_TIMEOUT", "45"))
MAX_MATERIAS_POR_TERMO = int(os.environ.get("DOU_MAX_MATERIAS", "50"))
DOU_PROXY = os.environ.get("DOU_PROXY", "").strip()

_PARAMS_RE = re.compile(r'id=["\']params["\'][^>]*>(.*?)</script>', re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

app = Flask(__name__, static_folder="static", static_url_path="")


def _strip(text):
    return _TAG_RE.sub(" ", text or "").strip()


def _norm(text):
    text = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def _build_session(user_agent):
    session = requests.Session()
    retry = Retry(
        total=4, connect=2, read=2,
        status_forcelist=(429, 500, 502, 503, 504),
        backoff_factor=1.5,
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Referer": LEITURA_URL,
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    })
    if DOU_PROXY:
        session.proxies.update({"http": DOU_PROXY, "https": DOU_PROXY})
    return session


def _fetch_jsonarray(secao, data):
    last = "sem resposta"
    for ua in random.sample(_USER_AGENTS, len(_USER_AGENTS)):
        session = _build_session(ua)
        try:
            session.get(BASE_URL + "/", timeout=TIMEOUT)
            time.sleep(random.uniform(0.3, 0.7))
            resp = session.get(LEITURA_URL, params={"data": data, "secao": secao}, timeout=TIMEOUT)
            if resp.status_code == 200:
                match = _PARAMS_RE.search(resp.text or "")
                if match:
                    try:
                        payload = json.loads(match.group(1).strip())
                        return payload.get("jsonArray", []), resp.url, "ok"
                    except json.JSONDecodeError:
                        last = "json invalido"
                        continue
                return [], resp.url, "vazio"
            last = "HTTP %d" % resp.status_code
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(random.uniform(0.8, 1.6))
                continue
        except requests.RequestException as exc:
            last = exc.__class__.__name__
            time.sleep(random.uniform(0.5, 1.2))
        finally:
            session.close()
    status = "bloqueado" if ("HTTP" in last or "json" in last) else "erro"
    return [], LEITURA_URL, status


def _materia_publica(m):
    titulo = _strip(m.get("title") or m.get("titulo") or "Sem titulo")
    url_title = m.get("urlTitle", "")
    url = ("https://www.in.gov.br/web/dou/-/%s" % url_title) if url_title else LEITURA_URL
    conteudo = _strip(m.get("content", ""))
    return {
        "id": str(m.get("classificacao") or m.get("id") or ""),
        "titulo": titulo,
        "orgao": _strip(m.get("hierarchyStr", "")),
        "tipo": _strip(m.get("artType", "")),
        "pagina": m.get("numberPage", ""),
        "edicao": m.get("editionNumber", "") or m.get("numero", ""),
        "data_pub": m.get("pubDate", ""),
        "trecho": conteudo[:300],
        "link": url,
    }


def _daterange(d0, d1):
    cur = d0
    while cur <= d1:
        if cur.weekday() < 5:  # dias uteis
            yield cur
        cur += dt.timedelta(days=1)


@app.get("/health")
def health():
    return jsonify(ok=True, proxy=bool(DOU_PROXY))


@app.post("/buscar")
def buscar():
    try:
        body = request.get_json(silent=True) or {}
        keywords = [k.strip() for k in body.get("keywords", []) if str(k).strip()]
        if not keywords:
            return jsonify(ok=False, error="Informe ao menos uma palavra-chave."), 400

        secao = (body.get("secao") or "do3").lower()

        # Suporta data unica OU intervalo (date_from/date_to em dd-mm-aaaa)
        date_from = body.get("date_from") or body.get("data")
        date_to = body.get("date_to") or body.get("data")
        if not date_from:
            date_from = date_to = dt.date.today().strftime("%d-%m-%Y")
        if not date_to:
            date_to = date_from

        def parse(s):
            return dt.datetime.strptime(s, "%d-%m-%Y").date()

        try:
            d0, d1 = parse(date_from), parse(date_to)
        except ValueError:
            return jsonify(ok=False, error="Datas invalidas (use dd-mm-aaaa)."), 400
        if d1 < d0:
            d0, d1 = d1, d0

        kn = {k: _norm(k) for k in keywords}
        agg = {k: [] for k in keywords}
        dias_com_edicao, dias_sem = 0, []

        for dia in _daterange(d0, d1):
            ds = dia.strftime("%d-%m-%Y")
            materias, _url, status = _fetch_jsonarray(secao, ds)
            if status != "ok" or not materias:
                dias_sem.append(ds)
                continue
            dias_com_edicao += 1
            for m in materias:
                blob = _norm(" ".join([
                    str(m.get("title", "")), str(m.get("subTitulo", "")),
                    str(m.get("content", "")), str(m.get("hierarchyStr", "")),
                ]))
                for termo in keywords:
                    if kn[termo] in blob:
                        agg[termo].append(m)

        resultados = [{
            "termo": t,
            "total": len(agg[t]),
            "encontrou": bool(agg[t]),
            "materias": [_materia_publica(m) for m in agg[t][:MAX_MATERIAS_POR_TERMO]],
        } for t in keywords]

        return jsonify(
            ok=True,
            consulta={"secao": secao, "date_from": d0.strftime("%d-%m-%Y"),
                      "date_to": d1.strftime("%d-%m-%Y"),
                      "dias_com_edicao": dias_com_edicao, "dias_sem": dias_sem},
            resultados=resultados,
        )
    except Exception as exc:
        return jsonify(ok=False, error="Falha interna: %s" % exc), 500


@app.get("/")
def index():
    static_dir = app.static_folder or "static"
    if os.path.exists(os.path.join(static_dir, "index.html")):
        return send_from_directory(static_dir, "index.html")
    return jsonify(ok=True, service="Hubuzz / Agente DOU", endpoints=["/buscar", "/health"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
