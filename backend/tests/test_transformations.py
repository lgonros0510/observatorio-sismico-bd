import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from app.catalog import CountryCatalog
from app.load_historical import _fetch_interval
from app.main import _base_filters
from app.storage import InvalidEvent, normalize_event


class TransformationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        project = Path(__file__).resolve().parents[2]
        project_catalog = project / "frontend" / "public" / "datos" / "paises.geojson"
        catalog_path = project_catalog if project_catalog.exists() else Path(os.environ["COUNTRY_GEOJSON"])
        cls.catalog = CountryCatalog(str(catalog_path))

    def envelope(self, event=None):
        return {
            "source": "unit-test",
            "mode": "live",
            "ingested_at": "2025-01-02T03:04:05Z",
            "event": event or {
                "id": "test-event",
                "properties": {
                    "type": "earthquake",
                    "time": 1735787045000,
                    "updated": 1735787050000,
                    "mag": 4.2,
                    "magType": "mb",
                    "place": "Madrid",
                },
                "geometry": {"type": "Point", "coordinates": [-3.7038, 40.4168, 12.5]},
            },
        }

    def test_point_is_enriched_using_catalog_polygon(self):
        row = normalize_event(self.envelope(), self.catalog)
        self.assertEqual(row["country_id"], "ESP")
        self.assertEqual(row["longitude"], -3.7038)
        self.assertEqual(row["depth_km"], 12.5)
        self.assertEqual(row["event_time"].isoformat(), "2025-01-02T03:04:05+00:00")

    def test_null_magnitude_and_depth_are_preserved(self):
        envelope = self.envelope()
        envelope["event"]["properties"]["mag"] = None
        envelope["event"]["geometry"]["coordinates"] = [-3.7038, 40.4168]
        row = normalize_event(envelope, self.catalog)
        self.assertIsNone(row["magnitude"])
        self.assertIsNone(row["depth_km"])

    def test_replay_test_case_flag_is_preserved(self):
        envelope = self.envelope()
        envelope["mode"] = "replay"
        envelope["test_case"] = True
        row = normalize_event(envelope, self.catalog)
        self.assertTrue(row["test_case"])

    def test_test_case_flag_is_rejected_outside_replay(self):
        envelope = self.envelope()
        envelope["test_case"] = True
        with self.assertRaisesRegex(InvalidEvent, "solo se permite en modo replay"):
            normalize_event(envelope, self.catalog)

    def test_replay_statistics_exclude_test_cases_but_event_queries_can_include_them(self):
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        stats_where, _ = _base_filters("replay", start, end, None, None)
        events_where, _ = _base_filters("replay", start, end, None, None, include_test_cases=True)
        self.assertIn("test_case = false", stats_where)
        self.assertNotIn("test_case", events_where)

    def test_invalid_coordinate_is_rejected(self):
        envelope = self.envelope()
        envelope["event"]["geometry"]["coordinates"][0] = 181
        with self.assertRaisesRegex(InvalidEvent, "fuera de rango"):
            normalize_event(envelope, self.catalog)

    def test_ocean_point_has_no_nearest_country_fallback(self):
        row = normalize_event(
            self.envelope({
                "id": "ocean-event",
                "properties": {
                    "type": "earthquake", "time": 1735787045000,
                    "updated": 1735787050000, "mag": 3.1,
                },
                "geometry": {"type": "Point", "coordinates": [-30, 0, 10]},
            }),
            self.catalog,
        )
        self.assertIsNone(row["country_id"])

    def test_historical_loader_reuses_cached_interval(self):
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            prefix = f"{start.strftime('%Y%m%dT%H%M%S')}_{end.strftime('%Y%m%dT%H%M%S')}_"
            (output / f"{prefix}cached.geojson").write_text(
                json.dumps({"type": "FeatureCollection", "metadata": {"count": 0}, "features": []}),
                encoding="utf-8",
            )
            client = Mock()
            features = _fetch_interval(client, start, end, output)
            self.assertEqual(features, [])
            client.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
