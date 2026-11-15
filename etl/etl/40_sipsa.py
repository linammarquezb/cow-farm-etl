"""SIPSA (DANE): servicio web SOAP. FASE 1 = guardar los datos crudos sin perder nada.

IMPORTANTE: según la guía del servicio, los métodos devuelven solo registros que aún no se han consultado, así que
cada llamada puede "consumir" datos. Por eso este script guarda primero TODO lo recibido en output/raw/ y en la
tabla mk_raw, antes de cualquier transformación. No hagas llamadas de prueba aparte: usa --list para ver los métodos.

Uso:
  python etl/40_sipsa.py --list                 # muestra los métodos del WSDL (no consume datos)
  python etl/40_sipsa.py --op NOMBRE            # llama a un método y guarda lo recibido
  python etl/40_sipsa.py                        # llama a los métodos de SIPSA_OPERATIONS (o los predeterminados)
La transformación a mk_observation se escribe cuando conozcamos los campos reales (ver la lista de verificación).
"""
import argparse, json, os, sys
from common import *

SOURCE = "dane_sipsa"
WSDL = os.environ.get("SIPSA_WSDL", "http://appweb.dane.gov.co/sipsaWS/SrvSipsaUpraBeanService?WSDL")
DEFAULT_OPS = ["promediosSipsaCiudad", "promedioAbasSipsaMesMadr", "promediosSipsaMesMadr", "promediosSipsaSemanaMadr"]


def to_jsonable(o):
    if isinstance(o, dict):
        return {k: to_jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [to_jsonable(v) for v in o]
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    if isinstance(o, (bytes, bytearray)):
        return o.decode("utf-8", "replace")
    return o


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--op", action="append")
    ap.add_argument("--dry-run", action="store_true", help="Guarda solo en output/raw (no en Supabase)")
    a = ap.parse_args()
    from zeep import Client
    from zeep.helpers import serialize_object
    client = Client(WSDL)
    ops = sorted({op for s in client.wsdl.services.values() for p in s.ports.values()
                  for op in p.binding._operations})
    if a.list:
        log("Métodos disponibles en el WSDL:")
        for o in ops:
            log(" -", o)
        return
    wanted = a.op or [o for o in os.environ.get("SIPSA_OPERATIONS", "").split(",") if o] or DEFAULT_OPS
    write = sb_ready() and not a.dry_run
    rawdir = OUT / "raw"
    rawdir.mkdir(exist_ok=True)
    run = start_run(SOURCE) if write else None
    total = 0
    try:
        for op in wanted:
            if op not in ops:
                log(f"[{op}] NO existe en el WSDL. Métodos disponibles: {ops}")
                continue
            log(f"[{op}] llamando…")
            res = to_jsonable(serialize_object(getattr(client.service, op)()))
            recs = res if isinstance(res, list) else [res]
            stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            path = rawdir / f"sipsa_{op}_{stamp}.json"
            path.write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")  # 1.º a disco
            log(f"[{op}] {len(recs):,} registros guardados en {path}")
            for r in recs[:3]:
                log("   ejemplo:", json.dumps(r, ensure_ascii=False)[:300])
            if write:
                rows = [{"source_id": SOURCE, "endpoint": op, "parametros": {}, "payload": recs[i:i + 500],
                         "ingest_run_id": run} for i in range(0, len(recs), 500)]
                sb_insert("mk_raw", rows, batch=20)
            total += len(recs)
        finish_run(run, "ok" if total else "sin_cambios", total, f"raw:{dt.date.today()}")
        if write:
            mark_source(SOURCE, f"raw:{dt.date.today()}")
    except Exception as e:
        finish_run(run, "error", error=str(e))
        raise
    log("SIPSA (crudo) terminado. Registros:", total)


if __name__ == "__main__":
    main()
