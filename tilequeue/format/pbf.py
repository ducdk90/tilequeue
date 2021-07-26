from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid
from mapbox_vector_tile import encode as pbf_encode


def encode(fp, feature_layers, bounds_merc, extents=4096):
    tile = pbf_encode(
        feature_layers,
        quantize_bounds=bounds_merc,
        on_invalid_geometry=on_invalid_geometry_make_valid,
        round_fn=round,
        extents=extents,
    )
    fp.write(tile)
