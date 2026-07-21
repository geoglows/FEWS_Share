# FEWS4All
A web map that flags flood-forecast locations (H3 grid cells or HydroBASINS basins)
and shows each model's forecast on click. Data comes from GEOGLOWS and Google Flood Hub.

## Stack

Built with **Vite** — it bundles the app, serves it with hot reload during
development, and produces the static `dist/` build. Dependencies come from npm
(Leaflet, Iconify, Tailwind CSS v4 via `@tailwindcss/vite`), so there are no CDN
`<script>` tags. Vite also replaces VS Code Go Live — `npm run dev` is the dev server.

## Run the app

```
npm install
npm run dev        # dev server at http://localhost:5173
npm run build      # static build -> dist/
npm run preview    # preview the built site
```

## Layout

- `index.html` — page shell, loads `/src/main.js`
- `src/main.js` — map, panel, legend, data loading
- `src/style.css` — Tailwind + theme + Leaflet overrides
- `public/` — static assets copied as-is (favicon, logo)
- `scripts/` — the Python pipeline that builds the GeoJSON
- `Files/` — input CSVs, basin parquets, impact stats (gitignored)

## Data

The map fetches its GeoJSON from CloudFront:

```
https://d3hbj0z0f67zhd.cloudfront.net/fews4all/data_basins.geojson
```

Regenerate it with the pipeline below, then upload the result.

### Grid (H3) pipeline

```
cd scripts
python csv_to_json_vgrid.py     # model CSVs -> flood_points.json
python build_cells_h3.py        # points     -> data.geojson
```

`build_cells_s2.py` is a drop-in alternative producing S2 cells instead of hexagons.

### Basin (HydroBASINS) pipeline

```
cd scripts
python csv_to_json_basins.py    # model CSVs           -> flood_state.json
python build_basins.py          # + HUC0{4..8}.parquet -> data.geojson
```

Basins telescope from level 4 (coarse) to level 8 (fine); GEOGLOWS rivers join via
`global_matches.csv`, Flood Hub gauges by point-in-polygon.

Both pipelines write the same `data.geojson` format, so whichever you run last is
what gets uploaded.

## Severity

Four tiers — none / warning / danger / extreme. Flood Hub uses its own labels;
GEOGLOWS is derived from return period (>=20yr extreme, >=5yr danger, >=2yr warning).
Thresholds and the mean-flow floor are set at the top of `csv_to_json_vgrid.py`.
