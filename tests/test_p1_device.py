import logging

import pytest
from copy import deepcopy

from api.capabilities import EnergyMeter
from devices.p1 import P1
from devices_manager.devices_manager import DevicesManager
from tests.conftest import StubConnector, build_p1_telegram as _build_p1_telegram, make_p1


@pytest.fixture
def p1_mqtt():
    return make_p1()


@pytest.fixture
def p1_lora():
    return P1(
        name="p1_lora",
        connector_options={"name": "lora_1", "protocol": "lora"},
        listener_options={},
        controller_options={},
    )


class TestP1ReceiveMQTT:
    def test_parses_obis(self, p1_mqtt):
        telegram = _build_p1_telegram("1-0:1.8.1(001234.567*kWh)\r\n")
        p1_mqtt.receive_mqtt("p1/data", telegram)
        assert p1_mqtt.data != {}
        assert p1_mqtt.data["model_id"] == "ISK"
        assert p1_mqtt.data["data"][0]["data"][0]["value"] == 1234.567

    def test_invalid_payload_empty_dict(self, p1_mqtt):
        p1_mqtt.receive_mqtt("p1/data", "garbage")
        assert p1_mqtt.data == {}

    def test_receive_dispatches_to_mqtt(self, p1_mqtt):
        telegram = _build_p1_telegram("1-0:1.8.1(100*kWh)\r\n")
        p1_mqtt.receive("p1/data", telegram)
        assert p1_mqtt.data != {}


class TestP1ReceiveLoRa:
    def test_lora_noop(self, p1_lora):
        # LoRa receive is a stub — should not crash
        p1_lora.receive("some_data")
        # data stays at initial empty dict
        assert p1_lora.data == {}


class TestP1ReceiveDispatch:
    def test_unknown_protocol_raises(self):
        device = P1(
            name="p1_bad",
            connector_options={"name": "x", "protocol": "zigbee"},
            listener_options={},
            controller_options={},
        )
        with pytest.raises(NotImplementedError):
            device.receive("topic", "payload")

    def test_framework_publishes_after_receive(self, p1_mqtt):
        # receive() only parses into self.data — it no longer self-registers.
        # The connector's on_device_data_received() hook publishes the device
        # to DevicesManager, exactly as every connector does after receive().
        dm = DevicesManager()
        telegram = _build_p1_telegram("1-0:1.8.1(555*kWh)\r\n")
        p1_mqtt.receive("p1/data", telegram)
        assert dm.get_device("p1_meter") is None  # not self-registered by receive()
        StubConnector().on_device_data_received(p1_mqtt)
        assert dm.get_device("p1_meter").data != {}


class TestP1Properties:
    def test_is_readable_not_writable(self, p1_mqtt):
        assert p1_mqtt.is_readable is True
        assert p1_mqtt.is_writable is False

    def test_control_is_noop(self, p1_mqtt):
        # P1 is not writable — Device.control() warns and no-ops
        p1_mqtt.control("anything")  # should not raise


def _obis_energy_entry(values, unit="kWh", instance=8):
    return {
        "obis": {"medium": 1, "channel": 0, "class": 1, "instance": instance, "attribute": 1},
        "data": [{"value": v, "unit": unit} for v in values],
    }


class TestP1EnergyMeter:
    def test_is_energy_meter(self, p1_mqtt):
        assert isinstance(p1_mqtt, EnergyMeter)

    def test_sums_kwh_registers(self, p1_mqtt):
        p1_mqtt.data = {"data": [_obis_energy_entry([300.0, 250.0])]}
        assert p1_mqtt.get_total_energy_kwh() == 550.0

    def test_end_to_end_via_telegram(self, p1_mqtt):
        telegram = _build_p1_telegram("1-0:1.8.1(001234.567*kWh)\r\n")
        p1_mqtt.receive_mqtt("p1/data", telegram)
        assert p1_mqtt.get_total_energy_kwh() == 1234.567

    def test_malformed_data_returns_none(self, p1_mqtt, caplog):
        p1_mqtt.data = {"garbage": True}  # no "data" key
        assert p1_mqtt.get_total_energy_kwh() is None
        assert "Malformed data" in caplog.text

    def test_empty_data_returns_none(self, p1_mqtt):
        assert p1_mqtt.data == {}
        assert p1_mqtt.get_total_energy_kwh() is None

    def test_non_kwh_unit_ignored(self, p1_mqtt):
        p1_mqtt.data = {"data": [_obis_energy_entry([9999], unit="W")]}
        assert p1_mqtt.get_total_energy_kwh() == 0.0

    def test_non_matching_obis_ignored(self, p1_mqtt):
        p1_mqtt.data = {"data": [_obis_energy_entry([9999], instance=7)]}
        assert p1_mqtt.get_total_energy_kwh() == 0.0

    def test_deepcopy_keeps_capability(self, p1_mqtt):
        # Algorithms receive deep-copied snapshots — the capability must survive
        p1_mqtt.data = {"data": [_obis_energy_entry([100.0])]}
        snapshot = deepcopy(p1_mqtt)
        assert isinstance(snapshot, EnergyMeter)
        assert snapshot.get_total_energy_kwh() == 100.0


def _obis_entry(code: str, blocks: list[dict]) -> dict:
    """Build a parsed OBIS entry from an A-B:C.D.E code string."""
    medium, rest = code.split("-", 1)
    channel, quantity = rest.split(":", 1)
    obis_class, instance, attribute = quantity.split(".")
    return {
        "obis": {
            "medium": int(medium), "channel": int(channel), "class": int(obis_class),
            "instance": int(instance), "attribute": int(attribute),
        },
        "data": blocks,
    }


class TestP1ObisCode:
    def test_rebuilds_the_standard_reference(self):
        obis = {"medium": 1, "channel": 0, "class": 1, "instance": 8, "attribute": 1}
        assert P1.obis_code(obis) == "1-0:1.8.1"

    def test_tolerates_non_integer_parts(self):
        # The parser's groups are \w+, so a vendor code with letters arrives as a string.
        obis = {"medium": 0, "channel": 1, "class": "C", "instance": 1, "attribute": 0}
        assert P1.obis_code(obis) == "0-1:C.1.0"


class TestP1Metrics:
    """Keys must name the register, not its position in the telegram."""

    def test_is_a_metric_source(self, p1_mqtt):
        from api.capabilities import MetricSource
        assert isinstance(p1_mqtt, MetricSource)

    def test_known_registers_get_friendly_names(self, p1_mqtt):
        p1_mqtt.data = {"data": [
            _obis_entry("1-0:1.8.1", [{"value": 1234.567, "unit": "kWh"}]),
            _obis_entry("1-0:2.8.2", [{"value": 42.0, "unit": "kWh"}]),
            _obis_entry("1-0:32.7.0", [{"value": 230.1, "unit": "V"}]),
        ]}
        assert p1_mqtt.get_metrics() == {
            "energy_import_t1_kwh": 1234.567,
            "energy_export_t2_kwh": 42.0,
            "voltage_l1_v": 230.1,
        }

    def test_unknown_register_keeps_its_raw_code(self, p1_mqtt):
        p1_mqtt.data = {"data": [_obis_entry("1-0:99.1.7", [{"value": 5.0}])]}
        assert p1_mqtt.get_metrics() == {"1-0:99.1.7": 5.0}

    def test_keys_do_not_move_when_the_telegram_shrinks(self, p1_mqtt):
        """The whole point: index 7 meant a different register when a line was dropped."""
        full = [
            _obis_entry("0-0:96.3.10", [{"value": 1}]),
            _obis_entry("1-0:1.8.1", [{"value": 100.0, "unit": "kWh"}]),
        ]
        p1_mqtt.data = {"data": full}
        with_all = p1_mqtt.get_metrics()
        p1_mqtt.data = {"data": full[1:]}  # firmware stops emitting the breaker state
        with_fewer = p1_mqtt.get_metrics()
        assert with_all["energy_import_t1_kwh"] == with_fewer["energy_import_t1_kwh"] == 100.0

    def test_text_blocks_are_dropped(self, p1_mqtt):
        # Equipment ids and message registers are identity, not measurement.
        p1_mqtt.data = {"data": [
            _obis_entry("0-0:96.1.1", [{"value": "4B414C4C"}]),
            _obis_entry("1-0:1.8.1", [{"value": 100.0, "unit": "kWh"}]),
        ]}
        assert p1_mqtt.get_metrics() == {"energy_import_t1_kwh": 100.0}

    def test_gas_register_drops_its_capture_timestamp(self, p1_mqtt):
        # 0-1:24.2.3 carries a timestamp block and the reading; filtering to numbers is
        # what lets `gas_m3` mean the gas reading rather than the timestamp.
        p1_mqtt.data = {"data": [_obis_entry("0-1:24.2.3", [
            {"value": "230101120000S"}, {"value": 1234.567, "unit": "m3"},
        ])]}
        assert p1_mqtt.get_metrics() == {"gas_m3": 1234.567}

    def test_multiple_numeric_blocks_are_indexed(self, p1_mqtt):
        # Brackets, not dots: the storage field separator defaults to "." and an OBIS
        # code already contains dots.
        p1_mqtt.data = {"data": [_obis_entry("1-0:1.8.1", [
            {"value": 300.0, "unit": "kWh"}, {"value": 250.0, "unit": "kWh"},
        ])]}
        assert p1_mqtt.get_metrics() == {
            "energy_import_t1_kwh[0]": 300.0,
            "energy_import_t1_kwh[1]": 250.0,
        }

    def test_repeated_code_across_entries_is_indexed(self, p1_mqtt):
        p1_mqtt.data = {"data": [
            _obis_entry("1-0:1.8.1", [{"value": 1.0}]),
            _obis_entry("1-0:1.8.1", [{"value": 2.0}]),
        ]}
        assert p1_mqtt.get_metrics() == {
            "energy_import_t1_kwh[0]": 1.0,
            "energy_import_t1_kwh[1]": 2.0,
        }

    def test_booleans_are_not_treated_as_numbers(self, p1_mqtt):
        p1_mqtt.data = {"data": [_obis_entry("1-0:1.8.1", [{"value": True}])]}
        assert p1_mqtt.get_metrics() == {}

    def test_empty_data_returns_empty(self, p1_mqtt):
        p1_mqtt.data = {}
        assert p1_mqtt.get_metrics() == {}

    def test_malformed_data_warns_and_returns_empty(self, p1_mqtt, caplog):
        p1_mqtt.data = {"data": [{"garbage": True}]}
        assert p1_mqtt.get_metrics() == {}
        assert "Malformed data" in caplog.text

    def test_never_raises_on_junk(self, p1_mqtt):
        for junk in ({"data": "not-a-list"}, {"data": [None]}, {"data": [{"obis": 1}]}):
            p1_mqtt.data = junk
            assert p1_mqtt.get_metrics() == {}

    def test_end_to_end_from_a_real_telegram(self, p1_mqtt):
        telegram = _build_p1_telegram(
            "1-0:1.8.1(001234.567*kWh)\r\n1-0:1.7.0(00.512*kW)\r\n"
        )
        p1_mqtt.receive_mqtt("p1/data", telegram)
        assert p1_mqtt.get_metrics() == {
            "energy_import_t1_kwh": 1234.567,
            "power_import_kw": 0.512,
        }

    def test_survives_deepcopy(self, p1_mqtt):
        from api.capabilities import MetricSource
        p1_mqtt.data = {"data": [_obis_entry("1-0:1.8.1", [{"value": 7.0, "unit": "kWh"}])]}
        snapshot = deepcopy(p1_mqtt)
        assert isinstance(snapshot, MetricSource)
        assert snapshot.get_metrics() == {"energy_import_t1_kwh": 7.0}


class TestP1ParseFailure:
    """A telegram we cannot read is a gap in the record, not a reading of nothing.

    `self.data` used to be assigned unconditionally, so one corrupt telegram — a CRC
    error on a noisy serial line is routine — wiped the last good reading, and the
    framework then published and stored that emptiness as though the meter had reported
    it. Losing a sample is acceptable; inventing one is not.
    """

    GOOD = "1-0:1.8.1(001234.567*kWh)\r\n"

    def test_bad_telegram_keeps_the_last_good_reading(self, p1_mqtt):
        p1_mqtt.receive_mqtt("p1/data", _build_p1_telegram(self.GOOD))
        good = p1_mqtt.data

        p1_mqtt.receive_mqtt("p1/data", "garbage")

        assert p1_mqtt.data == good
        assert p1_mqtt.get_total_energy_kwh() == 1234.567

    def test_bad_telegram_before_any_good_one_leaves_data_empty(self, p1_mqtt):
        p1_mqtt.receive_mqtt("p1/data", "garbage")
        assert p1_mqtt.data == {}

    def test_failure_is_warned_once_then_quietly(self, p1_mqtt, caplog):
        # A meter that has gone bad reports every second; one warning, not a flood.
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                p1_mqtt.receive_mqtt("p1/data", "garbage")

        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    def test_recovery_is_reported(self, p1_mqtt, caplog):
        p1_mqtt.receive_mqtt("p1/data", "garbage")
        with caplog.at_level(logging.INFO):
            p1_mqtt.receive_mqtt("p1/data", _build_p1_telegram(self.GOOD))

        assert "recovered" in caplog.text
        assert p1_mqtt.get_total_energy_kwh() == 1234.567

    def test_a_good_telegram_still_replaces_the_previous_one(self, p1_mqtt):
        p1_mqtt.receive_mqtt("p1/data", _build_p1_telegram(self.GOOD))
        p1_mqtt.receive_mqtt("p1/data", _build_p1_telegram("1-0:1.8.1(002000.000*kWh)\r\n"))

        assert p1_mqtt.get_total_energy_kwh() == 2000.0


class TestP1RejectsUnreadableTelegrams:
    """The device tells the framework a telegram was unusable, so it records a gap."""

    def test_unreadable_telegram_is_rejected(self, p1_mqtt):
        assert p1_mqtt.receive("p1/data", "garbage") is False

    def test_readable_telegram_is_accepted(self, p1_mqtt):
        telegram = _build_p1_telegram("1-0:1.8.1(001234.567*kWh)\r\n")
        assert p1_mqtt.receive("p1/data", telegram) is True

    def test_the_framework_writes_nothing_for_an_unreadable_telegram(self, p1_mqtt, storage_manager):
        from tests.conftest import StubStorageBackend

        backend = StubStorageBackend()
        storage_manager.register(backend)
        connector = StubConnector()

        good = _build_p1_telegram("1-0:1.8.1(001234.567*kWh)\r\n")
        connector.on_device_data_received(p1_mqtt, p1_mqtt.receive("p1/data", good))
        connector.on_device_data_received(p1_mqtt, p1_mqtt.receive("p1/data", "garbage"))

        # One reading stored, not two — the corrupt telegram is a gap, and storage no
        # longer shows the previous value repeated under a new timestamp.
        assert len(backend.device_data_calls) == 1

    def test_the_lora_stub_produces_no_reading(self, p1_lora):
        assert p1_lora.receive("anything") is False
