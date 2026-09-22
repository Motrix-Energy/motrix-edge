from unittest.mock import MagicMock



class TestDevicesManager:
    def test_register_and_get(self, devices_manager, make_device):
        device = make_device(name="dev1")
        device.data = {"power": 100}
        devices_manager.update_device(device)

        retrieved = devices_manager.get_device("dev1")
        assert retrieved is not None
        assert retrieved.name == "dev1"
        assert retrieved.data == {"power": 100}

    def test_get_devices_no_filter(self, devices_manager, make_device):
        devices_manager.update_device(make_device(name="a"))
        devices_manager.update_device(make_device(name="b"))

        result = devices_manager.get_devices()
        assert len(result) == 2
        assert "a" in result
        assert "b" in result

    def test_get_devices_with_scalar_filter(self, devices_manager, make_device):
        d1 = make_device(name="d1", connector_options={"name": "mqtt_1"})
        d2 = make_device(name="d2", connector_options={"name": "mqtt_2"})
        devices_manager.update_device(d1)
        devices_manager.update_device(d2)

        result = devices_manager.get_devices(filters={"name": "d1"})
        assert len(result) == 1
        assert "d1" in result

    def test_get_devices_with_nested_filter(self, devices_manager, make_device):
        d1 = make_device(name="d1", connector_options={"name": "mqtt_1", "protocol": "mqtt"})
        d2 = make_device(name="d2", connector_options={"name": "mqtt_2", "protocol": "lora"})
        devices_manager.update_device(d1)
        devices_manager.update_device(d2)

        result = devices_manager.get_devices(filters={"connector_options": {"protocol": "mqtt"}})
        assert len(result) == 1
        assert "d1" in result

    def test_get_device_returns_deepcopy(self, devices_manager, make_device):
        device = make_device(name="dev1")
        device.data = {"power": 100}
        devices_manager.update_device(device)

        copy = devices_manager.get_device("dev1")
        copy.data["power"] = 999

        original = devices_manager.get_device("dev1")
        assert original.data["power"] == 100

    def test_get_device_not_found(self, devices_manager):
        assert devices_manager.get_device("nonexistent") is None

    def test_remove_device(self, devices_manager, make_device):
        devices_manager.update_device(make_device(name="dev1"))
        devices_manager.remove_device("dev1")

        assert devices_manager.get_device("dev1") is None
        assert len(devices_manager.get_devices()) == 0

    def test_empty_manager(self, devices_manager):
        result = devices_manager.get_devices()
        assert result == {}

    def test_filter_no_match(self, devices_manager, make_device):
        devices_manager.update_device(make_device(name="d1"))
        result = devices_manager.get_devices(filters={"name": "nonexistent"})
        assert result == {}


class TestDevicesManagerControl:
    """F-2: control() routes to the live device by name, not to a copy."""

    def test_control_dispatches_to_live_device(self, devices_manager, make_device, make_connector):
        device = make_device(name="relay1", is_writable=True)
        connector = make_connector()
        connector.send = MagicMock()
        connector.inject_devices({"relay1": device})  # sets device.connector
        devices_manager.update_device(device)

        assert devices_manager.control("relay1", "on") is True

        connector.send.assert_called_once()
        sent_device, command = connector.send.call_args[0]
        assert sent_device is device  # the LIVE device, not a copy
        assert command == "on"

    def test_control_routes_to_live_device_not_snapshot(self, devices_manager, make_device, make_connector):
        device = make_device(name="relay1", is_writable=True, controller_options={"topic": "live/topic"})
        connector = make_connector()
        connector.send = MagicMock()
        connector.inject_devices({"relay1": device})
        devices_manager.update_device(device)

        # An algorithm holds a snapshot and mutates it freely — this must not
        # affect what control() dispatches, because control routes to the live device.
        snapshot = devices_manager.get_devices()["relay1"]
        assert snapshot is not device
        snapshot.controller_options["topic"] = "mutated/topic"

        assert devices_manager.control("relay1", "on") is True

        sent_device = connector.send.call_args[0][0]
        assert sent_device is device
        assert sent_device.controller_options["topic"] == "live/topic"  # live value, not the snapshot mutation

    def test_control_unknown_device_does_not_raise(self, devices_manager):
        # No device registered — logs a warning, no-ops, and answers False so the caller
        # does not record a decision for a device that does not exist.
        assert devices_manager.control("nonexistent", "on") is False
