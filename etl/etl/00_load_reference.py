"""Paso 0: carga el registro de fuentes, el mapa de cultivos y la geografía base en Supabase."""
import csv, sys
from common import *


def main():
    if not sb_ready():
        sys.exit("Faltan SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY (ver .env.example).")
    with open(DATA / "market_sources.csv", encoding="utf-8") as f:
        src = [{k: (v or None) for k, v in r.items()} for r in csv.DictReader(f)]
    log("mk_source:", sb_upsert("mk_source", src, "id"), "filas")
    with open(DATA / "crop_market_map.csv", encoding="utf-8") as f:
        crops = [{k: (v or None) for k, v in r.items()} for r in csv.DictReader(f)]
    log("mk_crop_map:", sb_upsert("mk_crop_map", crops, "crop_id"), "filas")
    geo = [WORLD, geo_row("COL", "pais", "Colombia", "WLD", "COL")]
    log("mk_geo:", sb_upsert("mk_geo", geo, "id"), "filas")
    log("Listo. Siguiente: python etl/10_faostat.py --dry-run")


if __name__ == "__main__":
    main()
