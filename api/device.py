import copy
from abc import ABC, abstractmethod
from logging import Logger, getLogger
from threading import Event
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from connector import Connector

# The one refusal that reads the same for every device, so it lives here rather than being
# copied into six tables. A replay connector that declares `emulates` never reaches this —
# Config resolves that key before injecting, so the device sees the transport being stood in
# for. Seeing the literal "pseudo" therefore means the key is *absent*, and the fix is one
# line of config rather than a branch in a device.
_REPLAY_WITHOUT_EMULATES = (
	"a replay connector must declare `emulates: \"{protocol}\"` in its options for a device "
	"that dispatches on protocol to recognise it"
)


class Device(ABC):
	# The protocols this device can actually parse a payload from. Empty means "does not
	# dispatch on protocol at all" — devices/pseudo.py takes anything from anywhere — and
	# switches the whole check off, so a device that makes no claim is never accused of
	# breaking one.
	#
	# This is the single source of truth, not a second copy of anything: it *replaced* the
	# `match self.connector_options["protocol"]` each device used to carry, so there is no
	# other list of labels left to drift from. The derivation that would have avoided a
	# declaration entirely — `hasattr(self, f"receive_{protocol}")` — does not work, because
	# devices/lora.py serves "lorawan", "lora" and plain "mqtt" out of one receive_lorawan.
	#
	# A tuple, so a subclass cannot append to a list it shares with its base.
	SUPPORTED_PROTOCOLS: tuple[str, ...] = ()

	# Per-protocol refusal sentences, for the few transports where the generic one would
	# waste an operator's afternoon. An entry earns its place by carrying knowledge a generic
	# sentence could not produce — devices/p1.py's LoRaWAN payload arithmetic is the worked
	# example. This must not become a transcription of `ls connectors/`: anything absent
	# falls through to PROTOCOL_REFUSAL, which is still a true statement about this device.
	UNSERVABLE_PROTOCOLS: dict[str, str] = {}

	# What to say about every other protocol. One sentence naming what this device's payload
	# actually *is*, because "unsupported protocol" tells an operator nothing they did not
	# already know from the config line they just wrote.
	PROTOCOL_REFUSAL: str = ""

	name: str
	data: dict[str, Any]
	connector_options: dict[str, Any]
	listener_options: dict[str, Any]
	controller_options: dict[str, Any]
	connector: "Connector"
	LOGGER: Logger
	_data_ready_event: Event
	_connected_event: Event
	is_readable: bool = True  # receives data from connector
	is_writable: bool = False  # can receive control commands
	@abstractmethod
	def __init__(self, name: str, connector_options: dict[str, Any], listener_options: dict[str, Any], controller_options: dict[str, Any]) -> None:
		self.LOGGER = getLogger(f"{self.__class__.__name__}/{name}")
		self.name = name
		self.connector_options = connector_options
		self.listener_options = listener_options
		self.controller_options = controller_options
		self.data = {}
		self._data_ready_event = Event()
		self._connected_event = Event()
		if not self.is_readable:
			self._data_ready_event.set()  # no data to wait for
		# Say it once, here, rather than once per message from a connector's thread. This is
		# startup, on the main thread, where an operator reads logs — and it fires whether or
		# not a payload ever arrives, so a device wired to the wrong connector is a line at
		# boot instead of one that is quietly never going to report.
		#
		# In the base rather than left to each device, because this is the part of the
		# contract a device can omit without failing any test of its own — which is exactly
		# how devices/shelly_plug.py came to ignore the both-arities rule CONTRIBUTING.md had
		# stated for some time. Declaring SUPPORTED_PROTOCOLS is now the only thing a device
		# has to do to get it right.
		#
		# It does **not** prevent the device from being created, and it deliberately does not
		# raise to achieve that. `main.create_classes` now contains every exception a plugin
		# can throw, so raising here would no longer end the run — but it would still skip
		# this entry, and a device that silently does not exist is worse than one that visibly
		# never reports: an algorithm summing EnergyMeters would compute a site total short one
		# meter with nothing anywhere saying so. An ERROR naming the device and a `receive()`
		# that refuses every payload says the same thing, twice, and leaves the rest of the
		# config running.
		#
		# Which is also why nothing on this path may raise. `protocol_refusal` guards the
		# unhashable case; the isinstance here guards the other one — `connector_options`
		# arriving as something other than a dict would raise AttributeError, and
		# create_classes *catches* AttributeError, so the device would vanish without a word.
		# That is precisely the failure this line exists to report.
		#
		# Same idiom, and same reasoning, as devices/ha_switch.py and devices/lora_switch.py:
		# warn once about a misconfiguration, never once per operation.
		protocol = connector_options.get("protocol") if isinstance(connector_options, dict) else None
		refusal = self.protocol_refusal(protocol)
		if refusal is not None:
			self.LOGGER.error(
				f"{self.name} is a {self.__class__.__name__} on the {protocol!r} protocol: {refusal}. "
				f"This device will never produce a reading"
			)

	@classmethod
	def protocol_refusal(cls, protocol: Any) -> Optional[str]:
		"""Why this device cannot serve `protocol`, or None when it can.

		One table consulted from two places — `__init__`, so an operator hears it once at
		startup on the main thread, and `receive()`, so a payload that arrives anyway is
		refused rather than parsed.

		The `isinstance` guard before either membership test is load-bearing, not decoration.
		`protocol` is annotated `str` wherever it is declared, but it arrives from JSON by way
		of Config, which injects `options.emulates or connector["protocol"]` and only *warns*
		when the schema refuses either. A list or an object there is unhashable, so `in` on a
		dict would raise TypeError out of `__init__` — which `main.create_classes` catches,
		silently dropping the device instead of creating one that says why it is useless.
		That is the outcome the ERROR in `__init__` exists to prevent, so it must not be
		reachable from here.

		A device's own table is consulted before the shared `pseudo` sentence, so a device
		with something better to say about a replay can still say it.
		"""
		if not cls.SUPPORTED_PROTOCOLS:
			return None
		if isinstance(protocol, str):
			if protocol in cls.SUPPORTED_PROTOCOLS:
				return None
			if protocol in cls.UNSERVABLE_PROTOCOLS:
				return cls.UNSERVABLE_PROTOCOLS[protocol]
			if protocol == "pseudo":
				return _REPLAY_WITHOUT_EMULATES.format(protocol=cls.SUPPORTED_PROTOCOLS[0])
		return cls.PROTOCOL_REFUSAL or (
			"it reads " + " or ".join(f"'{p}'" for p in cls.SUPPORTED_PROTOCOLS) + ", and nothing else"
		)

	def refuse_unserved_protocol(self, *args, **kwargs) -> bool:
		"""True when this payload must be dropped because its protocol is not served.

		The first line of every `receive()` that dispatches on protocol. It logs at DEBUG and
		nothing louder: the ERROR was already stated once at construction, so a misconfigured
		device costs one line at boot rather than one per message for the life of the run.

		`.get`, not a subscript, and that is the same decision one step along from the raise
		this replaced. A KeyError here escapes onto whichever connector thread called
		`receive()`. `Connector.deliver` now catches it, but being caught by the guard meant
		for a *device bug* is the wrong way for a plain config mistake to surface — it would
		log a traceback and rate-limit the repeat, where this says the one sentence that names
		the fix. Nothing diagnostic is lost by tolerating a missing key either: `None` is not
		a served protocol, so it is refused and reported like any other wrong value.
		"""
		protocol = self.connector_options.get("protocol") if isinstance(self.connector_options, dict) else None
		if self.protocol_refusal(protocol) is None:
			return False
		self.LOGGER.debug(f"Refusing a payload on {protocol!r} for {self.name}: {args=}, {kwargs=}")
		return True

	def __deepcopy__(self, memo):
		"""Snapshot the *data*; share the live handles.

		A snapshot exists so an algorithm can read a device without racing the connector
		that writes it. Two attributes are not data and are deliberately shared rather
		than copied — both hold unpicklable locks, so copying them was never an option
		anyway, and both must reach the live object to mean anything:

		- `connector`: control has to arrive at the real transport, not at a copy of a
		  paho client.
		- the readiness `Event`s: they are how the connector *signals* the device, so a
		  copied Event is a handle nothing will ever set. Copying them made
		  `Algorithm._wait_for_required_devices` — which waits on a snapshot — burn its
		  full `wait_for_devices_timeout` on every required device, twice, however ready
		  the device actually was, and under a replay that silently cost the opening
		  timesteps of the backtest. Sharing also makes `is_data_ready()` / `is_connected()`
		  answer for *now* rather than for whenever the snapshot was taken, which is what
		  every caller of them wants.

		Nothing writes through a snapshot: `mark_data_ready()` and `mark_connected()` are
		called by connectors, on the live device.
		"""
		cls = self.__class__
		new = cls.__new__(cls)
		memo[id(self)] = new
		for k, v in self.__dict__.items():
			if isinstance(v, Event) or k == "connector":
				setattr(new, k, v)
			else:
				setattr(new, k, copy.deepcopy(v, memo))
		return new

	def is_data_ready(self) -> bool:
		return self._data_ready_event.is_set()

	def is_connected(self) -> bool:
		"""Non-blocking read of the connected event, the counterpart to is_data_ready().

		`wait_until_connected(timeout=0)` answers the same question, but putting a *wait*
		call in a read path is one careless edit away from blocking the caller forever.
		"""
		return self._connected_event.is_set()

	def mark_data_ready(self) -> None:
		"""Called by connector after first successful receive()"""
		self._data_ready_event.set()

	def mark_connected(self) -> None:
		"""Called by connector once transport is established"""
		self._connected_event.set()

	def wait_until_connected(self, timeout: float | None = None) -> bool:
		return self._connected_event.wait(timeout=timeout)

	def wait_until_ready(self, timeout: float | None = None) -> bool:
		"""Returns True if ready, False if timed out"""
		return self._data_ready_event.wait(timeout=timeout)
	@abstractmethod
	def receive(self, *args, **kwargs) -> Optional[bool]:
		"""Parse raw transport data into `self.data`.

		Return **False** when the payload produced no usable update — a corrupt frame, a
		topic this device does not handle — so the framework records a gap rather than
		republishing the previous reading under a new timestamp. Anything else, `None`
		included, counts as accepted, so a device written before this contract existed
		keeps working unchanged.
		"""
		pass

	def control(self, command: str) -> bool:
		"""Hand one command to this device's connector. True if it reached a transport.
		
		The return value is the whole point, and it is consumed one frame up: an
		algorithm records its decision in the versioned storage contract only when the
		command actually went somewhere. Both refusals below are ordinary configuration
		outcomes rather than faults — a read-only device that subclasses `Switch`, a
		device whose connector entry names a connector that failed to load — so this
		warns and answers False rather than raising. `Algorithm.control_device` used to
		write the decision regardless, which put a row into `algorithm_decisions.csv`
		every tick describing a command no hardware ever saw.
		
		A connector raising inside `send()` is a different thing and is deliberately not
		caught here: it unwinds past the decision write on its own, and CONTRIBUTING.md
		tells connector authors to handle their own transport errors inside `send()`.
		"""
		if not self.is_writable:
			self.LOGGER.warning(f"Device '{self.name}' is not writable, ignoring command '{command}'")
			return False
		if not hasattr(self, "connector"):
			self.LOGGER.warning("Cannot send control command: no connector assigned")
			return False
		self.connector.send(self, command)
		return True
