-- cow-farm · Módulo Mercado · Esquema Supabase (PostgreSQL)
-- Ejecutar en Supabase > SQL Editor. Lectura pública; escritura solo con la clave de servicio (ETL).
create extension if not exists unaccent;
create extension if not exists pg_trgm;

-- 1) Fuentes (registro de qué se consulta y cuándo se actualizó)
create table if not exists mk_source (
  id text primary key,
  nombre text not null,
  organismo text,
  url text,
  acceso text,
  requiere_clave text,
  limites_y_notas text,
  frecuencia_publicacion text,
  datos_que_aporta text,
  periodicidad_dato text,
  estado_verificacion text,
  licencia_o_terminos text,
  ultima_ingesta_ok timestamptz,
  ultima_version_fuente text
);

-- 2) Geografía jerárquica (mundo > país > departamento/estado > municipio/condado; mercados mayoristas aparte)
create table if not exists mk_geo (
  id text primary key,                 -- 'WLD', 'COL' (ISO3), 'COL-76' (departamento), 'COL-76520' (municipio, código DANE)
  nivel text not null check (nivel in ('mundo','pais','departamento','municipio','mercado')),
  nombre text not null,
  nombre_norm text,                    -- minúsculas y sin tildes (lo llena el ETL) para cruzar nombres
  padre_id text references mk_geo(id),
  iso3 text,
  lat double precision,
  lon double precision
);
create index if not exists mk_geo_padre_idx on mk_geo(padre_id);
create index if not exists mk_geo_nombre_trgm on mk_geo using gin (nombre_norm gin_trgm_ops);

-- 3) Mapa cultivo cow-farm -> códigos de cada fuente (se importa de crop_market_map.csv)
create table if not exists mk_crop_map (
  crop_id text primary key,
  nombre_es text,
  faostat_item_patron text,
  hs_codigo text,
  hs_descripcion text,
  wb_pinksheet_serie text,
  usda_psd_commodity text,
  sipsa_terminos text,
  eva_terminos text,
  verificado text,
  nota text
);

-- 4) Observaciones (formato largo; una fila = un dato con toda su trazabilidad)
create table if not exists mk_observation (
  id bigserial primary key,
  dedupe_key text not null unique,     -- hash de (fuente, indicador, producto, geo, socio, periodo, tipo_precio, unidad)
  source_id text not null references mk_source(id),
  indicador text not null check (indicador in (
    'precio_productor','precio_mayorista','precio_minorista','precio_internacional',
    'area_sembrada','area_cosechada','produccion','rendimiento',
    'oferta_abastecimiento','consumo_uso','existencias',
    'exportacion_valor','exportacion_cantidad','importacion_valor','importacion_cantidad')),
  crop_id text references mk_crop_map(crop_id),
  producto_original text not null,     -- nombre tal como lo publica la fuente
  geo_id text not null references mk_geo(id),
  nivel_geografico text not null,      -- nivel del dato publicado (puede diferir del consultado)
  contraparte_geo_id text references mk_geo(id),   -- socio comercial o país de destino/origen (comercio)
  periodo_inicio date not null,
  periodo_fin date not null,
  periodicidad text not null check (periodicidad in ('diaria','semanal','mensual','trimestral','semestral','anual')),
  valor numeric,
  unidad text not null,
  moneda text,                         -- null si no aplica (p. ej., toneladas)
  tipo_precio text,                    -- productor | mayorista | minorista | internacional | fob | cif ...
  mercado text,                        -- central de abastos o mercado de referencia, si existe
  estado_dato text not null check (estado_dato in ('observado','estimado','proyectado')),
  marca_fuente text,                   -- bandera/nota original de la fuente (p. ej. flag de FAOSTAT)
  url_procedencia text not null,
  fecha_publicacion date,
  fecha_consulta timestamptz not null default now(),
  ingest_run_id bigint
);
create index if not exists mk_obs_clave_idx on mk_observation (crop_id, indicador, geo_id, periodo_inicio desc);
create index if not exists mk_obs_fuente_idx on mk_observation (source_id, indicador);
create index if not exists mk_obs_contraparte_idx on mk_observation (crop_id, indicador, contraparte_geo_id, periodo_inicio desc);

-- 5) Bitácora de cargas
create table if not exists mk_ingest_run (
  id bigserial primary key,
  source_id text not null references mk_source(id),
  inicio timestamptz not null default now(),
  fin timestamptz,
  estado text check (estado in ('en_curso','ok','sin_cambios','error')),
  filas_nuevas int default 0,
  filas_actualizadas int default 0,
  version_fuente text,
  error text
);

-- 6) Radar de oportunidades (lo llena una función programada; cada fila trae su sustento)
create table if not exists mk_radar_config (
  tipo text primary key,
  umbral numeric not null,
  ventana_meses int not null,
  min_observaciones int not null default 6,
  descripcion text
);
insert into mk_radar_config (tipo, umbral, ventana_meses, min_observaciones, descripcion) values
 ('precio_mayorista_al_alza', 0.15, 3, 6, 'Variación de la media de los últimos 3 meses vs. los mismos meses del año anterior, mismo mercado, unidad y tipo de precio'),
 ('produccion_regional_en_alza', 0.20, 12, 2, 'Variación interanual de la producción en el departamento o municipio'),
 ('importaciones_en_alza', 0.10, 36, 3, 'Crecimiento anual compuesto de las importaciones reportadas por un país'),
 ('demanda_internacional_en_alza', 0.05, 36, 3, 'Crecimiento anual compuesto de las importaciones mundiales reportadas')
on conflict (tipo) do nothing;

create table if not exists mk_oportunidad (
  id bigserial primary key,
  tipo text not null references mk_radar_config(tipo),
  crop_id text references mk_crop_map(crop_id),
  geo_id text references mk_geo(id),
  contraparte_geo_id text references mk_geo(id),
  periodo_analizado_inicio date not null,
  periodo_analizado_fin date not null,
  valor_actual numeric,
  valor_referencia numeric,
  variacion numeric,
  unidad text,
  nivel_confianza text check (nivel_confianza in ('alta','media','baja')),
  observaciones_ids bigint[] not null,  -- datos que sustentan la señal
  fuentes text[] not null,
  limitaciones text not null default 'No se afirma rentabilidad: faltan costos, calidad, logística y condiciones comerciales.',
  generada_en timestamptz not null default now(),
  vigente_hasta timestamptz
);
create index if not exists mk_oport_idx on mk_oportunidad (crop_id, geo_id, generada_en desc);

-- 7) Vistas para que la app cargue rápido
create or replace view mk_ultimo as
select distinct on (crop_id, indicador, geo_id, contraparte_geo_id, tipo_precio, unidad, moneda, periodicidad, mercado)
  *
from mk_observation
order by crop_id, indicador, geo_id, contraparte_geo_id, tipo_precio, unidad, moneda, periodicidad, mercado, periodo_inicio desc;

-- Estacionalidad: índice mensual = promedio del (valor del mes / promedio del año), con >= 3 años y >= 10 puntos por año
drop materialized view if exists mk_estacionalidad;
create materialized view mk_estacionalidad as
with m as (
  select crop_id, geo_id, indicador, coalesce(tipo_precio,'') as tipo_precio, unidad, coalesce(moneda,'') as moneda,
         extract(year from periodo_inicio)::int as anio, extract(month from periodo_inicio)::int as mes,
         avg(valor) as v, count(*) as n
  from mk_observation
  where indicador in ('precio_mayorista','precio_productor','oferta_abastecimiento')
    and periodicidad in ('diaria','semanal','mensual') and estado_dato = 'observado' and valor is not null
  group by 1,2,3,4,5,6,7,8
), y as (
  select crop_id, geo_id, indicador, tipo_precio, unidad, moneda, anio, avg(v) as vy, count(*) as meses
  from m group by 1,2,3,4,5,6,7
)
select m.crop_id, m.geo_id, m.indicador, m.tipo_precio, m.unidad, m.moneda, m.mes,
       avg(m.v / nullif(y.vy,0)) as indice_estacional,
       count(distinct m.anio) as anios_usados,
       min(m.anio) as desde, max(m.anio) as hasta
from m join y using (crop_id, geo_id, indicador, tipo_precio, unidad, moneda, anio)
where y.meses >= 10
group by 1,2,3,4,5,6,7
having count(distinct m.anio) >= 3;
create index if not exists mk_estac_idx on mk_estacionalidad (crop_id, geo_id, indicador, mes);

-- 8) Mejor nivel geográfico disponible (municipio > departamento > país), identificando que es alternativa
create or replace function mk_mejor_nivel(p_crop text, p_indicador text, p_geo text)
returns table(out_geo_id text, out_nivel text, out_es_alternativa boolean, out_ultimo_periodo date)
language sql stable as $$
  with recursive cadena as (
    select g.id, g.nivel, g.padre_id, 0 as paso from mk_geo g where g.id = p_geo
    union all
    select g.id, g.nivel, g.padre_id, c.paso + 1 from mk_geo g join cadena c on g.id = c.padre_id
  )
  select c.id, c.nivel, (c.paso > 0), max(o.periodo_inicio)
  from cadena c
  join mk_observation o on o.geo_id = c.id and o.crop_id = p_crop and o.indicador = p_indicador
  group by c.id, c.nivel, c.paso
  order by c.paso
  limit 1;
$$;


-- 8b) Datos crudos (respaldo de lo descargado, p. ej. SIPSA, cuyo servicio entrega cada registro una sola vez)
create table if not exists mk_raw (
  id bigserial primary key,
  source_id text not null references mk_source(id),
  endpoint text not null,
  parametros jsonb,
  payload jsonb not null,
  fetched_at timestamptz not null default now(),
  ingest_run_id bigint
);

-- 8c) Funciones de apoyo para el ETL y las revisiones de calidad
create or replace function mk_refrescar() returns void language plpgsql security definer as $$
begin refresh materialized view mk_estacionalidad; end $$;

create or replace function mk_resumen()
returns table(out_fuente text, out_indicador text, out_filas bigint, out_desde date, out_hasta date, out_ultima_consulta timestamptz)
language sql stable as $$
  select source_id, indicador, count(*), min(periodo_inicio), max(periodo_fin), max(fecha_consulta)
  from mk_observation group by 1, 2 order by 1, 2;
$$;

create or replace function mk_calidad()
returns table(out_prueba text, out_filas bigint)
language sql stable as $$
  select 'valores_negativos', count(*) from mk_observation where valor < 0
  union all select 'sin_unidad', count(*) from mk_observation where coalesce(unidad,'') = ''
  union all select 'sin_url', count(*) from mk_observation where coalesce(url_procedencia,'') = ''
  union all select 'periodo_invertido', count(*) from mk_observation where periodo_inicio > periodo_fin
  union all select 'precio_sin_moneda', count(*) from mk_observation where indicador like 'precio%' and moneda is null
  union all select 'filas_total', count(*) from mk_observation;
$$;

create or replace function mk_tamano() returns text language sql stable security definer as $$
  select pg_size_pretty(pg_database_size(current_database()));
$$;

-- Las funciones de mantenimiento solo las puede ejecutar el ETL (service_role), no la app pública
revoke execute on function mk_refrescar() from public, anon, authenticated;
revoke execute on function mk_tamano() from public, anon, authenticated;
grant execute on function mk_refrescar() to service_role;
grant execute on function mk_tamano() to service_role;

-- 9) Seguridad: lectura pública, escritura solo con service_role (el ETL)
alter table mk_source enable row level security;
alter table mk_geo enable row level security;
alter table mk_crop_map enable row level security;
alter table mk_observation enable row level security;
alter table mk_ingest_run enable row level security;
alter table mk_radar_config enable row level security;
alter table mk_oportunidad enable row level security;
alter table mk_raw enable row level security;  -- sin política pública: solo el ETL (service_role) puede leer o escribir
do $$ declare t text; begin
  foreach t in array array['mk_source','mk_geo','mk_crop_map','mk_observation','mk_ingest_run','mk_radar_config','mk_oportunidad'] loop
    execute format('drop policy if exists "lectura publica" on public.%I', t);
    execute format('create policy "lectura publica" on public.%I for select using (true)', t);
  end loop;
end $$;
