from base64 import b64decode, b64encode
from dataclasses import dataclass
from json import dumps
from logging import getLogger
from re import compile, escape
from threading import Lock
from time import monotonic
from typing import Any, Optional, override

from api.capabilities import Switch
from api.device import Device
from api.options import float_option
from connectors.mqtt import MQTTConnector

# A LoRaWAN application port. 0 is reserved for MAC commands and 224 for certification, so
# an application payload lives in 1-223. There is deliberately no default: a command sent to
# the wrong port is silently ignored by the node, which looks exactly like the delivery delay
# Class A produces anyway — an undiagnosable combination.
_MIN_F_PORT = 1
_MAX_F_PORT = 223

# A 64-bit EUI as the topic trees spell it: 16 hex digits, no separators.
_HEX_EUI = compile(r"[0-9A-Fa-f]{16}")

# A template value that is *nothing but* one placeholder, e.g. "{f_port}".
_WHOLE_PLACEHOLDER = compile(r"\{(\w+)\}")


@dataclass(frozen=True)
class Profile:
	"""One network server's MQTT surface: where uplinks arrive, how a downlink is spelled.

	Deliberately only the *transport* half. Where the payload sits inside the uplink JSON is
	`devices/lora.py`'s table, and the two sets are disjoint — a device must work with any
	connector, including a `pseudo` one replaying a capture, so it cannot read this.
	"""
	uplink_topic: str
	downlink_topic: str
	downlink_body: Any
	identifier: str  # which of dev_eui / device_id the topic tree keys on
	username_from_application_id: bool
	queue_modes: tuple[str, ...]
	default_queue_mode: Optional[str]


# Keep the profile *names* in step with devices/lora.py's PROFILES — tests/test_lora_profiles.py
# asserts it. The contents are unrelated on purpose; see Profile's docstring.
PROFILES: dict[str, Profile] = {
	"chirpstack": Profile(
		uplink_topic="application/{application_id}/device/{dev_eui}/event/up",
		downlink_topic="application/{application_id}/device/{dev_eui}/command/down",
		downlink_body={"devEui": "{dev_eui}", "fPort": "{f_port}", "confirmed": "{confirmed}", "data": "{payload_b64}"},
		identifier="dev_eui",
		username_from_application_id=False,
		# ChirpStack can flush a device's downlink queue, but only over its gRPC/REST API —
		# there is no MQTT equivalent, so a stale command already queued *will* be delivered.
		# min_downlink_interval is the only mitigation available here. See the class docstring.
		queue_modes=(),
		default_queue_mode=None,
	),
	"things_stack": Profile(
		uplink_topic="v3/{application_id}/devices/{device_id}/up",
		downlink_topic="v3/{application_id}/devices/{device_id}/down/{queue_mode}",
		downlink_body={"downlinks": [{"f_port": "{f_port}", "frm_payload": "{payload_b64}", "priority": "NORMAL", "confirmed": "{confirmed}"}]},
		identifier="device_id",
		username_from_application_id=True,
		queue_modes=("push", "replace"),
		# "replace" rather than the API's own "push" default, and this is an EMS decision
		# rather than a copied one: a downlink queued forty minutes ago saying "on" must not
		# be delivered after the algorithm has since decided "off". replace clears the queue.
		default_queue_mode="replace",
	),
}


def _render(template: Any, values: dict[str, Any]) -> Any:
	"""Substitute {placeholders} through a JSON template, preserving native types.

	A string that is *exactly* one placeholder takes that value's own type, so
	`"fPort": "{f_port}"` emits `10` and not `"10"`. That is what lets a single table entry —
	or a `custom_downlink_body` written by an operator in `config.json`, where there is no
	way to say "integer" — express an int port beside a string payload. Anything else is
	formatted as text.
	"""
	if isinstance(template, dict):
		return {key: _render(value, values) for key, value in template.items()}
	if isinstance(template, list):
		return [_render(item, values) for item in template]
	if isinstance(template, str):
		whole = _WHOLE_PLACEHOLDER.fullmatch(template)
		if whole is not None:
			return values[whole.group(1)]
		return template.format(**values)
	return template


def _filter_to_regex(topic_filter: str) -> str:
	"""The client-side routing regex for an MQTT topic filter.

	Derived from the *rendered filter* rather than built alongside it, so the broker-side
	subscription and the client-side routing can never describe different topics.

	Three details are load-bearing. Every literal segment is `re.escape`d — an unescaped `.`
	in a device id matches any character, which cross-routes one node's reading onto another
	device: a plausible wrong number attributed to the wrong meter, with nothing erroring
	anywhere. The result is anchored with `$` because `Pattern.match` anchors only the start,
	so `.../event/up` would otherwise also match a future `.../event/uplink`. And `(?i)` goes
	at position 0 because Python accepts a global inline flag nowhere else — which is why
	`MQTTConnector.resolve_listener` returns a string rather than a compiled Pattern.
	"""
	segments = []
	for segment in topic_filter.split("/"):
		if segment == "+":
			segments.append("[^/]+")
		elif segment == "#":
			segments.append(".*")
		else:
			segments.append(escape(segment))
	return "(?i)" + "/".join(segments) + "$"


class LoRaWANConnector(MQTTConnector):
	"""LoRaWAN through a network server's MQTT integration — ChirpStack, The Things Stack, any.

	The EMS never touches a radio here, and that is the whole point: the network server has
	already abstracted every gateway and every end node. What is left varying between
	deployments is the topic tree, the downlink body, the uplink envelope and the payload
	layout — all four data, none of them code. The first two are the profile table above, the
	third is `devices/lora.py`'s, and the fourth is a field map in `config.json`. A network
	server nobody has heard of is `profile: "custom"` plus four templates.

	Subclasses `MQTTConnector` rather than reimplementing it — this *is* MQTT, so the connect
	ladder, paho's reconnect, the resubscribe-on-reconnect and the bounded, order-preserving
	per-device dispatch are already right. Exactly three things are LoRaWAN-specific: the constructor's options, how a
	devEUI becomes a topic filter and a routing regex (`resolve_listener`), and how a Switch
	token becomes base64 inside a vendor JSON envelope (`resolve_downlink`).

	**A downlink is queued, not sent.** A Class A node opens two short receive windows only
	immediately after its own uplink, so a command waits from seconds to an hour depending on
	how often the node reports. Nothing in this connector can shorten that, and three
	consequences follow that are worth stating before they are discovered:

	- `Algorithm.control_device` writes the decision to `algorithm_decisions.csv` as soon as
	  `send()` has accepted the command for delivery — which here means *queued*. That row's
	  timestamp is when the EMS decided, never when the node acted, and nothing downstream
	  confirms it ever did. True of every connector; here the gap is minutes rather than
	  milliseconds, so the distinction is worth stating before it is discovered.
	- `algorithms/auto_toggle.py` re-issues its decision every tick, with no feedback that the
	  last one landed. Unthrottled, that is one queued downlink per tick — which is what
	  `min_downlink_interval` exists to bound.
	- Duty cycle (1% in EU868) is enforced by the gateway and the network server, not here.
	  This connector cannot measure airtime and will not pretend to. What it can do is not
	  flood the queue.

	Note for anyone adding an option: `tests/test_config.py`'s schema-lockstep check inspects
	*this* class's `__init__`, not the parent's, so every inherited MQTT option that
	`lorawan.schema.json` declares has to be an explicit parameter here and forwarded to
	`super()`. `**kwargs` does not satisfy the subset check.
	"""

	profile_name: str
	application_id: Optional[str]
	min_downlink_interval: float

	@override
	def __init__(self, name: str, host: str = "", port: Any = 1883, version: str = "3.1.1",
				 profile: str = "chirpstack", application_id: Optional[str] = None,
				 tenant_id: Optional[str] = None, min_downlink_interval: Any = 0,
				 custom_uplink_topic: Optional[str] = None, custom_downlink_topic: Optional[str] = None,
				 custom_downlink_body: Any = None, custom_topic_identifier: str = "dev_eui",
				 username: Optional[str] = None, password: Optional[str] = None,
				 client_id: Optional[str] = None, tls: Any = False,
				 ca_certs: Optional[str] = None, certfile: Optional[str] = None,
				 keyfile: Optional[str] = None) -> None:
		# self.LOGGER does not exist until Connector.__init__ runs, but the username has to be
		# resolved before it is forwarded to super(). getLogger is a registry, so this is the
		# *same object* Connector.__init__ retrieves a moment later; nothing is duplicated.
		logger = getLogger(f"{self.__class__.__name__}/{name}")

		self.profile_name = str(profile).strip().lower()
		if self.profile_name not in PROFILES and self.profile_name != "custom":
			logger.warning(f"Unknown LoRaWAN profile '{profile}' on {name}, falling back to 'custom'")
			self.profile_name = "custom"

		if self.profile_name == "custom":
			self._profile = Profile(
				uplink_topic=custom_uplink_topic or "",
				downlink_topic=custom_downlink_topic or "",
				downlink_body=custom_downlink_body,
				identifier="device_id" if str(custom_topic_identifier).strip().lower() == "device_id" else "dev_eui",
				username_from_application_id=False,
				queue_modes=(),
				default_queue_mode=None,
			)
			if not custom_uplink_topic:
				logger.warning(f"{name} uses profile 'custom' with no custom_uplink_topic; devices must declare listener_options.subscription and .pattern themselves")
		else:
			self._profile = PROFILES[self.profile_name]

		# The Things Stack puts the tenant in the application id itself. Accepting the two
		# halves separately is what lets one config file carry `TTN_APP` and `TTN_TENANT` as
		# distinct ${VAR}s; the open-source stack has no tenant and the bare id is correct.
		if application_id and tenant_id and "@" not in application_id:
			application_id = f"{application_id}@{tenant_id}"
		self.application_id = application_id
		self.tenant_id = tenant_id

		if self._profile.username_from_application_id:
			if not application_id:
				logger.warning(f"{name} uses profile '{self.profile_name}' with no application_id; it is both the topic segment and the MQTT username, so nothing will route")
			elif username is None:
				# The Things Stack authenticates as {application id}@{tenant} with an API key
				# as the password. Synthesising it is not a convenience — an operator who sets
				# only the API key has given us everything, and the alternative is a silent
				# authentication failure.
				username = application_id
				logger.info(f"{name}: using application_id '{application_id}' as the MQTT username")

		super().__init__(
			name, host=host, port=port, version=version,
			username=username, password=password, client_id=client_id,
			tls=tls, ca_certs=ca_certs, certfile=certfile, keyfile=keyfile,
		)

		self.min_downlink_interval = float_option(self.LOGGER, "min_downlink_interval", min_downlink_interval, 0.0, minimum=0.0)
		self._last_downlink: dict[str, float] = {}
		self._throttled_devices: set[str] = set()
		# send() runs on an algorithm's thread and there may be several; the throttle's
		# read-then-write is not atomic.
		self._downlink_lock = Lock()

		if self.min_downlink_interval == 0:
			self.LOGGER.info(f"{name}: min_downlink_interval is 0, downlinks are not throttled. A Class A queue is shared with every other node on the gateway and an algorithm that re-issues its decision every tick will fill it")
		if self._profile.username_from_application_id and str(self.port) == "8883" and not self.tls:
			self.LOGGER.warning(f"{name} points at port 8883 without tls; The Things Stack will drop the connection immediately and silently")

	@override
	def resolve_listener(self, device: Device) -> tuple[Any, Optional[str]]:
		"""The topic filter and routing regex for one node, synthesised from its devEUI.

		A declared `listener_options.subscription` / `pattern` wins, independently of each
		other, and is passed through **verbatim** — not escaped, not anchored, not
		case-folded. That is what makes a network server no profile describes supportable
		with no code at all: declare the two by hand and this connector becomes a plain
		`MQTTConnector` that happens to build LoRaWAN downlink bodies.
		"""
		declared_subscription = device.listener_options.get("subscription")
		declared_pattern = device.listener_options.get("pattern")
		if declared_subscription is not None and declared_pattern is not None:
			return declared_subscription, declared_pattern

		subscription, pattern = self._synthesize_listener(device)
		return (
			declared_subscription if declared_subscription is not None else subscription,
			declared_pattern if declared_pattern is not None else pattern,
		)

	def _synthesize_listener(self, device: Device) -> tuple[Optional[str], Optional[str]]:
		"""(filter, regex) from the profile's topic tree, or (None, None) having warned."""
		if not self._profile.uplink_topic:
			return None, None
		key = self._profile.identifier
		identifier = device.listener_options.get(key)
		if not identifier:
			self.LOGGER.warning(f"Device '{device.name}' has no listener_options.{key}, which profile '{self.profile_name}' keys its topics on; it will not receive messages")
			return None, None
		normalised = self._normalise_identifier(key, str(identifier))
		if normalised != str(identifier):
			self.LOGGER.info(f"Device '{device.name}': normalised {key} '{identifier}' to '{normalised}'")
		topic_filter = self._profile.uplink_topic.format(
			# An unset application id becomes the single-level wildcard, which is a legal
			# filter and the right answer for a ChirpStack deployment with one application.
			application_id=self.application_id or "+",
			tenant_id=self.tenant_id or "+",
			dev_eui=normalised if key == "dev_eui" else "+",
			device_id=normalised if key == "device_id" else "+",
		)
		return topic_filter, _filter_to_regex(topic_filter)

	@staticmethod
	def _normalise_identifier(key: str, value: str) -> str:
		"""Lower-case a hex EUI and strip its separators; leave anything else alone.

		Not cosmetic. **MQTT topic filters are case-sensitive and there is no
		case-insensitive wildcard**, so a subscription built from the `70B3D57ED0001234` an
		operator pasted off a datasheet never matches ChirpStack's lower-cased topic — and
		the symptom is total silence, with no error at any layer. A Things Stack `device_id`
		is an opaque operator-chosen name and is never touched.
		"""
		if key != "dev_eui":
			return value
		stripped = value.replace("-", "").replace(":", "").replace(" ", "").strip()
		return stripped.lower() if _HEX_EUI.fullmatch(stripped) else value

	@override
	def resolve_downlink(self, device: Device, payload: str) -> Optional[tuple[str, str]]:
		"""A Switch token as base64 inside the network server's downlink envelope.

		Returns None having logged why, for every reachable failure — `send()` runs on an
		algorithm's supervised thread and a raise there is counted as an algorithm crash.
		"""
		options = device.controller_options
		f_port = self._resolve_f_port(device)
		if f_port is None:
			return None

		remaining = self._throttle(device, options)
		if remaining is not None:
			return None

		try:
			raw = self._resolve_payload(payload, options)
			values = {
				"payload_b64": b64encode(raw).decode("ascii"),
				"payload_hex": raw.hex(),
				"f_port": f_port,
				"confirmed": self._as_bool(options.get("confirmed"), False),
				"dev_eui": self._normalise_identifier("dev_eui", str(device.listener_options.get("dev_eui", ""))),
				"device_id": str(device.listener_options.get("device_id", "")),
				"application_id": self.application_id or "",
				"tenant_id": self.tenant_id or "",
				"queue_mode": self._resolve_queue_mode(options),
			}
			topic = options.get("topic") or (self._profile.downlink_topic.format(**values) if self._profile.downlink_topic else "")
			if not topic:
				self.LOGGER.warning(f"Device '{device.name}': profile '{self.profile_name}' declares no downlink topic and controller_options.topic is unset, dropping command '{payload}'")
				return None
			if self._profile.downlink_body is None:
				self.LOGGER.warning(f"Device '{device.name}': profile '{self.profile_name}' declares no downlink body, dropping command '{payload}'")
				return None
			body = dumps(_render(self._profile.downlink_body, values))
		except (AttributeError, IndexError, KeyError, TypeError, ValueError) as e:
			self.LOGGER.error(f"Cannot turn command '{payload}' for '{device.name}' into a downlink: {e}")
			return None

		# "Queued", never "sent". The parent's "Sent message to ..." would be a lie here, and
		# an operator watching a relay not move for twenty minutes will start power-cycling
		# hardware that is working correctly.
		self.LOGGER.info(f"Queued downlink for '{device.name}' on fPort {f_port} ({len(raw)} byte(s), confirmed={values['confirmed']}) — LoRaWAN delivers it in the receive window after the node's next uplink")
		return topic, body

	def _resolve_f_port(self, device: Device) -> Optional[int]:
		"""The application port, or None having warned. Deliberately has no default.

		Ports 1-223 are application-defined, 0 is MAC-only and 224 is certification, so there
		is nothing to guess from. A command sent to a port the node does not listen on is
		accepted by the network server, delivered, and ignored — indistinguishable from the
		delivery delay Class A produces anyway.
		"""
		raw = device.controller_options.get("f_port")
		if raw is None or raw == "":
			self.LOGGER.warning(f"Device '{device.name}' has no controller_options.f_port; a LoRaWAN downlink has no default port, so the command is dropped")
			return None
		try:
			f_port = int(raw)
		except (TypeError, ValueError):
			self.LOGGER.warning(f"Device '{device.name}' declares a non-numeric controller_options.f_port {raw!r}, dropping the command")
			return None
		if not _MIN_F_PORT <= f_port <= _MAX_F_PORT:
			self.LOGGER.warning(f"Device '{device.name}' declares controller_options.f_port {f_port}, outside the application range {_MIN_F_PORT}-{_MAX_F_PORT}, dropping the command")
			return None
		return f_port

	def _throttle(self, device: Device, options: dict[str, Any]) -> Optional[float]:
		"""Seconds still to wait, or None when the downlink may go now.

		Coalescing — hold the newest command and flush it when the interval elapses — would
		be better behaviour, and is deliberately not done: this connector is parked inside
		paho's `loop_forever` with no tick of its own, so it would need a thread per connector
		to hold at most one string. On The Things Stack `queue_mode: "replace"` achieves the
		same thing on the server's side, for free.
		"""
		interval = float_option(self.LOGGER, f"{device.name}.min_downlink_interval", options.get("min_downlink_interval"), self.min_downlink_interval, minimum=0.0)
		if interval <= 0:
			return None
		now = monotonic()
		first = False
		recovered = False
		with self._downlink_lock:
			previous = self._last_downlink.get(device.name)
			if previous is not None and now - previous < interval:
				remaining = interval - (now - previous)
				first = device.name not in self._throttled_devices
				self._throttled_devices.add(device.name)
			else:
				self._last_downlink[device.name] = now
				remaining = None
				recovered = device.name in self._throttled_devices
				self._throttled_devices.discard(device.name)
		if remaining is None:
			if recovered:
				self.LOGGER.info(f"Downlinks to '{device.name}' resumed")
			return None
		# Edge-triggered: an algorithm on a ten-second tick would otherwise log six times a
		# minute, forever.
		message = f"Dropping downlink to '{device.name}': {remaining:.1f}s left of the {interval:.0f}s min_downlink_interval"
		if first:
			self.LOGGER.warning(message)
		else:
			self.LOGGER.debug(message)
		return remaining

	@staticmethod
	def _resolve_payload(payload: str, options: dict[str, Any]) -> bytes:
		"""The application payload bytes a command carries.

		`payload_encoding` is declared, never inferred. "Does this look like hex" would make
		`"00"` and `"0"` mean different things depending on parity, which is the kind of rule
		nobody can hold in their head at 3am.
		"""
		token = payload.strip().lower()
		if token == Switch.COMMAND_ON:
			raw = str(options.get("on_payload", "01"))
		elif token == Switch.COMMAND_OFF:
			raw = str(options.get("off_payload", "00"))
		else:
			raw = payload
		encoding = str(options.get("payload_encoding", "hex")).strip().lower()
		if encoding == "utf8":
			return raw.encode("utf-8")
		if encoding == "base64":
			return b64decode(raw, validate=True)
		return bytes.fromhex(raw)

	def _resolve_queue_mode(self, options: dict[str, Any]) -> str:
		"""push (append) or replace (clear the queue first), for servers that offer both."""
		if not self._profile.queue_modes:
			return ""
		declared = str(options.get("queue_mode", "")).strip().lower()
		if declared in self._profile.queue_modes:
			return declared
		if declared:
			self.LOGGER.warning(f"Unknown queue_mode '{declared}' for profile '{self.profile_name}', using '{self._profile.default_queue_mode}'")
		return self._profile.default_queue_mode or self._profile.queue_modes[0]

	@staticmethod
	def _as_bool(value: Any, default: bool) -> bool:
		"""Local rather than api/options.py's bool_option: this one takes no logger, and a
		per-command coercion must not log once per tick."""
		if isinstance(value, bool):
			return value
		if value is None:
			return default
		return str(value).strip().lower() in ("1", "true", "yes", "on")
