from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Optional, override

import requests
from requests.auth import HTTPBasicAuth

from api.connector import Connector
from api.device import Device
from api.options import bool_option, float_option

# The longest a poll loop will sleep before re-checking the stop event. Sleeps are already
# interruptible — `wait_stop` returns as soon as `stop()` is called — so this only bounds
# how stale a newly injected interval can be, not shutdown latency.
_MAX_SLEEP_SECONDS = 60.0


@dataclass
class PollTask:
	"""One device's polling schedule, resolved once at injection time.

	A dataclass rather than a dict because `last_poll` needs somewhere to live: as a dict
	it had to be a second structure keyed by device name, re-looked-up on every tick.
	"""
	device: Device
	endpoint: str
	interval: float
	method: str
	params: Optional[Any] = None
	body: Optional[Any] = None
	last_poll: float = field(default=0.0, compare=False)

	def next_due(self) -> float:
		"""Monotonic timestamp at which this device should next be polled."""
		return self.last_poll + self.interval


class HttpApiConnector(Connector):
	"""Polls REST endpoints on configurable intervals.

	Each device declares its own endpoint path and optional polling interval
	via listener_options. The connector holds the base URL, global headers/auth,
	and a default polling interval. Algorithms are completely unaware of the
	transport — devices receive plain text responses via device.receive().
	"""
	base_url: str
	default_interval: float
	timeout: float
	verify_ssl: bool
	_session: requests.Session
	_poll_tasks: list[PollTask]
	_failed_devices: set[str]

	@override
	def __init__(self, name: str, base_url: str, headers: Optional[dict[str, str]] = None,
				 auth: Optional[dict[str, str]] = None, default_interval: Any = 60,
				 timeout: Any = 30, verify_ssl: Any = True) -> None:
		super().__init__(name)  # first: the coercion helpers below log through self.LOGGER
		self.base_url = base_url.rstrip('/')
		# Coerced, not taken raw: Config validates the plugin schema *before* resolving
		# ${VAR}, so a "${POLL_SECONDS}" declared `number` in http_api.schema.json still
		# arrives here as a str and would be compared against a float in the poll loop.
		self.default_interval = float_option(self.LOGGER, "default_interval", default_interval, 60.0, minimum=0.0)
		self.timeout = float_option(self.LOGGER, "timeout", timeout, 30.0, minimum=0.0)
		self.verify_ssl = bool_option(self.LOGGER, "verify_ssl", verify_ssl, True)

		self._session = requests.Session()
		if headers:
			self._session.headers.update(headers)
		if auth:
			self._session.auth = HTTPBasicAuth(auth["username"], auth["password"])
		self._session.verify = self.verify_ssl

		self._poll_tasks = []
		self._failed_devices = set()

	def resolve_endpoint(self, device: Device) -> Optional[str]:
		"""The path this device is polled at, or None to skip it (having logged why).

		An overridable seam rather than an inline lookup, so a subclass whose devices
		describe *what* to read rather than *where* — `OpenemsConnector`, where a device
		names an OpenEMS component and a channel regex — synthesizes the path here and
		inherits the whole poll loop, schedule and failure/recovery tracking unchanged.

		The alternatives were both worse. Writing a synthesized endpoint into
		`device.listener_options` mutates the device's declared config, which
		`Device.__deepcopy__` then copies into every snapshot — the exact thing
		`config/config.py` goes out of its way to avoid when it injects `protocol`.
		Delegating first and rebuilding `_poll_tasks` afterwards emits the warning below
		for every device, immediately before the subclass configures them all.
		"""
		endpoint = device.listener_options.get("endpoint")
		if not endpoint:
			self.LOGGER.warning(f"Device '{device.name}' has no listener_options.endpoint, skipping polling")
		return endpoint

	@override
	def inject_devices(self, devices: dict[str, Device]) -> None:
		super().inject_devices(devices)
		self._poll_tasks = []
		for device in devices.values():
			if not device.is_readable:
				continue
			endpoint = self.resolve_endpoint(device)
			if not endpoint:
				continue
			self._poll_tasks.append(PollTask(
				device=device,
				endpoint=endpoint,
				interval=float_option(self.LOGGER, f"{device.name}.interval",
									  device.listener_options.get("interval"), self.default_interval, minimum=0.0),
				method=device.listener_options.get("method", "GET").upper(),
				params=device.listener_options.get("params"),
				body=device.listener_options.get("body"),
			))
		self.LOGGER.info(f"{len(self._poll_tasks)} polling task(s) configured")

	@override
	def start(self) -> None:
		"""Blocking poll loop: each device on its own interval, until stopped."""
		if not self._poll_tasks:
			self.LOGGER.warning("No polling tasks configured, connector idle")
			return

		self.on_connected()
		self.LOGGER.info(f"Starting HTTP polling on {self.base_url} ({len(self._poll_tasks)} device(s))")

		try:
			while not self.is_stopping():
				now = monotonic()
				for task in self._poll_tasks:
					if self.is_stopping():
						break
					if now >= task.next_due():
						self._poll_device(task)
						task.last_poll = monotonic()
				# Sleep until the next device is actually due rather than ticking every
				# second: an hourly device would otherwise cost 3600 no-op wakeups per poll.
				idle = min(task.next_due() for task in self._poll_tasks) - monotonic()
				self.wait_stop(max(0.0, min(idle, _MAX_SLEEP_SECONDS)))
		finally:
			# Closed here rather than in stop(): tearing the session down under an
			# in-flight request from another thread is not safe
			self._session.close()
			self.LOGGER.info("Polling stopped, HTTP session closed")

	def _poll_device(self, task: PollTask) -> None:
		"""Fetch one device's endpoint and hand the response body to it."""
		device = task.device
		url = f"{self.base_url}/{task.endpoint.lstrip('/')}"

		try:
			response = self._session.request(
				method=task.method,
				url=url,
				params=task.params,
				json=task.body,
				timeout=self.timeout,
			)
			response.raise_for_status()

			accepted = device.receive(response.text)
			self.on_device_data_received(device, accepted)

			if device.name in self._failed_devices:
				self.LOGGER.info(f"Device '{device.name}' recovered")
				self._failed_devices.discard(device.name)

		except requests.HTTPError as e:
			self._failed_devices.add(device.name)
			self.LOGGER.warning(f"HTTP {e.response.status_code} polling '{device.name}' at {url}")
		except requests.RequestException as e:
			self._failed_devices.add(device.name)
			self.LOGGER.error(f"Error polling '{device.name}' at {url}: {e}")

	@override
	def send(self, device: Device, payload: str) -> None:
		"""POST (or the configured method) a control command to the device's endpoint."""
		endpoint = device.controller_options.get("endpoint")
		if not endpoint:
			self.LOGGER.warning(f"Device '{device.name}' has no controller_options.endpoint, cannot send")
			return

		url = f"{self.base_url}/{endpoint.lstrip('/')}"
		method = device.controller_options.get("method", "POST").upper()

		try:
			response = self._session.request(
				method=method,
				url=url,
				data=payload,
				timeout=self.timeout,
			)
			response.raise_for_status()
			self.LOGGER.info(f"Sent {method} to {url}: {payload} (status {response.status_code})")
		except requests.HTTPError as e:
			self.LOGGER.warning(f"HTTP {e.response.status_code} sending to '{device.name}' at {url}")
		except requests.RequestException as e:
			self.LOGGER.error(f"Error sending to '{device.name}' at {url}: {e}")
