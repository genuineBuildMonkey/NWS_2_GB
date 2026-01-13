from app.config import NWS_HEADERS


def fetch_json(session, url, params=None, headers=None, etag=None, last_modified=None, timeout=20):
    h = dict(headers or {})
    if etag:
        h["If-None-Match"] = etag
    if last_modified:
        h["If-Modified-Since"] = last_modified
    resp = session.get(url, params=params, headers=h, timeout=timeout)
    if resp.status_code == 304:
        return None, etag, last_modified
    resp.raise_for_status()
    return resp.json(), resp.headers.get("ETag"), resp.headers.get("Last-Modified")


def collect_alert_geometries(alert_feature):
    geom = alert_feature.get("geometry") or {}
    if geom.get("type") in ("Polygon", "MultiPolygon"):
        return [geom]
    return []


def _collect_from_feature_collection(zdata):
    geoms = []
    for feat in zdata.get("features") or []:
        geom = feat.get("geometry") or {}
        if geom.get("type") in ("Polygon", "MultiPolygon"):
            geoms.append(geom)
    return geoms


def collect_zone_geometries(session, affected_zones):
    geoms = []
    for zurl in affected_zones or []:
        try:
            zdata, _, _ = fetch_json(session, zurl, headers=NWS_HEADERS)
            if not zdata:
                continue

            geom = zdata.get("geometry") or {}
            if geom.get("type") in ("Polygon", "MultiPolygon"):
                geoms.append(geom)
                continue

            geoms.extend(_collect_from_feature_collection(zdata))
        except Exception:
            continue
    return geoms


def choose_geometries_for_alert(session, alert_feature):
    sources = []
    geoms = []

    alert_geoms = collect_alert_geometries(alert_feature)
    if alert_geoms:
        sources.extend(["alert.geometry"] * len(alert_geoms))
        geoms.extend(alert_geoms)

    props = alert_feature.get("properties") or {}
    affected = props.get("affectedZones") or []
    zone_geoms = collect_zone_geometries(session, affected)
    if zone_geoms:
        sources.extend(["affectedZones"] * len(zone_geoms))
        geoms.extend(zone_geoms)

    return sources, geoms
