from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.ops import unary_union

from app.config import SIMPLIFY_ENABLED, SIMPLIFY_TOLERANCE


def geojson_to_shapely(geom):
    if not geom:
        return None
    if geom.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    return shape(geom)


def union_geometries(geoms):
    if not geoms:
        return None
    return unary_union(geoms)


def _simplify_shape(shp):
    if not SIMPLIFY_ENABLED:
        return shp
    if SIMPLIFY_TOLERANCE is None:
        return shp
    simplified = shp.simplify(SIMPLIFY_TOLERANCE, preserve_topology=True)
    if simplified.is_empty:
        return shp
    return simplified


def _ring_to_gb_points(coords):
    if not coords:
        return []
    ring = [(float(x), float(y)) for x, y in coords]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return [{"lat": lat, "lng": lon} for lon, lat in ring]


def _polygon_to_zones(poly):
    ring = list(poly.exterior.coords)
    gb_points = _ring_to_gb_points(ring)
    return gb_points


def shapely_to_goodbarber_zones(shp):
    if shp is None or shp.is_empty:
        return None

    shp = _simplify_shape(shp)

    zones = []
    if isinstance(shp, Polygon):
        zones.append(_polygon_to_zones(shp))
    elif isinstance(shp, MultiPolygon):
        for poly in shp.geoms:
            zones.append(_polygon_to_zones(poly))
    else:
        return None

    zones = [zone for zone in zones if zone]
    if not zones:
        return None
    return zones
