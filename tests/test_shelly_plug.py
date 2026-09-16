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

    @pytest.mark.parametrize("suffix,payload", [
        ("relay/0", "overpower"),
        ("overtemperature", "2"),
    ])
    def test_unknown_value_on_a_known_topic_raises(self, shelly, suffix, payload):
        with pytest.raises(NotImplementedError):
            shelly.receive_mqtt(f"{TOPIC_PREFIX}/{suffix}", payload)

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

    def test_unknown_protocol_raises(self):
        device = ShellyPlug(
            name="shelly_bad",
            connector_options={"name": "x", "protocol": "zigbee"},
            listener_options={},
            controller_options={},
        )
        with pytest.raises(NotImplementedError):
            device.receive("topic", "payload")

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
