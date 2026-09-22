"""Generic Modbus register-map meter: decoding, the gap contract, and capabilities.

No importorskip: devices/modbus_meter.py imports nothing from pymodbus and decodes raw
words with `struct` from the stdlib, which is exactly what lets a Modbus meter be replayed
on a machine with no Modbus stack installed.
"""
import json
import logging

import pytest

from api.capabilities import EnergyMeter, MetricSource, Switch
from devices.modbus_meter import WORDS, ModbusMeter
from devices.modbus_switch import ModbusSwitch

REGISTERS = [
    {"name": "energy_t1_kwh", "address": 0, "data_type": "UINT32", "scale": 0.01, "unit": "kWh", "role": "energy_import_kwh"},
    {"name": "energy_t2_kwh", "address": 2, "data_type": "UINT32", "scale": 0.01, "unit": "kWh", "role": "energy_import_kwh"},
    {"name": "power_w", "address": 12, "data_type": "INT32"},
    {"name": "voltage_v", "address": 20, "type": "input", "data_type": "UINT16", "scale": 0.1, "unit": "V"},
    {"name": "relay", "address": 8, "type": "coil"},
    {"name": "serial", "address": 90, "data_type": "STRING", "count": 4},
]


def make_meter(name: str = "meter", registers=None, protocol: str = "modbus_tcp", **listener) -> ModbusMeter:
    options = {"registers": REGISTERS if registers is None else registers, **listener}
    return ModbusMeter(name, {"name": "mb", "protocol": protocol}, options, {})


def blocks(**named) -> str:
    """The connector's wire format: raw words, JSON-encoded, exactly as a replay row."""
    return json.dumps({"blocks": named})


class TestWireFormat:
    def test_single_string_argument_is_accepted(self):
        meter = make_meter()
        assert meter.receive(blocks(voltage_v=[2301])) is True
        assert meter.data["registers"]["voltage_v"]["value"] == pytest.approx(230.1)

    def test_topic_and_payload_arity_is_tolerated(self, caplog):
        """A replay CSV row with a non-empty topic column calls receive() with two args.

        PseudoConnector swallows the resulting TypeError in its own except handler, so
        without this the backtest would silently produce nothing at all.
        """
        meter = make_meter()
        with caplog.at_level(logging.DEBUG):
            assert meter.receive("some/topic", blocks(voltage_v=[2301])) is True
        assert meter.data["registers"]["voltage_v"]["value"] == pytest.approx(230.1)

    def test_no_argument_is_rejected(self, caplog):
        meter = make_meter()
        with caplog.at_level(logging.WARNING):
            assert meter.receive() is False

    def test_an_unserved_protocol_is_refused_rather_than_raised(self):
        meter = make_meter(protocol="carrier_pigeon")
        assert meter.receive(blocks(voltage_v=[1])) is False
        assert meter.data == {}

    def test_the_refusal_is_reported_once_at_construction(self, caplog):
        with caplog.at_level(logging.ERROR):
            meter = make_meter(protocol="carrier_pigeon")
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert "carrier_pigeon" in errors[0].message

        caplog.clear()
        with caplog.at_level(logging.ERROR):
            for _ in range(5):
                meter.receive(blocks(voltage_v=[1]))
        assert [r for r in caplog.records if r.levelname == "ERROR"] == []


class TestDecoding:
    def test_uint32_big_word_order(self):
        meter = make_meter()
        meter.receive(blocks(energy_t1_kwh=[1, 2]))
        assert meter.data["registers"]["energy_t1_kwh"]["value"] == pytest.approx(65538 * 0.01)

    def test_word_order_little_swaps_the_words(self):
        meter = make_meter(word_order="little")
        meter.receive(blocks(energy_t1_kwh=[1, 2]))
        # little: [1, 2] reads as words 2,1 -> 0x00020001
        assert meter.data["registers"]["energy_t1_kwh"]["value"] == pytest.approx(131073 * 0.01)

    def test_per_register_word_order_overrides_the_device_default(self):
        registers = [{"name": "e", "address": 0, "data_type": "UINT32", "word_order": "little"}]
        meter = make_meter(registers=registers, word_order="big")
        meter.receive(blocks(e=[1, 2]))
        assert meter.data["registers"]["e"]["value"] == 131073

    def test_int32_is_signed(self):
        meter = make_meter()
        meter.receive(blocks(power_w=[0xFFFF, 0xF448]))
        assert meter.data["registers"]["power_w"]["value"] == -3000

    def test_float32(self):
        registers = [{"name": "f", "address": 0, "data_type": "FLOAT32"}]
        meter = make_meter(registers=registers)
        meter.receive(blocks(f=[0x4248, 0x0000]))  # 50.0
        assert meter.data["registers"]["f"]["value"] == pytest.approx(50.0)

    def test_string_is_ascii_and_nul_trimmed(self):
        meter = make_meter()
        meter.receive(blocks(serial=[0x4142, 0x4344, 0x4546, 0x0000]))
        assert meter.data["registers"]["serial"]["value"] == "ABCDEF"

    def test_coil_words_decode_to_bool(self):
        meter = make_meter()
        meter.receive(blocks(relay=[True]))
        assert meter.data["registers"]["relay"]["value"] is True

    def test_raw_keeps_the_words(self):
        registers = [{"name": "vendor", "address": 0, "data_type": "RAW", "count": 3}]
        meter = make_meter(registers=registers)
        meter.receive(blocks(vendor=[1, 2, 3]))
        assert meter.data["registers"]["vendor"]["value"] == [1, 2, 3]

    def test_unit_rides_along_when_declared(self):
        meter = make_meter()
        meter.receive(blocks(voltage_v=[2301], power_w=[0, 5]))
        assert meter.data["registers"]["voltage_v"]["unit"] == "V"
        assert "unit" not in meter.data["registers"]["power_w"]

    def test_raw_words_are_kept_beside_the_decoded_value(self):
        meter = make_meter()
        meter.receive(blocks(voltage_v=[2301]))
        assert meter.data["registers"]["voltage_v"]["raw"] == [2301]

    def test_undeclared_register_in_the_payload_is_ignored(self):
        meter = make_meter()
        assert meter.receive(blocks(voltage_v=[2301], mystery=[7])) is True
        assert "mystery" not in meter.data["registers"]

    def test_short_block_for_a_wide_type_warns_and_is_skipped(self, caplog):
        meter = make_meter()
        with caplog.at_level(logging.WARNING):
            assert meter.receive(blocks(energy_t1_kwh=[1], voltage_v=[2301])) is True
        assert "energy_t1_kwh" not in meter.data["registers"]
        assert any("Cannot decode register" in r.message for r in caplog.records)


class TestGapContract:
    """A malformed payload must never overwrite the last good reading."""

    def test_unreadable_payload_returns_false_and_keeps_the_reading(self, caplog):
        meter = make_meter()
        meter.receive(blocks(voltage_v=[2301]))
        good = dict(meter.data)
        with caplog.at_level(logging.WARNING):
            assert meter.receive("not json at all") is False
        assert meter.data == good
        assert any("Unreadable payload" in r.message for r in caplog.records)

    def test_missing_blocks_key_returns_false(self):
        meter = make_meter()
        assert meter.receive(json.dumps({"nope": {}})) is False

    def test_empty_blocks_returns_false(self):
        meter = make_meter()
        assert meter.receive(blocks()) is False

    def test_payload_with_only_undeclared_registers_returns_false(self):
        meter = make_meter()
        assert meter.receive(blocks(mystery=[1])) is False

    def test_failure_is_edge_triggered_then_recovers(self, caplog):
        meter = make_meter()
        with caplog.at_level(logging.DEBUG):
            meter.receive("bad")
            meter.receive("bad")
            meter.receive(blocks(voltage_v=[2301]))
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "Unreadable payload" in r.message]
        assert len(warnings) == 1, "a broken meter must warn once, not once per poll"
        assert any("recovered" in r.message for r in caplog.records)

    def test_a_partial_poll_replaces_the_reading_wholesale(self):
        """A stale register must not sit beside a fresh one under one timestamp."""
        meter = make_meter()
        meter.receive(blocks(voltage_v=[2301], power_w=[0, 5]))
        meter.receive(blocks(voltage_v=[2302]))
        assert set(meter.data["registers"]) == {"voltage_v"}


class TestMetrics:
    def test_metrics_are_named_and_scalar_only(self):
        meter = make_meter()
        meter.receive(blocks(voltage_v=[2301], relay=[True], serial=[0x4142], power_w=[0, 5]))
        metrics = meter.get_metrics()
        assert metrics["voltage_v"] == pytest.approx(230.1)
        assert metrics["relay"] is True
        assert metrics["power_w"] == 5
        assert "serial" not in metrics, "a string register is not a measurement"

    def test_raw_word_lists_are_not_metrics(self):
        meter = make_meter(registers=[{"name": "vendor", "address": 0, "data_type": "RAW"}])
        meter.receive(blocks(vendor=[1]))
        assert meter.get_metrics() == {}

    def test_metrics_on_an_empty_device_are_empty_not_an_exception(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert make_meter().get_metrics() == {}

    def test_is_a_metric_source(self):
        assert isinstance(make_meter(), MetricSource)


class TestEnergyCapability:
    def test_roles_are_summed_across_tariffs(self):
        meter = make_meter()
        meter.receive(blocks(energy_t1_kwh=[0, 10000], energy_t2_kwh=[0, 5000]))
        assert meter.get_total_energy_kwh() == pytest.approx(150.0)

    def test_only_role_bearing_registers_count(self):
        meter = make_meter()
        meter.receive(blocks(energy_t1_kwh=[0, 10000], power_w=[0, 999]))
        assert meter.get_total_energy_kwh() == pytest.approx(100.0)

    def test_no_reading_yet_is_none(self):
        assert make_meter().get_total_energy_kwh() is None

    def test_no_role_declared_reports_zero(self, caplog):
        """The capability contract's 0.0: well-formed data holding no kWh register."""
        with caplog.at_level(logging.INFO):
            meter = make_meter(registers=[{"name": "voltage_v", "address": 0}])
        meter.receive(blocks(voltage_v=[2301]))
        assert meter.get_total_energy_kwh() == 0.0
        assert any("will report 0.0" in r.message for r in caplog.records), "the silent zero must be announced at startup"

    def test_non_numeric_energy_register_is_none(self, caplog):
        registers = [{"name": "e", "address": 0, "data_type": "STRING", "count": 2, "role": "energy_import_kwh"}]
        meter = make_meter(registers=registers)
        meter.receive(blocks(e=[0x4142, 0x4344]))
        with caplog.at_level(logging.WARNING):
            assert meter.get_total_energy_kwh() is None

    def test_is_an_energy_meter(self):
        assert isinstance(make_meter(), EnergyMeter)


class TestRegisterMapValidation:
    """A malformed map must never raise out of the constructor: main.create_classes would
    contain it, but the meter would then be skipped entirely rather than reporting the
    registers it could still read."""

    def test_unknown_data_type_falls_back_to_uint16(self, caplog):
        with caplog.at_level(logging.WARNING):
            meter = make_meter(registers=[{"name": "x", "address": 0, "data_type": "FLOAT24"}])
        meter.receive(blocks(x=[7]))
        assert meter.data["registers"]["x"]["value"] == 7

    def test_unknown_role_is_dropped(self, caplog):
        with caplog.at_level(logging.WARNING):
            meter = make_meter(registers=[{"name": "x", "address": 0, "role": "energy_export_kwh"}])
        meter.receive(blocks(x=[7]))
        assert meter.get_total_energy_kwh() == 0.0
        assert any("Unknown role" in r.message for r in caplog.records)

    def test_role_with_a_conflicting_unit_warns_but_does_not_convert(self, caplog):
        """Deriving a conversion from `unit` would let a typo rescale a revenue reading."""
        registers = [{"name": "e", "address": 0, "unit": "Wh", "role": "energy_import_kwh"}]
        with caplog.at_level(logging.WARNING):
            meter = make_meter(registers=registers)
        meter.receive(blocks(e=[1000]))
        assert meter.get_total_energy_kwh() == 1000.0, "no implicit unit conversion"
        assert any("no conversion is applied" in r.message for r in caplog.records)

    def test_unknown_word_order_falls_back_to_big(self, caplog):
        with caplog.at_level(logging.WARNING):
            meter = make_meter(word_order="sideways")
        meter.receive(blocks(energy_t1_kwh=[1, 2]))
        assert meter.data["registers"]["energy_t1_kwh"]["value"] == pytest.approx(655.38)

    def test_registers_not_a_list_warns_and_decodes_nothing(self, caplog):
        with caplog.at_level(logging.WARNING):
            meter = make_meter(registers="oops")
        assert meter.receive(blocks(x=[1])) is False

    def test_non_object_and_unnamed_entries_are_skipped(self):
        meter = make_meter(registers=["nope", {"address": 4}, {"name": "ok", "address": 0}])
        assert meter.receive(blocks(ok=[3])) is True


class TestModbusSwitch:
    def test_is_a_switch_and_writable(self):
        switch = ModbusSwitch("sw", {"name": "mb", "protocol": "modbus_tcp"},
                              {"registers": [{"name": "relay", "address": 8, "type": "coil"}]},
                              {"kind": "coil", "address": 8})
        assert isinstance(switch, Switch)
        assert switch.is_writable is True

    def test_the_read_only_meter_is_not_a_switch(self):
        """Load-bearing, and the reasoning lives in devices/modbus_switch.py: `Switch` is a
        class-level type claim, so a read-only class inheriting it is selected as an actuator
        by every algorithm that looks for one."""
        assert not isinstance(make_meter(), Switch)
        assert make_meter().is_writable is False

    def test_it_inherits_the_parsing(self):
        switch = ModbusSwitch("sw", {"name": "mb", "protocol": "modbus_tcp"},
                              {"registers": [{"name": "relay", "address": 8, "type": "coil"}]},
                              {"kind": "coil", "address": 8})
        assert switch.receive(blocks(relay=[True])) is True
        assert switch.get_metrics() == {"relay": True}

    def test_missing_controller_address_warns_at_startup(self, caplog):
        with caplog.at_level(logging.WARNING):
            ModbusSwitch("sw", {"name": "mb", "protocol": "modbus_tcp"}, {"registers": []}, {})
        assert any("declares no controller_options.address" in r.message for r in caplog.records)


def test_word_table_matches_the_connectors_copy():
    """The two tables are deliberately duplicated to keep the plugin axes independent —
    a connector must work with any device and a device with any connector — so nothing but
    this test stops them drifting apart."""
    pytest.importorskip("pymodbus", reason="pip install -r requirements-modbus.txt")
    from connectors.modbus_tcp import _WORDS

    assert _WORDS == WORDS
