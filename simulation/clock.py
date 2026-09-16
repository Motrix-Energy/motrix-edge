from dataclasses import dataclass
from datetime import datetime
from logging import Logger, getLogger
from threading import Condition
from time import monotonic
from typing import Callable, Optional

from __metaclasses.singleton import Singleton

# Barrier waits are sliced so a shutdown during a wait is observed promptly instead
# of sitting out the whole step timeout.
_STOP_POLL_SECONDS = 0.1


@dataclass(frozen=True)
class Step:
	"""One committed replay timestep, handed to an algorithm to process."""
	generation: int
	time: datetime


class SimulationClock(metaclass=Singleton):
	"""The replay clock, and the barrier that keeps a backtest deterministic.

	Two clocks, because one value cannot serve both readers:

	- **event time** is the timestamp of the data being dispatched *right now*. The
	  replay advances it **before** each entry, so a storage backend writing a device
	  reading stamps it with the moment that reading was produced.
	- **step time** is the last *committed* timestep. The replay advances it **after**
	  the final entry of a timestep, so an algorithm woken by it sees every device at
	  that timestep rather than a half-updated set. This is what `get_simulation_time()`
	  returns, and its semantics are unchanged from when it was the only clock.

	On top of the step clock sits a **barrier**: each algorithm joins as a participant
	and acknowledges every step it finishes, and the replay waits for all outstanding
	acknowledgements before dispatching the next timestep. Without it the replay merely
	published a step and slept 0.01s, which is a handshake and not a guarantee — a
	`main()` slower than that sleep let the replay run ahead, so the algorithm read
	devices from a later timestep than the one it stepped on and skipped timesteps
	outright.

	Deliberately *not* stored on `DevicesManager`: a replay thread waiting for
	algorithms would be holding `devices_lock`, blocking the very `update_device()`
	calls the algorithms are waiting for.
	"""
	LOGGER: Logger

	def __init__(self) -> None:
		self.LOGGER = getLogger(self.__class__.__name__)
		self._condition = Condition()
		self._simulated = False
		self._event_time: Optional[datetime] = None
		self._step_time: Optional[datetime] = None
		self._generation = 0
		# participant -> the last generation it finished. A participant is "pending"
		# while that is behind the current generation.
		self._acked: dict[str, int] = {}

	# --- mode ------------------------------------------------------------------

	def start_simulation(self) -> None:
		"""Declare that this run is driven by a replay clock rather than the wall clock.

		Called from `PseudoConnector.__init__`, which runs on the main thread during
		startup — before any worker exists. Waiting until the replay thread published
		its first step would leave a window in which an algorithm sees no clock, takes
		the wall-clock branch, and disappears into a `delay_seconds` sleep while the
		replay is already waiting for it.
		"""
		with self._condition:
			self._simulated = True
			self._condition.notify_all()

	def is_simulated(self) -> bool:
		with self._condition:
			return self._simulated

	def reset(self) -> None:
		"""End of replay: drop both clocks and hand algorithms back to the wall clock."""
		with self._condition:
			self._simulated = False
			self._event_time = None
			self._step_time = None
			self._condition.notify_all()

	# --- writing (replay thread) -----------------------------------------------

	def set_event_time(self, moment: Optional[datetime]) -> None:
		"""Advance the event clock to the entry about to be dispatched."""
		with self._condition:
			self._event_time = moment

	def publish_step(self, moment: datetime) -> None:
		"""Commit a timestep: every device for it has been updated."""
		with self._condition:
			self._simulated = True
			self._step_time = moment
			self._generation += 1
			self._condition.notify_all()

	def wait_for_completion(self, timeout: Optional[float], is_stopping: Callable[[], bool]) -> list[str]:
		"""Block until every participant has finished the current step.

		Returns the participants that did not finish in time — empty on success, and
		empty on a stop, where a laggard is expected rather than a fault.
		"""
		if timeout is not None and timeout <= 0:
			return []  # lockstep disabled
		deadline = None if timeout is None else monotonic() + timeout
		with self._condition:
			while True:
				# any() rather than _pending(): this loop wakes ten times a second, and
				# the list is only ever needed on the timeout path below.
				if not self._any_pending() or is_stopping():
					return []
				remaining = None if deadline is None else deadline - monotonic()
				if remaining is not None and remaining <= 0:
					return self._pending()
				slice_seconds = _STOP_POLL_SECONDS if remaining is None else min(remaining, _STOP_POLL_SECONDS)
				self._condition.wait(slice_seconds)

	def _pending(self) -> list[str]:
		"""Participants that have not acknowledged the current step. Caller holds the lock."""
		return [name for name, generation in self._acked.items() if generation < self._generation]

	def _any_pending(self) -> bool:
		"""Whether anyone still owes an ack for the current step. Caller holds the lock."""
		return any(generation < self._generation for generation in self._acked.values())

	# --- participating (algorithm threads) -------------------------------------

	def join(self, name: str) -> None:
		"""Register as a participant the replay must wait for.

		A new participant joins at the current generation, so one that appears mid-run is
		not held responsible for a step it never saw. Re-joining is a no-op rather than a
		reset: an algorithm registers at construction *and* on entering its loop, and the
		second call must not discard a step published in between — that would silently
		skip the first timestep of every backtest.
		"""
		with self._condition:
			self._acked.setdefault(name, self._generation)
			self._condition.notify_all()

	def leave(self, name: str) -> None:
		"""Deregister. Releases a replay that is waiting on this participant."""
		with self._condition:
			self._acked.pop(name, None)
			self._condition.notify_all()

	def wait_for_step(self, name: str, timeout: float) -> Optional[Step]:
		"""Block until there is a step for this participant to process.

		Returns None when this run is not simulated (the caller should fall back to its
		wall-clock cadence) or when nothing arrived within `timeout` (the caller should
		poll its own stop flag and come back).
		"""
		with self._condition:
			if not self._simulated:
				return None
			if not self._is_pending(name):
				self._condition.wait(timeout)
			if not self._simulated or self._step_time is None or not self._is_pending(name):
				return None
			return Step(self._generation, self._step_time)

	def ack(self, name: str, generation: int) -> None:
		"""Report a finished step, releasing the replay."""
		with self._condition:
			# max(): an ack for an older generation must never walk the marker back and
			# make a participant look permanently behind.
			self._acked[name] = max(self._acked.get(name, generation), generation)
			self._condition.notify_all()

	def _is_pending(self, name: str) -> bool:
		"""Caller holds the lock. An unregistered name is treated as up to date."""
		return self._acked.get(name, self._generation) < self._generation

	def participants(self) -> list[str]:
		"""Everyone the replay currently waits for."""
		with self._condition:
			return sorted(self._acked)

	def pending(self) -> list[str]:
		"""Participants that have not yet acknowledged the current step.

		The "which algorithm is the replay stuck on *right now*" answer. Without it that
		only surfaces in `PseudoConnector`'s laggard warning, which fires after
		`step_timeout_seconds` has already expired — thirty seconds late by default.
		"""
		with self._condition:
			return sorted(self._pending())

	def generation(self) -> int:
		"""The current step number.

		Monotonic for the life of the process: `reset()` drops both clocks but
		deliberately does not rewind this, so it stays a usable progress counter across a
		replay that ends and one that loops. Step time cannot serve — a looping replay
		restarts and its timestamps go backwards.
		"""
		with self._condition:
			return self._generation

	# --- reading (any thread) --------------------------------------------------

	def get_step_time(self) -> Optional[datetime]:
		"""The last committed timestep. What algorithms step on and decisions are stamped with."""
		with self._condition:
			return self._step_time

	def get_event_time(self) -> Optional[datetime]:
		"""The timestamp of the data currently being dispatched.

		Falls back to the step clock so a third-party replay connector that only
		publishes steps still stamps its readings sensibly.
		"""
		with self._condition:
			return self._event_time if self._event_time is not None else self._step_time
