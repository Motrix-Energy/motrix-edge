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
	_headers: dict[str, str]
	_auth: Optional[HTTPBasicAuth]
	# None outside a run: built by start(), dropped by its finally. See _build_session().
	_session: Optional[requests.Session]
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

		# Kept rather than applied: the session they configure is built per run, in start().
		# HTTPBasicAuth is still resolved here on purpose — a malformed `auth` block is a
		# config error, and the KeyError it raises belongs on the main thread at startup,
		# where it is one traceback, rather than on the supervised thread, where it would be
		# five restarts and a CRITICAL over a typo in config.json.
		self._headers = dict(headers) if headers else {}
		self._auth = HTTPBasicAuth(auth["username"], auth["password"]) if auth else None
		self._session = None

		self._poll_tasks = []
		self._failed_devices = set()

	def _build_session(self) -> requests.Session:
		"""A configured session. One per run of start(), never carried across runs.

		`start()`'s finally closes the session, and `SupervisedWorker._run` re-invokes the
		*same bound* `start()` on the *same instance* after a crash — so a session built once
		in `__init__` left run two, and every run after it, polling through adapters whose
		connection pools `close()` had already released. urllib3 rebuilds those pools lazily,
		so the polls usually still succeeded: an accident of its implementation, not a
		promise, and the only reason this was never seen in production. `modbus_tcp.py` builds
		its client inside `start()` for the same reason, and closes it in the same finally.

		A method rather than four lines inlined in `start()` so the constructor wiring —
		headers, Basic auth, TLS verification — stays assertable without driving the loop.
		"""
		session = requests.Session()
		session.headers.update(self._headers)
		session.auth = self._auth
		session.verify = self.verify_ssl
		return session

	def _live_session(self, device: Device, payload: str) -> Optional[requests.Session]:
		"""The session a command may go out on, or None having warned and dropped it.

		`main` constructs every worker before starting any of them, and `send()` runs on the
		ALGORITHM's thread — so a command can arrive before `start()` has built the session,
		and again in the gap between a crash and the supervisor's restart. Unguarded, that is
		an AttributeError on None escaping `send()`, and nothing in `Algorithm.control_device`
		-> `DevicesManager.control` -> `Device.control` catches it: the *algorithm's* restart
		budget spent because a connector had not come up yet.

		Dropping the command is the answer `connectors/mqtt.py` already gives for a missing
		client, and the right one here: an algorithm re-decides every tick, so a setpoint
		delivered late is worse than one never delivered.

		On the base class rather than inline in `send()` so `connectors/openems.py`, whose
		`send()` is an override and not a delegation, inherits the guard instead of growing
		its own copy — it reached `self._session` directly, and an AttributeError is not one
		of the four exception types its handlers name.
		"""
		session = self._session
		if session is None:
			self.LOGGER.warning(f"Not connected yet, dropping command '{payload}' for '{device.name}'")
		return session

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

		self._session = self._build_session()
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
			# in-flight request from another thread is not safe.
			#
			# Dropped as well as closed, and in that order, because send() runs on an
			# algorithm's thread that outlives this loop: leaving the closed object in place
			# would have a command arriving between two supervised runs POST through adapters
			# whose pools are already released. Cleared, `_live_session()` warns and drops it
			# instead, and the next run's `_build_session()` fills the attribute back in.
			session, self._session = self._session, None
			session.close()
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

			# Gated on deliver(): a poll that reached the device but crashed inside it has
			# recovered nothing. Announcing a fix on the exact poll that proved the device
			# still broken — and clearing the flag, so the next failure logs as if it were the
			# first — is worse than saying nothing.
			if self.deliver(device, response.text) and device.name in self._failed_devices:
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
		session = self._live_session(device, payload)
		if session is None:
			return

		url = f"{self.base_url}/{endpoint.lstrip('/')}"
		method = device.controller_options.get("method", "POST").upper()

		try:
			response = session.request(
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
