import pandas as pd
import geopandas as gpd
import networkx as nx
from multiprocessing import Pool, cpu_count
from tqdm import tqdm

# Tolerance for catching nexus points that sit just outside a basin's polygon
# boundary due to cross-dataset misalignment -- HydroBASINS (SRTM-derived)
# and this TDX-Hydro/TanDEM-X-derived stream network don't share a coordinate
# source, so a confluence that's conceptually "at" a basin's outlet can land
# a short distance on the wrong side of the boundary line.
# Too small and it won't catch genuine near-misses; too large and basins
# start picking up nexus points that really belong to a neighboring
# sub-catchment. In meters, since basins are reprojected into the nexus
# points' native EPSG:3857 CRS below.
BUFFER_METERS = 1000


def build_graph(meta_data):
    """Directed graph of upstream -> downstream LINKNO connectivity.
    Native int64 node labels (matches uslinknos, which is now a list of ints) --
    no str() conversion, and no per-row iterrows() loop."""
    return nx.from_pandas_edgelist(
        meta_data[meta_data["DSLINKNO"] > 0],
        source="LINKNO",
        target="DSLINKNO",
        create_using=nx.DiGraph(),
    )


def init_worker(graph, nexus, linkno_map):
    """Runs once per worker process. Stashes the big read-only objects as
    globals so they aren't re-pickled for every basin."""
    global G, NEXUS_POINTS, LINKNO_USCONTAREA_MAP
    G = graph
    NEXUS_POINTS = nexus
    LINKNO_USCONTAREA_MAP = linkno_map


def process_basin(basin_data):
    """Find the best-matching nexus link for a single basin."""
    basin_geom, basin_area, basin_hybas_id = basin_data

    # Buffer the basin polygon before the containment check, so a nexus point
    # sitting just outside the nominal boundary is still picked up as a
    # candidate.
    search_geom = basin_geom.buffer(BUFFER_METERS)
    contained_points = NEXUS_POINTS[NEXUS_POINTS.within(search_geom)]
    if contained_points.empty:
        return {"Basin_ID": basin_hybas_id, "Best_Match": None, "Match_Percentage": 0}

    uslinknos = [int(ln) for sublist in contained_points["USLINKNOs"].str.split(",") for ln in sublist]

    filtered_uscontareas = {
        ln: ratio
        for ln in uslinknos
        if (uscontarea := LINKNO_USCONTAREA_MAP.get(ln)) is not None
        and (ratio := abs((uscontarea / 1e6) / basin_area - 1)) <= 0.50
    }

    top_matches = dict(sorted(filtered_uscontareas.items(), key=lambda item: item[1])[:30])
    local_links = set(uslinknos)

    # Pick the candidate that explains the most of the basin's own local
    # evidence (raw overlap count), not the highest percentage of its own
    # global ancestor set -- dividing by the candidate's total upstream size
    # unfairly penalizes large, correct rivers in favor of small ones that
    # happen to have a higher ratio. Ties are broken by preferring the
    # smallest total ancestor set: the most "parsimonious" explanation, since
    # it doesn't drag in a huge unrelated network just to tie on overlap.
    best_match = None
    best_overlap_count = -1
    best_ancestor_count = None
    for uslinkno in top_matches:
        upstream_rivers = nx.ancestors(G, uslinkno)
        overlap_count = len(upstream_rivers.intersection(local_links))
        ancestor_count = len(upstream_rivers)

        is_better = overlap_count > best_overlap_count or (
            overlap_count == best_overlap_count
            and best_ancestor_count is not None
            and ancestor_count < best_ancestor_count
        )
        if is_better:
            best_match = uslinkno
            best_overlap_count = overlap_count
            best_ancestor_count = ancestor_count

    # Reporting-only confidence score: fraction of the basin's own local link
    # pool explained by the winning candidate. Normalized by a fixed,
    # basin-local denominator (not the candidate's global ancestor count), so
    # it's comparable across basins and never changes which candidate won.
    match_percentage = (best_overlap_count / len(local_links)) if best_match is not None and local_links else 0

    return {"Basin_ID": basin_hybas_id, "Best_Match": best_match, "Match_Percentage": match_percentage}


def main():
    shapefile_path = "/Users/maugh24/FEWS_Share/hybas_au_lev01-12_v1c/hybas_au_lev08_v1c.shp"
    hydrobasins = gpd.read_file(shapefile_path)
    print("have basins")

    nexus_points = gpd.read_file("/Users/maugh24/FEWS_Share/pfafsetter/global_nexus.gpkg")
    print("have nexus points")

    meta_data = pd.read_parquet("/Users/maugh24/FEWS_Share/pfafsetter/v2-model-table.parquet", engine="pyarrow")
    print("have parquet file")

    hydrobasins_sorted = hydrobasins.sort_values(by="SORT", ascending=False)
    # Reproject basins into the nexus points' native CRS (EPSG:3857, meters)
    # rather than the other way around -- HydroBASINS shapefiles are
    # typically geographic (degrees), and buffering in degrees distorts by
    # latitude. Buffering in meters keeps BUFFER_METERS consistent everywhere.
    hydrobasins_sorted = hydrobasins_sorted.to_crs(nexus_points.crs)

    G = build_graph(meta_data)
    # Built once (was rebuilt every basin iteration in the original version).
    linkno_uscontarea_map = meta_data.set_index("LINKNO")["USContArea"].to_dict()

    basin_inputs = [
        (row.geometry, row["UP_AREA"], row["HYBAS_ID"])
        for _, row in hydrobasins_sorted.iterrows()
    ]

    n_workers = max(cpu_count() - 1, 1)
    results = []
    with Pool(processes=n_workers, initializer=init_worker,
              initargs=(G, nexus_points, linkno_uscontarea_map)) as pool:
        for result in tqdm(pool.imap(process_basin, basin_inputs),
                            total=len(basin_inputs), desc="Matching basins"):
            results.append(result)

    results_df = pd.DataFrame(results)
    results_df.to_csv("/Users/maugh24/FEWS_Share/pfafsetter/results_au_8_buffer.csv", index=False)
    print(f"Done. Wrote {len(results_df)} rows.")


if __name__ == "__main__":
    main()
