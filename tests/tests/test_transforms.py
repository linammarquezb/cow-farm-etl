"""Pruebas con datos sintéticos (no usan internet ni Supabase). Ejecutar:  python tests/test_transforms.py"""
import sys, io, zipfile, importlib.util, tempfile, os
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "etl"))
import pandas as pd
from common import *


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace(".py", "").replace("-", "_").lstrip("0123456789_"),
                                                  ROOT / "etl" / name)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


crop_map = load_crop_map()
fails = []


def check(cond, msg):
    print(("OK   " if cond else "FALLA"), msg)
    if not cond:
        fails.append(msg)


# ---------- FAOSTAT ----------
fao = load("10_faostat.py")
rows = [
    ["'170", "Colombia", "'656", "Coffee, green", "Production", "2021", "t", "1000", "A"],
    ["'170", "Colombia", "'656", "Coffee, green", "Yield", "2021", "hg/ha", "9000", "E"],
    ["'170", "Colombia", "'656", "Coffee, green", "Area harvested", "2005", "ha", "900000", "A"],   # historia COL
    ["'076", "Brazil", "'656", "Coffee, green", "Production", "2005", "t", "5", "A"],               # fuera de rango
    ["'076", "Brazil", "'056", "Maize (corn)", "Production", "2020", "t", "7000", "X"],
    ["'001", "World", "'056", "Maize (corn)", "Production", "2020", "t", "9", "A"],                # mundo (M49 001)
    ["'5707", "Europe", "'056", "Maize (corn)", "Production", "2020", "t", "11", "A"],              # región: se omite
    ["'170", "Colombia", "'056", "Maize (corn)", "Production", "2020", "t", "", "M"],               # faltante
    ["'170", "Colombia", "'999", "Cosa rara", "Production", "2020", "t", "1", "A"],
]
df = pd.DataFrame(rows, columns=["Area Code (M49)", "Area", "Item Code (CPC)", "Item", "Element", "Year", "Unit", "Value", "Flag"])
itemmap = term_index(crop_map, "faostat_item_patron")
geos, matched = {}, set()
keep = lambda y, iso3: y >= 2018 or (iso3 == "COL" and y >= 2000)
obs = fao.process_chunk(df, "QCL", itemmap, keep, None, "http://x/qcl.zip", dt.date(2025, 1, 1), None, geos, matched)
ind = {(o["geo_id"], o["indicador"], o["periodo_inicio"][:4]): o for o in obs}
check(("COL", "produccion", "2021") in ind and ind[("COL", "produccion", "2021")]["estado_dato"] == "observado", "FAOSTAT: producción de café en Colombia observada")
check(ind[("COL", "rendimiento", "2021")]["valor"] == 900 and ind[("COL", "rendimiento", "2021")]["unidad"] == "kg/ha", "FAOSTAT: hg/ha -> kg/ha (9000 -> 900)")
check(ind[("COL", "rendimiento", "2021")]["estado_dato"] == "estimado", "FAOSTAT: bandera E -> estimado")
check(("COL", "area_cosechada", "2005") in ind, "FAOSTAT: historia larga de Colombia")
check(("BRA", "produccion", "2005") not in ind, "FAOSTAT: Brasil 2005 excluido")
check("WLD" in geos and "BRA" in geos and len(geos) == 3, f"FAOSTAT: países y mundo en geografía, regiones omitidas ({sorted(geos)})")
check(not any(o["valor"] is None for o in obs) and len(obs) == 5, f"FAOSTAT: sin nulos y 5 observaciones (hay {len(obs)})")
check({"cafe", "maiz"} <= matched, "FAOSTAT: cultivos con coincidencia")
check(all(o["url_procedencia"] and o["unidad"] for o in obs), "FAOSTAT: trazabilidad completa (URL y unidad)")
tcl = pd.DataFrame([["'170", "Colombia", "'656", "Coffee, green", "Export value", "2021", "1000 USD", "2500000", "A"]],
                   columns=df.columns)
o2 = fao.process_chunk(tcl, "TCL", itemmap, keep, None, "u", None, None, {}, set())
check(o2 and o2[0]["valor"] == 2.5e9 and o2[0]["unidad"] == "USD" and o2[0]["moneda"] == "USD", "FAOSTAT TCL: 1000 USD -> USD")
pp = pd.DataFrame([["'170", "Colombia", "'656", "Coffee, green", "Producer Price (USD/tonne)", "2021", "USD", "3000", "A", "Annual value"],
                   ["'170", "Colombia", "'656", "Coffee, green", "Producer Price (USD/tonne)", "2021", "USD", "3100", "A", "March"]],
                  columns=list(df.columns) + ["Months"])
o3 = fao.process_chunk(pp, "PP", itemmap, keep, None, "u", None, None, {}, set())
check({o["periodicidad"] for o in o3} == {"anual", "mensual"} and all(o["moneda"] == "USD" and o["tipo_precio"] == "productor" for o in o3), "FAOSTAT PP: anual y mensual con moneda")
# ZIP de extremo a extremo
tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
with zipfile.ZipFile(tmp.name, "w") as z:
    z.writestr("Production_Crops_Livestock_E_All_Data_(Normalized).csv", df.to_csv(index=False))
    z.writestr("Production_Crops_Livestock_E_Flags.csv", "Flag,Description\nA,Official\n")
n = sum(len(c) for c in fao.read_zip_csv(tmp.name))
check(n == len(df), "FAOSTAT: lectura del ZIP elige el CSV normalizado")

# ---------- Pink Sheet ----------
pk = load("20_pinksheet.py")
grid = [["World Bank Commodity Price Data (The Pink Sheet)"] + [None] * 6, [None] * 7,
        [None, "Coffee, Arabica", "Coffee, Robusta", "Maize", "Banana, Europe", "Gold", "Crude oil, Brent"],
        [None, "($/kg)", "($/kg)", "($/mt)", "($/kg)", "($/troy oz)", "($/bbl)"],
        ["1960M01", "..", "..", "50.1", "0.2", "35", "1.5"],
        ["2024M12", "4.2", "2.1", "198.5", "1.1", "2600", "74"]]
xl = pd.DataFrame(grid)
obs, found = pk.transform(xl, crop_map, "http://wb/x.xlsx")
check(set(found) == {"Coffee, Arabica", "Coffee, Robusta", "Maize", "Banana, Europe"}, f"Pink Sheet: series detectadas {sorted(found)}")
check(len(obs) == 4 + 4 - 2 and all(o["moneda"] == "USD" for o in obs), f"Pink Sheet: '..' omitido ({len(obs)} obs)")
check({o["unidad"] for o in obs if o["producto_original"] == "Maize"} == {"t"}, "Pink Sheet: $/mt -> t")

# ---------- EVA ----------
eva = load("30_eva.py")
d = pd.DataFrame({
    "c_digo_dane_departamento": ["76", "76", "76", "5"], "departamento": ["VALLE DEL CAUCA"] * 3 + ["ANTIOQUIA"],
    "c_digo_dane_municipio": ["76520", "76520", "76520", "5001"], "municipio": ["PALMIRA"] * 3 + ["MEDELLIN"],
    "grupo_de_cultivo": ["CEREALES"] * 3 + ["FRUTALES"], "cultivo": ["MAIZ", "MAIZ AMARILLO", "TOMATE DE ARBOL", "FRESA"],
    "a_o": ["2018", "2018", "2018", "2018"], "periodo": ["2018A", "2018A", "2018A", "2018"],
    "rea_sembrada_ha": ["100", "50", "10", "20"], "rea_cosechada_ha": ["90", "50", "9", "20"],
    "producci_n_t": ["300", "200", "50", "400"], "rendimiento_t_ha": ["3.3", "4", "5", "20"]})
obs, geos, cruce = eva.transform(d, crop_map, "https://www.datos.gov.co/d/x", dt.date(2024, 1, 1))
pal = {(o["indicador"], o["geo_id"]): o for o in obs if o["crop_id"] == "maiz"}
check(pal[("produccion", "COL-76520")]["valor"] == 500, "EVA: suma de dos variedades de maíz en Palmira (300+200)")
check(pal[("rendimiento", "COL-76")]["valor"] == 500 / 140 and pal[("rendimiento", "COL-76")]["unidad"] == "t/ha", "EVA: rendimiento departamental calculado")
check(pal[("area_cosechada", "COL")]["valor"] == 140 and "agregado" in pal[("area_cosechada", "COL")]["marca_fuente"], "EVA: nacional = suma de departamentos, etiquetado")
check(cruce.get("TOMATE DE ARBOL") == "", "EVA: 'tomate de árbol' NO se cruza con tomate")
check(pal[("produccion", "COL-76520")]["periodicidad"] == "semestral" and pal[("produccion", "COL-76520")]["periodo_fin"] == "2018-06-30", "EVA: periodo 2018A -> primer semestre")
fr = [o for o in obs if o["crop_id"] == "fresa" and o["geo_id"] == "COL-05001"]
check(fr and fr[0]["periodicidad"] == "anual", "EVA: periodo solo año -> anual")
check({"COL-76520", "COL-76", "COL", "COL-05001"} <= {g["id"] for g in geos}, "EVA: geografía jerárquica (país, depto, municipio)")

print("\nRESULTADO:", "TODO OK" if not fails else f"{len(fails)} fallas: {fails}")
sys.exit(1 if fails else 0)
