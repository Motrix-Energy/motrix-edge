"""Modbus TCP connector: option coercion, read planning, failure isolation, control."""
import json
import logging
from unittest.mock import MagicMock, patch

import pytest

# pymodbus is not in requirements.txt on purpose (see requirements-modbus.txt). A hard
# import here aborts collection for the WHOLE suite, not just this file — which is exactly
# what tests/test_storage_influxdb.py did before it was guarded.
pytest.importorskip("pymodbus", reason="pip install -r requirements-modbus.txt")

from pymodbus import ModbusException

from connectors.modbus_tcp import ModbusTcpConnector
from tests.conftest import StubDevice, assert_stops, run_in_thread, wait_until


def make_connector(**kwargs) -> ModbusTcpConnector:
    defaults = dict(name="MB 1", host="10.0.0.5")
    defaults.update(kwargs)
    return ModbusTcpConnector(**defaults)


def make_device(name: str = "meter", registers=None, **listener) -> StubDevice:
    options = {"registers": registers if registers is not None else [{"name": "v", "address": 20}], **listener}
    return StubDevice(name=name, listener_options=options)


def _ok(registers=None, bits=None) -> MagicMock:
    """A successful pymodbus response. isError() is a *method*, not an exception."""
    result = MagicMock()
    result.isError.return_value = False
    result.registers = registers if registers is not None else []
    result.bits = bits if bits is not None else []
    return result


def _modbus_error() -> MagicMock:
    """A Modbus *exception response*: the meter answered, and said no."""
    result = MagicMock()
    result.isError.return_value = True
    return result


class TestOptions:
    def test_numeric_options_arriving_as_strings_are_coerced(self):
        """Config validates the plugin schema BEFORE resolving ${VAR}, so a "${MODBUS_PORT}"
        declared `integer` reaches the constructor as a str."""
        connector = make_connector(port="1502", device_id="3", timeout="1.5", default_interval="7")
        assert connector.port == 1502
        assert connector.device_id == 3
        assert connector.timeout == 1.5
        assert connector.default_interval == 7.0

    def test_unresolved_env_reference_falls_back_to_the_default(self, caplog):
        with caplog.at_level(logging.WARNING):
            connector = make_connector(port="${MODBUS_PORT}")
        assert connector.port == 502
        assert any("is not an integer" in r.message for r in caplog.records)

    def test_retries_defaults_to_one_not_pymodbus_three(self):
        """timeout x retries is the worst-case block against a 10s shutdown grace, and for
        a poller the next poll IS the retry."""
        assert make_connector().retries == 1

    def test_zero_reconnect_backoff_is_refused(self, caplog):
        """min(delay * 2, max) with delay == 0.0 is 0.0 forever: a hot reconnect loop."""
        with caplog.at_level(logging.WARNING):
            connector = make_connector(reconnect_backoff_seconds=0)
        assert connector.reconnect_backoff_seconds == 1.0

    def test_zero_poll_interval_is_refused(self):
        assert make_connector(default_interval=0).default_interval == 10.0

    def test_device_id_255_is_allowed(self):
        """255 is the Modbus-TCP 'unit id not used' convention, so the cap is not 247."""
        assert make_connector(device_id=255).device_id == 255

    def test_no_client_is_built_in_the_constructor(self):
        """A stop() before start() must have nothing to tear down, and a constructor that
        can raise escapes create_classes, which catches only three exception types."""
        assert make_connector()._client is None


class TestReadPlan:
    def test_one_read_per_declared_register(self):
        connector = make_connector(default_interval=30)
        device = make_device(registers=[
            {"name": "energy", "address": 0, "data_type": "UINT32"},
            {"name": "relay", "address": 8, "type": "coil"},
        ])
        connector.inject_devices({"meter": device})

        assert len(connector._poll_tasks) == 1
        task = connector._poll_tasks[0]
        assert task.interval == 30
        assert [(r.name, r.reader, r.address, r.count) for r in task.reads] == [
            ("energy", "read_holding_registers", 0, 2),
            ("relay", "read_coils", 8, 1),
        ]

    def test_count_defaults_from_data_type(self):
        connector = make_connector()
        device = make_device(registers=[
            {"name": "a", "address": 0, "data_type": "UINT16"},
            {"name": "b", "address": 1, "data_type": "FLOAT32"},
            {"name": "c", "address": 3, "data_type": "INT64"},
        ])
        connector.inject_devices({"meter": device})
        assert [r.count for r in connector._poll_tasks[0].reads] == [1, 2, 4]

    def test_explicit_count_wins(self):
        connector = make_connector()
        device = make_device(registers=[{"name": "serial", "address": 90, "data_type": "STRING", "count": 8}])
        connector.inject_devices({"meter": device})
        assert connector._poll_tasks[0].reads[0].count == 8

    def test_each_of_the_four_tables_maps_to_its_own_reader(self):
        connector = make_connector()
        device = make_device(registers=[
            {"name": "h", "address": 0, "type": "holding"},
            {"name": "i", "address": 1, "type": "input"},
            {"name": "c", "address": 2, "type": "coil"},
            {"name": "d", "address": 3, "type": "discrete"},
        ])
        connector.inject_devices({"meter": device})
        assert [r.reader for r in connector._poll_tasks[0].reads] == [
            "read_holding_registers", "read_input_registers", "read_coils", "read_discrete_inputs",
        ]

    def test_unit_id_resolves_register_then_device_then_connector(self):
        connector = make_connector(device_id=1)
        device = make_device(device_id=7, registers=[
            {"name": "a", "address": 0},
            {"name": "b", "address": 1, "device_id": 9},
        ])
        connector.inject_devices({"meter": device})
        assert [r.device_id for r in connector._poll_tasks[0].reads] == [7, 9]

    def test_write_only_device_is_excluded(self):
        connector = make_connector()
        device = StubDevice(name="relay", is_readable=False, is_writable=True,
                            listener_options={"registers": [{"name": "x", "address": 0}]})
        connector.inject_devices({"relay": device})
        assert connector._poll_tasks == []

    def test_device_without_registers_warns_and_is_skipped(self, caplog):
        connector = make_connector()
        with caplog.at_level(logging.WARNING):
            connector.inject_devices({"meter": StubDevice(name="meter")})
        assert any("has no listener_options.registers" in r.message for r in caplog.records)
        assert connector._poll_tasks == []

    def test_unknown_table_is_skipped_with_a_warning(self, caplog):
        connector = make_connector()
        device = make_device(registers=[{"name": "x", "address": 0, "type": "wishful"}])
        with caplog.at_level(logging.WARNING):
            connector.inject_devices({"meter": device})
        assert any("unknown type 'wishful'" in r.message for r in caplog.records)
        assert connector._poll_tasks == []

    def test_missing_address_is_skipped_with_a_warning(self, caplog):
        connector = make_connector()
        device = make_device(registers=[{"name": "x"}, {"name": "y", "address": 3}])
        with caplog.at_level(logging.WARNING):
            connector.inject_devices({"meter": device})
        assert any("no usable address" in r.message for r in caplog.records)
        assert [r.name for r in connector._poll_tasks[0].reads] == ["y"]

    def test_duplicate_register_name_is_refused(self, caplog):
        """`blocks` is a dict keyed by name, so a duplicate would silently drop one read."""
        connector = make_connector()
        device = make_device(registers=[{"name": "x", "address": 0}, {"name": "x", "address": 4}])
        with caplog.at_level(logging.WARNING):
            connector.inject_devices({"meter": device})
        assert any("Duplicate register name" in r.message for r in caplog.records)
        assert len(connector._poll_tasks[0].reads) == 1

    def test_count_is_capped_at_the_pdu_limit(self, caplog):
        connector = make_connector()
        device = make_device(registers=[{"name": "x", "address": 0, "count": 500}])
        with caplog.at_level(logging.WARNING):
            connector.inject_devices({"meter": device})
        assert connector._poll_tasks[0].reads[0].count == 1, "above the cap falls back to the default"


class TestPollDevice:
    def _poll(self, connector, device):
        connector.inject_devices({device.name: device})
        connector._poll_device(connector._poll_tasks[0])

    def test_one_receive_call_carrying_every_block_as_json(self):
        connector = make_connector()
        connector._client = MagicMock()
        connector._client.read_holding_registers.return_value = _ok(registers=[0, 1234])
        connector._client.read_coils.return_value = _ok(bits=[True, False, False, False, False, False, False, False])
        device = make_device(registers=[
            {"name": "energy", "address": 0, "data_type": "UINT32"},
            {"name": "relay", "address": 8, "type": "coil"},
        ])
        device.receive = MagicMock(return_value=True)

        self._poll(connector, device)

        device.receive.assert_called_once()
        payload = device.receive.call_args.args[0]
        assert isinstance(payload, str), "a JSON string, so live and replay paths are identical"
        assert json.loads(payload) == {"blocks": {"energy": [0, 1234], "relay": [True]}}

    def test_bits_are_sliced_to_count(self):
        """read_coils pads to a byte boundary, so `bits` is routinely longer than asked."""
        connector = make_connector()
        connector._client = MagicMock()
        connector._client.read_coils.return_value = _ok(bits=[True] * 8)
        device = make_device(registers=[{"name": "relay", "address": 8, "type": "coil", "count": 2}])
        device.receive = MagicMock(return_value=True)

        self._poll(connector, device)

        assert json.loads(device.receive.call_args.args[0])["blocks"]["relay"] == [True, True]

    def test_the_read_is_issued_with_count_and_device_id(self):
        connector = make_connector(device_id=4)
        connector._client = MagicMock()
        connector._client.read_input_registers.return_value = _ok(registers=[1])
        device = make_device(registers=[{"name": "v", "address": 20, "type": "input"}])

        self._poll(connector, device)

        connector._client.read_input_registers.assert_called_once_with(20, count=1, device_id=4)

    def test_receive_result_is_forwarded_to_the_framework_hook(self):
        connector = make_connector()
        connector._client = MagicMock()
        connector._client.read_holding_registers.return_value = _ok(registers=[1])
        connector.on_device_data_received = MagicMock()
        device = make_device(registers=[{"name": "v", "address": 0}])
        device.receive = MagicMock(return_value=False)

        self._poll(connector, device)

        connector.on_device_data_received.assert_called_once_with(device, False)

    def test_modbus_exception_response_warns_once_and_keeps_going(self, caplog):
        connector = make_connector()
        connector._client = MagicMock()
        connector._client.read_holding_registers.side_effect = [_modbus_error(), _ok(registers=[7])]
        device = make_device(registers=[
            {"name": "missing", "address": 0},
            {"name": "present", "address": 1},
        ])
        device.receive = MagicMock(return_value=True)

        with caplog.at_level(logging.WARNING):
            self._poll(connector, device)

        assert any("Modbus exception reading 'meter.missing'" in r.message for r in caplog.records)
        assert json.loads(device.receive.call_args.args[0])["blocks"] == {"present": [7]}

    def test_a_failed_register_recovers_with_one_line(self, caplog):
        connector = make_connector()
        connector._client = MagicMock()
        device = make_device(registers=[{"name": "v", "address": 0}])
        connector.inject_devices({"meter": device})

        connector._client.read_holding_registers.return_value = _modbus_error()
        connector._poll_device(connector._poll_tasks[0])
        connector._client.read_holding_registers.return_value = _ok(registers=[1])
        with caplog.at_level(logging.INFO):
            connector._poll_device(connector._poll_tasks[0])

        assert any("Register 'meter.v' recovered" in r.message for r in caplog.records)

    def test_transport_error_drops_the_socket_and_stops_reading(self, caplog):
        connector = make_connector()
        connector._client = MagicMock()
        connector._client.read_holding_registers.side_effect = ModbusException("gone")
        device = make_device(registers=[{"name": "a", "address": 0}, {"name": "b", "address": 1}])
        device.receive = MagicMock()

        with caplog.at_level(logging.ERROR):
            self._poll(connector, device)

        assert "meter" in connector._failed_devices
        assert connector._client.read_holding_registers.call_count == 1, "the socket is gone; do not retry the rest"
        connector._client.close.assert_called_once()
        device.receive.assert_not_called()

    def test_close_on_error_false_keeps_the_socket(self):
        connector = make_connector(close_on_error=False)
        connector._client = MagicMock()
        connector._client.read_holding_registers.side_effect = OSError("reset")
        self._poll(connector, make_device())
        connector._client.close.assert_not_called()

    def test_every_register_failing_publishes_nothing(self):
        """Nothing arrived that is a reading, so the device must not be marked data-ready."""
        connector = make_connector()
        connector._client = MagicMock()
        connector._client.read_holding_registers.return_value = _modbus_error()
        connector.on_device_data_received = MagicMock()
        device = make_device(registers=[{"name": "v", "address": 0}])
        device.receive = MagicMock()

        self._poll(connector, device)

        device.receive.assert_not_called()
        connector.on_device_data_received.assert_not_called()

    def test_device_recovery_is_logged_once(self, caplog):
        connector = make_connector()
        connector._client = MagicMock()
        connector._client.read_holding_registers.return_value = _ok(registers=[1])
        device = make_device()
        connector.inject_devices({"meter": device})
        connector._failed_devices.add("meter")

        with caplog.at_level(logging.INFO):
            connector._poll_device(connector._poll_tasks[0])

        assert "meter" not in connector._failed_devices
        assert any("Device 'meter' recovered" in r.message for r in caplog.records)

    def test_no_client_is_a_uniform_not_connected_answer(self):
        connector = make_connector()
        device = make_device()
        device.receive = MagicMock()
        self._poll(connector, device)
        device.receive.assert_not_called()


class TestSend:
    def _writable(self, **controller) -> StubDevice:
        return StubDevice(name="relay", is_writable=True, controller_options=controller)

    def test_coil_on_and_off(self):
        connector = make_connector(device_id=2)
        connector._client = MagicMock()
        connector._client.write_coil.return_value = _ok()
        device = self._writable(kind="coil", address=8)

        connector.send(device, "on")
        connector.send(device, "OFF")

        assert connector._client.write_coil.call_args_list[0].args == (8, True)
        assert connector._client.write_coil.call_args_list[1].args == (8, False)
        assert connector._client.write_coil.call_args_list[0].kwargs == {"device_id": 2}

    def test_unknown_coil_command_warns_and_writes_nothing(self, caplog):
        connector = make_connector()
        connector._client = MagicMock()
        with caplog.at_level(logging.WARNING):
            connector.send(self._writable(kind="coil", address=8), "boost")
        assert any("Unknown coil command" in r.message for r in caplog.records)
        connector._client.write_coil.assert_not_called()

    def test_register_write_uses_on_value_and_scale(self):
        connector = make_connector()
        connector._client = MagicMock()
        connector._client.write_register.return_value = _ok()
        device = self._writable(kind="register", address=40, on_value=3000, scale=10)

        connector.send(device, "on")

        connector._client.write_register.assert_called_once_with(40, 300, device_id=1)

    def test_register_write_accepts_a_numeric_command(self):
        connector = make_connector()
        connector._client = MagicMock()
        connector._client.write_register.return_value = _ok()
        connector.send(self._writable(kind="register", address=40), "1234")
        connector._client.write_register.assert_called_once_with(40, 1234, device_id=1)

    def test_out_of_range_register_value_is_refused(self, caplog):
        connector = make_connector()
        connector._client = MagicMock()
        with caplog.at_level(logging.WARNING):
            connector.send(self._writable(kind="register", address=40), "70000")
        assert any("outside 0..65535" in r.message for r in caplog.records)
        connector._client.write_register.assert_not_called()

    def test_signed_registers_accept_negative_values(self):
        connector = make_connector()
        connector._client = MagicMock()
        connector._client.write_register.return_value = _ok()
        connector.send(self._writable(kind="register", address=40, signed=True), "-3000")
        connector._client.write_register.assert_called_once_with(40, -3000, device_id=1)

    def test_non_numeric_register_command_is_refused(self, caplog):
        connector = make_connector()
        connector._client = MagicMock()
        with caplog.at_level(logging.WARNING):
            connector.send(self._writable(kind="register", address=40), "MANUAL_ON")
        assert any("is not a number" in r.message for r in caplog.records)

    def test_controller_device_id_overrides(self):
        connector = make_connector(device_id=1)
        connector._client = MagicMock()
        connector._client.write_coil.return_value = _ok()
        connector.send(self._writable(kind="coil", address=8, device_id=17), "on")
        assert connector._client.write_coil.call_args.kwargs == {"device_id": 17}

    def test_missing_address_warns_and_writes_nothing(self, caplog):
        connector = make_connector()
        connector._client = MagicMock()
        with caplog.at_level(logging.WARNING):
            connector.send(self._writable(kind="coil"), "on")
        assert any("no usable controller_options.address" in r.message for r in caplog.records)
        connector._client.write_coil.assert_not_called()

    def test_unknown_kind_warns(self, caplog):
        connector = make_connector()
        connector._client = MagicMock()
        with caplog.at_level(logging.WARNING):
            connector.send(self._writable(kind="telepathy", address=1), "on")
        assert any("Unknown controller_options.kind" in r.message for r in caplog.records)

    def test_transport_error_is_swallowed_not_raised(self, caplog):
        """send() runs on the ALGORITHM's thread and nothing in that chain catches: a raise
        would crash the algorithm's supervised worker because a relay did not answer."""
        connector = make_connector()
        connector._client = MagicMock()
        connector._client.write_coil.side_effect = ModbusException("no route")
        with caplog.at_level(logging.ERROR):
            connector.send(self._writable(kind="coil", address=8), "on")  # must not raise
        assert any("Error sending 'on'" in r.message for r in caplog.records)

    def test_not_connected_drops_the_command_with_a_warning(self, caplog):
        connector = make_connector()
        with caplog.at_level(logging.WARNING):
            connector.send(self._writable(kind="coil", address=8), "on")
        assert any("Not connected" in r.message for r in caplog.records)

    def test_modbus_exception_response_warns(self, caplog):
        connector = make_connector()
        connector._client = MagicMock()
        connector._client.write_coil.return_value = _modbus_error()
        with caplog.at_level(logging.WARNING):
            connector.send(self._writable(kind="coil", address=8), "on")
        assert any("Modbus exception writing" in r.message for r in caplog.records)

    def test_multi_register_writes_are_reported_as_unimplemented(self, caplog):
        connector = make_connector()
        connector._client = MagicMock()
        connector._client.write_register.return_value = _ok()
        with caplog.at_level(logging.WARNING):
            connector.send(self._writable(kind="register", address=40, count=2), "5")
        assert any("Multi-register writes are not implemented" in r.message for r in caplog.records)


class TestStart:
    def test_no_host_returns_idle(self, caplog):
        connector = make_connector(host="")
        with caplog.at_level(logging.ERROR):
            connector.start()  # must return, not loop
        assert any("No host configured" in r.message for r in caplog.records)

    def test_no_poll_tasks_returns_idle(self, caplog):
        connector = make_connector()
        connector.inject_devices({})
        with caplog.at_level(logging.WARNING):
            connector.start()
        assert any("connector idle" in r.message for r in caplog.records)

    def test_stop_unblocks_a_running_poll_loop(self):
        connector = make_connector(default_interval=60)
        device = make_device()
        connector.inject_devices({"meter": device})
        client = MagicMock()
        client.connected = True
        client.read_holding_registers.return_value = _ok(registers=[1])

        with patch("connectors.modbus_tcp.ModbusTcpClient", return_value=client):
            thread = run_in_thread(connector.start)
            assert wait_until(lambda: device.is_data_ready())
            assert_stops(connector, thread)
        client.close.assert_called()

    def test_an_unreachable_meter_backs_off_instead_of_raising(self, caplog):
        """Raising would burn the supervisor's restart budget, and past the cap the worker
        is finished — which makes main shut the whole EMS down over a rebooting meter."""
        connector = make_connector(reconnect_backoff_seconds=0.1)
        connector.inject_devices({"meter": make_device()})
        client = MagicMock()
        client.connected = False
        client.connect.return_value = False

        with patch("connectors.modbus_tcp.ModbusTcpClient", return_value=client):
            with caplog.at_level(logging.ERROR):
                thread = run_in_thread(connector.start)
                assert wait_until(lambda: client.connect.call_count >= 2)
                assert_stops(connector, thread)

        assert any("Cannot reach" in r.message for r in caplog.records), "the first failure must be visible at the default level"

    def test_on_connected_fires_once_when_the_socket_comes_up(self):
        connector = make_connector()
        write_only = StubDevice(name="relay", is_readable=False, is_writable=True)
        connector.inject_devices({"meter": make_device(), "relay": write_only})
        client = MagicMock()
        client.connected = True
        client.read_holding_registers.return_value = _ok(registers=[1])

        with patch("connectors.modbus_tcp.ModbusTcpClient", return_value=client):
            thread = run_in_thread(connector.start)
            assert wait_until(lambda: write_only.is_connected())
            assert_stops(connector, thread)
