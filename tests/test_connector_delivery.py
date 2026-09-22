"""`Connector.deliver()` — the one guarded boundary between a connector and a device plugin.

Its own file, the way `tests/test_supervisor.py` owns the supervisor: this is shared
behaviour every connector inherits, and the per-connector files assert only that each one
routes through it. No importorskip — `api/connector.py` imports nothing outside the stdlib.
"""
import logging
from unittest.mock import MagicMock

import pytest

from tests.conftest import StubConnector, StubDevice


@pytest.fixture
def connector():
    return StubConnector()


@pytest.fixture
def device():
    return StubDevice("meter")


def _boom(device, exception=None):
    device.receive = MagicMock(side_effect=exception or ValueError("bad payload"))
    return device


class TestTheHappyPath:
    def test_the_receive_result_is_forwarded_to_the_framework_hook(self, connector, device):
        device.receive = MagicMock(return_value=False)
        connector.on_device_data_received = MagicMock()
        assert connector.deliver(device, "topic", "payload") is True
        connector.on_device_data_received.assert_called_once_with(device, False)

    @pytest.mark.parametrize("args", [("payload",), ("topic", "payload")])
    def test_both_arities_are_forwarded_verbatim(self, connector, device, args):
        # CONTRIBUTING.md pins the two positional arities because PseudoConnector replays a
        # topic and a payload column out of a CSV. deliver() must not reshape either.
        device.receive = MagicMock(return_value=True)
        connector.deliver(device, *args)
        device.receive.assert_called_once_with(*args)

    def test_a_device_returning_false_is_not_treated_as_a_failure(self, connector, device, caplog):
        # `False` is the documented way to refuse a payload. It must not enter the
        # once-then-quiet set, or the next genuine raise would log as a repeat — and it must
        # not produce a spurious "handling payloads again" line.
        device.receive = MagicMock(return_value=False)
        with caplog.at_level(logging.DEBUG):
            assert connector.deliver(device, "payload") is True
        assert connector._raising_devices == set()
        assert not [r for r in caplog.records if "again" in r.message]


class TestARaisingDevice:
    def test_a_raising_device_does_not_end_the_session(self, connector, device, caplog):
        with caplog.at_level(logging.ERROR):
            assert connector.deliver(_boom(device), "payload") is False  # must not raise
        assert any("raised on a payload" in r.message for r in caplog.records)

    def test_the_routing_key_is_named_but_never_the_payload(self, connector, device, caplog):
        # A DSMR telegram or a get_states snapshot would put kilobytes in the log on every
        # poll of a failing device; the traceback carries the rest.
        with caplog.at_level(logging.ERROR):
            connector.deliver(_boom(device), "sensors/grid", "a" * 5000)
        message = caplog.records[0].message
        assert "'sensors/grid'" in message
        assert "aaaa" not in message

    def test_a_raising_device_is_marked_connected_but_not_data_ready(self, connector, device):
        # A payload arrived, so the transport reaches this device — reporting it as
        # never-connected would point an operator at the network instead of at the code.
        # It produced no reading, so data_ready stays unset and algorithms keep waiting.
        connector.deliver(_boom(device), "payload")
        assert device.is_connected() is True
        assert device.is_data_ready() is False

    def test_the_first_failure_logs_a_traceback_and_the_next_is_quiet(self, connector, device, caplog):
        _boom(device)
        with caplog.at_level(logging.DEBUG):
            connector.deliver(device, "payload")
            connector.deliver(device, "payload")
            connector.deliver(device, "payload")
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        debugs = [r for r in caplog.records if r.levelname == "DEBUG" and "raised again" in r.message]
        assert len(errors) == 1, "a device polled every second would otherwise fill the disk"
        assert len(debugs) == 2
        assert errors[0].exc_info is not None, "the handler for the exception nobody predicted"
        assert debugs[0].exc_info is not None, "DEBUG keeps the traceback; that is what it is for"

    def test_a_device_that_starts_working_again_is_logged_once(self, connector, device, caplog):
        _boom(device)
        connector.deliver(device, "payload")
        device.receive = MagicMock(return_value=True)
        with caplog.at_level(logging.INFO):
            assert connector.deliver(device, "payload") is True
            connector.deliver(device, "payload")
        recovered = [r for r in caplog.records if "handling payloads again" in r.message]
        assert len(recovered) == 1
        assert connector._raising_devices == set()

    def test_the_recovery_wording_does_not_collide_with_transport_recovery(self, connector, device, caplog):
        # "Device 'x' recovered" belongs to http_api and modbus_tcp, for a transport that came
        # back, and their tests assert on it. This one means something else.
        _boom(device)
        connector.deliver(device, "payload")
        device.receive = MagicMock(return_value=True)
        with caplog.at_level(logging.INFO):
            connector.deliver(device, "payload")
        assert not any("recovered" in r.message for r in caplog.records)

    def test_a_hook_failure_does_not_escape(self, connector, device, caplog):
        # The hook is inside the guard because it is a second place a device's own state can
        # bite: it publishes `device.data` to DevicesManager and hands it to every storage
        # backend, so a device that parked something unserialisable there raises one frame
        # later than receive() did. The except branch then re-enters the hook with
        # accepted=False, which marks connected and returns before either of those.
        connector.on_device_data_received = MagicMock(side_effect=[TypeError("unserialisable"), None])
        with caplog.at_level(logging.ERROR):
            assert connector.deliver(device, "payload") is False  # must not raise
        # Called once, not twice: the except branch marks the device connected directly
        # rather than re-entering a hook that has just proved it can raise.
        assert connector.on_device_data_received.call_count == 1
        assert device.is_connected() is True

    def test_a_hook_that_always_fails_still_does_not_escape(self, connector, device, caplog):
        # The case that made the first version of this guard wrong: it re-entered the hook in
        # the except branch, so a hook that raises every time escaped `deliver()` one frame
        # from the end and defeated the whole guard. Nothing on the failure path may call it.
        connector.on_device_data_received = MagicMock(side_effect=TypeError("always"))
        with caplog.at_level(logging.ERROR):
            assert connector.deliver(device, "payload") is False  # must not raise

    def test_keyboard_interrupt_is_not_swallowed(self, connector, device):
        # `except Exception`, never a bare `except`: a shutdown signal is not a device bug.
        with pytest.raises(KeyboardInterrupt):
            connector.deliver(_boom(device, KeyboardInterrupt()), "payload")

    def test_two_devices_are_tracked_independently(self, connector, caplog):
        broken, fine = StubDevice("broken"), StubDevice("fine")
        _boom(broken)
        fine.receive = MagicMock(return_value=True)
        with caplog.at_level(logging.ERROR):
            assert connector.deliver(broken, "payload") is False
            assert connector.deliver(fine, "payload") is True
        assert connector._raising_devices == {"broken"}
