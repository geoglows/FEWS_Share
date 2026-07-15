#!/usr/bin/env python3
"""
build_cells_h3.py — bin flood points into H3 hexagons at several resolutions.

Reads:  ../flood_points.json   per-point forecasts with lat/lon
Writes: data.geojson           one GeoJSON FeatureCollection; each feature tagged
                               with `res`, plus a top-level `resolutions` member
"""

import json
import os
import sys
import inspect

RESOLUTIONS = [3, 4, 5, 6]
FIX_ANTIMERIDIAN = "split"

HERE = os.path.dirname(os.path.abspath(__file__))       
DATA_ROOT = os.path.dirname(os.path.dirname(HERE))         
FRONT_END = os.path.join(os.path.dirname(HERE), "Front_End")
POINTS_JSON = os.path.join(DATA_ROOT, "flood_points.json")
OUTPUT = os.path.join(FRONT_END, "data.geojson")

SEVERITY_RANK = {
    "none": 0, "minor": 1, "moderate": 2, "major": 3, "severe": 4, "extreme": 5,
}


def worst_severity(forecasts):
    best, best_rank = "", -1
    for fc in forecasts:
        r = SEVERITY_RANK.get(str(fc.get("severity", "")).lower(), -1)
        if r > best_rank:
            best_rank, best = r, str(fc.get("severity", "")).lower()
    return best


def _pick(params, names):
    return next((n for n in names if n in params), None)


def detect_token_col(orig_cols, orig_index, gridded, pd):
    new_cols = [c for c in gridded.columns if c not in orig_cols]
    token_candidates = [c for c in new_cols if not str(c).endswith("_res")]
    if token_candidates:
        return token_candidates[0], gridded
    if gridded.index.name not in (None, orig_index):
        return gridded.index.name, gridded.reset_index()
    if not isinstance(gridded.index, pd.RangeIndex) and gridded.index.name is None:
        return "h3", gridded.rename_axis("h3").reset_index()
    return None, gridded


def cells_to_geom(uniq_df, token_col, fix):
    h32geo = uniq_df.h3.h32geo
    params = inspect.signature(h32geo).parameters
    col_kw = _pick(params, ("h3_col", "h3_column", "column", "col"))
    kwargs = {col_kw: token_col} if col_kw else {}
    if "fix_antimeridian" in params:
        kwargs["fix_antimeridian"] = fix
    try:
        gdf = h32geo(**kwargs)
    except (ValueError, TypeError) as e:
        print(f"  note: h32geo({kwargs}) failed ({e}); retrying without "
              f"fix_antimeridian.", file=sys.stderr)
        kwargs.pop("fix_antimeridian", None)
        gdf = h32geo(**kwargs)
    if token_col in gdf.columns:
        return dict(zip(gdf[token_col], gdf.geometry))
    return dict(zip(gdf.index, gdf.geometry))


def features_for_resolution(df, res, latlon_kwargs, orig_cols, orig_index, pd, mapping):
    gridded = df.h3.latlon2h3(res, **latlon_kwargs)
    token_col, gridded = detect_token_col(orig_cols, orig_index, gridded, pd)
    if token_col is None:
        sys.exit(
            f"Could not find the H3 cell column added by latlon2h3 (res {res}).\n"
            f"  columns now: {list(gridded.columns)}\n"
            f"  index name:  {gridded.index.name}"
        )

    uniq = pd.DataFrame({token_col: sorted(gridded[token_col].unique())})
    geom_by_token = cells_to_geom(uniq, token_col, FIX_ANTIMERIDIAN)

    drop_cols = [c for c in gridded.columns
                 if c == token_col or (str(c).endswith("_res") and c not in orig_cols)]
    features = []
    for token, sub in gridded.groupby(token_col, sort=False):
        geom = geom_by_token.get(token)
        if geom is None:
            print(f"  warning: no geometry for cell {token} (res {res})", file=sys.stderr)
            continue
        fcs = sub.drop(columns=drop_cols).to_dict("records")
        features.append({
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": {
                "res": res,
                "cell_id": token,
                "severity": worst_severity(fcs),
                "model_count": len(fcs),
                "forecasts": fcs,
            },
        })
    return features


def main():
    try:
        import pandas as pd
        from shapely.geometry import mapping
        from vgridpandas import h3pandas  # noqa: F401
    except ImportError as e:
        sys.exit(
            f"Missing dependency: {e.name}. This build needs your GDAL/vgridpandas "
            f"environment:\n"
            f"    conda install -c conda-forge gdal geopandas\n"
            f"    pip install vgridpandas"
        )

    if not os.path.exists(POINTS_JSON):
        sys.exit(f"Not found: {POINTS_JSON}\nRun csv_to_json_vgrid.py first.")

    with open(POINTS_JSON, encoding="utf-8") as f:
        points = json.load(f)
    print(f"Read {len(points)} flood point(s).")

    df = pd.DataFrame(points)
    before = len(df)
    df = df[pd.to_numeric(df["lat"], errors="coerce").notna()
            & pd.to_numeric(df["lon"], errors="coerce").notna()].copy()
    if len(df) < before:
        print(f"  note: {before - len(df)} point(s) had no usable coordinate "
              f"and were skipped.", file=sys.stderr)
    df["lat"] = df["lat"].astype(float)
    df["lon"] = df["lon"].astype(float)

    l2h_params = inspect.signature(df.h3.latlon2h3).parameters
    lat_kw = _pick(l2h_params, ("lat_col",))
    lon_kw = _pick(l2h_params, ("lon_col", "lng_col", "long_col", "longitude_col"))
    latlon_kwargs = {}
    if lat_kw:
        latlon_kwargs[lat_kw] = "lat"
    if lon_kw:
        latlon_kwargs[lon_kw] = "lon"

    orig_cols = list(df.columns)
    orig_index = df.index.name

    all_features = []
    for res in RESOLUTIONS:
        feats = features_for_resolution(df, res, latlon_kwargs,
                                        orig_cols, orig_index, pd, mapping)
        all_features.extend(feats)
        print(f"  res {res}: {len(feats)} hexagon(s)")

    payload = {
        "type": "FeatureCollection",
        "kind": "h3-telescoping",
        "resolutions": RESOLUTIONS,
        "features": all_features,
    }
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"Wrote {len(RESOLUTIONS)} resolution(s), {len(all_features)} feature(s) "
          f"total -> {OUTPUT}")


if __name__ == "__main__":
    main()
