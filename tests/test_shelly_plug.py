import logging

import pytest

from api.capabilities import Switch
from devices.shelly_plug import ShellyPlug
from devices_manager.devices_manager import DevicesManager
from tests.conftest import StubConnector, make_shelly


@pytest.fixture
def shelly():
    return make_shelly()


TOPIC_PREFIX = "shellies/shplg-s-ABC123"


class TestShellyPlugReceiveMQTT:
    @pytest.mark.parametrize("suffix,payload,key,expected", [
        ("relay/0/power", "42.5", "power", "42.5"),
        ("relay/0/energy", "1234", "energy", "1234"),
        ("relay/0/overpower_value", "2300", "overpower_value", "2300"),
        ("temperature", "38.2", "temperature", "38.2"),
        ("temperature_f", "100.7", "temperature_f", "100.7"),
        # The two booleans: the payload is a token, not the value stored.
        ("relay/0", "on", "status", True),
        ("relay/0", "off", "status", False),
        ("overtemperature", "1", "overtemperature", True),
        ("overtemperature", "0", "overtemperature", False),
    ])
    def test_topic_lands_in_data(self, shelly, suffix, payload, key, expected):
        shelly.receive_mqtt(f"{TOPIC_PREFIX}/{suffix}", payload)
        assert shelly.data[key] == expected

    def test_overpower_is_a_fault_flag_not_a_relay_state(self, shelly, caplog):
        """`overpower` is a status this hardware really publishes when its protection trips.

        It used to raise NotImplementedError from the MQTT per-message daemon thread, where
        threading.excepthook prints past the configured logger — losing the log line at the
        moment an operator most needs it.
        """
        shelly.receive_mqtt(f"{TOPIC_PREFIX}/relay/0", "on")
        with caplog.at_level(logging.WARNING):
            assert shelly.receive_mqtt(f"{TOPIC_PREFIX}/relay/0", "overpower") is True

        assert shelly.data["overpower"] is True
        # Left alone, deliberately: the plug is saying it tripped, not what its relay now
        # reads. Writing False would be an inference published as a measurement.
        assert shelly.data["status"] is True
        assert any("overpower trip" in r.message for r in caplog.records)

    def test_a_recovered_relay_clears_the_fault_flag(self, shelly):
        shelly.receive_mqtt(f"{TOPIC_PREFIX}/relay/0", "overpower")
        shelly.receive_mqtt(f"{TOPIC_PREFIX}/relay/0", "off")
        assert "overpower" not in shelly.data
        assert shelly.data["status"] is False

    def test_overpower_is_absent_until_it_happens(self, shelly):
        """The standing golden-fixture guard.

        storage/csv_file.py writes self.data verbatim, so initialising this key to False
        instead of popping it would rewrite all eighteen shelly rows of
        examples/auto_toggle/expected/device_data.csv — checksummed in MANIFEST.json and
        vendored into the viewer's repository. This fails the moment someone "tidies" the
        pop into `= False`.
        """
        shelly.receive_mqtt(f"{TOPIC_PREFIX}/relay/0", "on")
        assert "overpower" not in shelly.data

    @pytest.mark.parametrize("suffix,payload,key", [
        ("relay/0", "sideways", "status"),
        ("overtemperature", "2", "overtemperature"),
    ])
    def test_an_unmodelled_value_on_a_known_topic_is_a_gap(self, shelly, caplog, suffix, payload, key):
        with caplog.at_level(logging.WARNING):
            assert shelly.receive_mqtt(f"{TOPIC_PREFIX}/{suffix}", payload) is False
        assert key not in shelly.data
        assert any("no reading taken" in r.message for r in caplog.records)

    def test_unknown_topic_no_crash(self, shelly):
        # Unknown topic just logs warning, doesn't raise
        shelly.receive_mqtt("shellies/shplg-s-ABC123/unknown/topic", "value")
        assert "unknown" not in shelly.data

    def test_multiple_fields_accumulated(self, shelly):
        shelly.receive_mqtt("shellies/shplg-s-ABC123/relay/0/power", "50")
        shelly.receive_mqtt("shellies/shplg-s-ABC123/relay/0", "on")
        shelly.receive_mqtt("shellies/shplg-s-ABC123/temperature", "35")
        assert shelly.data["power"] == "50"
        assert shelly.data["status"] is True
        assert shelly.data["temperature"] == "35"


class TestShellyPlugReceiveDispatch:
    def test_mqtt_protocol_dispatches(self, shelly):
        shelly.receive("shellies/shplg-s-ABC123/relay/0/power", "100")
        assert shelly.data["power"] == "100"

    @staticmethod
    def _shelly_on(protocol: str):
        return ShellyPlug(
            name="shelly_bad",
            connector_options={"name": "x", "protocol": protocol},
            listener_options={},
            controller_options={},
        )

    def test_an_unserved_protocol_is_refused_rather_than_raised(self):
        device = self._shelly_on("zigbee")
        assert device.receive("topic", "payload") is False
        assert device.data == {}

    def test_the_refusal_is_reported_once_at_construction(self, caplog):
        with caplog.at_level(logging.ERROR):
            device = self._shelly_on("zigbee")
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert "zigbee" in errors[0].message

        caplog.clear()
        with caplog.at_level(logging.ERROR):
            for _ in range(5):
                device.receive("topic", "payload")
        assert [r for r in caplog.records if r.levelname == "ERROR"] == []

    def test_http_api_is_refused_with_the_status_document_reason(self, caplog):
        # A Gen1 Shelly really does serve /status, so this is worth its own sentence.
        with caplog.at_level(logging.ERROR):
            self._shelly_on("http_api")
        assert any("/status" in r.message for r in caplog.records)


class TestShellyPlugArity:
    """Both call shapes, because PseudoConnector picks one per replay row.

    This device is the exception among the six: it cannot absorb the one-argument arity,
    because the topic *is* the parse. So a topic-less row is a stated gap rather than a
    TypeError the replay loop swallows as "Error replaying entry".
    """

    def test_a_topic_less_replay_row_is_a_gap_not_a_crash(self, shelly, caplog):
        with caplog.at_level(logging.WARNING):
            assert shelly.receive("118.4") is False  # must not raise TypeError
        assert shelly.data == {}
        assert any("needs its topic" in r.message for r in caplog.records)

    def test_a_padded_replay_row_is_not_a_reading(self, shelly, caplog):
        # csv.DictReader pads a short row with None, so (topic, None) is reachable — and
        # storing it would file a reading of nothing under a real topic name.
        with caplog.at_level(logging.WARNING):
            assert shelly.receive(f"{TOPIC_PREFIX}/relay/0/power", None) is False
        assert "power" not in shelly.data

    def test_the_normal_two_argument_arity_still_parses(self, shelly):
        assert shelly.receive(f"{TOPIC_PREFIX}/relay/0/power", "118.4") is True
        assert shelly.data["power"] == "118.4"

    def test_framework_publishes_after_receive(self, shelly):
        # receive() only parses into self.data; the connector's
        # on_device_data_received() hook publishes the device to DevicesManager.
        dm = DevicesManager()
        shelly.receive("shellies/shplg-s-ABC123/relay/0/power", "75")
        assert dm.get_device("shelly_1") is None  # not self-registered by receive()
        StubConnector().on_device_data_received(shelly)
        assert dm.get_device("shelly_1").data["power"] == "75"


class TestShellyPlugProperties:
    def test_is_readable_and_writable(self, shelly):
        assert shelly.is_readable is True
        # The relay accepts on/off commands, so the plug must be writable —
        # Device.control() enforces is_writable (F-7)
        assert shelly.is_writable is True

    def test_is_switch(self, shelly):
        # Capability interface: algorithms select switches via isinstance(device, Switch)
        assert isinstance(shelly, Switch)
        assert shelly.COMMAND_ON == "on"
        assert shelly.COMMAND_OFF == "off"


class TestShellyPlugUnknownTopic:
    """A topic this device does not model is not a reading."""

    def test_unknown_topic_is_rejected(self, shelly):
        assert shelly.receive("shellies/plug/relay/0/unmodelled", "1") is False

    def test_known_topic_is_accepted(self, shelly):
        assert shelly.receive("shellies/plug/relay/0/power", "75") is True

    def test_the_framework_writes_nothing_for_an_unknown_topic(self, shelly, storage_manager):
        from tests.conftest import StubConnector, StubStorageBackend

        backend = StubStorageBackend()
        storage_manager.register(backend)
        connector = StubConnector()

        connector.on_device_data_received(shelly, shelly.receive("shellies/plug/relay/0/power", "75"))
        connector.on_device_data_received(shelly, shelly.receive("shellies/plug/other", "x"))

        assert len(backend.device_data_calls) == 1
