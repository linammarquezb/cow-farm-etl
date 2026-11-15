# COW-FARM · Data pipeline

Python scripts that load agricultural market data into the COW-FARM database.
Built by team **Las Vacas Cósmicas** for the NASA Space Apps Challenge 2026,
challenge "Course Correction: Adapting Farms with NASA Data".

- App repository: https://github.com/linammarquezb/cow-farm

## What it does

COW-FARM is a decision-support web app that compares three crop rotation
strategies for a field. Its Market module shows production, trade and price
data with full traceability. This repository contains the pipeline that
downloads that data from open sources, checks it, and loads it into the
database the app reads from.

## Data sources

| Source | What is loaded | Period |
| --- | --- | --- |
| FAOSTAT (FAO) | Production, harvested area, yield, trade and producer prices by country | 2000–2024 |
| World Bank Commodity Price Data ("Pink Sheet") | Monthly international commodity prices in USD | 1960–2026 |
| UPRA, Evaluaciones Agropecuarias Municipales (EVA), datos.gov.co | Planted area, harvested area and production by municipality in Colombia | 2010–2018 |

About 560,000 observations are loaded, covering 44 of the 51 crops in the app.

## Repository contents

- `etl/` — one loader per source, plus shared helpers
  - `00_load_reference.py` — sources, crop mapping and base geography
  - `10_faostat.py` — FAOSTAT production, trade and producer prices
  - `20_pinksheet.py` — World Bank Pink Sheet prices
  - `30_eva.py` — Colombian municipal agricultural survey (EVA)
  - `40_sipsa.py` — DANE SIPSA wholesale prices (raw capture only, not yet transformed)
  - `50_import_generic.py` — generic importer driven by a mapping file
  - `99_check.py` — quality checks
  - `common.py` — shared functions
- `data/` — reference files: source registry, crop-to-source mapping, database schema
- `tests/` — unit tests for the transformations
- `cow_farm_etl_colab.ipynb` — notebook to run the pipeline in Google Colab
- `requirements.txt` — Python dependencies

## How to run

1. Install the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Set two environment variables with your own database credentials.
   Never write them in the code or commit them:

   ```bash
   export SUPABASE_URL="https://<your-project>.supabase.co"
   export SUPABASE_SERVICE_ROLE_KEY="<your-service-role-key>"
   ```

3. Load the reference data, then test each loader without writing anything:

   ```bash
   python etl/00_load_reference.py
   python etl/10_faostat.py --dry-run
   python etl/20_pinksheet.py --dry-run
   python etl/30_eva.py --dry-run
   ```

4. Review the samples and the crop matching saved in `output/`, then run the
   same commands without `--dry-run` to load the data.

5. Run the quality checks:

   ```bash
   python etl/99_check.py
   ```

Useful options: `--dry-run` (no writes), `--force` (reload even if the source
has not changed), `--sample N` (process only the first N rows, for tests).

## Data rules

- No value is invented, estimated or filled in. Missing data stays missing.
- Every observation keeps its source, URL, publication date, period,
  geographic level, unit, currency and price type.
- Currencies are never converted.
- Each production figure from EVA keeps the physical state reported by the
  source (for example dry grain or fresh fruit).
- Crop names from each source are matched to the app's crops through a
  reviewed mapping file; unmatched crops are left out.

## Status

FAOSTAT, Pink Sheet and EVA are loaded and reviewed. Wholesale prices (SIPSA)
and trade-partner data are planned for a later phase.

## AI acknowledgement

The code in this repository was generated with AI assistance (Claude, by
Anthropic) and reviewed by the team. The data loaded comes from the cited
sources, not from AI.

## Disclaimer

Independent project by team Las Vacas Cósmicas. It is not official and is not
endorsed by NASA or by any of the data providers.
