from base64 import b64decode, b64encode
from binascii import Error as BinasciiError
from dataclasses import dataclass
from json import dumps
from re import Pattern, compile, error
from threading import Lock
from typing import Any, Optional, override

import serial
from serial import SerialException

from api.capabilities import Switch
from api.connector import Connector
from api.device import Device
from api.options import float_option, int_option

# The canonical frame this connector puts on the wire to its devices. devices/lora.py reads it
# back under `profile: "native"`; the two are in packages that may not import each other, so
# tests/test_lora_connector.py pins them together. Deliberately the same *shape* as a network
# server's uplink — a base64 payload plus metadata — so one device class serves both halves of
# LoRa and a point-to-point capture replays exactly like a LoRaWAN one.
NATIVE_ENVELOPE_KEYS = frozenset({"data", "f_port", "rssi", "snr", "address"})

_DEFAULT_F_PORT = 1


@dataclass(frozen=True)
class Dialect:
	"""One module family's serial language: how a received frame looks, how a sent one is spelled.

	`receive_pattern` is a regex whose named groups name what they capture — `data` (required),
	and optionally `port`, `rssi`, `snr`, `address`. `send_template` is a format string over
	`{port}` and `{data}`. Both are plain configuration, which is the whole point: the module
	families below share no syntax whatsoever, and the ones that do not exist yet share none
	with them either.
	"""
	receive_pattern: str
	send_template: str
	init_commands: tuple[str, ...] = ()


# Documented starting points, not a compatibility guarantee — that is what `custom` is for.
# These strings are transcribed from the vendors' AT command manuals and they move between
# firmware revisions, so an operator whose module answers differently overrides
# receive_pattern / send_template in config.json and needs no code change. init_commands is
# empty everywhere on purpose: a join sequence depends on region, keys and device class, all
# of which are site-specific and none of which this repo can guess.
DIALECTS: dict[str, Dialect] = {
	# RAK WisDuo (RAK3172, RAK811). Unsolicited receive looks like
	#   +EVT:RX_1:-53:8:UNICAST:2:0102030405
	"rak": Dialect(
		receive_pattern=r"\+EVT:RX_[12]:(?P<rssi>-?\d+):(?P<snr>-?\d+):\w+:(?P<port>\d+):(?P<data>[0-9A-Fa-f]+)",
		send_template="AT+SEND={port}:{data}",
	),
	# Microchip RN2483, which predates the AT convention entirely:
	#   mac_rx 1 0102030405
	"rn2483": Dialect(
		receive_pattern=r"mac_rx\s+(?P<port>\d+)\s+(?P<data>[0-9A-Fa-f]+)",
		send_template="mac tx uncnf {port} {data}",
	),
	# Seeed LoRa-E5 (STM32WLE5), which quotes its payload:
	#   +MSG: PORT: 8; RX: "0102030405"   /   +TEST: RX "0102030405"
	"lora_e5": Dialect(
		receive_pattern=r"\+(?:MSG|TEST):(?:\s*PORT:\s*(?P<port>\d+);)?\s*RX:?\s*\"(?P<data>[0-9A-Fa-f]*)\"",
		send_template="AT+MSGHEX=\"{data}\"",
	),
	"custom": Dialect(receive_pattern="", send_template=""),
}


@dataclass(frozen=True)
class Route:
	"""One device's claim on the single stream of frames coming off the radio."""
	device: Device
	address: Optional[str]
	f_port: Optional[int]


class LoRaConnector(Connector):
	"""A LoRa radio wired straight to this host, over a serial port.

	The point-to-point half of LoRa, and the one that genuinely has no network server: a UART
	module (RAK WisDuo, Microchip RN2483, Seeed LoRa-E5, …) speaking its own AT dialect. For
	LoRaWAN through ChirpStack, The Things Stack or any other network server — which is what
	most sites have — use `connectors/lorawan.py` instead: it needs no radio on this machine
	and no dependency outside the core.

	**Hardware-agnostic by configuration, not by driver.** The three module families above
	share no command syntax at all, so this connector holds no per-module code: a dialect is a
	receive regex with named groups, a send template, and an optional list of init commands.
	The named dialects are documented starting points transcribed from vendor manuals;
	`dialect: "custom"` with your own regex is what makes a module nobody here has heard of
	work, and is the actual compatibility guarantee.

	Every matched frame is normalised into the same envelope a network server would have sent
	(`NATIVE_ENVELOPE_KEYS`), so `devices/lora.py` decodes a point-to-point payload and a
	LoRaWAN one through one code path, and a capture off this radio replays through
	`emulates: "lora"` exactly as a broker capture does.

	`stop()` is deliberately **not** overridden. The read loop blocks in `readline()` for at
	most `read_timeout`, so the cooperative stop event is reached within a bounded slice.
	Closing the port from another thread to shorten that is not documented as safe across
	pyserial's platform backends — the inverse of `connectors/home_assistant.py`, where
	`abort()` is the library's documented wake-up primitive and overriding `stop()` is right.

	A downlink here reaches a Class A node no faster than it would through a network server:
	see `connectors/lorawan.py`'s class docstring for what that means for
	`algorithm_decisions.csv`.
	"""

	port: str
	baudrate: int
	dialect: str

	@override
	def __init__(self, name: str, port: str = "", baudrate: Any = 9600, dialect: str = "custom",
				 receive_pattern: Optional[str] = None, send_template: Optional[str] = None,
				 init_commands: Optional[list] = None, encoding: str = "hex",
				 read_timeout: Any = 1.0, write_timeout: Any = 2.0,
				 reconnect_backoff_seconds: Any = 5.0, max_backoff_seconds: Any = 120.0,
				 newline: str = "\r\n") -> None:
		super().__init__(name)
		self.port = port
		self.baudrate = int_option(self.LOGGER, "baudrate", baudrate, 9600, minimum=50, maximum=4000000)
		self.dialect = str(dialect).strip().lower()
		if self.dialect not in DIALECTS:
			self.LOGGER.warning(f"Unknown dialect '{dialect}' on {name}, falling back to 'custom'")
			self.dialect = "custom"
		preset = DIALECTS[self.dialect]

		# An explicitly declared value always wins over the preset. That is the escape hatch:
		# a firmware revision that moved a field costs one config line, not a release.
		raw_pattern = receive_pattern if receive_pattern else preset.receive_pattern
		self._receive_pattern: Optional[Pattern] = None
		if raw_pattern:
			try:
				self._receive_pattern = compile(raw_pattern)
			except error as e:
				self.LOGGER.error(f"Invalid receive_pattern '{raw_pattern}' : {e}")
		if self._receive_pattern is not None and "data" not in (self._receive_pattern.groupindex or {}):
			self.LOGGER.error(f"receive_pattern on {name} captures no (?P<data>...) group, so no frame could ever carry a payload")
			self._receive_pattern = None
		self._send_template = send_template if send_template else preset.send_template
		self.init_commands = [str(command) for command in (init_commands or preset.init_commands)]

		self.encoding = str(encoding).strip().lower()
		if self.encoding not in ("hex", "base64"):
			self.LOGGER.warning(f"Unknown encoding '{encoding}' on {name}, using 'hex'")
			self.encoding = "hex"
		self.read_timeout = float_option(self.LOGGER, "read_timeout", read_timeout, 1.0, minimum=0.05, maximum=60.0)
		self.write_timeout = float_option(self.LOGGER, "write_timeout", write_timeout, 2.0, minimum=0.05, maximum=60.0)
		self.reconnect_backoff_seconds = float_option(self.LOGGER, "reconnect_backoff_seconds", reconnect_backoff_seconds, 5.0, minimum=0.1)
		self.max_backoff_seconds = float_option(self.LOGGER, "max_backoff_seconds", max_backoff_seconds, 120.0, minimum=0.1)
		self.newline = str(newline).replace("\\r", "\r").replace("\\n", "\n")

		self._serial: Optional[serial.Serial] = None
		self._routes: list[Route] = []
		# The read loop and any algorithm's send() share one port, and a pyserial write is not
		# documented as thread-safe. Interleaved AT commands surface as a module that answers
		# ERROR to a command nobody sent.
		self._write_lock = Lock()

	@override
	def inject_devices(self, devices: dict[str, Device]) -> None:
		super().inject_devices(devices)  # sets self.devices + back-references
		self._routes = []
		unfiltered: list[str] = []
		for device in devices.values():
			if not device.is_readable:
				continue
			declared = device.listener_options.get("address") or device.listener_options.get("dev_eui")
			route = Route(
				device=device,
				address=self._normalise_address(str(declared)) if declared else None,
				f_port=self._as_optional_int(device.listener_options.get("f_port")),
			)
			self._routes.append(route)
			if route.address is None and route.f_port is None:
				unfiltered.append(device.name)
		if len(unfiltered) > 1:
			# One device with no filter is the common single-node case and is fine. Several is
			# a config that silently gives every node's reading to every device.
			self.LOGGER.warning(f"Devices {unfiltered} declare neither listener_options.address nor listener_options.f_port; every frame will be delivered to all of them")

	@override
	def start(self) -> None:
		if not self.port:
			self.LOGGER.error("No serial port configured, connector idle")
			return
		if self._receive_pattern is None:
			self.LOGGER.error(f"No usable receive_pattern for dialect '{self.dialect}', connector idle: nothing arriving on the radio could be decoded")
			return
		if not self._routes:
			self.LOGGER.warning("No readable device injected, connector idle")
			return

		backoff = self.reconnect_backoff_seconds
		while not self.is_stopping():
			try:
				self._open()
				backoff = self.reconnect_backoff_seconds
				self._read_loop()
			except (OSError, SerialException) as e:
				# Never raised out of start(): burning the supervisor's restart budget makes
				# this worker *finished*, and main waits on connectors — so the whole EMS
				# would shut down because a USB radio was unplugged for a minute.
				self.LOGGER.error(f"Serial link on {self.port} failed: {e}. Retrying in {backoff:.0f}s...")
				self._close()
				if self.wait_stop(backoff):
					break
				backoff = min(backoff * 2, self.max_backoff_seconds)
		self._close()
		self.LOGGER.info("Serial loop stopped")

	def _open(self) -> None:
		self._serial = serial.Serial(port=self.port, baudrate=self.baudrate,
									 timeout=self.read_timeout, write_timeout=self.write_timeout)
		self.LOGGER.info(f"Opened {self.port} at {self.baudrate} baud, dialect '{self.dialect}'")
		for command in self.init_commands:
			self._write_line(command)
		self.on_connected()  # marks write-only devices as connected

	def _close(self) -> None:
		port, self._serial = self._serial, None
		if port is None:
			return
		try:
			port.close()
		except (OSError, SerialException) as e:
			self.LOGGER.debug(f"Closing {self.port} raised {e}")

	def _read_loop(self) -> None:
		while not self.is_stopping():
			line = self._serial.readline() if self._serial is not None else b""
			if not line:
				continue  # the read timeout elapsed — the bounded slice that makes stop() land
			text = line.decode("utf-8", errors="replace").strip()
			if not text:
				continue
			self.LOGGER.debug(f"[{self.port}] {text}")
			self._dispatch(text)

	def _dispatch(self, line: str) -> None:
		"""Route one received line to whichever devices claim it.

		A line that does not match is not an error: a module echoes commands, answers OK, and
		emits unsolicited status of its own. Only a match is a frame.
		"""
		match = self._receive_pattern.search(line) if self._receive_pattern else None
		if match is None:
			return
		groups = match.groupdict()
		payload = self._decode_wire_payload(groups.get("data"))
		if payload is None:
			return
		frame = {
			"data": b64encode(payload).decode("ascii"),
			"f_port": self._as_optional_int(groups.get("port")),
			"rssi": self._as_optional_float(groups.get("rssi")),
			"snr": self._as_optional_float(groups.get("snr")),
			"address": self._normalise_address(groups["address"]) if groups.get("address") else None,
		}
		targets = self._route(frame, payload.hex())
		if not targets:
			self.LOGGER.debug(f"No device claims a frame on fPort {frame['f_port']} from {frame['address']}")
			return
		body = dumps(frame)
		for device in targets:
			try:
				accepted = device.receive(body)
				self.on_device_data_received(device, accepted)
			except Exception as e:
				# One device's parser must not end the session for every other device on this
				# radio, and an escape here would land on the supervised start() thread.
				self.LOGGER.error(f"Device '{device.name}' failed on a frame: {e}", exc_info=True)

	def _route(self, frame: dict[str, Any], payload_hex: str) -> list[Device]:
		matched: list[Device] = []
		for route in self._routes:
			if route.address is not None:
				candidate = frame.get("address")
				if candidate is None:
					# The dialect exposes no address group. A raw point-to-point link has no
					# MAC addressing at all, so who sent a frame can only be inside the frame:
					# fall back to an application-level address in the leading bytes.
					candidate = payload_hex[:len(route.address)]
				if candidate != route.address:
					continue
			if route.f_port is not None and route.f_port != frame.get("f_port"):
				continue
			matched.append(route.device)
		return matched

	def _decode_wire_payload(self, text: Optional[str]) -> Optional[bytes]:
		if text is None:
			return None
		try:
			if self.encoding == "base64":
				return b64decode(text + "=" * (-len(text) % 4))
			return bytes.fromhex(text)
		except (BinasciiError, ValueError) as e:
			self.LOGGER.warning(f"Undecodable {self.encoding} payload {text!r} on {self.port}: {e}")
			return None

	@override
	def send(self, device: Device, payload: str) -> None:
		# Every conversion is inside the try, not just the write: send() runs on an
		# *algorithm's* thread and nothing in Algorithm.control_device -> DevicesManager
		# .control -> Device.control -> here catches, so a ValueError from an operator's typo
		# would be counted as an algorithm crash — five restarts, backoff, CRITICAL.
		try:
			if not self._send_template:
				self.LOGGER.warning(f"Dialect '{self.dialect}' declares no send_template, dropping command '{payload}' for '{device.name}'")
				return
			options = device.controller_options
			f_port = int_option(self.LOGGER, f"{device.name}.f_port", options.get("f_port"), _DEFAULT_F_PORT, minimum=1, maximum=223)
			raw = self._resolve_payload(payload, options)
			data = b64encode(raw).decode("ascii") if self.encoding == "base64" else raw.hex().upper()
			line = self._send_template.format(port=f_port, data=data)
			radio = self._serial
			if radio is None:
				self.LOGGER.warning(f"Radio on {self.port} is not open, dropping command '{payload}' for '{device.name}'")
				return
			self._write_line(line)
		except (KeyError, IndexError, OSError, SerialException, TypeError, ValueError) as e:
			self.LOGGER.error(f"Error sending '{payload}' to '{device.name}': {e}")
			return
		self.LOGGER.info(f"Queued downlink for '{device.name}' on fPort {f_port} ({len(raw)} byte(s)) — a Class A node receives it in the window after its next uplink")

	def _write_line(self, line: str) -> None:
		radio = self._serial
		if radio is None:
			return
		with self._write_lock:
			radio.write((line + self.newline).encode("ascii", errors="replace"))
		self.LOGGER.debug(f"[{self.port}] > {line}")

	@staticmethod
	def _resolve_payload(payload: str, options: dict[str, Any]) -> bytes:
		"""The application payload bytes a command carries.

		Mirrors `connectors/lorawan.py`'s resolution so one `lora_switch` config works behind
		either connector; `payload_encoding` is declared and never inferred, because "does
		this look like hex" would make "00" and "0" mean different things by parity.
		"""
		token = payload.strip().lower()
		if token == Switch.COMMAND_ON:
			raw = str(options.get("on_payload", "01"))
		elif token == Switch.COMMAND_OFF:
			raw = str(options.get("off_payload", "00"))
		else:
			raw = payload
		encoding = str(options.get("payload_encoding", "hex")).strip().lower()
		if encoding == "utf8":
			return raw.encode("utf-8")
		if encoding == "base64":
			return b64decode(raw, validate=True)
		return bytes.fromhex(raw)

	@staticmethod
	def _normalise_address(value: str) -> str:
		"""Separator-free lower-case hex, so a datasheet's `70-B3-…` matches a module's raw hex."""
		return value.replace("-", "").replace(":", "").replace(" ", "").strip().lower()

	@staticmethod
	def _as_optional_int(value: Any) -> Optional[int]:
		if value is None or value == "":
			return None
		try:
			return int(value)
		except (TypeError, ValueError):
			return None

	@staticmethod
	def _as_optional_float(value: Any) -> Optional[float]:
		if value is None or value == "":
			return None
		try:
			return float(value)
		except (TypeError, ValueError):
			return None
