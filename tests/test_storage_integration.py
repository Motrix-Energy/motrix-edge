import csv
import os
from datetime import datetime
from json import loads

from tests.conftest import StubAlgorithm, StubConnector, StubDevice, StubStorageBackend
from storage.csv_file import CsvFileBackend


class TestStorageIntegration:
    def test_connector_triggers_storage_on_device_data(self, storage_manager):
        """on_device_data_received() writes to all registered storage backends."""
        backend = StubStorageBackend("test")
        storage_manager.register(backend)

        connector = StubConnector("conn1")
        device = StubDevice(name="d1")
        device.data = {"power": 100}
        connector.inject_devices({"d1": device})

        connector.on_device_data_received(device)

        assert len(backend.device_data_calls) == 1
        assert backend.device_data_calls[0][0].name == "d1"
        assert backend.device_data_calls[0][1] == {"power": 100}

    def test_csv_end_to_end(self, tmp_path, storage_manager):
        """Full flow: connector receives data → CSV backend persists → read back."""
        out = str(tmp_path / "storage")
        csv_backend = CsvFileBackend("csv1", output_dir=out)
        storage_manager.register(csv_backend)

        connector = StubConnector("conn1")
        device = StubDevice(name="sensor1")
        device.data = {"temperature": 22.5}
        connector.inject_devices({"sensor1": device})

        connector.on_device_data_received(device)

        # Verify CSV was written
        filepath = os.path.join(out, "device_data.csv")
        assert os.path.exists(filepath)
        with open(filepath, newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["device_name"] == "sensor1"
        assert loads(rows[0]["data_json"]) == {"temperature": 22.5}

        # Verify read() returns the data
        results = csv_backend.read("sensor1", datetime(2020, 1, 1), datetime(2030, 1, 1))
        assert len(results) == 1
        assert results[0]["data"] == {"temperature": 22.5}

    def test_algorithm_decision_logged(self, tmp_path, storage_manager):
        """control_device() writes algorithm decisions to storage."""
        backend = StubStorageBackend("test")
        storage_manager.register(backend)

        algo = StubAlgorithm("test_algo", delay_seconds=1)
        device = StubDevice(name="relay1")
        connector = StubConnector("conn1")
        connector.inject_devices({"relay1": device})

        algo.control_device(device, "on")

        assert len(backend.algorithm_decision_calls) == 1
        assert backend.algorithm_decision_calls[0] == ("test_algo", "relay1", "on")
