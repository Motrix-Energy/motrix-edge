"""DeviceChecker reports which devices have produced data. Its output *is* the log line,
so that is what these tests assert on — "did not crash" says nothing about a reporter."""
import logging

import pytest

from algorithms.device_checker import DeviceChecker
from tests.conftest import StubDevice, make_devices_access


@pytest.fixture
def mock_dm():
    return make_devices_access()


@pytest.fixture
def checker(mock_dm):
    return DeviceChecker(name="checker_test", devices_manager=mock_dm, delay_seconds=0)


def device(name: str, data: dict) -> StubDevice:
    stub = StubDevice(name=name)
    stub.data = data
    return stub


class TestDeviceCheckerLogic:
    def test_no_devices_says_so(self, checker, caplog):
        with caplog.at_level(logging.INFO):
            checker.main()
        assert any(r.message == "No devices" for r in caplog.records)

    @pytest.mark.parametrize("devices,with_data,without_data", [
        pytest.param({"has_data": {"power": 42}, "no_data": {}}, {"has_data"}, {"no_data"}, id="split"),
        pytest.param({"d1": {"value": 1}}, {"d1"}, set(), id="all-with-data"),
        pytest.param({"d1": {}}, set(), {"d1"}, id="all-without-data"),
    ])
    def test_splits_by_data_presence(self, checker, mock_dm, caplog, devices, with_data, without_data):
        mock_dm.get_devices.return_value = {name: device(name, data) for name, data in devices.items()}

        with caplog.at_level(logging.INFO):
            checker.main()

        report = next(r.message for r in caplog.records if "Devices without data" in r.message)
        missing, present = report.split("\n")
        assert {name for name in devices if name in missing} == without_data
        assert {name for name in devices if name in present} == with_data
