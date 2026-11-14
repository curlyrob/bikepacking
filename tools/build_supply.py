#!/usr/bin/env python3
"""Zerlegt Versorgungspunkte aus OpenStreetMap-Exporten in kleine Kacheln für Bikepacking.

Aufruf: build_supply.py <ausgabeordner> <supply.geojsonseq> <gebiet1.poly> [<gebiet2.poly> …]

- Eingabe: `osmium export … -f geojsonseq -a type,id` (Punkte, Linien, Flächen), alle Länder in einer Datei
- Kacheln: 0,25° × 0,25° (ca. 28 × 17 km), Datei <ausgabe>/<floor(lat*4)>/<floor(lon*4)>.json
- Jede Kachel im abgedeckten Gebiet bekommt eine Datei, auch wenn sie leer ist ("[]").
  Fehlt eine Kachel (HTTP 404), liegt sie außerhalb – die App fragt dann Overpass.
- Inhalt je Kachel: [[id, lat, lon, code, name, öffnungszeiten], …] (name/öffnungszeiten entfallen, wenn leer)
    code: "<präfix>=<wert>" mit a = amenity, s = shop, t = tourism, m = man_made, n = natural,
          bei Schutzhütten zusätzlich ";<shelter_type>". Brunnen und Quellen nur mit Trinkwasser.
  Die App ordnet den Code mit derselben Logik wie bei Overpass-Ergebnissen einer Kategorie zu.

Daten © OpenStreetMap-Mitwirkende, ODbL 1.0.
"""
import json
import math
import os
import sys
from datetime import datetime, timezone

TILE = 0.25
CLOSED_ACCESS = {"private", "no"}
AMENITY = {"drinking_water", "water_point", "restaurant", "cafe", "fast_food", "fuel", "biergarten",
           "bicycle_repair_station", "toilets"}
SHOP = {"supermarket", "convenience", "bakery", "greengrocer", "butcher", "farm", "kiosk", "bicycle"}
TOURISM = {"camp_site", "alpine_hut", "wilderness_hut", "hostel", "guest_house", "hotel", "motel"}
SKIP_SHELTER = {"public_transport", "field_shelter", "changing_rooms"}


def tile_key(lat, lon):
    return math.floor(lat / TILE + 1e-9), math.floor(lon / TILE + 1e-9)


def classify(p):
    """Code der Art oder None, wenn das Objekt nicht gebraucht wird (gleiche Reihenfolge wie in der App)."""
    if p.get("access") in CLOSED_ACCESS:
        return None
    amenity, shop, tourism = p.get("amenity"), p.get("shop"), p.get("tourism")
    drinkable = p.get("drinking_water") in ("yes", "treated")
    if amenity in AMENITY:
        return "a=" + amenity
    if amenity == "charging_station" and p.get("bicycle") in ("yes", "designated"):
        return "a=charging_station"  # nur Ladestationen für (E-)Fahrräder
    if amenity == "fountain" and drinkable:
        return "a=fountain"
    if amenity == "shelter":
        kind = p.get("shelter_type") or ""
        if kind in SKIP_SHELTER:
            return None
        return "a=shelter" + (";" + kind if kind else "")
    if shop in SHOP:
        return "s=" + shop
    if tourism in TOURISM:
        return "t=" + tourism
    if p.get("man_made") == "water_tap":
        return "m=water_tap"
    if p.get("natural") == "spring" and drinkable:
        return "n=spring"
    return None


def representative_point(geometry):
    kind, coords = geometry.get("type"), geometry.get("coordinates")
    if kind == "Point":
        return coords[1], coords[0]
    ring = {"LineString": lambda c: c, "Polygon": lambda c: c[0], "MultiPolygon": lambda c: c[0][0]}.get(kind)
    if not ring:
        return None
    ring = ring(coords)
    if not ring:
        return None
    return sum(c[1] for c in ring) / len(ring), sum(c[0] for c in ring) / len(ring)


def read_poly(path):
    """Liest eine .poly-Datei als shapely-(Multi)Polygon."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    outer, holes, current, hole = [], [], None, False
    with open(path, encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]
    for line in lines[1:]:
        if current is None:
            if line == "END":
                break
            hole = line.startswith("!")
            current = []
        elif line == "END":
            if len(current) >= 3:
                (holes if hole else outer).append(Polygon(current))
            current = None
        else:
            lon, lat = map(float, line.split()[:2])
            current.append((lon, lat))
    shape = unary_union([p.buffer(0) for p in outer])
    if holes:
        shape = shape.difference(unary_union([p.buffer(0) for p in holes]))
    return shape


def covered_tiles(shapes):
    from shapely.geometry import box
    from shapely.prepared import prep
    tiles = set()
    for shape in shapes:
        prepared = prep(shape)
        lon0, lat0, lon1, lat1 = shape.bounds
        la0, lo0 = tile_key(lat0, lon0)
        la1, lo1 = tile_key(lat1, lon1)
        for la in range(la0, la1 + 1):
            for lo in range(lo0, lo1 + 1):
                if prepared.intersects(box(lo * TILE, la * TILE, (lo + 1) * TILE, (la + 1) * TILE)):
                    tiles.add((la, lo))
    return tiles


def main():
    out, src, *polys = sys.argv[1:]
    tiles, seen, counts = {}, set(), {}
    with open(src, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            feature = json.loads(line)
            props = feature.get("properties") or {}
            code = classify(props)
            if code is None:
                continue
            osm_id = f'{props.get("@type", "node")[0]}{props.get("@id")}'
            if osm_id in seen:
                continue  # Grenzgebiete stecken in zwei Länderauszügen
            point = representative_point(feature.get("geometry") or {})
            if point is None:
                continue
            seen.add(osm_id)
            lat, lon = point
            entry = [osm_id, round(lat, 5), round(lon, 5), code]
            name, hours = (props.get("name") or "").strip(), (props.get("opening_hours") or "").strip()
            if name or hours:
                entry.append(name)
            if hours:
                entry.append(hours)
            tiles.setdefault(tile_key(lat, lon), []).append(entry)
            counts[code.split(";")[0]] = counts.get(code.split(";")[0], 0) + 1

    covered = (covered_tiles([read_poly(p) for p in polys]) if polys else set()) | set(tiles)
    for la, lo in covered:
        folder = os.path.join(out, str(la))
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, f"{lo}.json"), "w", encoding="utf-8") as f:
            json.dump(tiles.get((la, lo), []), f, ensure_ascii=False, separators=(",", ":"))

    meta = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "OpenStreetMap via Geofabrik (Länderauszüge Europa)",
        "license": "ODbL 1.0 – © OpenStreetMap-Mitwirkende, https://www.openstreetmap.org/copyright",
        "tileSizeDegrees": TILE,
        "points": len(seen),
        "tiles": len(covered),
        "tilesWithData": len(tiles),
        "counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
    }
    with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
