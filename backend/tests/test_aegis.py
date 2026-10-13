"""
AEGIS Backend Tests
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Run: python -m pytest tests/ -v

Tests cover:
  - Mock sensor output schema
  - GNN model graph construction
  - Data pipeline (altitude binning, proximity)
  - API routes (with Flask test client)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import sys
import os
import math
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ─── Mock Sensor Tests ────────────────────────────────────────────────────────

class TestMockSensor:
    def _make_sensor(self):
        from backend.mock_sensor import MockSensor
        readings = []
        sensor = MockSensor(emit_callback=lambda d: readings.append(d))
        return sensor, readings

    def test_reading_has_required_keys(self):
        sensor, _ = self._make_sensor()
        sensor._tick = 0
        sensor._storm_active = False

        reading = sensor._generate_reading()

        assert "timestamp" in reading
        assert "source" in reading
        assert "rf" in reading
        assert "tick" in reading

    def test_rf_noise_is_float(self):
        sensor, _ = self._make_sensor()
        reading = sensor._generate_reading()
        assert isinstance(reading["rf"]["noise_dbm"], float)

    def test_solar_alert_levels(self):
        sensor, _ = self._make_sensor()
        valid_levels = {"NONE", "MINOR", "MODERATE", "STRONG", "SEVERE"}
        reading = sensor._generate_reading()
        assert reading["solar"]["alert_level"] in valid_levels

    def test_storm_inject_sets_active(self):
        sensor, _ = self._make_sensor()
        sensor._storm_active = False
        # Force storm inject by setting high probability
        import random
        original = random.random
        random.random = lambda: 0.0  # always triggers (< 0.03)
        sensor._inject_storm()
        random.random = original
        assert sensor._storm_active is True
        assert sensor._storm_intensity > 0

    def test_storm_detection_in_rf(self):
        sensor, _ = self._make_sensor()
        sensor._storm_active = True
        sensor._storm_intensity = 1.0
        sensor._storm_remaining_ticks = 100
        sensor._tick = 10

        reading = sensor._generate_reading()

        assert "rf" in reading
        assert reading["rf"]["storm_detected"] is True

    def test_history_stores_readings(self):
        sensor, _ = self._make_sensor()
        for _ in range(5):
            r = sensor._generate_reading()
            sensor.history.append(r)
        assert len(sensor.history) == 5
        latest = sensor.get_latest()
        assert latest is not None

    def test_source_is_mock(self):
        sensor, _ = self._make_sensor()
        reading = sensor._generate_reading()
        assert reading["source"] == "mock"


# ─── GNN Model Tests ──────────────────────────────────────────────────────────

class TestGNNModel:
    SAMPLE_DEBRIS = [
        {
            "NORAD_CAT_ID": "12345",
            "OBJECT_NAME": "TEST DEB",
            "APOAPSIS": "500",
            "INCLINATION": "51.6",
            "ECCENTRICITY": "0.001",
            "BSTAR": "1e-4",
            "MEAN_MOTION": "15.5",
            "RA_OF_ASC_NODE": "180",
            "ARG_OF_PERICENTER": "90",
            "LAUNCH_YEAR": "2010",
        }
        for _ in range(20)
    ]

    def test_graph_builds_from_tle_data(self):
        try:
            from backend.gnn.model import build_graph_from_tle_data, TORCH_GEOMETRIC_AVAILABLE
            if not TORCH_GEOMETRIC_AVAILABLE:
                pytest.skip("torch-geometric not installed")
            graph = build_graph_from_tle_data(self.SAMPLE_DEBRIS, kp_index=2.0)
            assert graph is not None
            assert graph.x.shape[0] == len(self.SAMPLE_DEBRIS)
            from backend.gnn.model import NODE_FEATURE_DIM
            assert graph.x.shape[1] == NODE_FEATURE_DIM  # 12
        except ImportError:
            pytest.skip("torch not available")

    def test_node_features_normalized(self):
        try:
            from backend.gnn.model import build_graph_from_tle_data, TORCH_GEOMETRIC_AVAILABLE
            if not TORCH_GEOMETRIC_AVAILABLE:
                pytest.skip("torch-geometric not installed")
            graph = build_graph_from_tle_data(self.SAMPLE_DEBRIS, kp_index=0.0)
            # All features should be in [0, 1] range
            assert (graph.x >= 0).all()
            assert (graph.x <= 1).all()
        except ImportError:
            pytest.skip("torch not available")

    def test_kp_index_affects_features(self):
        try:
            from backend.gnn.model import build_graph_from_tle_data, TORCH_GEOMETRIC_AVAILABLE
            if not TORCH_GEOMETRIC_AVAILABLE:
                pytest.skip("torch-geometric not installed")
            g0 = build_graph_from_tle_data(self.SAMPLE_DEBRIS, kp_index=0.0)
            g9 = build_graph_from_tle_data(self.SAMPLE_DEBRIS, kp_index=9.0)
            # Feature index 7 is kp_proxy — should differ
            assert not (g0.x[:, 7] == g9.x[:, 7]).all()
        except ImportError:
            pytest.skip("torch not available")

    def test_load_model_without_checkpoint_returns_none(self):
        try:
            from backend.gnn.model import load_model, TORCH_GEOMETRIC_AVAILABLE
            if not TORCH_GEOMETRIC_AVAILABLE:
                pytest.skip("torch-geometric not installed")
            assert load_model(checkpoint_path=None) is None
        except ImportError:
            pytest.skip("torch not available")

    def test_predict_risk_passthrough_when_model_none(self):
        try:
            from backend.gnn.model import predict_risk
            debris = [dict(d) for d in self.SAMPLE_DEBRIS]
            result = predict_risk(None, debris)
            assert all("risk_score" not in o for o in result)
        except ImportError:
            pytest.skip("torch not available")

    def test_predict_risk_returns_scores(self):
        try:
            from backend.gnn.model import DebrisGNN, predict_risk, TORCH_GEOMETRIC_AVAILABLE
            if not TORCH_GEOMETRIC_AVAILABLE:
                pytest.skip("torch-geometric not installed")
            # load_model(None) intentionally returns None now (no silent
            # untrained model), so build the network directly for this
            # schema/range test.
            model = DebrisGNN().eval()
            debris = [dict(d) for d in self.SAMPLE_DEBRIS]
            result = predict_risk(model, debris, kp_index=2.0)
            for obj in result:
                assert "risk_score" in obj
                assert "risk_level" in obj
                assert 0 <= obj["risk_score"] <= 100
                assert obj["risk_level"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
        except ImportError:
            pytest.skip("torch not available")


# ─── Data Pipeline Tests ──────────────────────────────────────────────────────

class TestDataPipeline:
    def test_altitude_heatmap_bands(self):
        from backend.data.pipeline import build_altitude_heatmap
        debris = [
            {"APOAPSIS": "400"},   # ISS altitude
            {"APOAPSIS": "550"},   # Starlink altitude
            {"APOAPSIS": "800"},   # Dense zone
            {"APOAPSIS": "1200"},
            {"APOAPSIS": "100"},   # Below range — should be excluded
            {"APOAPSIS": "9999"},  # Above range — should be excluded
        ]
        bands = build_altitude_heatmap(debris)
        assert len(bands) > 0
        # Bands should be sorted by altitude
        alts = [b["alt_lower"] for b in bands]
        assert alts == sorted(alts)
        # Total count should match objects in 200-2000km range (4 objects)
        total = sum(b["count"] for b in bands)
        assert total == 4

    def test_altitude_heatmap_labels(self):
        from backend.data.pipeline import build_altitude_heatmap
        debris = [{"APOAPSIS": "412"}, {"APOAPSIS": "550"}, {"APOAPSIS": "800"}]
        bands = build_altitude_heatmap(debris)
        labels_text = " ".join(b["label"] for b in bands)
        assert "ISS" in labels_text
        assert "Starlink" in labels_text
        assert "DENSE" in labels_text

    def test_iss_proximity_returns_dict(self):
        from backend.data.pipeline import compute_iss_proximity
        iss_pos = {"x_km": 0, "y_km": 0, "z_km": 6800}
        debris_positions = [
            {"norad_id": "12345", "name": "TEST DEB",
             "position": {"x_km": 0, "y_km": 0, "z_km": 6801}},
        ]
        result = compute_iss_proximity(iss_pos, debris_positions)
        assert "nearest_km" in result
        assert "alert" in result
        assert result["alert"] in {"RED", "AMBER", "GREEN", "UNKNOWN"}

    def test_iss_proximity_distance_correct(self):
        from backend.data.pipeline import compute_iss_proximity
        iss_pos = {"x_km": 0, "y_km": 0, "z_km": 0}
        debris_positions = [
            {"norad_id": "1", "name": "A",
             "position": {"x_km": 3, "y_km": 4, "z_km": 0}},
        ]
        result = compute_iss_proximity(iss_pos, debris_positions)
        # Distance = sqrt(3² + 4²) = 5.0 km
        assert abs(result["nearest_km"] - 5.0) < 0.01

    def test_proximity_alert_red_below_5km(self):
        from backend.data.pipeline import compute_iss_proximity
        iss_pos = {"x_km": 0, "y_km": 0, "z_km": 0}
        debris_positions = [
            {"norad_id": "1", "name": "A",
             "position": {"x_km": 0, "y_km": 0, "z_km": 3}},  # 3km
        ]
        result = compute_iss_proximity(iss_pos, debris_positions)
        assert result["alert"] == "RED"

    def test_proximity_alert_amber_5_to_10km(self):
        from backend.data.pipeline import compute_iss_proximity
        iss_pos = {"x_km": 0, "y_km": 0, "z_km": 0}
        debris_positions = [
            {"norad_id": "1", "name": "A",
             "position": {"x_km": 0, "y_km": 0, "z_km": 7}},  # 7km
        ]
        result = compute_iss_proximity(iss_pos, debris_positions)
        assert result["alert"] == "AMBER"

    def test_tle_to_position_requires_sgp4(self):
        from backend.data.pipeline import tle_to_position, SGP4_AVAILABLE
        if not SGP4_AVAILABLE:
            pytest.skip("sgp4 not installed")
        # ISS TLE (slightly outdated, but valid for testing)
        tle1 = "1 25544U 98067A   24001.50000000  .00001234  00000-0  12345-4 0  9990"
        tle2 = "2 25544  51.6400 338.0000 0004321 125.0000 275.0000 15.50000000 12345"
        pos = tle_to_position(tle1, tle2)
        # If SGP4 fails on old TLE that's fine; just check return type
        assert pos is None or isinstance(pos, dict)


# ─── API Route Tests ──────────────────────────────────────────────────────────

class TestAPIRoutes:
    @pytest.fixture
    def client(self):
        from backend.app import app
        # Minimal app state
        app.config["APP_STATE"] = {
            "debris_objects": [
                {
                    "NORAD_CAT_ID": "12345",
                    "OBJECT_NAME": "TEST DEBRIS",
                    "APOAPSIS": "500",
                    "INCLINATION": "51.6",
                    "risk_score": 75.0,
                    "risk_level": "HIGH",
                },
                {
                    "NORAD_CAT_ID": "67890",
                    "OBJECT_NAME": "TEST DEBRIS 2",
                    "APOAPSIS": "800",
                    "INCLINATION": "45.0",
                    "risk_score": 30.0,
                    "risk_level": "LOW",
                },
            ],
            "iss_position": {"lat": 10.0, "lon": 70.0, "alt_km": 408},
            "iss_proximity": {"nearest_km": 12.5, "alert": "GREEN"},
            "current_risk_score": 60.0,
            "mock_sensor": None,
        }
        app.config["TESTING"] = True
        return app.test_client()

    def test_health_check(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert "debris_count" in data

    def test_debris_list(self, client):
        resp = client.get("/api/debris/list")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "objects" in data
        assert "count" in data

    def test_debris_list_limit(self, client):
        resp = client.get("/api/debris/list?limit=1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["objects"]) <= 1

    def test_debris_list_min_risk_filter(self, client):
        resp = client.get("/api/debris/list?min_risk=50")
        data = resp.get_json()
        for obj in data["objects"]:
            assert obj["risk_score"] >= 50

    def test_top_threats(self, client):
        resp = client.get("/api/debris/top-threats?n=1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "threats" in data
        assert len(data["threats"]) <= 1
        # Should be sorted highest risk first
        if len(data["threats"]) > 1:
            scores = [t["risk_score"] for t in data["threats"]]
            assert scores == sorted(scores, reverse=True)

    def test_debris_heatmap(self, client):
        resp = client.get("/api/debris/heatmap")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "bands" in data

    def test_debris_object_by_id(self, client):
        resp = client.get("/api/debris/object/12345")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["NORAD_CAT_ID"] == "12345"

    def test_debris_object_not_found(self, client):
        resp = client.get("/api/debris/object/99999")
        assert resp.status_code == 404

    def test_iss_position(self, client):
        resp = client.get("/api/iss/position")
        assert resp.status_code == 200

    def test_iss_proximity(self, client):
        resp = client.get("/api/iss/proximity")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "nearest_km" in data
        assert "alert" in data


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])