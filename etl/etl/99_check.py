"""Revisión de calidad: resumen por fuente e indicador, pruebas de consistencia y tamaño de la base."""
import sys
from common import *


def main():
    if not sb_ready():
        sys.exit("Faltan SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY.")
    log("== Tamaño de la base de datos:", sb_rpc("mk_tamano"))
    log("== Filas por fuente e indicador")
    for r in sb_rpc("mk_resumen") or []:
        log(f"  {r['out_fuente']:<14} {r['out_indicador']:<22} {r['out_filas']:>9,}  {r['out_desde']} → {r['out_hasta']}")
    log("== Pruebas de calidad (todas deben ser 0, salvo filas_total)")
    problemas = 0
    for r in sb_rpc("mk_calidad") or []:
        marca = "" if r["out_prueba"] == "filas_total" or r["out_filas"] == 0 else "  <-- REVISAR"
        problemas += bool(marca)
        log(f"  {r['out_prueba']:<20} {r['out_filas']:>9,}{marca}")
    log("== Cultivos sin ninguna observación")
    con = {r["crop_id"] for r in sb_select("mk_observation", {"select": "crop_id", "crop_id": "not.is.null", "limit": 1000})}
    sin = [c for c in load_crop_map() if c not in con]
    log("  (solo se revisó una muestra de 1000 filas; usa el SQL de la lista de verificación para el recuento completo)")
    log("  posibles cultivos sin datos en la muestra:", sin)
    log("Resultado:", "hay problemas por revisar" if problemas else "sin problemas en las pruebas automáticas")


if __name__ == "__main__":
    main()
