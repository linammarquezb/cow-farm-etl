"""UPRA / EVA (Evaluaciones Agropecuarias Municipales) desde datos.gov.co (Socrata).

Uso:
  python etl/30_eva.py --search                 # busca en el catálogo los conjuntos EVA (para ubicar 2019 en adelante)
  python etl/30_eva.py --columns                # muestra columnas y valores del conjunto (no escribe)
  python etl/30_eva.py --dry-run                # transforma y muestra el cruce de cultivos
  python etl/30_eva.py --dataset-id XXXX-XXXX   # carga otro conjunto EVA (por defecto 2pnw-mmge, años 2007-2018)
Revisa SIEMPRE output/eva_cruce_cultivos.csv antes de la carga real.
"""
import argparse, re, sys
import pandas as pd
from common import *

SOURCE = "upra_eva"
DEFAULT_ID = "2pnw-mmge"
BASE = "https://www.datos.gov.co"
EXCLUDE = re.compile(r"DE ARBOL|FORRAJER|ORNAMENT|SEMILLA", re.I)


def app_headers():
    t = os.environ.get("SOCRATA_APP_TOKEN")
    return {"X-App-Token": t} if t else {}


def fetch_all(dataset_id, limit_rows=0):
    rows, off, page = [], 0, 50000
    while True:
        r = http_get(f"{BASE}/resource/{dataset_id}.json", params={"$limit": page, "$offset": off, "$order": ":id"},
                     headers=app_headers(), pause=0.5)
        chunk = r.json()
        rows += chunk
        log(f"  filas descargadas: {len(rows):,}")
        if len(chunk) < page or (limit_rows and len(rows) >= limit_rows):
            return pd.DataFrame(rows)
        off += page


def find_col(cols, *must, none=()):
    for c in cols:
        n = norm(c).replace(" ", "_")
        if all(m in n for m in must) and not any(x in n for x in none):
            return c
    return None


def detect(df):
    cols = list(df.columns)
    m = {
        "dep_cod": find_col(cols, "digo", "dane", "departamento") or find_col(cols, "cod", "dep"),
        "dep": find_col(cols, "departamento", none=("digo", "cod")),
        "mun_cod": find_col(cols, "digo", "dane", "municipio") or find_col(cols, "cod", "mun"),
        "mun": find_col(cols, "municipio", none=("digo", "cod")),
        "cultivo": next((c for c in cols if norm(c).replace(" ", "_") in ("cultivo",)), None)
                   or find_col(cols, "cultivo", none=("grupo", "subgrupo", "nombre", "ciclo", "desagreg")),
        "anio": find_col(cols, "a_o") or find_col(cols, "ano") or find_col(cols, "anio"),
        "periodo": find_col(cols, "periodo"),
        "sembrada": find_col(cols, "sembrada"),
        "cosechada": find_col(cols, "cosechada"),
        "produccion": find_col(cols, "producci"),
    }
    m["dep_cod"] = m["dep_cod"] or next((c for c in cols if str(c).strip().lower() == "c_d_dep"), None)
    m["mun_cod"] = m["mun_cod"] or next((c for c in cols if str(c).strip().lower() == "c_d_mun"), None)
    faltan = [k for k in ("dep_cod", "dep", "mun_cod", "mun", "cultivo", "anio", "sembrada", "cosechada", "produccion")
              if not m[k]]
    return m, faltan


def semester(per, year):
    p = txt(per).upper().replace(" ", "")
    if p.endswith("A") or p in ("I", "1", "1S", "S1") or p.endswith("-I") or p.endswith("1S"):
        return dt.date(year, 1, 1), dt.date(year, 6, 30), "semestral"
    if p.endswith("B") or p in ("II", "2", "2S", "S2") or p.endswith("-II") or p.endswith("2S"):
        return dt.date(year, 7, 1), dt.date(year, 12, 31), "semestral"
    return dt.date(year, 1, 1), dt.date(year, 12, 31), "anual"


def pad_codes(dep, mun):
    dep = str(int(float(dep))).zfill(2)
    mun = str(int(float(mun)))
    mun = dep + mun.zfill(3) if len(mun) <= 3 else mun.zfill(5)
    return dep, mun


def transform(df, crop_map, url, pub, run_id=None, from_year=2000):
    m, faltan = detect(df)
    if faltan:
        raise RuntimeError(f"No pude identificar estas columnas: {faltan}. Columnas del conjunto: {list(df.columns)}")
    terms = term_index(crop_map, "eva_terminos")
    compiled = [(re.compile(rf"\b{re.escape(t)}\b"), cids) for t, cids in terms.items()]
    cache, cruce = {}, {}

    def crop_of(name):
        k = norm(name)
        if k in cache:
            return cache[k]
        res = None
        if not EXCLUDE.search(name.upper()):
            res = next((cids for rx, cids in compiled if rx.search(k)), None)
        cache[k] = res
        cruce[name] = ",".join(res) if res else ""
        return res

    d = pd.DataFrame({
        "dep_cod": df[m["dep_cod"]], "dep": df[m["dep"]], "mun_cod": df[m["mun_cod"]], "mun": df[m["mun"]],
        "cultivo": df[m["cultivo"]].astype(str), "anio": pd.to_numeric(df[m["anio"]], errors="coerce"),
        "periodo": df[m["periodo"]] if m["periodo"] else "",
        "sembrada": pd.to_numeric(df[m["sembrada"]], errors="coerce"),
        "cosechada": pd.to_numeric(df[m["cosechada"]], errors="coerce"),
        "produccion": pd.to_numeric(df[m["produccion"]], errors="coerce")})
    d = d.dropna(subset=["anio", "dep_cod", "mun_cod"])
    d = d[d["anio"] >= from_year]
    d["crops"] = d["cultivo"].map(crop_of)
    d = d[d["crops"].notna()]
    d["crop"] = d["crops"].map(lambda x: x[0])
    _ef_col = next((c for c in df.columns if "estado_fisico" in str(c).lower()), None)
    estados = {}
    if _ef_col:
        d["_ef"] = df.loc[d.index, _ef_col].astype(str).str.strip().str.upper()
        _v = d[~d["_ef"].isin(["", "NAN", "NONE"])]
        estados = _v.groupby("crop")["_ef"].agg(lambda x: " / ".join(sorted(set(x)))).to_dict()
    d["dep_id"], d["mun_id"] = zip(*[pad_codes(a, b) for a, b in zip(d["dep_cod"], d["mun_cod"])]) if len(d) else ([], [])
    d["anio"] = d["anio"].astype(int)
    keys = ["dep_id", "mun_id", "dep", "mun", "crop", "anio", "periodo"]
    g = d.groupby(keys, dropna=False).agg(
        sembrada=("sembrada", lambda s: s.sum(min_count=1)), cosechada=("cosechada", lambda s: s.sum(min_count=1)),
        produccion=("produccion", lambda s: s.sum(min_count=1)),
        nombres=("cultivo", lambda s: " + ".join(sorted(set(s))))).reset_index()

    geos = {"COL": geo_row("COL", "pais", "Colombia", "WLD", "COL")}
    obs = []

    def emit(level_id, nivel, row, label, derived):
        ini, fin, per = semester(row["periodo"], int(row["anio"]))
        ef = f" [estado físico: {estados[row['crop']]}]" if estados.get(row["crop"]) else ""
        marca = "agregado ETL: suma de los municipios reportados" if derived else "EVA (UPRA)"
        for ind, col, unit in (("area_sembrada", "sembrada", "ha"), ("area_cosechada", "cosechada", "ha"),
                               ("produccion", "produccion", "t")):
            if derived or ind != "rendimiento":
                o = make_obs(source_id=SOURCE, indicador=ind, crop_id=row["crop"], producto_original=row["nombres"] + (ef if ind == "produccion" else ""),
                             geo_id=level_id, nivel=nivel, inicio=ini, fin=fin, periodicidad=per, valor=row[col],
                             unidad=unit, estado="observado", url=url, marca=marca, publicacion=pub, run_id=run_id)
                if o:
                    obs.append(o)
        if derived and num(row["cosechada"]) and num(row["cosechada"]) > 0 and num(row["produccion"]) is not None:
            o = make_obs(source_id=SOURCE, indicador="rendimiento", crop_id=row["crop"], producto_original=row["nombres"] + ef,
                         geo_id=level_id, nivel=nivel, inicio=ini, fin=fin, periodicidad=per,
                         valor=row["produccion"] / row["cosechada"], unidad="t/ha", estado="observado", url=url,
                         marca="calculado en el ETL: producción / área cosechada", publicacion=pub, run_id=run_id)
            if o:
                obs.append(o)

    for r in g.to_dict("records"):  # municipal (el rendimiento municipal se calcula en la app)
        geos[f"COL-{r['dep_id']}"] = geo_row(f"COL-{r['dep_id']}", "departamento", txt(r["dep"]), "COL")
        geos[f"COL-{r['mun_id']}"] = geo_row(f"COL-{r['mun_id']}", "municipio", txt(r["mun"]), f"COL-{r['dep_id']}")
        emit(f"COL-{r['mun_id']}", "municipio", r, r["mun"], False)
    dep = g.groupby(["dep_id", "crop", "anio", "periodo"], dropna=False).agg(
        sembrada=("sembrada", lambda s: s.sum(min_count=1)), cosechada=("cosechada", lambda s: s.sum(min_count=1)),
        produccion=("produccion", lambda s: s.sum(min_count=1)),
        nombres=("nombres", lambda s: " + ".join(sorted(set(s))))).reset_index()
    for r in dep.to_dict("records"):
        emit(f"COL-{r['dep_id']}", "departamento", r, "", True)
    nat = dep.groupby(["crop", "anio", "periodo"], dropna=False).agg(
        sembrada=("sembrada", lambda s: s.sum(min_count=1)), cosechada=("cosechada", lambda s: s.sum(min_count=1)),
        produccion=("produccion", lambda s: s.sum(min_count=1)),
        nombres=("nombres", lambda s: " + ".join(sorted(set(" + ".join(s).split(" + ")))))).reset_index()
    for r in nat.to_dict("records"):
        emit("COL", "pais", r, "", True)
    return obs, list(geos.values()), cruce


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-id", default=DEFAULT_ID)
    ap.add_argument("--search", action="store_true")
    ap.add_argument("--columns", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--from-year", type=int, default=2010)
    ap.add_argument("--sample", type=int, default=0)
    a = ap.parse_args()
    if a.search:
        r = http_get(f"{BASE}/api/catalog/v1", params={"q": "Evaluaciones Agropecuarias Municipales", "only": "datasets",
                                                       "limit": 20}, headers=app_headers()).json()
        for x in r.get("results", []):
            res = x.get("resource", {})
            log(res.get("id"), "|", res.get("name"), "| actualizado:", res.get("updatedAt"))
        return
    meta = http_get(f"{BASE}/api/views/{a.dataset_id}.json").json()
    upd = dt.datetime.utcfromtimestamp(meta["rowsUpdatedAt"]).date() if meta.get("rowsUpdatedAt") else None
    version = f"{a.dataset_id}@{upd}"
    log("Conjunto:", meta.get("name"), "| filas actualizadas:", upd)
    write = sb_ready() and not (a.dry_run or a.columns)
    if write and not a.force and get_version(SOURCE) == version:
        log("Sin cambios desde la última carga; omito.")
        return
    df = fetch_all(a.dataset_id, a.sample)
    if a.columns:
        m, faltan = detect(df)
        log("Columnas:", list(df.columns))
        log("Detección:", m, "| faltan:", faltan)
        if m.get("periodo"):
            log("Valores de periodo:", sorted(df[m["periodo"]].astype(str).unique())[:30])
        if m.get("cultivo"):
            log("Ejemplos de cultivo:", sorted(df[m["cultivo"]].astype(str).unique())[:60])
        return
    url = f"{BASE}/d/{a.dataset_id}"
    run = start_run(SOURCE) if write else None
    try:
        obs, geos, cruce = transform(df, load_crop_map(), url, upd, run, a.from_year)
        pd.Series(cruce, name="crop_id").rename_axis("cultivo_eva").to_csv(OUT / "eva_cruce_cultivos.csv")
        log(f"Observaciones: {len(obs):,}; geografías: {len(geos):,}; cultivos EVA vistos: {len(cruce)}")
        log("Cruce guardado en output/eva_cruce_cultivos.csv (revísalo: cultivo EVA -> cultivo cow-farm)")
        write_sample("eva_muestra.csv", obs)
        if write:
            sb_upsert("mk_geo", geos, "id")
            n = sb_upsert("mk_observation", obs, "dedupe_key")
            mark_source(SOURCE, version)
            finish_run(run, "ok", n, version)
            sb_rpc("mk_refrescar")
            log("Filas escritas:", n)
    except Exception as e:
        finish_run(run, "error", error=str(e))
        raise


if __name__ == "__main__":
    main()
