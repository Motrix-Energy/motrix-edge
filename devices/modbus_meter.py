from dataclasses import dataclass
from json import JSONDecodeError, loads
from struct import error as StructError, pack, unpack
from typing import Any, Optional, override

from api.capabilities import EnergyMeter, MetricSource
from api.device import Device

# Register widths in 16-bit words. Deliberately duplicated in connectors/modbus_tcp.py
# rather than shared through an import: a device must work with any connector and a
# connector with any device, so neither package may import the other. Keep the two in step
# — tests/test_modbus_meter.py asserts they agree.
WORDS: dict[str, int] = {
	"INT16": 1, "UINT16": 1, "RAW": 1,
	"INT32": 2, "UINT32": 2, "FLOAT32": 2,
	"INT64": 4, "UINT64": 4, "FLOAT64": 4,
}

# struct format for the *whole* value, big-endian, once the words are in the right order.
_FORMATS: dict[str, str] = {
	"INT16": ">h", "UINT16": ">H",
	"INT32": ">i", "UINT32": ">I",
	"INT64": ">q", "UINT64": ">Q",
	"FLOAT32": ">f", "FLOAT64": ">d",
}

# The one role a generic meter needs to understand. A closed enum rather than free text so
# a typo is a config-load warning, not a silently-zero energy total six months later.
ROLE_ENERGY_IMPORT_KWH = "energy_import_kwh"


@dataclass(frozen=True)
class RegisterSpec:
	"""One declared register, as the device needs it for decoding."""
	name: str
	data_type: str
	count: int
	scale: float
	unit: Optional[str]
	role: Optional[str]
	word_order: str


class ModbusMeter(Device, EnergyMeter, MetricSource):
	"""A Modbus meter whose register map comes from `config.json`.

	One class covers many meter models: Modbus standardises the framing and standardises
	nothing about what lives at which address, so the map is configuration, not code.

	This module imports **nothing** from pymodbus and decodes raw words with `struct` from
	the stdlib. That is deliberate and load-bearing: it is what lets a Modbus meter be
	replayed from a CSV with `emulates: "modbus_tcp"` on a machine with no Modbus stack
	installed, and what keeps this device's tests running in a core-only checkout.
	"""
	_registers: dict[str, RegisterSpec]
	_energy_import_names: list[str]

	SUPPORTED_PROTOCOLS = ("modbus_tcp",)

	# No UNSERVABLE_PROTOCOLS table: there is no near-miss transport worth a sentence of its
	# own here. Everything this could be misconfigured onto is refused for the reason below.
	PROTOCOL_REFUSAL = (
		"a Modbus meter is polled by connectors/modbus_tcp.py, which serialises the register "
		"words to JSON itself — no other transport produces that payload"
	)

	@override
	def __init__(self, name: str, connector_options: dict[str, Any], listener_options: dict[str, Any], controller_options: dict[str, Any]) -> None:
		super().__init__(name, connector_options, listener_options, controller_options)
		self._parse_failing = False
		self._failed_registers: set[str] = set()
		self._registers = {}
		self._energy_import_names = []
		self._build_specs()

	def _build_specs(self) -> None:
		"""Resolve the declared register map once, warning about anything unusable.

		Never raises: a constructor that throws is contained by main.create_classes, but the
		containment costs the whole device — one bad entry in the map and nothing here
		reports at all. A malformed entry is dropped with a warning; the rest of the meter still works.
		"""
		default_order = str(self.listener_options.get("word_order", "big")).strip().lower()
		if default_order not in ("big", "little"):
			self.LOGGER.warning(f"Unknown word_order '{default_order}' on {self.name}, using 'big'")
			default_order = "big"
		registers = self.listener_options.get("registers")
		if not isinstance(registers, list):
			self.LOGGER.warning(f"No usable listener_options.registers on {self.name}; it will decode nothing")
			return
		for entry in registers:
			if not isinstance(entry, dict):
				continue
			name = entry.get("name")
			if not isinstance(name, str) or not name or name in self._registers:
				continue
			data_type = str(entry.get("data_type", "UINT16")).strip().upper()
			if data_type not in WORDS and data_type != "STRING":
				self.LOGGER.warning(f"Unknown data_type '{data_type}' for register '{name}' on {self.name}, using UINT16")
				data_type = "UINT16"
			order = str(entry.get("word_order", default_order)).strip().lower()
			if order not in ("big", "little"):
				order = default_order
			role = entry.get("role")
			if role is not None and role != ROLE_ENERGY_IMPORT_KWH:
				self.LOGGER.warning(f"Unknown role '{role}' for register '{name}' on {self.name}, ignoring it")
				role = None
			unit = entry.get("unit")
			if role == ROLE_ENERGY_IMPORT_KWH and isinstance(unit, str) and unit.strip().lower() != "kwh":
				# No implicit conversion. `role` is a promise that the *scaled* value is
				# already kWh; converting from `unit` instead would let a typo ("Wh",
				# "KWh") silently rescale a revenue reading by a factor of 1000.
				self.LOGGER.warning(
					f"Register '{name}' on {self.name} declares role '{ROLE_ENERGY_IMPORT_KWH}' but unit '{unit}'. "
					f"The role means the scaled value is already kWh — no conversion is applied; fix `scale` instead."
				)
			self._registers[name] = RegisterSpec(
				name=name,
				data_type=data_type,
				count=self._as_int(entry.get("count"), WORDS.get(data_type, 1)),
				scale=self._as_float(entry.get("scale"), 1.0),
				unit=unit if isinstance(unit, str) else None,
				role=role,
				word_order=order,
			)
			if role == ROLE_ENERGY_IMPORT_KWH:
				self._energy_import_names.append(name)
		if self._registers and not self._energy_import_names:
			# Said once at startup rather than never: get_total_energy_kwh() reports 0.0
			# for a meter with no energy role, which is exactly what the capability
			# contract prescribes and is also indistinguishable from a real zero.
			self.LOGGER.info(
				f"No register on {self.name} declares role '{ROLE_ENERGY_IMPORT_KWH}'; "
				f"get_total_energy_kwh() will report 0.0"
			)

	@staticmethod
	def _as_int(value: Any, default: int) -> int:
		try:
			return int(str(value).strip())
		except (TypeError, ValueError):
			return default

	@staticmethod
	def _as_float(value: Any, default: float) -> float:
		try:
			number = float(str(value).strip())
		except (TypeError, ValueError):
			return default
		return number if number == number and abs(number) != float("inf") else default

	@override
	def receive(self, *args, **kwargs) -> Optional[bool]:
		if self.refuse_unserved_protocol(*args, **kwargs):
			return False
		# Tolerate the (topic, payload) arity as well as (payload,). The live connector always
		# sends one argument, but a replay CSV row whose `topic` column is non-empty makes
		# PseudoConnector call with two — so a device that accepted only one shape would raise
		# TypeError on every such row. `Connector.deliver` catches that now and logs one
		# traceback against this device, but it is still a backtest that produces no readings.
		if len(args) == 2:
			self.LOGGER.debug(f"Ignoring topic '{args[0]}' on {self.name}: modbus_tcp payloads carry no topic")
		if not args:
			self.LOGGER.warning(f"Empty receive() call on {self.name}")
			return False
		return self.receive_modbus_tcp(args[-1])

	def receive_modbus_tcp(self, payload: str) -> bool:
		"""Decode `{"blocks": {name: [words...]}}` into `self.data`.

		The wire format is a JSON string rather than a dict so the live path and the replay
		path are byte-identical: PseudoConnector replays strings from a CSV, so a payload
		cell is literally what the connector produced. A dict would give this device two
		parse paths, and the replay one — the one every backtest and regression fixture
		uses — would be the one never exercised against hardware.
		"""
		try:
			message = loads(payload)
			blocks = message["blocks"]
			if not isinstance(blocks, dict) or not blocks:
				raise ValueError("empty or non-object 'blocks'")
		except (JSONDecodeError, KeyError, TypeError, ValueError) as e:
			# Keep the last good reading. Assigning the empty result would turn an
			# unreadable frame into a reading *of* nothing, which the framework then
			# publishes to algorithms and writes to storage. Losing a sample is
			# acceptable; inventing one is not.
			self._log_parse_failure(f"Unreadable payload on {self.name}: {e}")
			return False

		decoded: dict[str, dict[str, Any]] = {}
		for name, words in blocks.items():
			spec = self._registers.get(name)
			if spec is None:
				self.LOGGER.debug(f"Ignoring undeclared register '{name}' on {self.name}")
				continue
			value = self._decode(spec, words)
			if value is None:
				continue  # already warned, edge-triggered per register
			entry: dict[str, Any] = {"value": value, "raw": list(words)}
			if spec.unit:
				entry["unit"] = spec.unit
			decoded[name] = entry

		if not decoded:
			self._log_parse_failure(f"No decodable register in the payload on {self.name}")
			return False
		self._clear_parse_failure()
		# Replaced wholesale rather than merged, like devices/p1.py: a partial poll must
		# not leave a stale register beside a fresh one under one timestamp, because
		# nothing downstream can tell the two apart.
		self.data = {"registers": decoded}
		self.LOGGER.debug(f"{self.data=}")
		return True

	def _decode(self, spec: RegisterSpec, words: Any) -> Any:
		"""One register's words to a Python value, or None (having warned once)."""
		if not isinstance(words, list) or not words:
			self._log_register_failure(spec.name, f"Register '{spec.name}' on {self.name} carried no words")
			return None
		try:
			if all(isinstance(word, bool) for word in words):
				value: Any = words[0] if len(words) == 1 else list(words)
			elif spec.data_type == "RAW":
				value = list(words)
			elif spec.data_type == "STRING":
				value = self._decode_string(words)
			else:
				value = self._decode_number(spec, words)
		except (KeyError, StructError, TypeError, UnicodeDecodeError, ValueError) as e:
			self._log_register_failure(spec.name, f"Cannot decode register '{spec.name}' on {self.name}: {e}")
			return None
		self._failed_registers.discard(spec.name)
		return value

	def _decode_number(self, spec: RegisterSpec, words: list[int]) -> Any:
		needed = WORDS.get(spec.data_type, 1)
		if len(words) < needed:
			raise ValueError(f"needs {needed} word(s), got {len(words)}")
		# Word order, not byte order: vendors disagree about which 16-bit half of a 32-bit
		# value comes first, and both halves are big-endian internally either way. Declared
		# per device (and overridable per register) rather than per connector, because two
		# meters behind one gateway routinely disagree — so it cannot be a property of the
		# TCP transport.
		ordered = words[:needed] if spec.word_order == "big" else list(reversed(words[:needed]))
		raw = b"".join(pack(">H", word & 0xFFFF) for word in ordered)
		number = unpack(_FORMATS[spec.data_type], raw)[0]
		# Meters publish integers with an implied exponent, and datasheets state the
		# multiplier ("LSB = 0.01 kWh"), so scale is a multiplier here too — no mental
		# inversion at config time.
		return number * spec.scale if spec.scale != 1.0 else number

	@staticmethod
	def _decode_string(words: list[int]) -> str:
		raw = b"".join(pack(">H", word & 0xFFFF) for word in words)
		return raw.decode("ascii", errors="replace").rstrip("\x00 ").strip()

	def _log_parse_failure(self, message: str) -> None:
		"""First occurrence WARNING, repeats DEBUG — a broken meter must not flood the log."""
		if not self._parse_failing:
			self._parse_failing = True
			self.LOGGER.warning(f"{message}, keeping the last known reading")
		else:
			self.LOGGER.debug(f"{message}, keeping the last known reading")

	def _clear_parse_failure(self) -> None:
		if self._parse_failing:
			self._parse_failing = False
			self.LOGGER.info(f"Readings on {self.name} recovered")

	def _log_register_failure(self, name: str, message: str) -> None:
		if name not in self._failed_registers:
			self._failed_registers.add(name)
			self.LOGGER.warning(message)
		else:
			self.LOGGER.debug(message)

	@override
	def get_metrics(self) -> dict[str, Any]:
		"""The reading keyed by what each register measures, not by its position.

		Only scalars are reported: a STRING register (a serial number) would put one
		unchanging string field per meter into a measurement and buy nothing, and a RAW
		word list is not a scalar at all. Bools stay — a relay state is a real metric.
		"""
		metrics: dict[str, Any] = {}
		try:
			for name, entry in self.data["registers"].items():
				value = entry["value"]
				if isinstance(value, bool) or isinstance(value, (int, float)):
					metrics[name] = value
		except (AttributeError, KeyError, TypeError) as e:
			self.LOGGER.warning(f"Malformed data on {self.name}: {e}")
			return {}
		return metrics

	@override
	def get_total_energy_kwh(self) -> float | None:
		"""Sum of every register declaring role 'energy_import_kwh'.

		A generic meter cannot guess which register holds the total, and guessing by *name*
		is the failure mode worth designing out: a meter whose register is called
		"total_active_energy" would report 0.0 forever, an algorithm steering on kWh would
		steer on nothing for the life of the deployment, and nothing anywhere would error.
		The role is declared, its enum is closed, and a typo is caught at config load.

		Several registers may carry the role — a two-tariff meter has T1 and T2 — and they
		are summed, exactly as devices/p1.py sums the OBIS tariff registers.
		"""
		total = 0.0
		try:
			registers = self.data["registers"]  # KeyError on a device that never received
			for name in self._energy_import_names:
				entry = registers.get(name)
				if entry is None:
					continue  # declared but absent from this poll
				value = entry["value"]
				if isinstance(value, bool) or not isinstance(value, (int, float)):
					self.LOGGER.warning(f"Non-numeric energy register '{name}' on {self.name}: {value!r}")
					return None
				total += float(value)
		except (KeyError, TypeError) as e:
			self.LOGGER.warning(f"Malformed data on {self.name}: {e}")
			return None
		return total
