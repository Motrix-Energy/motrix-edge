from datetime import datetime
from unittest.mock import MagicMock

import pytest

from api.decisions import DecisionLog
from tests.conftest import StubDevice, StubStorageBackend


class TestStorageManager:
    def test_no_backends_write_device_data_noop(self, storage_manager):
        device = StubDevice(name="d1")
        device.data = {"val": 1}
        storage_manager.write_device_data(device, device.data)  # should not raise

    def test_no_backends_write_algorithm_decision_noop(self, storage_manager):
        storage_manager.write_algorithm_decision("algo", "dev", "on")  # should not raise

    def test_no_backends_read_raises(self, storage_manager):
        with pytest.raises(ValueError):
            storage_manager.read("dev", datetime(2024, 1, 1), datetime(2024, 12, 31))

    def test_fanout_calls_all_backends(self, storage_manager):
        b1 = StubStorageBackend("b1")
        b2 = StubStorageBackend("b2")
        storage_manager.register(b1)
        storage_manager.register(b2)

        device = StubDevice(name="d1")
        device.data = {"val": 1}
        storage_manager.write_device_data(device, device.data)

        assert len(b1.device_data_calls) == 1
        assert len(b2.device_data_calls) == 1

    def test_fanout_algorithm_decision(self, storage_manager):
        b1 = StubStorageBackend("b1")
        storage_manager.register(b1)
        storage_manager.write_algorithm_decision("algo", "dev", "off")
        assert len(b1.algorithm_decision_calls) == 1
        assert b1.algorithm_decision_calls[0] == ("algo", "dev", "off")

    def test_error_isolation(self, storage_manager):
        """Exception in one backend does not prevent others from being called."""
        failing = MagicMock()
        failing.name = "failing"
        failing.write_device_data.side_effect = RuntimeError("boom")

        good = StubStorageBackend("good")
        storage_manager.register(failing)
        storage_manager.register(good)

        device = StubDevice(name="d1")
        device.data = {"val": 1}
        storage_manager.write_device_data(device, device.data)

        assert len(good.device_data_calls) == 1  # good backend was still called


class TestDecisionHistory:
    """The in-memory history hangs off this funnel, not off a backend.

    That is what makes GET /decisions and algorithm_decisions.csv agree by construction —
    one call site, so the two surfaces cannot drift.
    """

    def test_records_with_no_backends_registered(self, storage_manager):
        """The whole reason the history is not a storage backend: a viewer pointed at an EMS
        with "storage": [] must still be able to see decisions, and must not have to
        distinguish "none yet" from "nobody configured a backend"."""
        storage_manager.write_algorithm_decision("AutoToggle", "shelly_plug", "on")

        page = DecisionLog().page()
        assert [(d.algorithm, d.device, d.command) for d in page.decisions] == [
            ("AutoToggle", "shelly_plug", "on")
        ]

    def test_log_and_backends_see_the_same_calls_in_the_same_order(self, storage_manager):
        backend = StubStorageBackend("b1")
        storage_manager.register(backend)

        storage_manager.write_algorithm_decision("AutoToggle", "shelly_plug", "off")
        storage_manager.write_algorithm_decision("AutoToggle", "shelly_plug", "on")

        assert backend.algorithm_decision_calls == [
            ("AutoToggle", "shelly_plug", "off"),
            ("AutoToggle", "shelly_plug", "on"),
        ]
        assert [(d.algorithm, d.device, d.command) for d in DecisionLog().page().decisions] == \
            backend.algorithm_decision_calls

    def test_a_failing_log_is_logged_and_does_not_stop_the_fan_out(self, storage_manager, monkeypatch):
        """The history is a side channel too. A broken one must not take down the algorithm
        thread that was merely reporting a decision."""
        backend = StubStorageBackend("b1")
        storage_manager.register(backend)
        monkeypatch.setattr(
            DecisionLog, "record",
            MagicMock(side_effect=RuntimeError("boom")),
        )

        storage_manager.write_algorithm_decision("AutoToggle", "shelly_plug", "on")

        assert len(backend.algorithm_decision_calls) == 1

    def test_device_data_does_not_touch_the_log(self, storage_manager):
        device = StubDevice(name="d1")
        device.data = {"val": 1}
        storage_manager.write_device_data(device, device.data)
        assert DecisionLog().counts().total == 0
