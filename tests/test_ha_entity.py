"""Home Assistant entity device: string states, the gap contract, metrics, capabilities.

No importorskip: devices/ha_entity.py parses JSON with the stdlib and imports nothing from
websocket-client, which is what lets an HA device be replayed on a core-only checkout.
"""
import json
import logging

import pytest

from api.capabilities import EnergyMeter, MetricSource, Switch
from devices.ha_entity import HaEntity
from devices.ha_switch import HaSwitch


def state(value: str = "23.4", entity_id: str = "sensor.temperature", **attributes) -> str:
    return json.dumps({
        "entity_id": entity_id,
        "state": value,
        "attributes": attributes,
        "last_changed": "2026-08-14T10:00:00+00:00",
        "last_updated": "2026-08-14T10:00:00+00:00",
        "context": {"id": "abc", "parent_id": None, "user_id": None},
    })


def make_entity(name: str = "temp", protocol: str = "home_assistant", **listener) -> HaEntity:
    return HaEntity(name, {"name": "ha", "protocol": protocol},
                    {"entity_id": "sensor.temperature", **listener}, {})


class TestReceive:
    def test_entity_id_and_payload_arity(self):
        entity = make_entity()
        assert entity.receive("sensor.temperature", state("23.4")) is True
        assert entity.data["entity_id"] == "sensor.temperature"
        assert entity.data["state_value"] == pytest.approx(23.4)

    def test_payload_only_arity_falls_back_to_the_state_objects_own_id(self):
        """A replay CSV row with an empty topic column calls receive() with one argument."""
        entity = make_entity()
        assert entity.receive(state("23.4")) is True
        assert entity.data["entity_id"] == "sensor.temperature"

    def test_an_unserved_protocol_is_refused_rather_than_raised(self):
        device = make_entity(protocol="smoke_signals")
        assert device.receive("x", state()) is False
        assert device.data == {}

    def test_the_refusal_is_reported_once_at_construction(self, caplog):
        with caplog.at_level(logging.ERROR):
            device = make_entity(protocol="smoke_signals")
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1, "an operator hears this once at startup, not once per message"
        assert "smoke_signals" in errors[0].message
        assert "websocket" in errors[0].message

        caplog.clear()
        with caplog.at_level(logging.ERROR):
            for _ in range(5):
                device.receive("x", state())
        assert [r for r in caplog.records if r.levelname == "ERROR"] == []

    def test_mqtt_is_refused_with_the_statestream_reason(self, caplog):
        # The near miss worth its own sentence: HA does speak MQTT, but statestream publishes
        # a bare string where this device parses the state object.
        with caplog.at_level(logging.ERROR):
            make_entity(protocol="mqtt")
        assert any("statestream" in r.message for r in caplog.records)

    def test_attributes_are_kept(self):
        entity = make_entity()
        entity.receive("sensor.temperature", state("23.4", unit_of_measurement="°C", friendly_name="Hall"))
        assert entity.data["attributes"]["friendly_name"] == "Hall"


class TestStateTyping:
    """Home Assistant's `state` is a string by protocol, so the device does the typing."""

    def test_numeric_state_becomes_a_float(self):
        entity = make_entity()
        entity.receive("x", state("1234.5"))
        assert entity.data["state_value"] == pytest.approx(1234.5)

    @pytest.mark.parametrize("text,expected", [
        ("on", True), ("off", False), ("open", True), ("closed", False),
        ("home", True), ("not_home", False),
    ])
    def test_boolean_states_become_bools(self, text, expected):
        entity = make_entity()
        entity.receive("x", state(text))
        assert entity.data["state_value"] is expected

    def test_free_text_state_has_no_numeric_value(self):
        entity = make_entity()
        entity.receive("x", state("heat"))
        assert entity.data["state"] == "heat"
        assert entity.data["state_value"] is None

    @pytest.mark.parametrize("text", ["nan", "inf", "Infinity", "1_0"])
    def test_float_lookalikes_are_not_numbers(self, text):
        """float() accepts all of these. A NaN in self.data makes every threshold
        comparison an algorithm writes silently False."""
        entity = make_entity()
        entity.receive("x", state(text))
        assert entity.data["state_value"] is None


class TestGapContract:
    @pytest.mark.parametrize("text", ["unavailable", "unknown"])
    def test_unavailable_is_not_a_reading(self, text, caplog):
        entity = make_entity()
        entity.receive("x", state("23.4"))
        good = dict(entity.data)
        with caplog.at_level(logging.WARNING):
            assert entity.receive("x", state(text)) is False
        assert entity.data == good, "an entity going offline is a gap, not a flat line"

    def test_unreadable_payload_returns_false_and_keeps_the_reading(self, caplog):
        entity = make_entity()
        entity.receive("x", state("23.4"))
        good = dict(entity.data)
        with caplog.at_level(logging.WARNING):
            assert entity.receive("x", "<html>login</html>") is False
        assert entity.data == good

    def test_state_object_without_a_state_key_returns_false(self):
        assert make_entity().receive("x", json.dumps({"entity_id": "sensor.x"})) is False

    def test_non_object_payload_returns_false(self):
        assert make_entity().receive("x", json.dumps([1, 2, 3])) is False

    def test_failure_is_edge_triggered_then_recovers(self, caplog):
        entity = make_entity()
        with caplog.at_level(logging.DEBUG):
            entity.receive("x", state("unavailable"))
            entity.receive("x", state("unavailable"))
            entity.receive("x", state("23.4"))
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, "an offline entity must warn once, not once per event"
        assert any("recovered" in r.message for r in caplog.records)


class TestMetrics:
    def test_state_is_numeric_only_and_state_text_is_always_a_string(self):
        """Neither key may ever change type: InfluxDB rejects a field whose type flips, and
        an entity reporting '23.4' then 'heat' is exactly how that happens."""
        entity = make_entity()
        entity.receive("x", state("23.4"))
        numeric = entity.get_metrics()
        entity.receive("x", state("heat"))
        text = entity.get_metrics()

        assert numeric["state"] == pytest.approx(23.4)
        assert numeric["state_text"] == "23.4"
        assert "state" not in text
        assert text["state_text"] == "heat"

    def test_boolean_state_is_reported_separately(self):
        entity = make_entity()
        entity.receive("x", state("on"))
        metrics = entity.get_metrics()
        assert metrics["state_on"] is True
        assert "state" not in metrics, "a relay is not a float"

    def test_presentation_attributes_are_not_metrics(self):
        entity = make_entity()
        entity.receive("x", state("23.4", friendly_name="Hall", icon="mdi:thermometer",
                                  device_class="temperature", unit_of_measurement="°C", battery=87))
        metrics = entity.get_metrics()
        assert metrics["battery"] == 87
        for noise in ("friendly_name", "icon", "device_class", "unit_of_measurement"):
            assert noise not in metrics

    def test_non_scalar_attributes_are_dropped(self):
        entity = make_entity()
        entity.receive("x", state("on", hs_color=[30, 60]))
        assert "hs_color" not in entity.get_metrics(), "MetricSource is {name: scalar}"

    def test_metric_attributes_allowlist(self):
        entity = make_entity(metric_attributes=["battery"])
        entity.receive("x", state("23.4", battery=87, humidity=55))
        metrics = entity.get_metrics()
        assert "battery" in metrics
        assert "humidity" not in metrics

    def test_an_attribute_cannot_shadow_a_reserved_name(self):
        entity = make_entity()
        entity.receive("x", state("23.4", state=99, state_text=1, state_on=True))
        assert entity.get_metrics()["state"] == pytest.approx(23.4)
        assert entity.get_metrics()["state_text"] == "23.4"

    def test_metrics_before_any_reading_are_empty(self):
        assert make_entity().get_metrics() == {}

    def test_is_a_metric_source(self):
        assert isinstance(make_entity(), MetricSource)


class TestEnergyCapability:
    def test_unconfigured_entity_reports_zero(self):
        """The capability contract's 0.0, so a thermostat stays harmless in an algorithm
        that sums every EnergyMeter it can see."""
        entity = make_entity()
        entity.receive("x", state("23.4"))
        assert entity.get_total_energy_kwh() == 0.0

    def test_kwh_state_is_reported_as_is(self):
        entity = make_entity(energy_unit="kWh")
        entity.receive("x", state("1234.5"))
        assert entity.get_total_energy_kwh() == pytest.approx(1234.5)

    def test_wh_state_is_converted(self):
        entity = make_entity(energy_unit="Wh")
        entity.receive("x", state("1234500"))
        assert entity.get_total_energy_kwh() == pytest.approx(1234.5)

    def test_unit_is_taken_from_the_entitys_own_attribute(self):
        entity = make_entity(energy_attribute="total")
        entity.receive("x", state("23.4", total=1234500, unit_of_measurement="Wh"))
        assert entity.get_total_energy_kwh() == pytest.approx(1234.5)

    def test_named_attribute_is_read(self):
        entity = make_entity(energy_attribute="lifetime_energy", energy_unit="kWh")
        entity.receive("x", state("980", lifetime_energy=4321.0))
        assert entity.get_total_energy_kwh() == pytest.approx(4321.0)

    def test_missing_named_attribute_is_none_not_zero(self, caplog):
        """A configured meter that cannot be read must not silently deflate a site total."""
        entity = make_entity(energy_attribute="lifetime_energy")
        entity.receive("x", state("980"))
        with caplog.at_level(logging.WARNING):
            assert entity.get_total_energy_kwh() is None

    def test_non_numeric_state_is_none(self, caplog):
        entity = make_entity(energy_unit="kWh")
        entity.receive("x", state("heat"))
        with caplog.at_level(logging.WARNING):
            assert entity.get_total_energy_kwh() is None

    def test_unknown_unit_is_none(self, caplog):
        entity = make_entity(energy_unit="furlongs")
        entity.receive("x", state("12"))
        with caplog.at_level(logging.WARNING):
            assert entity.get_total_energy_kwh() is None

    def test_no_reading_yet_is_none(self):
        assert make_entity(energy_unit="kWh").get_total_energy_kwh() is None

    def test_the_energy_warning_is_edge_triggered(self, caplog):
        entity = make_entity(energy_attribute="missing")
        entity.receive("x", state("1"))
        with caplog.at_level(logging.DEBUG):
            entity.get_total_energy_kwh()
            entity.get_total_energy_kwh()
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    def test_is_an_energy_meter(self):
        assert isinstance(make_entity(), EnergyMeter)


class TestHaSwitch:
    def _switch(self, **controller) -> HaSwitch:
        return HaSwitch("boiler", {"name": "ha", "protocol": "home_assistant"},
                        {"entity_id": "switch.boiler"}, controller)

    def test_is_a_switch_and_writable(self):
        switch = self._switch(entity_id="switch.boiler")
        assert isinstance(switch, Switch)
        assert switch.is_writable is True

    def test_a_plain_entity_is_not_a_switch(self):
        """Load-bearing, and the reasoning lives in devices/ha_switch.py: `Switch` is a
        class-level type claim, so a read-only class inheriting it is selected as an actuator
        by every algorithm that looks for one."""
        assert not isinstance(make_entity(), Switch)
        assert make_entity().is_writable is False

    def test_it_inherits_the_parsing(self):
        switch = self._switch(entity_id="switch.boiler")
        assert switch.receive("switch.boiler", state("on", entity_id="switch.boiler")) is True
        assert switch.get_metrics()["state_on"] is True

    def test_unroutable_switch_warns_at_startup(self, caplog):
        with caplog.at_level(logging.WARNING):
            self._switch()
        assert any("declares neither controller_options.entity_id" in r.message for r in caplog.records)
