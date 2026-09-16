from abc import ABC, abstractmethod
from logging import Logger, getLogger
from typing import TYPE_CHECKING

from api.devices_access import DevicesAccess
from api.stoppable import Stoppable

if TYPE_CHECKING:
	from supervisor.supervisor import Supervisor


class Service(Stoppable, ABC):
	"""A config-declared supervised worker that owns no devices.

	The fifth plugin axis, loaded exactly like the other four: `services/<class>.py`,
	config key `class`, class name `<Class>Service`.

	What a service shares with a connector is only what the supervisor asks for — a
	blocking `start()` run in its own daemon thread, and a `stop()` that unblocks it.
	What it does not share is devices: `send()` and `inject_devices()` would be lies on
	an observer.

	**A service is not part of the run's liveness.** `main` waits on the connectors only,
	so a server that never returns cannot keep a finished replay alive — which is also
	why an API cannot simply be declared as a `Connector`: it would join that set and hang
	every backtest forever. The corollary is worth knowing before you debug it: a config
	with services and no connectors exits immediately, exactly as a config with no workers
	at all does today.

	Both runtime handles are injected by `main` through `create_classes(arguments=...)`,
	never by config. They are declared on the ABC rather than left to each subclass
	because `create_classes` spreads `arguments` into *every* plugin on the axis: a
	service that did not accept them would be skipped with a `TypeError`. Taking them as
	parameters instead of reaching for a singleton is also what keeps a service
	unit-testable.
	"""
	name: str
	devices_manager: DevicesAccess
	supervisor: "Supervisor"
	LOGGER: Logger

	@abstractmethod
	def __init__(self, name: str, devices_manager: DevicesAccess, supervisor: "Supervisor") -> None:
		super().__init__()  # Stoppable: arms the cooperative-stop event
		self.LOGGER = getLogger(f"{self.__class__.__name__}/{name}")
		self.name = name
		self.devices_manager = devices_manager
		self.supervisor = supervisor

	@abstractmethod
	def start(self) -> None:
		"""Blocking run loop, driven by the supervisor in its own thread.

		Named `start` rather than `run` so `main` supervises services with the same method
		name it already supervises connectors with.

		Poll `self.is_stopping()` and sleep via `self.wait_stop(seconds)` so a shutdown is
		honoured promptly; override `stop()` when the loop blocks on something an event
		alone cannot reach — a foreign server's blocking `run()`, say (see `Stoppable` and
		`services/rest_api.py`).

		Returning is a *normal completion* and is never restarted; raising is a crash,
		logged with its traceback and restarted with bounded backoff.

		Import optional dependencies at **module top**, not in here: `create_classes`
		catches `ModuleNotFoundError` and skips the plugin with one honest error line
		while the rest of the EMS starts, whereas the same error raised in here is a crash
		— five restarts with backoff, then CRITICAL — because an operator did not install
		an optional extra.
		"""
		pass
