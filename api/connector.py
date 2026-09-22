from abc import ABC, abstractmethod
from logging import Logger, getLogger
from typing import Any, Optional

from api.device import Device
from api.stoppable import Stoppable


class Connector(Stoppable, ABC):
	name: str
	devices: dict[str, Device]
	LOGGER: Logger

	@abstractmethod
	def __init__(self, name: str) -> None:
		super().__init__()  # Stoppable: arms the cooperative-stop event
		self.LOGGER = getLogger(f"{self.__class__.__name__}/{name}")
		self.name = name
		# Device names currently failing inside deliver(). Once-then-quiet bookkeeping, held
		# on the base so every connector gets it without a constructor edit. Mutated from the
		# per-device dispatch threads connectors/mqtt.py runs, so two devices can race — `set.add` and
		# `set.discard` are atomic under the GIL and the worst a race costs is a duplicate
		# first ERROR, which is a better trade than a lock on the logging path of every
		# message.
		self._raising_devices: set[str] = set()

	@abstractmethod
	def start(self) -> None:
		"""Blocking run loop, driven by the supervisor in its own thread.

		Poll `self.is_stopping()` and sleep via `self.wait_stop(seconds)` so a
		shutdown request is honoured promptly; override `stop()` if the loop
		blocks on something an event alone cannot interrupt (see `Stoppable`).
		"""
		pass

	def inject_devices(self, devices: dict[str, Device]) -> None:
		self.devices = devices
		for device in devices.values():
			device.connector = self  # back-reference for control commands
		self.LOGGER.info(f"{len(devices)} device(s) injected: {list(devices.keys())}")

	@abstractmethod
	def send(self, device: Device, payload: str) -> None:
		pass

	def on_connected(self) -> None:
		"""Call this in concrete connectors once transport is established"""
		for device in self.devices.values():
			if not device.is_readable:
				device.mark_connected()
		self.LOGGER.info("Connector established, write-only devices marked connected")

	def deliver(self, device: Device, *args: Any) -> bool:
		"""Hand one payload to a device and run the framework hook. True if it survived.

		**The only place a connector calls `device.receive()`.** Call this rather than the
		two-step: the two calls belong together, and one of them has to be inside a guard.

		A device is a plugin, and the contract for a payload it cannot serve is to return
		False, never to raise (`api/device.py`, and the rule in CLAUDE.md). A device that
		raised anyway used to end the run. On a polling connector the exception escapes
		`start()`, `SupervisedWorker._run` counts a crash, five restarts and ~31s of backoff
		later it logs CRITICAL and sets `_finished` — and `main`'s
		`all(worker.is_finished())` reads that as "all connectors finished" and shuts the EMS
		down, so one meter's register-map typo silences the battery, the grid meter and every
		algorithm reading them. On `connectors/mqtt.py` it was quieter and no better: the call
		runs on that connector's per-device dispatch thread, which is the top of its own stack,
		so the traceback went to `threading.excepthook` —
		raw stderr, past every configured handler — the supervisor never counted it,
		`/workers` never showed it, and the device simply stopped reporting.

		This is **not** the rule the narrow handlers in `connectors/modbus_tcp.py` and
		`connectors/home_assistant.py` state. Those guard *our own* frames — one pymodbus
		transaction, one websocket session — where a TypeError is our bug and the supervisor's
		traceback is the right remedy. The only foreign frame in this `try` is the plugin's,
		and there the supervisor is the wrong remedy three times over: the restart cannot fix
		a bug that is deterministic on that payload shape; the unit it restarts is the
		connector, so one device's bug takes out every other device on the same transport; and
		the budget it spends is shared with the algorithms, so crash protection gets weaker
		everywhere. The rule is therefore about *whose frame is in the try*, and it is
		greppable: narrow when ours is, broad when a plugin's is. After this, the read path
		through `connectors/` holds no bare `except Exception` at all — this is its one plugin
		boundary. The single broad catch left in that directory is
		`connectors/home_assistant.py`'s `abort()` during `stop()`, which is neither: it
		swallows a teardown race on the way out, and says so.

		Containing it is not hiding it. The traceback is logged and no new reading is published,
		so storage records the gap the device actually produced rather than republishing its
		last value under a fresh timestamp — a broken device stops producing, visibly, instead
		of taking the process with it. Note what does *not* happen: `data_ready` is a latching
		Event, so a device that produced one good reading before it started raising keeps
		reporting ready. That flag answers "has this device ever delivered", not "is it healthy
		now", and the honest signal for the latter is the ERROR below.

		`*args` only, never `**kwargs`: CONTRIBUTING.md pins the two positional arities
		because `PseudoConnector` replays a topic and a payload column out of a CSV, so a
		device reachable only by keyword would be un-backtestable. This must not offer a shape
		the replay contract forbids.
		"""
		try:
			accepted = device.receive(*args)
			self.on_device_data_received(device, accepted)
		except Exception as e:
			# The routing key, never the payload. A two-argument call passes the topic or
			# entity_id first and it is short by construction; a payload is unbounded — a DSMR
			# telegram or a get_states snapshot would put kilobytes in the log on every poll of
			# a failing device. The traceback carries the rest.
			hint = f"'{args[0]}'" if len(args) > 1 else "a payload"
			if device.name in self._raising_devices:
				# Once, then quiet, then once on the way back — the shape `devices/p1.py`'s
				# `_parse_failing` and `http_api`'s `_failed_devices` already use. A device
				# polled every second would otherwise write 86 400 tracebacks a day, which is a
				# log nobody reads and a disk that fills. DEBUG keeps `exc_info`: if you turned
				# DEBUG on for this device, the traceback is what you turned it on for.
				self.LOGGER.debug(f"Device '{device.name}' raised again on {hint}: {e}", exc_info=True)
			else:
				self._raising_devices.add(device.name)
				# exc_info, always. This handler exists for the exception nobody predicted, and
				# f"...: {e}" on a KeyError renders as a bare quoted key — the symptom with no
				# type, no file and no line. `SupervisedWorker._run` logs its crashes with the
				# traceback for the same reason, and this stands in for it.
				self.LOGGER.error(
					f"Device '{device.name}' ({type(device).__name__}) raised on {hint}, dropping this reading: {e}",
					exc_info=True,
				)
			# Something arrived, so the transport reaches this device. Reporting it as
			# never-connected would point an operator at the network instead of at the code,
			# and would show a healthy meter as unreachable on /devices. This is exactly what
			# `on_device_data_received(device, False)` does — the contract that already exists
			# for "a payload arrived and produced no reading" — because a device that raised is
			# a device that should have returned False, and this guard's job is to make a broken
			# device behave like a correct one rather than to invent a third state.
			#
			# Called directly rather than through the hook, and that is the difference between
			# a guard that holds and one that holds *as long as nobody overrides the hook*. The
			# `except` above may have fired because the hook itself raised, so re-entering it
			# here would be the one path on which `deliver()` could still raise — defeating the
			# whole point one frame from the end. `Event.set()` cannot fail; that is the entire
			# reason this line is safe, and it stays true however the hook changes.
			device.mark_connected()
			return False
		if device.name in self._raising_devices:
			self._raising_devices.discard(device.name)
			# Deliberately not "Device 'x' recovered": that string is taken by http_api and
			# modbus_tcp for *transport* recovery and asserted on by their tests. This one
			# means the device stopped crashing on payloads the transport was delivering all
			# along.
			self.LOGGER.info(f"Device '{device.name}' is handling payloads again")
		return True

	def on_device_data_received(self, device: Device, accepted: Optional[bool] = True) -> None:
		"""Called by `deliver()` after device.receive(), with its result.

		A payload arriving always proves the transport is alive, but only an accepted one
		is a reading. Publishing a rejected payload would re-report the device's previous
		value under the new timestamp, turning a stalled or corrupt meter into a flat line
		in storage instead of the gap it actually is.

		`accepted` defaults to True so a connector written before this contract existed
		keeps publishing everything, exactly as it did.

		Reached through `deliver()`, which is what puts a guard around both this and the
		`receive()` above it.
		"""
		device.mark_connected()  # something arrived, so the connection is live
		if accepted is False:
			return
		device.mark_data_ready()
		from devices_manager.devices_manager import DevicesManager
		DevicesManager().update_device(device)  # publish live snapshot so algorithms see it
		from storage_manager.storage_manager import StorageManager
		StorageManager().write_device_data(device, device.data)
