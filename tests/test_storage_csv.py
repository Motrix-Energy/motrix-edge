import csv
import os
from datetime import datetime
from json import loads

from tests.conftest import StubDevice
from storage.csv_file import CsvFileBackend


class TestCsvFileBackend:
    def test_instantiation(self, tmp_path):
        backend = CsvFileBackend("test_csv", output_dir=str(tmp_path / "storage"))
        assert backend.name == "test_csv"

    def test_write_device_data_creates_file(self, tmp_path):
        out = str(tmp_path / "storage")
        backend = CsvFileBackend("test_csv", output_dir=out)
        device = StubDevice(name="d1")
        device.data = {"power": 42}
        backend.write_device_data(device, device.data)

        filepath = os.path.join(out, "device_data.csv")
        assert os.path.exists(filepath)

        with open(filepath, newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["device_name"] == "d1"
        assert loads(rows[0]["data_json"]) == {"power": 42}

    def test_write_device_data_appends(self, tmp_path):
        out = str(tmp_path / "storage")
        backend = CsvFileBackend("test_csv", output_dir=out)

        for i in range(3):
            device = StubDevice(name=f"d{i}")
            device.data = {"val": i}
            backend.write_device_data(device, device.data)

        filepath = os.path.join(out, "device_data.csv")
        with open(filepath, newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 3

    def test_write_algorithm_decision(self, tmp_path):
        out = str(tmp_path / "storage")
        backend = CsvFileBackend("test_csv", output_dir=out)
        backend.write_algorithm_decision("auto_toggle", "shelly1", "on")

        filepath = os.path.join(out, "algorithm_decisions.csv")
        with open(filepath, newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["algorithm"] == "auto_toggle"
        assert rows[0]["device"] == "shelly1"
        assert rows[0]["command"] == "on"

    def test_read_filters_by_device_and_time(self, tmp_path):
        out = str(tmp_path / "storage")
        backend = CsvFileBackend("test_csv", output_dir=out)

        # Write data for two devices
        d1 = StubDevice(name="d1")
        d1.data = {"power": 10}
        d2 = StubDevice(name="d2")
        d2.data = {"power": 20}
        backend.write_device_data(d1, d1.data)
        backend.write_device_data(d2, d2.data)

        # Read all for d1
        results = backend.read("d1", datetime(2020, 1, 1), datetime(2030, 1, 1))
        assert len(results) == 1
        assert results[0]["device_name"] == "d1"
        assert results[0]["data"] == {"power": 10}

    def test_read_empty_file(self, tmp_path):
        out = str(tmp_path / "storage")
        backend = CsvFileBackend("test_csv", output_dir=out)
        results = backend.read("d1", datetime(2020, 1, 1), datetime(2030, 1, 1))
        assert results == []

    def test_dir_created_on_first_write(self, tmp_path):
        out = str(tmp_path / "new_dir" / "storage")
        assert not os.path.exists(out)
        backend = CsvFileBackend("test_csv", output_dir=out)
        device = StubDevice(name="d1")
        device.data = {}
        backend.write_device_data(device, device.data)
        assert os.path.isdir(out)
