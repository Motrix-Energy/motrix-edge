from re import Pattern, compile, error
from threading import Thread
from typing import Any, Optional, override

from paho.mqtt.client import Client, MQTTMessage
# noinspection PyUnresolvedReferences
from paho.mqtt.enums import CallbackAPIVersion, MQTTProtocolVersion
from paho.mqtt.reasoncodes import ReasonCode

from api.connector import Connector
from api.device import Device
from api.options import bool_option, int_option
from config.enums.mqtt_version import MQTTVersion

# noinspection PyUnresolvedReferences

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

	def _protocol_version(self, version: Any) -> MQTTProtocolVersion:
		"""Coerce the configured version, warning and defaulting rather than raising.

		`MQTTVersion` is a StrEnum, so an unknown value raises `ValueError` — and the
		commonest unknown value is `None`, which is what `"version": "${MQTT_VERSION}"`
		interpolates to when the variable is unset. `main.create_classes` catches only
		AttributeError, ModuleNotFoundError and TypeError, so that ValueError would not
		skip this connector the way a bad option normally does: it escapes and takes the
		whole process down, over one unset environment variable. Same reasoning as
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

	@override
	def stop(self) -> None:
		super().stop()
		# loop_forever() blocks in paho's own event loop; disconnecting is what makes it return.
		# mqtt_client only exists once start() ran, so a stop before/without start is a no-op.
		client = getattr(self, "mqtt_client", None)
		if client is not None:
			client.disconnect()

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
				Thread(target=self._receive_and_notify, args=(device, topic, payload), daemon=True).start()

	def _receive_and_notify(self, device: Device, topic: str, payload: str) -> None:
		accepted = device.receive(topic, payload)
		self.on_device_data_received(device, accepted)  # marks connected, publishes if accepted

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
