from json import JSONDecodeError, loads
from typing import Any, Optional, override

from api.capabilities import EnergyMeter, MetricSource
from api.device import Device

# OpenEMS cumulative-energy channels (_sum/GridBuyActiveEnergy, _sum/EssActiveChargeEnergy,
# ...) are Watt-hours. kWh is what EnergyMeter promises, so Wh is the default here.
_ENERGY_DIVISORS: dict[str, float] = {"wh": 1000.0, "kwh": 1.0, "mwh": 0.001}

# OpenEMS channel addresses read "component/Channel". The slash is replaced in metric names
# because a storage field separator is conventionally "." and a name carrying a path
# separator reads badly in a query — the same reason devices/p1.py chose brackets over dots
# for its repeated OBIS registers.
_ADDRESS_SEPARATOR = "_"


class Openems(Device, MetricSource, EnergyMeter):
	"""One OpenEMS component's channels, as an EMS device.

	An Edge exposes every channel of every component it drives over one REST surface, so a
	device here is a *view*: a component id plus a channel regex. `_sum` gives the whole
	installation's aggregates, `ess0` a battery, `meter0` a meter.

	Algorithms consume it by capability like any other device, which is the point: an
	OpenEMS-driven battery and a P1 meter are interchangeable to an algorithm that only
	asks for an `EnergyMeter`.
	"""

	@override
	def __init__(self, name: str, connector_options: dict[str, Any], listener_options: dict[str, Any], controller_options: dict[str, Any]) -> None:
		super().__init__(name, connector_options, listener_options, controller_options)
		self._parse_failing = False
		self._energy_warned = False

	@override
	def receive(self, *args, **kwargs) -> Optional[bool]:
		match self.connector_options["protocol"]:
			case "openems":
				if len(args) == 2:
					self.LOGGER.debug(f"Ignoring topic '{args[0]}' on {self.name}: openems payloads carry no topic")
				if not args:
					self.LOGGER.warning(f"Empty receive() call on {self.name}")
					return False
				return self.receive_openems(args[-1])
			case _:
				self.LOGGER.error(f"Unknown protocol {self.connector_options['protocol']} for {self.name}")
				self.LOGGER.debug(f"{self.connector_options=}, {args=}, {kwargs=}")
				raise NotImplementedError(f"Protocol {self.connector_options['protocol']} not implemented for {self.name}")

	def receive_openems(self, payload: str) -> bool:
		"""Parse a `/rest/channel/...` response body into `self.data`.

		The Edge returns a single JSON **object** when exactly one channel matched the
		regex and a JSON **array** when several did — the shape depends on the data, not on
		the request, so both are handled. Getting this wrong is the failure that only shows
		up once a component happens to expose one channel matching the filter.
		"""
		try:
			body = loads(payload)
		except (JSONDecodeError, TypeError) as e:
			# Keep the last good reading: an Edge that 404s an HTML error page, or a proxy
			# that returns a login form, must not become a reading of nothing.
			self._log_parse_failure(f"Unreadable response on {self.name}: {e}")
			return False

		entries = body if isinstance(body, list) else [body]
		channels: dict[str, dict[str, Any]] = {}
		for entry in entries:
			if not isinstance(entry, dict):
				continue
			address = entry.get("address")
			if not isinstance(address, str) or not address:
				continue
			channel: dict[str, Any] = {"value": entry.get("value")}
			for key in ("unit", "type", "accessMode", "text"):
				if entry.get(key):
					channel[key] = entry[key]
			channels[address] = channel

		if not channels:
			self._log_parse_failure(f"No usable channel in the response on {self.name}")
			return False
		self._clear_parse_failure()
		self.data = {"channels": channels}
		self.LOGGER.debug(f"{self.data=}")
		return True

	def _log_parse_failure(self, message: str) -> None:
		"""First occurrence WARNING, repeats DEBUG — a broken Edge must not flood the log."""
		if not self._parse_failing:
			self._parse_failing = True
			self.LOGGER.warning(f"{message}, keeping the last known reading")
		else:
			self.LOGGER.debug(f"{message}, keeping the last known reading")

	def _clear_parse_failure(self) -> None:
		if self._parse_failing:
			self._parse_failing = False
			self.LOGGER.info(f"Readings on {self.name} recovered")

	@staticmethod
	def metric_name(address: str) -> str:
		"""`_sum/EssSoc` -> `_sum_EssSoc`: a stable name with no path separator in it."""
		return address.replace("/", _ADDRESS_SEPARATOR)

	@override
	def get_metrics(self) -> dict[str, Any]:
		"""Every numeric channel, keyed by its address.

		Only scalars: a channel whose value is a string (a state text, an equipment id) or a
		list would put a non-measurement field into a measurement and buy nothing. Bools
		stay — a relay channel is a real metric.
		"""
		metrics: dict[str, Any] = {}
		try:
			for address, channel in self.data["channels"].items():
				value = channel["value"]
				if isinstance(value, bool) or isinstance(value, (int, float)):
					metrics[self.metric_name(address)] = value
		except (AttributeError, KeyError, TypeError) as e:
			self.LOGGER.warning(f"Malformed data on {self.name}: {e}")
			return {}
		return metrics

	@override
	def get_total_energy_kwh(self) -> float | None:
		"""The channel named by `listener_options.energy_channel`, converted to kWh.

		Named explicitly rather than guessed. An Edge exposes several plausible cumulative
		channels (`_sum/GridBuyActiveEnergy`, `_sum/EssActiveChargeEnergy`,
		`_sum/ProductionActiveEnergy`) and they mean different things — picking one by
		heuristic would give an algorithm a number that looks right and steers wrong.

		Reports 0.0 when nothing is configured, which is what the capability contract
		prescribes for well-formed data holding no kWh register, so a `_sum` view stays
		harmless in an algorithm that sums every EnergyMeter it can see.
		"""
		address = self.listener_options.get("energy_channel")
		if not address:
			return 0.0
		if not self.data:
			return None
		try:
			channel = self.data["channels"].get(address)
		except (AttributeError, KeyError, TypeError) as e:
			self.LOGGER.warning(f"Malformed data on {self.name}: {e}")
			return None
		if channel is None:
			self._warn_energy_once(
				f"Channel '{address}' is not in the reading on {self.name}; "
				f"is it matched by listener_options.channels?"
			)
			return None
		value = channel.get("value")
		if isinstance(value, bool) or not isinstance(value, (int, float)):
			self._warn_energy_once(f"Non-numeric energy channel '{address}' on {self.name}: {value!r}")
			return None

		unit = self.listener_options.get("energy_unit") or "Wh"
		divisor = _ENERGY_DIVISORS.get(str(unit).strip().lower())
		if divisor is None:
			self._warn_energy_once(f"Unknown energy unit '{unit}' on {self.name}, expected one of {sorted(_ENERGY_DIVISORS)}")
			return None
		self._energy_warned = False
		return float(value) / divisor

	def _warn_energy_once(self, message: str) -> None:
		if not self._energy_warned:
			self._energy_warned = True
			self.LOGGER.warning(message)
		else:
			self.LOGGER.debug(message)
