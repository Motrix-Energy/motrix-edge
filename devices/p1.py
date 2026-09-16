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


class P1(Device, EnergyMeter, MetricSource):
	PARSER: Parser

	@override
	def __init__(self, name: str, connector_options: dict[str, Any], listener_options: dict[str, Any], controller_options: dict[str, Any]) -> None:
		super().__init__(name, connector_options, listener_options, controller_options)
		self.PARSER = OBISParser()
		self._parse_failing = False

	# TODO: depending on what connector is used and if options are specified and passed from the config, either :
	# - use the known defaults for the connector but with overriding options
	# - use the known defaults for the connector
	# - use only the options passed from the config if the connector doesn't have defaults
	# - log an error telling it's not implemented and raise an exception to prevent the device from being created
	# note : idk about the exception, i might find another way because handling exceptions is quite some gymnastics, ideally i'd prevent it from happening even earlier

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
		match self.connector_options["protocol"]:
			case "mqtt":
				return self.receive_mqtt(*args, **kwargs)
			case "lora":
				return self.receive_lora(*args, **kwargs)
			case _:
				self.LOGGER.error(f"Unknown protocol {self.connector_options['protocol']} for {self.name}")
				self.LOGGER.debug(f"{self.connector_options=}, {args=}, {kwargs=}")
				raise NotImplementedError(f"Protocol {self.connector_options['protocol']} not implemented for {self.name}")

	def receive_mqtt(self, topic: str, payload: str) -> bool:
		parsed = self.PARSER.parse(payload)
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

	def receive_lora(self, *args, **kwargs) -> bool:
		"""A P1 telegram cannot cross a LoRa link, and that is permanent rather than pending.

		A DSMR telegram is roughly 700-1000 bytes. The largest LoRaWAN application payload is
		51 bytes at SF12 and 222 at SF7, so there is no data rate at which one fits — a
		"P1 over LoRa" gateway sends a *summary*, which is a different payload with a
		different schema. Use `devices/lora.py` with a field map for it.

		Kept as a branch returning False rather than deleted, deliberately: `case _` raises
		`NotImplementedError`, and `MQTTConnector._receive_and_notify` has no try/except, so
		that exception would die on a bare daemon thread through `threading.excepthook`
		instead of being logged against the device. This says the same thing where an
		operator will actually see it, and produces a gap rather than an invented reading.
		"""
		if not self._parse_failing:
			self._parse_failing = True
			self.LOGGER.error(
				f"{self.name} is a p1 device on the 'lora' protocol, which cannot carry a telegram "
				f"(700-1000 bytes against a 51-222 byte LoRaWAN payload). Use kind 'lora' with a "
				f"field map instead; this device will never produce a reading."
			)
		return False

	def control_mqtt(self, action: str) -> None:
		self.LOGGER.info(f"Controlled {self.name} with action {action}")
		self.control(action)
