from dataclasses import dataclass
from queue import Empty, Full, Queue
from re import Pattern, compile, error
from threading import Lock, Thread
from time import monotonic
from typing import Any, Optional, override

from paho.mqtt.client import Client, MQTTMessage
# noinspection PyUnresolvedReferences
from paho.mqtt.enums import CallbackAPIVersion, MQTTProtocolVersion
from paho.mqtt.reasoncodes import ReasonCode

from api.connector import Connector
from api.device import Device
from api.options import bool_option, int_option
from config.enums.mqtt_version import MQTTVersion

# How many payloads may wait for one device before the oldest is dropped. A burst absorber,
# not a buffer: it swallows a retained-message flush on reconnect or a GC pause, but a meter
# publishing every second whose queue is this deep is already a minute behind, and nothing
# waiting in it is worth handing an algorithm. Raising the bound would not preserve more
# truth, it would only postpone the drop and lengthen the interval over which the EMS acts
# on the past.
_MAX_QUEUED_PER_DEVICE = 64

# How long the end of start() waits for the dispatchers to wind down — one deadline shared by
# all of them, never this much per thread, so a connector with twenty devices cannot turn a
# bounded wait into a twenty-fold one. Deliberately well under the default
# runtime.shutdown_timeout_seconds (10): `Supervisor.stop_all` gives that budget to every
# worker in the process at once, and `Main.shutdown` still has to flush storage after it.
_DISPATCH_JOIN_TIMEOUT = 2.0

# Pushed into a device's queue to unblock its dispatcher. A sentinel rather than a timed poll
# on get(): the dispatchers are idle almost all the time, and waking every one of them several
# times a second to ask whether the EMS is still running is a cost paid for the whole life of
# the run to make shutdown — which happens once — marginally simpler.
_STOP = object()


@dataclass(frozen=True)
class _Dispatcher:
	"""One device's inbox: the bounded queue `on_message` fills, and the thread draining it."""
	queue: Queue[Any]
	thread: Thread


class MQTTConnector(Connector):
	mqtt_client: Client
	host: str
	port: int
	protocol: MQTTProtocolVersion
	callbacks: dict[Pattern, Device]
	subscriptions: set[str]
	client_id: str | None
	username: str | None
	password: str | None
	tls: bool
	ca_certs: str | None
	certfile: str | None
	keyfile: str | None

	@override
	def __init__(self, name: str, host: str, port: int, version: str,
				 username: str | None = None, password: str | None = None,
				 client_id: str | None = None, tls: bool = False,
				 ca_certs: str | None = None, certfile: str | None = None,
				 keyfile: str | None = None) -> None:
		super().__init__(name)
		self.host = host
		# Coerced, not taken raw. Config validates the plugin schema BEFORE resolving ${VAR},
		# so a "${MQTT_PORT}" reaches here as a str — and paho compares the port against an
		# int inside connect(), which made an unresolved variable a TypeError on the
		# supervised worker thread rather than a config warning.
		self.port = int_option(self.LOGGER, "port", port, 1883, minimum=1, maximum=65535)
		self.protocol = self._protocol_version(version)
		self.callbacks = {}
		self.subscriptions = set()
		self.client_id = client_id
		self.username = username
		self.password = password
		# Likewise, and this one fails silently rather than loudly: tls is only ever read for
		# truthiness below, and the string "false" is truthy — so an operator disabling TLS
		# through an environment variable would have got TLS anyway.
		self.tls = bool_option(self.LOGGER, "tls", tls, False)
		self.ca_certs = ca_certs
		self.certfile = certfile
		self.keyfile = keyfile
		# One bounded queue and one dispatch thread per device that actually receives traffic,
		# built on demand by `_dispatcher_for`. Keyed by device name rather than by the device
		# itself: `Device` states no `__hash__` contract of its own, and the names are already
		# unique per connector because `inject_devices` takes them as dict keys.
		self._dispatchers: dict[str, _Dispatcher] = {}
		# Guards `_dispatchers` and nothing else. `on_message` inserts on paho's network thread
		# while `stop()` reads on the supervisor's, and iterating a dict through that is a
		# RuntimeError. Never held across a put() or a deliver().
		self._dispatch_lock = Lock()
		# Devices currently dropping payloads. Edge-triggered, the shape
		# `LoRaWANConnector._throttle` and `Connector._raising_devices` already use: a gateway
		# publishing faster than a device parses would otherwise write one WARNING per message,
		# which is the same flood in the log that it already is on the wire.
		self._overflowing_devices: set[str] = set()

	def _protocol_version(self, version: Any) -> MQTTProtocolVersion:
		"""Coerce the configured version, warning and defaulting rather than raising.

		`MQTTVersion` is a StrEnum, so an unknown value raises `ValueError` — and the
		commonest unknown value is `None`, which is what `"version": "${MQTT_VERSION}"`
		interpolates to when the variable is unset. `main.create_classes` would contain
		that ValueError, but containment here means losing the broker connection and every
		device behind it for the whole run, over one unset environment variable — when the
		default is almost certainly what the operator wanted. Same reasoning as
		`api/options.py`, which exists for exactly this class of failure.
		"""
		try:
			return MQTTVersion(version).mqtt_protocol_version()
		except (TypeError, ValueError):
			fallback = MQTTVersion.MQTTv311
			self.LOGGER.warning(f"Unknown MQTT version {version!r} on {self.name}, using {fallback.value}")
			return fallback.mqtt_protocol_version()

	@override
	def start(self) -> None:
		if not self.host:
			# Returning is a *clean completion*: the supervisor logs it and leaves it alone.
			# Falling through instead makes paho raise ValueError("Invalid host") on every
			# attempt, which the supervisor counts as a crash — five restarts, backoff,
			# CRITICAL — and once the budget is spent the worker is finished, which is what
			# main waits on. An unset ${MQTT_HOST} would take the whole EMS down with it.
			# Same guard, and same reasoning, as connectors/modbus_tcp.py and connectors/lora.py.
			self.LOGGER.error("No host configured, connector idle")
			return
		client_id = self.client_id or self.name  # unique per connector; avoids the shared-"EMS" reconnect loop
		self.mqtt_client = Client(callback_api_version=CallbackAPIVersion.VERSION2, client_id=client_id, protocol=self.protocol)
		self.mqtt_client.on_connect = self._on_connect
		self.mqtt_client.on_message = self.on_message
		if self.username is not None:
			self.mqtt_client.username_pw_set(self.username, self.password)
		if self.tls or self.ca_certs is not None or self.certfile is not None:
			self.mqtt_client.tls_set(ca_certs=self.ca_certs, certfile=self.certfile, keyfile=self.keyfile)
		self.mqtt_client.reconnect_delay_set(min_delay=1, max_delay=120)
		retry_delay = 1
		max_delay = 120
		while not self.is_stopping():
			try:
				self.mqtt_client.connect(host=self.host, port=self.port)
				break
			except (TimeoutError, OSError) as e:
				self.LOGGER.error(f"Connection to {self.host}:{self.port} failed: {e}. Retrying in {retry_delay}s...")
				if self.wait_stop(retry_delay):  # interruptible: a shutdown aborts the retry
					break
				retry_delay = min(retry_delay * 2, max_delay)
		if self.is_stopping():
			self.LOGGER.info("Stop requested before connection, not starting the network loop")
			return
		self.mqtt_client.loop_forever(retry_first_connection=True)
		self.LOGGER.info("Network loop stopped")
		# The network loop has returned, so nothing will ever be queued again. Winding the
		# dispatchers down *here* rather than in stop() is what keeps the wait off the shared
		# grace period: this runs on the supervised thread the supervisor is already joining
		# against its own deadline, whereas stop() runs before any worker has been joined at
		# all. A loop_forever() that *raised* skips both calls on purpose — the supervisor
		# restarts start() in the same object, and the dispatchers it finds still alive are the
		# ones already holding this connector's queues.
		self._release_dispatchers()
		self._join_dispatchers()

	@override
	def stop(self) -> None:
		super().stop()
		# loop_forever() blocks in paho's own event loop; disconnecting is what makes it return.
		# mqtt_client only exists once start() ran, so a stop before/without start is a no-op.
		client = getattr(self, "mqtt_client", None)
		if client is not None:
			client.disconnect()
		self._release_dispatchers()

	def _on_connect(self, client: Client, userdata: Any, flags: Any, reason_code: ReasonCode, properties: Any) -> None:
		self.LOGGER.info(f"Connected to {self.host}:{self.port}")
		# Subscribe here (not in start): paho does not auto-resubscribe, so this re-runs on every reconnect
		if self.subscriptions:
			self.mqtt_client.subscribe([(topic, 0) for topic in sorted(self.subscriptions)])
			self.LOGGER.info(f"Subscribed to {sorted(self.subscriptions)}")
		else:
			self.LOGGER.warning("No subscription filters declared; connector will receive no messages")
		self.on_connected()  # marks write-only devices as connected

	def on_message(self, client: Client, userdata: Any, message: MQTTMessage) -> None:
		topic: str = message.topic
		payload: str = message.payload.decode()
		self.LOGGER.info(f"[{topic}]: {payload}")
		for topic_regex, device in self.callbacks.items():
			if topic_regex.match(topic):
				self._enqueue(device, topic, payload)

	def _enqueue(self, device: Device, topic: str, payload: str) -> None:
		"""Hand one payload to the device's dispatcher. Never blocks, never raises.

		This replaced a `Thread(target=..., daemon=True).start()` per matching message, which
		was wrong in two independent ways — both of them driven by data the EMS does not
		control.

		It created one OS thread per message with no ceiling and no backpressure, so the thread
		count followed *broker traffic*: a retained-message flush on reconnect, a
		`listener_options.subscription` of `#`, or a gateway that got chatty after a firmware
		update. None of those are decisions this process makes.

		And it lost ordering. Two messages for the same device ran concurrently, and
		`Device.receive` writes `self.data` wholesale (`devices/p1.py:208`), so a meter could
		publish an older reading after a newer one — and because `DevicesManager.update_device`
		and the storage write both happen inside `Connector.on_device_data_received`, the stale
		value then reached the algorithms *and* the versioned CSV. A stale value presented as
		current is what an algorithm acts on; for an EMS reading a meter that is a correctness
		bug, not a performance one.

		Nothing on this path may block, because `on_message` runs on the thread `loop_forever()`
		owns (see `start()`) and that same thread answers PINGREQ. Waiting here on a slow parser
		stalls keepalive for *every* device on this connector until the broker drops the
		connection — one device's parser taking the transport down with it.
		"""
		if self.is_stopping():
			# A payload arriving after stop() has nobody left to act on it: the algorithms are
			# winding down and `Main.shutdown` closes the storage backends moments later, so a
			# write that did land would race a closing backend. Returning here also keeps
			# `_dispatcher_for` from resurrecting a thread that has just been told to exit.
			return
		dispatcher = self._dispatcher_for(device)
		try:
			dispatcher.queue.put_nowait((topic, payload))
		except Full:
			self._drop_oldest(device, dispatcher, topic, payload)
			return
		self._clear_overflow(device)

	def _dispatcher_for(self, device: Device) -> _Dispatcher:
		"""The device's queue and dispatch thread, started on its first matching message.

		**This is the ceiling.** One thread per *device* is bounded by the device list, which is
		`config.json`; one thread per *message* was bounded by broker traffic, which is not
		ours. A `#` subscription now costs one thread per device that matches it, once, instead
		of one thread per message that arrives, forever.

		Per device rather than a single dispatcher for the whole connector, and the deciding
		reason is isolation rather than throughput. With one shared bounded queue, a chatty
		gateway's messages evict a grid meter's from the same buffer — so the reading the
		algorithms actually need is the one lost, to a device nobody is optimising for. Per
		device, a backlog is charged to the device that caused it, and one slow parser (a DSMR
		telegram with a CRC over a kilobyte) delays only its own readings. The price is the
		thread count above, which is precisely what this change exists to bound.

		Lazy rather than one per device in `inject_devices()`: a connector that never reaches
		its network loop — `start()`'s no-host path returns before it — would otherwise have
		spawned a thread per device for a transport that will never deliver anything.
		"""
		with self._dispatch_lock:
			dispatcher = self._dispatchers.get(device.name)
			if dispatcher is not None and dispatcher.thread.is_alive():
				return dispatcher
			if dispatcher is not None:
				# The previous thread died on something `Connector.deliver` does not contain.
				# Replacing it is what stops that from silencing this one device for the life
				# of the run while the connector stays up and every other device goes on
				# reporting — the failure that looks like nothing at all from outside.
				self.LOGGER.error(f"Dispatcher for '{device.name}' is gone, starting a new one")
			queue: Queue[Any] = Queue(maxsize=_MAX_QUEUED_PER_DEVICE)
			thread = Thread(target=self._dispatch, args=(device, queue), name=f"{self.name}/{device.name}", daemon=True)
			dispatcher = _Dispatcher(queue=queue, thread=thread)
			self._dispatchers[device.name] = dispatcher
			thread.start()
			return dispatcher

	def _drop_oldest(self, device: Device, dispatcher: _Dispatcher, topic: str, payload: str) -> None:
		"""Make room for the newest payload by discarding the oldest queued one.

		**Oldest, not newest — and for a meter that is the defensible direction.** The newest
		payload is the closest thing to the present state of the installation and the one an
		algorithm is about to act on; whatever sits at the head of a full queue has already been
		superseded by everything behind it. Dropping the newest instead would do exactly what
		this dispatcher exists to prevent: leave the device's `data` holding a stale value while
		a fresher one was thrown away. Dropping from the head also keeps what remains in arrival
		order, so the ordering guarantee survives the overflow.

		It is not free, which is why it is logged rather than silent. A device whose payloads
		are *cumulative* rather than absolute — a LoRaWAN uplink carrying a counter — loses a
		sample here, where an absolute meter only loses a restatement. The alternative was never
		"keep everything": it was blocking paho's loop, which costs the connection and every
		device behind it.
		"""
		try:
			dispatcher.queue.get_nowait()
		except Empty:
			pass  # the dispatcher drained it between the failed put and here; there is room either way
		try:
			dispatcher.queue.put_nowait((topic, payload))
		except Full:
			# Unreachable while paho's single network loop is the only producer. Logged rather
			# than passed, because the day that stops being true the symptom is a device quietly
			# missing readings with no line anywhere saying so.
			self.LOGGER.error(f"Queue for '{device.name}' is still full after dropping its oldest payload; dropping '{topic}' instead")
			return
		if device.name in self._overflowing_devices:
			self.LOGGER.debug(f"Dropped the oldest queued payload for '{device.name}' to make room for '{topic}'")
			return
		self._overflowing_devices.add(device.name)
		self.LOGGER.warning(
			f"Device '{device.name}' is receiving faster than it parses: its queue is full at {_MAX_QUEUED_PER_DEVICE} "
			f"payload(s) and the oldest is being dropped. Readings are being lost, newest kept"
		)

	def _clear_overflow(self, device: Device) -> None:
		"""Say so once when a device stops overflowing — the edge, not the level.

		Paired with the WARNING in `_drop_oldest` so an operator who saw one is told when it
		ended. Without this, "readings are being lost" is a line with no closing bracket, and
		the only way to know whether it still holds is to watch the log for silence.
		"""
		if device.name in self._overflowing_devices:
			self._overflowing_devices.discard(device.name)
			self.LOGGER.info(f"Device '{device.name}' is keeping up again")

	def _dispatch(self, device: Device, queue: Queue[Any]) -> None:
		"""One device's payloads, one at a time, in arrival order. Runs until stopped.

		The serialisation *is* the fix for the ordering half of the bug. `Device.receive` writes
		`self.data` wholesale and `Connector.deliver` then publishes it through
		`DevicesManager.update_device` and every storage backend, all inside
		`on_device_data_received`. Two threads doing that for one device could interleave so
		that the *older* reading landed last — and once it has, an algorithm reading
		`device.data` and a row in `device_data.csv` both state that the meter's present value
		is a number it reported a second ago. A FIFO queue with exactly one consumer makes
		arrival order a property of the mechanism instead of a property of timing.

		No broad `except` around the call below, deliberately. `Connector.deliver` is this
		connector's one plugin boundary and already contains anything a device raises; a second
		catch here would swallow *our* bugs too, which is the rule CLAUDE.md states — narrow
		where the frame is ours — and it is why `_dispatcher_for` bothers to notice a dispatcher
		that died.
		"""
		while True:
			item = queue.get()
			if item is _STOP or self.is_stopping():
				# Abandon what is left, do not drain it. Whatever is still queued describes an
				# installation this process is no longer managing, and `Main.shutdown` closes
				# the storage backends as soon as its bounded join returns.
				return
			topic, payload = item
			self._receive_and_notify(device, topic, payload)

	def _receive_and_notify(self, device: Device, topic: str, payload: str) -> None:
		# A named method rather than `Thread(target=self.deliver, ...)`: a thread takes its
		# target's name into a traceback, and this frame is where the mqtt-specific reason
		# belongs. Nothing above this call can catch — the dispatch thread is the top of its own
		# stack, and `on_message` returned the moment the payload was queued — so an escape here
		# never reached the supervisor at all. It went to `threading.excepthook`: a raw stderr
		# traceback past every configured handler, no crash counted, nothing in /workers, and a
		# device that silently stopped reporting. Containment here buys observability, where on
		# the polling connectors it buys survival.
		self.deliver(device, topic, payload)

	def _release_dispatchers(self) -> None:
		"""Abandon every queue and unblock every dispatcher. Returns at once, waits for none.

		`SupervisedWorker.request_stop()` promises never to block, and `Supervisor.stop_all`
		builds on it: it calls `request_stop()` on *every* worker before joining *any* of them,
		against one shared deadline. A `stop()` that waited for its dispatchers here would spend
		that budget before the first join had even started, so one connector with one slow
		device would eat the grace period the other connectors — and the storage flush after
		them — are all drawing on. The waiting belongs at the end of `start()`, on the thread
		the supervisor is already joining.
		"""
		for name, dispatcher in self._dispatch_snapshot():
			abandoned = 0
			while True:
				try:
					dispatcher.queue.get_nowait()
				except Empty:
					break
				abandoned += 1
			if abandoned:
				self.LOGGER.info(f"Abandoning {abandoned} queued payload(s) for '{name}'")
			try:
				dispatcher.queue.put_nowait(_STOP)
			except Full:
				# A payload raced in between the drain above and this put. Harmless: the
				# dispatcher takes it, sees is_stopping(), and returns on the same pass.
				pass

	def _join_dispatchers(self) -> None:
		"""Wait out the dispatchers once, against a single shared deadline.

		One deadline for all of them rather than `_DISPATCH_JOIN_TIMEOUT` each: twenty devices
		would otherwise turn a two-second bound into a forty-second one, and `Main.shutdown`
		grants `runtime.shutdown_timeout_seconds` (10 by default) to every worker in the process
		put together.

		A dispatcher still running when the deadline passes is left alone. It is a daemon thread
		parked inside `deliver()`, so it dies with the interpreter — the same contract
		`Supervisor.stop_all` states for the workers themselves — and naming it in the log is
		more use than hanging the shutdown on it.
		"""
		deadline = monotonic() + _DISPATCH_JOIN_TIMEOUT
		for name, dispatcher in self._dispatch_snapshot():
			dispatcher.thread.join(max(0.0, deadline - monotonic()))
			if dispatcher.thread.is_alive():
				self.LOGGER.warning(
					f"Dispatcher for '{name}' did not finish within {_DISPATCH_JOIN_TIMEOUT:g}s; "
					f"it is a daemon and will be killed at exit"
				)

	def _dispatch_snapshot(self) -> list[tuple[str, _Dispatcher]]:
		"""A stable view of the dispatchers, taken under the lock.

		`on_message` may be inserting on paho's network thread while this runs on the
		supervisor's, and iterating a dict through that raises RuntimeError — inside `stop()`,
		where `SupervisedWorker.request_stop` would catch it, log "raised while stopping", and
		leave every dispatcher it had not yet reached blocked on an empty queue.
		"""
		with self._dispatch_lock:
			return list(self._dispatchers.items())

	def resolve_listener(self, device: Device) -> tuple[Any, Optional[str]]:
		"""The (subscription filter, client-side routing regex) this device listens on.

		The seam a subclass overrides when its devices describe *which node they are*
		rather than *which topic carries them* — `connectors/lorawan.py` synthesises both
		from a devEUI and a network-server profile. Same role as
		`HttpApiConnector.resolve_endpoint()`, and the alternatives were rejected for the
		same reasons: writing the synthesised values back into `device.listener_options`
		would put them in every snapshot `Device.__deepcopy__` hands out, and rebuilding
		the mappings after delegating to `super()` would emit the missing-subscription
		warning for every device.

		The pattern comes back as a **string**, not a compiled `Pattern`. That keeps
		`compile()` and its `re.error` handling here instead of in every subclass, and it
		lets a subclass ship an inline flag — `(?i)`, for a hex EUI that is
		case-insensitive by nature — which Python accepts only at position 0 of the
		expression.

		One seam rather than two (`resolve_subscription` / `resolve_pattern`), because a
		subclass derives both from a single identifier: splitting them would normalise it
		twice, and would let the filter and the regex end up describing different topics.
		"""
		return device.listener_options.get("subscription"), device.listener_options.get("pattern")

	def resolve_downlink(self, device: Device, payload: str) -> Optional[tuple[str, str]]:
		"""The (topic, body) to publish for a control command, or None having logged why.

		The write-path counterpart of `resolve_listener()`. A subclass whose broker
		expects a wrapped body — a LoRaWAN network server wants base64 inside a JSON
		envelope, never the bare token — overrides this rather than `send()`, and inherits
		the client check and the error handling below.
		"""
		topic = device.controller_options.get("topic")
		if not topic:
			self.LOGGER.warning(f"Device '{device.name}' has no controller_options.topic, dropping command '{payload}'")
			return None
		return topic, payload

	@override
	def send(self, device: Device, payload: str) -> None:
		# send() runs on the ALGORITHM's thread — Algorithm.control_device →
		# DevicesManager.control → Device.control → here — and nothing in that chain
		# catches. Anything raising here is counted as an algorithm crash and spends the
		# supervisor's restart budget, because a broker hiccuped or an operator typed a
		# value the transport cannot hold. So the resolution is inside the try too, not
		# just the publish.
		try:
			resolved = self.resolve_downlink(device, payload)
			if resolved is None:
				return
			topic, body = resolved
			# main constructs every worker before starting any of them, so an algorithm
			# can reach this before start() has built the client.
			client = getattr(self, "mqtt_client", None)
			if client is None:
				self.LOGGER.warning(f"Not connected yet, dropping command '{payload}' for '{device.name}'")
				return
			client.publish(topic, body)
		except (OSError, RuntimeError, TypeError, ValueError) as e:
			self.LOGGER.error(f"Error sending '{payload}' to '{device.name}': {e}")
			return
		self.LOGGER.info(f"Sent message to {topic}: {body}")

	@override
	def inject_devices(self, devices: dict[str, Device]) -> None:
		super().inject_devices(devices)  # sets self.devices + back-references
		for device in devices.values():
			if not device.is_readable:
				continue
			subscription, pattern = self.resolve_listener(device)
			if pattern is not None:
				try:
					compiled = compile(pattern)  # regex for client-side message routing
				except error as e:
					self.LOGGER.error(f"Invalid regex '{pattern}' : {e}")
				else:
					# re.compile is memoised, so two devices whose pattern *string* is
					# equal produce the same dict key and the second silently replaces the
					# first — one of them then receives nothing for the life of the run,
					# with no error anywhere. Say so instead of losing it quietly.
					existing = self.callbacks.get(compiled)
					if existing is not None and existing is not device:
						self.LOGGER.error(f"Device '{device.name}' declares the same routing pattern '{pattern}' as '{existing.name}'; '{existing.name}' will stop receiving messages")
					self.callbacks[compiled] = device
			# subscription is the MQTT topic filter (distinct from the routing regex above)
			if subscription is None:
				self.LOGGER.warning(f"Device '{device.name}' has no listener_options.subscription; it will not receive messages")
			elif isinstance(subscription, str):
				self.subscriptions.add(subscription)
			else:
				self.subscriptions.update(subscription)
