from typing import Any, Optional, override

from api.capabilities import EnergyMeter, MetricSource
from api.device import Device
from api.parser import Parser
from parsers.obis import OBISParser

# Readable names for the registers a Belgian/Dutch P1 port actually emits. An OBIS code
# reads A-B:C.D.E — medium, channel, quantity, measurement type, tariff — so 1-0:1.8.1 is
# "electricity, local channel, positive active energy, cumulative, tariff 1". Anything not
# listed keeps its raw code, so a vendor extension is still recorded, just unnamed.
OBIS_ALIASES: dict[str, str] = {
	"1-0:1.8.1": "energy_import_t1_kwh",
	"1-0:1.8.2": "energy_import_t2_kwh",
	"1-0:2.8.1": "energy_export_t1_kwh",
	"1-0:2.8.2": "energy_export_t2_kwh",
	"1-0:1.7.0": "power_import_kw",
	"1-0:2.7.0": "power_export_kw",
	"1-0:21.7.0": "power_import_l1_kw",
	"1-0:41.7.0": "power_import_l2_kw",
	"1-0:61.7.0": "power_import_l3_kw",
	"1-0:22.7.0": "power_export_l1_kw",
	"1-0:42.7.0": "power_export_l2_kw",
	"1-0:62.7.0": "power_export_l3_kw",
	"1-0:31.7.0": "current_l1_a",
	"1-0:51.7.0": "current_l2_a",
	"1-0:71.7.0": "current_l3_a",
	"1-0:32.7.0": "voltage_l1_v",
	"1-0:52.7.0": "voltage_l2_v",
	"1-0:72.7.0": "voltage_l3_v",
	"1-0:31.4.0": "current_limit_a",
	"0-0:96.14.0": "tariff_indicator",
	"0-0:96.3.10": "breaker_state",
	"0-0:17.0.0": "power_limit_kw",
	"0-1:24.2.3": "gas_m3",
	"0-1:24.4.0": "gas_valve_state",
}

# Distinguishes "called with one argument" from "called with two, the second of which is
# None" — see `receive_mqtt`. A module-level object, because `None` is a value a caller can
# legitimately pass and therefore cannot double as the absence of one.
_NO_PAYLOAD = object()


class P1(Device, EnergyMeter, MetricSource):
	# The one transport that can carry a DSMR telegram to this device, and therefore the one
	# this device serves. A `pseudo` connector declaring `emulates: "mqtt"` reaches here as
	# "mqtt" — Config resolves that before injecting it — so a replay is covered by this entry
	# and needs no branch of its own.
	#
	# There are deliberately **no defaults** behind it: no default topic, no default
	# subscription, no default routing pattern. A P1 has only addressing options, and there is
	# no standard P1-over-MQTT topic — dsmr2mqtt, Tasmota's `tele/<name>/SENSOR`, HomeWizard and
	# every hand-rolled ESP8266 reader choose their own, and the operator chooses again on top.
	# A synthesised default would therefore not fail loudly; it would compile a routing regex
	# that matches a *different* meter's telegram on the same broker and file its readings under
	# this device's name. `receive_mqtt` already states the principle for a payload — losing a
	# sample is acceptable, inventing one is not — and it holds the same way for a reading
	# attributed to the wrong meter. So the config's values are used verbatim, or nothing is.
	#
	# This is why `devices/lora.py`'s `profile` idiom does not transfer. That one defaults
	# *interpretation* — where a payload sits inside ChirpStack's uplink envelope is a vendor
	# fact, identical in every deployment, so a wrong guess is wrong everywhere and loudly.
	# `connectors/lorawan.py` and `devices/lora_switch.py` both refuse to default *addressing*
	# for exactly the reason above, and addressing is all a P1 has. Its "profile" already
	# exists, is called `OBISParser`, and is hardcoded because DSMR has one.
	SUPPORTED_PROTOCOLS = ("mqtt",)

	PROTOCOL_REFUSAL = "a P1 meter carries a raw DSMR telegram, which reaches a device over 'mqtt'"

	# Both LoRa transports get the same sentence, and it earns its place: a generic message
	# could not tell an operator that the limit is arithmetic rather than a missing feature.
	# A DSMR telegram is roughly 700-1000 bytes; the largest LoRaWAN application payload is
	# 51 bytes at SF12 and 222 at SF7, so there is no data rate at which one fits. A "P1 over
	# LoRa" gateway sends a *summary* — a different payload with a different schema — which is
	# why the fix is a different device kind and not a different option.
	UNSERVABLE_PROTOCOLS = {
		"lora": "a telegram is 700-1000 bytes against a 51-222 byte LoRaWAN payload, so no data rate carries one; a 'P1 over LoRa' gateway sends a summary instead, which is a different schema. Use kind 'lora' with a field map",
		"lorawan": "a telegram is 700-1000 bytes against a 51-222 byte LoRaWAN payload, so no data rate carries one; a 'P1 over LoRa' gateway sends a summary instead, which is a different schema. Use kind 'lora' with a field map",
	}

	PARSER: Parser

	@override
	def __init__(self, name: str, connector_options: dict[str, Any], listener_options: dict[str, Any], controller_options: dict[str, Any]) -> None:
		super().__init__(name, connector_options, listener_options, controller_options)
		self.PARSER = OBISParser()
		self._parse_failing = False

	def update_data(self, data: dict[str, Any]) -> None:
		self.LOGGER.info(f"Updated data for {self.name}")
		self.data = data

	@override
	def get_total_energy_kwh(self) -> float | None:
		total: float = 0
		try:
			for entry in self.data["data"]:
				if entry["obis"]["medium"] == 1 and entry["obis"]["channel"] == 0 and entry["obis"]["class"] == 1 and entry["obis"]["instance"] == 8 and entry["obis"]["attribute"] == 1:
					for value in entry["data"]:
						if value.get("unit") == "kWh":
							total += value["value"]
		except (KeyError, TypeError) as e:
			self.LOGGER.warning(f"Malformed data on {self.name}: {e}")
			return None
		return total

	@staticmethod
	def obis_code(obis: dict[str, Any]) -> str:
		"""Rebuild the standard A-B:C.D.E reference from the five parsed parts.

		str() rather than assuming int: the parser's groups are `\\w+`, so a vendor code
		carrying letters comes back as a string.
		"""
		return f"{obis['medium']}-{obis['channel']}:{obis['class']}.{obis['instance']}.{obis['attribute']}"

	@override
	def get_metrics(self) -> dict[str, Any]:
		"""The telegram keyed by what each register measures, not by its position.

		Only numeric value blocks are reported. That is what makes the multi-block
		registers tractable — 0-1:24.2.3 carries a capture timestamp *and* the gas
		reading, so filtering to numbers lets `gas_m3` mean the gas reading — and it
		keeps identity registers (equipment id, text message) out of a measurement,
		where they would put a string field per meter and buy nothing.

		Values are left exactly as the meter reported them; summing tariffs is
		`get_total_energy_kwh()`'s job, not this one's.
		"""
		grouped: dict[str, list[Any]] = {}
		try:
			for entry in self.data["data"]:
				name = OBIS_ALIASES.get(self.obis_code(entry["obis"]), self.obis_code(entry["obis"]))
				for block in entry["data"]:
					value = block["value"]
					if isinstance(value, bool) or not isinstance(value, (int, float)):
						continue
					grouped.setdefault(name, []).append(value)
		except (KeyError, TypeError) as e:
			self.LOGGER.warning(f"Malformed data on {self.name}: {e}")
			return {}
		metrics: dict[str, Any] = {}
		for name, values in grouped.items():
			if len(values) == 1:
				metrics[name] = values[0]
			else:
				# Brackets, not dots: the storage field separator defaults to "." and an
				# OBIS code already contains dots.
				for index, value in enumerate(values):
					metrics[f"{name}[{index}]"] = value
		return metrics

	@override
	def receive(self, *args, **kwargs) -> Optional[bool]:
		"""Refuse a protocol this device cannot serve, then parse.

		There is no `match` here and no `raise`. The refusal is `Device`'s — see
		`refuse_unserved_protocol`, and `SUPPORTED_PROTOCOLS` above for what this device
		claims — and returning False produces a gap rather than an invented reading, which is
		`Device.receive`'s documented contract for a payload that yielded nothing. The ERROR
		an operator acts on was already logged once, at construction.
		"""
		if self.refuse_unserved_protocol(*args, **kwargs):
			return False
		return self.receive_mqtt(*args, **kwargs)

	def receive_mqtt(self, payload_or_topic: str, payload: Any = _NO_PAYLOAD) -> bool:
		"""Parse a telegram, whether or not the caller passed a topic alongside it.

		Both arities, because `PseudoConnector` chooses between them per row: a replay row
		with a non-empty `topic` column calls `receive(topic, payload)` and one without calls
		`receive(payload)`. This accepted only the two-argument form, so a topic-less row
		produced a `TypeError` that the replay loop caught and logged as a bad entry — one
		ERROR per row, no readings, and a gap indistinguishable from a device that was never
		wired. CONTRIBUTING.md's device recipe states the rule ("accept **both** arities even
		if your connector only ever sends one"), and `devices/modbus_meter.py` and
		`devices/lora.py` both already guard it.

		A sentinel rather than `payload is None`, because the two are not the same question.
		`csv.DictReader` pads a short row with `None`, so a replay line truncated after its
		`topic` column reaches here as `receive_mqtt(topic, None)` — and reading that as the
		one-argument form would parse the *topic* as a telegram and blame the meter for an
		unreadable one. The row is malformed, not the meter.

		The topic is not read. Routing happened in the connector, against
		`listener_options.pattern`, before this was called.
		"""
		telegram = payload_or_topic if payload is _NO_PAYLOAD else payload
		if not isinstance(telegram, str):
			self.LOGGER.warning(f"No telegram in the payload handed to {self.name}, nothing to parse")
			return False
		parsed = self.PARSER.parse(telegram)
		if not parsed:
			# Keep the last good reading. Assigning the empty result would turn an
			# unreadable telegram — a CRC error on a noisy line is routine — into a
			# reading of nothing, which the framework then publishes to algorithms and
			# writes to storage. Losing a sample is acceptable; inventing one is not.
			# The parser has already logged why; this says what it cost, once.
			if not self._parse_failing:
				self._parse_failing = True
				self.LOGGER.warning(f"Unreadable telegram on {self.name}, keeping the last known reading")
			else:
				self.LOGGER.debug(f"Unreadable telegram on {self.name}, keeping the last known reading")
			return False
		if self._parse_failing:
			self._parse_failing = False
			self.LOGGER.info(f"Telegrams on {self.name} recovered")
		self.data = parsed
		self.LOGGER.debug(f"{self.data=}")
		return True

	def control_mqtt(self, action: str) -> bool:
		# Report what happened, not what was attempted. A P1 meter is never writable, so
		# `control` always refuses and the old unconditional "Controlled ..." line was a
		# claim this method has never once been able to make.
		accepted = self.control(action)
		if accepted:
			self.LOGGER.info(f"Controlled {self.name} with action {action}")
		return accepted
