import threading
from unittest.mock import MagicMock

from tests.conftest import StubDevice


class TestDeviceReadiness:
    def test_initial_state_readable(self, make_device):
        device = make_device(is_readable=True)
        assert not device.is_data_ready()
        assert not device._connected_event.is_set()

    def test_initial_state_non_readable(self, make_device):
        device = make_device(is_readable=False)
        # Non-readable devices have data_ready pre-set
        assert device.is_data_ready()
        assert not device._connected_event.is_set()

    def test_mark_data_ready(self, make_device):
        device = make_device()
        assert not device.is_data_ready()

        device.mark_data_ready()
        assert device.is_data_ready()

    def test_mark_connected(self, make_device):
        device = make_device()
        assert not device._connected_event.is_set()

        device.mark_connected()
        assert device.wait_until_connected(timeout=0) is True

    def test_wait_until_ready_timeout(self, make_device):
        device = make_device()
        result = device.wait_until_ready(timeout=0.01)
        assert result is False

    def test_wait_until_ready_success(self, make_device):
        device = make_device()

        def mark_after_delay():
            device.mark_data_ready()

        t = threading.Thread(target=mark_after_delay)
        t.start()
        result = device.wait_until_ready(timeout=2.0)
        t.join()
        assert result is True

    def test_wait_until_connected_timeout(self, make_device):
        device = make_device()
        result = device.wait_until_connected(timeout=0.01)
        assert result is False

    def test_control_delegates_to_connector(self, make_device, make_connector):
        device = make_device(name="plug", is_writable=True)
        connector = make_connector()
        connector.send = MagicMock()
        device.connector = connector

        assert device.control("on") is True
        connector.send.assert_called_once_with(device, "on")

    def test_control_without_connector(self, make_device):
        device = make_device(name="orphan", is_writable=True)
        # Should not raise: warns, answers False, and writes no decision upstream.
        assert device.control("on") is False

    def test_control_non_writable_never_reaches_connector(self, make_device, make_connector):
        # F-7: Device.control() enforces is_writable — read-only devices
        # warn and no-op instead of firing commands at the connector.
        device = make_device(name="meter", is_writable=False)
        connector = make_connector()
        connector.send = MagicMock()
        device.connector = connector

        assert device.control("on") is False
        connector.send.assert_not_called()


class TestConnectorDeviceInjection:
    def test_inject_sets_back_reference(self, make_device, make_connector):
        device = make_device(name="d1")
        connector = make_connector()
        connector.inject_devices({"d1": device})

        assert device.connector is connector

    def test_on_connected_marks_write_only(self, make_connector):
        readable = StubDevice(name="reader", is_readable=True)
        writable = StubDevice(name="writer", is_readable=False, is_writable=True)

        connector = make_connector()
        connector.inject_devices({"reader": readable, "writer": writable})
        connector.on_connected()

        # Writer (non-readable) should be marked connected
        assert writable._connected_event.is_set()
        # Reader should NOT be marked connected by on_connected
        assert not readable._connected_event.is_set()

    def test_on_device_data_received(self, make_device, make_connector):
        device = make_device()
        connector = make_connector()

        connector.on_device_data_received(device)
        assert device.is_data_ready()
        assert device._connected_event.is_set()

    def test_on_device_data_received_publishes_to_manager(self, make_device, make_connector, devices_manager):
        # The framework — not the device — publishes to DevicesManager. A device
        # whose receive() only mutates self.data (never calling update_device)
        # still reaches algorithms via get_devices(), because the connector hook
        # registers it. This would FAIL under the old device-self-registers design.
        device = make_device(name="fresh")
        device.data = {"x": 1}
        connector = make_connector()

        assert devices_manager.get_device("fresh") is None  # starts unregistered

        connector.on_device_data_received(device)

        stored = devices_manager.get_device("fresh")
        assert stored is not None
        assert stored.data == {"x": 1}
        assert "fresh" in devices_manager.get_devices()


class TestRejectedPayload:
    """A payload a device cannot use is a gap in the record, not a reading.

    The framework used to publish and store after *every* receive(), so an unusable
    payload re-reported the device's previous value under the new timestamp — a stalled
    or corrupt meter looked like a flat line in storage rather than missing data.
    A device says so by returning False from receive().
    """

    def test_rejected_payload_is_not_published(self, make_device, make_connector, devices_manager, storage_manager):
        from tests.conftest import StubStorageBackend

        backend = StubStorageBackend()
        storage_manager.register(backend)
        device = make_device(name="meter")
        device.data = {"power": 1}

        make_connector().on_device_data_received(device, accepted=False)

        assert backend.device_data_calls == []
        assert devices_manager.get_device("meter") is None

    def test_rejected_payload_still_proves_the_transport_is_alive(self, make_device, make_connector):
        # Something arrived, it just was not usable — the connection is up either way.
        device = make_device(name="meter")

        make_connector().on_device_data_received(device, accepted=False)

        assert device._connected_event.is_set()
        assert not device.is_data_ready()

    def test_a_rejection_does_not_unset_earlier_readiness(self, make_device, make_connector):
        device = make_device(name="meter")
        connector = make_connector()

        connector.on_device_data_received(device, accepted=True)
        connector.on_device_data_received(device, accepted=False)

        assert device.is_data_ready()

    def test_accepted_payload_publishes_as_before(self, make_device, make_connector, devices_manager, storage_manager):
        from tests.conftest import StubStorageBackend

        backend = StubStorageBackend()
        storage_manager.register(backend)
        device = make_device(name="meter")
        device.data = {"power": 1}

        make_connector().on_device_data_received(device, accepted=True)

        assert len(backend.device_data_calls) == 1
        assert devices_manager.get_device("meter") is not None

    def test_a_device_that_returns_nothing_is_accepted(self, make_device, make_connector, storage_manager):
        # Backward compatibility: receive() returning None predates this contract.
        from tests.conftest import StubStorageBackend

        backend = StubStorageBackend()
        storage_manager.register(backend)
        device = make_device(name="meter")

        make_connector().on_device_data_received(device, device.receive("payload"))

        assert len(backend.device_data_calls) == 1

    def test_a_connector_that_omits_the_result_is_accepted(self, make_device, make_connector, storage_manager):
        # Backward compatibility: a connector written against the one-argument hook.
        from tests.conftest import StubStorageBackend

        backend = StubStorageBackend()
        storage_manager.register(backend)

        make_connector().on_device_data_received(make_device(name="meter"))

        assert len(backend.device_data_calls) == 1
