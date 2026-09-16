from threading import Event


class Stoppable:
	"""Cooperative-stop machinery shared by connectors and algorithms.

	The framework never kills a worker thread: it *asks* it to stop and joins it
	with a bounded grace period (see `supervisor/supervisor.py` and `main.py`).
	A blocking `run` method therefore has two obligations:

	1. Poll `is_stopping()` in its loop condition.
	2. Sleep through `wait_stop(seconds)` rather than `time.sleep(seconds)`, so a
	   stop request interrupts the sleep instead of waiting it out.

	Override `stop()` when merely setting the event is not enough to unblock the
	worker — e.g. a transport blocked in a foreign event loop must also be
	disconnected. Always call `super().stop()` first.
	"""
	_stop_event: Event

	def __init__(self) -> None:
		self._stop_event = Event()

	def stop(self) -> None:
		"""Request a graceful stop. Idempotent, safe to call from any thread."""
		self._stop_event.set()

	def is_stopping(self) -> bool:
		return self._stop_event.is_set()

	def wait_stop(self, timeout: float | None = None) -> bool:
		"""Interruptible sleep. Returns True if a stop was requested, False on timeout."""
		return self._stop_event.wait(timeout)
