"""Point-to-point LoRa connector: dialects, routing off one stream, and the read-loop stop.

pyserial is not in requirements.txt on purpose (see requirements-lora.txt). A hard import here
aborts collection for the WHOLE suite, not just this file — which is why connectors/lora.py is
deliberately absent from tests/test_shutdown.py's module-top imports.
"""
import json
import logging
import threading
from unittest.mock import MagicMock, patch

import pytest

# The submodule rather than the package, and that is not pedantry: a half-removed pyserial
# leaves a `serial/` directory with no __init__.py, which Python happily imports as an empty
# NAMESPACE package. importorskip("serial") then succeeds, the next line raises ImportError,
# and collection aborts for the WHOLE suite — the exact outcome this guard exists to prevent.
# serial.serialutil only resolves when the package is genuinely there.
pytest.importorskip("serial.serialutil", reason="pip install -r requirements-lora.txt")

from serial import SerialException

from connectors.lora import DIALECTS, NATIVE_ENVELOPE_KEYS, LoRaConnector
from devices.lora import PROFILES as DEVICE_PROFILES
from tests.conftest import StubDevice, assert_stops, run_in_thread, wait_until

# hex 00 01 86 A0 01 2C 81 — the same seven bytes tests/test_lora_device.py decodes.
PAYLOAD_HEX = "000186A0012C81"
PAYLOAD_B64 = "AAGGoAEsgQ=="


class FakePort:
    """A scripted serial port: hands out lines in order, then goes quiet.

    Going quiet means blocking for `timeout` and returning b"", which is exactly what pyserial
    does on a read timeout — and modelling that faithfully is what makes the stop() test real.
    The connector must be sitting inside readline() when stop() arrives, and it is the bounded
    return, not a close() from another thread, that lets it notice. A fake that blocked
    indefinitely would pass a connector that can never be shut down.
    """

    def __init__(self, lines=(), block_when_empty: bool = True, timeout: float = 0.02):
        self.lines = list(lines)
        self.written: list[bytes] = []
        self.closed = False
        self.is_open = True
        self.timeout = timeout
        self._block_when_empty = block_when_empty
        self._released = threading.Event()

    def readline(self) -> bytes:
        while self.lines:
            line = self.lines.pop(0)
            if isinstance(line, Exception):
                raise line
            return line.encode() if isinstance(line, str) else line
        if self._block_when_empty:
            self._released.wait(self.timeout)
        return b""

    def write(self, payload: bytes) -> int:
        self.written.append(payload)
        return len(payload)

    def close(self) -> None:
        self.closed = True
        self.is_open = False
        self._released.set()

    def commands(self) -> list[str]:
        return [entry.decode().strip() for entry in self.written]


def make_connector(**kwargs) -> LoRaConnector:
    defaults = dict(name="LoRa 1", port="/dev/ttyUSB0", dialect="rak", read_timeout=0.05)
    defaults.update(kwargs)
    return LoRaConnector(**defaults)


def make_device(name: str = "node", **listener) -> StubDevice:
    return StubDevice(name=name, listener_options=listener)


def run_session(connector, port, until):
    """Drive start() against a fake port until `until` holds, then stop it."""
    with patch("connectors.lora.serial.Serial", return_value=port):
        thread = run_in_thread(connector.start)
        held = wait_until(until)
        assert_stops(connector, thread)
    return held


class TestConstruction:
    def test_unknown_dialect_falls_back_to_custom(self, caplog):
        with caplog.at_level(logging.WARNING):
            connector = make_connector(dialect="meshtastic")
        assert connector.dialect == "custom"
        assert any("Unknown dialect" in r.message for r in caplog.records)

    def test_an_explicit_pattern_overrides_the_dialect(self):
        """A firmware revision that moved a field costs one config line, not a release."""
        connector = make_connector(dialect="rak", receive_pattern=r"RX (?P<data>[0-9A-Fa-f]+)")
        assert connector._receive_pattern.pattern == r"RX (?P<data>[0-9A-Fa-f]+)"

    def test_a_pattern_without_a_data_group_is_refused(self, caplog):
        """It could never carry a payload, so it would route frames that decode to nothing."""
        with caplog.at_level(logging.ERROR):
            connector = make_connector(receive_pattern=r"RX (?P<port>\d+)")
        assert connector._receive_pattern is None
        assert any("captures no (?P<data>" in r.message for r in caplog.records)

    def test_an_invalid_regex_is_logged_not_raised(self, caplog):
        with caplog.at_level(logging.ERROR):
            connector = make_connector(receive_pattern=r"(?P<data>[")
        assert connector._receive_pattern is None
        assert any("Invalid receive_pattern" in r.message for r in caplog.records)

    def test_string_numeric_options_are_coerced(self):
        """Config validates the plugin schema BEFORE resolving ${VAR}, so a "${LORA_BAUD}"
        declared `integer` reaches the constructor as a str."""
        connector = make_connector(baudrate="115200", read_timeout="0.5")
        assert connector.baudrate == 115200 and connector.read_timeout == 0.5

    def test_unknown_encoding_falls_back_to_hex(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert make_connector(encoding="ascii").encoding == "hex"
        assert any("Unknown encoding" in r.message for r in caplog.records)

    def test_escaped_newlines_from_json_are_unescaped(self):
        """config.json can carry a literal backslash-r-backslash-n."""
        assert make_connector(newline="\\r\\n").newline == "\r\n"


class TestDialects:
    def _frame(self, dialect: str, line: str) -> dict:
        connector = make_connector(dialect=dialect)
        device = make_device()
        device.receive = MagicMock(return_value=True)
        connector.inject_devices({"node": device})
        connector._dispatch(line)
        device.receive.assert_called_once()
        return json.loads(device.receive.call_args.args[0])

    def test_rak(self):
        frame = self._frame("rak", f"+EVT:RX_1:-53:8:UNICAST:2:{PAYLOAD_HEX}")
        assert frame == {"data": PAYLOAD_B64, "f_port": 2, "rssi": -53.0, "snr": 8.0, "address": None}

    def test_rn2483(self):
        frame = self._frame("rn2483", f"mac_rx 5 {PAYLOAD_HEX}")
        assert frame["data"] == PAYLOAD_B64 and frame["f_port"] == 5

    def test_lora_e5(self):
        frame = self._frame("lora_e5", f'+MSG: PORT: 8; RX: "{PAYLOAD_HEX}"')
        assert frame["data"] == PAYLOAD_B64 and frame["f_port"] == 8

    def test_lora_e5_test_mode(self):
        frame = self._frame("lora_e5", f'+TEST: RX "{PAYLOAD_HEX}"')
        assert frame["data"] == PAYLOAD_B64

    def test_a_custom_dialect_reaches_a_module_nobody_here_has_heard_of(self):
        connector = make_connector(
            dialect="custom",
            receive_pattern=r"^\$RX,(?P<address>[0-9A-Fa-f]+),(?P<data>[0-9A-Fa-f]+)$",
        )
        device = make_device(address="beef")
        device.receive = MagicMock(return_value=True)
        connector.inject_devices({"node": device})
        connector._dispatch(f"$RX,BEEF,{PAYLOAD_HEX}")
        frame = json.loads(device.receive.call_args.args[0])
        assert frame["address"] == "beef" and frame["data"] == PAYLOAD_B64

    def test_every_shipped_dialect_names_a_data_group(self):
        for name, dialect in DIALECTS.items():
            if dialect.receive_pattern:
                assert "(?P<data>" in dialect.receive_pattern, f"dialect '{name}' captures no data"

    def test_every_shipped_send_template_carries_the_payload(self):
        for name, dialect in DIALECTS.items():
            if dialect.send_template:
                assert "{data}" in dialect.send_template, f"dialect '{name}' sends no payload"


class TestNativeEnvelope:
    """The joint between this connector and devices/lora.py, which may not import each other."""

    def test_the_device_reads_every_key_this_connector_writes(self):
        envelope = DEVICE_PROFILES["native"]
        declared = {envelope.payload, envelope.f_port, envelope.rssi, envelope.snr, envelope.dev_eui}
        assert declared - {None} <= NATIVE_ENVELOPE_KEYS

    def test_a_frame_carries_exactly_the_declared_keys(self):
        connector = make_connector()
        device = make_device()
        device.receive = MagicMock(return_value=True)
        connector.inject_devices({"node": device})
        connector._dispatch(f"+EVT:RX_1:-53:8:UNICAST:2:{PAYLOAD_HEX}")
        assert set(json.loads(device.receive.call_args.args[0])) == set(NATIVE_ENVELOPE_KEYS)

    def test_the_frame_decodes_through_the_real_device(self):
        """End to end with no radio: this connector's output is that device's input."""
        from devices.lora import LoRa
        node = LoRa("node", {"name": "radio", "protocol": "lora"},
                    {"profile": "native", "fields": [
                        {"name": "energy_kwh", "offset": 0, "data_type": "UINT32", "scale": 0.01,
                         "role": "energy_import_kwh"},
                    ]}, {})
        connector = make_connector()
        connector.inject_devices({"node": node})
        connector._dispatch(f"+EVT:RX_1:-53:8:UNICAST:2:{PAYLOAD_HEX}")
        assert node.get_total_energy_kwh() == 1000.0
        assert node.data["uplink"]["rssi"] == -53.0


class TestRouting:
    def _connector_with(self, *devices) -> LoRaConnector:
        connector = make_connector()
        for device in devices:
            device.receive = MagicMock(return_value=True)
        connector.inject_devices({device.name: device for device in devices})
        return connector

    def test_a_single_unfiltered_device_receives_everything(self):
        device = make_device()
        connector = self._connector_with(device)
        connector._dispatch(f"+EVT:RX_1:-53:8:UNICAST:2:{PAYLOAD_HEX}")
        device.receive.assert_called_once()

    def test_f_port_selects_between_devices(self):
        periodic, status = make_device("periodic", f_port=1), make_device("status", f_port=2)
        connector = self._connector_with(periodic, status)
        connector._dispatch(f"+EVT:RX_1:-53:8:UNICAST:2:{PAYLOAD_HEX}")
        status.receive.assert_called_once()
        periodic.receive.assert_not_called()

    def test_an_address_group_selects_between_devices(self):
        mine = make_device("mine", address="beef")
        theirs = make_device("theirs", address="cafe")
        connector = make_connector(dialect="custom", receive_pattern=r"^\$RX,(?P<address>[0-9A-Fa-f]+),(?P<data>[0-9A-Fa-f]+)$")
        for device in (mine, theirs):
            device.receive = MagicMock(return_value=True)
        connector.inject_devices({"mine": mine, "theirs": theirs})
        connector._dispatch(f"$RX,BEEF,{PAYLOAD_HEX}")
        mine.receive.assert_called_once()
        theirs.receive.assert_not_called()

    def test_an_address_falls_back_to_the_leading_payload_bytes(self):
        """A raw point-to-point link has no MAC addressing at all, so who sent a frame can
        only be inside the frame."""
        device = make_device(address="0001")
        connector = self._connector_with(device)
        connector._dispatch(f"+EVT:RX_1:-53:8:UNICAST:2:{PAYLOAD_HEX}")  # payload starts 0001
        device.receive.assert_called_once()

    def test_a_non_matching_leading_address_is_skipped(self):
        device = make_device(address="ffff")
        connector = self._connector_with(device)
        connector._dispatch(f"+EVT:RX_1:-53:8:UNICAST:2:{PAYLOAD_HEX}")
        device.receive.assert_not_called()

    def test_a_declared_address_tolerates_datasheet_separators(self):
        device = make_device(address="00-01")
        connector = self._connector_with(device)
        connector._dispatch(f"+EVT:RX_1:-53:8:UNICAST:2:{PAYLOAD_HEX}")
        device.receive.assert_called_once()

    def test_several_unfiltered_devices_warn(self, caplog):
        with caplog.at_level(logging.WARNING):
            self._connector_with(make_device("a"), make_device("b"))
        assert any("every frame will be delivered to all of them" in r.message for r in caplog.records)

    def test_one_unfiltered_device_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING):
            self._connector_with(make_device("only"))
        assert not any("every frame" in r.message for r in caplog.records)

    def test_write_only_devices_are_excluded(self):
        connector = make_connector()
        connector.inject_devices({"relay": StubDevice(name="relay", is_readable=False, is_writable=True)})
        assert connector._routes == []

    def test_the_receive_result_is_forwarded_to_the_framework_hook(self):
        device = make_device()
        connector = self._connector_with(device)
        device.receive = MagicMock(return_value=False)
        connector.on_device_data_received = MagicMock()
        connector._dispatch(f"+EVT:RX_1:-53:8:UNICAST:2:{PAYLOAD_HEX}")
        connector.on_device_data_received.assert_called_once_with(device, False)

    def test_a_raising_device_does_not_end_the_session(self, caplog):
        boom, fine = make_device("boom"), make_device("fine")
        connector = self._connector_with(boom, fine)
        boom.receive = MagicMock(side_effect=ValueError("bad payload"))
        with caplog.at_level(logging.ERROR):
            connector._dispatch(f"+EVT:RX_1:-53:8:UNICAST:2:{PAYLOAD_HEX}")  # must not raise
        fine.receive.assert_called_once()
        assert any("failed on a frame" in r.message for r in caplog.records)

    def test_an_unmatched_line_is_not_an_error(self, caplog):
        """A module echoes commands, answers OK, and emits unsolicited status of its own."""
        device = make_device()
        connector = self._connector_with(device)
        with caplog.at_level(logging.WARNING):
            connector._dispatch("OK")
            connector._dispatch("+EVT:JOINED")
        device.receive.assert_not_called()
        assert not caplog.records

    def test_an_undecodable_payload_warns_and_routes_nothing(self, caplog):
        device = make_device()
        connector = self._connector_with(device)
        with caplog.at_level(logging.WARNING):
            connector._dispatch("+EVT:RX_1:-53:8:UNICAST:2:0A1")  # odd-length hex
        device.receive.assert_not_called()
        assert any("Undecodable hex payload" in r.message for r in caplog.records)


class TestSend:
    def _ready(self, **controller):
        connector = make_connector()
        port = FakePort(block_when_empty=False)
        connector._serial = port
        device = StubDevice(name="relay", is_writable=True, controller_options=controller)
        return connector, port, device

    def test_on_and_off_default_to_01_and_00(self):
        connector, port, device = self._ready(f_port=10)
        connector.send(device, "on")
        connector.send(device, "off")
        assert port.commands() == ["AT+SEND=10:01", "AT+SEND=10:00"]

    def test_custom_payloads(self):
        connector, port, device = self._ready(f_port=3, on_payload="ff00", off_payload="0000")
        connector.send(device, "on")
        assert port.commands() == ["AT+SEND=3:FF00"]

    def test_f_port_defaults_to_one(self):
        """Unlike the network-server connector, the port here is a parameter of the module's
        own transmit command rather than a remote node's application port — and a template
        that has no {port} at all is legitimate for a raw point-to-point link."""
        connector, port, device = self._ready()
        connector.send(device, "on")
        assert port.commands() == ["AT+SEND=1:01"]

    def test_the_rn2483_template(self):
        connector = make_connector(dialect="rn2483")
        port = FakePort(block_when_empty=False)
        connector._serial = port
        connector.send(StubDevice(name="r", is_writable=True, controller_options={"f_port": 2}), "on")
        assert port.commands() == ["mac tx uncnf 2 01"]

    def test_base64_encoding_on_the_wire(self):
        connector = make_connector(encoding="base64")
        port = FakePort(block_when_empty=False)
        connector._serial = port
        connector.send(StubDevice(name="r", is_writable=True, controller_options={"f_port": 1}), "on")
        assert port.commands() == ["AT+SEND=1:AQ=="]

    def test_a_closed_radio_drops_the_command(self, caplog):
        connector = make_connector()
        device = StubDevice(name="relay", is_writable=True, controller_options={"f_port": 10})
        with caplog.at_level(logging.WARNING):
            connector.send(device, "on")  # must not raise
        assert any("is not open" in r.message for r in caplog.records)

    def test_a_dialect_without_a_send_template_drops_the_command(self, caplog):
        connector = make_connector(dialect="custom", receive_pattern=r"(?P<data>[0-9A-Fa-f]+)")
        connector._serial = FakePort(block_when_empty=False)
        with caplog.at_level(logging.WARNING):
            connector.send(StubDevice(name="r", is_writable=True, controller_options={"f_port": 1}), "on")
        assert any("no send_template" in r.message for r in caplog.records)

    def test_bad_hex_is_logged_not_raised(self, caplog):
        """send() runs on the ALGORITHM's thread and nothing in that chain catches."""
        connector, port, device = self._ready(f_port=10, on_payload="0x1")
        with caplog.at_level(logging.ERROR):
            connector.send(device, "on")  # must not raise
        assert any("Error sending 'on' to 'relay'" in r.message for r in caplog.records)
        assert port.commands() == []

    def test_a_serial_error_is_logged_not_raised(self, caplog):
        connector, port, device = self._ready(f_port=10)
        port.write = MagicMock(side_effect=SerialException("cable pulled"))
        with caplog.at_level(logging.ERROR):
            connector.send(device, "on")  # must not raise
        assert any("Error sending" in r.message for r in caplog.records)

    def test_it_logs_a_queue_not_a_send(self, caplog):
        connector, port, device = self._ready(f_port=10)
        with caplog.at_level(logging.INFO):
            connector.send(device, "on")
        assert any("Queued downlink" in r.message for r in caplog.records)


class TestStart:
    def test_no_port_returns_idle(self, caplog):
        connector = make_connector(port="")
        with caplog.at_level(logging.ERROR):
            connector.start()  # must return, not loop
        assert any("No serial port configured" in r.message for r in caplog.records)

    def test_no_usable_pattern_returns_idle(self, caplog):
        connector = make_connector(dialect="custom")
        connector.inject_devices({"node": make_device()})
        with caplog.at_level(logging.ERROR):
            connector.start()
        assert any("connector idle" in r.message for r in caplog.records)

    def test_no_device_returns_idle(self, caplog):
        connector = make_connector()
        connector.inject_devices({})
        with caplog.at_level(logging.WARNING):
            connector.start()
        assert any("No readable device injected" in r.message for r in caplog.records)

    def test_a_scripted_frame_reaches_the_device(self):
        connector = make_connector()
        device = make_device()
        connector.inject_devices({"node": device})
        port = FakePort([f"+EVT:RX_1:-53:8:UNICAST:2:{PAYLOAD_HEX}\r\n"])
        assert run_session(connector, port, lambda: device.is_data_ready())
        assert port.closed

    def test_init_commands_are_written_on_open(self):
        connector = make_connector(init_commands=["AT+NWM=1", "AT+JOIN=1:0:10:8"])
        connector.inject_devices({"node": make_device()})
        port = FakePort()
        run_session(connector, port, lambda: len(port.written) >= 2)
        assert port.commands() == ["AT+NWM=1", "AT+JOIN=1:0:10:8"]

    def test_on_connected_fires_for_write_only_devices(self):
        connector = make_connector()
        relay = StubDevice(name="relay", is_readable=False, is_writable=True)
        connector.inject_devices({"node": make_device(), "relay": relay})
        assert run_session(connector, FakePort(), lambda: relay.is_connected())

    def test_stop_unblocks_the_read_loop(self):
        """The bounded read_timeout is what makes the cooperative stop land; the port is not
        closed from another thread, which pyserial does not document as safe."""
        connector = make_connector()
        connector.inject_devices({"node": make_device()})
        port = FakePort()
        with patch("connectors.lora.serial.Serial", return_value=port):
            thread = run_in_thread(connector.start)
            assert wait_until(lambda: connector._serial is not None)
            assert_stops(connector, thread)

    def test_an_unreachable_radio_backs_off_instead_of_raising(self, caplog):
        """Raising would spend the supervisor's restart budget, and past the cap the worker is
        finished — which makes main shut the whole EMS down over an unplugged USB radio."""
        connector = make_connector(reconnect_backoff_seconds=0.1)
        connector.inject_devices({"node": make_device()})
        opens = []

        def refuse(*args, **kwargs):
            opens.append(1)
            raise SerialException("no such device")

        with patch("connectors.lora.serial.Serial", side_effect=refuse):
            with caplog.at_level(logging.ERROR):
                thread = run_in_thread(connector.start)
                assert wait_until(lambda: len(opens) >= 2)
                assert_stops(connector, thread)
        assert any("Retrying in" in r.message for r in caplog.records)

    def test_a_mid_session_failure_reopens(self):
        connector = make_connector(reconnect_backoff_seconds=0.1)
        connector.inject_devices({"node": make_device()})
        first = FakePort([SerialException("cable pulled")])
        second = FakePort()
        with patch("connectors.lora.serial.Serial", side_effect=[first, second]):
            thread = run_in_thread(connector.start)
            assert wait_until(lambda: connector._serial is second)
            assert_stops(connector, thread)
        assert first.closed

    def test_stop_before_start_is_a_no_op(self):
        connector = make_connector()
        connector.stop()  # must not raise: _serial only exists once start() ran
        assert connector.is_stopping()
