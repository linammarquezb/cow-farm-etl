"""Importador genérico para los datos de prueba que entreguen el 14 de noviembre (CSV o Excel).

1) Copia el archivo a data/challenge/ y mira sus columnas:   python etl/50_import_generic.py --inspect ARCHIVO
2) Copia data/mapping_ejemplo.json a data/mapping.json y ajústalo a las columnas reales.
3) Prueba sin escribir:                                    python etl/50_import_generic.py ARCHIVO --map data/mapping.json --dry-run
4) Carga real:                                             python etl/50_import_generic.py ARCHIVO --map data/mapping.json
Nunca se inventan valores: las filas sin valor, unidad o período válidos se descartan y se cuentan.
"""
import argparse, json, sys
import pandas as pd
from common import *

SOURCE = "challenge_datos"


def read_any(path, sheet=None):
    if str(path).lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(path, sheet_name=sheet or 0, dtype=str)
    try:
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    except UnicodeDecodeError:
        return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="latin-1")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--map")
    ap.add_argument("--sheet")
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    df = read_any(a.file, a.sheet)
    if a.inspect or not a.map:
        log("Columnas:", list(df.columns))
        log("Filas:", len(df))
        print(df.head(10).to_string())
        return
    mp = json.load(open(a.map, encoding="utf-8"))
    const, cols = mp.get("constantes", {}), mp.get("columnas", {})
    crops = load_crop_map()
    cmap = {norm(k): v for k, v in mp.get("cultivos", {}).items()}
    ok, bad, obs, geos = 0, 0, [], {}
    url = mp.get("url_procedencia", a.file)
    for r in df.to_dict("records"):
        def g(field):
            c = cols.get(field)
            return txt(r.get(c)) if c else txt(const.get(field))
        crop = cmap.get(norm(g("producto")))
        geo = g("geo_id")
        inicio, fin = parse_date(g("periodo_inicio")), parse_date(g("periodo_fin"))
        o = make_obs(source_id=SOURCE, indicador=g("indicador"), crop_id=crop if crop in crops else None,
                     producto_original=g("producto") or "(sin nombre)", geo_id=geo, nivel=g("nivel_geografico"),
                     inicio=inicio, fin=fin, periodicidad=g("periodicidad"), valor=g("valor"), unidad=g("unidad"),
                     moneda=g("moneda") or None, tipo_precio=g("tipo_precio") or None,
                     estado=g("estado_dato") or "observado", url=url, marca="datos del challenge")
        if not o or not (inicio and fin) or not geo or not g("indicador"):
            bad += 1
            continue
        geos[geo] = geo_row(geo, g("nivel_geografico") or "pais", g("geo_nombre") or geo,
                            g("geo_padre") or None, geo if len(geo) == 3 else None)
        obs.append(o)
        ok += 1
    log(f"Filas válidas: {ok}; descartadas: {bad}")
    write_sample("challenge_muestra.csv", obs)
    if sb_ready() and not a.dry_run:
        sb_upsert("mk_geo", list(geos.values()), "id")
        log("Escritas:", sb_upsert("mk_observation", obs, "dedupe_key"))
        mark_source(SOURCE, dt.date.today().isoformat())
        sb_rpc("mk_refrescar")


if __name__ == "__main__":
    main()
