from abc import ABC, abstractmethod
from datetime import datetime
from logging import Logger, getLogger
from time import monotonic, time
from typing import Callable

from api.device import Device
from api.devices_access import DevicesAccess
from api.stoppable import Stoppable

_UNSET = object()

# Readiness gates are polled in slices so a shutdown during startup is not stuck
# behind an Event.wait() that nothing is going to satisfy
_READINESS_POLL_SECONDS = 0.5

# How long a clock-driven step waits before coming back to check the stop flag
_STEP_POLL_SECONDS = 0.5


class Algorithm(Stoppable, ABC):
	name: str
	delay_seconds: float
	devices_manager: DevicesAccess
	devices: dict[str, Device]
	LOGGER: Logger
	required_devices: list[str] = []  # energy expert declares this, or override via config
	wait_for_devices_timeout: float | None = 60.0  # configurable, None = wait forever
	# Run accounting, filled by _run_main(). Class-level defaults, so an algorithm written
	# before this existed reports honestly (never run) instead of raising on attribute
	# access. `last_run_seconds` is the only place an overrun is visible anywhere in the
	# system: loop()'s `max(0., delay_seconds - elapsed)` silently swallows an algorithm
	# that takes longer than its own cadence — it simply stops sleeping, and says nothing.
	last_run_at: datetime | None = None
	last_run_seconds: float | None = None
	runs: int = 0

	@abstractmethod
	def __init__(self, name: str, devices_manager: DevicesAccess, delay_seconds: float = 15 * 60,
				 required_devices: list[str] | None = None, wait_for_devices_timeout: float | None = _UNSET) -> None:
		super().__init__()  # Stoppable: arms the cooperative-stop event
		self.LOGGER = getLogger(f"{self.__class__.__name__}/{name}")
		self.name = name
		self.delay_seconds = delay_seconds
		self.devices_manager = devices_manager
		if required_devices is not None:
			self.required_devices = required_devices
		if wait_for_devices_timeout is not _UNSET:
			self.wait_for_devices_timeout = wait_for_devices_timeout
		# Registered here, not in loop(): main.py supervises connectors before
		# algorithms, so a replay can reach its first timestep before this thread is
		# running. Construction happens on the main thread before any worker starts,
		# which is the only point that reliably precedes the first step.
		from simulation.clock import SimulationClock
		SimulationClock().join(self.name)

	def loop(self) -> None:
		"""Steps main() until stop() is requested. Returns cleanly on shutdown.

		Two cadences, chosen per iteration: one step per committed replay timestep when
		a replay connector drives the clock, and a `delay_seconds` wall-clock cadence
		otherwise — so one `main()` backtests and runs live unchanged.
		"""
		from simulation.clock import SimulationClock
		clock = SimulationClock()
		# Re-join: __init__ already registered us, but the supervisor restarts a crashed
		# algorithm by re-entering loop() on the same object, and the previous run's
		# `finally` deregistered it. Joining at the current generation is right — a
		# restarted algorithm is not answerable for the step it died on.
		clock.join(self.name)
		# Whether this algorithm has processed at least one replay timestep. It is what
		# separates "running live" from "was being replayed, and the replay ended".
		stepped = False
		try:
			self._wait_for_required_devices()
			while not self.is_stopping():
				step = clock.wait_for_step(self.name, _STEP_POLL_SECONDS)
				if step is not None:
					stepped = True
					try:
						self._run_main()
					finally:
						# In a finally so a raising main() cannot strand the replay: the
						# supervisor will restart us, but the backtest must keep moving.
						clock.ack(self.name, step.generation)
				elif clock.is_simulated():
					continue  # between timesteps; the replay has not published the next one
				elif stepped:
					# The replay that was driving us called SimulationClock.reset(). Falling
					# through to the wall-clock branch here would fire one more main() in
					# the 0-500ms it takes main to notice the connectors finished and stop
					# us — a decision belonging to no timestep, stamped with the wall clock
					# because both clocks are now None, appended to the end of a backtest.
					# Whether it landed at all depended on where the poll happened to be,
					# so it was a race that changed a backtest's output row count.
					self.LOGGER.info("Replay finished, stopping")
					break
				else:
					self._run_main()
					self.wait_stop(max(0., self.delay_seconds - (self.last_run_seconds or 0.)))
		finally:
			clock.leave(self.name)
		self.LOGGER.info("Loop stopped")

	def _run_main(self) -> None:
		"""One accounted invocation of main().

		Wraps *both* cadences — a replay step and a wall-clock tick — so an algorithm
		reports the same way whether it is backtesting or running live; recording in only
		one of them is how the two would drift.

		The bookkeeping sits in a `finally` and the exception propagates unchanged: the
		supervisor still owns crash handling, this only counts. A crashed step is still a
		step that happened, so it counts too.

		`last_run_at` is deliberately the wall clock, not the simulation clock. The
		question it answers is operational — did this algorithm run in the last fifteen
		*real* minutes — and the simulated moment is already available separately through
		`devices_manager.get_simulation_time()`.
		"""
		started = time()
		try:
			self.main()
		finally:
			self.runs += 1
			self.last_run_seconds = time() - started
			self.last_run_at = datetime.now()

	@abstractmethod
	def main(self) -> None:
		self.devices = self.devices_manager.get_devices()
		pass

	def control_device(self, device: Device, command: str) -> None:
		"""Routes a control command to the live device and logs the decision to storage."""
		self.devices_manager.control(device.name, command)
		from storage_manager.storage_manager import StorageManager
		StorageManager().write_algorithm_decision(self.name, device.name, command)

	def _wait_for_required_devices(self) -> None:
		if not self.required_devices:
			return
		devices = self.devices_manager.get_devices()
		for device_name in self.required_devices:
			if self.is_stopping():
				return
			device = devices.get(device_name)
			if device is None:
				self.LOGGER.error(f"Required device '{device_name}' not found")
				continue
			self.LOGGER.info(f"Waiting for '{device_name}'...")
			ready = self._wait_interruptibly(device.wait_until_ready)
			connected = self._wait_interruptibly(device.wait_until_connected)
			if self.is_stopping():
				return
			if not ready or not connected:
				self.LOGGER.warning(f"Timeout waiting for '{device_name}', proceeding anyway")

	def _wait_interruptibly(self, wait_call: Callable[..., bool]) -> bool:
		"""Run a device readiness wait so that stop() can cut it short.

		Both timeouts poll in slices, for the same reason. Handing the whole wait to the
		Event in one call is what a finite `wait_for_devices_timeout` used to do, and the
		stop flag then went unread for the length of it — twice per required device, since
		the caller waits on ready and connected separately, so a site whose hardware is
		absent could hold a Ctrl-C for 2 x timeout x len(required_devices) and be reported
		as a straggler after `main`'s grace period. A timeout of None means "wait forever",
		which is this loop with no deadline.
		"""
		timeout = self.wait_for_devices_timeout
		deadline = None if timeout is None else monotonic() + timeout
		while not self.is_stopping():
			# Clamped at 0 rather than returned on: a timeout of 0 is a poll of the
			# current state, and it still gets its one wait_call.
			remaining = None if deadline is None else max(0., deadline - monotonic())
			slice_seconds = _READINESS_POLL_SECONDS if remaining is None else min(remaining, _READINESS_POLL_SECONDS)
			if wait_call(timeout=slice_seconds):
				return True
			if remaining is not None and remaining <= _READINESS_POLL_SECONDS:
				return False  # that slice ran out the deadline
		return False
