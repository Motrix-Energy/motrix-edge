import logging
import os
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from api.decisions import DecisionLog
from storage.csv_file import CsvFileBackend
from storage.null import NullBackend
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


class TestTwoBackendsOneDirectory:
    """Two backends on one `output_dir` interleave their rows into one file.

    `CsvFileBackend._writer` decides the header from the size on disk at open time, so the
    second backend appends to a file the first already started with no second header to
    mark the seam. What comes out still parses as `device_data.csv` format 1.0 and the
    viewer reads it happily — which is exactly why it is worth a line at startup. No
    third party is needed to reach this; two `csv_file` entries in one config do it.
    """

    def test_a_shared_output_dir_warns_once_and_both_still_register(self, tmp_path, storage_manager, caplog):
        with caplog.at_level(logging.WARNING):
            storage_manager.register(CsvFileBackend("csv1", output_dir=str(tmp_path)))
            storage_manager.register(CsvFileBackend("csv2", output_dir=str(tmp_path)))

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "csv1" in warnings[0].message and "csv2" in warnings[0].message
        # A warning, never a refusal: the operator may have meant it, and storage is a
        # side channel that must not decide whether the run happens.
        storage_manager.write_algorithm_decision("algo", "dev", "on")

    def test_the_same_directory_written_two_ways_is_still_the_same_directory(self, tmp_path, storage_manager, caplog):
        """Normalised through abspath+normcase, so `data/storage` and `./data/storage/`
        are not two directories, and neither are `Data` and `data` on Windows."""
        with caplog.at_level(logging.WARNING):
            storage_manager.register(CsvFileBackend("csv1", output_dir=str(tmp_path)))
            storage_manager.register(CsvFileBackend("csv2", output_dir=str(tmp_path) + os.sep + "." + os.sep))

        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    def test_distinct_directories_are_silent(self, tmp_path, storage_manager, caplog):
        with caplog.at_level(logging.WARNING):
            storage_manager.register(CsvFileBackend("csv1", output_dir=str(tmp_path / "a")))
            storage_manager.register(CsvFileBackend("csv2", output_dir=str(tmp_path / "b")))

        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_a_backend_that_writes_no_files_is_not_compared(self, storage_manager, caplog):
        """Duck-typed: `null` and `influxdb` have no `output_dir` and must not be made to
        collide with each other just for lacking the attribute."""
        with caplog.at_level(logging.WARNING):
            storage_manager.register(NullBackend("null1"))
            storage_manager.register(NullBackend("null2"))

        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
