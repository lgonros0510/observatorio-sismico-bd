import json
import os
from pathlib import Path
from typing import Optional

from shapely.geometry import Point, shape
from shapely.strtree import STRtree


class CountryCatalog:
    def __init__(self, path: Optional[str] = None) -> None:
        catalog_path = Path(path or os.getenv("COUNTRY_GEOJSON", "frontend/public/datos/paises.geojson"))
        with catalog_path.open(encoding="utf-8") as file:
            collection = json.load(file)

        self.features = []
        self.geometries = []
        codes: set[str] = set()
        for feature in collection["features"]:
            properties = feature.get("properties") or {}
            code = properties.get("ADM0_A3")
            if not isinstance(code, str) or not code:
                raise ValueError("Cada país debe tener un ADM0_A3 no vacío")
            if code in codes:
                raise ValueError(f"ADM0_A3 duplicado en el catálogo: {code}")
            codes.add(code)
            self.features.append({
                "country_id": code,
                "name_es": properties.get("NAME_ES"),
                "admin_name": properties.get("ADMIN"),
                "continent": properties.get("CONTINENT"),
                "subregion": properties.get("SUBREGION"),
            })
            self.geometries.append(shape(feature["geometry"]))
        self.tree = STRtree(self.geometries)

    def country_for(self, longitude: float, latitude: float) -> Optional[str]:
        point = Point(longitude, latitude)
        candidates = self.tree.query(point)
        matches = [
            self.features[int(index)]["country_id"]
            for index in candidates
            if self.geometries[int(index)].covers(point)
        ]
        return min(matches) if matches else None
