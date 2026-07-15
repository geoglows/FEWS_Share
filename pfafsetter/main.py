import pandas as pd
import geopandas as gpd
import networkx as nx

# Define the file path
shapefile_path = "/Users/maugh24/FEWS_Share/hybas_au_lev01-12_v1c/hybas_au_lev04_v1c.shp"

# Read the shapefile into a GeoDataFrame
hydrobasins = gpd.read_file(shapefile_path)
print("have basins")

nexus_points = gpd.read_file("/Users/maugh24/FEWS_Share/pfafsetter/global_nexus.gpkg")
print("have nexus points")

# Define the file path
parquet_file = "/Users/maugh24/FEWS_Share/pfafsetter/v2-model-table.parquet"
print("have parquet file")

# Read the Parquet file into a DataFrame
meta_data = pd.read_parquet(parquet_file, engine="pyarrow")

hydrobasins_sorted = hydrobasins.sort_values(by="SORT", ascending=False)
nexus_points = nexus_points.to_crs(hydrobasins.crs)
# Step 1: Create the directed graph once outside the loop
# Build the graph directly from the edge list (faster than the iterrows + add_edge
# loop). Nodes keep the parquet's native int64 dtype -- no str() conversion -- so
# uslinknos below is now built as a list of ints to match.
# DSLINKNO <= 0 marks "no downstream" (e.g. flows to the ocean), so those rows
# aren't real edges and are excluded.
G = nx.from_pandas_edgelist(
    meta_data[meta_data['DSLINKNO'] > 0],
    source='LINKNO',
    target='DSLINKNO',
    create_using=nx.DiGraph()
)
results = []

# Step 2: Loop through each hydrobasin and process contained points
for index, basin in hydrobasins_sorted.iterrows():
    basin_area = basin["UP_AREA"]
    # Select nexus points contained within the current hydrobasin
    contained_points = nexus_points[nexus_points.within(basin.geometry)]

    # Extract all USLINKNO values from the contained nexus points
    uslinknos = [int(ln) for sublist in contained_points["USLINKNOs"].str.split(',') for ln in sublist]  # Convert to a list of ints (G's nodes are ints now)

    # Match each USLINKNO to its corresponding USContArea (converted to km²)
    linkno_uscontarea_map = meta_data.set_index("LINKNO")["USContArea"].to_dict()

    # Filter USContAreas based on the basin_area and add to the filtered dictionary
    filtered_uscontareas = {
        ln: ratio for ln in uslinknos
        if (uscontarea := linkno_uscontarea_map.get(int(ln))) is not None and  # Convert ln to int before lookup
        (ratio := abs((uscontarea / 1e6) / basin_area - 1)) <= 0.50
    }

    # Get the top matches based on the smallest ratio (ascending order)
    top_matches = dict(sorted(filtered_uscontareas.items(), key=lambda item: item[1])[:30])
    print(top_matches)

    # Step 3: Find the best match based on the highest percentage of contained rivers
    best_match = None
    best_percentage = 0

    for uslinkno, _ in top_matches.items():
        # Find all upstream rivers for the current USLINKNO
        upstream_rivers = nx.ancestors(G, uslinkno)  # Get all upstream rivers

        # Calculate how many of the upstream rivers are in the 'contained_rivers' list
        #contained_rivers = set(filtered_uscontareas.keys())  # These are the rivers that match the basin criteria
        contained_upstream_rivers = upstream_rivers.intersection(uslinknos)

        # Calculate the percentage of upstream rivers that are contained
        percentage = len(contained_upstream_rivers) / len(upstream_rivers) if len(upstream_rivers) > 0 else 0

        # Keep track of the best match with the highest percentage
        if percentage > best_percentage:
            best_percentage = percentage
            best_match = uslinkno

    # Step 4: Save the basin ID and the best match into results (once per basin,
    # after all candidates have been evaluated -- not once per candidate)
    results.append({"Basin_ID": basin["HYBAS_ID"], "Best_Match": best_match, "Match_Percentage": best_percentage})

    # Step 4: Print the best match for the current basin
    print(f"Best match for basin {basin['HYBAS_ID']}: {best_match} with {best_percentage * 100:.2f}% of upstream rivers contained.")

results_df = pd.DataFrame(results)
results_df.to_csv("/Users/maugh24/FEWS_Share/pfafsetter/results.csv")