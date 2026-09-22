from dataclasses import dataclass
from logging import Logger, getLogger
from threading import Event, Thread
from time import monotonic
from typing import Any, Callable, Optional

from api.options import bool_option, float_option, int_option


@dataclass(frozen=True)
class RestartPolicy:
	"""How a supervised worker reacts to an unexpected death.

	A crash is always logged loudly; `enabled` only decides whether the worker is
	then brought back. Restarts are bounded and back off exponentially so a
	deterministic crash cannot spin the CPU.
	"""
	enabled: bool = True
	max_restarts: int = 5
	backoff_seconds: float = 1.0
	max_backoff_seconds: float = 60.0

	@classmethod
	def from_runtime(cls, runtime: dict[str, Any]) -> "RestartPolicy":
		"""Build from the config `runtime` block (see `config.schema.json`).

		Coerced through `api/options.py` rather than with bare `int()`/`float()`: this
		runs in `Main.__init__`, *before* the `try/finally` in `main()` is entered, so a
		raise here has no shutdown path — a mistyped tuning knob would be a bare traceback
		with the storage backends never closed. It also means `"restart": "false"` from an
		interpolated `${VAR}` disables restarts, where `bool("false")` enabled them.
		"""
		defaults = cls()
		logger = getLogger(cls.__name__)
		return cls(
			enabled=bool_option(logger, "restart", runtime.get("restart"), defaults.enabled),
			max_restarts=int_option(logger, "max_restarts", runtime.get("max_restarts"), defaults.max_restarts),
			backoff_seconds=float_option(logger, "backoff_seconds", runtime.get("backoff_seconds"), defaults.backoff_seconds),
			max_backoff_seconds=float_option(logger, "max_backoff_seconds", runtime.get("max_backoff_seconds"), defaults.max_backoff_seconds),
		)


class SupervisedWorker:
	"""One daemon thread running one blocking method, watched for death.

	A restart re-invokes the target inside the *same* thread, so the thread name
	stays stable and callers keep a handle whose identity survives restarts.

	The three outcomes are deliberately distinguished:
	- the target **raises** — logged as ERROR with its traceback, then restarted
	  per policy;
	- the target **returns** — a normal completion (a finished replay, a stub
	  connector, an idle poller), logged as INFO and never restarted;
	- the target **exits** — a library calling sys.exit() on a worker thread,
	  logged as CRITICAL and deliberately *not* restarted. See `_run`.
	"""
	name: str
	policy: RestartPolicy
	restarts: int
	crashes: int
	completed_cleanly: bool
	LOGGER: Logger

	def __init__(self, worker: Any, method_name: str, policy: RestartPolicy, logger: Optional[Logger] = None) -> None:
		self.worker = worker
		self.name = getattr(worker, "name", worker.__class__.__name__)
		self.policy = policy
		self.LOGGER = logger or getLogger(self.__class__.__name__)
		self._target: Callable[[], None] = getattr(worker, method_name)
		self._stop_event = Event()
		self._finished = Event()
		self._thread = Thread(target=self._run, name=self.name, daemon=True)
		self.restarts = 0
		self.crashes = 0
		# `_finished` is set from a `finally` in `_run`, which is the only construction that
		# covers every exit path: the six the loop takes deliberately (a clean return, a
		# SystemExit, a crash during shutdown, a disabled restart policy, an exhausted restart
		# budget, a stop during backoff) and the BaseException that unwinds straight through it.
		# A bare `self._finished.set()` after the loop covered only the first six, so a worker
		# whose target called sys.exit() left it clear forever — and main's
		# `all(worker.is_finished())` then polled a connector that would never finish, leaving
		# the EMS neither restarting nor exiting until SIGTERM. From outside, no
		# combination of `restarts`/`crashes`/`policy` separates them: a policy with
		# restart disabled breaks with `restarts == 0`, and a worker that crashed three
		# times before returning normally has `crashes > 0`. This flag is the only honest
		# answer to "did it finish, or did it give up", which is what a health check needs.
		self.completed_cleanly = False

	def start(self) -> None:
		self._thread.start()

	def _run(self) -> None:
		delay = self.policy.backoff_seconds
		# The whole loop sits inside the try: `_finished` must be set however this method
		# leaves, including by a BaseException that nothing here catches.
		try:
			while True:
				try:
					self._target()
				except SystemExit as e:
					self.crashes += 1
					# A give-up, not a restart, and the distinction is load-bearing:
					# services/rest_api.py converts uvicorn's sys.exit() into a RuntimeError
					# precisely so a port that will not bind earns the restart a crash earns.
					# Were this branch to restart as well, that conversion would buy nothing
					# and become dead code. Counted as a crash so /workers reports the same
					# numbers for both paths.
					self.LOGGER.critical(
						f"Worker '{self.name}' exited with SystemExit({e.code!r}) instead of returning; "
						f"not restarting. A worker returns or raises — only main decides when this run ends"
					)
					break
				except Exception:
					self.crashes += 1
					# Loud, and through the configured logger — the default
					# threading.excepthook would bypass it and print to stderr
					self.LOGGER.exception(f"Worker '{self.name}' crashed")
				else:
					self.LOGGER.info(f"Worker '{self.name}' finished")
					self.completed_cleanly = True
					break
				if self._stop_event.is_set():
					self.LOGGER.info(f"Worker '{self.name}' crashed during shutdown, not restarting")
					break
				if not self.policy.enabled:
					self.LOGGER.critical(f"Worker '{self.name}' is down and restart is disabled")
					break
				if self.restarts >= self.policy.max_restarts:
					self.LOGGER.critical(
						f"Worker '{self.name}' crashed {self.crashes} time(s) and reached the restart limit "
						f"({self.policy.max_restarts}); giving up"
					)
					break
				self.restarts += 1
				self.LOGGER.warning(f"Restarting worker '{self.name}' in {delay:g}s ({self.restarts}/{self.policy.max_restarts})")
				if self._stop_event.wait(delay):  # interruptible backoff
					self.LOGGER.info(f"Worker '{self.name}' stopped during backoff, not restarting")
					break
				delay = min(delay * 2, self.policy.max_backoff_seconds)
		finally:
			self._finished.set()

	def is_alive(self) -> bool:
		return self._thread.is_alive()

	def is_finished(self) -> bool:
		"""True once the worker returned, exited, gave up, or was stopped — restarts excluded."""
		return self._finished.is_set()

	def request_stop(self) -> None:
		"""Ask the worker to wind down. Never blocks, never kills."""
		self._stop_event.set()
		stop = getattr(self.worker, "stop", None)
		if not callable(stop):
			self.LOGGER.warning(f"Worker '{self.name}' has no stop(), it can only be waited on")
			return
		try:
			stop()
		except Exception:
			self.LOGGER.exception(f"Worker '{self.name}' raised while stopping")

	def join(self, timeout: Optional[float] = None) -> None:
		self._thread.join(timeout)

	def __repr__(self) -> str:
		return (f"SupervisedWorker({self.name!r}, {type(self.worker).__name__}, "
				f"restarts={self.restarts}/{self.policy.max_restarts}, crashes={self.crashes})")


class Supervisor:
	"""Owns the worker threads: starts them, watches them, winds them down.

	Not a singleton — `Main` holds the one instance for the process.
	"""
	LOGGER: Logger
	policy: RestartPolicy
	workers: list[SupervisedWorker]

	def __init__(self, policy: Optional[RestartPolicy] = None) -> None:
		self.LOGGER = getLogger(self.__class__.__name__)
		self.policy = policy or RestartPolicy()
		self.workers = []

	def supervise(self, worker: Any, method_name: str, policy: Optional[RestartPolicy] = None) -> SupervisedWorker:
		supervised = SupervisedWorker(worker, method_name, policy or self.policy, self.LOGGER)
		self.workers.append(supervised)
		supervised.start()
		self.LOGGER.info(f"Thread {supervised.name} started")
		self.LOGGER.debug(f"{supervised!r}")
		return supervised

	def supervise_all(self, workers: list[Any], method_name: str, policy: Optional[RestartPolicy] = None) -> list[SupervisedWorker]:
		"""Supervise each worker, in order.

		A list, not a set, for the reason `main.create_classes` documents: set iteration
		over instances is id-ordered, so startup order varied between runs of the same
		config.
		"""
		return [self.supervise(worker, method_name, policy) for worker in workers]

	def stop_all(self, timeout: float) -> list[SupervisedWorker]:
		"""Ask every worker to stop, then join them within one shared grace period.

		Returns the workers still running when the period expired — they are
		daemon threads and die with the interpreter, so the caller reports them
		rather than hanging on them.
		"""
		for worker in self.workers:
			worker.request_stop()
		deadline = monotonic() + timeout
		for worker in self.workers:
			worker.join(max(0.0, deadline - monotonic()))
		return [worker for worker in self.workers if worker.is_alive()]
