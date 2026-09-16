from dataclasses import dataclass, field
from json import dumps
from threading import Lock
from time import monotonic
from typing import Any, Optional, override

from pymodbus import ModbusException
from pymodbus.client import ModbusTcpClient

from api.capabilities import Switch
from api.connector import Connector
from api.device import Device
from api.options import bool_option, float_option, int_option

# The longest the poll loop will sleep before re-checking the stop event. Sleeps are
# already interruptible — `wait_stop` returns as soon as `stop()` is called — so this only
# bounds how stale a newly injected interval can be, not shutdown latency.
_MAX_SLEEP_SECONDS = 60.0

# The four Modbus tables are four separate address spaces, so which one a register lives in
# is part of its address, not a formatting detail: address 0 is four different things.
# Method *names* rather than bound methods, because the client only exists once start() has
# run and this table has to survive inject_devices(), which runs on the main thread before.
_READERS: dict[str, str] = {
	"holding": "read_holding_registers",
	"input": "read_input_registers",
	"coil": "read_coils",
	"discrete": "read_discrete_inputs",
}
_BIT_TABLES = frozenset({"coil", "discrete"})

# Modbus PDU limits. A request asking for more comes back as an IllegalDataValue exception
# response, which reads as "the meter is broken" rather than "the config is".
_MAX_COUNT: dict[str, int] = {"holding": 125, "input": 125, "coil": 2000, "discrete": 2000}

# Register widths in 16-bit words, used only to default `count` when a register map omits
# it. Deliberately duplicated from devices/modbus_meter.py rather than imported: a
# connector must work with any device and a device with any connector, so neither package
# may import the other. Six lines of table is the price of keeping the two axes
# independent — keep them in step, `tests/test_modbus_meter.py` asserts they agree.
_WORDS: dict[str, int] = {
	"INT16": 1, "UINT16": 1, "RAW": 1,
	"INT32": 2, "UINT32": 2, "FLOAT32": 2,
	"INT64": 4, "UINT64": 4, "FLOAT64": 4,
}


@dataclass(frozen=True)
class RegisterRead:
	"""One Modbus request, resolved once at injection time."""
	name: str
	table: str      # holding | input | coil | discrete
	reader: str     # the ModbusTcpClient method name for that table
	address: int
	count: int
	device_id: int


@dataclass
class PollTask:
	"""One device's polling schedule and read plan, resolved once at injection time.

	Mirrors `HttpApiConnector.PollTask`: a dataclass rather than a dict because `last_poll`
	needs somewhere to live that is not a second structure keyed by device name.
	"""
	device: Device
	reads: list[RegisterRead]
	interval: float
	last_poll: float = field(default=0.0, compare=False)

	def next_due(self) -> float:
		"""Monotonic timestamp at which this device should next be polled."""
		return self.last_poll + self.interval


class ModbusTcpConnector(Connector):
	"""Polls Modbus TCP registers, each device on its own interval.

	Each device declares its own register map in `listener_options.registers`, so one
	generic device class covers many meter models without new code — which is the whole
	point of a Modbus integration, since the protocol standardises the framing and
	standardises nothing about what lives at which address.

	The connector reads raw 16-bit words and decodes **nothing**: it hands the device a
	JSON string of the words it read. Decoding lives in the device (see
	`devices/modbus_meter.py`), which is what lets a Modbus meter be replayed from a CSV
	with `emulates: "modbus_tcp"` on a machine that has no Modbus stack installed at all.
	"""
	host: str
	port: int
	device_id: int
	timeout: float
	retries: int
	default_interval: float
	reconnect_backoff_seconds: float
	max_reconnect_backoff_seconds: float
	_client: Optional[ModbusTcpClient]
	_lock: Lock
	_poll_tasks: list[PollTask]
	_failed_devices: set[str]
	_failed_reads: set[str]
	# None until the first connect attempt resolves, so a never-reachable meter and a
	# meter that dropped can be told apart in the logs.
	_connected: Optional[bool]

	@override
	def __init__(self, name: str, host: str, port: Any = 502, device_id: Any = 1,
				 timeout: Any = 3, retries: Any = 1, default_interval: Any = 10,
				 reconnect_backoff_seconds: Any = 1, max_reconnect_backoff_seconds: Any = 60,
				 close_on_error: Any = True) -> None:
		super().__init__(name)  # first: the coercion helpers below log through self.LOGGER
		self.host = host
		# Coerced, not taken raw: Config validates the plugin schema *before* resolving
		# ${VAR}, so a "${MODBUS_PORT}" declared `number` in modbus_tcp.schema.json still
		# arrives here as a str — and a ValueError escaping __init__ escapes
		# main.create_classes too, taking the whole process down over one tuning knob.
		self.port = int_option(self.LOGGER, "port", port, 502, minimum=1, maximum=65535)
		# 0 is broadcast and 1..247 are the valid RS-485 slave addresses, but 255 is the
		# Modbus-TCP "unit id not used" convention many native-TCP meters expect — so the
		# cap is 255, not 247.
		self.device_id = int_option(self.LOGGER, "device_id", device_id, 1, minimum=0, maximum=255)
		# minimum=0.1: a zero timeout is a non-blocking socket, i.e. every read fails
		# instantly. The maxima on this and `retries` bound the worst-case in-flight
		# transaction (timeout x retries) against main's 10s shutdown grace — see stop().
		self.timeout = float_option(self.LOGGER, "timeout", timeout, 3.0, minimum=0.1, maximum=60.0)
		# Default 1, not pymodbus's 3. A *poller* gains nothing from in-transaction
		# retries — the next poll is the retry — and at pymodbus's default the worst-case
		# block is 9s against a 10s shutdown grace.
		self.retries = int_option(self.LOGGER, "retries", retries, 1, minimum=1, maximum=10)
		# minimum=0.1, unlike HttpApiConnector's 0.0: a poll interval of 0 makes the loop
		# spin on wait_stop(0.0), and a cheap RS-485 meter behind a gateway wedges under it.
		self.default_interval = float_option(self.LOGGER, "default_interval", default_interval, 10.0, minimum=0.1)
		# Load-bearing floor. The ladder is `delay = min(delay * 2, max_delay)`, and with
		# delay == 0.0 that is 0.0 forever: a hot reconnect loop against a dead meter,
		# at full CPU, with no log throttling.
		self.reconnect_backoff_seconds = float_option(self.LOGGER, "reconnect_backoff_seconds", reconnect_backoff_seconds, 1.0, minimum=0.1)
		self.max_reconnect_backoff_seconds = float_option(self.LOGGER, "max_reconnect_backoff_seconds", max_reconnect_backoff_seconds, 60.0, minimum=0.1)
		# Some gateways answer one transaction per connection and then go quiet; dropping
		# the socket after a transport error is what recovers those. A meter that is simply
		# slow is better served by keeping it, hence the knob.
		self.close_on_error = bool_option(self.LOGGER, "close_on_error", close_on_error, True)

		# Built in start(), never here. Three reasons: a stop() arriving before start()
		# then has nothing to tear down (main wraps startup in try/finally, so shutdown()
		# reaches connectors that never ran); main.create_classes catches only
		# AttributeError/ModuleNotFoundError/TypeError, so anything a client constructor
		# raises on a malformed host would take the process down before storage is even
		# registered; and @patch("connectors.modbus_tcp.ModbusTcpClient") only works as a
		# test seam if construction happens inside a method a test can drive.
		# HttpApiConnector building its Session in __init__ is not a counter-precedent: a
		# Session is a connection *pool* with no connect() and no half-built state.
		self._client = None
		self._lock = Lock()
		self._poll_tasks = []
		self._failed_devices = set()
		self._failed_reads = set()
		self._connected = None

	@override
	def inject_devices(self, devices: dict[str, Device]) -> None:
		super().inject_devices(devices)
		self._poll_tasks = []
		for device in devices.values():
			if not device.is_readable:
				continue
			registers = device.listener_options.get("registers")
			if not registers:
				self.LOGGER.warning(f"Device '{device.name}' has no listener_options.registers, skipping polling")
				continue
			# Resolution order for the unit id is register -> device -> connector. The
			# device level is the useful one: one Modbus TCP gateway commonly fronts
			# several RS-485 slaves, so one host:port addresses many meters.
			device_default_id = int_option(self.LOGGER, f"{device.name}.device_id",
										   device.listener_options.get("device_id"), self.device_id,
										   minimum=0, maximum=255)
			reads = self._build_reads(device, registers, device_default_id)
			if not reads:
				self.LOGGER.warning(f"Device '{device.name}' declared no usable register, skipping polling")
				continue
			self._poll_tasks.append(PollTask(
				device=device,
				reads=reads,
				interval=float_option(self.LOGGER, f"{device.name}.interval",
									  device.listener_options.get("interval"), self.default_interval, minimum=0.1),
			))
		self.LOGGER.info(f"{len(self._poll_tasks)} polling task(s) configured")

	def _build_reads(self, device: Device, registers: Any, device_default_id: int) -> list[RegisterRead]:
		"""Turn a declared register map into one resolved request per entry.

		One request per entry, deliberately: no contiguous-address grouping. Meters
		routinely leave holes in their maps and a great many answer IllegalDataAddress for
		the *entire* PDU when any address in the span is unimplemented — so grouping turns
		a config where 11 of 12 registers work into one where 0 of 12 do, and the error
		names a synthetic range rather than the register the operator has to fix. `count`
		already covers the only case that is contiguous by construction (a 32/64-bit value,
		or a STRING). Explicit grouping declared by whoever holds the vendor's register-map
		PDF would be the right optimisation if a real site ever measures a real problem.
		"""
		reads: list[RegisterRead] = []
		if not isinstance(registers, list):
			self.LOGGER.warning(f"listener_options.registers on '{device.name}' is not a list, ignoring")
			return reads
		seen: set[str] = set()
		for entry in registers:
			if not isinstance(entry, dict):
				self.LOGGER.warning(f"Ignoring non-object register entry on '{device.name}': {entry!r}")
				continue
			name = entry.get("name")
			if not isinstance(name, str) or not name:
				self.LOGGER.warning(f"Ignoring register with no name on '{device.name}': {entry!r}")
				continue
			if name in seen:
				# The name is the join key between the connector's blocks and the device's
				# decoder, and blocks is a dict — a duplicate would silently drop one.
				self.LOGGER.warning(f"Duplicate register name '{name}' on '{device.name}', ignoring the second")
				continue
			table = str(entry.get("type", "holding")).strip().lower()
			if table not in _READERS:
				self.LOGGER.warning(f"Register '{name}' on '{device.name}' has unknown type '{table}', expected one of {sorted(_READERS)}")
				continue
			address = int_option(self.LOGGER, f"{device.name}.{name}.address", entry.get("address"), -1, minimum=0, maximum=65535)
			if address < 0:
				self.LOGGER.warning(f"Register '{name}' on '{device.name}' has no usable address, skipping it")
				continue
			data_type = str(entry.get("data_type", "UINT16")).strip().upper()
			default_count = 1 if table in _BIT_TABLES else _WORDS.get(data_type, 1)
			count = int_option(self.LOGGER, f"{device.name}.{name}.count", entry.get("count"), default_count,
							   minimum=1, maximum=_MAX_COUNT[table])
			seen.add(name)
			reads.append(RegisterRead(
				name=name,
				table=table,
				reader=_READERS[table],
				address=address,
				count=count,
				device_id=int_option(self.LOGGER, f"{device.name}.{name}.device_id",
									 entry.get("device_id"), device_default_id, minimum=0, maximum=255),
			))
		return reads

	@override
	def start(self) -> None:
		"""Blocking poll loop: connect, read every device on its interval, until stopped."""
		if not self.host:
			# Returning rather than raising: an empty host is a config error no restart
			# fixes, and the supervisor would spend its whole budget on it. Same shape as
			# PseudoConnector's missing replay file.
			self.LOGGER.error("No host configured, connector idle")
			return
		if not self._poll_tasks:
			self.LOGGER.warning("No polling tasks configured, connector idle")
			return

		self._client = ModbusTcpClient(self.host, port=self.port, timeout=self.timeout, retries=self.retries)
		self.LOGGER.info(f"Starting Modbus TCP polling on {self.host}:{self.port} ({len(self._poll_tasks)} device(s))")
		backoff = self.reconnect_backoff_seconds
		try:
			while not self.is_stopping():
				if not self._ensure_connected():
					# Reconnect here rather than raising to be restarted. The restart budget
					# (max_restarts, default 5) is for programming faults; a gateway that
					# drops TCP nightly or a meter that reboots on a firmware update would
					# burn it over a long run — and past the cap the worker is *finished*,
					# which makes main shut the entire EMS down. A rebooting meter must not
					# terminate the process.
					if self.wait_stop(backoff):
						break
					backoff = min(backoff * 2, self.max_reconnect_backoff_seconds)
					continue
				backoff = self.reconnect_backoff_seconds  # a live session resets the ladder

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
			# Closed here rather than in stop(): tearing the socket down under an in-flight
			# transaction from another thread is a race on a client that is not
			# thread-safe, and pymodbus would surface it as a spurious ConnectionException
			# traceback during a *clean* shutdown. Same rule as HttpApiConnector's session.
			self._close_client()
			self.LOGGER.info("Polling stopped, Modbus connection closed")

	def _ensure_connected(self) -> bool:
		"""Connect if needed. True when a transaction can be attempted.

		Connect/disconnect transitions are logged edge-triggered: an unreachable meter is
		retried on the backoff ladder for as long as it stays down, and one line per
		attempt would be the only thing in the log by morning.
		"""
		reason: Optional[str] = None
		with self._lock:
			client = self._client
			if client is None:
				return False
			if client.connected:
				established = True
			else:
				try:
					established = bool(client.connect())
					if not established:
						reason = "connection refused or timed out"
				except (ModbusException, OSError) as e:
					established = False
					reason = str(e)

		if not established:
			if self._connected is None:
				# Never connected: say so once at ERROR. Demoting the first failure to
				# DEBUG would leave an unreachable meter completely silent at the default
				# log level, which is the one case an operator most needs to see.
				self.LOGGER.error(f"Cannot reach {self.host}:{self.port} ({reason}), retrying with backoff")
			elif self._connected:
				self.LOGGER.warning(f"Lost connection to {self.host}:{self.port} ({reason}), reconnecting")
			else:
				self.LOGGER.debug(f"Still cannot reach {self.host}:{self.port} ({reason})")
			self._connected = False
			return False
		if not self._connected:
			self._connected = True
			self.LOGGER.info(f"Connected to {self.host}:{self.port}")
			self.on_connected()  # marks write-only devices as connected
		return True

	def _poll_device(self, task: PollTask) -> None:
		"""Read every declared block for one device and hand it a single payload."""
		device = task.device
		blocks: dict[str, list[Any]] = {}
		transport_failed = False

		for read in task.reads:
			key = f"{device.name}.{read.name}"
			try:
				result = self._call(read.reader, read.address, count=read.count, device_id=read.device_id)
			except (ModbusException, OSError) as e:
				# Narrow on purpose, never bare Exception: a bug in our own code should
				# reach the supervisor with its traceback, which is what it is for.
				transport_failed = True
				if key not in self._failed_reads:
					self._failed_reads.add(key)
					self.LOGGER.error(f"Transport error reading '{key}' at {read.address}: {e}")
				break  # the socket is gone; the remaining blocks would only repeat this
			if result is None:
				transport_failed = True  # no client: stopping, or a teardown raced us
				break
			if result.isError():
				# A Modbus *exception response*, not a Python exception: the meter answered,
				# and said no. Usually an undeclared register or a wrong unit id — a config
				# problem, so the other blocks are still worth reading.
				if key not in self._failed_reads:
					self._failed_reads.add(key)
					self.LOGGER.warning(f"Modbus exception reading '{key}' at {read.address}: {result}")
				continue
			# read_coils pads to a byte boundary, so `bits` is routinely longer than count.
			blocks[read.name] = list(result.bits[:read.count]) if read.table in _BIT_TABLES else list(result.registers)
			if key in self._failed_reads:
				self.LOGGER.info(f"Register '{key}' recovered")
				self._failed_reads.discard(key)

		if transport_failed:
			self._failed_devices.add(device.name)
			if self.close_on_error:
				self._disconnect()  # forces the loop's reconnect branch on the next pass
			return
		if not blocks:
			# Every block came back an exception response. Nothing arrived that is a
			# reading, so there is nothing for the device to accept or reject — calling
			# on_device_data_received() here would mark it connected and data-ready off a
			# payload we never built.
			self._failed_devices.add(device.name)
			return

		accepted = device.receive(dumps({"blocks": blocks}))
		self.on_device_data_received(device, accepted)
		if device.name in self._failed_devices:
			self.LOGGER.info(f"Device '{device.name}' recovered")
			self._failed_devices.discard(device.name)

	@override
	def send(self, device: Device, payload: str) -> None:
		"""Write one coil or holding register, routed by `controller_options`."""
		options = device.controller_options
		address = int_option(self.LOGGER, f"{device.name}.controller.address", options.get("address"), -1,
							 minimum=0, maximum=65535)
		if address < 0:
			self.LOGGER.warning(f"Device '{device.name}' has no usable controller_options.address, cannot send")
			return
		device_id = int_option(
			self.LOGGER, f"{device.name}.controller.device_id", options.get("device_id"),
			int_option(self.LOGGER, f"{device.name}.device_id", device.listener_options.get("device_id"),
					   self.device_id, minimum=0, maximum=255),
			minimum=0, maximum=255,
		)
		kind = str(options.get("kind", "coil")).strip().lower()
		command = payload.strip().lower()

		# Everything below is inside the try, not just the transaction: send() runs on the
		# *algorithm's* thread (Algorithm.control_device -> DevicesManager.control ->
		# Device.control -> here) and nothing in that chain catches. A raise would crash the
		# algorithm's supervised worker — five restarts, backoff, CRITICAL — because a relay
		# did not answer or an operator typed a value the meter cannot hold.
		try:
			if kind == "coil":
				if command == Switch.COMMAND_ON:
					result = self._call("write_coil", address, True, device_id=device_id)
				elif command == Switch.COMMAND_OFF:
					result = self._call("write_coil", address, False, device_id=device_id)
				else:
					self.LOGGER.warning(f"Unknown coil command '{payload}' for '{device.name}', expected on/off")
					return
			elif kind == "register":
				value = self._register_value(device, command, options)
				if value is None:
					return  # already warned
				result = self._call("write_register", address, value, device_id=device_id)
			else:
				self.LOGGER.warning(f"Unknown controller_options.kind '{kind}' for '{device.name}', expected coil/register")
				return
		except (ModbusException, OSError) as e:
			self.LOGGER.error(f"Error sending '{payload}' to '{device.name}' at {address}: {e}")
			return

		if result is None:
			self.LOGGER.warning(f"Not connected, dropped command '{payload}' for '{device.name}'")
		elif result.isError():
			self.LOGGER.warning(f"Modbus exception writing '{payload}' to '{device.name}' at {address}: {result}")
		else:
			self.LOGGER.info(f"Sent {kind} write to '{device.name}' at {address}: {payload}")

	def _register_value(self, device: Device, command: str, options: dict[str, Any]) -> Optional[int]:
		"""The 16-bit word a register write should carry, or None (having warned).

		Multi-register writes are deliberately not implemented: a 32-bit setpoint needs an
		encoder, and encoding lives on the device side of this connector's contract. Named
		here rather than silently missing.
		"""
		if options.get("count") not in (None, 1):
			self.LOGGER.warning(f"Multi-register writes are not implemented; ignoring controller_options.count on '{device.name}'")
		if command == Switch.COMMAND_ON:
			raw: Any = options.get("on_value", 1)
		elif command == Switch.COMMAND_OFF:
			raw = options.get("off_value", 0)
		else:
			raw = command
		# scale is the inverse of the read path's `value = raw * scale`, so an operator
		# states the multiplier once, the way the datasheet does.
		scale = float_option(self.LOGGER, f"{device.name}.controller.scale", options.get("scale"), 1.0, minimum=1e-9)
		try:
			value = int(round(float(raw) / scale))
		except (TypeError, ValueError):
			self.LOGGER.warning(f"Command '{raw!r}' for '{device.name}' is not a number, cannot write a register")
			return None
		signed = bool_option(self.LOGGER, f"{device.name}.controller.signed", options.get("signed"), False)
		low, high = (-32768, 32767) if signed else (0, 65535)
		if not low <= value <= high:
			# Out of range would otherwise be a pymodbus ParameterException or, worse,
			# a silent truncation that writes a plausible wrong setpoint.
			self.LOGGER.warning(f"Value {value} for '{device.name}' is outside {low}..{high}, not writing")
			return None
		return value

	def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
		"""Every ModbusTcpClient call goes through here, under the lock.

		The client is not thread-safe and two threads reach it: the supervised poll thread,
		and any algorithm thread calling device.control(). Modbus TCP is strictly
		request/response over one socket, so two interleaved transactions each read the
		other's reply — which surfaces as a *plausible wrong number*, a power reading in a
		kWh register, not as an error.

		The lock is per *transaction*, not per device poll, deliberately: holding it across
		a whole device would block a control command for N round trips, and control latency
		is the point of an EMS. A send() therefore waits at most one transaction. The
		consequence — a write landing between two of a device's register blocks — is
		harmless: a relay command does not invalidate a kWh reading.

		Returns None when there is no client, so every caller has one uniform "not
		connected" answer instead of a TOCTOU race on self._client.
		"""
		with self._lock:
			client = self._client
			if client is None:
				return None
			return getattr(client, method)(*args, **kwargs)

	def _disconnect(self) -> None:
		"""Drop the socket so the loop's next pass reconnects. Keeps the client object."""
		with self._lock:
			client = self._client
			if client is None:
				return
			try:
				client.close()
			except (ModbusException, OSError) as e:
				self.LOGGER.debug(f"Error closing the Modbus socket: {e}")
		self._connected = False

	def _close_client(self) -> None:
		"""Shutdown teardown: close and forget the client, under the lock."""
		with self._lock:
			client, self._client = self._client, None
			if client is not None:
				try:
					client.close()
				except (ModbusException, OSError) as e:
					self.LOGGER.debug(f"Error closing the Modbus client: {e}")
		self._connected = False

	# stop() is deliberately NOT overridden. Every sleep in start() is wait_stop(), which
	# the base event interrupts, and the only other blocking point is a socket read bounded
	# by timeout x retries (3s at the defaults, against a 10s shutdown grace) with
	# is_stopping() checked between devices. MQTTConnector must override stop() because
	# loop_forever() blocks inside *paho's* event loop, which the stop event cannot reach;
	# here the loop is ours. Closing the socket from the main thread while the poll thread
	# is mid-transaction would be a race on a client that is not thread-safe.
