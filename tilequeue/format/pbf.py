from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid
from mapbox_vector_tile import encode as pbf_encode

def encode(out, layers, bounds, extents=4096):
    content = pbf_encode(
        layers,
        quantize_bounds=bounds,
        on_invalid_geometry=on_invalid_geometry_make_valid,
        round_fn=round,
        extents=extents,
    )
    out.write(content)
