import pytest

from algorithms.auto_toggle import AutoToggle
from api.capabilities import EnergyMeter, Switch
from tests.conftest import StubDevice, make_devices_access


class StubEnergyMeter(StubDevice, EnergyMeter):
    """Capability stub: reports a preset total (or None for unusable data)."""

    def __init__(self, name: str, energy_kwh: float | None):
        super().__init__(name=name)
        self._energy_kwh = energy_kwh
        self.data = {"some": "data"}

    def get_total_energy_kwh(self) -> float | None:
        return self._energy_kwh


class StubSwitch(StubDevice, Switch):
    """Capability stub: an on/off-controllable device."""

    def __init__(self, name: str, with_data: bool = True):
        super().__init__(name=name, is_writable=True)
        if with_data:
            self.data = {"status": False}


class LoudSwitch(StubSwitch):
    """Switch whose hardware speaks different command tokens."""

    COMMAND_ON = "ON"
    COMMAND_OFF = "OFF"


@pytest.fixture
def mock_dm():
    """A DevicesAccess double that returns devices directly (no deepcopy)."""
    return make_devices_access()


@pytest.fixture
def algo(mock_dm):
    return AutoToggle(name="auto_toggle_test", devices_manager=mock_dm, delay_seconds=0)


def _set_devices(mock_dm, *devices):
    """Configure mock_dm.get_devices to return the given devices."""
    mock_dm.get_devices.return_value = {d.name: d for d in devices}


class TestAutoToggleLogic:
    def test_no_devices_no_crash(self, algo):
        algo.main()  # no devices, should not crash

    @pytest.mark.parametrize("energies,expected", [
        pytest.param([600.0], "on", id="above-threshold"),
        pytest.param([100.0], "off", id="below-threshold"),
        pytest.param([500.0], "off", id="exactly-at-threshold"),
        pytest.param([300.0, 250.0], "on", id="summed-across-meters"),  # 550 > 500
        pytest.param([], "off", id="no-meters"),
    ])
    def test_threshold_decides_the_command(self, algo, mock_dm, energies, expected):
        meters = [StubEnergyMeter(f"meter_{i}", energy) for i, energy in enumerate(energies)]
        switch = StubSwitch("switch_1")
        _set_devices(mock_dm, *meters, switch)

        algo.main()

        mock_dm.control.assert_called_once_with("switch_1", expected)

    def test_switch_without_data_skipped(self, algo, mock_dm):
        meter = StubEnergyMeter("meter_1", 600.0)
        switch = StubSwitch("switch_empty", with_data=False)
        _set_devices(mock_dm, meter, switch)

        algo.main()

        mock_dm.control.assert_not_called()

    def test_meter_without_usable_data_skipped(self, algo, mock_dm, caplog):
        meter = StubEnergyMeter("meter_bad", None)
        switch = StubSwitch("switch_1")
        _set_devices(mock_dm, meter, switch)

        algo.main()  # should not crash

        assert "Skipping meter_bad" in caplog.text
        assert mock_dm.control.call_args[0] == ("switch_1", "off")  # total remains 0

    def test_device_without_capability_ignored(self, algo, mock_dm):
        plain = StubDevice(name="plain_1")
        plain.data = {"some": "data"}
        switch = StubSwitch("switch_1")
        _set_devices(mock_dm, plain, switch)

        algo.main()

        # plain device is neither meter nor switch: contributes nothing, isn't controlled
        assert mock_dm.control.call_args[0] == ("switch_1", "off")

    def test_switch_command_tokens_overridable(self, algo, mock_dm):
        meter = StubEnergyMeter("meter_1", 600.0)
        switch = LoudSwitch("switch_loud")
        _set_devices(mock_dm, meter, switch)

        algo.main()

        mock_dm.control.assert_called_once_with("switch_loud", "ON")
