import argparse
import json
import platform
import random
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

from shapely.geometry import Point

from app.catalog import CountryCatalog


def timed(function):
    start = time.perf_counter()
    value = function()
    return value, time.perf_counter() - start


def benchmark(size: int, catalog: CountryCatalog) -> dict:
    randomizer = random.Random(20251027 + size)
    tracemalloc.start()
    rows = [
        {
            "event_id": f"synthetic-{index}",
            "updated_ms": 1_700_000_000_000 + index,
            "longitude": randomizer.uniform(-180, 180),
            "latitude": randomizer.uniform(-85, 85),
            "hour": index % 168,
            "country_id": f"country-{index % 177}",
        }
        for index in range(size)
    ]
    index, index_seconds = timed(lambda: {(row["event_id"], row["updated_ms"]): row for row in rows})
    _, dataset_and_index_peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    probes = rows[::max(1, size // 100)]
    _, linear_seconds = timed(lambda: [
        next((row for row in rows if row["event_id"] == probe["event_id"]), None)
        for probe in probes
    ])
    _, dictionary_seconds = timed(lambda: [
        index.get((probe["event_id"], probe["updated_ms"])) for probe in probes
    ])

    points = [Point(row["longitude"], row["latitude"]) for row in rows]
    _, spatial_seconds = timed(lambda: [catalog.country_for(point.x, point.y) for point in points])
    _, linear_geo_seconds = timed(lambda: [
        next((
            feature["country_id"]
            for feature, geometry in zip(catalog.features, catalog.geometries)
            if geometry.covers(point)
        ), None)
        for point in points
    ])
    _, aggregation_seconds = timed(lambda: _aggregate(rows))
    return {
        "input_rows": size,
        "synthetic": True,
        "version_lookup_probes": len(probes),
        "version_index_build_seconds": index_seconds,
        "version_linear_lookup_seconds": linear_seconds,
        "version_dictionary_lookup_seconds": dictionary_seconds,
        "dataset_and_version_index_peak_allocated_bytes": dataset_and_index_peak_bytes,
        "spatial_strtree_seconds": spatial_seconds,
        "spatial_linear_scan_seconds": linear_geo_seconds,
        "hour_country_aggregation_seconds": aggregation_seconds,
    }


def _aggregate(rows: list[dict]) -> dict:
    counts: dict[tuple[int, str], int] = {}
    for row in rows:
        key = (row["hour"], row["country_id"])
        counts[key] = counts.get(key, 0) + 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Rendimiento reproducible con datos sintéticos, fuera del análisis real")
    parser.add_argument("--sizes", nargs="+", type=int, default=[1000, 5000, 10000])
    parser.add_argument("--output", type=Path, default=Path("/data/benchmark.json"))
    args = parser.parse_args()
    if any(size < 1 for size in args.sizes):
        parser.error("Todos los tamaños deben ser positivos")
    container_catalog = Path("/app/frontend/datos/paises.geojson")
    project_catalog = Path(__file__).resolve().parents[2] / "frontend" / "public" / "datos" / "paises.geojson"
    project_data = container_catalog if container_catalog.exists() else project_catalog
    catalog = CountryCatalog(str(project_data))
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "machine": {"platform": platform.platform(), "processor": platform.processor()},
        "method": "Mismos tamaños deterministas; los registros sintéticos no se usan en la aplicación.",
        "results": [benchmark(size, catalog) for size in args.sizes],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
