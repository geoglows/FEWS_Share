import pandas as pd
from vgridpandas import h3pandas

df = pd.DataFrame({'lat': [10, 11], 'lon': [106, 107]})
resolution = 10

df = df.h3.latlon2h3(resolution, lat_col='lat', lon_col='lon')
df = df.h3.h32geo()

print(df)

df.to_file('output.geojson', driver='GeoJSON')
