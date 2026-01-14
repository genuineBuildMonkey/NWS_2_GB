from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.ops import unary_union

from app.config import MAX_POINTS, SIMPLIFY_ENABLED, SIMPLIFY_TOLERANCE


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
    tol = SIMPLIFY_TOLERANCE
    simplified = shp
    for _ in range(10):
        candidate = simplified.simplify(tol, preserve_topology=True)
        if candidate.is_empty:
            return simplified
        simplified = candidate
        if MAX_POINTS is None or _count_points(simplified) <= MAX_POINTS:
            return simplified
        tol *= 2
    return simplified


def _count_points(shp):
    if shp is None or shp.is_empty:
        return 0
    if isinstance(shp, Polygon):
        return len(shp.exterior.coords)
    if isinstance(shp, MultiPolygon):
        return sum(len(poly.exterior.coords) for poly in shp.geoms)
    return 0


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


def _shape_to_zones(shp):
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


def _zones_point_count(zones):
    if not zones:
        return 0
    return sum(len(zone) for zone in zones)


def shapely_to_goodbarber_zones(shp):
    if shp is None or shp.is_empty:
        return None

    if SIMPLIFY_ENABLED and MAX_POINTS is not None:
        zones = _shape_to_zones(shp)
        if zones is not None and _zones_point_count(zones) <= MAX_POINTS:
            return zones

    shp = _simplify_shape(shp)
    return _shape_to_zones(shp)
