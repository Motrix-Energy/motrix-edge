from json import JSONDecodeError, loads
from re import compile
from typing import Any, Optional, override

from api.capabilities import EnergyMeter, MetricSource
from api.device import Device

# The same deliberately strict numeric test storage/influxdb.py uses, for the same reason:
# float() also accepts "nan", "inf", "Infinity" and "1_0" (-> 10.0). Home Assistant states
# are strings *by protocol*, so a broken sensor reporting "nan" would otherwise put a NaN
# into self.data — where every threshold comparison an algorithm makes against it is False,
# silently, and where a CSV row needs special handling to stay parseable.
_NUMERIC = compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")

# Home Assistant's two "there is no reading" states. They are states, not values: treating
# them as data would publish a reading of nothing and overwrite the last good one.
_UNAVAILABLE = frozenset({"unavailable", "unknown", "none", ""})

# On/off-ish state strings worth exposing as a numeric metric so a relay can be charted.
_BOOLEAN_STATES: dict[str, bool] = {
	"on": True, "off": False,
	"open": True, "closed": False,
	"home": True, "not_home": False,
	"locked": False, "unlocked": True,
	"detected": True, "clear": False,
}

# Presentation attributes, not measurements: a dozen unchanging fields per reading per
# entity, forever, if they were flattened generically.
_NOISE_ATTRIBUTES = frozenset({
	"friendly_name", "icon", "entity_picture", "device_class", "state_class",
	"unit_of_measurement", "supported_features", "supported_color_modes", "attribution",
	"editable", "assumed_state", "restored",
})

# Names get_metrics() produces itself. An attribute colliding with one of these would
# silently overwrite the reading.
_RESERVED_METRICS = frozenset({"state", "state_text", "state_on"})

# What a cumulative-energy reading has to be divided by to become kWh.
_ENERGY_DIVISORS: dict[str, float] = {"wh": 1000.0, "kwh": 1.0, "mwh": 0.001}


class HaEntity(Device, MetricSource, EnergyMeter):
	"""One Home Assistant entity, as an EMS device.

	Home Assistant's `state` is a **string by protocol** — "on", "23.4", "unavailable" —
	so this device does the numeric work rather than leaving it to a storage backend: a
	generic flattening would make a temperature a string field that can never be averaged,
	and would flip a field's type mid-stream the moment an entity reports "23.4" and then
	"heat". `get_metrics()` fixes that by construction: `state` is numeric-only and
	`state_text` is always a string, so neither key ever changes type.
	"""

	@override
	def __init__(self, name: str, connector_options: dict[str, Any], listener_options: dict[str, Any], controller_options: dict[str, Any]) -> None:
		super().__init__(name, connector_options, listener_options, controller_options)
		self._parse_failing = False
		self._energy_warned = False

	@override
	def receive(self, *args, **kwargs) -> Optional[bool]:
		match self.connector_options["protocol"]:
			case "home_assistant":
				if len(args) == 2:
					return self.receive_home_assistant(args[0], args[1])
				if len(args) == 1:
					# A replay row with an empty topic column. The state object carries its
					# own entity_id, so this is still usable.
					return self.receive_home_assistant(None, args[0])
				self.LOGGER.warning(f"Unexpected receive args on {self.name}: {args!r}")
				return False
			case _:
				self.LOGGER.error(f"Unknown protocol {self.connector_options['protocol']} for {self.name}")
				self.LOGGER.debug(f"{self.connector_options=}, {args=}, {kwargs=}")
				raise NotImplementedError(f"Protocol {self.connector_options['protocol']} not implemented for {self.name}")

	def receive_home_assistant(self, entity_id: Optional[str], payload: str) -> bool:
		"""Parse one Home Assistant state object into `self.data`.

		The payload is a JSON string rather than a dict so the live path and the replay
		path are byte-identical: PseudoConnector replays a `topic` and a `payload` column,
		both strings, so `emulates: "home_assistant"` reproduces exactly what the live
		connector sends. A dict would give this device a second, replay-only parse path —
		and the backtest would then never exercise the production parser.
		"""
		try:
			state = loads(payload)
			if not isinstance(state, dict):
				raise ValueError("state object is not a JSON object")
			raw = state["state"]
		except (JSONDecodeError, KeyError, TypeError, ValueError) as e:
			self._log_parse_failure(f"Unreadable state on {self.name}: {e}")
			return False

		text = str(raw).strip()
		if text.lower() in _UNAVAILABLE:
			# Not a reading. Keeping the last good one is right: the framework would
			# otherwise republish the previous value under a new timestamp, turning an
			# entity that went offline into a flat line rather than the gap it actually is.
			self._log_parse_failure(f"Entity {entity_id or self.name} is '{text}'")
			return False

		self._clear_parse_failure()
		attributes = state.get("attributes")
		self.data = {
			"entity_id": entity_id or state.get("entity_id"),
			"state": text,
			"state_value": self._as_value(text),
			"attributes": attributes if isinstance(attributes, dict) else {},
			"last_updated": state.get("last_updated"),
		}
		self.LOGGER.debug(f"{self.data=}")
		return True

	@staticmethod
	def _as_value(text: str) -> Any:
		"""A number, a bool, or None — the typed reading behind a Home Assistant state."""
		lowered = text.lower()
		if lowered in _BOOLEAN_STATES:
			return _BOOLEAN_STATES[lowered]
		if _NUMERIC.fullmatch(text):
			return float(text)
		return None

	def _log_parse_failure(self, message: str) -> None:
		"""First occurrence WARNING, repeats DEBUG — an offline entity must not flood."""
		if not self._parse_failing:
			self._parse_failing = True
			self.LOGGER.warning(f"{message}, keeping the last known reading")
		else:
			self.LOGGER.debug(f"{message}, keeping the last known reading")

	def _clear_parse_failure(self) -> None:
		if self._parse_failing:
			self._parse_failing = False
			self.LOGGER.info(f"Readings on {self.name} recovered")

	@override
	def get_metrics(self) -> dict[str, Any]:
		"""The reading as stable, type-stable named scalars."""
		if not self.data:
			return {}
		metrics: dict[str, Any] = {}
		try:
			metrics["state_text"] = self.data["state"]
			value = self.data.get("state_value")
			if isinstance(value, bool):
				metrics["state_on"] = value  # bool before float: bool subclasses int
			elif isinstance(value, float):
				metrics["state"] = value
			allowed = self.listener_options.get("metric_attributes")
			for key, item in (self.data.get("attributes") or {}).items():
				if allowed is not None and key not in allowed:
					continue
				if key in _NOISE_ATTRIBUTES or key in _RESERVED_METRICS:
					continue
				if not isinstance(item, (bool, int, float)):
					continue  # MetricSource is {name: scalar}; hs_color: [30, 60] has no single name
				metrics[key] = item
		except (AttributeError, KeyError, TypeError) as e:
			self.LOGGER.warning(f"Malformed data on {self.name}: {e}")
			return {}
		return metrics

	@override
	def get_total_energy_kwh(self) -> float | None:
		"""Cumulative imported energy, when `listener_options` says where to find it.

		Reports 0.0 rather than None when no energy source is configured — which is exactly
		what the capability contract prescribes for well-formed data holding no kWh
		register, and what keeps a thermostat harmless in an algorithm that sums every
		EnergyMeter it can see. A *configured* source that cannot be read is None, because
		a misconfigured meter reporting 0.0 would silently deflate a site total.
		"""
		if not self.data:
			return None
		attribute = self.listener_options.get("energy_attribute")
		try:
			if attribute:
				raw = (self.data.get("attributes") or {}).get(attribute)
				if raw is None:
					self._warn_energy_once(f"No attribute '{attribute}' on {self.name}")
					return None
				value = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else self._as_value(str(raw).strip())
			elif self.listener_options.get("energy_unit"):
				# The unit alone declares "this entity's own state is the energy reading".
				value = self.data.get("state_value")
			else:
				return 0.0  # no energy source configured: not an energy meter, and harmless
		except (TypeError, ValueError) as e:
			self._warn_energy_once(f"Malformed energy data on {self.name}: {e}")
			return None
		if not isinstance(value, float):
			self._warn_energy_once(f"Non-numeric energy reading on {self.name}: {self.data.get('state')!r}")
			return None

		unit = self.listener_options.get("energy_unit") or (self.data.get("attributes") or {}).get("unit_of_measurement") or "kWh"
		divisor = _ENERGY_DIVISORS.get(str(unit).strip().lower())
		if divisor is None:
			self._warn_energy_once(f"Unknown energy unit '{unit}' on {self.name}, expected one of {sorted(_ENERGY_DIVISORS)}")
			return None
		self._energy_warned = False
		return value / divisor

	def _warn_energy_once(self, message: str) -> None:
		if not self._energy_warned:
			self._energy_warned = True
			self.LOGGER.warning(message)
		else:
			self.LOGGER.debug(message)
