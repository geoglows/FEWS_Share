#!/usr/bin/env python3
"""
build_cells_s2.py — bin flood points into S2 cells at several levels.

Reads:  ../flood_points.json   per-point forecasts with lat/lon
Writes: data.geojson           one GeoJSON FeatureCollection; each feature tagged
                               with `res` (the S2 level), plus a top-level
                               `resolutions` member
"""

import json
import os
import sys
import inspect

LEVELS = [2, 3, 4, 5, 6, 7, 8]
FIX_ANTIMERIDIAN = "split"

HERE = os.path.dirname(os.path.abspath(__file__))            # Software/Back_End
DATA_ROOT = os.path.dirname(os.path.dirname(HERE))           # FEWS_Share (data files)
FRONT_END = os.path.join(os.path.dirname(HERE), "Front_End")  # Software/Front_End
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
        return "s2", gridded.rename_axis("s2").reset_index()
    return None, gridded


def cells_to_geom(uniq_df, token_col, fix):
    s22geo = uniq_df.s2.s22geo
    params = inspect.signature(s22geo).parameters
    col_kw = _pick(params, ("s2_col", "s2_column", "column", "col"))
    kwargs = {col_kw: token_col} if col_kw else {}
    if "fix_antimeridian" in params:
        kwargs["fix_antimeridian"] = fix
    try:
        gdf = s22geo(**kwargs)
    except (ValueError, TypeError) as e:
        print(f"  note: s22geo({kwargs}) failed ({e}); retrying without "
              f"fix_antimeridian.", file=sys.stderr)
        kwargs.pop("fix_antimeridian", None)
        gdf = s22geo(**kwargs)
    if token_col in gdf.columns:
        return dict(zip(gdf[token_col], gdf.geometry))
    return dict(zip(gdf.index, gdf.geometry))


def features_for_level(df, level, latlon_kwargs, orig_cols, orig_index, pd, mapping):
    gridded = df.s2.latlon2s2(level, **latlon_kwargs)
    token_col, gridded = detect_token_col(orig_cols, orig_index, gridded, pd)
    if token_col is None:
        sys.exit(
            f"Could not find the S2 token column added by latlon2s2 (level {level}).\n"
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
            print(f"  warning: no geometry for cell {token} (level {level})", file=sys.stderr)
            continue
        fcs = sub.drop(columns=drop_cols).to_dict("records")
        features.append({
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": {
                "res": level,
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
        from vgridpandas import s2pandas  # noqa: F401
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

    l2s_params = inspect.signature(df.s2.latlon2s2).parameters
    lat_kw = _pick(l2s_params, ("lat_col",))
    lon_kw = _pick(l2s_params, ("lon_col", "lng_col", "long_col", "longitude_col"))
    latlon_kwargs = {}
    if lat_kw:
        latlon_kwargs[lat_kw] = "lat"
    if lon_kw:
        latlon_kwargs[lon_kw] = "lon"

    orig_cols = list(df.columns)
    orig_index = df.index.name

    all_features = []
    for level in LEVELS:
        feats = features_for_level(df, level, latlon_kwargs,
                                   orig_cols, orig_index, pd, mapping)
        all_features.extend(feats)
        print(f"  level {level}: {len(feats)} cell(s)")

    payload = {
        "type": "FeatureCollection",
        "kind": "s2-telescoping",
        "resolutions": LEVELS,
        "features": all_features,
    }
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"Wrote {len(LEVELS)} level(s), {len(all_features)} feature(s) "
          f"total -> {OUTPUT}")


if __name__ == "__main__":
    main()
