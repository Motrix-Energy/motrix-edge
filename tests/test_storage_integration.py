import csv
import os
from datetime import datetime
from json import loads

from api.capabilities import Switch
from devices_manager.devices_manager import DevicesManager
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


class ReadOnlySwitch(StubDevice, Switch):
    """A read-only device that claims Switch anyway — the shape this gate exists for.

    `Switch` is a class-level type claim and algorithms act on it directly:
    `algorithms/auto_toggle.py` selects actuators with `isinstance(device, Switch)` and no
    `is_writable` check. Deriving `is_writable` per instance cannot help, because
    `isinstance` is class-level. That is why the shipped tree splits every writable device
    into a `*_switch` subclass — and why a decision must be recorded only once the command
    has actually reached a transport.
    """

    def __init__(self, name: str = "fake_relay"):
        super().__init__(name=name, is_writable=False)


class TestADecisionIsRecordedOnlyWhenItLands:
    """`Algorithm.control_device` used to write the decision unconditionally.

    It called `devices_manager.control(...)` and then wrote to storage regardless of what
    came back, because `Device.control` logs and returns rather than raising when the
    device is not writable. A read-only device claiming `Switch` therefore put one false
    row per tick into `algorithm_decisions.csv` — the versioned contract Motrix Edge View
    reads — describing a command no hardware ever saw.
    """

    def _algorithm_over(self, device, storage_manager):
        backend = StubStorageBackend("test")
        storage_manager.register(backend)
        devices_manager = DevicesManager()
        devices_manager.update_device(device)
        return StubAlgorithm("test_algo", devices_manager=devices_manager, delay_seconds=1), backend

    def test_a_read_only_device_claiming_switch_writes_no_row(self, storage_manager):
        device = ReadOnlySwitch()
        connector = StubConnector("conn1")
        connector.inject_devices({"fake_relay": device})
        algo, backend = self._algorithm_over(device, storage_manager)

        assert algo.control_device(device, "on") is False
        assert backend.algorithm_decision_calls == []

    def test_a_device_with_no_connector_writes_no_row(self, storage_manager):
        # Reachable without any third party: a device whose `connector_options` name a
        # connector that failed to load is never injected, so it has no `connector`.
        device = StubDevice(name="orphan", is_writable=True)
        algo, backend = self._algorithm_over(device, storage_manager)

        assert algo.control_device(device, "on") is False
        assert backend.algorithm_decision_calls == []

    def test_an_unknown_device_name_writes_no_row(self, storage_manager):
        device = StubDevice(name="relay1", is_writable=True)
        connector = StubConnector("conn1")
        connector.inject_devices({"relay1": device})
        algo, backend = self._algorithm_over(device, storage_manager)

        stale = StubDevice(name="removed_since", is_writable=True)
        assert algo.control_device(stale, "on") is False
        assert backend.algorithm_decision_calls == []

    def test_a_writable_device_still_writes_its_row(self, storage_manager):
        """The gate must not cost the happy path its row."""
        device = StubDevice(name="relay1", is_writable=True)
        connector = StubConnector("conn1")
        connector.inject_devices({"relay1": device})
        algo, backend = self._algorithm_over(device, storage_manager)

        assert algo.control_device(device, "on") is True
        assert backend.algorithm_decision_calls == [("test_algo", "relay1", "on")]
