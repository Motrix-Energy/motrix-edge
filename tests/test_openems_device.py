"""OpenEMS channel device: both response shapes, the gap contract, metrics, capabilities."""
import json
import logging

import pytest

from api.capabilities import EnergyMeter, MetricSource, Switch
from devices.openems import Openems
from devices.openems_switch import OpenemsSwitch


def channel(address: str, value, unit: str = "", access: str = "RO") -> dict:
    return {"address": address, "type": "INTEGER", "accessMode": access, "text": "", "unit": unit, "value": value}


def make_device(name: str = "sum", protocol: str = "openems", **listener) -> Openems:
    return Openems(name, {"name": "edge", "protocol": protocol},
                   {"component": "_sum", **listener}, {})


class TestResponseShapes:
    """The Edge returns a single object when exactly one channel matched the regex and an
    array when several did — the shape depends on the data, not on the request."""

    def test_array_when_several_channels_match(self):
        device = make_device()
        payload = json.dumps([channel("_sum/EssSoc", 63, "%"), channel("_sum/GridActivePower", -1420, "W")])
        assert device.receive(payload) is True
        assert set(device.data["channels"]) == {"_sum/EssSoc", "_sum/GridActivePower"}

    def test_single_object_when_exactly_one_matches(self):
        device = make_device()
        assert device.receive(json.dumps(channel("ess0/Soc", 63, "%"))) is True
        assert device.data["channels"]["ess0/Soc"]["value"] == 63

    def test_metadata_rides_along(self):
        device = make_device()
        device.receive(json.dumps(channel("_sum/EssSoc", 63, "%")))
        entry = device.data["channels"]["_sum/EssSoc"]
        assert entry["unit"] == "%"
        assert entry["accessMode"] == "RO"

    def test_a_write_only_channel_has_no_value_key(self):
        device = make_device()
        payload = json.dumps({"address": "ess0/SetActivePowerEquals", "type": "INTEGER",
                              "accessMode": "WO", "text": "", "unit": "W"})
        assert device.receive(payload) is True
        assert device.data["channels"]["ess0/SetActivePowerEquals"]["value"] is None

    def test_topic_and_payload_arity_is_tolerated(self):
        device = make_device()
        assert device.receive("ignored/topic", json.dumps(channel("ess0/Soc", 63))) is True

    def test_unknown_protocol_raises(self):
        with pytest.raises(NotImplementedError):
            make_device(protocol="carrier_pigeon").receive("{}")


class TestGapContract:
    def test_an_html_error_page_is_not_a_reading(self, caplog):
        """An Edge that 404s, or a proxy returning a login form, must not overwrite data."""
        device = make_device()
        device.receive(json.dumps(channel("_sum/EssSoc", 63)))
        good = dict(device.data)
        with caplog.at_level(logging.WARNING):
            assert device.receive("<html><body>404 Not Found</body></html>") is False
        assert device.data == good

    def test_an_empty_array_returns_false(self):
        assert make_device().receive("[]") is False

    def test_entries_without_an_address_are_unusable(self):
        assert make_device().receive(json.dumps([{"value": 63}])) is False

    def test_a_bare_json_scalar_returns_false(self):
        assert make_device().receive("42") is False

    def test_failure_is_edge_triggered_then_recovers(self, caplog):
        device = make_device()
        with caplog.at_level(logging.DEBUG):
            device.receive("nope")
            device.receive("nope")
            device.receive(json.dumps(channel("_sum/EssSoc", 63)))
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, "a broken Edge must warn once, not once per poll"
        assert any("recovered" in r.message for r in caplog.records)

    def test_a_reading_replaces_the_previous_one_wholesale(self):
        device = make_device()
        device.receive(json.dumps([channel("_sum/A", 1), channel("_sum/B", 2)]))
        device.receive(json.dumps(channel("_sum/A", 3)))
        assert set(device.data["channels"]) == {"_sum/A"}


class TestMetrics:
    def test_the_path_separator_is_not_carried_into_metric_names(self):
        """A storage field separator is conventionally '.', and a name carrying a path
        separator reads badly in a query — devices/p1.py made the same call with brackets."""
        device = make_device()
        device.receive(json.dumps(channel("_sum/EssSoc", 63)))
        assert device.get_metrics() == {"_sum_EssSoc": 63}

    def test_only_scalars_are_reported(self):
        device = make_device()
        device.receive(json.dumps([
            channel("_sum/EssSoc", 63),
            channel("_sum/State", "RUN"),
            channel("io0/Relay1", True),
            channel("_sum/List", [1, 2]),
        ]))
        metrics = device.get_metrics()
        assert metrics == {"_sum_EssSoc": 63, "io0_Relay1": True}

    def test_metrics_before_any_reading_are_empty(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert make_device().get_metrics() == {}

    def test_is_a_metric_source(self):
        assert isinstance(make_device(), MetricSource)


class TestEnergyCapability:
    ENERGY = "_sum/GridBuyActiveEnergy"

    def test_wh_is_the_default_unit(self):
        """OpenEMS cumulative-energy channels are Watt-hours; EnergyMeter promises kWh."""
        device = make_device(energy_channel=self.ENERGY, channels="GridBuy.*")
        device.receive(json.dumps(channel(self.ENERGY, 4_523_000, "Wh")))
        assert device.get_total_energy_kwh() == pytest.approx(4523.0)

    def test_kwh_is_reported_as_is(self):
        device = make_device(energy_channel=self.ENERGY, energy_unit="kWh")
        device.receive(json.dumps(channel(self.ENERGY, 4523)))
        assert device.get_total_energy_kwh() == pytest.approx(4523.0)

    def test_no_energy_channel_configured_reports_zero(self):
        """A `_sum` view must stay harmless in an algorithm that sums every EnergyMeter."""
        device = make_device()
        device.receive(json.dumps(channel("_sum/EssSoc", 63)))
        assert device.get_total_energy_kwh() == 0.0

    def test_no_reading_yet_is_none(self):
        assert make_device(energy_channel=self.ENERGY).get_total_energy_kwh() is None

    def test_a_channel_the_filter_did_not_fetch_is_none_and_says_so(self, caplog):
        device = make_device(energy_channel=self.ENERGY, channels="EssSoc")
        device.receive(json.dumps(channel("_sum/EssSoc", 63)))
        with caplog.at_level(logging.WARNING):
            assert device.get_total_energy_kwh() is None
        assert any("is it matched by listener_options.channels" in r.message for r in caplog.records)

    def test_non_numeric_energy_channel_is_none(self, caplog):
        device = make_device(energy_channel=self.ENERGY)
        device.receive(json.dumps(channel(self.ENERGY, "lots")))
        with caplog.at_level(logging.WARNING):
            assert device.get_total_energy_kwh() is None

    def test_unknown_unit_is_none(self, caplog):
        device = make_device(energy_channel=self.ENERGY, energy_unit="joules")
        device.receive(json.dumps(channel(self.ENERGY, 1)))
        with caplog.at_level(logging.WARNING):
            assert device.get_total_energy_kwh() is None

    def test_the_energy_warning_is_edge_triggered(self, caplog):
        device = make_device(energy_channel=self.ENERGY)
        device.receive(json.dumps(channel("_sum/EssSoc", 63)))
        with caplog.at_level(logging.DEBUG):
            device.get_total_energy_kwh()
            device.get_total_energy_kwh()
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    def test_is_an_energy_meter(self):
        assert isinstance(make_device(), EnergyMeter)


class TestOpenemsSwitch:
    def _switch(self, **controller) -> OpenemsSwitch:
        return OpenemsSwitch("ess", {"name": "edge", "protocol": "openems"},
                             {"component": "ess0", "channels": "Soc"}, controller)

    def test_is_a_switch_and_writable(self):
        switch = self._switch(component="ess0", channel="SetActivePowerEquals")
        assert isinstance(switch, Switch)
        assert switch.is_writable is True

    def test_the_read_only_view_is_not_a_switch(self):
        """Load-bearing: Algorithm.control_device writes the decision to storage BEFORE
        Device.control refuses a non-writable device, so a read-only `_sum` view
        subclassing Switch would put a false row in algorithm_decisions.csv on every tick."""
        assert not isinstance(make_device(), Switch)
        assert make_device().is_writable is False

    def test_it_inherits_the_parsing(self):
        switch = self._switch(component="ess0", channel="SetActivePowerEquals")
        assert switch.receive(json.dumps(channel("ess0/Soc", 63))) is True
        assert switch.get_metrics() == {"ess0_Soc": 63}

    def test_unroutable_switch_warns_at_startup(self, caplog):
        with caplog.at_level(logging.WARNING):
            self._switch()
        assert any("declares no controller_options.component/channel" in r.message for r in caplog.records)
