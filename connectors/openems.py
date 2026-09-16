from json import JSONDecodeError, loads
from logging import getLogger
from typing import Any, Optional, override

import requests

from api.capabilities import Switch
from api.device import Device
from api.options import int_option
from connectors.http_api import HttpApiConnector

# The Edge's REST controller (io.openems.edge.controller.api.rest) default port.
_DEFAULT_PORT = 8084

# The Basic-auth username the Edge ignores. OpenEMS authenticates on the *password*, which
# is a user role, so the username half is conventional filler — "x" is what the OpenEMS
# documentation uses in its own examples.
_USERNAME = "x"


class OpenemsConnector(HttpApiConnector):
	"""Polls an OpenEMS Edge's REST channel API.

	Interop rather than competition: an OpenEMS installation already drives inverters,
	batteries and EV chargers, and one connector wrapping its Edge exposes all of them as
	EMS devices without reimplementing a single hardware driver.

	Subclasses `HttpApiConnector` rather than reimplementing it — an OpenEMS Edge is an
	HTTP endpoint polled on an interval, so the poll loop, the per-device schedule, the
	bounded interruptible sleep and the edge-triggered failure/recovery logging are already
	correct here. Exactly three things are OpenEMS-specific: how the URL is spelled, how a
	device's component+channels become a path (`resolve_endpoint`), and that a write is
	`{"value": ...}` as JSON rather than a raw body (`send`).

	Note for anyone adding an option: `tests/test_config.py`'s schema-lockstep check
	inspects *this* class's `__init__`, not the parent's, so an inherited option that
	`openems.schema.json` declares has to be an explicit parameter here and forwarded to
	`super()`. `**kwargs` does not satisfy the subset check.
	"""

	@override
	def __init__(self, name: str, host: Optional[str] = None, port: Any = _DEFAULT_PORT,
				 password: str = "user", scheme: str = "http", base_url: Optional[str] = None,
				 default_interval: Any = 10, timeout: Any = 30, verify_ssl: Any = True) -> None:
		# self.LOGGER does not exist until Connector.__init__ runs, but base_url has to be
		# built from host/port before super().__init__() is called — and coercing the port
		# needs a logger. getLogger is a registry, so this is the *same object*
		# Connector.__init__ retrieves a moment later; nothing is duplicated.
		logger = getLogger(f"{self.__class__.__name__}/{name}")
		if not base_url:
			resolved_port = int_option(logger, "port", port, _DEFAULT_PORT, minimum=1, maximum=65535)
			base_url = f"{scheme}://{host}:{resolved_port}" if host else ""
		self.host = host
		self.password = password
		super().__init__(
			name,
			base_url=base_url,
			# The Edge ignores the username and authenticates on the password, which is an
			# OpenEMS user ROLE: guest | user | owner | admin. It is a privilege level, not
			# a per-user secret — `user` is enough to read, `admin` grants writes to every
			# channel on the installation.
			auth={"username": _USERNAME, "password": password},
			default_interval=default_interval,
			timeout=timeout,
			verify_ssl=verify_ssl,
		)

	@override
	def resolve_endpoint(self, device: Device) -> Optional[str]:
		"""`/rest/channel/{component}/{channels}` from what the device declares.

		`channels` is a **regex** on the Edge side (it matches with `Pattern.matches`), so a
		list is joined into one alternation and fetched in a single request rather than one
		request per channel. Omitting it polls every channel of the component.
		"""
		component = device.listener_options.get("component")
		if not component:
			self.LOGGER.warning(f"Device '{device.name}' has no listener_options.component, skipping polling")
			return None
		channels = device.listener_options.get("channels")
		if isinstance(channels, list):
			usable = [str(channel) for channel in channels if channel]
			pattern = "|".join(usable) if usable else ".*"
		else:
			pattern = str(channels) if channels else ".*"
		return f"/rest/channel/{component}/{pattern}"

	@override
	def send(self, device: Device, payload: str) -> None:
		"""POST `{"value": ...}` to a writable channel.

		Overridden rather than inherited because the parent sends the payload as a raw body
		(`data=payload`) and the Edge requires a JSON object with a `value` key — a missing
		one is an explicit error on its side.
		"""
		options = device.controller_options
		component = options.get("component")
		channel = options.get("channel")
		if not component or not channel:
			self.LOGGER.warning(f"Device '{device.name}' has no controller_options.component/channel, cannot send")
			return

		# Every conversion is inside the try, not just the request: send() runs on an
		# *algorithm's* thread and nothing in Algorithm.control_device -> DevicesManager
		# .control -> Device.control -> here catches, so a ValueError from coercing an
		# operator's typo would be counted as an algorithm crash — five restarts, backoff,
		# CRITICAL. The parent's send() guards only the requests call, which is why this
		# override cannot simply delegate.
		try:
			value = self._resolve_value(payload, options)
			url = f"{self.base_url}/rest/channel/{component}/{channel}"
			response = self._session.post(url, json={"value": value}, timeout=self.timeout)
			response.raise_for_status()
			self.LOGGER.info(f"Set {component}/{channel} to {value!r} for '{device.name}' (status {response.status_code})")
		except requests.HTTPError as e:
			self.LOGGER.warning(f"HTTP {e.response.status_code} setting {component}/{channel} for '{device.name}'")
		except requests.RequestException as e:
			self.LOGGER.error(f"Error setting {component}/{channel} for '{device.name}': {e}")
		except (JSONDecodeError, TypeError, ValueError) as e:
			self.LOGGER.error(f"Cannot turn command '{payload}' for '{device.name}' into a channel value: {e}")

	@staticmethod
	def _resolve_value(payload: str, options: dict[str, Any]) -> Any:
		"""The JSON value a command writes.

		`on_value`/`off_value` default to true/false, so a relay channel needs no config —
		but setting them to numbers is what makes a setpoint expressible: an ESS charging at
		3 kW is `on_value: -3000` on `ess0/SetActivePowerEquals`, and the algorithm still
		only says "on".
		"""
		token = payload.strip().lower()
		if token == Switch.COMMAND_ON:
			return options.get("on_value", True)
		if token == Switch.COMMAND_OFF:
			return options.get("off_value", False)
		# Anything else is a literal: a bare number, or any JSON scalar. loads() rather
		# than float() so `true` and `"MANUAL"` both survive; a non-JSON token falls back
		# to the string it already is, which is what a mode channel wants.
		try:
			return loads(payload)
		except (JSONDecodeError, TypeError):
			return payload
