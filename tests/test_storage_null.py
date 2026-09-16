from datetime import datetime

from tests.conftest import StubDevice
from storage.null import NullBackend


class TestNullBackend:
    def test_instantiation(self):
        backend = NullBackend("test_null")
        assert backend.name == "test_null"

    def test_write_device_data_noop(self):
        backend = NullBackend("test_null")
        device = StubDevice(name="d1")
        device.data = {"power": 100}
        backend.write_device_data(device, device.data)  # should not raise

    def test_write_algorithm_decision_noop(self):
        backend = NullBackend("test_null")
        backend.write_algorithm_decision("algo1", "device1", "on")  # should not raise

    def test_read_returns_empty(self):
        backend = NullBackend("test_null")
        result = backend.read("device1", datetime(2024, 1, 1), datetime(2024, 12, 31))
        assert result == []
