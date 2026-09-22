from json import JSONDecodeError, dumps, loads
from ssl import CERT_NONE
from threading import Lock
from typing import Any, Optional, override

# MODULE TOP on purpose. websocket-client is optional (requirements-homeassistant.txt) and
# main.create_classes catches ModuleNotFoundError here, logging one clean per-entry skip —
# the EMS runs, minus Home Assistant. The same import inside start() would be a *crash*
# inside a supervised worker: five restarts with backoff, then CRITICAL, and once the
# restart budget is spent the worker counts as finished, which makes main shut the whole
# run down — over a dependency that is optional by design. Same rule, same reason, as
# services/rest_api.py and connectors/modbus_tcp.py.
from websocket import (
	WebSocket,
	WebSocketException,
	WebSocketTimeoutException,
	create_connection,
)

from api.capabilities import Switch
from api.connector import Connector
from api.device import Device
from api.options import bool_option, float_option, int_option

# Home Assistant's domain-agnostic services. They work for lights, switches, fans, scripts,
# media players and climate alike, so the overwhelmingly common case needs no controller
# config beyond an entity_id.
_DEFAULT_DOMAIN = "homeassistant"


class HomeAssistantConnector(Connector):
	"""Subscribes to Home Assistant entity state changes over its WebSocket API.

	One long-lived socket carries everything: the auth handshake, an initial `get_states`
	snapshot, a `state_changed` subscription, and outbound `call_service` commands. Devices
	are routed by `entity_id`, declared in `listener_options`.

	The connector parses nothing. It hands the device the entity id and the raw state
	object as JSON strings — the same two strings a replay CSV carries in its `topic` and
	`payload` columns — so a Home Assistant device is backtestable through a `pseudo`
	connector declaring `emulates: "home_assistant"` without a second parsing path.
	"""
	url: str
	verify_ssl: bool
	receive_timeout: float
	max_missed_pongs: int
	reconnect_backoff_seconds: float
	max_reconnect_backoff_seconds: float
	_ws: Optional[WebSocket]
	_by_entity: dict[str, list[Device]]

	@override
	def __init__(self, name: str, url: Optional[str] = None, host: Optional[str] = None,
				 port: Any = 8123, ssl: Any = False, access_token: Optional[str] = None,
				 verify_ssl: Any = True, receive_timeout: Any = 30, max_missed_pongs: Any = 2,
				 reconnect_backoff_seconds: Any = 1, max_reconnect_backoff_seconds: Any = 60) -> None:
		super().__init__(name)  # first: the coercion helpers below log through self.LOGGER
		# Coerced, not taken raw: Config validates the plugin schema *before* resolving
		# ${VAR}, so a "${HA_PORT}" declared `integer` still arrives here as a str — and a
		# ValueError escaping __init__ escapes main.create_classes too.
		self.verify_ssl = bool_option(self.LOGGER, "verify_ssl", verify_ssl, True)
		# minimum=1: the socket timeout is what lets the receive loop re-check the stop
		# event and drive the ping cadence, so a zero would spin.
		self.receive_timeout = float_option(self.LOGGER, "receive_timeout", receive_timeout, 30.0, minimum=1.0, maximum=300.0)
		self.max_missed_pongs = int_option(self.LOGGER, "max_missed_pongs", max_missed_pongs, 2, minimum=1, maximum=10)
		# Load-bearing floor, as in modbus_tcp: `min(delay * 2, max)` with delay == 0.0 is
		# 0.0 forever, i.e. a hot reconnect loop against a box that is down.
		self.reconnect_backoff_seconds = float_option(self.LOGGER, "reconnect_backoff_seconds", reconnect_backoff_seconds, 1.0, minimum=0.1)
		self.max_reconnect_backoff_seconds = float_option(self.LOGGER, "max_reconnect_backoff_seconds", max_reconnect_backoff_seconds, 60.0, minimum=0.1)
		self.url = self._resolve_url(url, host, port, ssl)

		# Private and never logged. It is also never in __repr__ — Connector has none, so
		# the default object repr applies, and services/rest_api.py serialises an explicit
		# field allowlist that never reaches connector attributes.
		self._access_token = access_token

		self._ws = None
		self._by_entity = {}
		self._warned_entities: set[str] = set()
		self._pending: dict[int, str] = {}
		# One lock over BOTH allocating an id and writing the frame. websocket-client's
		# enable_multithread lock makes each *frame* atomic, which is not the guarantee
		# needed here: Home Assistant rejects a command whose id is not strictly greater
		# than the last it saw (error code "id_reuse"). Allocate under one lock and write
		# under another and two algorithm threads take ids 5 and 6, the 6 wins the write
		# race, and the perfectly valid 5 is refused.
		self._send_lock = Lock()
		self._next_id = 1
		self._snapshot_id: Optional[int] = None

	def _resolve_url(self, url: Optional[str], host: Optional[str], port: Any, ssl: Any) -> str:
		"""The WebSocket endpoint: an explicit `url` wins, else built from host/port/ssl."""
		if url:
			return url
		if not host:
			return ""  # start() reports this; a constructor must not raise
		scheme = "wss" if bool_option(self.LOGGER, "ssl", ssl, False) else "ws"
		return f"{scheme}://{host}:{int_option(self.LOGGER, 'port', port, 8123, minimum=1, maximum=65535)}/api/websocket"

	@override
	def inject_devices(self, devices: dict[str, Device]) -> None:
		super().inject_devices(devices)
		self._by_entity = {}
		for device in devices.values():
			if not device.is_readable:
				continue
			declared = device.listener_options.get("entity_id")
			entities = [declared] if isinstance(declared, str) else declared
			if not entities:
				self.LOGGER.warning(f"Device '{device.name}' has no listener_options.entity_id, it will receive nothing")
				continue
			for entity in entities:
				if not isinstance(entity, str) or not entity:
					continue
				# A list per entity, not a single device: two devices legitimately watch one
				# entity — a raw view and an energy view of the same sensor, say — and
				# keying one device per entity would silently drop the second.
				self._by_entity.setdefault(entity, []).append(device)
		self.LOGGER.info(f"{len(self._by_entity)} entity subscription(s) configured")

	@override
	def start(self) -> None:
		"""Blocking session loop: connect, authenticate, subscribe, receive, reconnect."""
		if not self.url:
			self.LOGGER.error("No url (or host) configured, connector idle")
			return
		if not self._access_token:
			self.LOGGER.critical(
				f"No access_token configured for {self.url}; this connector cannot start. "
				f"Set `access_token` in config.json, or the environment variable it interpolates."
			)
			return
		if not self._by_entity:
			self.LOGGER.warning("No device declares listener_options.entity_id; this connector will receive nothing")

		backoff = self.reconnect_backoff_seconds
		while not self.is_stopping():
			try:
				if not self._connect_and_authenticate():
					return  # auth_invalid: permanent, see _connect_and_authenticate
				backoff = self.reconnect_backoff_seconds  # a live session resets the ladder
				self._subscribe()
				self._receive_loop()
			except (WebSocketException, OSError) as e:
				# Narrow on purpose: a genuine bug (TypeError, AttributeError) must still
				# reach the supervisor with its traceback, which is what it is for. It can
				# stay narrow because the only foreign frame this would otherwise catch —
				# a device's — is caught one level down by `Connector.deliver`.
				if not self.is_stopping():
					self.LOGGER.warning(f"Home Assistant session on {self.url} ended ({e}); reconnecting in {backoff:g}s")
			finally:
				self._teardown()
			if self.is_stopping() or self.wait_stop(backoff):
				break
			# Reconnect here rather than raising to be restarted. Five restarts is roughly
			# 31s of total tolerance and a Home Assistant box reboots in about two minutes;
			# past the cap the worker is *finished*, and because main waits on connectors
			# only, the whole EMS shuts down. An EMS that terminates because Home Assistant
			# restarted is not acceptable. Raising would also burn a restart budget shared
			# with the algorithms, weakening crash protection everywhere.
			backoff = min(backoff * 2, self.max_reconnect_backoff_seconds)
		self.LOGGER.info("Home Assistant connector stopped")

	def _connect_and_authenticate(self) -> bool:
		"""Open the socket and run the auth handshake. False means *permanently* failed."""
		options: dict[str, Any] = {
			# Explicit rather than left to the library default: this single option decides
			# whether two algorithm threads calling send() can interleave frames on the
			# wire. A default that changes upstream would corrupt the protocol silently.
			"enable_multithread": True,
		}
		if not self.verify_ssl:
			options["sslopt"] = {"cert_reqs": CERT_NONE, "check_hostname": False}

		self.LOGGER.info(f"Connecting to Home Assistant at {self.url}")
		# create_connection's timeout is the socket timeout: it bounds the handshake and
		# every later recv(), which is what keeps the receive loop responsive to stop().
		socket = create_connection(self.url, timeout=self.receive_timeout, **options)
		with self._send_lock:
			self._ws = socket

		hello = self._expect(socket)
		if hello.get("type") != "auth_required":
			raise WebSocketException(f"Unexpected first message from Home Assistant: {hello.get('type')!r}")
		# No "id" on the auth frame: ids exist only in the command phase.
		socket.send(dumps({"type": "auth", "access_token": self._access_token}))
		reply = self._expect(socket)

		if reply.get("type") == "auth_invalid":
			# Log and return, never raise. A bad token is deterministic, so restarting
			# achieves nothing but five CRITICALs — and Home Assistant IP-bans repeated
			# failed logins, so retrying can lock this host out *after* the token is fixed.
			# Same shape as PseudoConnector returning on a missing replay file: a config
			# error no amount of retrying repairs ends the worker cleanly.
			self.LOGGER.critical(
				f"Home Assistant rejected the access token for {self.url}: {reply.get('message')!r}. "
				f"This is a configuration error, not a transient failure, so the connector will NOT retry — "
				f"repeated failed logins can get this host IP-banned by Home Assistant. Fix `access_token` in "
				f"config.json (or the ${{VAR}} it interpolates) and restart. If this is the only connector, "
				f"the EMS will now shut down."
			)
			return False
		if reply.get("type") != "auth_ok":
			raise WebSocketException(f"Unexpected reply to auth: {reply.get('type')!r}")

		self.LOGGER.info(f"Authenticated with Home Assistant {reply.get('ha_version')}")
		self.on_connected()  # transport is up: write-only devices are reachable from here
		return True

	def _subscribe(self) -> None:
		"""Subscribe first, then snapshot.

		This order is lossless and the reverse is not: Home Assistant processes one socket's
		messages in order, so a snapshot taken after the subscription can only be as new as
		the subscription point, and any change from then on arrives as an event. Snapshot
		first would drop every change that happened while the snapshot was in flight.
		"""
		self._send_command({"type": "subscribe_events", "event_type": "state_changed"})
		self._snapshot_id = self._send_command({"type": "get_states"})

	def _receive_loop(self) -> None:
		missed = 0
		while not self.is_stopping():
			try:
				frame = self._ws.recv()
			except WebSocketTimeoutException:
				# Not an error. Home Assistant speaks only when something changes and a
				# quiet house is silent for minutes. The timeout is what lets this loop
				# re-check the stop event; the ping is what tells a *quiet* socket from a
				# *dead* one. A half-open TCP connection — a NAT rebind, an AP roam — would
				# otherwise keep timing out forever with every device still marked
				# connected, which is the worst failure mode for an EMS because it looks
				# healthy from the outside.
				if self.is_stopping():
					return
				missed += 1
				if missed > self.max_missed_pongs:
					self.LOGGER.warning(
						f"No reply to {missed} ping(s) in {missed * self.receive_timeout:g}s; "
						f"assuming the connection is dead"
					)
					return  # start()'s loop reconnects
				self._send_command({"type": "ping"})
				continue
			missed = 0  # any frame, a pong included, proves the socket is alive
			if frame:  # control frames come back as ""
				self._handle_frame(frame)

	def _handle_frame(self, frame: Any) -> None:
		try:
			message = loads(frame)
		except (JSONDecodeError, TypeError) as e:
			self.LOGGER.warning(f"Unreadable frame from Home Assistant: {e}")
			return
		if not isinstance(message, dict):
			self.LOGGER.warning(f"Unexpected frame from Home Assistant: {message!r}")
			return

		match message.get("type"):
			case "event":
				data = (message.get("event") or {}).get("data") or {}
				state = data.get("new_state")
				if state is None:
					# A removed entity has new_state null. Nothing to publish, and
					# publishing the previous reading under a new timestamp would be wrong.
					self.LOGGER.debug(f"Ignoring state_changed with no new_state: {data.get('entity_id')}")
					return
				self._dispatch(state)
			case "result":
				self._handle_result(message)
			case "pong":
				self.LOGGER.debug("pong")
			case other:
				self.LOGGER.debug(f"Ignoring Home Assistant message of type {other!r}")

	def _handle_result(self, message: dict[str, Any]) -> None:
		message_id = message.get("id")
		if not message.get("success", True):
			error = message.get("error") or {}
			what = self._pending.pop(message_id, f"command {message_id}")
			self.LOGGER.warning(f"Home Assistant refused {what}: {error.get('code')} {error.get('message')}")
			return
		self._pending.pop(message_id, None)
		if message_id != self._snapshot_id:
			return
		# The get_states snapshot: seed every device so an algorithm's readiness gate is
		# satisfied without waiting for the entity to happen to change.
		states = message.get("result") or []
		self._snapshot_id = None
		if not isinstance(states, list):
			self.LOGGER.warning("get_states returned no list, skipping the initial snapshot")
			return
		self.LOGGER.info(f"Initial snapshot: {len(states)} entity state(s)")
		for state in states:
			if isinstance(state, dict) and state.get("entity_id") in self._by_entity:
				self._dispatch(state)

	def _dispatch(self, state: dict[str, Any]) -> None:
		"""Hand one state object to every device watching that entity."""
		entity_id = state.get("entity_id")
		devices = self._by_entity.get(entity_id)
		if not devices:
			if entity_id not in self._warned_entities:
				# Warn once per entity: the subscription is to *all* state changes, so an
				# unfiltered house would otherwise log a line per change per second.
				self._warned_entities.add(entity_id)
				self.LOGGER.debug(f"No device watches entity '{entity_id}'")
			return
		# The bare state object, not the event envelope: get_states returns state objects
		# with no envelope, so passing the envelope would give the device two shapes for the
		# same information. Serialised rather than passed as a dict so the live path and the
		# replay path are byte-identical — PseudoConnector replays two strings from a CSV.
		payload = dumps(state)
		for device in devices:
			# The guard this used to carry inline now lives in `Connector.deliver`, which also
			# rate-limits the repeat and logs the traceback. The reasoning it carried — one
			# misbehaving device must not end the session for the others, because the
			# supervisor would restart the whole connector and lose the subscription — is in
			# that docstring.
			self.deliver(device, entity_id, payload)

	@override
	def send(self, device: Device, payload: str) -> None:
		"""Turn a command token into a Home Assistant `call_service` message."""
		call = self._resolve_command(device, payload)
		if call is None:
			return  # already warned
		try:
			message_id = self._send_command(call)
		except (WebSocketException, OSError) as e:
			# send() runs on an *algorithm's* thread and nothing in Algorithm.control_device
			# -> DevicesManager.control -> Device.control -> here catches, so a raise would
			# crash that algorithm's supervised worker over a transient socket state.
			self.LOGGER.error(f"Could not send '{payload}' to '{device.name}': {e}")
			return
		self._pending[message_id] = f"{device.name} '{payload}' -> {call['domain']}.{call['service']}"
		self.LOGGER.info(f"[{message_id}] {device.name}: {payload} -> {call['domain']}.{call['service']} {call['target']}")

	def _resolve_command(self, device: Device, payload: str) -> Optional[dict[str, Any]]:
		"""The call_service message for a command token, or None (having warned).

		`controller_options.commands` maps a token to a *full* service call rather than the
		obvious {domain, service_on, service_off}: that shape only ever expresses a Switch
		and dead-ends the moment an algorithm wants climate.set_temperature.
		"""
		options = device.controller_options
		entity_id = options.get("entity_id")
		token = payload.strip().lower()
		commands = options.get("commands") or {}
		spec = commands.get(token) if isinstance(commands, dict) else None

		if spec is None:
			if token not in (Switch.COMMAND_ON, Switch.COMMAND_OFF):
				# Never guess a service name from an arbitrary token: homeassistant.boost
				# does not exist, and inventing it turns a config mistake into a silent no-op
				# with an error buried in a result frame.
				self.LOGGER.warning(
					f"No controller_options.commands entry for '{payload}' on '{device.name}', not sending. "
					f"Only '{Switch.COMMAND_ON}'/'{Switch.COMMAND_OFF}' have a default mapping."
				)
				return None
			spec = {"service": f"turn_{token}"}
		if not isinstance(spec, dict) or not spec.get("service"):
			self.LOGGER.warning(f"controller_options.commands['{token}'] on '{device.name}' names no service, not sending")
			return None

		target = spec.get("target")
		if target is None:
			if not entity_id:
				self.LOGGER.warning(f"Device '{device.name}' has no controller_options.entity_id and the command names no target, not sending")
				return None
			target = {"entity_id": entity_id}
		call: dict[str, Any] = {
			"type": "call_service",
			"domain": spec.get("domain") or options.get("domain") or _DEFAULT_DOMAIN,
			"service": spec["service"],
			"target": target,
		}
		if spec.get("service_data"):
			call["service_data"] = spec["service_data"]
		return call

	def _send_command(self, message: dict[str, Any]) -> int:
		"""Allocate an id and write the frame, both under one lock. Returns the id."""
		with self._send_lock:
			socket = self._ws
			if socket is None:
				raise WebSocketException("not connected")
			message_id = self._next_id
			self._next_id += 1
			socket.send(dumps({**message, "id": message_id}))
		return message_id

	def _expect(self, socket: WebSocket) -> dict[str, Any]:
		"""One decoded message from the socket, during the handshake."""
		frame = socket.recv()
		try:
			message = loads(frame)
		except (JSONDecodeError, TypeError) as e:
			raise WebSocketException(f"Unreadable handshake frame: {e}") from e
		if not isinstance(message, dict):
			raise WebSocketException(f"Unexpected handshake frame: {message!r}")
		return message

	def _teardown(self) -> None:
		"""End a session on the thread that owns the socket."""
		with self._send_lock:
			socket, self._ws = self._ws, None
			self._pending.clear()
			self._snapshot_id = None
		if socket is None:
			return
		try:
			# timeout=0 skips close()'s wait for the peer's close frame. Closing here
			# rather than in stop() is the same reasoning HttpApiConnector documents for
			# its session: tearing the transport down from another thread while this one
			# is mid-read is a race.
			socket.close(timeout=0)
		except (WebSocketException, OSError) as e:
			self.LOGGER.debug(f"Error closing the Home Assistant socket: {e}")

	@override
	def stop(self) -> None:
		super().stop()  # always first: sets the stop event
		socket = getattr(self, "_ws", None)  # only exists once start() ran
		if socket is None:
			return
		try:
			# abort(), NOT close(). close() writes a close frame and then loops on
			# recv_frame() for up to 3s *without* taking websocket-client's readlock, so it
			# races the connector thread already sitting in recv() for the same bytes — and
			# it would eat 3s of main's 10s shutdown grace. abort() is socket.shutdown(
			# SHUT_RDWR) and nothing else: the library's documented way to wake a thread
			# blocked in recv_*. The socket itself is closed by _teardown(), on the thread
			# that owns it.
			socket.abort()
		except Exception as e:
			# Racing a teardown on the connector thread: self.sock may already be None or
			# closed. Neither deserves the traceback the supervisor prints on a clean stop.
			self.LOGGER.debug(f"abort() during stop: {e}")
