from unittest.mock import MagicMock

from tests.conftest import make_pseudo as make_pseudo_device


class TestPseudoDeviceReceive:
    def test_receive_topic_and_payload(self):
        device = make_pseudo_device()
        device.receive("some/topic", '{"power": 100}')

        assert device.data["topic"] == "some/topic"
        assert device.data["payload"] == '{"power": 100}'
        assert device.data["parsed"] == {"power": 100}

    def test_receive_payload_only(self):
        device = make_pseudo_device()
        device.receive('{"temp": 22}')

        assert device.data["topic"] is None
        assert device.data["payload"] == '{"temp": 22}'
        assert device.data["parsed"] == {"temp": 22}

    def test_receive_non_json_payload(self):
        device = make_pseudo_device()
        device.receive("some/topic", "plain_string")

        assert device.data["payload"] == "plain_string"
        assert device.data["parsed"] is None

    def test_receive_updates_devices_manager(self, devices_manager):
        device = make_pseudo_device()
        devices_manager.update_device(device)

        device.receive("topic", '{"value": 42}')

        retrieved = devices_manager.get_device(device.name)
        assert retrieved.data["parsed"] == {"value": 42}


class TestPseudoDeviceProperties:
    def test_is_readable_and_writable(self):
        device = make_pseudo_device()
        assert device.is_readable is True
        assert device.is_writable is True


class TestPseudoDeviceControl:
    def test_control_delegates_to_connector(self):
        device = make_pseudo_device()
        mock_connector = MagicMock()
        device.connector = mock_connector

        device.control("turn_off")

        mock_connector.send.assert_called_once_with(device, "turn_off")
