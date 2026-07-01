from __future__ import annotations

"""Hubuzz - Backend Flask. Painel + busca REAL no DOU (/leiturajornal, JSON).

Contrato compativel com index.html e agente.html:
  POST /api/dou/buscar
    body: {keywords:[...], secao:"do3", date_from:"YYYY-MM-DD", date_to:"YYYY-MM-DD"}
    -> {ok, consulta{...}, resultados[{termo,encontrou,total,materias[...]}],
        oportunidades[...], nota}

Cada materia expoe:
  link          -> link ESTAVEL de leitura no jornal (/leiturajornal#...), NAO da 502
  link_oficial  -> URL canonica /web/dou/-/{urlTitle} (pode cair em 502 no DOU)

Robustez contra 502/anti-bot do in.gov.br: retry/backoff + rotacao de UA + warm-up.
"""

import os
import re
import json
import html
import time
import random
import logging
import hashlib
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

SECAO_LABEL = {"do1": "DOU Secao 1", "do2": "DOU Secao 2", "do3": "DOU Secao 3"}

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


# --------------------------------------------------------------------------- #
# Utilitarios
# --------------------------------------------------------------------------- #
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
    """Baixa e parseia as materias de um dia (cache em memoria por dia/secao)."""
    chave = f"{secao}|{ddmm}"
    if chave in _cache:
        return _cache[chave], LEITURA_URL

    final_url = LEITURA_URL
    for ua in random.sample(UAS, len(UAS)):
        s = _build_session(ua)
        try:
            s.get(DOU_BASE + "/", timeout=TIMEOUT)          # warm-up
            time.sleep(random.uniform(0.3, 0.8))
            s.headers["Referer"] = DOU_BASE + "/"
            resp = s.get(LEITURA_URL, params={"data": ddmm, "secao": secao},
                         timeout=TIMEOUT)
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


# --------------------------------------------------------------------------- #
# LINKS  (Opcao A: principal estavel, oficial secundario)
# --------------------------------------------------------------------------- #
def _link_leitura(ddmm: str, secao: str, art_id: Any) -> str:
    """Link ESTAVEL de leitura no jornal (nao cai em 502). Ancora na materia."""
    base = f"{LEITURA_URL}?data={ddmm}&secao={secao}"
    return f"{base}#{art_id}" if art_id else base


def _link_oficial(item: dict[str, Any]) -> str:
    """URL canonica da materia (pode retornar 502 no proprio DOU)."""
    ut = item.get("urlTitle") or item.get("url_title") or ""
    return f"{DOU_BASE}/web/dou/-/{ut}" if ut else ""


def _materia(item: dict[str, Any], ddmm: str, secao: str) -> dict[str, Any]:
    content = _strip(_field(item, "content", "conteudo"))
    art_id = _field(item, "id", "articleId", "pk")
    oficial = _link_oficial(item)
    leitura = _link_leitura(ddmm, secao, art_id)
    return {
        "titulo": _strip(_field(item, "title", "titulo")) or "(sem titulo)",
        "orgao": _strip(_field(item, "hierarchyStr", "hierarquia", "orgao")),
        "tipo_ato": _strip(_field(item, "artType", "tipo", "tipoAto")),
        "pagina": _field(item, "numberPage", "pagina"),
        "edicao": _field(item, "editionNumber", "edicao"),
        "data_pub": _field(item, "pubDate", "dataPublicacao"),
        "trecho": content[:400],
        "link": leitura,            # principal = ESTAVEL (Opcao A)
        "link_oficial": oficial,    # secundario = pagina oficial (pode dar 502)
    }


# --------------------------------------------------------------------------- #
# Mapeamento para o painel "Oportunidades" (OPS)
# --------------------------------------------------------------------------- #
def _score(materia: dict[str, Any], termo: str) -> int:
    base = 60
    blob = _norm(materia["titulo"] + " " + materia["trecho"])
    if _norm(termo) in _norm(materia["titulo"]):
        base += 18
    for forte in ("edital", "licitacao", "pregao", "chamada", "concorrencia",
                  "fomento", "selecao", "credenciamento"):
        if forte in blob:
            base += 6
            break
    return max(0, min(100, base))


def _urgencia(data_pub: str) -> int:
    try:
        d = dt.datetime.strptime(str(data_pub)[:10], "%d/%m/%Y").date()
    except ValueError:
        return 50
    dias = (dt.date.today() - d).days
    return max(20, min(95, 95 - dias * 3))


def _tags(sc: int, tipo_ato: str) -> list[list[str]]:
    tags: list[list[str]] = []
    tags.append(["Alta aderencia", "b-green"] if sc >= 80
                else ["Aderencia media", "b-gray"])
    if tipo_ato:
        tags.append([tipo_ato[:24], "b-blue"])
    tags.append(["DOU", "b-gray"])
    return tags


def _oportunidade(materia: dict[str, Any], termo: str, secao: str) -> dict[str, Any]:
    sc = _score(materia, termo)
    origem = SECAO_LABEL.get(secao, "DOU")
    seed = (materia["titulo"] + str(materia["data_pub"])).encode("utf-8")
    oid = "DOU-" + hashlib.md5(seed).hexdigest()[:6].upper()
    return {
        "id": oid, "t": materia["titulo"], "or": origem,
        "ins": materia["orgao"] or origem, "te": termo, "rg": "Brasil",
        "vl": "A confirmar", "pz": materia["data_pub"] or "-",
        "rp": "Triagem automatica", "sc": sc, "ur": _urgencia(materia["data_pub"]),
        "rs": materia["trecho"] or materia["titulo"],
        "tags": _tags(sc, materia["tipo_ato"]),
        "link": materia["link"], "link_oficial": materia["link_oficial"],
        "fonteDOU": True, "url": materia["link"],
        "pagina": materia["pagina"], "edicao": materia["edicao"],
    }


# --------------------------------------------------------------------------- #
# Rotas
# --------------------------------------------------------------------------- #
@app.get("/")
def hub() -> Any:
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health() -> Any:
    return jsonify(status="ok", time=dt.datetime.utcnow().isoformat() + "Z")


@app.post("/api/dou/buscar")
def buscar_dou() -> Any:
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
            return jsonify(
                ok=False,
                error=f"Intervalo muito grande (max {MAX_DIAS_RANGE} dias)."), 400

        knorm = {k: _norm(k) for k in keywords}
        agg: dict[str, list] = {k: [] for k in keywords}
        oportunidades: list[dict[str, Any]] = []
        vistos: set[str] = set()
        total_materias = 0
        dias_uteis = 0
        dias_pulados: list[str] = []
        fonte = LEITURA_URL

        cur = d_from
        while cur <= d_to:
            if cur.weekday() >= 5:                     # sabado/domingo
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
                            mat = _materia(a, ddmm, secao)
                            agg[k].append(mat)
                            op = _oportunidade(mat, k, secao)
                            if op["id"] not in vistos:
                                vistos.add(op["id"])
                                oportunidades.append(op)
            cur += dt.timedelta(days=1)

        resultados = [{
            "termo": k,
            "encontrou": len(agg[k]) > 0,
            "total": len(agg[k]),
            "materias": agg[k][:MAX_MATERIAS_POR_TERMO],
        } for k in keywords]

        oportunidades.sort(key=lambda o: (o["sc"], o["ur"]), reverse=True)

        return jsonify(
            ok=True,
            consulta={
                "keywords": keywords, "secao": secao,
                "date_from": d_from.isoformat(), "date_to": d_to.isoformat(),
                "dias_uteis": dias_uteis, "dias_pulados": dias_pulados,
                "materias_varridas": total_materias, "fonte": fonte,
            },
            resultados=resultados,
            oportunidades=oportunidades,
            nota=("Consulta real ao DOU (/leiturajornal, JSON). O link principal de "
                  "cada materia aponta para a leitura estavel no jornal (sem 502); "
                  "'link_oficial' leva a pagina canonica /web/dou/-/... (que as vezes "
                  "retorna 502 no proprio DOU). 'oportunidades' ja vem no formato do painel."),
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
