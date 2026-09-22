from base64 import b64decode
from binascii import Error as BinasciiError
from dataclasses import dataclass, fields as dataclass_fields
from json import JSONDecodeError, loads
from re import compile
from struct import error as StructError, unpack
from typing import Any, Optional, override

from api.capabilities import EnergyMeter, MetricSource
from api.device import Device

# Byte widths per fixed-size data type. UINT24/INT24 are here because a LoRaWAN payload is
# paid for in airtime, so a 24-bit counter is common precisely because 32 bits is one byte
# too many. `struct` has no 24-bit format — which is why every integer decodes through
# int.from_bytes (1/2/3/4/8 bytes and both endiannesses for free) and struct.unpack is used
# only for the two floats. That is deliberately simpler than devices/modbus_meter.py's
# _FORMATS table, because Modbus counts in 16-bit words and LoRaWAN counts in bytes.
_WIDTHS: dict[str, int] = {
	"UINT8": 1, "INT8": 1, "BOOL": 1,
	"UINT16": 2, "INT16": 2,
	"UINT24": 3, "INT24": 3,
	"UINT32": 4, "INT32": 4, "FLOAT32": 4,
	"UINT64": 8, "INT64": 8, "FLOAT64": 8,
}
_SIGNED = frozenset({"INT8", "INT16", "INT24", "INT32", "INT64"})
_FLOATS: dict[str, str] = {"FLOAT32": "f", "FLOAT64": "d"}
# Variable-width: `length` is required, there is nothing to default it from.
_VARIABLE = frozenset({"STRING", "RAW"})

# The one role a generic node needs to understand. A closed enum rather than free text so a
# typo is a config-load warning, not a silently-zero energy total six months later. Must stay
# equal to devices/modbus_meter.py's constant — both are read through EnergyMeter by the same
# algorithms, so they have to mean the same thing. tests/test_lora_profiles.py asserts it.
ROLE_ENERGY_IMPORT_KWH = "energy_import_kwh"

# Above the 222-byte maximum an SF7 uplink can carry, so a legitimate payload never trips it.
_DEFAULT_MAX_PAYLOAD_BYTES = 256

# A 64-bit EUI as the network servers spell it. Mirrored in connectors/lorawan.py, which
# normalises the same way to build a topic filter; neither package may import the other.
_HEX_EUI = compile(r"[0-9A-Fa-f]{16}")

# 'a.b[0].c' and 'a.b[*].c' tokenise to keys, ints, and the any-element sentinel.
_PATH_TOKENS = compile(r"\[(\*|-?\d+)\]|([^.\[\]]+)")
_ANY = object()


@dataclass(frozen=True)
class Envelope:
	"""Where each thing sits inside one network server's uplink JSON.

	Only `payload`, `decoded` and `f_port` are load-bearing; the rest populate
	`self.data["uplink"]` for observability. Every path resolves to None on a miss, so a
	field spelled wrong costs a one-line table edit rather than a crash — which is what makes
	adding a network server a data change.
	"""
	payload: Optional[str]
	decoded: Optional[str]
	f_port: Optional[str]
	f_cnt: Optional[str]
	dev_eui: Optional[str]
	device_id: Optional[str]
	rssi: Optional[str]
	snr: Optional[str]


# Keep the profile *names* in step with connectors/lorawan.py's PROFILES —
# tests/test_lora_profiles.py asserts it. The contents are unrelated on purpose: that table
# holds topic trees and downlink bodies, this one holds envelope read paths, and the two sets
# are disjoint. `native` is what connectors/lora.py emits over a directly-attached radio.
PROFILES: dict[str, Envelope] = {
	"chirpstack": Envelope(
		payload="data",
		decoded="object",
		f_port="fPort",
		f_cnt="fCnt",
		dev_eui="deviceInfo.devEui",
		device_id="deviceInfo.deviceName",
		rssi="rxInfo[*].rssi",
		snr="rxInfo[*].snr",
	),
	"things_stack": Envelope(
		payload="uplink_message.frm_payload",
		decoded="uplink_message.decoded_payload",
		f_port="uplink_message.f_port",
		f_cnt="uplink_message.f_cnt",
		dev_eui="end_device_ids.dev_eui",
		device_id="end_device_ids.device_id",
		rssi="uplink_message.rx_metadata[*].rssi",
		snr="uplink_message.rx_metadata[*].snr",
	),
	"native": Envelope(
		payload="data",
		decoded="object",
		f_port="f_port",
		f_cnt=None,
		dev_eui="address",
		device_id=None,
		rssi="rssi",
		snr="snr",
	),
}


@dataclass(frozen=True)
class FieldSpec:
	"""One declared value, as the device needs it for decoding."""
	name: str
	offset: Optional[int]
	source: Optional[str]
	data_type: str
	length: int
	byte_order: str
	bit: Optional[int]
	f_port: Optional[int]
	scale: float
	bias: float
	unit: Optional[str]
	role: Optional[str]


def _split_path(path: str) -> list[Any]:
	"""'uplink_message.rx_metadata[*].rssi' -> ['uplink_message', 'rx_metadata', ANY, 'rssi']"""
	segments: list[Any] = []
	for index, key in _PATH_TOKENS.findall(path):
		if index:
			segments.append(_ANY if index == "*" else int(index))
		elif key:
			segments.append(key)
	return segments


def _resolve_path(document: Any, path: Any) -> Any:
	"""Read a path out of a decoded JSON document, or None. Never raises.

	Dotted segments, `[n]` for a literal index and `[*]` for "the first element that yields a
	value". A list of literal segments is accepted too, for the pathological case of a key
	containing a dot.

	Dotted rather than JSON Pointer: it is how the vendor documentation writes these paths,
	JSON Pointer's ~0/~1 escaping is a footgun in a hand-edited config, and — decisively —
	Pointer cannot express `[*]`. `rxInfo` and `rx_metadata` are arrays of *gateway
	receptions*, so index 0 is whichever gateway the network server happened to list first,
	and a hard-coded 0 silently changes meaning the day a second gateway comes online.
	"""
	if path is None:
		return None
	segments = list(path) if isinstance(path, list) else _split_path(str(path))
	return _walk(document, segments)


def _walk(node: Any, segments: list[Any]) -> Any:
	if not segments:
		return node
	head, rest = segments[0], segments[1:]
	if head is _ANY:
		if not isinstance(node, list):
			return None
		for item in node:
			found = _walk(item, rest)
			if found is not None:
				return found
		return None
	if isinstance(head, int) and not isinstance(head, bool):
		if not isinstance(node, list) or not -len(node) <= head < len(node):
			return None
		return _walk(node[head], rest)
	if not isinstance(node, dict) or head not in node:
		return None
	return _walk(node[head], rest)


def _normalise_eui(value: str) -> str:
	"""Strip separators and lower-case a 16-hex-digit EUI; leave anything else alone.

	The Things Stack reports `end_device_ids.dev_eui` upper-cased while ChirpStack lower-cases
	it, and an operator pastes whichever the datasheet showed — so the identity check has to
	compare normalised forms or it would reject every uplink from a correctly configured node.
	"""
	stripped = value.replace("-", "").replace(":", "").replace(" ", "").strip()
	return stripped.lower() if _HEX_EUI.fullmatch(stripped) else value


class LoRa(Device, EnergyMeter, MetricSource):
	"""A LoRa end node whose payload layout comes from `config.json`.

	One class covers every node model behind every network server, because LoRaWAN
	standardises the framing and standardises nothing about the bytes inside: the envelope is
	a named profile and the payload layout is a field map, both configuration rather than
	code. It serves the `lorawan` protocol (a network server over MQTT), the `lora` protocol
	(a radio wired to this host) and plain `mqtt` — a node is a node, and how its frame
	reached the EMS is the connector's business.

	Accepting `mqtt` is the graceful-degradation path: a plain MQTT connector with a
	hand-written subscription and pattern, plus this device, is a working *read* path against
	any network server at all, with no profile and no LoRaWANConnector.

	This module imports nothing outside the stdlib — no paho, no pyserial. That is deliberate
	and load-bearing: it is what lets a LoRa node be replayed from a CSV with
	`emulates: "lorawan"` on a machine with no radio and no broker, and what keeps this
	device's tests running in a core-only checkout.
	"""
	_fields: list[FieldSpec]
	_energy_names: list[str]

	# Three labels, one handler — the case that makes the declaration worth having, because
	# no derivation from method names could produce it. A network server reaches this over
	# 'lorawan', a directly attached radio over 'lora', and a server no profile describes over
	# plain 'mqtt' with a hand-written subscription and pattern.
	SUPPORTED_PROTOCOLS = ("lorawan", "lora", "mqtt")

	PROTOCOL_REFUSAL = (
		"a LoRa reading is a network server's uplink envelope, or connectors/lora.py's frame "
		"off a directly attached radio; either reaches a device over 'lorawan', 'lora' or "
		"plain 'mqtt'"
	)

	@override
	def __init__(self, name: str, connector_options: dict[str, Any], listener_options: dict[str, Any], controller_options: dict[str, Any]) -> None:
		super().__init__(name, connector_options, listener_options, controller_options)
		self._fields = []
		self._energy_names = []
		self._failed_fields: set[str] = set()
		self._parse_failing = False
		expected = listener_options.get("dev_eui")
		self._expected_dev_eui: Optional[str] = _normalise_eui(str(expected)) if expected else None
		self._max_payload_bytes = self._as_int(listener_options.get("max_payload_bytes"), _DEFAULT_MAX_PAYLOAD_BYTES)
		self._envelope = self._resolve_envelope()
		self._build_specs()

	def _resolve_envelope(self) -> Envelope:
		"""Which uplink JSON shape this node's frames arrive in.

		Declared on the *device* rather than read from the connector, because a device must
		work with any connector — including a `pseudo` one replaying a capture, which knows
		only `emulates: "lorawan"`. `Config` injects nothing but `protocol` into
		`connector_options`, so there is no path from here to the connector's options, and
		that is the property that makes a raw broker capture replayable.
		"""
		profile = str(self.listener_options.get("profile", "chirpstack")).strip().lower()
		if profile == "custom":
			paths = self.listener_options.get("paths")
			if not isinstance(paths, dict):
				self.LOGGER.warning(f"Profile 'custom' on {self.name} declares no listener_options.paths; it will decode nothing")
				paths = {}
			return Envelope(**{spec.name: paths.get(spec.name) for spec in dataclass_fields(Envelope)})
		if profile not in PROFILES:
			self.LOGGER.warning(f"Unknown profile '{profile}' on {self.name}, using 'chirpstack'")
			profile = "chirpstack"
		return PROFILES[profile]

	def _build_specs(self) -> None:
		"""Resolve the declared field map once, warning about anything unusable.

		Never raises: a constructor that throws is contained by main.create_classes, but the
		containment costs the whole device — one bad entry in the map and nothing here
		reports at all. A malformed entry is dropped with a warning; the rest of the node still decodes.
		"""
		default_order = str(self.listener_options.get("byte_order", "big")).strip().lower()
		if default_order not in ("big", "little"):
			self.LOGGER.warning(f"Unknown byte_order '{default_order}' on {self.name}, using 'big'")
			default_order = "big"
		declared = self.listener_options.get("fields")
		if not isinstance(declared, list):
			self.LOGGER.warning(f"No usable listener_options.fields on {self.name}; it will decode nothing")
			return
		seen: set[str] = set()
		for entry in declared:
			if not isinstance(entry, dict):
				continue
			name = entry.get("name")
			if not isinstance(name, str) or not name or name in seen:
				continue
			data_type = str(entry.get("data_type", "UINT16")).strip().upper()
			if data_type not in _WIDTHS and data_type not in _VARIABLE:
				self.LOGGER.warning(f"Unknown data_type '{data_type}' for field '{name}' on {self.name}, using UINT16")
				data_type = "UINT16"
			source = entry.get("source")
			offset = self._as_optional_int(entry.get("offset"))
			if source is not None and offset is not None:
				self.LOGGER.warning(f"Field '{name}' on {self.name} declares both offset and source; using source")
				offset = None
			if source is None and offset is None:
				self.LOGGER.warning(f"Field '{name}' on {self.name} declares neither offset nor source, ignoring it")
				continue
			length = self._as_int(entry.get("length"), _WIDTHS.get(data_type, 0))
			if source is None and length <= 0:
				self.LOGGER.warning(f"Field '{name}' on {self.name} is {data_type} and declares no length, ignoring it")
				continue
			order = str(entry.get("byte_order", default_order)).strip().lower()
			if order not in ("big", "little"):
				order = default_order
			bit = self._as_optional_int(entry.get("bit"))
			scale = self._as_float(entry.get("scale"), 1.0)
			bias = self._as_float(entry.get("bias"), 0.0)
			if bit is not None and (scale != 1.0 or bias != 0.0):
				# A bit is a flag: there is nothing to scale, and silently applying a scale
				# to a bool would turn True into a number downstream.
				self.LOGGER.warning(f"Field '{name}' on {self.name} declares a bit and a scale/bias; the scale is ignored")
				scale, bias = 1.0, 0.0
			role = entry.get("role")
			if role is not None and role != ROLE_ENERGY_IMPORT_KWH:
				self.LOGGER.warning(f"Unknown role '{role}' for field '{name}' on {self.name}, ignoring it")
				role = None
			unit = entry.get("unit")
			if role == ROLE_ENERGY_IMPORT_KWH and isinstance(unit, str) and unit.strip().lower() != "kwh":
				# No implicit conversion. `role` promises the *scaled* value is already kWh;
				# converting from `unit` instead would let a typo ("Wh", "KWh") silently
				# rescale a revenue reading by a factor of 1000.
				self.LOGGER.warning(
					f"Field '{name}' on {self.name} declares role '{ROLE_ENERGY_IMPORT_KWH}' but unit '{unit}'. "
					f"The role means the scaled value is already kWh — no conversion is applied; fix `scale` instead."
				)
			seen.add(name)
			self._fields.append(FieldSpec(
				name=name,
				offset=offset,
				source=str(source) if source is not None else None,
				data_type=data_type,
				length=length,
				byte_order=order,
				bit=bit,
				f_port=self._as_optional_int(entry.get("f_port")),
				scale=scale,
				bias=bias,
				unit=unit if isinstance(unit, str) else None,
				role=role,
			))
			if role == ROLE_ENERGY_IMPORT_KWH:
				self._energy_names.append(name)
		if self._fields and not self._energy_names:
			self.LOGGER.info(
				f"No field on {self.name} declares role '{ROLE_ENERGY_IMPORT_KWH}'; "
				f"get_total_energy_kwh() will report 0.0"
			)

	@override
	def receive(self, *args, **kwargs) -> Optional[bool]:
		if self.refuse_unserved_protocol(*args, **kwargs):
			return False
		# Tolerate the (topic, payload) arity as well as (payload,). A LoRaWAN connector always
		# sends two and a serial one always sends one, but a replay CSV row decides by whether
		# its `topic` column is empty — so a device that accepted only one shape would raise
		# TypeError on every row of the other. `Connector.deliver` catches that now and logs
		# one traceback against this device, which is a great deal better than the silence it
		# used to be, but it is still a backtest that produces no readings.
		if not args:
			self.LOGGER.warning(f"Empty receive() call on {self.name}")
			return False
		return self.receive_lorawan(args[-1])

	def receive_lorawan(self, payload: str) -> bool:
		"""Decode one uplink envelope into `self.data`.

		The wire format is the network server's own JSON string, unchanged. That is what lets
		a capture taken straight off the broker — `mosquitto_sub -v -t 'application/#'` —
		replay byte-identically through `emulates`, and it keeps the live path and the replay
		path the same path. Unwrapping the envelope in the connector instead would mean the
		replayable format was one this repo invented, and no real capture would fit it.
		"""
		try:
			message = loads(payload)
			if not isinstance(message, dict):
				raise ValueError("uplink is not a JSON object")
		except (JSONDecodeError, TypeError, ValueError) as e:
			# Keep the last good reading. Assigning the empty result would turn an unreadable
			# uplink into a reading *of* nothing, which the framework then publishes to
			# algorithms and writes to storage.
			self._log_parse_failure(f"Unreadable uplink on {self.name}: {e}")
			return False

		dev_eui = _resolve_path(message, self._envelope.dev_eui)
		if not self._matches_identity(dev_eui):
			return False

		f_port = self._as_optional_int(_resolve_path(message, self._envelope.f_port))
		f_cnt = self._as_optional_int(_resolve_path(message, self._envelope.f_cnt))
		decoded_object = _resolve_path(message, self._envelope.decoded)
		try:
			raw = self._decode_payload(_resolve_path(message, self._envelope.payload))
		except (BinasciiError, TypeError, ValueError) as e:
			self._log_parse_failure(f"Undecodable payload on {self.name}: {e}")
			return False
		if len(raw) > self._max_payload_bytes:
			self._log_parse_failure(f"Payload on {self.name} is {len(raw)} bytes, over max_payload_bytes ({self._max_payload_bytes}) — this is an envelope mismatch, not a reading")
			return False
		if not raw and not decoded_object:
			# Completely normal: an empty uplink is how a Class A node opens a receive window
			# to collect a queued downlink. Not a failure, so it must not set _parse_failing —
			# but no reading came of it either, and the framework still marks the device
			# connected, which is the true statement: alive, and reported nothing.
			self.LOGGER.debug(f"Empty uplink on {self.name} (fPort {f_port}), no reading")
			return False

		previous = self.data.get("fields") if isinstance(self.data.get("fields"), dict) else {}
		fields: dict[str, dict[str, Any]] = {}
		fresh = False
		for spec in self._fields:
			# LoRaWAN multiplexes payload layouts by port BY DESIGN — 1 periodic, 2 status,
			# 10 config echo — so a port-2 uplink legitimately decodes two of twelve fields.
			# Replacing self.data wholesale the way devices/modbus_meter.py does would erase
			# the energy reading on every status uplink and make get_total_energy_kwh()
			# return 0.0 intermittently forever, with nothing erroring anywhere: the exact
			# failure `role`'s closed enum exists to prevent, arriving through another door.
			# So the rule here is replace-within-the-eligible-set and carry the rest, with
			# each value keeping the f_cnt of the uplink that produced it so a reader can
			# tell a fresh value from a carried one.
			if spec.f_port is not None and spec.f_port != f_port:
				carried = previous.get(spec.name)
				if carried is not None:
					fields[spec.name] = carried
				continue
			value = self._decode_field(spec, raw, decoded_object)
			if value is None:
				continue  # already warned, edge-triggered per field
			fresh = True
			entry: dict[str, Any] = {"value": value, "f_port": f_port, "f_cnt": f_cnt}
			if spec.unit:
				entry["unit"] = spec.unit
			fields[spec.name] = entry

		if not fresh:
			self._log_parse_failure(f"No decodable field in the uplink on {self.name} (fPort {f_port})")
			return False
		self._clear_parse_failure()
		self.data = {
			"uplink": {
				"dev_eui": dev_eui,
				"device_id": _resolve_path(message, self._envelope.device_id),
				"f_port": f_port,
				"f_cnt": f_cnt,
				"rssi": _resolve_path(message, self._envelope.rssi),
				"snr": _resolve_path(message, self._envelope.snr),
				# Hex rather than the raw bytes: it survives JSON, CSV and a log line, and it
				# is what a node vendor's support desk asks for.
				"payload_hex": raw.hex(),
			},
			"fields": fields,
		}
		self.LOGGER.debug(f"{self.data=}")
		return True

	def _matches_identity(self, dev_eui: Any) -> bool:
		"""Reject an uplink from a different node than the one this device declares.

		Reads the devEUI from the **envelope**, never from the topic. `PseudoConnector` routes
		by device name and never evaluates a topic, so a topic-based check would exist only on
		the live path — the one path a backtest never exercises. This way it is
		defence-in-depth against a mis-synthesised routing regex, a fan-out subscription, or a
		network server that shares one topic, and it survives replay.
		"""
		if self._expected_dev_eui is None or dev_eui is None:
			return True
		if _normalise_eui(str(dev_eui)) == self._expected_dev_eui:
			return True
		self._log_parse_failure(f"Uplink on {self.name} carries devEUI '{dev_eui}', not the declared '{self._expected_dev_eui}'")
		return False

	@staticmethod
	def _decode_payload(encoded: Any) -> bytes:
		"""The application payload bytes, base64 out of the envelope.

		Padding is restored defensively. Every network server pads, but a hand-edited replay
		row will not — and that is a "works live, fails in replay" bug class for one line.
		"""
		if encoded is None or encoded == "":
			return b""
		if isinstance(encoded, (bytes, bytearray)):
			return bytes(encoded)
		text = str(encoded)
		return b64decode(text + "=" * (-len(text) % 4))

	def _decode_field(self, spec: FieldSpec, raw: bytes, decoded: Any) -> Any:
		"""One field's value, or None (having warned once)."""
		try:
			if spec.source is not None:
				value = _resolve_path(decoded, spec.source)
				if value is None:
					self._log_field_failure(spec.name, f"Field '{spec.name}' on {self.name}: nothing at decoded path '{spec.source}'")
					return None
			else:
				value = self._decode_bytes(spec, raw)
				if value is None:
					return None
			value = self._apply_scale(spec, value)
		except (KeyError, StructError, TypeError, UnicodeDecodeError, ValueError) as e:
			self._log_field_failure(spec.name, f"Cannot decode field '{spec.name}' on {self.name}: {e}")
			return None
		self._failed_fields.discard(spec.name)
		return value

	def _decode_bytes(self, spec: FieldSpec, raw: bytes) -> Any:
		end = (spec.offset or 0) + spec.length
		if spec.offset is None or spec.offset < 0 or end > len(raw):
			# The normal case for a short status uplink, not an error — hence edge-triggered.
			self._log_field_failure(spec.name, f"Field '{spec.name}' on {self.name} needs bytes {spec.offset}..{end} of a {len(raw)}-byte payload")
			return None
		chunk = raw[spec.offset:end]
		if spec.data_type == "RAW":
			return list(chunk)
		if spec.data_type == "STRING":
			return chunk.decode("ascii", errors="replace").rstrip("\x00 ").strip()
		if spec.data_type in _FLOATS:
			return unpack(f"{'>' if spec.byte_order == 'big' else '<'}{_FLOATS[spec.data_type]}", chunk)[0]
		number = int.from_bytes(chunk, spec.byte_order, signed=spec.data_type in _SIGNED)
		if spec.bit is not None:
			# LSB is bit 0, which is what a datasheet's "bit 0 = alarm" means.
			return bool((number >> spec.bit) & 1)
		if spec.data_type == "BOOL":
			return number != 0
		return number

	@staticmethod
	def _apply_scale(spec: FieldSpec, value: Any) -> Any:
		"""value = raw * scale + bias.

		A multiplier and then an offset, because that is the order a vendor decoder writes it:
		a temperature is `raw * 0.1 - 40`, not `(raw - 400) * 0.1`. Bools and strings pass
		through untouched — scaling a flag would turn True into a number downstream.
		"""
		if isinstance(value, bool) or not isinstance(value, (int, float)):
			return value
		if spec.scale == 1.0 and spec.bias == 0.0:
			return value
		return value * spec.scale + spec.bias

	def _log_parse_failure(self, message: str) -> None:
		"""First occurrence WARNING, repeats DEBUG — a broken node must not flood the log."""
		if not self._parse_failing:
			self._parse_failing = True
			self.LOGGER.warning(f"{message}, keeping the last known reading")
		else:
			self.LOGGER.debug(f"{message}, keeping the last known reading")

	def _clear_parse_failure(self) -> None:
		if self._parse_failing:
			self._parse_failing = False
			self.LOGGER.info(f"Uplinks on {self.name} recovered")

	def _log_field_failure(self, name: str, message: str) -> None:
		if name not in self._failed_fields:
			self._failed_fields.add(name)
			self.LOGGER.warning(message)
		else:
			self.LOGGER.debug(message)

	@override
	def get_metrics(self) -> dict[str, Any]:
		"""The uplink keyed by what each field measures, not by its position in the payload.

		Only scalars are reported: a STRING field (a firmware version) would put one
		unchanging string per node into a measurement and buy nothing, and a RAW byte list is
		not a scalar at all. Bools stay — a relay state is a real metric.
		"""
		metrics: dict[str, Any] = {}
		try:
			for name, entry in self.data["fields"].items():
				value = entry["value"]
				if isinstance(value, bool) or isinstance(value, (int, float)):
					metrics[name] = value
		except (AttributeError, KeyError, TypeError) as e:
			self.LOGGER.warning(f"Malformed data on {self.name}: {e}")
			return {}
		return metrics

	@override
	def get_total_energy_kwh(self) -> float | None:
		"""Sum of every field declaring role 'energy_import_kwh'.

		A generic node cannot guess which field holds the total, and guessing by *name* is the
		failure mode worth designing out: a field called "total_energy" would report 0.0
		forever, an algorithm steering on kWh would steer on nothing for the life of the
		deployment, and nothing anywhere would error. Several fields may carry the role — an
		import/export pair, a two-tariff meter — and they are summed, exactly as
		devices/p1.py sums the OBIS tariff registers.
		"""
		total = 0.0
		try:
			fields = self.data["fields"]  # KeyError on a node that never reported
			for name in self._energy_names:
				entry = fields.get(name)
				if entry is None:
					continue  # declared but absent from this uplink
				value = entry["value"]
				if isinstance(value, bool) or not isinstance(value, (int, float)):
					self.LOGGER.warning(f"Non-numeric energy field '{name}' on {self.name}: {value!r}")
					return None
				total += float(value)
		except (KeyError, TypeError) as e:
			self.LOGGER.warning(f"Malformed data on {self.name}: {e}")
			return None
		return total

	@staticmethod
	def _as_optional_int(value: Any) -> Optional[int]:
		if value is None or value == "":
			return None
		try:
			return int(value)
		except (TypeError, ValueError):
			return None

	@staticmethod
	def _as_int(value: Any, default: int) -> int:
		try:
			return int(value)
		except (TypeError, ValueError):
			return default

	@staticmethod
	def _as_float(value: Any, default: float) -> float:
		try:
			return float(value)
		except (TypeError, ValueError):
			return default
