"""FAOSTAT: producción, área cosechada, rendimiento (QCL), comercio por país (TCL) y precios al productor (PP).

Uso típico:
  python etl/10_faostat.py --dry-run --dataset QCL --sample 300000     # prueba sin escribir en Supabase
  python etl/10_faostat.py --dataset QCL                               # carga real
Para limitar el tamaño de la base: --from-year (años recientes de todos los países) y
--history-from con FAOSTAT_HISTORY_COUNTRIES (historia larga solo de esos países, por defecto COL).
"""
import argparse, io, json, os, sys, tempfile, zipfile
import pandas as pd
from common import *

SOURCE = "faostat"
CATALOG = "https://bulks-faostat.fao.org/production/datasets_E.json"
QCL_EL = {"area harvested": "area_cosechada", "production": "produccion", "yield": "rendimiento"}
TCL_EL = {"import quantity": "importacion_cantidad", "import value": "importacion_valor",
          "export quantity": "exportacion_cantidad", "export value": "exportacion_valor"}
DEFAULT_FROM = {"QCL": 2018, "TCL": 2020, "PP": 2015}


def find_datasets(node, out):
    if isinstance(node, dict):
        if "DatasetCode" in node:
            out[node["DatasetCode"]] = node
        for v in node.values():
            find_datasets(v, out)
    elif isinstance(node, list):
        for v in node:
            find_datasets(v, out)


def colmap(df):
    return {norm(c): c for c in df.columns}


def pick(cm, *names):
    for n in names:
        if norm(n) in cm:
            return cm[norm(n)]
    return None


def read_zip_csv(path):
    """Devuelve un iterador de trozos (DataFrame) del CSV normalizado dentro del ZIP."""
    z = zipfile.ZipFile(path)
    cands = [n for n in z.namelist() if n.lower().endswith(".csv")]
    best = [n for n in cands if "normalized" in n.lower() and "flag" not in n.lower() and "item" not in n.lower()
            and "area" not in n.lower().replace("all_data", "")]
    name = (best or sorted(cands, key=lambda n: z.getinfo(n).file_size, reverse=True))[0]
    log("Archivo dentro del ZIP:", name)
    enc = "utf-8"
    try:
        pd.read_csv(z.open(name), nrows=5, dtype=str, encoding=enc)
    except UnicodeDecodeError:
        enc = "latin-1"
    return pd.read_csv(z.open(name), chunksize=200_000, dtype=str, keep_default_na=False, encoding=enc)


def process_chunk(df, code, itemmap, keep_year, only_countries, url, pub, run_id, geos, matched):
    cm = colmap(df)
    c_area_code = pick(cm, "Area Code (M49)", "Area Code")
    c_area, c_item, c_el = pick(cm, "Area"), pick(cm, "Item"), pick(cm, "Element")
    c_year, c_unit, c_val, c_flag = pick(cm, "Year"), pick(cm, "Unit"), pick(cm, "Value"), pick(cm, "Flag")
    c_month = pick(cm, "Months")
    if not all([c_area_code, c_area, c_item, c_el, c_year, c_unit, c_val]):
        raise RuntimeError(f"Columnas inesperadas en {code}: {list(df.columns)}")
    df = df[df[c_item].map(norm).isin(itemmap)]
    if df.empty:
        return []
    out = []
    for r in df.to_dict("records"):
        year = int(num(r[c_year]) or 0)
        if year == 0:
            continue
        geo = m49_to_geo(r[c_area_code], r[c_area])
        if not geo:
            continue
        iso3 = geo["id"]
        if only_countries and iso3 not in only_countries:
            continue
        if not keep_year(year, iso3):
            continue
        v = num(r[c_val])
        if v is None or txt(r[c_flag]).upper() == "M":
            continue
        el = norm(r[c_el])
        item = r[c_item]
        extra = dict(moneda=None, tipo_precio=None)
        if code == "QCL":
            ind = QCL_EL.get(el)
        elif code == "TCL":
            ind = TCL_EL.get(el)
        else:  # PP: precios al productor
            if not el.startswith("producer price (") or "index" in el:
                continue
            ind = "precio_productor"
            cur = el.split("(")[1].split("/")[0].strip().upper()  # USD, LCU, SLC
            extra = dict(moneda=cur, tipo_precio="productor")
        if not ind:
            continue
        v, unit = scale_unit(v, r[c_unit])
        if code == "PP":
            unit = "t"  # el elemento ya dice "por tonelada"; la moneda va aparte
        if code == "TCL" and ind.endswith("_valor"):
            extra["moneda"] = "USD"
        month = norm(r[c_month]) if c_month else ""
        months = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
                  "october", "november", "december"]
        if month in months:
            m = months.index(month) + 1
            ini = dt.date(year, m, 1)
            fin = (dt.date(year + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))
            per = "mensual"
        else:
            ini, fin, per = dt.date(year, 1, 1), dt.date(year, 12, 31), "anual"
        geos[iso3] = geo
        for cid in itemmap[norm(item)]:
            matched.add(cid)
            o = make_obs(source_id=SOURCE, indicador=ind, crop_id=cid, producto_original=item, geo_id=iso3,
                         nivel="pais" if iso3 != "WLD" else "mundo", inicio=ini, fin=fin, periodicidad=per,
                         valor=v, unidad=unit, estado=estado_from_flag(r[c_flag]), url=url,
                         marca=txt(r[c_flag]) or "sin bandera", publicacion=pub, run_id=run_id, **extra)
            if o:
                out.append(o)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", action="append", choices=list(DEFAULT_FROM), help="QCL, TCL o PP (repetible)")
    ap.add_argument("--from-year", type=int, help="Año mínimo para todos los países")
    ap.add_argument("--history-from", type=int, default=2000, help="Año mínimo para los países de historia larga")
    ap.add_argument("--only-countries", help="Lista ISO3 separada por comas (por defecto: todos)")
    ap.add_argument("--dry-run", action="store_true", help="No escribe en Supabase")
    ap.add_argument("--force", action="store_true", help="Carga aunque la versión no haya cambiado")
    ap.add_argument("--sample", type=int, default=0, help="Procesa solo las primeras N filas (pruebas)")
    ap.add_argument("--zip", help="Usa un ZIP local en lugar de descargarlo (pruebas)")
    a = ap.parse_args()
    datasets = a.dataset or ["QCL", "TCL", "PP"]
    hist = {c.strip().upper() for c in os.environ.get("FAOSTAT_HISTORY_COUNTRIES", "COL").split(",") if c.strip()}
    only = {c.strip().upper() for c in a.only_countries.split(",")} if a.only_countries else None
    write = sb_ready() and not a.dry_run
    crop_map = load_crop_map()
    itemmap = term_index(crop_map, "faostat_item_patron")
    log(f"{len(itemmap)} nombres de producto FAOSTAT a buscar; escritura en Supabase: {write}")

    cat = {}
    if not a.zip:
        find_datasets(http_get(CATALOG).json(), cat)
        log("Catálogo FAOSTAT leído:", len(cat), "conjuntos")
    versions = dict(p.split("=", 1) for p in get_version(SOURCE).split(";") if "=" in p)
    new_versions = dict(versions)
    total = 0
    for code in datasets:
        info = cat.get(code, {})
        url = info.get("FileLocation") or ""
        upd = txt(info.get("DateUpdate")) or "desconocida"
        if not a.zip and not url:
            log(f"[{code}] No encontré FileLocation en el catálogo; revisa {CATALOG} y usa --zip con el archivo descargado.")
            continue
        if not a.force and not a.zip and versions.get(code) == upd and write:
            log(f"[{code}] sin cambios desde {upd}; omito (usa --force para cargar de todos modos)")
            continue
        run = start_run(SOURCE) if write else None
        try:
            path = a.zip
            if not path:
                log(f"[{code}] descargando {url}")
                tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
                for chunk in http_get(url, stream=True, timeout=600).iter_content(1 << 20):
                    tmp.write(chunk)
                tmp.close()
                path = tmp.name
            fy = a.from_year or DEFAULT_FROM[code]
            keep = lambda y, iso3, fy=fy: y >= fy or (iso3 in hist and y >= a.history_from)
            geos, matched, obs, seen = {}, set(), [], 0
            for chunk in read_zip_csv(path):
                seen += len(chunk)
                obs += process_chunk(chunk, code, itemmap, keep, only, url or "archivo local",
                                     parse_date(upd), run, geos, matched)
                if a.sample and seen >= a.sample:
                    break
            log(f"[{code}] filas leídas: {seen:,}; observaciones: {len(obs):,}; países: {len(geos)}")
            faltan = [c for c in crop_map if txt(crop_map[c].get("faostat_item_patron")) and c not in matched]
            log(f"[{code}] cultivos SIN coincidencia (revisar nombres en crop_market_map.csv): {faltan or 'ninguno'}")
            write_sample(f"faostat_{code}_muestra.csv", obs)
            if write:
                sb_upsert("mk_geo", list(geos.values()), "id")
                n = sb_upsert("mk_observation", obs, "dedupe_key")
                new_versions[code] = upd
                ver = ";".join(f"{k}={v}" for k, v in new_versions.items())
                mark_source(SOURCE, ver)
                finish_run(run, "ok", n, ver)
                total += n
        except Exception as e:
            finish_run(run, "error", error=str(e))
            raise
    if write:
        sb_rpc("mk_refrescar")
    log("FAOSTAT terminado. Filas escritas:", total)


if __name__ == "__main__":
    main()
