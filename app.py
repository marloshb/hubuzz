"""
Agente de monitoramento do Diario Oficial da Uniao (DOU) - pronto para Render.

Fonte (URL leiturajornal): data (dia atual, automatico) + secao (FIXA, ex.: do3).
Palavras-chave PARAMETRIZAVEIS via formulario da interface e persistidas em arquivo.
"""
import os, json, re, uuid, datetime, threading
from urllib.parse import urlencode
import requests
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder="static", static_url_path="")
LOCK = threading.Lock()

DB_FILE = os.environ.get("DB_FILE", "agents_db.json")

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")}

DEFAULT_KEYWORDS = ["geoprocessamento", "mapeamento", "plataforma",
                    "sensoriamento", "imageamento", "sistemas"]

def load_db():
    try:
        with open(DB_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"agents": []}

def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def seed():
    db = load_db()
    if not db["agents"]:
        db["agents"].append({
            "id": "ag-dou-geo", "name": "Monitor DOU - Geotecnologias", "type": "dou",
            "source": "https://www.in.gov.br/leiturajornal", "secao": "do3",
            "keywords": list(DEFAULT_KEYWORDS), "active": True,
            "created": datetime.date.today().isoformat(),
            "last_run": None, "results": []
        })
        save_db(db)
seed()

def today_ddmmyyyy():
    return datetime.date.today().strftime("%d-%m-%Y")

def build_dou_url(secao, data):
    return "https://www.in.gov.br/leiturajornal?" + urlencode({"data": data, "secao": secao})

def fetch_dou_articles(secao, data, timeout=60):
    url = build_dou_url(secao, data)
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    m = re.search(r'id="params"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("Bloco de dados (#params) nao encontrado na pagina do DOU.")
    return url, json.loads(m.group(1).strip()).get("jsonArray", [])

def match_keywords(article, keywords):
    blob = " ".join([article.get("title", ""), article.get("subTitulo", ""),
                     article.get("content", ""), article.get("hierarchyStr", "")]).lower()
    return [k for k in keywords if k.lower() in blob]

def run_dou_agent(agent, data=None):
    data = data or today_ddmmyyyy()
    url, articles = fetch_dou_articles(agent["secao"], data)
    hits = []
    for a in articles:
        matched = match_keywords(a, agent["keywords"])
        if matched:
            ut = a.get("urlTitle", "")
            hits.append({
                "title": a.get("title", "") or a.get("titulo", ""),
                "artType": a.get("artType", ""), "page": a.get("numberPage", ""),
                "edition": a.get("editionNumber", ""), "pubDate": a.get("pubDate", ""),
                "org": a.get("hierarchyStr", ""), "matched": matched,
                "snippet": (a.get("content", "") or "")[:360],
                "link": f"https://www.in.gov.br/web/dou/-/{ut}" if ut else url
            })
    return {"data": data, "secao": agent["secao"], "source_url": url,
            "scanned": len(articles), "found": len(hits),
            "ran_at": datetime.datetime.now().isoformat(timespec="seconds"), "items": hits}

def find(db, aid):
    return next((a for a in db["agents"] if a["id"] == aid), None)

def parse_keywords(value):
    items = value if isinstance(value, list) else str(value).split(",")
    seen, out = set(), []
    for k in items:
        k = k.strip()
        if k and k.lower() not in seen:
            seen.add(k.lower()); out.append(k)
    return out

@app.route("/api/agents", methods=["GET"])
def list_agents():
    return jsonify(load_db()["agents"])

@app.route("/api/agents", methods=["POST"])
def create_agent():
    b = request.get_json(force=True)
    with LOCK:
        db = load_db()
        ag = {"id": "ag-" + uuid.uuid4().hex[:8], "name": b.get("name", "Novo agente"),
              "type": "dou", "source": b.get("source", "https://www.in.gov.br/leiturajornal"),
              "secao": (b.get("secao", "do3") or "do3").lower(),
              "keywords": parse_keywords(b.get("keywords", DEFAULT_KEYWORDS)),
              "active": True, "created": datetime.date.today().isoformat(),
              "last_run": None, "results": []}
        db["agents"].append(ag); save_db(db)
    return jsonify(ag), 201

@app.route("/api/agents/<aid>", methods=["DELETE"])
def delete_agent(aid):
    with LOCK:
        db = load_db()
        db["agents"] = [a for a in db["agents"] if a["id"] != aid]
        save_db(db)
    return jsonify({"ok": True})

@app.route("/api/agents/<aid>/keywords", methods=["GET"])
def get_keywords(aid):
    ag = find(load_db(), aid)
    if not ag: return jsonify({"error": "nao encontrado"}), 404
    return jsonify({"keywords": ag["keywords"]})

@app.route("/api/agents/<aid>/keywords", methods=["PUT"])
def set_keywords(aid):
    b = request.get_json(force=True)
    kws = parse_keywords(b.get("keywords", []))
    if not kws:
        return jsonify({"error": "informe ao menos uma palavra-chave"}), 400
    with LOCK:
        db = load_db(); ag = find(db, aid)
        if not ag: return jsonify({"error": "nao encontrado"}), 404
        ag["keywords"] = kws; save_db(db)
    return jsonify({"ok": True, "keywords": kws})

@app.route("/api/agents/<aid>/run", methods=["POST"])
def run_agent(aid):
    data = request.args.get("data")
    ag = find(load_db(), aid)
    if not ag: return jsonify({"error": "nao encontrado"}), 404
    try:
        result = run_dou_agent(ag, data)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    with LOCK:
        db = load_db(); ag = find(db, aid)
        ag["last_run"] = result["ran_at"]; ag["results"] = result["items"]
        ag["last_summary"] = {k: result[k] for k in ("data","secao","scanned","found","source_url","ran_at")}
        save_db(db)
    return jsonify(result)

@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
