"""Banco Mundial · Commodity Markets (Pink Sheet): precios internacionales mensuales.

Uso:
  python etl/20_pinksheet.py --inspect          # muestra qué detectó en el Excel (no escribe nada)
  python etl/20_pinksheet.py --dry-run
  python etl/20_pinksheet.py
Si el enlace del Excel cambia, pásalo con --url o con la variable PINKSHEET_URL.
"""
import argparse, hashlib, io, os, re, sys
import pandas as pd
from common import *

SOURCE = "wb_pinksheet"
PAGES = ["https://www.worldbank.org/en/research/commodity-markets", "https://www.worldbank.org/commodities"]
LINK_RE = re.compile(r"https?://thedocs\.worldbank\.org/[^\s\"'<>]*CMO-Historical-Data-Monthly\.xlsx", re.I)
UNIT_MAP = {"$/mt": "t", "$/kg": "kg", "$/lb": "lb", "$/mmbtu": "mmbtu", "$/bbl": "bbl", "$/troy oz": "oz t",
            "$/cubic meter": "m3", "$/sheet": "hoja", "cents/kg": "cent/kg"}


def find_url(cli_url=None):
    url = cli_url or os.environ.get("PINKSHEET_URL")
    if url:
        return url
    for p in PAGES:
        try:
            m = LINK_RE.search(http_get(p).text)
        except Exception as e:
            log("No pude leer", p, e)
            continue
        if m:
            return m.group(0)
    sys.exit("No encontré el enlace del Excel. Abre https://www.worldbank.org/en/research/commodity-markets, "
             "copia el enlace de 'Monthly prices (XLS)' y ejecútalo con --url ENLACE.")


def parse_sheet(raw):
    """raw: DataFrame sin encabezados de la hoja 'Monthly Prices'. Devuelve (series, nombres, unidades)."""
    pat = re.compile(r"^\d{4}M\d{2}$")
    first = next((i for i in range(len(raw)) if pat.match(txt(raw.iat[i, 0]))), None)
    if first is None:
        raise RuntimeError("No encontré filas con fechas tipo 1960M01 en la hoja.")
    head = raw.iloc[:first]
    # Fila de unidades: la que más celdas empieza con "(" ; fila de nombres: la de más textos que no son unidades
    def score_units(row):
        return sum(txt(x).startswith("(") for x in row)
    def score_names(row):
        return sum(bool(txt(x)) and not txt(x).startswith("(") for x in row)
    ui = max(range(len(head)), key=lambda i: score_units(head.iloc[i]))
    ni = max((i for i in range(len(head)) if i != ui), key=lambda i: score_names(head.iloc[i]))
    names = {c: txt(head.iat[ni, c]) for c in range(1, raw.shape[1]) if txt(head.iat[ni, c])}
    units = {c: txt(head.iat[ui, c]).strip("()").lower() for c in names}
    return first, names, units


def transform(raw, crop_map, url, run_id=None):
    first, names, units = parse_sheet(raw)
    wanted = {}  # norm(serie) -> crop_ids
    for cid, row in crop_map.items():
        for s in txt(row.get("wb_pinksheet_serie")).split("|"):
            if s.strip():
                wanted.setdefault(norm(s), []).append(cid)
    cols = {}
    for c, nm in names.items():
        k = norm(nm)
        hit = k if k in wanted else next((w for w in wanted if k.startswith(w)), None)
        if hit:
            cols[c] = (nm, wanted[hit])
    obs = []
    for i in range(first, len(raw)):
        d = txt(raw.iat[i, 0])
        if not re.match(r"^\d{4}M\d{2}$", d):
            continue
        y, m = int(d[:4]), int(d[5:])
        ini = dt.date(y, m, 1)
        fin = dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)
        for c, (nm, crops) in cols.items():
            v = num(raw.iat[i, c])  # '..' u otros marcadores -> None
            if v is None:
                continue
            unit = UNIT_MAP.get(units.get(c, ""), units.get(c, "") or "")
            for cid in crops:
                o = make_obs(source_id=SOURCE, indicador="precio_internacional", crop_id=cid, producto_original=nm,
                             geo_id="WLD", nivel="mundo", inicio=ini, fin=fin, periodicidad="mensual", valor=v,
                             unidad=unit, moneda="USD", tipo_precio="internacional", estado="observado", url=url,
                             marca="USD nominales", run_id=run_id)
                if o:
                    obs.append(o)
    return obs, {names[c]: units.get(c) for c in cols}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--file", help="Excel local (pruebas)")
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    url = a.url or ""
    if a.file:
        content, url = open(a.file, "rb").read(), a.file
    else:
        url = find_url(a.url)
        log("Descargando", url)
        content = http_get(url, timeout=300).content
    sha = hashlib.sha256(content).hexdigest()[:16]
    write = sb_ready() and not (a.dry_run or a.inspect)
    if write and not a.force and get_version(SOURCE) == sha:
        log("Sin cambios desde la última carga; omito.")
        return
    xls = pd.ExcelFile(io.BytesIO(content))
    sheet = next((s for s in xls.sheet_names if norm(s) == "monthly prices"), None)
    if not sheet:
        sys.exit(f"No encuentro la hoja 'Monthly Prices'. Hojas: {xls.sheet_names}")
    raw = xls.parse(sheet, header=None, dtype=object)
    if a.inspect:
        first, names, units = parse_sheet(raw)
        log("Primera fila de datos:", first, "| ejemplo:", [txt(x) for x in raw.iloc[first, :4]])
        for c, n in list(names.items())[:80]:
            log(f"  col {c}: {n}  [{units.get(c)}]")
        return
    run = start_run(SOURCE) if write else None
    try:
        obs, found = transform(raw, load_crop_map(), url, run)
        log(f"Series encontradas: {len(found)}; observaciones: {len(obs):,}")
        for n, u in found.items():
            log(f"  {n} [{u}]")
        write_sample("pinksheet_muestra.csv", obs)
        if write:
            n = sb_upsert("mk_observation", obs, "dedupe_key")
            mark_source(SOURCE, sha)
            finish_run(run, "ok", n, sha)
            sb_rpc("mk_refrescar")
            log("Filas escritas:", n)
    except Exception as e:
        finish_run(run, "error", error=str(e))
        raise


if __name__ == "__main__":
    main()
