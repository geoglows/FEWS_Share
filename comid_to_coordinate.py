#!/usr/bin/env python3
"""
comid_to_coordinate.py — add lat/lon columns to the GEOGLOWS spreadsheet.

The GEOGLOWS export (Geoglows_2024-04-01-00.csv) is keyed by `comid` (the reach id
`LINKNO`) but carries no coordinate. This script looks up each comid IN THE
SPREADSHEET in the global stream network, takes one representative coordinate per
reach, and writes those coordinates back into the spreadsheet as new `lat`/`lon`
columns — updating the file in place rather than making a separate lookup.

Only the comids present in the spreadsheet are processed (not every reach in the
world), so no extra rows are produced.

This is a ONE-TIME build that lives in the FEWS_Share root (not a software
folder): rerun it only when the spreadsheet or the hydrography changes.

Reads (both in this folder, FEWS_Share):
  GEOGLOWS_CSV   the spreadsheet to update (needs a comid column)
  STREAMS_FILE   a stream network file (.gpkg / .parquet / ...) with a reach-id
                 column (LINKNO / comid / ...) and line geometry (or lat/lon cols)

Writes:
  GEOGLOWS_CSV   the same spreadsheet, now with `lat` and `lon` columns appended.

Representative point per reach:
  - if the stream file already has lat/lon columns, those are used as-is;
  - else the reach's OUTLET vertex (downstream end) is used — TDX-Hydro lines are
    digitized upstream->downstream, so the last vertex is the reach mouth, the
    most hydrologically meaningful single point. Falls back to a guaranteed
    on-geometry point for odd geometries.
Coordinates are reprojected to EPSG:4326 (lat/lon degrees) if needed.

Run it:
    pip install geopandas pyogrio pyarrow shapely
    python comid_to_coordinate.py                 # uses the CONFIG paths below
    python comid_to_coordinate.py streams.gpkg    # override the stream file
"""

import os
import sys

# ---------------------------------------------------------------------------
# CONFIG — files sit next to this script (FEWS_Share root).
# ---------------------------------------------------------------------------
GEOGLOWS_CSV = "Geoglows_2024-04-01-00.csv"       # <- spreadsheet to update in place
STREAMS_FILE = "global_streams_simplified.gpkg"   # <- stream network to look up in
# Candidate names for the reach-id column, tried in order (in both files).
ID_CANDIDATES = ["comid", "COMID", "ComID", "LINKNO", "LinkNo", "linkno",
                 "TDXHydroLinkNo", "river_id", "rivid"]
# Candidate names for explicit coordinate columns in the stream file (skip geometry if present).
LAT_CANDIDATES = ["lat", "Lat", "latitude", "Latitude", "y", "Y"]
LON_CANDIDATES = ["lon", "Lon", "longitude", "Longitude", "x", "X"]
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))


def _pick(cols, candidates):
    lower = {str(c).lower(): c for c in cols}
    for name in candidates:
        if name in cols:
            return name
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def outlet_point(geom):
    """Return (lon, lat) for a reach geometry: the downstream-most vertex of a
    line (its mouth), or a safe on-geometry point for anything else."""
    if geom is None or geom.is_empty:
        return None
    gt = geom.geom_type
    try:
        if gt == "LineString":
            x, y = geom.coords[-1]
            return (x, y)
        if gt == "MultiLineString":
            # last vertex of the last part (downstream end for u/s->d/s digitising)
            last = list(geom.geoms)[-1]
            x, y = last.coords[-1]
            return (x, y)
        if gt == "Point":
            return (geom.x, geom.y)
    except Exception:
        pass
    p = geom.representative_point()
    return (p.x, p.y)


def build_coords(stream_path, wanted):
    """Read the stream network, returning DataFrame[comid,lat,lon] for only the
    reaches whose id is in `wanted`."""
    import geopandas as gpd
    import pandas as pd

    ext = os.path.splitext(stream_path)[1].lower()
    if ext in (".parquet", ".geoparquet", ".pq"):
        try:
            gdf = gpd.read_parquet(stream_path)
        except Exception:
            gdf = pd.read_parquet(stream_path)
    else:
        gdf = gpd.read_file(stream_path)

    id_col = _pick(list(gdf.columns), ID_CANDIDATES)
    if id_col is None:
        sys.exit(f"No reach-id column in the stream file. Columns: {list(gdf.columns)}")
    ids_int = pd.to_numeric(gdf[id_col], errors="coerce").astype("Int64")
    keep = ids_int.isin(wanted)
    gdf = gdf.loc[keep].copy()
    comids = ids_int.loc[keep].astype("int64").values
    print(f"  reach-id column {id_col!r}; {len(gdf):,} matching reaches.")

    lat_col = _pick(list(gdf.columns), LAT_CANDIDATES)
    lon_col = _pick(list(gdf.columns), LON_CANDIDATES)
    if lat_col and lon_col:
        print(f"  using existing coordinate columns: {lat_col!r}, {lon_col!r}")
        out = pd.DataFrame({
            "comid": comids,
            "lat": pd.to_numeric(gdf[lat_col], errors="coerce").values,
            "lon": pd.to_numeric(gdf[lon_col], errors="coerce").values,
        })
    else:
        if not hasattr(gdf, "geometry"):
            sys.exit("No lat/lon columns and no geometry — can't derive points.")
        if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
            print(f"  reprojecting {gdf.crs} -> EPSG:4326")
            gdf = gdf.to_crs(4326)
        print("  deriving each reach's outlet point ...")
        pts = gdf.geometry.map(outlet_point)
        out = pd.DataFrame({
            "comid": comids,
            "lon": [p[0] if p else None for p in pts],
            "lat": [p[1] if p else None for p in pts],
        })

    return (out.dropna(subset=["lat", "lon"])
               .drop_duplicates(subset="comid")[["comid", "lat", "lon"]])


def main(argv=None):
    try:
        import geopandas as gpd
        import pandas as pd
    except ImportError as e:
        sys.exit(f"Missing dependency: {e.name}. Install:\n"
                 f"    pip install geopandas pyogrio pyarrow shapely")

    geo_path = os.path.join(HERE, GEOGLOWS_CSV)
    stream_path = (argv[0] if argv else None) or os.path.join(HERE, STREAMS_FILE)
    for p in (geo_path, stream_path):
        if not os.path.exists(p):
            sys.exit(f"Not found: {p}")

    # --- 1) which comids do we actually need? (only those in the spreadsheet) ---
    print(f"Reading spreadsheet: {geo_path}")
    geo = pd.read_csv(geo_path)
    geo_id = _pick(list(geo.columns), ID_CANDIDATES)
    if geo_id is None:
        sys.exit(f"No comid column in the spreadsheet. Columns: {list(geo.columns)}")
    geo[geo_id] = pd.to_numeric(geo[geo_id], errors="coerce").astype("Int64")
    wanted = set(int(x) for x in geo[geo_id].dropna().unique())
    print(f"  {len(geo):,} rows, {len(wanted):,} unique comids to locate.")

    # Don't clobber pre-existing lat/lon columns silently — drop them so the fresh
    # ones replace cleanly.
    geo = geo.drop(columns=[c for c in ("lat", "lon") if c in geo.columns])

    # --- 2) stream the network, collecting coords for only the wanted comids ---
    print(f"Scanning stream network: {stream_path}")
    coords = build_coords(stream_path, wanted)
    print(f"  found coordinates for {len(coords):,} of {len(wanted):,} comids.")

    # --- 3) merge coordinates back into the spreadsheet ------------------------
    coords = coords.rename(columns={"comid": geo_id})
    merged = geo.merge(coords, on=geo_id, how="left")

    # --- 4) coverage report ----------------------------------------------------
    missing_mask = merged["lat"].isna()
    n_missing = int(missing_mask.sum())
    print(f"  coordinates attached to {len(merged) - n_missing:,} / {len(merged):,} rows.")
    if n_missing:
        sample = merged.loc[missing_mask, geo_id].dropna().astype("int64").unique()[:10]
        print(f"  WARNING: {n_missing:,} row(s) have NO coordinate — their comid was "
              f"not in the stream file. e.g. {list(sample)}", file=sys.stderr)

    # --- 5) write the spreadsheet back in place --------------------------------
    merged.to_csv(geo_path, index=False)
    print(f"Updated {geo_path} (added lat, lon).")


if __name__ == "__main__":
    main(sys.argv[1:])
