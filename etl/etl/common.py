"""Utilidades comunes del ETL de cow-farm (módulo Mercado)."""
import os, re, math, json, time, hashlib, unicodedata, datetime as dt
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)
UA = os.environ.get("ETL_USER_AGENT", "cow-farm-etl/1.0 (NASA Space Apps 2026; proyecto academico)")


def log(*a):
    print(dt.datetime.utcnow().strftime("%H:%M:%S"), *a, flush=True)


def norm(s):
    """Minúsculas, sin tildes ni espacios repetidos (ñ -> n)."""
    s = unicodedata.normalize("NFKD", str(s if s is not None else ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def txt(v):
    """Texto seguro: None/NaN -> ''."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v).strip()


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def dkey(*parts):
    return hashlib.sha1("|".join("" if p is None else str(p) for p in parts).encode("utf-8")).hexdigest()


def iso(d):
    return d.isoformat() if isinstance(d, (dt.date, dt.datetime)) else d


# ---------- HTTP educado: pausas, reintentos con espera creciente, User-Agent identificable ----------
def http_get(url, *, params=None, headers=None, stream=False, timeout=180, retries=5, pause=1.0):
    h = {"User-Agent": UA}
    h.update(headers or {})
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=h, stream=stream, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                wait = float(r.headers.get("Retry-After", "0") or 0) or (2 ** i) * 3
                log(f"HTTP {r.status_code} en {url[:80]}… espero {wait:.0f}s (intento {i + 1}/{retries})")
                time.sleep(wait)
                last = f"HTTP {r.status_code}"
                continue
            r.raise_for_status()
            time.sleep(pause)
            return r
        except requests.RequestException as e:
            last = str(e)
            time.sleep((2 ** i) * 2)
    raise RuntimeError(f"No se pudo descargar {url}: {last}")


# ---------- Supabase (PostgREST) ----------
def sb_ready():
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))


def _h(extra=None):
    k = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    h = {"apikey": k, "Authorization": f"Bearer {k}", "Content-Type": "application/json", "User-Agent": UA}
    h.update(extra or {})
    return h


def _u(path):
    return os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/" + path


def _check(r):
    if r.status_code >= 400:
        raise RuntimeError(f"Supabase {r.status_code}: {r.text[:600]}")
    return r


def sb_select(table, params=None, page=1000):
    out, offset = [], 0
    while True:
        p = dict(params or {})
        p.update({"limit": page, "offset": offset})
        rows = _check(requests.get(_u(table), headers=_h(), params=p, timeout=120)).json()
        out += rows
        if len(rows) < page:
            return out
        offset += page


def sb_rpc(fn, args=None):
    r = _check(requests.post(_u("rpc/" + fn), headers=_h(), data=json.dumps(args or {}), timeout=300))
    return r.json() if r.text else None


def _clean_row(row):
    c = {}
    for k, v in row.items():
        if isinstance(v, float) and not math.isfinite(v):
            v = None
        c[k] = iso(v)
    return c


def sb_upsert(table, rows, on_conflict, batch=1000):
    """Inserta o actualiza por la clave on_conflict. Elimina duplicados dentro del mismo lote."""
    if not rows:
        return 0
    uniq = {}
    for r in rows:
        uniq[r[on_conflict]] = _clean_row(r)
    rows = list(uniq.values())
    n = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        for intento in range(4):
            r = requests.post(
                _u(table), params={"on_conflict": on_conflict},
                headers=_h({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                data=json.dumps(chunk, allow_nan=False), timeout=300)
            if r.status_code >= 500:
                time.sleep(2 ** intento * 2)
                continue
            _check(r)
            break
        else:
            raise RuntimeError("Supabase no responde (5xx repetidos)")
        n += len(chunk)
        if (i // batch) % 20 == 0:
            log(f"  {table}: {n}/{len(rows)} filas")
    return n


def sb_insert(table, rows, batch=500):
    for i in range(0, len(rows), batch):
        _check(requests.post(_u(table), headers=_h({"Prefer": "return=minimal"}),
                             data=json.dumps([_clean_row(r) for r in rows[i:i + batch]], allow_nan=False), timeout=300))


# ---------- Bitácora de cargas y versiones ----------
def start_run(source_id):
    if not sb_ready():
        return None
    r = _check(requests.post(_u("mk_ingest_run"), headers=_h({"Prefer": "return=representation"}),
                             data=json.dumps({"source_id": source_id, "estado": "en_curso"}), timeout=60))
    return r.json()[0]["id"]


def finish_run(run_id, estado, nuevas=0, version=None, error=None):
    if not (sb_ready() and run_id):
        return
    body = {"fin": dt.datetime.utcnow().isoformat() + "Z", "estado": estado, "filas_nuevas": nuevas,
            "version_fuente": version, "error": (error or "")[:1000] or None}
    _check(requests.patch(_u("mk_ingest_run"), params={"id": f"eq.{run_id}"}, headers=_h(),
                          data=json.dumps(body), timeout=60))


def get_version(source_id):
    if not sb_ready():
        return ""
    rows = sb_select("mk_source", {"id": f"eq.{source_id}", "select": "ultima_version_fuente"})
    return (rows[0].get("ultima_version_fuente") or "") if rows else ""


def mark_source(source_id, version):
    if not sb_ready():
        return
    body = {"ultima_ingesta_ok": dt.datetime.utcnow().isoformat() + "Z", "ultima_version_fuente": version}
    _check(requests.patch(_u("mk_source"), params={"id": f"eq.{source_id}"}, headers=_h(),
                          data=json.dumps(body), timeout=60))


# ---------- Cultivos ----------
def load_crop_map():
    """Mapa cultivo -> códigos de fuentes. Usa Supabase si hay credenciales; si no, el CSV local."""
    if sb_ready():
        rows = sb_select("mk_crop_map")
        if rows:
            return {r["crop_id"]: {k: txt(v) for k, v in r.items()} for r in rows}
    import csv
    with open(DATA / "crop_market_map.csv", encoding="utf-8") as f:
        return {r["crop_id"]: {k: txt(v) for k, v in r.items()} for r in csv.DictReader(f)}


def term_index(crop_map, field, sep="|"):
    """norm(término) -> [crop_id,...]"""
    idx = {}
    for cid, row in crop_map.items():
        for t in txt(row.get(field)).split(sep):
            t = t.strip()
            if t:
                idx.setdefault(norm(t), []).append(cid)
    return idx


# ---------- Geografía ----------
def geo_row(id_, nivel, nombre, padre=None, iso3=None):
    return {"id": id_, "nivel": nivel, "nombre": nombre, "nombre_norm": norm(nombre), "padre_id": padre, "iso3": iso3}


WORLD = geo_row("WLD", "mundo", "World")


def m49_to_geo(code, name=""):
    """Código M49 (FAOSTAT lo entrega como '170) -> fila de geografía de país, o None si no es un país."""
    c = txt(code).strip("'").strip()
    if not c:
        return None
    if c.lstrip("0") in ("1", ""):
        return WORLD
    try:
        import pycountry
        p = pycountry.countries.get(numeric=c.zfill(3))
    except Exception:
        p = None
    if not p:
        return None
    return geo_row(p.alpha_3, "pais", txt(name) or p.name, "WLD", p.alpha_3)


# ---------- Observaciones ----------
def estado_from_flag(flag):
    """FAOSTAT: A=oficial, X=organismo internacional, S=estandarizado -> observado; el resto se marca estimado."""
    return "observado" if txt(flag).upper() in {"A", "X", "S"} else "estimado"


def scale_unit(v, u):
    """Conversiones exactas: '1000 USD' -> USD (x1000); hg/ha -> kg/ha (/10); US$ -> USD."""
    u = txt(u)
    m = re.match(r"^1000\s+(.*)$", u)
    if m:
        v, u = v * 1000, m.group(1)
    u = u.replace("US$", "USD")
    if u == "hg/ha":
        v, u = v / 10, "kg/ha"
    return v, u


def make_obs(*, source_id, indicador, crop_id, producto_original, geo_id, nivel, inicio, fin, periodicidad,
             valor, unidad, estado, url, moneda=None, tipo_precio=None, mercado=None, contraparte=None,
             marca=None, publicacion=None, run_id=None):
    valor = num(valor)
    if valor is None or not unidad:
        return None
    return {
        "dedupe_key": dkey(source_id, indicador, crop_id, producto_original, geo_id, contraparte, inicio, fin,
                           periodicidad, tipo_precio, mercado, unidad, moneda),
        "source_id": source_id, "indicador": indicador, "crop_id": crop_id,
        "producto_original": producto_original, "geo_id": geo_id, "nivel_geografico": nivel,
        "contraparte_geo_id": contraparte, "periodo_inicio": iso(inicio), "periodo_fin": iso(fin),
        "periodicidad": periodicidad, "valor": valor, "unidad": unidad, "moneda": moneda,
        "tipo_precio": tipo_precio, "mercado": mercado, "estado_dato": estado, "marca_fuente": marca,
        "url_procedencia": url, "fecha_publicacion": iso(publicacion), "ingest_run_id": run_id,
    }


def parse_date(s):
    s = txt(s)
    for fmt, n in (("%Y-%m-%d", 10), ("%d/%m/%Y", 10), ("%d-%m-%Y", 10), ("%m/%d/%Y", 10)):
        try:
            return dt.datetime.strptime(s[:n], fmt).date()
        except ValueError:
            continue
    return None


def write_sample(name, rows, n=200):
    import csv
    if not rows:
        return
    p = OUT / name
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows[:n])
    log(f"Muestra guardada en {p}")
